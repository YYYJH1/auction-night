"""The economist bids the textbook bid of the lot, scaled by the pacing factor, and nothing else.

Every expectation below is written out with fractions from the formulas of the textbook, not
with the functions under test.
"""

import math
from fractions import Fraction

import pytest
from bots_support import bid_request, schedule

from auction_night.bots import EconomistBot
from auction_night.engine.deal import BlindBounds
from auction_night.engine.night import Answer


def floor(x: Fraction) -> int:
    return math.floor(x)


def bid_of(bot: EconomistBot, request) -> int:
    answer = bot.answer(request)
    assert isinstance(answer, Answer) and answer.abstain is None
    assert answer.seat == bot.seat and answer.value is not None
    return answer.value


NO_PACING = EconomistBot(1, pacing=False)


@pytest.mark.parametrize("value", [0, 1, 37, 100, 199, 200])
@pytest.mark.parametrize("fmt", ["english", "sealed_second"])
def test_in_second_price_rules_the_economist_bids_its_value(fmt: str, value: int) -> None:
    request = bid_request(fmt, value=value, tier=200)
    assert bid_of(NO_PACING, request) == value
    assert bid_of(EconomistBot(1), request) == value  # no pacing pressure with one cheap lot


@pytest.mark.parametrize("n", [2, 3, 5, 8])
@pytest.mark.parametrize("value", [0, 1, 37, 100, 199, 200])
@pytest.mark.parametrize("fmt", ["sealed_first", "dutch"])
def test_in_first_price_rules_it_bids_n_minus_one_over_n_of_its_value(
    fmt: str, value: int, n: int
) -> None:
    request = bid_request(fmt, value=value, bidders=n)
    assert bid_of(NO_PACING, request) == floor(Fraction((n - 1) * value, n))


@pytest.mark.parametrize("n", [2, 4, 8])
@pytest.mark.parametrize("value", [0, 20, 100, 199, 200])
def test_in_all_pay_it_bids_the_symmetric_equilibrium(value: int, n: int) -> None:
    tier = 200
    request = bid_request("all_pay", value=value, tier=tier, bidders=n)
    exact = Fraction(n - 1, n) * Fraction(value) ** n / Fraction(tier) ** (n - 1)
    assert bid_of(NO_PACING, request) == floor(exact)


@pytest.mark.parametrize("n", [2, 3, 8])
@pytest.mark.parametrize("signal", [100, 120, 190, 200, 250, 290, 300])
def test_in_a_blind_box_it_shades_the_appraisal_and_stays_inside_the_prior(
    signal: int, n: int
) -> None:
    tier, noise = 200, 40
    request = bid_request("blind_box", signal=signal, tier=tier, bidders=n)
    shaded = Fraction(signal) - Fraction(noise * (n - 2), n)
    expected = floor(min(max(shaded, Fraction(100)), Fraction(300)))
    assert bid_of(NO_PACING, request) == expected
    assert 100 <= bid_of(NO_PACING, request) <= 300


def test_with_two_bidders_a_blind_box_appraisal_is_bid_as_it_is() -> None:
    assert bid_of(NO_PACING, bid_request("blind_box", signal=222, bidders=2)) == 222


def test_the_economist_never_bids_more_than_it_has() -> None:
    for fmt, value in (("english", 150), ("sealed_first", 150), ("all_pay", 150), ("dutch", 150)):
        request = bid_request(fmt, value=value, budget=30)
        assert bid_of(NO_PACING, request) <= 30
    assert bid_of(NO_PACING, bid_request("sealed_second", value=150, budget=30)) == 30
    assert bid_of(NO_PACING, bid_request("blind_box", signal=200, budget=45)) == 45


def test_fewer_than_two_bidders_are_treated_as_two() -> None:
    lone = bid_request("sealed_first", value=100, bidders=1)
    two = bid_request("sealed_first", value=100, bidders=2)
    assert bid_of(NO_PACING, lone) == bid_of(NO_PACING, two) == 50


# ------------------------------------------------------------------------------ pacing


def expected_alpha(left: int, lots: list[tuple[bool, int]], n: int) -> Fraction:
    """min(1, left / sum(tier * rho / n)) with rho = (n-1)/(n+1), or 1 for a blind box."""
    plan = (
        sum(
            (Fraction(tier) * (1 if blind else Fraction(n - 1, n + 1)) for blind, tier in lots),
            Fraction(0),
        )
        / n
    )
    return min(Fraction(1), Fraction(left) / plan)


def test_pacing_scales_the_bid_by_the_share_of_the_plan_the_budget_covers() -> None:
    lots = [("sealed_second", 100)] * 6 + [("sealed_second", 400)] * 6
    upcoming = schedule(*lots)
    plan = [(False, tier) for _, tier in lots]
    for budget in (60, 120, 180, 240, 400):
        request = bid_request("sealed_second", value=87, tier=100, budget=budget, upcoming=upcoming)
        alpha = expected_alpha(budget, plan, 8)
        assert alpha <= 1
        assert bid_of(EconomistBot(1), request) == min(floor(alpha * 87), budget)
        assert bid_of(NO_PACING, request) == min(87, budget)


