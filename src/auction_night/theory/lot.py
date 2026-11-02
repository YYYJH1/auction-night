"""The reference numbers of one lot, and of a whole night.

For every lot this module collects what the overlay and the show layer print next to the
result:

* ``ref_bids``: the textbook bid of each seat that was asked (see :mod:`~.bne`);
* ``excess``: what each seat earned minus what its textbook bid would have earned, the others
  held fixed (positive when leaving the textbook bid paid off);
* ``regret``: what the best single-lot deviation would have earned beyond the real payoff;
* ``alpha_milli``: the pacing factor of the seats that pace, in thousandths;
* ``expected_revenue_milli``: the exact expected revenue of the lot for the number of bidders
  it had, in thousandths of a coin;
* ``to_highest_value``: whether the lot went to a seat with the highest value among the
  bidders (private-value lots that were sold);
* ``curse_gap`` and ``top_signal_error`` (blind boxes): the price minus the true value, and
  the highest appraisal minus the true value.

Every number is **descriptive and belongs to one lot**. They are not added up, divided by each
other or turned into a score for the night: a sum of per-lot figures says nothing the lots do
not say better, and a ratio of two such sums invites a reading that the model does not support.

A seat that the cost gate muted in a lot (:mod:`auction_night.engine.muted`) did not choose the
number the log shows for it. It keeps its textbook bid, which is a fact about its cards, and has
no regret and no excess return, which measure a choice. A lot with a muted seat has no
``to_highest_value``: the seats that might have had the lot were silent, so the flag would say
nothing about the bids.
"""

import math
from collections.abc import Collection
from dataclasses import dataclass

from auction_night.contracts.config import BlindConfig
from auction_night.contracts.mechanism import MechanismDef
from auction_night.contracts.replay import LotOverlay
from auction_night.engine.deal import Deal, blind_bounds
from auction_night.engine.muted import muted_actions
from auction_night.engine.night import LotRecord, NightState
from auction_night.theory.bne import MIN_BIDDERS, reference_bid_int
from auction_night.theory.counterfactual import LotSetup, excess_return
from auction_night.theory.exact import expected_revenue
from auction_night.theory.pacing import alpha_milli, pacing_alpha
from auction_night.theory.regret import lot_regrets


@dataclass(frozen=True, slots=True)
class LotTheory:
    """The reference numbers of one lot. Seats that were not asked have no entry.

    ``bidders`` is the number of seats that were asked to bid, which is also the ``n`` of the
    reference bids (raised to two if smaller). A seat that the cost gate muted has a reference
    bid and no regret or excess return (see the module text).
    """

    lot: int
    bidders: int
    ref_bids: dict[int, int]
    excess: dict[int, int]
    regret: dict[int, int]
    alpha_milli: dict[int, int]
    expected_revenue_milli: int | None
    to_highest_value: bool | None
    curse_gap: int | None
    top_signal_error: int | None

    def overlay(self) -> LotOverlay:
        """The part of these numbers that the replay shows next to the lot."""
        return LotOverlay(
            ref_bids=self.ref_bids,
            alpha_milli=self.alpha_milli,
            regret=self.regret,
            to_highest_value=self.to_highest_value,
            excess=self.excess,
            expected_revenue_milli=self.expected_revenue_milli,
            curse_gap=self.curse_gap,
        )


def analyse_lot(
    deal: Deal,
    record: LotRecord,
    mech: MechanismDef,
    blind: BlindConfig,
    *,
    pacing_seats: Collection[int] = (),
    muted: Collection[int] = (),
) -> LotTheory:
    """The reference numbers of one decided lot.

    ``mech`` is the mechanism as the night used it (reserve, theta and levels applied), ``blind``
    the prior of the blind boxes, ``pacing_seats`` the seats whose pacing factor is wanted and
    ``muted`` the seats whose bid the cost gate skipped in this lot.
    """
    lot = deal.lot(record.lot)
    outcome = record.outcome
    asked = outcome.asked
    silent = frozenset(muted) & set(asked)
    n = max(len(asked), MIN_BIDDERS)
    bounds = blind_bounds(lot.tier, blind) if lot.is_blind else None
    setup = LotSetup.of(
        mech,
        tier=lot.tier,
        budgets=record.budgets_before,
        tie_key=lot.tie_key,
        values=lot.values,
        true_value=lot.blind_value,
        outcome=outcome,
    )

    ref_bids = {
        seat: reference_bid_int(
            mech,
            tier=lot.tier,
            n=n,
            budget=record.budgets_before[seat],
            value=None if lot.is_blind else lot.values[seat],
            signal=lot.signals[seat] if lot.is_blind else None,
            blind=bounds,
        )
        for seat in asked
    }
    schedule = [(item.tier, item.is_blind) for item in deal.lots[lot.lot - 1 :]]
    alphas = {
        seat: alpha_milli(pacing_alpha(record.budgets_before[seat], schedule, n))
        for seat in asked
        if seat in pacing_seats
    }
    revenue = expected_revenue(mech, lot.tier, len(asked), blind)

    winner = outcome.hammer.winner
    to_highest = None
    if not lot.is_blind and winner is not None and not silent:
        to_highest = lot.values[winner] >= max(lot.values[seat] for seat in asked)
    curse_gap = top_signal_error = None
    if lot.is_blind and lot.blind_value is not None:
        if winner is not None:
            curse_gap = outcome.hammer.price - lot.blind_value
        if asked:
            top_signal_error = max(lot.signals[seat] for seat in asked) - lot.blind_value

    return LotTheory(
        lot=lot.lot,
        bidders=len(asked),
        ref_bids=ref_bids,
        excess={
            seat: excess_return(setup, seat, ref_bids[seat]) for seat in asked if seat not in silent
        },
        regret={seat: gap for seat, gap in lot_regrets(setup).items() if seat not in silent},
        alpha_milli=alphas,
        expected_revenue_milli=None if revenue is None else math.floor(revenue * 1000),
        to_highest_value=to_highest,
        curse_gap=curse_gap,
        top_signal_error=top_signal_error,
    )


def analyse_night(state: NightState) -> list[LotTheory]:
    """The reference numbers of every lot decided so far, in lot order.

    Seats that pace are the economist bots whose configuration says so. The seats that the cost
    gate muted are read from the log of the state.
    """
    night = state.config.night
    muted = muted_actions(state.events)
    pacing = {
        seat.seat
        for seat in state.config.seats
        if seat.kind == "bot" and seat.policy == "economist" and seat.pacing
    }
    return [
        analyse_lot(
            state.deal,
            record,
            night.mechanism_for(state.deal.lot(record.lot).format),
            night.blind,
            pacing_seats=pacing,
            muted=muted.bids.get(record.lot, ()),
        )
        for record in state.records
    ]
