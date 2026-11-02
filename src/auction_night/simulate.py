"""Simulate whole nights with rule bots only, to check the economy of the game.

No model is called and nothing is written. A night is dealt from a seed, played to the final
scores through :func:`~auction_night.engine.night.start` and
:func:`~auction_night.engine.night.step` by rule bots, and summarised. Many nights, each with
its own seed, give averages with intervals. This is how the budget scale, the mission bonus and
the pacing switch of the economist are tuned.

Who sits where
--------------

* The ``bot`` seats of the configuration are the bots that sit at a real table: the economist
  (``P`` when it paces, ``E`` when it does not) and the truthful bot (``T``).
* Every other seat stands in for a model. It gets a style bot (see
  :mod:`auction_night.bots.styles`): ``E``, ``O`` and ``S`` in turn, so six model seats are two of
  each. The styles are dealt to the seats at random with a stream of the night seed, so no style
  is tied to a seat number, a speaking position or a mission.
* With ``chase`` on (the default), a stand-in that holds a mission goes after it (see
  :mod:`auction_night.bots.chasers`). With it off, the missions are ignored and the stand-ins
  only bid by style. The bots of the table never have a mission.

The configuration cannot describe the style bots or the pursuit of a mission, so this module
builds them with :func:`auction_night.bots.make_styled_bot` and not with
:func:`auction_night.bots.make_bot`.

What is reported
----------------

For every style the average surplus of a seat, and how often a seat spent at least nine tenths of
its budget. For the pairs ``P-E`` and ``P-S`` the average of the differences within a night. For
every kind of mission how often it was completed and the average bonus per holder. An interval is
the mean plus and minus 1.96 standard errors over nights.

The result does not depend on the number of processes.
"""

import json
import multiprocessing
import re
import statistics
from collections.abc import Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from typing import Any, Final, NamedTuple

from auction_night.bots import ChaserBot, RuleBot, make_bot, make_styled_bot
from auction_night.bots.styles import Style
from auction_night.contracts.config import NightConfig
from auction_night.contracts.events import SEED_PATTERN, HammerEvent, Mission
from auction_night.engine.deal import deal
from auction_night.engine.night import Answer, start, step
from auction_night.engine.rng import Stream

MODEL_STYLES: Final[tuple[Style, ...]] = ("E", "O", "S")
STYLE_ORDER: Final[tuple[str, ...]] = ("E", "O", "S", "P", "T")
STYLE_LEGEND: Final = (
    "E textbook bid, O 5/4 of it, S 7/10 of it, P economist with pacing, T truthful bot"
)
PAIRS: Final[tuple[tuple[str, str], ...]] = (("P", "E"), ("P", "S"))
MISSION_KINDS: Final[tuple[str, ...]] = ("collector", "shill")
Z95: Final = 1.96
DEFAULT_NIGHTS: Final = 1000  # nights of a run without a preset
DEFAULT_SEED: Final = "simulation"
MAX_SEED_LENGTH: Final = 128  # the longest seed the deal accepts


__all__ = [
    "DEFAULT_NIGHTS",
    "DEFAULT_SEED",
    "PRESETS",
    "Interval",
    "MissionRow",
    "NightResult",
    "PairRow",
    "Preset",
    "Report",
    "SeatResult",
    "SimulationError",
    "StyleRow",
    "format_report",
    "get_preset",
    "interval",
    "night_seed",
    "play_night",
    "play_nights",
    "preset_config",
    "run_simulation",
    "seat_table",
    "summarize",
]


class SimulationError(ValueError):
    """The simulation cannot run as asked (an unknown preset, a seed that is not a seed)."""


# ------------------------------------------------------------------------------- presets


@dataclass(frozen=True, slots=True)
class Preset:
    """A named setup: budget scale, number of nights and the seed the nights are numbered from."""

    name: str
    c: int
    nights: int
    seed: str


# The two runs of the calibration that these presets repeat (see tests/sim): the table of
# average surplus per style and the paired differences at budget scale 300, and the completion
# of the missions at the same scale. Both play 3000 nights; they differ in the seed, so the
# second is an independent sample.
PRESETS: Final[Mapping[str, Preset]] = {
    "s1-c300": Preset("s1-c300", c=300, nights=3000, seed="s1"),
    "s2": Preset("s2", c=300, nights=3000, seed="s2"),
}