def test_pacing_never_raises_a_bid_and_is_off_when_the_budget_covers_the_plan() -> None:
    upcoming = schedule(*[("sealed_first", 400)] * 12)  # a plan of about 466 coins for 8 bidders
    rich = bid_request("sealed_first", value=90, tier=100, budget=500, upcoming=upcoming)
    assert bid_of(EconomistBot(1), rich) == bid_of(NO_PACING, rich) == floor(Fraction(7 * 90, 8))
    poor = bid_request("sealed_first", value=90, tier=100, budget=200, upcoming=upcoming)
    assert bid_of(EconomistBot(1), poor) < bid_of(NO_PACING, poor) <= 200
    for budget in range(1, 600, 37):
        request = bid_request("sealed_first", value=90, tier=100, budget=budget, upcoming=upcoming)
        assert bid_of(EconomistBot(1), request) <= bid_of(NO_PACING, request)


def test_the_plan_counts_a_blind_box_at_its_full_tier() -> None:
    plain = schedule(*[("english", 400)] * 6)
    blind = schedule(*[("english", 400)] * 5, ("blind_box", 400))
    budget = 150
    a = bid_of(EconomistBot(1), bid_request("english", value=100, upcoming=plain, budget=budget))
    b = bid_of(EconomistBot(1), bid_request("english", value=100, upcoming=blind, budget=budget))
    assert a == floor(expected_alpha(budget, [(False, 400)] * 6, 8) * 100)
    assert b == floor(expected_alpha(budget, [(False, 400)] * 5 + [(True, 400)], 8) * 100)
    assert b < a < 100  # a blind box costs more than an ordinary lot of the same tier


def test_pacing_applies_to_every_bid_phase() -> None:
    upcoming = schedule(("dutch", 400), ("all_pay", 400), ("blind_box", 400))
    budget = 100
    alpha = expected_alpha(budget, [(False, 400), (False, 400), (True, 400)], 8)
    assert Fraction(1, 2) < alpha < 1
    for fmt, unpaced in (
        ("dutch", Fraction(7 * 60, 8)),
        ("sealed_first", Fraction(7 * 60, 8)),
        ("english", Fraction(60)),
        ("sealed_second", Fraction(60)),
    ):
        request = bid_request(fmt, value=60, tier=400, budget=budget, upcoming=upcoming)
        assert bid_of(EconomistBot(1), request) == floor(alpha * unpaced) < unpaced
    request = bid_request("all_pay", value=60, tier=400, budget=budget, upcoming=upcoming)
    exact = Fraction(7, 8) * Fraction(60) ** 8 / Fraction(400) ** 7
    assert bid_of(EconomistBot(1), request) == floor(alpha * exact)
    # a blind box of tier 100 in front of eleven lots of tier 400
    upcoming = schedule(("blind_box", 100), *[("sealed_second", 400)] * 11)
    alpha = expected_alpha(300, [(True, 100)] + [(False, 400)] * 11, 8)
    request = bid_request("blind_box", signal=110, tier=100, budget=300, upcoming=upcoming)
    assert alpha < 1
    assert bid_of(EconomistBot(1), request) == floor(alpha * (110 - Fraction(20 * 6, 8)))


def test_the_pacing_switch_is_on_by_default_and_the_bid_follows_the_switch() -> None:
    assert EconomistBot(3).pacing is True and EconomistBot(3, pacing=False).pacing is False
    upcoming = schedule(*[("sealed_second", 400)] * 12)
    request = bid_request("sealed_second", value=200, tier=400, budget=300, upcoming=upcoming)
    assert bid_of(EconomistBot(1), request) < 200
    assert bid_of(EconomistBot(1, pacing=False), request) == 200


# ------------------------------------------------------------------------- other phases


def test_the_economist_keeps_its_exit_price_in_a_revision_round() -> None:
    request = bid_request("english", value=90, phase="english_revise", exit_price=77, price=40)
    assert bid_of(EconomistBot(1), request) == 77
    broken = bid_request("english", value=90, phase="english_revise", price=40)
    with pytest.raises(ValueError, match="exit price"):
        EconomistBot(1).answer(broken)


def test_the_economist_makes_no_plan_and_is_only_asked_for_itself() -> None:
    plan = bid_request("english", value=1, phase="plan")
    with pytest.raises(ValueError, match="no plan"):
        EconomistBot(1).answer(plan)
    request = bid_request("english", seat=2, value=1)
    with pytest.raises(ValueError, match="was not asked"):
        EconomistBot(9).answer(request)


def test_blind_bounds_come_from_the_request() -> None:
    request = bid_request(
        "blind_box", signal=130, bidders=4, blind=BlindBounds(low=120, high=140, noise=10)
    )
    assert bid_of(NO_PACING, request) == max(120, 130 - 5)
