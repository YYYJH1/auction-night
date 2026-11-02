"""Season leaderboard: mean surplus, rank intervals and a footer, recomputed from the logs.

A season is a set of finished nights. A night is a directory that holds the event log that
``auction-night run`` writes (``events.jsonl``) and, next to it, a snapshot of the configuration
the night was run with (``config.yaml``). The board is a pure function of those files, a seed and
a number of resamples, so anyone can recompute it. ``tools/check_leaderboard.py`` does so in CI
and compares the bytes with the published ``leaderboard.json``.

What is read from where
-----------------------
The event log holds the scores (``night_end``), the status of the night, the missions
(``mission_result``), every action (``action``) and the seed commitment with the deck
(``seed_commit``). It does not name the models, the season or the rotation, so these come from
the snapshot of the configuration (``season.id``, ``season.format``, ``season.deck``,
``season.rotation`` and the model of each seat). Without a snapshot the model of a seat is read
from the first call of that seat in ``calls.jsonl``, the deck from the ``seed_commit`` event, and
the season is taken to be single-night. A night directory that holds ``human.jsonl`` had a person
at the table.

Which nights count
------------------
Only a night whose ``night_end`` says ``completed`` counts, and only if nobody sat at the table
and the night was played in plain mode (``night_start`` says which). The status is read from
``night_end`` and never inferred from the actions. Every other night is left out and listed with
its reason (``budget_aborted``, ``human``, ``variety``, ``no_night_end``). The lineup must be the
same on every night that counts, and so must the season and its format.

The board
---------
An entry is a model (named by its model id) or a bot (named by its policy). Bots are ranked with
the models and marked as bots. For an entry ``e`` let ``S_e`` be the surplus of its seats summed
over the nights that count and ``N_e`` the number of those seat-nights. The score is
``S_e / N_e``: the mean surplus per night, which is per seat and night when an entry holds more
than one seat. Mission bonus is not part of it. Entries are ordered by score, best first, and
equal scores share the best rank (1, 2, 2, 4); among them the order is that of the entry names.
Bonus is kept apart in the mission board: missions held, completed, and the mean bonus per night.

Reference columns do not take part in the ranking. Two are computed from the log: the abstain
rate (share of actions that ended in an abstention other than a refusal, a kept English exit price
included) and the refusal rate, with the abstentions counted by reason. A seat that was never asked
is not an action. Columns that need the theory module (the excess over the textbook bid) or the
show layer plug in as :class:`ReferenceColumn`: a function from the events of a night to one
integer per seat. The board shows the mean of it per seat-night.

Rank intervals
--------------
The unit of the bootstrap follows the format of the season. In a rotation season it is the deck:
the nights of one deck are drawn together or not at all. In a single-night season it is the night.
Number the ``m`` units in a fixed order (decks and nights by their ids). Resample ``b`` of ``B``
draws ``m`` units with replacement, with the hash-based stream ``Stream(seed, "season.resample",
b)`` (SHA-256, the same on every platform and Python version). In it the score of an entry is
``sum S / sum N`` over the drawn units, and its rank is one plus the number of entries with a
strictly larger score; the scores are ratios of integers and are compared exactly. The interval of
an entry is ``[r_lo, r_hi]`` where ``r_p`` is the smallest rank ``r`` such that at least a share
``p`` of the resamples ranks the entry at ``r`` or better, for ``p = 0.025`` and ``p = 0.975``
(the inverted empirical distribution function). The 95% interval of the mean uses the same rule on
the sorted resampled scores. With fewer than two units there is nothing to resample and the
intervals are absent. A bootstrap over few units is optimistic: read the footer.

Footer
------
The footer says how large a difference between two models must be before the nights played so far
can tell it apart. For two entries ``i`` and ``j`` and a unit ``u`` let ``d_u = x_iu - x_ju`` with
``x_eu = S_eu / N_eu``, and let ``s_ij^2`` be the sample variance of ``d_u`` over the ``m`` units
(divisor ``m - 1``). With ``sigma^2`` the mean of ``s_ij^2`` over all pairs of model entries::

    X = (z_0.975 + z_0.80) * sigma / sqrt(m)

``X`` is the smallest true difference per night that a paired comparison over ``m`` units finds
with probability 80% at a two-sided level of 5%. Smaller differences are mostly not told apart.
In a rotation season the paired difference is taken between two models on the same deck, which
removes the luck of the cards.

The JSON
--------
``schema`` is ``auction-night/leaderboard@1``. ``season`` holds the id, the format, the unit of
the resampling (``deck`` or ``night``), the number of units, the nights that count, the nights
that were left out with their reason and, for a rotation season, the decks with their rotations.
``method`` records the metric, the number of resamples, the seed and the rule for ties.
``entries`` is the main board, best first: ``rank`` (shared by equal scores), ``rank_low`` and
``rank_high``, ``mean_milli`` with ``mean_low_milli`` and ``mean_high_milli``, ``nights``,
``seat_nights``, ``actions``, ``abstain_rate_milli``, ``refusal_rate_milli``, ``abstentions`` by
reason and the reference ``columns``. ``missions`` is the mission board, ``nights`` lists the
nights that count with their unit, and ``footer`` holds the gap, the standard deviation it comes
from and the sentences with their basis, in English and in Chinese. What cannot be computed is
null.

Determinism
-----------
The JSON holds no floats: means, rates and the gap are integer thousandths, rounded half to even
from exact fractions. Keys come in a fixed order and the text ends with a newline, so the same
logs, seed and number of resamples give the same bytes on every machine.
"""

