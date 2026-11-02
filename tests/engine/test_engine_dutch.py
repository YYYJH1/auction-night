"""The Dutch auction with one-shot grab prices: ladder, budget rule, who is asked, ties."""

import copy

import pytest

from auction_night.contracts import make_mechanism
from auction_night.contracts.mechanism import FormatKey
from auction_night.engine.formats import (
    LotOutcome,
    dutch_asked,
    dutch_ladder,
    open_dutch,
    settle_dutch,
    step_dutch,
)

KEYS = {s: s - 1 for s in range(1, 9)}
RICH = dict.fromkeys(range(1, 9), 1000)
LADDER_200 = (200, 180, 160, 140, 120, 100, 80, 60, 40, 20)


def settle(
    tier: int,
    grabs,  # noqa: ANN001
    budgets=None,  # noqa: ANN001
    *,
    keys=KEYS,  # noqa: ANN001
    reserve: int = 0,
    levels: int = 10,
) -> LotOutcome:
    mech = make_mechanism("dutch", reserve=reserve, levels=levels)
    table = budgets if budgets is not None else {seat: 1000 for seat in grabs}
    return settle_dutch(mech, tier, grabs, table, keys)


# ------------------------------------------------------------------------------ ladder


def test_the_ladder_falls_in_equal_steps() -> None:
    assert dutch_ladder(200, 10) == LADDER_200
    assert dutch_ladder(100, 10) == (100, 90, 80, 70, 60, 50, 40, 30, 20, 10)
    assert dutch_ladder(400, 10)[:3] == (400, 360, 320)
    assert dutch_ladder(400, 10)[-1] == 40
    assert dutch_ladder(100, 5) == (100, 80, 60, 40, 20)


