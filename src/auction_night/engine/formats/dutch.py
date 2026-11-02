"""The Dutch auction with one-shot grab prices.

The price falls through a fixed ladder of levels, from the tier down to ``tier / levels`` in
equal steps, skipping levels below the reserve. Instead of watching the clock, every seat
reports one grab price before the lot starts. As no information arrives while the price
falls, this is the same game as the descending clock, and one question per seat is enough.

Rules (they are what ``rules_text`` states):

* The ladder is ``tier * (levels - k) / levels`` for ``k = 0 .. levels - 1``, highest first.
  Levels below the reserve are skipped. ``tier`` must be divisible by ``levels`` so that
  every price is whole.
* A grab price is floored and clamped to the remaining budget (a legal clamp, recorded with
  the raw value). A seat buys at the first level whose price is at most its grab price. As
  the clamp keeps the grab price within the budget, this is the first level where the grab
  price and the remaining budget both reach the level price.
* Seats whose remaining budget is below the lowest level are not asked at all.
* The earliest level on which any seat buys decides the lot. Several seats on the same level
  are settled by the tie key. The buyer pays the price of the level. With no buyer the lot is
  unsold.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from auction_night.contracts.events import DutchResultData, HammerData
from auction_night.contracts.mechanism import MechanismDef
from auction_night.engine.formats.base import (
    Answers,
    LotOutcome,
    LotStep,
    TieKey,
    check_answers,
    check_table,
    make_record,
    newly_out,
    tie_order,
)


@dataclass(frozen=True, slots=True)
class DutchState:
    """A Dutch lot waiting for its grab prices."""

    mech: MechanismDef
    tier: int
    budgets: dict[int, int]
    tie_key: dict[int, int]
    ladder: tuple[int, ...]
    asked: tuple[int, ...]


def dutch_ladder(tier: int, levels: int, reserve: int = 0) -> tuple[int, ...]:
    """Level prices, highest first, without the levels below the reserve."""
    for name, value in (("tier", tier), ("levels", levels), ("reserve", reserve)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
    if tier < 1 or levels < 2 or reserve < 0:
        raise ValueError("need tier >= 1, levels >= 2 and reserve >= 0")
    if tier % levels:
        raise ValueError(f"tier {tier} is not divisible by {levels} levels")
    step = tier // levels
    return tuple(price for k in range(levels) if (price := tier - k * step) >= reserve)


def dutch_asked(budgets: Mapping[int, int], ladder: tuple[int, ...]) -> tuple[int, ...]:
    """Seats to ask: those whose remaining budget reaches the lowest level of the ladder."""
    if not ladder:
        return ()
    lowest = ladder[-1]
    return tuple(sorted(seat for seat, budget in budgets.items() if budget >= lowest))


def _check_mechanism(mech: MechanismDef) -> int:
    if mech.clock != "descending_levels" or mech.levels is None:
        raise ValueError(f"{mech.key} is not a Dutch format")
    return mech.levels


def _firing_level(ladder: tuple[int, ...], grab: int) -> int | None:
    """Index of the first level at or below the grab price, or None if there is none."""
    for index, price in enumerate(ladder):
        if price <= grab:
            return index
    return None


def settle_dutch(
    mech: MechanismDef,
    tier: int,
    grabs: Answers,
    budgets: Mapping[int, int],
    tie_key: TieKey,
) -> LotOutcome:
    """Settle one Dutch lot from the grab prices of the seats that were asked.

    ``grabs`` may only name seats that :func:`dutch_asked` returns. A seat without an entry
    (or with ``None``) abstains and never buys.
    """
    levels = _check_mechanism(mech)
    check_table(budgets, tie_key)
    ladder = dutch_ladder(tier, levels, mech.reserve)
    asked = dutch_asked(budgets, ladder)
    check_answers(asked, grabs)
    records = tuple(make_record(seat, "dutch", grabs.get(seat), budgets[seat]) for seat in asked)

    firing: dict[int, int] = {}
    for record in records:
        if record.value is None:
            continue
        index = _firing_level(ladder, record.value)
        if index is not None:
            firing[record.seat] = index
    if not firing:
        return LotOutcome(
            hammer=HammerData(format=mech.key, unsold=True),
            records=records,
            asked=asked,
            dutch=DutchResultData(ladder=list(ladder), unsold=True),
        )

    first = min(firing.values())
    contenders = [seat for seat, index in firing.items() if index == first]
    buyer = tie_order(contenders, tie_key)[0]
    price = ladder[first]
    payments = {buyer: price}
    hammer = HammerData(
        format=mech.key,
        winner=buyer,
        price=price,
        payments=payments,
        tie_broken=len(contenders) > 1,
        tied=sorted(contenders) if len(contenders) > 1 else [],
        newly_out=newly_out(budgets, payments),
    )
    result = DutchResultData(ladder=list(ladder), level=first + 1, price=price, buyer=buyer)
    return LotOutcome(hammer=hammer, records=records, asked=asked, dutch=result)


def open_dutch(
    mech: MechanismDef, tier: int, budgets: Mapping[int, int], tie_key: TieKey
) -> LotStep:
    """Open a Dutch lot: ask the seats that can afford at least the lowest level."""
    levels = _check_mechanism(mech)
    check_table(budgets, tie_key)
    ladder = dutch_ladder(tier, levels, mech.reserve)
    asked = dutch_asked(budgets, ladder)
    state = DutchState(mech, tier, dict(budgets), dict(tie_key), ladder, asked)
    if not asked:
        return step_dutch(state, {})
    return LotStep(ask=asked, phase="dutch", state=state)


def step_dutch(state: DutchState, answers: Answers) -> LotStep:
    """Take the grab prices and decide the lot."""
    outcome = settle_dutch(state.mech, state.tier, answers, state.budgets, state.tie_key)
    return LotStep(ask=(), phase=None, state=state, records=outcome.records, outcome=outcome)