import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction
from itertools import combinations
from operator import itemgetter
from pathlib import Path
from typing import Any, Final, Literal, get_args

import yaml

from auction_night.contracts.config import NightConfig, load_config
from auction_night.contracts.events import (
    AbstainReason,
    ActionEvent,
    AnyEvent,
    MissionResultEvent,
    NightEndEvent,
    NightStartEvent,
    SeedCommitEvent,
    canonical_json,
    parse_event,
    validate_log,
)
from auction_night.engine.rng import Stream

__all__ = [
    "ABSTAIN_REASONS",
    "CALLS_FILE",
    "CONFIG_FILE",
    "DEFAULT_RESAMPLES",
    "DEFAULT_SEED",
    "EVENTS_FILE",
    "HUMAN_FILE",
    "LEADERBOARD_FILE",
    "SCHEMA_ID",
    "NightRecord",
    "ReferenceColumn",
    "SeasonError",
    "SeatRecord",
    "build_leaderboard",
    "discover_night_dirs",
    "leaderboard_for",
    "leaderboard_json",
    "leaderboard_text",
    "read_night",
]

SCHEMA_ID: Final = "auction-night/leaderboard@1"
EVENTS_FILE: Final = "events.jsonl"  # the same names as llm.nightrun, which is not imported
CALLS_FILE: Final = "calls.jsonl"  # here because it pulls in the model clients
HUMAN_FILE: Final = "human.jsonl"
CONFIG_FILE: Final = "config.yaml"
LEADERBOARD_FILE: Final = "leaderboard.json"

DEFAULT_RESAMPLES: Final = 10_000
DEFAULT_SEED: Final = "season-leaderboard"
MAX_SEED_LENGTH: Final = 128
RESAMPLE_PURPOSE: Final = "season.resample"
INTERVAL_PER_MILLE: Final = (25, 975)  # the 2.5% and 97.5% quantiles: a 95% interval
Z_ALPHA: Final = 1.959963984540054  # standard normal quantile at 0.975 (two-sided level 5%)
Z_POWER: Final = 0.8416212335729143  # standard normal quantile at 0.80 (power 80%)
ALPHA_PER_MILLE: Final = 50
POWER_PER_MILLE: Final = 800

ABSTAIN_REASONS: Final[tuple[str, ...]] = (*get_args(AbstainReason), "kept_previous")
_NOT_AN_ACTION: Final = "not_asked"
_RANGE_DASH: Final = "–"
_KINDS: Final = ("collector", "shill")

Format = Literal["single", "rotation"]


class SeasonError(ValueError):
    """The nights cannot be made into a leaderboard. The message is for the person at the
    command line."""


# ------------------------------------------------------------------------------- records


@dataclass(frozen=True, slots=True)
class ReferenceColumn:
    """A reference column that is computed from the events of one night.

    ``compute`` gets the events of a night that counts and returns one integer for every seat of
    it. The board shows the mean of the integers per seat-night, in thousandths, and never
    ranks by it.
    """

    name: str
    label: str
    compute: Callable[[Sequence[AnyEvent]], Mapping[int, int]]


@dataclass(frozen=True, slots=True)
class SeatRecord:
    """One seat of one night that counts: who sat there and what the log says it did."""

    seat: int
    codename: str
    entry: str
    label: str
    kind: Literal["model", "bot"]
    surplus: int
    bonus: int
    actions: int
    abstentions: Mapping[str, int]
    mission: str | None
    mission_done: bool
    columns: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NightRecord:
    """What the season needs from one night. ``seats`` is empty for a night that is left out."""

    night: str
    source: str
    status: str | None
    season_id: str | None
    season_format: Format | None
    deck: str | None
    rotation: int | None
    human: bool
    players: int
    seats: tuple[SeatRecord, ...]
    variety: bool = False

    @property
    def exclusion(self) -> str | None:
        """Why the night does not count, or None when it does."""
        if self.human:
            return "human"
        if self.variety:
            return "variety"
        if self.status is None:
            return "no_night_end"
        return None if self.status == "completed" else self.status

    @property
    def counts(self) -> bool:
        """True when the night enters the board."""
        return self.exclusion is None


# --------------------------------------------------------------------------- reading a night


def discover_night_dirs(paths: Sequence[Path]) -> list[Path]:
    """The night directories behind ``paths``.

    A path that holds ``events.jsonl`` is a night. Any other directory is a season directory:
    every directory under it that holds ``events.jsonl`` is a night. Each night is listed once.
    """
    found: dict[Path, Path] = {}
    for given in paths:
        path = Path(given)
        if not path.is_dir():
            raise SeasonError(f"not a directory: {path}")
        if (path / EVENTS_FILE).is_file():
            nights = [path]
        else:
            nights = sorted(log.parent for log in path.rglob(EVENTS_FILE))
        if not nights:
            raise SeasonError(f"no {EVENTS_FILE} in or under {path}")
        for night in nights:
            found.setdefault(night.resolve(), night)
    return sorted(found.values(), key=str)