def preset_config(c: int = 300) -> NightConfig:
    """The table of the calibration runs: six model seats, the truthful bot and a pacing economist.

    Everything but the budget scale ``c`` is the default of the contract: twelve lots in all six
    formats, tiers 100, 200 and 400, three collectors and three shills, a bonus of 150.
    """
    seats: list[dict[str, Any]] = [
        {"seat": number, "codename": f"选手{number}", "kind": "mock", "mock_policy": "honest"}
        for number in range(1, 7)
    ]
    seats.append({"seat": 7, "codename": "老实人", "kind": "bot", "policy": "truthful"})
    seats.append(
        {"seat": 8, "codename": "经济学家", "kind": "bot", "policy": "economist", "pacing": True}
    )
    return NightConfig.model_validate(
        {
            "night": {"id": "simulation", "seed": "simulation", "budget": {"c": c}},
            "seats": seats,
            "show": {"host_offline": {"kind": "mock"}, "judges": [{"kind": "mock"}] * 2},
        }
    )


def get_preset(name: str) -> Preset:
    """The preset of that name, or a :class:`SimulationError` that lists the names."""
    try:
        return PRESETS[name]
    except KeyError:
        raise SimulationError(
            f"unknown preset {name!r}: use one of {', '.join(sorted(PRESETS))}"
        ) from None


# ---------------------------------------------------------------------------- one night


class SeatResult(NamedTuple):
    """What one seat did over a night: its style, its books and its mission."""

    seat: int
    style: str
    budget: int
    surplus: int
    spent: int
    mission: str | None
    completed: bool
    bonus: int

    @property
    def spent_most(self) -> bool:
        """True when the seat spent at least nine tenths of its budget."""
        return 10 * self.spent >= 9 * self.budget


class NightResult(NamedTuple):
    """A played night, reduced to what the report needs."""

    seed: str
    seats: tuple[SeatResult, ...]


def night_seed(base: str, index: int) -> str:
    """The seed of night number ``index`` of a run with seed ``base``."""
    return f"{base}:{index}"


def _bot_style(policy: str, pacing: bool) -> str:
    if policy == "truthful":
        return "T"
    return "P" if pacing else "E"


def seat_table(
    config: NightConfig, seed: str, tags: tuple[str, ...], missions: Mapping[int, Mission] | None
) -> tuple[dict[int, RuleBot], dict[int, str]]:
    """The bot and the style label of every seat of a night.

    ``missions`` maps a seat to the mission it holds and is None when the missions are ignored.
    """
    bots: dict[int, RuleBot] = {}
    labels: dict[int, str] = {}
    stand_ins = sorted(seat.seat for seat in config.model_seats)
    cycle = [MODEL_STYLES[k % len(MODEL_STYLES)] for k in range(len(stand_ins))]
    for seat, style in zip(stand_ins, Stream(seed, "simulate.style").shuffle(cycle), strict=True):
        mission = None if missions is None else missions.get(seat)
        bots[seat] = make_styled_bot(style, seat, mission=mission, tags=tags)
        labels[seat] = style
    for seat_config in config.seats:
        if seat_config.kind == "bot":
            assert seat_config.policy is not None
            bots[seat_config.seat] = make_bot(seat_config)
            labels[seat_config.seat] = _bot_style(seat_config.policy, seat_config.pacing)
    return bots, labels


def play_night(config: NightConfig, seed: str, *, chase: bool = True) -> NightResult:
    """Deal the night of ``seed``, play it with rule bots and return how every seat fared."""
    the_deal = deal(config, seed=seed)
    tags = tuple(lot.tag for lot in the_deal.lots)
    bots, labels = seat_table(config, seed, tags, the_deal.missions if chase else None)
    chasers = tuple(seat for seat, bot in bots.items() if isinstance(bot, ChaserBot))

    state, request = start(config, the_deal)
    while request is not None:
        if request.phase == "plan":
            blank = (0,) * request.n_lots
            answers = {
                seat: Answer(seat=seat, priorities=blank, budget_plan=blank)
                for seat in request.seats
            }
        else:
            answers = {seat: bots[seat].answer(request) for seat in request.seats}
        state, events, request = step(state, answers)
        for event in events:
            if isinstance(event, HammerEvent):
                assert event.lot is not None
                for seat in chasers:
                    bot = bots[seat]
                    assert isinstance(bot, ChaserBot)
                    bots[seat] = bot.observe(event.lot, event.data)

    held = {result.seat: result for result in state.missions}
    seats = []
    for score in state.scores:
        mission = held.get(score.seat)
        seats.append(
            SeatResult(
                seat=score.seat,
                style=labels[score.seat],
                budget=the_deal.budgets[score.seat],
                surplus=score.surplus,
                spent=score.spent,
                mission=None if mission is None else mission.kind,
                completed=mission is not None and mission.completed,
                bonus=score.mission_bonus,
            )
        )
    return NightResult(seed=seed, seats=tuple(seats))


