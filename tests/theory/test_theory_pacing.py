"""The pacing factor: min(1, budget left / planned spend), exactly."""

from fractions import Fraction

from hypothesis import given
from hypothesis import strategies as st

from auction_night.theory.pacing import (
    alpha_milli,
    expected_share,
    pacing_alpha,
    planned_spend,
)

NIGHT = [(100, False)] * 5 + [(200, False)] * 4 + [(400, False)] * 3  # tiers add up to 2500


def test_an_ordinary_lot_is_expected_to_cost_n_minus_one_over_n_plus_one_of_its_tier() -> None:
    assert expected_share(8, blind=False) == Fraction(7, 9)
    assert expected_share(2, blind=False) == Fraction(1, 3)
    assert expected_share(1, blind=False) == Fraction(1, 3)  # raised to two bidders
    assert expected_share(8, blind=True) == 1 == expected_share(3, blind=True)


def test_the_planned_spend_is_the_expected_prices_split_between_the_bidders() -> None:
    assert planned_spend(NIGHT, 8) == Fraction(2500) * Fraction(7, 9) / 8
    assert planned_spend([(400, True)], 8) == Fraction(400, 8)
    assert planned_spend([(400, True), (200, False)], 4) == (400 + Fraction(200 * 3, 5)) / 4
    assert planned_spend([], 8) == 0


def test_alpha_is_the_share_of_the_plan_that_the_budget_covers() -> None:
    plan = Fraction(2500) * Fraction(7, 9) / 8  # 243 and a bit
    assert pacing_alpha(150, NIGHT, 8) == Fraction(150) / plan
    assert pacing_alpha(0, NIGHT, 8) == 0
    assert pacing_alpha(243, NIGHT, 8) < 1
    assert pacing_alpha(244, NIGHT, 8) == 1
    assert pacing_alpha(10**6, NIGHT, 8) == 1


def test_alpha_is_one_when_nothing_is_planned() -> None:
    assert pacing_alpha(50, [], 8) == 1
    assert pacing_alpha(0, [], 8) == 1


def test_a_blind_box_in_the_plan_lowers_alpha() -> None:
    ordinary = [(200, False)] * 6
    with_box = [(200, False)] * 5 + [(200, True)]
    assert pacing_alpha(100, with_box, 8) < pacing_alpha(100, ordinary, 8) < 1


def test_alpha_rises_as_the_night_goes_on_when_the_budget_is_untouched() -> None:
    alphas = [pacing_alpha(120, NIGHT[k:], 8) for k in range(len(NIGHT))]
    assert alphas == sorted(alphas) and alphas[0] < 1 == alphas[-1]


def test_more_bidders_means_a_bigger_share_of_the_tier_per_lot_but_a_smaller_one_each() -> None:
    plans = [planned_spend(NIGHT, n) for n in range(2, 9)]
    assert plans == sorted(plans, reverse=True)  # splitting between more bidders wins out


@given(
    st.integers(0, 2000),
    st.lists(st.tuples(st.sampled_from([100, 200, 400]), st.booleans()), max_size=12),
    st.integers(2, 8),
)
def test_alpha_is_between_zero_and_one_and_grows_with_the_budget(
    budget: int, lots: list[tuple[int, bool]], n: int
) -> None:
    alpha = pacing_alpha(budget, lots, n)
    assert 0 <= alpha <= 1
    assert alpha <= pacing_alpha(budget + 1, lots, n)
    plan = planned_spend(lots, n)
    if plan > 0 and budget < plan:
        assert alpha == Fraction(budget) / plan  # exactly proportional below the plan
    else:
        assert alpha == 1


def test_alpha_is_given_in_whole_thousandths_rounded_down() -> None:
    assert alpha_milli(Fraction(1)) == 1000
    assert alpha_milli(Fraction(0)) == 0
    assert alpha_milli(Fraction(2, 3)) == 666
    assert alpha_milli(Fraction(1999, 2000)) == 999
    assert alpha_milli(pacing_alpha(150, NIGHT, 8)) == 617  # 108/175 = 0.6171...
