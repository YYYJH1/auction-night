"""The reference numbers hold up on nights with random rules and misbehaving seats.

``test_theory_regret`` and ``test_theory_lot`` use the rules of the mock night. Here the night is
random (reserve price, ``theta``, number of Dutch levels, budgets that sit on the edges of the
rules, tables of two to eight seats) and the seats answer with nothing, with abstentions of every
kind and with numbers at the edges. For every lot of every night:

* the replay model gives back what each seat really earned when its own number is played again;
* the regret is the best deviation. Where the budget is small enough to try every affordable
  number, brute force agrees with the exact search over the candidates;
* every per-lot number is present for the seats that were asked and only for them, the
  descriptive fields are set exactly where they apply, and the overlay of the replay can be built.
  A seat that the cost gate muted (an abstention for the cap) has a textbook bid and neither a
  regret nor an excess return, and its lot has no flag for the highest value.
"""

from chaos_support import Case, cases, players
from hypothesis import HealthCheck, example, given, settings
from night_support import play

from auction_night.engine.muted import muted_actions
from auction_night.theory import analyse_night
from auction_night.theory.counterfactual import LotSetup
from auction_night.theory.regret import regret

PROFILE = settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
SMALL_BUDGET = 60  # up to this budget every number can be tried


@PROFILE
@given(cases())
@example(Case())  # the table of the mock night
@example(Case(budget_c=20, seed=1))  # poor seats
@example(Case(reserve=25, theta=2, levels=4, seed=4))  # a reserve, few questions, four levels
@example(Case(shape="pay", collectors=2, shills=0, seed=6))
def test_the_replay_model_and_the_regret_hold_in_any_night(case: Case) -> None:
    config = case.config()
    the_deal = case.dealt(config)
    state = play(config, the_deal, players(case, config, the_deal)).state
    theories = analyse_night(state)
    assert [theory.lot for theory in theories] == list(range(1, config.night.n_lots + 1))
    muted_bids = muted_actions(state.events).bids

    for record, theory in zip(state.records, theories, strict=True):
        lot = the_deal.lot(record.lot)
        mech = config.night.mechanism_for(lot.format)
        asked = set(record.outcome.asked)
        muted = asked & muted_bids.get(record.lot, frozenset())
        hammer = record.outcome.hammer
        setup = LotSetup.of(
            mech,
            tier=lot.tier,
            budgets=record.budgets_before,
            tie_key=lot.tie_key,
            values=lot.values,
            true_value=lot.blind_value,
            outcome=record.outcome,
        )

        assert set(theory.ref_bids) == asked
        assert set(theory.excess) == set(theory.regret) == asked - muted
        assert theory.bidders == len(asked)
        theory.overlay()  # the replay accepts the numbers of this lot
        for seat in asked:
            assert setup.payoff(seat, setup.bids[seat]) == setup.actual[seat], (record.lot, seat)
        for seat in asked - muted:
            assert theory.regret[seat] == regret(setup, seat) >= 0
            budget = record.budgets_before[seat]
            if budget <= SMALL_BUDGET:
                best = max(setup.payoff(seat, own) for own in range(budget + 1))
                assert theory.regret[seat] == best - setup.actual[seat], (record.lot, seat)

        sold = not hammer.unsold
        assert (theory.to_highest_value is not None) == (sold and not lot.is_blind and not muted)
        assert (theory.curse_gap is not None) == (sold and lot.is_blind)
        # the exact expectation exists for lots without a reserve and with a rival
        assert (theory.expected_revenue_milli is None) == (mech.reserve > 0 or len(asked) < 2)