def _play_chunk(args: tuple[NightConfig, tuple[str, ...], bool]) -> list[NightResult]:
    config, seeds, chase = args
    return [play_night(config, seed, chase=chase) for seed in seeds]


def play_nights(
    config: NightConfig, seeds: Sequence[str], *, jobs: int = 1, chase: bool = True
) -> list[NightResult]:
    """Play one night per seed, in order. ``jobs`` processes share the work; one means none.

    Workers are started with ``spawn``, so a script that asks for more than one must run its
    entry point under ``if __name__ == "__main__":``.
    """
    if jobs < 1:
        raise SimulationError("jobs must be at least 1")
    if jobs == 1 or len(seeds) < 2:
        return _play_chunk((config, tuple(seeds), chase))
    size = -(-len(seeds) // (4 * jobs))
    chunks = [(config, tuple(seeds[i : i + size]), chase) for i in range(0, len(seeds), size)]
    with ProcessPoolExecutor(
        max_workers=jobs, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        return [night for part in pool.map(_play_chunk, chunks) for night in part]


# --------------------------------------------------------------------------------- report


@dataclass(frozen=True, slots=True)
class Interval:
    """A mean over nights with the half-width of its 95% interval (None for a single night)."""

    mean: float
    half: float | None

    @property
    def low(self) -> float | None:
        """Lower end of the interval."""
        return None if self.half is None else self.mean - self.half

    @property
    def high(self) -> float | None:
        """Upper end of the interval."""
        return None if self.half is None else self.mean + self.half

    def to_dict(self) -> dict[str, float | None]:
        """Plain numbers for JSON."""
        return {"mean": self.mean, "half": self.half, "low": self.low, "high": self.high}


def interval(values: Sequence[float]) -> Interval:
    """The mean of ``values`` and 1.96 standard errors of it (the sample standard deviation)."""
    mean = float(statistics.mean(values))
    if len(values) < 2:
        return Interval(mean, None)
    return Interval(mean, Z95 * statistics.stdev(values) / len(values) ** 0.5)


@dataclass(frozen=True, slots=True)
class StyleRow:
    """One style: seats per night, average surplus and how often a seat spent most of its budget."""

    style: str
    seats: int
    surplus: Interval
    spent_90: Interval


@dataclass(frozen=True, slots=True)
class PairRow:
    """The average within-night difference of the surplus of two styles."""

    first: str
    second: str
    difference: Interval


@dataclass(frozen=True, slots=True)
class MissionRow:
    """One kind of mission: holders over all nights, completion rate and bonus per holder."""

    kind: str
    holders: int
    completed: Interval
    bonus: Interval


@dataclass(frozen=True, slots=True)
class Report:
    """The summary of a run."""

    source: str
    seed: str
    nights: int
    chase: bool
    styles: tuple[StyleRow, ...]
    pairs: tuple[PairRow, ...]
    missions: tuple[MissionRow, ...]

    def to_dict(self) -> dict[str, Any]:
        """The report as plain data for JSON."""
        return {
            "source": self.source,
            "seed": self.seed,
            "nights": self.nights,
            "chase": self.chase,
            "styles": {
                row.style: {
                    "seats": row.seats,
                    "surplus": row.surplus.to_dict(),
                    "spent_90": row.spent_90.to_dict(),
                }
                for row in self.styles
            },
            "pairs": {f"{row.first}-{row.second}": row.difference.to_dict() for row in self.pairs},
            "missions": {
                row.kind: {
                    "holders": row.holders,
                    "completed": row.completed.to_dict(),
                    "bonus": row.bonus.to_dict(),
                }
                for row in self.missions
            },
        }

    def to_json(self) -> str:
        """The report as JSON text."""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


def _per_night(nights: Sequence[NightResult], style: str) -> tuple[list[float], list[float], int]:
    """Per night: mean surplus and share of seats that spent most of their budget, in a style."""
    surplus: list[float] = []
    spent: list[float] = []
    seats = 0
    for night in nights:
        own = [seat for seat in night.seats if seat.style == style]
        seats = max(seats, len(own))
        surplus.append(statistics.mean(seat.surplus for seat in own))
        spent.append(statistics.mean(1.0 if seat.spent_most else 0.0 for seat in own))
    return surplus, spent, seats


def summarize(nights: Sequence[NightResult], *, source: str, seed: str, chase: bool) -> Report:
    """Reduce played nights to a report. Needs at least one night."""
    if not nights:
        raise SimulationError("no nights to summarize")
    present = {seat.style for night in nights for seat in night.seats}
    styles: list[StyleRow] = []
    surplus_of: dict[str, list[float]] = {}
    for style in (s for s in STYLE_ORDER if s in present):
        surplus, spent, seats = _per_night(nights, style)
        surplus_of[style] = surplus
        styles.append(StyleRow(style, seats, interval(surplus), interval(spent)))
    pairs = [
        PairRow(a, b, interval([x - y for x, y in zip(surplus_of[a], surplus_of[b], strict=True)]))
        for a, b in PAIRS
        if a in surplus_of and b in surplus_of
    ]
    missions: list[MissionRow] = []
    for kind in MISSION_KINDS:
        held = [seat for night in nights for seat in night.seats if seat.mission == kind]
        if held:
            done = [1.0 if seat.completed else 0.0 for seat in held]
            bonus = [float(seat.bonus) for seat in held]
            missions.append(MissionRow(kind, len(held), interval(done), interval(bonus)))
    return Report(
        source=source,
        seed=seed,
        nights=len(nights),
        chase=chase,
        styles=tuple(styles),
        pairs=tuple(pairs),
        missions=tuple(missions),
    )


def run_simulation(
    config: NightConfig,
    *,
    nights: int,
    seed: str,
    jobs: int = 1,
    chase: bool = True,
    source: str | None = None,
) -> Report:
    """Play ``nights`` nights of ``config`` with rule bots and summarise them.

    Night ``k`` is dealt from the seed ``"<seed>:<k>"``, so the same arguments give the same
    report on every machine, whatever the number of ``jobs``.
    """
    if nights < 1:
        raise SimulationError("nights must be at least 1")
    last = night_seed(seed, nights - 1)
    if not seed or len(last) > MAX_SEED_LENGTH or not re.fullmatch(SEED_PATTERN, last):
        raise SimulationError(
            "the seed is not empty, may hold the characters A-Z a-z 0-9 _ . : - and, with the "
            f"night number appended, has at most {MAX_SEED_LENGTH} characters"
        )
    seeds = [night_seed(seed, k) for k in range(nights)]
    played = play_nights(config, seeds, jobs=jobs, chase=chase)
    return summarize(played, source=source or config.night.id, seed=seed, chase=chase)


# ---------------------------------------------------------------------------------- table


def _signed(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.1f}"


def _plain(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}"


def format_report(report: Report) -> str:
    """The report as plain text tables."""
    missions = "chased" if report.chase else "ignored"
    lines = [
        f"{report.source}: {report.nights} nights, seed {report.seed}, missions {missions}",
        "",
        "style  seats  mean surplus  95% half-width  spent >= 90% of budget",
    ]
    for row in report.styles:
        lines.append(
            f"{row.style:<5}  {row.seats:>5}  {row.surplus.mean:>12.1f}  "
            f"{_plain(row.surplus.half):>14}  {row.spent_90.mean:>22.2f}"
        )
    lines.append(STYLE_LEGEND)
    if report.pairs:
        lines += ["", "mean surplus difference within a night, with its 95% interval"]
        for pair in report.pairs:
            diff = pair.difference
            interval_text = f"[{_signed(diff.low)}, {_signed(diff.high)}]"
            lines.append(f"{pair.first}-{pair.second}  {_signed(diff.mean)} {interval_text}")
    if report.missions:
        lines += ["", "mission     holders  completed  mean bonus per holder  95% half-width"]
        for row in report.missions:
            lines.append(
                f"{row.kind:<10}  {row.holders:>7}  {row.completed.mean:>9.3f}  "
                f"{row.bonus.mean:>21.1f}  {_plain(row.bonus.half):>14}"
            )
    return "\n".join(lines)
