"""Reference bids: the textbook bid of each pricing rule, exactly."""

import math
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from auction_night.contracts.mechanism import FORMAT_KEYS, MECHANISMS
from auction_night.engine.deal import BlindBounds
from auction_night.theory import reference_bid, reference_bid_int

BLIND = BlindBounds(low=100, high=300, noise=40)  # tier 200: prior 0.5..1.5, noise 0.2


def float_textbook(fmt: str, v: int, n: int, tier: int, signal: int | None = None) -> float:
    """The same formulas in floating point, the way a quick simulation script writes them."""
    if fmt in ("english", "sealed_second"):
        return v
    if fmt in ("sealed_first", "dutch"):
        return (n - 1) / n * v
    if fmt == "all_pay":
        return (n - 1) / n * v**n / tier ** (n - 1)
    assert signal is not None
    e = 0.2 * tier
    return min(max(signal - e * (n - 2) / n, 0.5 * tier), 1.5 * tier)


@pytest.mark.parametrize("n", [2, 3, 5, 8])
@pytest.mark.parametrize("value", [0, 1, 7, 100, 133, 199, 200])
def test_second_price_bids_the_value_first_price_shades_by_n_minus_one_over_n(
    value: int, n: int
) -> None:
    for fmt in ("english", "sealed_second"):
        assert reference_bid(MECHANISMS[fmt], tier=200, n=n, value=value) == Fraction(value)
    for fmt in ("sealed_first", "dutch"):
        assert reference_bid(MECHANISMS[fmt], tier=200, n=n, value=value) == Fraction(
            (n - 1) * value, n
        )


@pytest.mark.parametrize("n", [2, 3, 8])
@pytest.mark.parametrize("value", [0, 1, 50, 133, 200])
def test_all_pay_bids_n_minus_one_over_n_times_value_to_the_n_over_tier_to_the_n_minus_one(
    value: int, n: int
) -> None:
    bid = reference_bid(MECHANISMS["all_pay"], tier=200, n=n, value=value)
    assert bid == Fraction(n - 1, n) * Fraction(value) ** n / Fraction(200) ** (n - 1)
    assert 0 <= bid <= value * Fraction(n - 1, n)  # never more than the first-price bid


@pytest.mark.parametrize("n", [2, 3, 8])
@pytest.mark.parametrize("signal", [60, 100, 130, 200, 290, 330])
def test_a_blind_box_bid_is_the_appraisal_less_the_shading_inside_the_prior(
    signal: int, n: int
) -> None:
    bid = reference_bid(MECHANISMS["blind_box"], tier=200, n=n, signal=signal, blind=BLIND)
    shaded = Fraction(signal) - Fraction(40 * (n - 2), n)
    assert bid == min(max(shaded, Fraction(100)), Fraction(300))


def test_the_reference_is_read_off_the_pricing_rule_not_the_name_of_the_format() -> None:
    by_rule: dict[tuple[str, str], set[Fraction]] = {}
    for fmt in FORMAT_KEYS:
        mech = MECHANISMS[fmt]
        if mech.value_model == "common":
            continue
        bid = reference_bid(mech, tier=400, n=6, value=173)
        by_rule.setdefault((mech.pricing, mech.value_model), set()).add(bid)
    assert all(len(bids) == 1 for bids in by_rule.values())  # same rule, same bid
    assert len({next(iter(bids)) for bids in by_rule.values()}) == 3  # three different rules


def test_fewer_than_two_bidders_are_treated_as_two() -> None:
    mech = MECHANISMS["sealed_first"]
    assert reference_bid(mech, tier=100, n=1, value=80) == reference_bid(
        mech, tier=100, n=2, value=80
    )
    assert reference_bid(mech, tier=100, n=0, value=80) == Fraction(40)


def test_a_missing_number_is_an_error_and_not_a_zero_bid() -> None:
    with pytest.raises(ValueError, match="needs the value"):
        reference_bid(MECHANISMS["english"], tier=100, n=8)
    with pytest.raises(ValueError, match="appraisal"):
        reference_bid(MECHANISMS["blind_box"], tier=100, n=8, value=50)
    with pytest.raises(ValueError, match="prior"):
        reference_bid(MECHANISMS["blind_box"], tier=100, n=8, signal=100)


def test_the_integer_bid_is_rounded_down_and_clamped_to_the_budget() -> None:
    mech = MECHANISMS["sealed_first"]
    assert reference_bid_int(mech, tier=100, n=8, budget=500, value=99) == math.floor(
        Fraction(7 * 99, 8)
    )
    assert reference_bid_int(mech, tier=100, n=8, budget=50, value=99) == 50
    assert reference_bid_int(mech, tier=100, n=8, budget=0, value=99) == 0
    assert reference_bid_int(mech, tier=100, n=8, budget=500, value=0) == 0
    exact = reference_bid(mech, tier=100, n=8, value=99)
    assert isinstance(exact, Fraction) and exact.denominator == 8


@given(
    st.sampled_from(FORMAT_KEYS),
    st.sampled_from([100, 200, 400]),
    st.integers(2, 8),
    st.data(),
)
def test_the_exact_bid_agrees_with_the_same_formulas_in_floating_point(
    fmt: str, tier: int, n: int, data: st.DataObject
) -> None:
    mech = MECHANISMS[fmt]
    if mech.value_model == "common":
        signal = data.draw(st.integers(tier // 2 - tier // 5, 3 * tier // 2 + tier // 5))
        blind = BlindBounds(low=tier // 2, high=3 * tier // 2, noise=tier // 5)
        exact = reference_bid(mech, tier=tier, n=n, signal=signal, blind=blind)
        approx = float_textbook(fmt, 0, n, tier, signal)
    else:
        value = data.draw(st.integers(0, tier))
        exact = reference_bid(mech, tier=tier, n=n, value=value)
        approx = float_textbook(fmt, value, n, tier)
    assert abs(float(exact) - approx) < 1e-9
    assert abs(math.floor(exact) - math.floor(approx)) <= 1  # equal unless a float straddles
