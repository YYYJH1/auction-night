"""An all-economist night earns what the exact expectations say, within five standard errors.

The table is eight economists with budgets far beyond anything the lots can cost, so nobody is
ever out and nobody is paced: every lot has eight bidders who bid the reference bid. Then the
revenue of a lot is a known random variable (see :mod:`auction_night.theory.exact`) and the
revenue of many lots must lie within five standard errors of the sum of the exact means.
Conditioning on the tier of each lot, as done here, removes the noise of the schedule.
"""

import math
from collections import defaultdict
from fractions import Fraction
from functools import cache

import pytest
from night_support import all_bots_config, play_seed

from auction_night.theory import revenue_moments
from auction_night.theory.exact import Moments

CONFIG = all_bots_config("economist", pacing=True, budget_c=100_000)
NIGHTS = 400
FORMATS = ("english", "sealed_second", "sealed_first", "dutch", "all_pay", "blind_box")


@cache
def moments_of(fmt: str, tier: int) -> Moments:
    """The exact revenue moments of one lot with eight bidders (they depend on nothing else)."""
    moments = revenue_moments(CONFIG.night.mechanism_for(fmt), tier, 8, CONFIG.night.blind)  # type: ignore[arg-type]
    assert moments is not None
    return moments


def lot_revenues() -> dict[str, list[tuple[int, int]]]:
    """Per format: (tier, revenue) of every lot of the nights."""
    out: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for index in range(NIGHTS):
        transcript = play_seed(CONFIG, f"revenue-{index}")
        for record in transcript.state.records:
            hammer = record.outcome.hammer
            lot = transcript.state.deal.lot(record.lot)
            assert len(record.outcome.asked) == 8, "nobody may run out in this test"
            out[hammer.format].append((lot.tier, sum(hammer.payments.values())))
    return out


@pytest.fixture(scope="module")
def revenues() -> dict[str, list[tuple[int, int]]]:
    return lot_revenues()


def z_score(fmts: dict[str, list[tuple[int, int]]]) -> float:
    """Standardised distance of the revenue of the lots from the sum of the exact means."""
    total = Fraction(0)
    mean = Fraction(0)
    variance = Fraction(0)
    for fmt, lots in fmts.items():
        for tier, revenue in lots:
            moments = moments_of(fmt, tier)
            total += revenue
            mean += moments.mean
            variance += moments.variance
    return float((total - mean) / Fraction(math.sqrt(variance)))


@pytest.mark.parametrize("fmt", FORMATS)
def test_the_revenue_of_each_format_matches_its_exact_expectation(
    fmt: str, revenues: dict[str, list[tuple[int, int]]]
) -> None:
    lots = revenues[fmt]
    assert len(lots) == 2 * NIGHTS
    z = z_score({fmt: lots})
    assert abs(z) < 5, f"{fmt}: {z:.2f} standard errors"


def test_the_whole_night_matches_the_sum_of_its_lots(
    revenues: dict[str, list[tuple[int, int]]],
) -> None:
    assert sum(len(lots) for lots in revenues.values()) == 12 * NIGHTS
    assert abs(z_score(revenues)) < 5


def test_the_exact_means_are_close_to_the_textbook_for_the_four_private_value_rules(
    revenues: dict[str, list[tuple[int, int]]],
) -> None:
    """A sanity check on the target itself: per coin of tier, the sealed rules earn about
    7/9, and the ten-level Dutch clock about 0.73."""
    for fmt, low, high in (
        ("english", 0.76, 0.80),
        ("sealed_second", 0.76, 0.80),
        ("sealed_first", 0.76, 0.79),
        ("dutch", 0.70, 0.76),
    ):
        lots = revenues[fmt]
        share = sum(revenue for _, revenue in lots) / sum(tier for tier, _ in lots)
        assert low < share < high, (fmt, share)


def test_the_check_has_the_power_to_see_a_wrong_bidder() -> None:
    """Truthful bidders in a first-price lot earn the seller far more than economists do, and
    the same z-score notices it. Without this the test above could pass by being blind."""
    truthful = all_bots_config("truthful", budget_c=100_000)
    lots: list[tuple[int, int]] = []
    for index in range(60):
        transcript = play_seed(truthful, f"revenue-wrong-{index}")
        for record in transcript.state.records:
            hammer = record.outcome.hammer
            if hammer.format == "sealed_first":
                tier = transcript.state.deal.lot(record.lot).tier
                lots.append((tier, sum(hammer.payments.values())))
    assert len(lots) == 120
    assert z_score({"sealed_first": lots}) > 10