def _brief(exc: Exception) -> str:
    """The first lines of an error, on one line: enough to find the problem."""
    return " ".join(line.strip() for line in str(exc).strip().splitlines()[:2]) or "unreadable"


def _read_events(path: Path) -> list[AnyEvent]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise SeasonError(f"cannot read {path}: {exc}") from None
    events: list[AnyEvent] = []
    for number, line in enumerate(text.split("\n"), start=1):
        if not line.strip():
            continue
        try:
            events.append(parse_event(line))
        except ValueError as exc:
            raise SeasonError(
                f"{path}: line {number} is not a valid event ({_brief(exc)})"
            ) from None
    problems = validate_log(events)
    if problems:
        raise SeasonError(f"{path}: {problems[0]}")
    return events


def _read_config(directory: Path, start: NightStartEvent) -> NightConfig | None:
    path = directory / CONFIG_FILE
    if not path.is_file():
        return None
    try:
        config = load_config(path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise SeasonError(f"cannot read {path}: {_brief(exc)}") from None
    data = start.data
    if config.night.id != data.night_id:
        raise SeasonError(
            f"{path} is the configuration of night {config.night.id!r}, "
            f"but the log is of night {data.night_id!r}"
        )
    configured = {seat.seat: seat for seat in config.seats}
    logged = {seat.seat: seat for seat in data.seats}
    same = set(configured) == set(logged) and all(
        configured[n].codename == logged[n].codename
        and (configured[n].kind == "bot") == (logged[n].role == "bot")
        for n in logged
    )
    if not same:
        raise SeasonError(f"{path} does not describe the seats of the log in {directory}")
    return config


def _models_from_calls(path: Path, seats: set[int]) -> dict[int, str]:
    """The model id of each seat, from the first call of the seat in the cassette."""
    found: dict[int, str] = {}
    if not path.is_file():
        return found
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                role, model = row.get("role"), row.get("model")
                match = re.fullmatch(r"seat:(\d+)", role) if isinstance(role, str) else None
                if match and isinstance(model, str) and int(match.group(1)) in seats:
                    found.setdefault(int(match.group(1)), model)
                if found.keys() >= seats:
                    break
    except OSError as exc:
        raise SeasonError(f"cannot read {path}: {exc}") from None
    return found


def _identities(
    directory: Path, start: NightStartEvent, config: NightConfig | None
) -> dict[int, tuple[str, str, Literal["model", "bot"]]]:
    """Entry key, label and kind of every seat. A model is named by its model id and a bot by
    its policy; without a configuration a bot is named by its code name."""
    seats = {seat.seat: seat for seat in start.data.seats}
    configured = {} if config is None else {seat.seat: seat for seat in config.seats}
    players = {number for number, seat in seats.items() if seat.role == "player"}
    from_calls: dict[int, str] | None = None
    out: dict[int, tuple[str, str, Literal["model", "bot"]]] = {}
    for number, seat in sorted(seats.items()):
        known = configured.get(number)
        if seat.role == "bot":
            policy = None if known is None else known.policy
            out[number] = (f"bot:{policy or seat.codename}", seat.codename, "bot")
            continue
        model: str | None = None
        if known is not None and known.kind == "llm":
            model = known.model
        elif known is not None and known.kind == "mock":
            model = f"mock-{known.mock_policy}"
        if model is None:
            if from_calls is None:
                from_calls = _models_from_calls(directory / CALLS_FILE, players)
            model = from_calls.get(number)
        if model is None:
            raise SeasonError(
                f"{directory}: cannot tell which model sat at seat {number}; "
                f"put the configuration of the night there as {CONFIG_FILE}"
            )
        out[number] = (model, model, "model")
    return out


def _logged_deck(events: Sequence[AnyEvent], source: str) -> str | None:
    decks = {
        event.data.deck_id
        for event in events
        if isinstance(event, SeedCommitEvent) and event.data.deck_id is not None
    }
    if len(decks) > 1:
        raise SeasonError(f"{source}: the log names more than one deck")
    return next(iter(decks), None)


def _tally_actions(events: Sequence[AnyEvent]) -> tuple[Counter[int], dict[int, Counter[str]]]:
    actions: Counter[int] = Counter()
    reasons: dict[int, Counter[str]] = defaultdict(Counter)
    for event in events:
        if not isinstance(event, ActionEvent):
            continue
        data = event.data
        reason = data.reason if data.status == "abstain" else None
        if data.status == "kept_previous":
            reason = "kept_previous"
        if reason is not None:
            reasons[data.seat][reason] += 1
        if reason != _NOT_AN_ACTION:
            actions[data.seat] += 1
    return actions, reasons


def read_night(directory: Path, columns: Sequence[ReferenceColumn] = ()) -> NightRecord:
    """Read one night directory.

    ``events.jsonl`` is required and must be a valid log. ``config.yaml`` and, failing that,
    ``calls.jsonl`` say who sat where. A night that does not count is read for its status and
    its place in the season only.
    """
    directory = Path(directory)
    source = str(directory)
    events = _read_events(directory / EVENTS_FILE)
    start = events[0]
    assert isinstance(start, NightStartEvent)
    ends = [event for event in events if isinstance(event, NightEndEvent)]
    config = _read_config(directory, start)

    deck = None if config is None else config.season.deck
    logged = _logged_deck(events, source)
    if deck is not None and logged is not None and deck != logged:
        raise SeasonError(f"{source}: the configuration and the log name different decks")
    human = (directory / HUMAN_FILE).is_file() or (config is not None and config.has_human)
    status = ends[0].data.status if ends else None
    players = sum(1 for seat in start.data.seats if seat.role == "player")
    record = NightRecord(
        night=start.data.night_id,
        source=source,
        status=status,
        season_id=None if config is None else config.season.id,
        season_format=None if config is None else config.season.format,
        deck=deck if deck is not None else logged,
        rotation=None if config is None else config.season.rotation,
        human=human,
        players=players,
        seats=(),
        variety=start.data.mode == "variety",
    )
    if not record.counts:
        return record
    assert ends
    seats = _seat_records(directory, events, start, ends[0], config, columns)
    return replace(record, seats=seats)


def _seat_records(
    directory: Path,
    events: Sequence[AnyEvent],
    start: NightStartEvent,
    end: NightEndEvent,
    config: NightConfig | None,
    columns: Sequence[ReferenceColumn],
) -> tuple[SeatRecord, ...]:
    night = start.data.night_id
    scores = {score.seat: score for score in end.data.scores}
    seats = {seat.seat: seat for seat in start.data.seats}
    if set(scores) != set(seats):
        raise SeasonError(f"{directory}: night_end does not score exactly the seats of the night")
    missions = {
        event.data.seat: event.data for event in events if isinstance(event, MissionResultEvent)
    }
    for number, mission in missions.items():
        if number not in scores or mission.bonus != scores[number].mission_bonus:
            raise SeasonError(
                f"{directory}: the bonus of seat {number} differs between mission_result "
                f"and night_end"
            )
    actions, reasons = _tally_actions(events)
    identities = _identities(directory, start, config)
    extra: dict[str, Mapping[int, int]] = {}
    for column in columns:
        values = column.compute(events)
        if set(values) != set(seats):
            raise SeasonError(f"column {column.name!r} gave no value for every seat of {night}")
        extra[column.name] = values
    out = []
    for number in sorted(seats):
        entry, label, kind = identities[number]
        mission = missions.get(number)
        counts = reasons.get(number, Counter())
        out.append(
            SeatRecord(
                seat=number,
                codename=seats[number].codename,
                entry=entry,
                label=label,
                kind=kind,
                surplus=scores[number].surplus,
                bonus=scores[number].mission_bonus,
                actions=actions.get(number, 0),
                abstentions={reason: counts.get(reason, 0) for reason in ABSTAIN_REASONS},
                mission=None if mission is None else mission.kind,
                mission_done=mission is not None and mission.completed,
                columns={name: values[number] for name, values in extra.items()},
            )
        )
    return tuple(out)


# ------------------------------------------------------------------------ the statistics


def _milli(value: Fraction) -> int:
    """Thousandths of ``value``, rounded half to even."""
    return round(value * 1000)


def _rate_milli(part: int, whole: int) -> int | None:
    return None if whole == 0 else _milli(Fraction(part, whole))


def _quantile_rank(histogram: Sequence[int], total: int, per_mille: int) -> int:
    """The smallest rank ``r`` with at least ``per_mille / 1000`` of the resamples at ``r`` or
    better. ``histogram[r]`` counts the resamples that put the entry at rank ``r``."""
    cumulative = 0
    for rank in range(1, len(histogram)):
        cumulative += histogram[rank]
        if cumulative * 1000 >= per_mille * total:
            return rank
    return len(histogram) - 1


def _order_index(per_mille: int, total: int) -> int:
    """Index of the order statistic that is the ``per_mille / 1000`` quantile (inverted CDF)."""
    return max(0, -(-per_mille * total // 1000) - 1)


@dataclass(frozen=True, slots=True)
class _Intervals:
    rank_low: list[int]
    rank_high: list[int]
    mean_low: list[Fraction]
    mean_high: list[Fraction]


def _bootstrap(
    surplus: Sequence[Sequence[int]],
    seat_nights: Sequence[Sequence[int]],
    resamples: int,
    seed: str,
) -> _Intervals:
    """Resample the units and rank the entries each time. Needs at least two units.

    ``surplus[e][u]`` and ``seat_nights[e][u]`` are ``S`` and ``N`` of entry ``e`` in unit ``u``.
    """
    entries, units = len(surplus), len(surplus[0])
    histograms = [[0] * (entries + 1) for _ in range(entries)]
    sums: list[list[int]] = [[] for _ in range(entries)]
    sizes: list[list[int]] = [[] for _ in range(entries)]
    for b in range(resamples):
        stream = Stream(seed, RESAMPLE_PURPOSE, b)
        pick = itemgetter(*[stream.randbelow(units) for _ in range(units)])
        total_s = [sum(pick(row)) for row in surplus]
        total_n = [sum(pick(row)) for row in seat_nights]
        for e in range(entries):
            sums[e].append(total_s[e])
            sizes[e].append(total_n[e])
            ahead = sum(
                1 for o in range(entries) if total_s[o] * total_n[e] > total_s[e] * total_n[o]
            )
            histograms[e][ahead + 1] += 1
    low_p, high_p = INTERVAL_PER_MILLE
    rank_low = [_quantile_rank(h, resamples, low_p) for h in histograms]
    rank_high = [_quantile_rank(h, resamples, high_p) for h in histograms]
    mean_low, mean_high = [], []
    for e in range(entries):
        ordered = sorted(Fraction(s, n) for s, n in zip(sums[e], sizes[e], strict=True))
        mean_low.append(ordered[_order_index(low_p, resamples)])
        mean_high.append(ordered[_order_index(high_p, resamples)])
    return _Intervals(rank_low, rank_high, mean_low, mean_high)


def _pooled_variance(per_unit: Mapping[str, Sequence[Fraction]], models: Sequence[str]) -> Fraction:
    """The mean over all pairs of models of the sample variance of their per-unit difference."""
    pairs = list(combinations(models, 2))
    units = len(per_unit[models[0]])
    total = Fraction(0)
    for first, second in pairs:
        diffs = [a - b for a, b in zip(per_unit[first], per_unit[second], strict=True)]
        centre = sum(diffs, Fraction(0)) / units
        total += sum(((d - centre) ** 2 for d in diffs), Fraction(0)) / (units - 1)
    return total / len(pairs)


# ------------------------------------------------------------------------ the leaderboard


def _check_method(resamples: int, seed: str) -> None:
    if isinstance(resamples, bool) or not isinstance(resamples, int) or resamples < 1:
        raise SeasonError("the number of resamples must be a whole number of at least 1")
    if not seed or len(seed) > MAX_SEED_LENGTH or re.search(r"[\x00-\x1f\x7f]", seed):
        raise SeasonError(
            f"the seed is text of 1 to {MAX_SEED_LENGTH} characters without control characters"
        )


def _agree(what: str, nights: Sequence[NightRecord], values: Sequence[Any]) -> Any:
    """The one value that all nights share, or None when no night names one."""
    distinct = sorted({v for v in values if v is not None}, key=str)
    if len(distinct) > 1:
        raise SeasonError(
            f"the nights do not agree on the {what}: {', '.join(repr(v) for v in distinct)}"
        )
    if distinct:
        for night, value in zip(nights, values, strict=True):
            if value is None:
                raise SeasonError(f"night {night.night!r} names no {what} while other nights do")
    return distinct[0] if distinct else None


def _units(
    counted: Sequence[NightRecord], season_format: Format
) -> tuple[list[tuple[str, list[NightRecord]]], list[dict[str, Any]]]:
    """The units of the bootstrap with their nights, and the facts about the decks."""
    if season_format == "single":
        return [(night.night, [night]) for night in counted], []
    by_deck: dict[str, list[NightRecord]] = defaultdict(list)
    for night in counted:
        if night.deck is None:
            raise SeasonError(
                f"night {night.night!r} names no deck: a rotation season needs season.deck"
            )
        if night.rotation is None:
            raise SeasonError(
                f"night {night.night!r} has no rotation index: set season.rotation in its "
                f"{CONFIG_FILE}"
            )
        by_deck[night.deck].append(night)
    units, decks = [], []
    for deck in sorted(by_deck):
        members = sorted(by_deck[deck], key=lambda n: (n.rotation or 0, n.night))
        rotations = [n.rotation for n in members]
        if len(set(rotations)) != len(rotations):
            raise SeasonError(f"deck {deck!r} has two nights with the same rotation index")
        complete = sorted(r or 0 for r in rotations) == list(range(members[0].players))
        units.append((deck, members))
        decks.append(
            {"deck": deck, "nights": len(members), "rotations": rotations, "complete": complete}
        )
    return units, decks


@dataclass(frozen=True, slots=True)
class _Season:
    """The nights of a season after the checks: what counts, how it is grouped, who sat."""

    counted: list[NightRecord]
    left_out: list[dict[str, str | None]]
    season_id: str | None
    season_format: Format
    format_known: bool
    units: list[tuple[str, list[NightRecord]]]
    decks: list[dict[str, Any]]
    keys: list[str]
    kind: dict[str, str]
    label: dict[str, str]
    seats: dict[str, list[SeatRecord]]

    @property
    def unit_name(self) -> str:
        return "deck" if self.season_format == "rotation" else "night"


def _assemble(nights: Sequence[NightRecord], columns: Sequence[ReferenceColumn]) -> _Season:
    """Check that the nights form one season and group the ones that count."""
    ordered = sorted(nights, key=lambda night: (night.night, night.source))
    for first, second in zip(ordered, ordered[1:], strict=False):
        if first.night == second.night:
            raise SeasonError(f"night {first.night!r} appears more than once")
    counted = [night for night in ordered if night.counts]
    left_out = [
        {"night": night.night, "reason": night.exclusion} for night in ordered if not night.counts
    ]
    if not counted:
        why = ", ".join(f"{item['night']} ({item['reason']})" for item in left_out)
        raise SeasonError("no night counts" + (f": {why}" if why else ""))

    season_id = _agree("season id", counted, [night.season_id for night in counted])
    named = _agree("season format", counted, [night.season_format for night in counted])
    season_format: Format = named or "single"
    if named is None:
        decks = [night.deck for night in counted if night.deck is not None]
        if len(decks) != len(set(decks)):
            raise SeasonError(
                f"several nights share a deck, but no {CONFIG_FILE} says that the season is a "
                f"rotation: put the configuration of each night next to its log"
            )
    units, deck_facts = _units(counted, season_format)

    keys = sorted({seat.entry for night in counted for seat in night.seats})
    kind: dict[str, str] = {}
    label: dict[str, str] = {}
    seats: dict[str, list[SeatRecord]] = {key: [] for key in keys}
    for night in counted:
        present = {seat.entry for seat in night.seats}
        if present != set(keys):
            differing = ", ".join(sorted(set(keys) ^ present))
            raise SeasonError(f"the lineup changes: night {night.night!r} differs in {differing}")
        for seat in night.seats:
            seats[seat.entry].append(seat)
            kind.setdefault(seat.entry, seat.kind)
            label.setdefault(seat.entry, seat.label)
        for column in columns:
            if any(column.name not in seat.columns for seat in night.seats):
                raise SeasonError(
                    f"night {night.night!r} was read without the column {column.name!r}"
                )
    return _Season(
        counted=counted,
        left_out=left_out,
        season_id=season_id,
        season_format=season_format,
        format_known=named is not None,
        units=units,
        decks=deck_facts,
        keys=keys,
        kind=kind,
        label=label,
        seats=seats,
    )


def _matrices(season: _Season) -> tuple[list[list[int]], list[list[int]]]:
    """``S[e][u]`` and ``N[e][u]``: surplus and seat-nights of entry ``e`` in unit ``u``."""
    row = {key: i for i, key in enumerate(season.keys)}
    surplus = [[0] * len(season.units) for _ in season.keys]
    seat_nights = [[0] * len(season.units) for _ in season.keys]
    for u, (_, members) in enumerate(season.units):
        for night in members:
            for seat in night.seats:
                surplus[row[seat.entry]][u] += seat.surplus
                seat_nights[row[seat.entry]][u] += 1
    return surplus, seat_nights


def _mission_rows(season: _Season) -> list[dict[str, Any]]:
    """The mission board: models only, because bots hold no mission."""
    rows = []
    for key in season.keys:
        if season.kind[key] != "model":
            continue
        seats = season.seats[key]
        held = [seat for seat in seats if seat.mission is not None]
        done = [seat for seat in held if seat.mission_done]
        bonus = sum(seat.bonus for seat in seats)
        rows.append(
            {
                "entry": key,
                "label": season.label[key],
                "nights": len(season.counted),
                "held": len(held),
                "completed": len(done),
                "completion_milli": _rate_milli(len(done), len(held)),
                "by_kind": {
                    kind: {
                        "held": sum(1 for seat in held if seat.mission == kind),
                        "completed": sum(1 for seat in done if seat.mission == kind),
                    }
                    for kind in _KINDS
                },
                "bonus_total": bonus,
                "bonus_mean_milli": _milli(Fraction(bonus, len(seats))),
            }
        )
    rows.sort(key=lambda row: (-row["bonus_mean_milli"], -row["completed"], row["entry"]))
    return rows


def _entry_rows(
    season: _Season,
    mean: Mapping[str, Fraction],
    intervals: _Intervals | None,
    columns: Sequence[ReferenceColumn],
) -> list[dict[str, Any]]:
    """The rows of the main board, best first."""
    rank = {key: 1 + sum(1 for other in mean.values() if other > mean[key]) for key in mean}
    rows = []
    for key in sorted(season.keys, key=lambda key: (-mean[key], key)):
        i = season.keys.index(key)
        seats = season.seats[key]
        actions = sum(seat.actions for seat in seats)
        reasons = {r: sum(seat.abstentions[r] for seat in seats) for r in ABSTAIN_REASONS}
        other = sum(n for r, n in reasons.items() if r not in ("refusal", _NOT_AN_ACTION))
        rows.append(
            {
                "entry": key,
                "label": season.label[key],
                "kind": season.kind[key],
                "rank": rank[key],
                "rank_low": None if intervals is None else intervals.rank_low[i],
                "rank_high": None if intervals is None else intervals.rank_high[i],
                "mean_milli": _milli(mean[key]),
                "mean_low_milli": None if intervals is None else _milli(intervals.mean_low[i]),
                "mean_high_milli": None if intervals is None else _milli(intervals.mean_high[i]),
                "nights": len(season.counted),
                "seat_nights": len(seats),
                "actions": actions,
                "abstain_rate_milli": _rate_milli(other, actions),
                "refusal_rate_milli": _rate_milli(reasons["refusal"], actions),
                "abstentions": reasons,
                "columns": {
                    c.name: _milli(Fraction(sum(s.columns[c.name] for s in seats), len(seats)))
                    for c in columns
                },
            }
        )
    return rows


def _footer(
    season: _Season, surplus: list[list[int]], seat_nights: list[list[int]]
) -> dict[str, Any]:
    """The footer: the smallest difference between models that the nights can tell apart."""
    units = len(season.units)
    models = [key for key in season.keys if season.kind[key] == "model"]
    sigma_milli = gap_milli = None
    if units >= 2 and len(models) >= 2:
        per_unit = {
            key: [Fraction(s, n) for s, n in zip(surplus[i], seat_nights[i], strict=True)]
            for i, key in enumerate(season.keys)
        }
        sigma = math.sqrt(float(_pooled_variance(per_unit, models)))
        sigma_milli = round(sigma * 1000)
        gap_milli = round((Z_ALPHA + Z_POWER) * sigma / math.sqrt(units) * 1000)
    deck = season.unit_name == "deck"
    basis_en = basis_zh = None
    if gap_milli is None or sigma_milli is None:
        text_en = (
            f"Too few nights so far to say which differences can be told apart: at least two "
            f"{season.unit_name}s and two models are needed."
        )
        need = "两副牌" if deck else "两夜"
        text_zh = f"按现有夜数，还算不出多大的差距才分得出来：至少要有{need}和两个模型。"
    else:
        gap, sigma = _decimal(gap_milli, 1), _decimal(sigma_milli, 1)
        power, alpha = POWER_PER_MILLE // 10, ALPHA_PER_MILLE // 10
        text_en = (
            f"With the nights played so far, differences smaller than about {gap} points per "
            f"night between models mostly cannot be told apart."
        )
        text_zh = f"按现有夜数，差距小于约 {gap} 分/夜时多半分不出来。"
        basis_en = (
            f"Basis: {_count(units, season.unit_name)}, {power}% probability at a two-sided "
            f"{alpha}% level, standard deviation of the difference between two models {sigma} "
            f"points per night."
        )
        basis_zh = (
            f"依据：{units} {'副牌' if deck else '夜'}，{power}% 把握，双侧 {alpha}%，"
            f"两个模型之差的标准差 {sigma} 分/夜。"
        )
    return {
        "unit": season.unit_name,
        "units": units,
        "model_pairs": len(models) * (len(models) - 1) // 2,
        "sigma_milli": sigma_milli,
        "gap_milli": gap_milli,
        "text_en": text_en,
        "text_zh": text_zh,
        "basis_en": basis_en,
        "basis_zh": basis_zh,
    }


def build_leaderboard(
    nights: Sequence[NightRecord],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    seed: str = DEFAULT_SEED,
    columns: Sequence[ReferenceColumn] = (),
) -> dict[str, Any]:
    """The leaderboard of a season as plain data (the module text defines every number).

    ``columns`` lists the reference columns that the nights were read with and fixes their order.
    Raises :class:`SeasonError` when the nights cannot form one season.
    """
    _check_method(resamples, seed)
    season = _assemble(nights, columns)
    surplus, seat_nights = _matrices(season)
    mean = {
        key: Fraction(sum(surplus[i]), sum(seat_nights[i])) for i, key in enumerate(season.keys)
    }
    intervals = None
    if len(season.units) >= 2:
        intervals = _bootstrap(surplus, seat_nights, resamples, seed)
    return {
        "schema": SCHEMA_ID,
        "season": {
            "id": season.season_id,
            "format": season.season_format,
            "format_known": season.format_known,
            "unit": season.unit_name,
            "units": len(season.units),
            "nights": len(season.counted),
            "left_out": season.left_out,
            "decks": season.decks,
        },
        "method": {
            "metric": "surplus",
            "resamples": resamples,
            "seed": seed,
            "interval_per_mille": list(INTERVAL_PER_MILLE),
            "ties": "shared_best_rank",
            "gap": {"alpha_per_mille": ALPHA_PER_MILLE, "power_per_mille": POWER_PER_MILLE},
        },
        "columns": [{"name": c.name, "label": c.label} for c in columns],
        "entries": _entry_rows(season, mean, intervals, columns),
        "missions": _mission_rows(season),
        "nights": [
            {"night": n.night, "unit": unit, "deck": n.deck, "rotation": n.rotation}
            for unit, members in season.units
            for n in members
        ],
        "footer": _footer(season, surplus, seat_nights),
    }


def leaderboard_for(
    paths: Sequence[Path],
    *,
    resamples: int = DEFAULT_RESAMPLES,
    seed: str = DEFAULT_SEED,
    columns: Sequence[ReferenceColumn] = (),
) -> dict[str, Any]:
    """Read every night under ``paths`` and build the leaderboard."""
    nights = [read_night(directory, columns) for directory in discover_night_dirs(paths)]
    return build_leaderboard(nights, resamples=resamples, seed=seed, columns=columns)


def leaderboard_json(board: Mapping[str, Any]) -> str:
    """The exact text of ``leaderboard.json``: indented, keys in the order they were built, no
    floats, one trailing newline."""
    canonical_json(board)  # rejects a float anywhere in the board
    return json.dumps(board, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


# ------------------------------------------------------------------------------- the text


def _decimal(milli: int, places: int) -> str:
    """``milli`` thousandths written with ``places`` decimals, rounded half to even."""
    exact = Decimal(milli).scaleb(-3)
    text = str(exact.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN))
    return text[1:] if text.startswith("-") and not text.strip("-0.") else text


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" + ("" if number == 1 else "s")


def _percent(milli: int | None) -> str:
    return "n/a" if milli is None else _decimal(milli * 100, 1) + "%"


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def _table(header: Sequence[str], rows: Sequence[Sequence[str]], align: str) -> list[str]:
    """Indented text columns. ``align`` has one letter per column: ``l`` or ``r``."""
    widths = [max(_width(cell) for cell in column) for column in zip(header, *rows, strict=True)]

    def line(cells: Sequence[str]) -> str:
        parts = []
        for cell, size, side in zip(cells, widths, align, strict=True):
            pad = " " * (size - _width(cell))
            parts.append(pad + cell if side == "r" else cell + pad)
        return "  " + "  ".join(parts).rstrip()

    return [line(header), *(line(row) for row in rows)]


def _rank_text(entry: Mapping[str, Any]) -> str:
    low, high = entry["rank_low"], entry["rank_high"]
    if low is None:
        return "n/a"
    return str(low) if low == high else f"{low}{_RANGE_DASH}{high}"


def _mean_text(entry: Mapping[str, Any]) -> str:
    mean = _decimal(entry["mean_milli"], 1)
    if entry["mean_low_milli"] is None:
        return mean
    low, high = _decimal(entry["mean_low_milli"], 1), _decimal(entry["mean_high_milli"], 1)
    return f"{mean} [{low}, {high}]"


def _headline(board: Mapping[str, Any]) -> list[str]:
    season, method = board["season"], board["method"]
    unit = season["unit"]
    title = "season" if season["id"] is None else f"season {season['id']}"
    parts = [f"{_count(season['nights'], 'night')} counted"]
    if unit == "deck":
        parts.append(_count(season["units"], "deck"))
    if season["left_out"]:
        parts.append(f"{len(season['left_out'])} left out")
    lines = [
        f"{title}: {season['format']}, {', '.join(parts)}",
        "ranked by mean surplus per night, mission bonus not included",
    ]
    if not season["format_known"]:
        lines.append(f"no {CONFIG_FILE} in the nights: the season is taken as single-night")
    if board["entries"][0]["rank_low"] is None:
        lines.append(f"rank intervals need at least two {unit}s")
    else:
        lines.append(
            f"rank: 95% interval over {method['resamples']} resamples of "
            f"{_count(season['units'], unit)} (seed {method['seed']!r}); "
            f"equal means share the best rank"
        )
    return lines


def leaderboard_text(board: Mapping[str, Any]) -> str:
    """The leaderboard as plain text tables."""
    season, footer = board["season"], board["footer"]
    names = [column["label"] for column in board["columns"]]
    header = ["#", "rank", "entry", "nights", "mean surplus [95% CI]", "abstain", "refusal", *names]
    rows = []
    for entry in board["entries"]:
        who = entry["label"] + (" [bot]" if entry["kind"] == "bot" else "")
        rows.append(
            [
                str(entry["rank"]),
                _rank_text(entry),
                who,
                str(entry["nights"]),
                _mean_text(entry),
                _percent(entry["abstain_rate_milli"]),
                _percent(entry["refusal_rate_milli"]),
                *(_decimal(entry["columns"][column["name"]], 1) for column in board["columns"]),
            ]
        )
    lines = [*_headline(board), "", *_table(header, rows, "rllrrrr" + "r" * len(names))]

    if board["missions"]:
        rows = [
            [
                row["label"],
                str(row["held"]),
                str(row["completed"]),
                _percent(row["completion_milli"]),
                _decimal(row["bonus_mean_milli"], 1),
            ]
            for row in board["missions"]
        ]
        lines += ["", "missions (the bonus is not part of the ranking)"]
        lines += _table(["entry", "held", "done", "done %", "bonus per night"], rows, "lrrrr")

    abstaining = [entry for entry in board["entries"] if any(entry["abstentions"].values())]
    if abstaining:
        reasons = list(ABSTAIN_REASONS)
        rows = [
            [entry["label"], *(str(entry["abstentions"][reason]) for reason in reasons)]
            for entry in abstaining
        ]
        lines += ["", "abstentions by reason"]
        lines += _table(["entry", *reasons], rows, "l" + "r" * len(reasons))

    if season["left_out"]:
        lines += ["", "left out"]
        lines += [f"  {item['night']}: {item['reason']}" for item in season["left_out"]]
    incomplete = [deck for deck in season["decks"] if not deck["complete"]]
    if incomplete:
        lines += ["", "decks that do not hold every rotation"]
        lines += [
            f"  {deck['deck']}: {deck['nights']} nights, rotations {deck['rotations']}"
            for deck in incomplete
        ]
    lines += ["", footer["text_en"]]
    lines += [footer["basis_en"]] if footer["basis_en"] else []
    lines += [footer["text_zh"]]
    lines += [footer["basis_zh"]] if footer["basis_zh"] else []
    return "\n".join(lines)
