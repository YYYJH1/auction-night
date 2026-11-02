"""The deal: every private card of a night, drawn from the deal seed.

``deal(config)`` is a pure function of the configuration and the seed. It decides the lot
schedule (formats, tiers and tags per lot), each seat's value for each ordinary lot, the
hidden value and the noisy appraisals of the blind boxes, the budgets, the secret missions,
the canary words and the tie key of every lot. Nothing else in the engine draws a random
number.

Each kind of draw has its own stream (see :mod:`auction_night.engine.rng`), keyed by purpose
and by lot number or seat number. A card never depends on how many seats or lots a night has
beyond what its own stream needs: draws are always made for all eight seats and the first
``n`` are kept, so a night with fewer seats shares the cards of seats ``1..n`` with the full
night. Changing one rule (the blind-box noise, the budget scale, the mission mix) leaves every
other card unchanged.

Draw table (purpose, index, what is drawn):

========================  =========  ===================================================
``schedule.format``       0          shuffle of each format repeated ``lots_per_format``
``schedule.tier``         0          shuffle of the tier multiset
``schedule.tag``          0          shuffle of the tag multiset (equal count per tag)
``value``                 lot        seat 1..8: integer value in ``0..tier``
``blind.value``           lot        hidden common value of a blind box
``blind.noise``           lot        seat 1..8: appraisal noise
``tie``                   lot        permutation of seats 1..8, the rank of each seat
``budget``                0          seat 1..8: budget
``mission.kind``          0          shuffle of the mission kinds over the model seats
``mission.tag``           seat       tag of a collector mission
``mission.shill_lot``     0          distinct lots for the shill missions
``canary``                seat       canary word of the seat's mission
========================  =========  ===================================================

Ratios in the configuration have at most four decimals and are converted exactly to integer
ten-thousandths, so no floating point arithmetic touches a card.
"""

import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Final

from auction_night.contracts.config import BlindConfig, NightConfig, NightSettings
from auction_night.contracts.events import (
    SEED_PATTERN,
    CollectorMission,
    DealPrivateData,
    Mission,
    ShillMission,
    canonical_json,
    sha256_digest,
)
from auction_night.contracts.mechanism import MAX_SEATS, FormatKey, MechanismDef
from auction_night.engine.rng import Stream

CANARY_ALPHABET: Final = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
CANARY_LENGTH: Final = 6
_E4: Final = 10_000


# ------------------------------------------------------------------------- exact numbers


def ratio_e4(ratio: float) -> int:
    """A configuration ratio (at most four decimals) as an integer number of 1/10000."""
    scaled = Fraction(repr(ratio)) * _E4
    if scaled.denominator != 1:
        raise ValueError(f"ratio {ratio!r} has more than four decimals")
    return int(scaled)


def budget_range(night: NightSettings) -> tuple[int, int]:
    """The public budget range: ``floor(c * low)`` and ``floor(c * high)``.

    A budget is the floor of ``c * u`` for ``u`` uniform on ``[low, high)``, so the top of the
    range is announced but reached only when ``c * high`` is not a whole number.
    """
    low, high = (ratio_e4(x) for x in night.budget.spread)
    return night.budget.c * low // _E4, night.budget.c * high // _E4


def budget_cells(night: NightSettings) -> tuple[int, int]:
    """The grid a budget is drawn from: ``c * low`` and ``c * high`` in units of 1/10000."""
    low, high = (ratio_e4(x) for x in night.budget.spread)
    return night.budget.c * low, night.budget.c * high


def draw_budget(stream: Stream, cells: tuple[int, int]) -> int:
    """One budget: ``floor(c * u)`` with ``u`` uniform on the grid of 1/10000 in the spread.

    ``cells`` is :func:`budget_cells`. A cell ``w`` stands for ``c * u`` in ``[w, w + 1)``
    ten-thousandths, so the draw has exactly the distribution of ``floor(c * u)`` for a
    continuous uniform ``u``.
    """
    start, stop = cells
    offset = stream.randbelow(stop - start) if stop > start else 0
    return (start + offset) // _E4


@dataclass(frozen=True, slots=True)
class BlindBounds:
    """Integer bounds of a blind box lot of one tier: the hidden value lies in
    ``low..high`` and every appraisal is within ``noise`` of it."""

    low: int
    high: int
    noise: int


