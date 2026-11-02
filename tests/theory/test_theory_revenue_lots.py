"""Revenue equivalence at the level of single lots: eight reference bidders, many lots.

For each of the six formats, thousands of lots with the tier mix of a night are dealt, bid by
reference bidders with no budget limit and settled by the engine. The mean revenue must lie within
five standard errors of the exact expectation, computed lot by lot from the tier. The numbers of
lots are lower than in a full season check; the exact expectations are what carries the test, and
the night-level test in ``tests/night`` runs the same comparison through the whole state machine.
"""

import math
from fractions import Fraction
from functools import cache

import numpy as np
import pytest

from auction_night.contracts.config import BlindConfig
from auction_night.contracts.mechanism import make_mechanism
from auction_night.engine.deal import blind_bounds
from auction_night.engine.formats import play_lot
from auction_night.theory import reference_bid_int, revenue_moments

LOTS = 10_000
N = 8
BIG = 10**6
BLIND = BlindConfig()
SEATS = tuple(range(1, N + 1))
FORMATS = ("english", "sealed_second", "sealed_first", "dutch", "all_pay", "blind_box")


@cache
def moments(key: str, tier: int):
    mech = make_mechanism(key)  # type: ignore[arg-type]
    result = revenue_moments(mech, tier, N, BLIND)
    assert result is not None
    return result


def play_many(key: str, seed: int) -> tuple[Fraction, Fraction, int]:
    """Total revenue, expected total and variance of the total over ``LOTS`` lots."""
    rng = np.random.default_rng(seed)
    mech = make_mechanism(key)  # type: ignore[arg-type]
    tiers = rng.choice([100, 200, 400], size=LOTS, p=[5 / 12, 4 / 12, 3 / 12])
    budgets = dict.fromkeys(SEATS, BIG)
    keys = {seat: seat - 1 for seat in SEATS}
    total = mean = variance = Fraction(0)
    for tier in map(int, tiers):
        if mech.value_model == "common":
            bounds = blind_bounds(tier, BLIND)
            hidden = int(rng.integers(bounds.low, bounds.high + 1))
            signals = (hidden + rng.integers(-bounds.noise, bounds.noise + 1, size=N)).tolist()
            bids = {
                seat: reference_bid_int(
                    mech, tier=tier, n=N, budget=BIG, signal=signal, blind=bounds
                )
                for seat, signal in zip(SEATS, signals, strict=True)
            }
        else:
            hidden = None
            values = rng.integers(0, tier + 1, size=N).tolist()
            bids = {
                seat: reference_bid_int(mech, tier=tier, n=N, budget=BIG, value=value)
                for seat, value in zip(SEATS, values, strict=True)
            }
        outcome = play_lot(
            mech,
            tier=tier,
            budgets=budgets,
            tie_key=keys,
            answer=lambda step, bids=bids: bids if step.phase != "english_revise" else {},
            true_value=hidden,
        )
        total += sum(outcome.hammer.payments.values())
        expected = moments(key, tier)
        mean += expected.mean
        variance += expected.variance
    return total, mean, variance  # type: ignore[return-value]


@pytest.mark.parametrize("key", FORMATS)
def test_the_mean_revenue_of_thousands_of_lots_matches_the_exact_expectation(key: str) -> None:
    total, mean, variance = play_many(key, seed=20261101 + FORMATS.index(key))
    z = float((total - mean) / Fraction(math.sqrt(variance)))
    assert abs(z) < 5, f"{key}: {z:.2f} standard errors"
    per_lot = float((total - mean) / LOTS)
    assert abs(per_lot) < 0.03 * 250, "within 3% of a typical tier"
