"""Helpers of the simulation tests: hand-built requests and an independent reference night.

The reference night plays the rules of the calibration with plain functions on the cards of a
deal: no state machine, no events, no bots, exact fractions. The engine and the bots, driven by
:func:`auction_night.simulate.play_night`, must give the same books seat by seat.
"""

import math
from dataclasses import dataclass
from fractions import Fraction

from auction_night.contracts.events import CollectorMission, ShillMission
from auction_night.contracts.mechanism import MECHANISMS, FormatKey
from auction_night.engine.deal import BlindBounds, Deal, LotDeal
from auction_night.engine.night import Request, SeatFacts, Slot

PHASE_OF = {"english": "english_init", "dutch": "dutch"}


def make_request(
    fmt: FormatKey,
    *,
    seat: int = 1,
    tier: int = 200,
    bidders: int = 8,
    budget: int = 400,
    value: int | None = None,
    signal: int | None = None,
    upcoming: tuple[Slot, ...] | None = None,
    lot: int = 1,
    phase: str | None = None,
    exit_price: int | None = None,
) -> Request:
    """A request that asks ``seat`` to bid in lot ``lot`` of format ``fmt``."""
    blind = None
    if MECHANISMS[fmt].value_model == "common":
        blind = BlindBounds(low=tier // 2, high=3 * tier // 2, noise=tier // 5)
    asked = tuple(sorted({*range(1, bidders + 1), seat}))
    facts = tuple(
        SeatFacts(
            seat=s,
            budget_left=budget,
            value=value if s == seat else None,
            signal=signal if s == seat else None,
            exit_price=exit_price if s == seat else None,
        )
        for s in asked
    )
    return Request(
        index=0,
        phase=phase or PHASE_OF.get(fmt, "sealed"),  # type: ignore[arg-type]
        lot=lot,
        seats=asked,
        facts=facts,
        n_lots=12,
        n_seats=8,
        mode="plain",
        upcoming=upcoming if upcoming is not None else (Slot(lot, fmt, tier),),
        format=fmt,
        tier=tier,
        reserve=0,
        bidders=bidders,
        blind=blind,
        remaining=asked if phase == "english_revise" else (),
    )


# ----------------------------------------------------------------------- reference night

FACTORS = {"E": Fraction(1), "O": Fraction(5, 4), "S": Fraction(7, 10), "P": Fraction(1)}
REACH = Fraction(3, 20)


@dataclass(frozen=True)
class Books:
    """What a seat ended the night with."""

    surplus: int
    spent: int
    bonus: int


def _textbook(lot: LotDeal, n: int, value: int, signal: int | None) -> Fraction:
    """The textbook bid for ``n`` bidders, written out from the formulas."""
    tier = lot.tier
    if lot.format in ("english", "sealed_second"):
        return Fraction(value)
    if lot.format in ("sealed_first", "dutch"):
        return Fraction((n - 1) * value, n)
    if lot.format == "all_pay":
        return Fraction(n - 1, n) * Fraction(value) ** n / Fraction(tier) ** (n - 1)
    assert signal is not None
    shaded = Fraction(signal) - Fraction(tier, 5) * Fraction(n - 2, n)
    return min(max(shaded, Fraction(tier, 2)), Fraction(3 * tier, 2))


def _pacing(lots: tuple[LotDeal, ...], first: int, left: int, n: int) -> Fraction:
    planned = Fraction(0)
    for lot in lots[first:]:
        share = Fraction(1) if lot.format == "blind_box" else Fraction(n - 1, n + 1)
        planned += Fraction(lot.tier) * share / n
    return min(Fraction(1), Fraction(left) / planned)


def play_reference(
    deal: Deal, styles: dict[int, str], *, chase: bool, dutch_levels: int = 10
) -> dict[int, Books]:
    """Play a deal to the end by the rules of the calibration, in exact arithmetic.

    ``styles`` gives each seat one of E, O, S, P and T. A seat that holds a mission chases it
    when ``chase`` is set. The number of bidders ``n`` is the number of seats asked to bid in
    the lot, at least two.
    """
    left = dict(deal.budgets)
    paid = dict.fromkeys(left, 0)
    won_value = dict.fromkeys(left, 0)
    wins: dict[int, list[int]] = {seat: [] for seat in left}
    sold: dict[int, tuple[int | None, int]] = {}

    for index, lot in enumerate(deal.lots):
        ladder = [lot.tier * (dutch_levels - k) // dutch_levels for k in range(dutch_levels)]
        floor_budget = ladder[-1] if lot.format == "dutch" else 1
        asked = [seat for seat in sorted(left) if left[seat] >= floor_budget]
        n = max(len(asked), 2)
        bids: dict[int, int] = {}
        for seat in asked:
            bids[seat] = _bid(deal, styles[seat], seat, lot, index, n, left[seat], wins, chase)

        winner, payments = _settle(lot, ladder, bids)
        sold[lot.lot] = (winner, payments.get(winner, 0) if winner is not None else 0)
        for seat, amount in payments.items():
            left[seat] -= amount
            paid[seat] += amount
        if winner is not None:
            won_value[winner] += lot.values[winner]
            wins[winner].append(lot.lot)

    books = {}
    for seat in left:
        bonus = 0
        mission = deal.missions.get(seat)
        if isinstance(mission, CollectorMission):
            tagged = [k for k in wins[seat] if deal.lots[k - 1].tag == mission.tag]
            bonus = mission.bonus if len(tagged) >= mission.need else 0
        elif isinstance(mission, ShillMission):
            buyer, price = sold[mission.lot]
            done = buyer is not None and buyer != seat and price >= mission.threshold_price
            bonus = mission.bonus if done else 0
        books[seat] = Books(won_value[seat] - paid[seat], paid[seat], bonus)
    return books


def _bid(
    deal: Deal,
    style: str,
    seat: int,
    lot: LotDeal,
    index: int,
    n: int,
    left: int,
    wins: dict[int, list[int]],
    chase: bool,
) -> int:
    value, signal = lot.values[seat], lot.signals.get(seat)
    if style == "T":
        return max(0, min(signal if signal is not None else value, left))
    mission = deal.missions.get(seat) if chase else None
    premium = Fraction(0)
    if (
        isinstance(mission, ShillMission)
        and mission.lot == lot.lot
        and mission.threshold_price <= left
    ):
        return mission.threshold_price
    if isinstance(mission, CollectorMission) and lot.tag == mission.tag:
        have = sum(1 for k in wins[seat] if deal.lots[k - 1].tag == mission.tag)
        later = sum(1 for other in deal.lots[index + 1 :] if other.tag == mission.tag)
        assert mission.need == 2
        if have == 1:
            premium = Fraction(mission.bonus)
        elif have == 0:
            premium = mission.bonus * (1 - (1 - REACH) ** later)

    factor = FACTORS[style]
    if lot.format == "blind_box":
        assert signal is not None
        exact = factor * _textbook(lot, n, value, signal) + premium
    else:
        shifted = Fraction(value) + premium
        if lot.format == "all_pay":
            shifted = min(shifted, Fraction(lot.tier))
        exact = factor * _shifted_textbook(lot, n, shifted)
    if style == "P":
        exact *= _pacing(deal.lots, index, left, n)
    return max(0, min(math.floor(exact), left))


def _shifted_textbook(lot: LotDeal, n: int, value: Fraction) -> Fraction:
    if lot.format in ("english", "sealed_second"):
        return value
    if lot.format in ("sealed_first", "dutch"):
        return Fraction(n - 1, n) * value
    return Fraction(n - 1, n) * value**n / Fraction(lot.tier) ** (n - 1)


def _settle(
    lot: LotDeal, ladder: list[int], bids: dict[int, int]
) -> tuple[int | None, dict[int, int]]:
    positive = {seat: bid for seat, bid in bids.items() if bid >= 1}
    if not positive:
        return None, {}
    if lot.format == "dutch":
        fires = {}
        for seat, bid in positive.items():
            level = next((k for k, price in enumerate(ladder) if price <= bid), None)
            if level is not None:
                fires[seat] = level
        if not fires:
            return None, {}
        winner = min(fires, key=lambda seat: (fires[seat], lot.tie_key[seat]))
        return winner, {winner: ladder[fires[winner]]}
    order = sorted(positive, key=lambda seat: (-positive[seat], lot.tie_key[seat]))
    winner = order[0]
    if lot.format in ("english", "sealed_second", "blind_box"):
        return winner, {winner: positive[order[1]] if len(order) > 1 else 0}
    if lot.format == "sealed_first":
        return winner, {winner: positive[winner]}
    return winner, dict(positive)
