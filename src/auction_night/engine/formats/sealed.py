"""The sealed-bid formats: first price, second price, all-pay and the blind box.

All four take one simultaneous round of bids, so settlement is one pure function of the bids,
the remaining budgets, the reserve and the tie key. The blind box prices like a second price
auction on a common value; the settlement is the same and the true value is revealed with
the hammer.

Rules (they are what ``rules_text`` states):

* A bid is an integer, floored, and clamped to the remaining budget (a legal clamp, recorded
  with the raw value). A missing bid is an abstention and counts as 0.
* The lot is sold to the highest bid, provided it is at least ``max(reserve, 1)``. Otherwise
  it goes unsold and nobody pays. All-pay has no reserve, so all zeros is the only way to
  stay unsold.
* Equal highest bids are settled by the tie key (the lowest rank wins).
* First price: the winner pays its own bid. Second price and blind box: the winner pays the
  higher of the best other bid and the reserve. All-pay: everyone pays their own bid, win or
  lose.
* Every bid, 0 for an abstention, is revealed with the hammer.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from auction_night.contracts.events import HammerData
from auction_night.contracts.mechanism import MechanismDef
from auction_night.engine.formats.base import (
    Answers,
    LotOutcome,
    LotStep,
    TieKey,
    active_seats,
    check_answers,
    check_table,
    make_record,
    newly_out,
    tie_order,
)


@dataclass(frozen=True, slots=True)
class SealedState:
    """A sealed lot waiting for its bids."""

    mech: MechanismDef
    budgets: dict[int, int]
    tie_key: dict[int, int]
    asked: tuple[int, ...]
    true_value: int | None = None


def _check_mechanism(mech: MechanismDef, true_value: int | None) -> None:
    if mech.clock != "none":
        raise ValueError(f"{mech.key} is not a sealed format")
    common = mech.value_model == "common"
    if common != (true_value is not None):
        raise ValueError("true_value is required for the blind box and refused for other formats")
    if true_value is not None and (
        isinstance(true_value, bool) or not isinstance(true_value, int) or true_value < 0
    ):
        raise ValueError("true_value must be a non-negative integer")


def settle_sealed(
    mech: MechanismDef,
    bids: Answers,
    budgets: Mapping[int, int],
    tie_key: TieKey,
    *,
    true_value: int | None = None,
) -> LotOutcome:
    """Settle one sealed lot.

    ``budgets`` holds the remaining budget of every seat still in the night. Seats with a
    budget of at least 1 are asked; ``bids`` may only name asked seats, and a seat without an
    entry (or with ``None``) abstains. ``true_value`` is the hidden value of a blind box lot
    and must be given for it, and only for it.
    """
    _check_mechanism(mech, true_value)
    check_table(budgets, tie_key)
    asked = active_seats(budgets)
    check_answers(asked, bids)
    records = tuple(make_record(seat, "sealed", bids.get(seat), budgets[seat]) for seat in asked)
    effective = {record.seat: record.value or 0 for record in records}

    reserve = mech.reserve
    qualifying = {seat: bid for seat, bid in effective.items() if bid >= max(reserve, 1)}
    if not qualifying:
        hammer = HammerData(format=mech.key, bids=effective, true_value=true_value, unsold=True)
        return LotOutcome(hammer=hammer, records=records, asked=asked)

    top = max(qualifying.values())
    contenders = [seat for seat, bid in qualifying.items() if bid == top]
    winner = tie_order(contenders, tie_key)[0]
    if mech.pricing == "first":
        price = top
        payments = {winner: price}
    elif mech.pricing == "second":
        best_other = max((bid for seat, bid in effective.items() if seat != winner), default=0)
        price = max(best_other, reserve)
        payments = {winner: price}
    else:
        price = top
        payments = {seat: bid for seat, bid in effective.items() if bid >= 1}

    hammer = HammerData(
        format=mech.key,
        winner=winner,
        price=price,
        payments=payments,
        bids=effective,
        true_value=true_value,
        tie_broken=len(contenders) > 1,
        tied=sorted(contenders) if len(contenders) > 1 else [],
        newly_out=newly_out(budgets, payments),
    )
    return LotOutcome(hammer=hammer, records=records, asked=asked)


def open_sealed(
    mech: MechanismDef,
    budgets: Mapping[int, int],
    tie_key: TieKey,
    *,
    true_value: int | None = None,
) -> LotStep:
    """Open a sealed lot: ask every seat that still has budget, in phase ``sealed``."""
    _check_mechanism(mech, true_value)
    check_table(budgets, tie_key)
    asked = active_seats(budgets)
    state = SealedState(mech, dict(budgets), dict(tie_key), asked, true_value)
    if not asked:
        return step_sealed(state, {})
    return LotStep(ask=asked, phase="sealed", state=state)


def step_sealed(state: SealedState, answers: Answers) -> LotStep:
    """Take the bids and decide the lot."""
    outcome = settle_sealed(
        state.mech, answers, state.budgets, state.tie_key, true_value=state.true_value
    )
    return LotStep(ask=(), phase=None, state=state, records=outcome.records, outcome=outcome)
