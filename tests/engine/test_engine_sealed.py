"""The sealed formats: first price, second price, all-pay and the blind box."""

import copy
from fractions import Fraction

import pytest

from auction_night.contracts import make_mechanism
from auction_night.contracts.events import ActionData
from auction_night.contracts.mechanism import FormatKey
from auction_night.engine.formats import LotOutcome, open_sealed, settle_sealed, step_sealed

BUDGETS = {1: 500, 2: 500, 3: 500}
KEYS = {1: 0, 2: 1, 3: 2}
EXAMPLE = {1: 120, 2: 90, 3: 40}


def settle(
    key: FormatKey,
    bids,  # noqa: ANN001
    *,
    budgets=BUDGETS,  # noqa: ANN001
    keys=KEYS,  # noqa: ANN001
    reserve: int = 0,
    true_value: int | None = None,
) -> LotOutcome:
    if key == "blind_box" and true_value is None:
        true_value = 100
    return settle_sealed(
        make_mechanism(key, reserve=reserve), bids, budgets, keys, true_value=true_value
    )


# -------------------------------------------------------------------------- the examples


@pytest.mark.parametrize(
    ("key", "price", "payments"),
    [
        ("sealed_first", 120, {1: 120}),
        ("sealed_second", 90, {1: 90}),
        ("blind_box", 90, {1: 90}),
        ("all_pay", 120, {1: 120, 2: 90, 3: 40}),
    ],
)
def test_the_worked_examples(key: FormatKey, price: int, payments: dict[int, int]) -> None:
    hammer = settle(key, EXAMPLE).hammer
    assert (hammer.winner, hammer.price, hammer.payments) == (1, price, payments)
    assert hammer.bids == EXAMPLE
    assert not hammer.unsold
    assert not hammer.tie_broken
    assert hammer.tied == []


def test_the_blind_box_reveals_the_hidden_value_and_others_do_not() -> None:
    assert settle("blind_box", EXAMPLE, true_value=137).hammer.true_value == 137
    assert settle("sealed_second", EXAMPLE).hammer.true_value is None


# --------------------------------------------------------------------------- reserve


def test_second_price_pays_at_least_the_reserve() -> None:
    assert settle("sealed_second", EXAMPLE, reserve=100).hammer.price == 100
    assert settle("sealed_second", EXAMPLE, reserve=90).hammer.price == 90
    assert settle("sealed_second", EXAMPLE, reserve=89).hammer.price == 90
    assert settle("blind_box", EXAMPLE, reserve=110).hammer.price == 110


def test_nothing_is_sold_below_the_reserve() -> None:
    for key in ("sealed_first", "sealed_second", "blind_box"):
        hammer = settle(key, EXAMPLE, reserve=121).hammer
        assert hammer.unsold
        assert (hammer.winner, hammer.price, hammer.payments) == (None, 0, {})
        assert hammer.bids == EXAMPLE, "the bids are revealed even when nothing is sold"
        assert hammer.tied == [] and not hammer.tie_broken
    assert not settle("sealed_first", EXAMPLE, reserve=120).hammer.unsold


def test_a_bid_equal_to_the_reserve_qualifies() -> None:
    hammer = settle("sealed_second", {1: 50, 2: 10}, reserve=50).hammer
    assert (hammer.winner, hammer.price) == (1, 50)


def test_a_lone_bidder_pays_the_reserve_in_second_price() -> None:
    free = settle("sealed_second", {1: 70}).hammer
    assert (free.winner, free.price, free.payments) == (1, 0, {1: 0})
    paid = settle("sealed_second", {1: 70}, reserve=40).hammer
    assert (paid.winner, paid.price) == (1, 40)
    assert settle("sealed_second", {1: 30}, reserve=40).hammer.unsold


def test_sub_reserve_rivals_do_not_change_the_price() -> None:
    assert settle("sealed_second", {1: 80, 2: 30}, reserve=50).hammer.price == 50
    assert settle("sealed_second", {1: 80, 2: 60}, reserve=50).hammer.price == 60


def test_all_pay_has_no_reserve() -> None:
    with pytest.raises(ValueError, match="reserve"):
        make_mechanism("all_pay", reserve=10)
    assert settle("all_pay", {1: 1}).hammer.winner == 1


# ----------------------------------------------------------------------------- ties


def test_ties_go_to_the_lowest_tie_key() -> None:
    keys = {1: 5, 2: 0, 3: 1}
    for key in ("sealed_first", "sealed_second", "all_pay", "blind_box"):
        hammer = settle(key, {1: 80, 2: 80, 3: 80}, keys=keys).hammer
        assert hammer.winner == 2
        assert hammer.tie_broken
        assert hammer.tied == [1, 2, 3]
    only_two = settle("sealed_first", {1: 80, 2: 80, 3: 79}, keys=keys).hammer
    assert (only_two.winner, only_two.tied) == (2, [1, 2])