def test_levels_below_the_reserve_are_skipped() -> None:
    assert dutch_ladder(200, 10, reserve=50) == (200, 180, 160, 140, 120, 100, 80, 60)
    assert dutch_ladder(200, 10, reserve=60) == (200, 180, 160, 140, 120, 100, 80, 60)
    assert dutch_ladder(200, 10, reserve=61) == (200, 180, 160, 140, 120, 100, 80)
    assert dutch_ladder(200, 10, reserve=200) == (200,)
    assert dutch_ladder(200, 10, reserve=201) == ()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ((105, 10), "divisible"),
        ((200, 1), "levels"),
        ((0, 10), "tier"),
        ((200, 10, -1), "reserve"),
        ((True, 10), "integer"),
        ((200, 10.0), "integer"),
    ],
)
def test_bad_ladders_are_refused(args: tuple, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        dutch_ladder(*args)


# ---------------------------------------------------------------------------- the example


def test_the_worked_example() -> None:
    outcome = settle(200, {1: 130})
    assert (outcome.hammer.winner, outcome.hammer.price, outcome.hammer.payments) == (
        1,
        120,
        {1: 120},
    )
    result = outcome.dutch
    assert result is not None
    assert (result.ladder, result.level, result.price, result.buyer, result.unsold) == (
        list(LADDER_200),
        5,
        120,
        1,
        False,
    )
    assert outcome.hammer.bids is None, "grab prices stay private"
    assert [(r.seat, r.phase, r.status, r.value) for r in outcome.records] == [
        (1, "dutch", "ok", 130)
    ]


def test_a_seat_buys_at_the_first_level_at_or_below_its_grab_price() -> None:
    for grab, price in [
        (200, 200),
        (199, 180),
        (181, 180),
        (180, 180),
        (130, 120),
        (20, 20),
        (25, 20),
    ]:
        assert settle(200, {1: grab}).hammer.price == price


def test_a_grab_price_below_the_lowest_level_never_buys() -> None:
    outcome = settle(200, {1: 19, 2: 0})
    assert outcome.hammer.unsold
    assert outcome.dutch is not None and outcome.dutch.unsold
    assert (outcome.dutch.level, outcome.dutch.price, outcome.dutch.buyer) == (None, None, None)
    assert outcome.dutch.ladder == list(LADDER_200)
    assert (outcome.hammer.winner, outcome.hammer.price, outcome.hammer.payments) == (None, 0, {})


def test_the_earliest_level_decides() -> None:
    outcome = settle(200, {1: 150, 2: 170, 3: 90})
    assert (outcome.hammer.winner, outcome.hammer.price) == (2, 160)
    assert not outcome.hammer.tie_broken
    assert outcome.dutch is not None and outcome.dutch.level == 3


# ------------------------------------------------------------------------ budget rule


def test_a_level_above_the_remaining_budget_does_not_fire() -> None:
    # grab 200 would fire at 200, but the seat only has 150: first level it can pay is 140
    outcome = settle(200, {1: 200}, {1: 150})
    assert (outcome.hammer.winner, outcome.hammer.price) == (1, 140)
    record = outcome.records[0]
    assert (record.status, record.value, record.raw_value) == ("clamped", 150, 200)


def test_the_buyer_never_pays_more_than_the_remaining_budget() -> None:
    for budget in range(20, 260, 7):
        outcome = settle(200, {1: 999}, {1: budget})
        # the highest level the seat can pay
        assert outcome.hammer.price == max(p for p in LADDER_200 if p <= budget)


def test_a_richer_seat_with_a_lower_grab_price_loses_to_an_earlier_level() -> None:
    outcome = settle(200, {1: 999, 2: 150}, {1: 150, 2: 1000})
    # seat 1 is capped at 150 and fires at 140, seat 2 fires at 140 too: tie, rank decides
    assert (outcome.hammer.winner, outcome.hammer.price) == (1, 140)
    assert outcome.hammer.tie_broken and outcome.hammer.tied == [1, 2]


def test_seats_that_cannot_afford_the_lowest_level_are_not_asked() -> None:
    budgets = {1: 1000, 2: 19, 3: 20, 4: 0, 5: 5}
    ladder = dutch_ladder(200, 10)
    assert dutch_asked(budgets, ladder) == (1, 3)
    step = open_dutch(make_mechanism("dutch"), 200, budgets, KEYS)
    assert (step.ask, step.phase, step.done) == ((1, 3), "dutch", False)
    outcome = step_dutch(step.state, {1: 90, 3: 500}).outcome
    assert outcome is not None
    assert outcome.asked == (1, 3)
    assert (outcome.hammer.winner, outcome.hammer.price) == (1, 80)
    assert {r.seat for r in outcome.records} == {1, 3}
    with pytest.raises(ValueError, match="not asked"):
        step_dutch(step.state, {2: 100})
    with pytest.raises(ValueError, match="not asked"):
        settle(200, {2: 100}, budgets)


def test_the_lowest_level_in_play_depends_on_the_reserve() -> None:
    budgets = {1: 1000, 2: 55, 3: 60}
    mech = make_mechanism("dutch", reserve=50)
    ladder = dutch_ladder(200, 10, 50)
    assert ladder[-1] == 60
    assert dutch_asked(budgets, ladder) == (1, 3)
    step = open_dutch(mech, 200, budgets, KEYS)
    assert step.ask == (1, 3)


def test_a_lot_nobody_can_afford_is_decided_without_questions() -> None:
    step = open_dutch(make_mechanism("dutch"), 200, {1: 10, 2: 19}, KEYS)
    assert step.done and step.ask == ()
    assert step.outcome is not None and step.outcome.hammer.unsold
    assert step.outcome.dutch is not None and step.outcome.dutch.unsold


def test_a_reserve_above_the_tier_leaves_an_empty_ladder() -> None:
    step = open_dutch(make_mechanism("dutch", reserve=300), 200, RICH, KEYS)
    assert step.done
    assert step.outcome is not None
    assert step.outcome.dutch is not None and step.outcome.dutch.ladder == []


# ----------------------------------------------------------------------------- reserve


def test_nothing_sells_below_the_reserve() -> None:
    outcome = settle(200, {1: 100}, reserve=130)
    assert outcome.hammer.unsold
    assert outcome.dutch is not None and outcome.dutch.ladder == [200, 180, 160, 140]
    assert settle(200, {1: 140}, reserve=130).hammer.price == 140


def test_a_level_equal_to_the_reserve_is_in_play() -> None:
    outcome = settle(200, {1: 100}, reserve=100)
    assert (outcome.hammer.winner, outcome.hammer.price) == (1, 100)


# ----------------------------------------------------------------------------- ties


def test_several_seats_on_the_same_level_go_to_the_tie_key() -> None:
    keys = {1: 5, 2: 1, 3: 0, 4: 2}
    outcome = settle(200, {1: 150, 2: 141, 3: 150, 4: 90}, keys=keys)
    assert (outcome.hammer.winner, outcome.hammer.price) == (3, 140)
    assert outcome.hammer.tie_broken
    assert outcome.hammer.tied == [1, 2, 3]
    assert outcome.dutch is not None and outcome.dutch.buyer == 3


def test_a_single_seat_on_the_earliest_level_is_no_tie() -> None:
    outcome = settle(200, {1: 150, 2: 170, 3: 170 - 20})
    assert not outcome.hammer.tie_broken and outcome.hammer.tied == []


def test_equal_tie_keys_fall_back_to_the_lower_seat() -> None:
    outcome = settle(200, {2: 150, 3: 150}, keys={1: 3, 2: 3, 3: 3})
    assert outcome.hammer.winner == 2


# -------------------------------------------------------------------- abstain, newly out


def test_a_missing_grab_price_is_an_abstention() -> None:
    outcome = settle(200, {1: None, 2: 100}, {1: 1000, 2: 1000, 3: 1000})
    states = {r.seat: (r.status, r.value, r.reason) for r in outcome.records}
    assert states == {
        1: ("abstain", None, "missing"),
        2: ("ok", 100, None),
        3: ("abstain", None, "missing"),
    }
    assert (outcome.hammer.winner, outcome.hammer.price) == (2, 100)
    nobody = settle(200, {}, {1: 1000, 2: 1000})
    assert nobody.hammer.unsold


def test_a_zero_grab_price_never_buys_but_is_not_an_abstention() -> None:
    outcome = settle(200, {1: 0})
    assert outcome.hammer.unsold
    assert (outcome.records[0].status, outcome.records[0].value) == ("ok", 0)


def test_the_buyer_who_spends_everything_is_announced() -> None:
    outcome = settle(200, {1: 999}, {1: 140, 2: 500}, keys={1: 0, 2: 1})
    assert (outcome.hammer.winner, outcome.hammer.price, outcome.hammer.newly_out) == (1, 140, [1])
    outcome = settle(200, {1: 999}, {1: 150, 2: 500}, keys={1: 0, 2: 1})
    assert (outcome.hammer.price, outcome.hammer.newly_out) == (140, [])


# ------------------------------------------------------------------------------ misuse


@pytest.mark.parametrize("key", ["english", "sealed_first", "all_pay"])
def test_only_the_dutch_format_opens_here(key: FormatKey) -> None:
    with pytest.raises(ValueError, match="not a Dutch"):
        settle_dutch(make_mechanism(key), 200, {}, {1: 100}, KEYS)


def test_a_tier_that_does_not_divide_into_levels_is_refused() -> None:
    with pytest.raises(ValueError, match="divisible"):
        settle(105, {1: 50})


@pytest.mark.parametrize("bad", [-1, True, "130", float("inf")])
def test_bad_grab_prices_are_refused(bad) -> None:  # noqa: ANN001
    with pytest.raises((ValueError, TypeError)):
        settle(200, {1: bad})


def test_other_level_counts() -> None:
    outcome = settle(100, {1: 55}, levels=5)
    assert outcome.dutch is not None
    assert outcome.dutch.ladder == [100, 80, 60, 40, 20]
    assert (outcome.hammer.price, outcome.dutch.level) == (40, 4)
    outcome = settle(100, {1: 55}, levels=20)
    assert outcome.hammer.price == 55


def test_settlement_is_pure() -> None:
    grabs, budgets, keys = {1: 130, 2: 90}, {1: 500, 2: 80}, dict(KEYS)
    before = copy.deepcopy((grabs, budgets, keys))
    first = settle(200, grabs, budgets, keys=keys)
    second = settle(200, grabs, budgets, keys=keys)
    assert (grabs, budgets, keys) == before
    assert first == second
