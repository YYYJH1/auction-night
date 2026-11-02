"""Regret: the best single-lot deviation, found exactly, checked against trying every number."""

from dataclasses import replace

from hypothesis import given, settings
from hypothesis import strategies as st
from night_support import all_bots_config, make_config, play, play_seed, wild_players

from auction_night.contracts.events import HammerData
from auction_night.contracts.mechanism import FormatKey, make_mechanism
from auction_night.engine.deal import deal
from auction_night.engine.formats import active_seats, dutch_asked, dutch_ladder, play_lot
from auction_night.theory.counterfactual import LotSetup
from auction_night.theory.regret import best_deviation, candidates, lot_regrets, regret


def setup_for(
    key: FormatKey,
    bids: dict[int, int],
    *,
    values: dict[int, int],
    budgets: dict[int, int] | None = None,
    tie_key: dict[int, int] | None = None,
    tier: int = 100,
    reserve: int = 0,
    true_value: int | None = None,
    levels: int | None = None,
) -> LotSetup:
    """A decided lot built from the numbers the seats played."""
    mech = make_mechanism(key, reserve=reserve, levels=levels)
    seats = sorted(bids)
    draft = LotSetup(
        mech=mech,
        tier=tier,
        budgets=budgets or dict.fromkeys(seats, 10_000),
        tie_key=tie_key or {seat: seat for seat in seats},
        values=values,
        true_value=true_value,
        bids=bids,
        hammer=HammerData(format=key, unsold=True),
    )
    return replace(draft, hammer=draft.settle(bids))


# --------------------------------------------------------------------- hand-built lots


def test_a_truthful_second_price_bidder_has_no_regret() -> None:
    setup = setup_for("sealed_second", {1: 70, 2: 55, 3: 30}, values={1: 70, 2: 55, 3: 30})
    assert lot_regrets(setup) == {1: 0, 2: 0, 3: 0}


def test_an_overbidder_who_won_at_a_loss_regrets_exactly_the_loss() -> None:
    # seat 1 values the lot at 70, bids 100, pays the second price 90: a loss of 20
    setup = setup_for("sealed_second", {1: 100, 2: 90, 3: 40}, values={1: 70, 2: 90, 3: 40})
    assert setup.actual[1] == -20
    assert regret(setup, 1) == 20
    assert best_deviation(setup, 1) == (0, 0)


def test_an_underbidder_who_lost_regrets_the_profit_it_missed() -> None:
    setup = setup_for("sealed_second", {1: 50, 2: 60, 3: 40}, values={1: 100, 2: 60, 3: 40})
    assert setup.hammer.winner == 2 and setup.actual[1] == 0
    assert regret(setup, 1) == 40  # bidding anything above 60 wins at 60


def test_first_price_regret_is_the_money_left_on_the_table() -> None:
    values = {1: 100, 2: 50, 3: 30}
    bids = {1: 80, 2: 50, 3: 30}
    unlucky = setup_for("sealed_first", bids, values=values, tie_key={1: 2, 2: 0, 3: 1})
    assert unlucky.actual[1] == 20
    assert regret(unlucky, 1) == 29  # a bid of 51 wins at 51, since a tie at 50 goes to seat 2
    lucky = setup_for("sealed_first", bids, values=values, tie_key={1: 0, 2: 1, 3: 2})
    assert regret(lucky, 1) == 30  # a tie at 50 already wins, for 50 coins of surplus


def test_all_pay_regret_counts_what_the_loser_paid_for_nothing() -> None:
    values = {1: 100, 2: 80, 3: 60}
    winner = setup_for("all_pay", {1: 60, 2: 50, 3: 20}, values=values)
    assert winner.actual == {1: 40, 2: -50, 3: -20}
    # Ties go to the lowest seat here. Seat 1 could have won with 50 (a tie with seat 2) and kept
    # 50 instead of 40. Seat 2 should have stayed out, or paid 61 to win and kept 19: 69 better
    # than what it did. Seat 3 should have stayed out: 20 better.
    assert lot_regrets(winner) == {1: 10, 2: 69, 3: 20}


