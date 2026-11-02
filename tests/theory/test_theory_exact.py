"""Exact expectations: the T1 targets of the equivalence test, and a check against the engine.

Two kinds of test. The first pins the numbers of the equivalence test: with eight bidders and
the tier mix of a night (five lots of tier 100, four of 200, three of 400), the exact expected
revenue as a share of the tier. The second does not trust the formulas of the module: for small
cases it plays *every* possible profile of values through the engine, bids with the reference
bids, and compares the mean and variance of the revenue with the module's.
"""

import math
from fractions import Fraction
from itertools import product

import pytest

from auction_night.contracts.config import BlindConfig
from auction_night.contracts.mechanism import MECHANISMS, make_mechanism
from auction_night.engine.deal import blind_bounds
from auction_night.engine.formats import play_lot
from auction_night.theory import expected_revenue, reference_bid_int, revenue_moments
from auction_night.theory.exact import (
    Moments,
    all_pay,
    blind_box,
    dutch,
    first_price,
    second_price,
    textbook_revenue,
)

MIX = {100: 5, 200: 4, 400: 3}  # lots of each tier in a night of twelve


def mixture(moments_of) -> tuple[float, float]:
    """Mean and standard deviation of revenue / tier for a lot drawn from the night's tier mix."""
    mean = Fraction(0)
    second = Fraction(0)
    for tier, count in MIX.items():
        moments = moments_of(tier)
        mean += Fraction(count, 12) * moments.mean / tier
        second += Fraction(count, 12) * (moments.variance / tier**2 + (moments.mean / tier) ** 2)
    return float(mean), math.sqrt(float(second - mean * mean))


# ------------------------------------------------------------------------ the T1 targets


@pytest.mark.parametrize(
    ("name", "moments_of", "mean", "sd", "tolerance"),
    [
        ("second price and English", lambda t: second_price(t, 8), 0.7796, 0.132, 0.0047),
        ("first price", lambda t: first_price(t, 8), 0.7771, 0.087, 0.0031),
        ("all-pay", lambda t: all_pay(t, 8), 0.7805, 0.545, 0.0193),
        ("Dutch, ten levels, one grab price", lambda t: dutch(t, 8, 10), 0.7303, 0.090, 0.0032),
    ],
)
def test_the_exact_revenue_of_a_night_of_eight_bidders(name, moments_of, mean, sd, tolerance):
    got_mean, got_sd = mixture(moments_of)
    assert round(got_mean, 4) == mean, name
    assert round(got_sd, 3) == sd, name
    # the tolerance of 20,000 lots at five standard errors
    assert round(5 * got_sd / math.sqrt(20_000), 4) == tolerance, name


def test_the_per_tier_values_lie_a_little_off_seven_ninths() -> None:
    expected = {
        "second": {100: 0.7806, 200: 0.7792, 400: 0.7785},
        "first": {100: 0.7765, 200: 0.7774, 400: 0.7776},
        "all_pay": {100: 0.7826, 200: 0.7795, 400: 0.7783},
        "dutch": {100: 0.7310, 200: 0.7304, 400: 0.7292},
    }
    makers = {
        "second": lambda t: second_price(t, 8),
        "first": lambda t: first_price(t, 8),
        "all_pay": lambda t: all_pay(t, 8),
        "dutch": lambda t: dutch(t, 8, 10),
    }
    for name, per_tier in expected.items():
        for tier, share in per_tier.items():
            assert round(float(makers[name](tier).mean) / tier, 4) == share, (name, tier)


def test_the_discrete_engine_differs_from_the_textbook_limit_by_less_than_one_percent_of_tier() -> (
    None
):
    for tier in (100, 200, 400):
        limit = textbook_revenue(tier, 8)
        assert limit == Fraction(7 * tier, 9)
        for moments in (second_price(tier, 8), first_price(tier, 8), all_pay(tier, 8)):
            assert abs(moments.mean - limit) < tier * Fraction(1, 100)
        assert dutch(tier, 8, 10).mean < limit - tier * Fraction(1, 25)  # ten levels cost 4.5%


def test_the_blind_box_is_worth_about_its_tier_and_sells_below_it() -> None:
    blind = BlindConfig()
    for tier in (100, 200, 400):
        moments = blind_box(tier, 8, blind)
        assert Fraction(95, 100) * tier < moments.mean < Fraction(98, 100) * tier


# --------------------------------------------------------------------- the dispatch