def blind_bounds(tier: int, blind: BlindConfig) -> BlindBounds:
    """Bounds for the integers in ``[low * tier, high * tier]`` and the noise ``noise * tier``."""
    low_e4, high_e4 = (ratio_e4(x) for x in blind.value_range)
    low = -(-tier * low_e4 // _E4)
    high = tier * high_e4 // _E4
    noise = tier * ratio_e4(blind.noise) // _E4
    if low > high:
        raise ValueError(f"tier {tier} leaves no integer value in the blind-box range")
    if low - noise < 0:
        raise ValueError(f"tier {tier}: an appraisal could become negative")
    return BlindBounds(low, high, noise)


def shill_threshold(tier: int, ratio: float) -> int:
    """The price a shill must reach: ``ceil(ratio * tier)``, exactly."""
    return -(-tier * ratio_e4(ratio) // _E4)


# --------------------------------------------------------------------------------- types


@dataclass(frozen=True, slots=True)
class LotDeal:
    """Schedule and secrets of one lot.

    ``values`` is what winning the lot is worth to each seat: the seat's own value for an
    ordinary lot, the hidden common value (for every seat) for a blind box. ``signals`` holds
    the appraisals of a blind box (empty otherwise). ``tie_key`` ranks the seats for ties; the
    lowest rank wins and the ranks are distinct.
    """

    lot: int
    format: FormatKey
    tier: int
    tag: str
    reserve: int
    mechanism: str
    values: dict[int, int]
    signals: dict[int, int]
    blind_value: int | None
    tie_key: dict[int, int]

    @property
    def is_blind(self) -> bool:
        """True for a blind box lot."""
        return self.blind_value is not None


@dataclass(frozen=True, slots=True)
class Deal:
    """All random draws of a night. The deal seed itself is not kept here."""

    n_seats: int
    lots: tuple[LotDeal, ...]
    budgets: dict[int, int]
    missions: dict[int, Mission]

    def lot(self, number: int) -> LotDeal:
        """One lot by its number (1-based)."""
        if not 1 <= number <= len(self.lots):
            raise ValueError(f"no lot {number}")
        return self.lots[number - 1]

    def card(self, seat: int) -> DealPrivateData:
        """The private card of one seat. It never holds the hidden value of a blind box."""
        if seat not in self.budgets:
            raise ValueError(f"no seat {seat}")
        return DealPrivateData(
            seat=seat,
            budget=self.budgets[seat],
            values={lot.lot: lot.values[seat] for lot in self.lots if not lot.is_blind},
            signals={lot.lot: lot.signals[seat] for lot in self.lots if lot.is_blind},
            mission=self.missions.get(seat),
        )

    def cards(self) -> list[DealPrivateData]:
        """The private cards of all seats, in seat order."""
        return [self.card(seat) for seat in sorted(self.budgets)]

    def to_dict(self) -> dict[str, Any]:
        """Everything, as plain JSON types (for golden files and digests)."""
        return {
            "n_seats": self.n_seats,
            "budgets": dict(self.budgets),
            "lots": [
                {
                    "lot": lot.lot,
                    "format": lot.format,
                    "tier": lot.tier,
                    "tag": lot.tag,
                    "reserve": lot.reserve,
                    "mechanism": lot.mechanism,
                    "values": dict(lot.values),
                    "signals": dict(lot.signals),
                    "blind_value": lot.blind_value,
                    "tie_key": dict(lot.tie_key),
                }
                for lot in self.lots
            ],
            "missions": {
                seat: mission.model_dump(mode="json") for seat, mission in self.missions.items()
            },
        }

    def digest(self) -> str:
        """``sha256:<hex>`` of the canonical JSON of the whole deal."""
        return sha256_digest(canonical_json(self.to_dict()))


# ------------------------------------------------------------------------------- the deal


def _check_seed(seed: str) -> str:
    if (
        not isinstance(seed, str)
        or not 1 <= len(seed) <= 128
        or not re.fullmatch(SEED_PATTERN, seed)
    ):
        raise ValueError("a seed is 1 to 128 characters from A-Z a-z 0-9 _ . : -")
    return seed


def deal(config: NightConfig, *, seed: str | None = None) -> Deal:
    """Draw all cards of the night described by ``config``.

    The seed is ``config.night.resolve_seed()``, which also checks the published commitment.
    Passing ``seed`` replaces it, for simulations over many seeds: the commitment is not
    checked then.
    """
    night = config.night
    root = night.resolve_seed() if seed is None else _check_seed(seed)
    n_seats = len(config.seats)
    seats = range(1, n_seats + 1)

    mechanisms = {fmt: night.mechanism_for(fmt) for fmt in night.formats}
    schedule = _schedule(root, night)
    lots = tuple(
        _draw_lot(root, night, mechanisms[fmt], number, tier, tag, n_seats)
        for number, (fmt, tier, tag) in enumerate(schedule, start=1)
    )

    budget_stream = Stream(root, "budget", 0)
    cells = budget_cells(night)
    drawn = [draw_budget(budget_stream, cells) for _ in range(MAX_SEATS)]
    budgets = {seat: drawn[seat - 1] for seat in seats}

    holders = [
        seat.seat for seat in sorted(config.seats, key=lambda s: s.seat) if seat.kind != "bot"
    ]
    missions = _draw_missions(root, night, lots, holders)
    return Deal(n_seats=n_seats, lots=lots, budgets=budgets, missions=missions)


def _schedule(root: str, night: NightSettings) -> list[tuple[FormatKey, int, str]]:
    n_lots = night.n_lots
    formats = [fmt for fmt in night.formats for _ in range(night.lots_per_format)]
    tiers = [tier for tier in sorted(night.tiers) for _ in range(night.tiers[tier])]
    per_tag, spare = divmod(n_lots, len(night.tags))
    if spare or len(formats) != n_lots or len(tiers) != n_lots:
        raise ValueError("the configuration does not describe a consistent schedule")
    tags = [tag for tag in night.tags for _ in range(per_tag)]
    return list(
        zip(
            Stream(root, "schedule.format", 0).shuffle(formats),
            Stream(root, "schedule.tier", 0).shuffle(tiers),
            Stream(root, "schedule.tag", 0).shuffle(tags),
            strict=True,
        )
    )


def _draw_lot(
    root: str,
    night: NightSettings,
    mech: MechanismDef,
    number: int,
    tier: int,
    tag: str,
    n_seats: int,
) -> LotDeal:
    seats = range(1, n_seats + 1)
    if mech.value_model == "common":
        bounds = blind_bounds(tier, night.blind)
        hidden = Stream(root, "blind.value", number).randint(bounds.low, bounds.high)
        noise = Stream(root, "blind.noise", number)
        offsets = [noise.randint(-bounds.noise, bounds.noise) for _ in range(MAX_SEATS)]
        values = {seat: hidden for seat in seats}
        signals = {seat: hidden + offsets[seat - 1] for seat in seats}
    else:
        hidden = None
        own = Stream(root, "value", number)
        drawn = [own.randint(0, tier) for _ in range(MAX_SEATS)]
        values = {seat: drawn[seat - 1] for seat in seats}
        signals = {}
    order = Stream(root, "tie", number).shuffle(range(1, MAX_SEATS + 1))
    rank = {seat: position for position, seat in enumerate(order)}
    return LotDeal(
        lot=number,
        format=mech.key,
        tier=tier,
        tag=tag,
        reserve=mech.reserve,
        mechanism=mech.ref,
        values=values,
        signals=signals,
        blind_value=hidden,
        tie_key={seat: rank[seat] for seat in seats},
    )


def _draw_missions(
    root: str, night: NightSettings, lots: tuple[LotDeal, ...], holders: list[int]
) -> dict[int, Mission]:
    rules = night.missions
    kinds: list[str | None] = ["collector"] * rules.collectors + ["shill"] * rules.shills
    if len(kinds) > len(holders):
        raise ValueError(f"{len(kinds)} missions but only {len(holders)} seats to hold them")
    kinds += [None] * (len(holders) - len(kinds))
    assigned = dict(zip(holders, Stream(root, "mission.kind", 0).shuffle(kinds), strict=True))

    eligible = [lot.lot for lot in lots if lot.format in rules.shill_formats]
    targets = iter(Stream(root, "mission.shill_lot", 0).sample(eligible, rules.shills))

    missions: dict[int, Mission] = {}
    used: set[str] = set()
    for seat in holders:
        kind = assigned[seat]
        if kind is None:
            continue
        canary_stream = Stream(root, "canary", seat)
        canary = canary_stream.token(CANARY_ALPHABET, CANARY_LENGTH)
        while canary in used:
            canary = canary_stream.token(CANARY_ALPHABET, CANARY_LENGTH)
        used.add(canary)
        if kind == "collector":
            tag = Stream(root, "mission.tag", seat).choice(night.tags)
            missions[seat] = CollectorMission(
                kind="collector",
                tag=tag,
                need=rules.collector_need,
                bonus=rules.bonus,
                canary=canary,
            )
        else:
            target = next(targets)
            missions[seat] = ShillMission(
                kind="shill",
                lot=target,
                threshold_price=shill_threshold(lots[target - 1].tier, rules.shill_threshold),
                bonus=rules.bonus,
                canary=canary,
            )
    return missions
