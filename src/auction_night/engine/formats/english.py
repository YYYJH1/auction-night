"""The English button auction, played as a pure step function.

Every seat reports an exit price at the start. The clock then rises from the reserve, and a
seat leaves when the price reaches its exit price. Once at most ``theta`` seats remain, each
exit opens a round in which the remaining seats may revise their exit price. The engine is
driven by the caller: :func:`open_english` says whom to ask first, and every call to
:func:`step_english` says whom to ask next, or that the lot is decided.

Rules (they are what ``rules_text`` states):

* The first round asks every seat that has budget for an exit price, in phase
  ``english_init``. An exit price is floored and clamped to the remaining budget. A price
  below ``max(reserve, 1)``, or no answer, means the seat does not take part. Such seats are
  announced at price 0 and do not count as remaining.
* The clock stops at the lowest exit price. Every seat with that exit price leaves there and
  is announced. Simultaneous exits are announced worst tie rank first.
* After an exit, if between 2 and ``theta`` seats remain, all of them are asked in phase
  ``english_revise``. A new exit price below the current price means leaving at once: the
  seat leaves at the current price, exactly as if it had answered with the current price, and
  that exit can open another round. If all remaining seats leave at once, they share one exit
  price and the tie key picks the buyer. No answer keeps the previous exit price (status
  ``kept_previous``). With more than ``theta`` seats remaining the exit prices are executed
  without questions. There is no revision round before the first exit.
* The last seat standing wins and pays the price of the last exit, which is never below the
  reserve. A lone participant pays the reserve. If all remaining seats share one exit price,
  the tie key picks the buyer, who pays that price. With no participant the lot is unsold.
"""

from collections.abc import Mapping
from dataclasses import dataclass, replace

from auction_night.contracts.events import EnglishDropoutData, HammerData
from auction_night.contracts.mechanism import BidPhase, MechanismDef
from auction_night.engine.formats.base import (
    Answers,
    BidRecord,
    LotOutcome,
    LotStep,
    TieKey,
    active_seats,
    check_answers,
    check_table,
    make_record,
    newly_out,
    normalize_bid,
    tie_order,
)


@dataclass(frozen=True, slots=True)
class EnglishState:
    """An English lot between two rounds of questions.

    ``phase`` is the phase the engine waits in, and ``None`` once the lot is decided. ``price``
    is the current price of the clock and ``exits`` the exit price of every seat still in.
    ``dropouts`` and ``records`` are cumulative. The mappings are copies; treat them as
    read-only.
    """

    mech: MechanismDef
    budgets: dict[int, int]
    tie_key: dict[int, int]
    phase: BidPhase | None
    asked: tuple[int, ...]
    price: int
    exits: dict[int, int]
    seated: tuple[int, ...]
    dropouts: tuple[EnglishDropoutData, ...] = ()
    records: tuple[BidRecord, ...] = ()

    @property
    def remaining(self) -> tuple[int, ...]:
        """Seats still in, in seat order."""
        return tuple(sorted(self.exits))


def _check_mechanism(mech: MechanismDef) -> int:
    if mech.clock != "ascending_button" or mech.theta is None:
        raise ValueError(f"{mech.key} is not an English button format")
    return mech.theta


def open_english(mech: MechanismDef, budgets: Mapping[int, int], tie_key: TieKey) -> LotStep:
    """Open an English lot: ask every seat that has budget for its exit price."""
    _check_mechanism(mech)
    check_table(budgets, tie_key)
    asked = active_seats(budgets)
    state = EnglishState(
        mech=mech,
        budgets=dict(budgets),
        tie_key=dict(tie_key),
        phase="english_init",
        asked=asked,
        price=mech.reserve,
        exits={},
        seated=asked,
    )
    if not asked:
        return step_english(state, {})
    return LotStep(ask=asked, phase="english_init", state=state)


def step_english(state: EnglishState, answers: Answers) -> LotStep:
    """Take the answers of the seats that were asked and move the lot on."""
    if state.phase is None:
        raise ValueError("the lot is already decided")
    check_answers(state.asked, answers)
    if state.phase == "english_init":
        return _after_initial_round(state, answers)
    return _after_revision_round(state, answers)


