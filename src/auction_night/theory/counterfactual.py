"""Replaying one lot with one seat's number changed.

Two reference numbers need the outcome of a lot under a number that the seat did not choose:
the **regret** (what the best single-lot deviation would have earned) and the **counterfactual
excess return** (what the seat earned minus what the textbook bid would have earned). Both hold
the other seats' numbers fixed and use the same tie keys, so the replay is the real settlement
of the engine with one entry replaced.

What "the other seats' number" is depends on the format:

* sealed formats (first price, second price, all-pay, blind box): the bids, which the hammer
  reveals;
* Dutch: the grab prices, which are fixed because each seat names one before the clock starts;
* English: the *final exit prices*. An English lot is played as a second-price auction on
  exit prices, and a seat's final exit price is the price at which it left, or its exit price
  in force when it won. Whether the others would have revised their exits had this seat acted
  differently is not modelled: this is an approximation, exact when nobody revises.

Ties between equal numbers are broken by the tie keys of the deal. An English lot is the one
exception: seats that leave at the same price are ordered by who stayed longer in the real play
(the winner first, then the last to leave), because a seat that left earlier at a price cannot
win it back by having named the same price. The replay then reproduces the real result.

The payoff of a seat in one lot is the worth of the lot to it if it wins (the true value of a
blind box) minus what it pays. All-pay losers pay too.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

from auction_night.contracts.events import HammerData
from auction_night.contracts.mechanism import MechanismDef, make_mechanism
from auction_night.engine.formats import LotOutcome, settle_dutch, settle_sealed


def effective_bids(mech: MechanismDef, outcome: LotOutcome) -> dict[int, int]:
    """The number each asked seat played with: bid, grab price or final English exit price.

    A seat that abstained, or an English seat that did not take part, plays 0.
    """
    if mech.clock == "none":
        bids = outcome.hammer.bids
        assert bids is not None
        return dict(bids)
    if mech.clock == "descending_levels":
        return {seat: 0 for seat in outcome.asked} | {
            record.seat: record.value or 0 for record in outcome.records
        }
    exits = dict.fromkeys(outcome.asked, 0)
    for dropout in outcome.dropouts:
        if dropout.participated:
            exits[dropout.seat] = dropout.price
    if outcome.hammer.winner is not None:
        assert outcome.winner_exit is not None
        exits[outcome.hammer.winner] = outcome.winner_exit
    return exits


def english_tie_key(outcome: LotOutcome) -> dict[int, int]:
    """Tie ranks of an English lot from how it was played: the winner, then the last seat to
    leave, and so on back to the first; seats that never took part come last. Seats that left
    in one step were announced worst tie rank first, so this also keeps their order."""
    order: list[int] = []
    if outcome.hammer.winner is not None:
        order.append(outcome.hammer.winner)
    order += [d.seat for d in reversed(outcome.dropouts) if d.participated]
    order += [d.seat for d in outcome.dropouts if not d.participated]
    order += [seat for seat in outcome.asked if seat not in order]
    return {seat: rank for rank, seat in enumerate(order)}


@dataclass(frozen=True, slots=True)
class LotSetup:
    """A decided lot, ready to be replayed with one number changed.

    ``budgets`` are the remaining budgets of all seats before the lot, ``values`` what winning
    is worth to each seat, ``bids`` the effective numbers (see :func:`effective_bids`) and
    ``hammer`` the real result.
    """

    mech: MechanismDef
    tier: int
    budgets: Mapping[int, int]
    tie_key: Mapping[int, int]
    values: Mapping[int, int]
    true_value: int | None
    bids: Mapping[int, int]
    hammer: HammerData
    _sealed: MechanismDef = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # An English lot is replayed as a second-price auction with the same reserve.
        sealed = (
            make_mechanism("sealed_second", reserve=self.mech.reserve)
            if self.mech.clock == "ascending_button"
            else self.mech
        )
        object.__setattr__(self, "_sealed", sealed)

    @classmethod
    def of(
        cls,
        mech: MechanismDef,
        *,
        tier: int,
        budgets: Mapping[int, int],
        tie_key: Mapping[int, int],
        values: Mapping[int, int],
        true_value: int | None,
        outcome: LotOutcome,
    ) -> "LotSetup":
        """The setup of a decided lot from its engine outcome."""
        if mech.clock == "ascending_button":
            tie_key = english_tie_key(outcome)
        return cls(
            mech=mech,
            tier=tier,
            budgets=budgets,
            tie_key=tie_key,
            values=values,
            true_value=true_value,
            bids=effective_bids(mech, outcome),
            hammer=outcome.hammer,
        )

    def settle(self, bids: Mapping[int, int]) -> HammerData:
        """The hammer of this lot if the seats had played ``bids``."""
        if self.mech.clock == "descending_levels":
            return settle_dutch(self.mech, self.tier, bids, self.budgets, self.tie_key).hammer
        return settle_sealed(
            self._sealed, bids, self.budgets, self.tie_key, true_value=self.true_value
        ).hammer

    def payoff_in(self, seat: int, hammer: HammerData) -> int:
        """What ``seat`` earns in a lot that ended as ``hammer``."""
        won = self.values[seat] if hammer.winner == seat else 0
        return won - hammer.payments.get(seat, 0)

    @property
    def actual(self) -> dict[int, int]:
        """What every asked seat really earned in the lot."""
        return {seat: self.payoff_in(seat, self.hammer) for seat in self.bids}

    def payoff(self, seat: int, own: int) -> int:
        """What ``seat`` would earn if it played ``own`` and the others stayed as they were."""
        return self.payoff_in(seat, self.settle({**self.bids, seat: own}))


def excess_return(setup: LotSetup, seat: int, reference: int) -> int:
    """Earnings of ``seat`` minus what it would have earned bidding ``reference``.

    Positive when leaving the textbook bid paid off in this lot, negative when it cost.
    """
    return setup.actual[seat] - setup.payoff(seat, reference)