def test_revenue_moments_follow_the_pricing_rule_of_the_mechanism() -> None:
    blind = BlindConfig()
    assert revenue_moments(MECHANISMS["english"], 200, 8) == second_price(200, 8)
    assert revenue_moments(MECHANISMS["sealed_second"], 200, 8) == second_price(200, 8)
    assert revenue_moments(MECHANISMS["sealed_first"], 200, 8) == first_price(200, 8)
    assert revenue_moments(MECHANISMS["all_pay"], 200, 8) == all_pay(200, 8)
    assert revenue_moments(MECHANISMS["dutch"], 200, 8) == dutch(200, 8, 10)
    assert revenue_moments(MECHANISMS["blind_box"], 200, 8, blind) == blind_box(200, 8, blind)
    ladder = make_mechanism("dutch", levels=5)
    assert revenue_moments(ladder, 200, 8) == dutch(200, 8, 5)
    assert dutch(200, 8, 5).mean < dutch(200, 8, 10).mean  # fewer levels, a coarser clock
    assert expected_revenue(MECHANISMS["english"], 200, 8) == second_price(200, 8).mean


def test_cases_outside_the_closed_form_have_no_answer_and_not_a_wrong_one() -> None:
    assert revenue_moments(make_mechanism("sealed_second", reserve=20), 200, 8) is None
    assert revenue_moments(MECHANISMS["english"], 200, 1) is None
    assert expected_revenue(MECHANISMS["english"], 200, 0) is None
    with pytest.raises(ValueError, match="prior"):
        revenue_moments(MECHANISMS["blind_box"], 200, 8)


def test_the_results_are_exact_fractions() -> None:
    moments = first_price(100, 8)
    assert isinstance(moments, Moments)
    assert isinstance(moments.mean, Fraction) and isinstance(moments.variance, Fraction)
    assert moments.variance > 0


# --------------------------------------------------- every profile, through the engine


def enumerate_revenue(
    key: str, tier: int, n: int, *, blind: BlindConfig | None = None, levels: int | None = None
) -> tuple[Fraction, Fraction]:
    """Mean and variance of the revenue, by playing every profile of values with the engine."""
    mech = make_mechanism(key, levels=levels)  # type: ignore[arg-type]
    seats = range(1, n + 1)
    budgets = dict.fromkeys(seats, 10**6)
    keys = {seat: seat for seat in seats}
    bounds = blind_bounds(tier, blind) if blind is not None else None
    total = square = Fraction(0)
    count = 0

    def run(values: dict[int, int], signals: dict[int, int], hidden: int | None) -> int:
        bids = {
            seat: reference_bid_int(
                mech,
                tier=tier,
                n=n,
                budget=10**6,
                value=values.get(seat),
                signal=signals.get(seat),
                blind=bounds,
            )
            for seat in seats
        }
        outcome = play_lot(
            mech,
            tier=tier,
            budgets=budgets,
            tie_key=keys,
            answer=lambda step: bids if step.phase != "english_revise" else {},
            true_value=hidden,
        )
        return sum(outcome.hammer.payments.values())

    if bounds is None:
        for profile in product(range(tier + 1), repeat=n):
            revenue = run(dict(zip(seats, profile, strict=True)), {}, None)
            total += revenue
            square += revenue * revenue
            count += 1
    else:
        offsets = range(-bounds.noise, bounds.noise + 1)
        for hidden in range(bounds.low, bounds.high + 1):
            for noises in product(offsets, repeat=n):
                signals = {seat: hidden + e for seat, e in zip(seats, noises, strict=True)}
                revenue = run({}, signals, hidden)
                total += revenue
                square += revenue * revenue
                count += 1
    mean = total / count
    return mean, square / count - mean * mean


@pytest.mark.parametrize(
    ("key", "tier", "n", "formula"),
    [
        ("sealed_second", 10, 3, lambda t, n: second_price(t, n)),
        ("english", 10, 3, lambda t, n: second_price(t, n)),
        ("english", 7, 5, lambda t, n: second_price(t, n)),
        ("sealed_first", 10, 3, lambda t, n: first_price(t, n)),
        ("sealed_first", 9, 4, lambda t, n: first_price(t, n)),
        ("all_pay", 10, 3, lambda t, n: all_pay(t, n)),
        ("all_pay", 6, 4, lambda t, n: all_pay(t, n)),
        ("dutch", 20, 3, lambda t, n: dutch(t, n, 10)),
    ],
)
def test_the_formulas_equal_the_engine_averaged_over_every_profile(key, tier, n, formula):
    mean, variance = enumerate_revenue(key, tier, n)
    expected = formula(tier, n)
    assert (mean, variance) == (expected.mean, expected.variance)


@pytest.mark.parametrize(("tier", "n"), [(20, 2), (20, 3), (10, 3)])
def test_the_blind_box_formula_equals_the_engine_averaged_over_every_profile(tier, n):
    blind = BlindConfig()
    mean, variance = enumerate_revenue("blind_box", tier, n, blind=blind)
    expected = blind_box(tier, n, blind)
    assert (mean, variance) == (expected.mean, expected.variance)