def _after_initial_round(state: EnglishState, answers: Answers) -> LotStep:
    floor = max(state.mech.reserve, 1)
    records = tuple(
        make_record(seat, "english_init", answers.get(seat), state.budgets[seat])
        for seat in state.asked
    )
    exits = {r.seat: r.value for r in records if r.value is not None and r.value >= floor}
    taking_part = sorted(exits)
    dropouts = [
        EnglishDropoutData(seat=seat, price=0, remaining=taking_part, participated=False)
        for seat in state.asked
        if seat not in exits
    ]
    return _advance(state, exits, state.mech.reserve, records, dropouts, exited=False)


def _after_revision_round(state: EnglishState, answers: Answers) -> LotStep:
    price = state.price
    exits = dict(state.exits)
    records: list[BidRecord] = []
    for seat in state.asked:
        raw = answers.get(seat)
        if raw is None:
            records.append(BidRecord(seat, "english_revise", "kept_previous", value=exits[seat]))
            continue
        status, value, raw_value = normalize_bid(raw, state.budgets[seat])
        assert value is not None
        records.append(BidRecord(seat, "english_revise", status, value, raw_value))
        # A price below the current price means leaving at once, which is the same as an exit
        # price equal to the current price: the clock removes the seat on its next step.
        exits[seat] = max(value, price)
    return _advance(state, exits, price, tuple(records), [], exited=False)


def _advance(
    state: EnglishState,
    exits: dict[int, int],
    price: int,
    new_records: tuple[BidRecord, ...],
    new_dropouts: list[EnglishDropoutData],
    *,
    exited: bool,
) -> LotStep:
    """Run the clock until a question is due or the lot is decided.

    ``exited`` says that seats have just left, so a revision round is due when between 2 and
    ``theta`` seats remain. Each pass of the loop removes at least one seat.
    """
    theta = _check_mechanism(state.mech)
    dropouts = list(new_dropouts)
    winner: int | None = None
    tied: list[int] = []
    while True:
        remaining = sorted(exits)
        if len(remaining) <= 1:
            winner = remaining[0] if remaining else None
            break
        if exited and len(remaining) <= theta:
            nxt = replace(
                state,
                phase="english_revise",
                asked=tuple(remaining),
                price=price,
                exits=dict(exits),
                dropouts=state.dropouts + tuple(dropouts),
                records=state.records + new_records,
            )
            return LotStep(
                ask=tuple(remaining),
                phase="english_revise",
                state=nxt,
                records=new_records,
                dropouts=tuple(dropouts),
            )
        price = min(exits.values())
        leaving = [seat for seat in remaining if exits[seat] == price]
        if len(leaving) == len(remaining):
            # Every remaining seat wants to leave at this price: the tie key picks the buyer.
            order = tie_order(remaining, state.tie_key)
            winner, tied, leaving = order[0], remaining, order[1:]
        for seat in reversed(tie_order(leaving, state.tie_key)):
            del exits[seat]
            dropouts.append(EnglishDropoutData(seat=seat, price=price, remaining=sorted(exits)))
        if winner is not None:
            break
        exited = True
    return _decide(state, exits, price, new_records, dropouts, winner, tied)


def _decide(
    state: EnglishState,
    exits: dict[int, int],
    price: int,
    new_records: tuple[BidRecord, ...],
    new_dropouts: list[EnglishDropoutData],
    winner: int | None,
    tied: list[int],
) -> LotStep:
    all_dropouts = state.dropouts + tuple(new_dropouts)
    all_records = state.records + new_records
    if winner is None:
        hammer = HammerData(format=state.mech.key, unsold=True)
        winner_exit = None
    else:
        price = max(price, state.mech.reserve)
        payments = {winner: price}
        hammer = HammerData(
            format=state.mech.key,
            winner=winner,
            price=price,
            payments=payments,
            tie_broken=bool(tied),
            tied=tied,
            newly_out=newly_out(state.budgets, payments),
        )
        winner_exit = exits[winner]
    outcome = LotOutcome(
        hammer=hammer,
        records=all_records,
        asked=state.seated,
        dropouts=all_dropouts,
        winner_exit=winner_exit,
    )
    final = replace(
        state,
        phase=None,
        asked=(),
        price=price,
        exits=dict(exits),
        dropouts=all_dropouts,
        records=all_records,
    )
    return LotStep(
        ask=(),
        phase=None,
        state=final,
        records=new_records,
        dropouts=tuple(new_dropouts),
        outcome=outcome,
    )