def test_a_tie_in_second_price_pays_the_tied_bid() -> None:
    hammer = settle("sealed_second", {1: 80, 2: 80, 3: 10}, keys={1: 0, 2: 1, 3: 2}).hammer
    assert (hammer.winner, hammer.price) == (1, 80)


def test_equal_tie_keys_fall_back_to_the_lower_seat() -> None:
    hammer = settle("sealed_first", {2: 80, 3: 80}, keys={1: 7, 2: 7, 3: 7}).hammer
    assert hammer.winner == 2


def test_a_tie_below_the_top_is_not_a_tie_for_the_win() -> None:
    hammer = settle("sealed_first", {1: 100, 2: 50, 3: 50}).hammer
    assert (hammer.winner, hammer.tie_broken, hammer.tied) == (1, False, [])


# ---------------------------------------------------------------------- clamp, abstain


def test_a_bid_above_the_budget_is_clamped_and_recorded() -> None:
    budgets = {1: 100, 2: 500, 3: 500}
    outcome = settle("sealed_first", {1: 250, 2: 90, 3: 40}, budgets=budgets)
    first = outcome.records[0]
    assert (first.seat, first.status, first.value, first.raw_value) == (1, "clamped", 100, 250)
    assert (outcome.hammer.winner, outcome.hammer.price) == (1, 100)
    assert outcome.hammer.bids == {1: 100, 2: 90, 3: 40}
    action = first.to_action("all in")
    assert isinstance(action, ActionData)
    assert (action.status, action.value, action.raw_value, action.phase) == (
        "clamped",
        100,
        250,
        "sealed",
    )


def test_a_clamped_bid_ties_with_an_exact_bid() -> None:
    budgets = {1: 100, 2: 100}
    hammer = settle("sealed_first", {1: 400, 2: 100}, budgets=budgets, keys={1: 3, 2: 1}).hammer
    assert (hammer.winner, hammer.tie_broken) == (2, True)


def test_bids_are_floored() -> None:
    outcome = settle("sealed_first", {1: 120.9, 2: Fraction(181, 2), 3: 40})
    assert outcome.hammer.bids == {1: 120, 2: 90, 3: 40}
    assert [r.status for r in outcome.records] == ["ok", "ok", "ok"]


def test_a_missing_bid_is_an_abstention_never_a_default_price() -> None:
    outcome = settle("sealed_first", {1: 120, 3: None})
    reasons = {r.seat: (r.status, r.value, r.reason) for r in outcome.records}
    assert reasons[2] == ("abstain", None, "missing")
    assert reasons[3] == ("abstain", None, "missing")
    assert outcome.hammer.bids == {1: 120, 2: 0, 3: 0}
    abstain = outcome.records[1].to_action()
    assert (abstain.status, abstain.value, abstain.reason) == ("abstain", None, "missing")


@pytest.mark.parametrize("key", ["sealed_first", "sealed_second", "all_pay", "blind_box"])
def test_nobody_bidding_leaves_the_lot_unsold(key: FormatKey) -> None:
    for bids in ({}, {1: None, 2: None, 3: None}, {1: 0, 2: 0, 3: 0}):
        hammer = settle(key, bids).hammer
        assert hammer.unsold
        assert hammer.payments == {}
        assert hammer.bids == {1: 0, 2: 0, 3: 0}


def test_all_pay_charges_every_positive_bid() -> None:
    hammer = settle("all_pay", {1: 70, 2: 0, 3: 40}).hammer
    assert hammer.payments == {1: 70, 3: 40}
    assert hammer.winner == 1


def test_all_pay_clamps_everyones_payment_to_the_budget() -> None:
    budgets = {1: 60, 2: 500, 3: 30}
    hammer = settle("all_pay", {1: 99, 2: 200, 3: 99}, budgets=budgets).hammer
    assert hammer.payments == {1: 60, 2: 200, 3: 30}
    assert hammer.winner == 2


def test_budget_exhausted_seats_are_announced() -> None:
    budgets = {1: 90, 2: 500, 3: 40}
    hammer = settle("all_pay", {1: 90, 2: 100, 3: 40}, budgets=budgets).hammer
    assert hammer.newly_out == [1, 3]
    hammer = settle("sealed_first", {1: 90, 2: 10}, budgets=budgets).hammer
    assert hammer.newly_out == [1]
    hammer = settle("sealed_first", {1: 89, 2: 10}, budgets=budgets).hammer
    assert hammer.newly_out == []
    hammer = settle("sealed_second", {1: 90, 2: 90}, budgets=budgets).hammer
    assert (hammer.winner, hammer.price, hammer.newly_out) == (1, 90, [1])


# ------------------------------------------------------------------- who is asked