def test_a_blind_box_winner_who_overpaid_regrets_the_overpayment() -> None:
    setup = setup_for(
        "blind_box",
        {1: 150, 2: 130, 3: 90},
        values=dict.fromkeys((1, 2, 3), 120),
        tier=100,
        true_value=120,
    )
    assert setup.hammer.winner == 1 and setup.hammer.price == 130
    assert setup.actual[1] == -10 and regret(setup, 1) == 10
    assert lot_regrets(setup) == {1: 10, 2: 0, 3: 0}


def test_a_seat_that_cannot_afford_the_deviation_has_no_regret_for_it() -> None:
    budgets = {1: 55, 2: 10_000, 3: 10_000}
    setup = setup_for(
        "sealed_second", {1: 55, 2: 60, 3: 20}, values={1: 100, 2: 60, 3: 20}, budgets=budgets
    )
    assert regret(setup, 1) == 0  # every bid it could make is 55 at most


def test_a_dutch_grab_price_matters_only_through_the_level_it_falls_on() -> None:
    values = {1: 100, 2: 90, 3: 20}
    setup = setup_for("dutch", {1: 70, 2: 40, 3: 20}, values=values, tier=100)
    assert setup.hammer.winner == 1 and setup.hammer.price == 70
    assert regret(setup, 1) == 30  # waiting until 40 still wins: seat 2 also grabs at 40, and
    # the tie goes to seat 1, so the lot costs 40 and not 70
    behind = setup_for(
        "dutch", {1: 70, 2: 40, 3: 20}, values=values, tier=100, tie_key={1: 1, 2: 0, 3: 2}
    )
    assert regret(behind, 1) == 20  # a tie at 40 is lost, so the best is to grab at 50
    assert candidates(setup, 1) == [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    poor = setup_for(
        "dutch", {1: 70, 2: 40, 3: 20}, values=values, tier=100, budgets={1: 55, 2: 99, 3: 99}
    )
    assert candidates(poor, 1) == [0, 10, 20, 30, 40, 50]


def test_an_english_lot_is_a_second_price_auction_on_the_final_exit_prices() -> None:
    mech = make_mechanism("english")
    seats = [1, 2, 3, 4]
    values = {1: 90, 2: 70, 3: 50, 4: 30}
    bids = {1: 90, 2: 95, 3: 50, 4: 30}  # seat 2 overbids: it wins at 90 and loses 20
    outcome = play_lot(
        mech,
        tier=100,
        budgets=dict.fromkeys(seats, 1000),
        tie_key={s: s for s in seats},
        answer=lambda step: bids if step.phase == "english_init" else {},
    )
    setup = LotSetup.of(
        mech,
        tier=100,
        budgets=dict.fromkeys(seats, 1000),
        tie_key={s: s for s in seats},
        values=values,
        true_value=None,
        outcome=outcome,
    )
    assert setup.bids == bids and setup.hammer.winner == 2 and setup.hammer.price == 90
    assert setup.actual == {1: 0, 2: -20, 3: 0, 4: 0}
    assert lot_regrets(setup) == {1: 0, 2: 20, 3: 0, 4: 0}


# ------------------------------------------------------------------- against brute force


@st.composite
def lots(draw: st.DrawFn) -> tuple[LotSetup, list[int]]:
    key = draw(
        st.sampled_from(
            ["sealed_first", "sealed_second", "all_pay", "blind_box", "dutch", "english"]
        )
    )
    n = draw(st.integers(2, 5))
    tier = draw(st.sampled_from([20, 100, 200]))
    reserve = 0 if key == "all_pay" else draw(st.sampled_from([0, 0, 5, 20]))
    mech = make_mechanism(key, reserve=reserve)
    seats = list(range(1, n + 1))
    budgets = {s: draw(st.integers(0, 120)) for s in seats}
    keys = dict(zip(seats, draw(st.permutations(seats)), strict=True))
    if key == "dutch":
        asked = list(dutch_asked(budgets, dutch_ladder(tier, mech.levels, reserve)))
    else:
        asked = list(active_seats(budgets))
    bids = {s: draw(st.integers(0, budgets[s])) for s in asked}
    true_value = draw(st.integers(0, 2 * tier)) if key == "blind_box" else None
    values = (
        dict.fromkeys(seats, true_value)
        if true_value is not None
        else {s: draw(st.integers(0, tier)) for s in seats}
    )
    setup = setup_for(
        key,  # type: ignore[arg-type]
        bids,
        values=values,
        budgets=budgets,
        tie_key=keys,
        tier=tier,
        reserve=reserve,
        true_value=true_value,
    )
    return setup, asked


@settings(max_examples=300, deadline=None)
@given(lots())
def test_the_candidate_numbers_find_the_best_deviation_that_trying_every_number_finds(case) -> None:
    setup, asked = case
    for seat in asked:
        budget = setup.budgets[seat]
        brute = max(setup.payoff(seat, own) for own in range(budget + 1))
        best, bid = best_deviation(setup, seat)
        assert best == brute
        assert setup.payoff(seat, bid) == best
        assert regret(setup, seat) == max(0, brute - setup.actual[seat]) >= 0
    assert sorted(lot_regrets(setup)) == sorted(asked)


@settings(max_examples=200, deadline=None)
@given(lots())
def test_replaying_the_lot_with_every_seat_unchanged_gives_the_real_result(case) -> None:
    setup, asked = case
    for seat in asked:
        assert setup.payoff(seat, setup.bids[seat]) == setup.actual[seat]


# ---------------------------------------------------------------------- whole nights


def test_an_economist_with_no_pacing_never_regrets_a_second_price_or_english_lot() -> None:
    from auction_night.theory import analyse_night

    config = all_bots_config("economist", pacing=False, budget_c=20_000)
    positive_elsewhere = 0
    checked = 0
    for index in range(40):
        transcript = play_seed(config, f"regret-economist-{index}")
        for theory, record in zip(
            analyse_night(transcript.state), transcript.state.records, strict=True
        ):
            fmt = record.outcome.hammer.format
            if fmt in ("english", "sealed_second"):
                assert set(theory.regret.values()) == {0}, (index, record.lot, fmt)
                checked += 1
            else:
                positive_elsewhere += sum(1 for r in theory.regret.values() if r > 0)
    assert checked == 40 * 4
    assert positive_elsewhere > 100, "first price, all-pay, Dutch and blind boxes do leave regret"


def test_pacing_makes_the_economist_regret_second_price_lots_it_shaded_away() -> None:
    from auction_night.theory import analyse_night

    config = all_bots_config("economist", pacing=True, budget_c=60)
    shaded = 0
    for index in range(40):
        transcript = play_seed(config, f"regret-paced-{index}")
        for theory, record in zip(
            analyse_night(transcript.state), transcript.state.records, strict=True
        ):
            if record.outcome.hammer.format in ("english", "sealed_second"):
                shaded += sum(1 for r in theory.regret.values() if r > 0)
    assert shaded > 0


@settings(max_examples=25, deadline=None)
@given(st.integers(0, 10**6))
def test_the_replay_model_reproduces_every_lot_of_a_night_of_arbitrary_play(seed: int) -> None:
    """Whatever the seats answer (numbers, nothing, too much), replaying a lot with each seat's
    own number gives back what the seat really earned, in every format and every round."""
    config = make_config()
    the_deal = deal(config, seed=f"replay-{seed}")
    transcript = play(config, the_deal, wild_players(config, seed))
    english_checked = 0
    for record in transcript.state.records:
        lot = the_deal.lot(record.lot)
        mech = config.night.mechanism_for(lot.format)
        setup = LotSetup.of(
            mech,
            tier=lot.tier,
            budgets=record.budgets_before,
            tie_key=lot.tie_key,
            values=lot.values,
            true_value=lot.blind_value,
            outcome=record.outcome,
        )
        assert sorted(setup.bids) == sorted(record.outcome.asked)
        for seat in record.outcome.asked:
            assert setup.payoff(seat, setup.bids[seat]) == setup.actual[seat]
            assert regret(setup, seat) >= 0
        english_checked += lot.format == "english"
    assert english_checked == 2


def test_a_seat_that_was_not_asked_has_no_entry() -> None:
    config = make_config(budget_c=40)
    transcript = play_seed(config, "regret-outs")
    from auction_night.theory import analyse_night

    seen_out = False
    for theory, record in zip(
        analyse_night(transcript.state), transcript.state.records, strict=True
    ):
        assert sorted(theory.regret) == sorted(record.outcome.asked)
        seen_out |= len(record.outcome.asked) < 8
    assert seen_out
