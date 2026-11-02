"""Counterfactual excess return: what a seat earned minus what the textbook bid would have."""

from dataclasses import replace

from night_support import all_bots_config, make_config, play_seed

from auction_night.contracts.events import HammerData
from auction_night.contracts.mechanism import FormatKey, make_mechanism
from auction_night.engine.formats import play_lot
from auction_night.theory import analyse_night
from auction_night.theory.counterfactual import (
    LotSetup,
    effective_bids,
    english_tie_key,
    excess_return,
)


def setup_for(
    key: FormatKey,
    bids: dict[int, int],
    values: dict[int, int],
    *,
    tie_key: dict[int, int] | None = None,
    tier: int = 100,
    true_value: int | None = None,
) -> LotSetup:
    mech = make_mechanism(key)
    seats = sorted(bids)
    draft = LotSetup(
        mech=mech,
        tier=tier,
        budgets=dict.fromkeys(seats, 10_000),
        tie_key=tie_key or {seat: seat for seat in seats},
        values=values,
        true_value=true_value,
        bids=bids,
        hammer=HammerData(format=key, unsold=True),
    )
    return replace(draft, hammer=draft.settle(bids))


def test_leaving_the_textbook_bid_in_a_second_price_lot_costs_what_it_lost() -> None:
    # seat 1 values the lot at 70 and bids 100: it wins at 90 and loses 20; the textbook bid
    # of 70 would have lost the lot, and earned nothing
    setup = setup_for("sealed_second", {1: 100, 2: 90, 3: 40}, {1: 70, 2: 90, 3: 40})
    assert excess_return(setup, 1, 70) == -20
    assert excess_return(setup, 2, 90) == 0  # seat 2 bid its value and earned nothing either way


def test_shading_deeper_than_the_textbook_in_a_first_price_lot_can_pay() -> None:
    # eight bidders: the textbook bid for a value of 100 is 87; seat 1 bid 80, won and kept 20;
    # the textbook bid would have won too and kept 13
    setup = setup_for("sealed_first", {1: 80, 2: 50, 3: 30}, {1: 100, 2: 50, 3: 30})
    assert excess_return(setup, 1, 87) == 20 - 13
    # a bid that lost, against a textbook bid that would have won at 87 and kept 13
    low = setup_for("sealed_first", {1: 40, 2: 50, 3: 30}, {1: 100, 2: 50, 3: 30})
    assert low.hammer.winner == 2
    assert excess_return(low, 1, 87) == 0 - 13


def test_playing_the_reference_bid_has_zero_excess_by_definition() -> None:
    setup = setup_for("all_pay", {1: 40, 2: 30, 3: 10}, {1: 90, 2: 70, 3: 20})
    for seat in (1, 2, 3):
        assert excess_return(setup, seat, setup.bids[seat]) == 0


def test_equal_numbers_are_settled_by_the_tie_keys_of_the_deal() -> None:
    values = {1: 80, 2: 80}
    first = setup_for("sealed_second", {1: 50, 2: 50}, values, tie_key={1: 0, 2: 1})
    assert first.hammer.winner == 1
    # seat 2 lost the tie; raising to 51 would have won at 50, leaving it 30 better off
    assert excess_return(first, 2, 51) == 0 - 30
    second = setup_for("sealed_second", {1: 50, 2: 50}, values, tie_key={1: 1, 2: 0})
    assert second.hammer.winner == 2 and excess_return(second, 2, 51) == 30 - 30


def test_in_a_dutch_lot_the_reference_is_a_grab_price() -> None:
    setup = setup_for("dutch", {1: 70, 2: 30}, {1: 100, 2: 60}, tier=100)
    assert setup.hammer.winner == 1 and setup.hammer.price == 70
    # grabbing at 50 would still have bought the lot, 20 coins cheaper
    assert excess_return(setup, 1, 50) == (100 - 70) - (100 - 50)
    # seat 2 sat out; a grab price of 80 would have bought the lot at 80 for a value of 60
    assert excess_return(setup, 2, 80) == 0 - (60 - 80)


def test_the_effective_numbers_of_each_clock() -> None:
    seats = [1, 2, 3, 4]
    budgets = dict.fromkeys(seats, 1000)
    keys = {s: s for s in seats}

    english = make_mechanism("english")
    outcome = play_lot(
        english,
        tier=100,
        budgets=budgets,
        tie_key=keys,
        answer=lambda step: {1: 90, 2: 70, 3: 0, 4: 30} if step.phase == "english_init" else {},
    )
    assert effective_bids(english, outcome) == {1: 90, 2: 70, 3: 0, 4: 30}
    assert english_tie_key(outcome) == {1: 0, 2: 1, 4: 2, 3: 3}  # winner, longest stayer, ..., idle

    dutch = make_mechanism("dutch")
    outcome = play_lot(
        dutch, tier=100, budgets=budgets, tie_key=keys, answer=lambda step: {1: 60, 2: 40, 4: 0}
    )
    assert effective_bids(dutch, outcome) == {1: 60, 2: 40, 3: 0, 4: 0}

    first = make_mechanism("sealed_first")
    outcome = play_lot(
        first, tier=100, budgets=budgets, tie_key=keys, answer=lambda step: {1: 60, 2: 4000}
    )
    assert effective_bids(first, outcome) == {1: 60, 2: 1000, 3: 0, 4: 0}  # clamped, 0 for none


def test_equal_exit_prices_in_english_are_ordered_by_who_stayed_longer() -> None:
    mech = make_mechanism("english", theta=2)
    seats = [1, 2, 3]
    budgets = dict.fromkeys(seats, 1000)
    keys = {1: 0, 2: 1, 3: 2}  # seat 1 wins every plain tie
    # seat 2 leaves first at 50; seats 1 and 3 then revise down to 50, which leaves at once
    outcome = play_lot(
        mech,
        tier=100,
        budgets=budgets,
        tie_key=keys,
        answer=lambda step: (
            {1: 80, 2: 50, 3: 70} if step.phase == "english_init" else {1: 10, 3: 10}
        ),
    )
    assert outcome.hammer.winner == 1 and outcome.hammer.price == 50
    assert outcome.hammer.tied == [1, 3]
    setup = LotSetup.of(
        mech,
        tier=100,
        budgets=budgets,
        tie_key=keys,
        values={1: 90, 2: 90, 3: 90},
        true_value=None,
        outcome=outcome,
    )
    # all three finish at 50; the replay must still say that seat 2 (who left first) lost
    assert setup.bids == {1: 50, 2: 50, 3: 50}
    assert setup.hammer == outcome.hammer
    assert setup.payoff(2, 50) == setup.actual[2] == 0
    assert setup.payoff(1, 50) == setup.actual[1] == 40


def test_an_economist_who_is_not_paced_leaves_no_excess_anywhere() -> None:
    config = all_bots_config("economist", pacing=False, budget_c=20_000)
    for index in range(20):
        transcript = play_seed(config, f"excess-{index}")
        for theory in analyse_night(transcript.state):
            assert set(theory.excess.values()) == {0}, (index, theory.lot)


def test_excess_return_is_defined_for_every_asked_seat_of_a_mixed_night() -> None:
    transcript = play_seed(make_config(), "excess-mixed")
    for theory, record in zip(
        analyse_night(transcript.state), transcript.state.records, strict=True
    ):
        assert sorted(theory.excess) == sorted(record.outcome.asked)
        assert all(isinstance(v, int) for v in theory.excess.values())