def test_only_seats_with_budget_are_asked() -> None:
    budgets = {1: 500, 2: 0, 3: 7, 5: 1}
    keys = {1: 0, 2: 1, 3: 2, 5: 3}
    mech = make_mechanism("sealed_first")
    step = open_sealed(mech, budgets, keys)
    assert (step.ask, step.phase, step.done) == ((1, 3, 5), "sealed", False)
    outcome = step_sealed(step.state, {1: 5, 3: 6, 5: 9}).outcome
    assert outcome is not None
    assert outcome.asked == (1, 3, 5)
    assert outcome.hammer.bids == {1: 5, 3: 6, 5: 1}
    assert outcome.records[2].status == "clamped"
    with pytest.raises(ValueError, match="not asked"):
        step_sealed(step.state, {2: 5})
    with pytest.raises(ValueError, match="not asked"):
        settle_sealed(mech, {2: 5}, budgets, keys)


def test_only_seats_that_can_be_asked_need_a_tie_key() -> None:
    mech = make_mechanism("sealed_first")
    outcome = settle_sealed(mech, {1: 5}, {1: 100, 2: 0}, {1: 0})
    assert outcome.hammer.winner == 1
    assert outcome.hammer.bids == {1: 5}
    with pytest.raises(ValueError, match="tie key"):
        settle_sealed(mech, {1: 5}, {1: 100, 2: 3}, {1: 0})


def test_a_table_where_nobody_can_bid_is_decided_at_once() -> None:
    mech = make_mechanism("sealed_second")
    step = open_sealed(mech, {1: 0, 2: 0}, {1: 0, 2: 1})
    assert step.done
    assert step.ask == ()
    assert step.outcome is not None and step.outcome.hammer.unsold
    assert step.outcome.hammer.bids == {}


def test_open_then_step_equals_settle() -> None:
    mech = make_mechanism("sealed_second", reserve=20)
    step = open_sealed(mech, BUDGETS, KEYS)
    done = step_sealed(step.state, EXAMPLE)
    assert done.done and done.ask == () and done.phase is None
    assert done.outcome == settle_sealed(mech, EXAMPLE, BUDGETS, KEYS)
    assert done.records == done.outcome.records


# ----------------------------------------------------------------------- bad input


@pytest.mark.parametrize(
    ("bids", "error"),
    [
        ({1: -1}, ValueError),
        ({1: True}, TypeError),
        ({1: "120"}, TypeError),
        ({1: float("nan")}, ValueError),
        ({1: float("inf")}, ValueError),
        ({1: 120.5, 4: 3}, ValueError),
    ],
)
def test_bad_bids_are_refused(bids, error) -> None:  # noqa: ANN001
    with pytest.raises(error):
        settle("sealed_first", bids)


@pytest.mark.parametrize(
    ("budgets", "keys"),
    [
        ({9: 100}, {9: 0}),
        ({1: -5}, {1: 0}),
        ({1: True}, {1: 0}),
        ({1: 100}, {}),
        ({1: 100}, {1: True}),
        ({0: 100}, {0: 0}),
    ],
)
def test_bad_tables_are_refused(budgets, keys) -> None:  # noqa: ANN001
    with pytest.raises(ValueError):
        settle_sealed(make_mechanism("sealed_first"), {}, budgets, keys)


def test_true_value_is_required_for_the_blind_box_and_refused_elsewhere() -> None:
    with pytest.raises(ValueError, match="true_value"):
        settle_sealed(make_mechanism("blind_box"), EXAMPLE, BUDGETS, KEYS)
    with pytest.raises(ValueError, match="true_value"):
        settle_sealed(make_mechanism("sealed_first"), EXAMPLE, BUDGETS, KEYS, true_value=5)
    with pytest.raises(ValueError, match="true_value"):
        settle_sealed(make_mechanism("blind_box"), EXAMPLE, BUDGETS, KEYS, true_value=-1)


@pytest.mark.parametrize("key", ["english", "dutch"])
def test_clock_formats_are_not_sealed(key: FormatKey) -> None:
    with pytest.raises(ValueError, match="not a sealed format"):
        settle_sealed(make_mechanism(key), EXAMPLE, BUDGETS, KEYS)


# --------------------------------------------------------------------------- purity


@pytest.mark.parametrize("key", ["sealed_first", "sealed_second", "all_pay", "blind_box"])
def test_settlement_changes_nothing_it_was_given(key: FormatKey) -> None:
    bids, budgets, keys = dict(EXAMPLE), dict(BUDGETS), dict(KEYS)
    before = copy.deepcopy((bids, budgets, keys))
    first = settle(key, bids, budgets=budgets, keys=keys)
    second = settle(key, bids, budgets=budgets, keys=keys)
    assert (bids, budgets, keys) == before
    assert first == second
