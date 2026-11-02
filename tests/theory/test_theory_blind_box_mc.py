"""The two corrections of the blind box, checked by Monte Carlo.

Every bidder sees the common value ``V`` through an appraisal ``s = V + e`` with the noise ``e``
uniform on ``-eps..eps``. Two effects follow, and the theory module states both in closed form
(:mod:`auction_night.theory.common_value`):

* **Selection.** The highest of ``n`` appraisals is the one with the luckiest noise. It
  overestimates ``V`` by ``eps (n - 1) / (n + 1)`` on average.
* **Shading.** A bidder whose appraisal ties the best rival appraisal expects the lot to be
  worth ``s - eps (n - 2) / n``. That is the equilibrium shading, the amount a bidder takes off
  its appraisal. A bidder who bids the appraisal as it is pays ``eps (n - 3) / (n + 1)`` more
  than ``V`` on average when it wins at the second price.

The checks come in three groups.

1. The closed forms against a *continuous* simulation: ``V`` uniform on a wide range, uniform
   noise, a tie taken as two appraisals within half a coin, the interior of the prior only. This
   is the setting in which the closed forms are exact.
2. The integer engine against *exact* expectations. Whole-number noise changes each closed form
   by a fraction of a coin. The exact values of the highest and second-highest noise are in the
   theory module; the exact value of the tie condition is derived here (and checked against a
   listing of every case), and the simulation must land within five standard errors of them.
3. The engine itself: lots settled by ``settle_sealed`` with the hidden value revealed in the
   hammer, bid naively and bid with the reference shading.

Every simulation has a fixed seed. A band of five standard errors lets a correct implementation
through and still shows an error of a fraction of ``eps``.
"""

import math
from fractions import Fraction
from itertools import product

import numpy as np
import pytest

from auction_night.contracts.mechanism import make_mechanism
from auction_night.engine.deal import BlindBounds
from auction_night.engine.formats import settle_sealed
from auction_night.theory import reference_bid_int
from auction_night.theory.common_value import (
    equilibrium_shading,
    expected_second_noise,
    expected_top_noise,
    naive_winner_overpay,
    top_signal_overestimate,
)

SEED = 20261101


def mean_and_error(sample: np.ndarray) -> tuple[float, float]:
    """Sample mean and its standard error."""
    values = np.asarray(sample, dtype=float)
    return float(values.mean()), float(values.std(ddof=1) / math.sqrt(len(values)))


def assert_within_five_errors(sample: np.ndarray, expected: Fraction | float, label: str) -> None:
    mean, error = mean_and_error(sample)
    assert abs(mean - float(expected)) <= 5 * error, (
        f"{label}: simulated {mean:.3f}, expected {float(expected):.3f}, standard error {error:.3f}"
    )


# --------------------------------------------------- 1. the closed forms, continuous setting

EPS_CONTINUOUS = 20


@pytest.mark.parametrize(("n", "lots"), [(4, 2_000_000), (8, 1_000_000)])
def test_the_closed_forms_hold_in_a_continuous_simulation(n: int, lots: int) -> None:
    rng = np.random.default_rng(SEED + n)
    true_value = rng.uniform(0, 1000, lots)
    signals = true_value[:, None] + rng.uniform(-EPS_CONTINUOUS, EPS_CONTINUOUS, (lots, n))
    own, rivals = signals[:, 0], signals[:, 1:]
    best_rival = rivals.max(axis=1)
    interior = (own > 200) & (own < 800)  # away from the edges of the range of V

    # selection: when the appraisal of seat 1 is the highest, it is too high by eps (n-1)/(n+1)
    selected = interior & (own > best_rival)
    assert_within_five_errors(
        (true_value - own)[selected],
        -top_signal_overestimate(EPS_CONTINUOUS, n),
        "value minus the highest appraisal",
    )
    # shading: when it ties the best rival, the lot is worth eps (n-2)/n less than the appraisal
    tied = interior & (np.abs(best_rival - own) < 0.5)
    assert_within_five_errors(
        (true_value - own)[tied],
        -equilibrium_shading(EPS_CONTINUOUS, n),
        "value minus an appraisal that ties the best rival",
    )
    # payment: the second-highest appraisal, which is what a naive winner pays
    second_highest = np.sort(signals, axis=1)[:, -2]
    assert_within_five_errors(
        second_highest - true_value,
        naive_winner_overpay(EPS_CONTINUOUS, n),
        "second-highest appraisal minus the value",
    )


# ------------------------------------------------------- 2. the integer engine, exact values


def exact_tied_noise(eps: int, n: int) -> Fraction:
    """Mean noise of an appraisal that ties the highest of the other ``n - 1`` appraisals.

    The noises are uniform on the ``2 eps + 1`` whole numbers ``-eps..eps``; write ``k`` for the
    noise plus ``eps``. The highest of ``m = n - 1`` rivals equals ``k`` with probability
    ``((k + 1)**m - k**m) / (2 eps + 1)**m``, and that weight is also the probability of the
    tie given that the seat's own noise is ``k``. The value of the lot is independent of the
    noise on a flat prior, so ``V - s = -e`` and the condition of the shading is this mean.
    """
    m = n - 1
    width = 2 * eps + 1
    weighted = sum((k - eps) * ((k + 1) ** m - k**m) for k in range(width))
    return Fraction(weighted, width**m)


@pytest.mark.parametrize(("eps", "n"), [(2, 3), (3, 3), (2, 4), (3, 4)])
def test_the_exact_tie_condition_equals_a_listing_of_every_case(eps: int, n: int) -> None:
    kept = []
    for noises in product(range(-eps, eps + 1), repeat=n):
        if max(noises[1:]) == noises[0]:
            kept.append(noises[0])
    assert exact_tied_noise(eps, n) == Fraction(sum(kept), len(kept))


@pytest.mark.parametrize("n", [2, 4, 5, 8])
def test_the_closed_forms_are_the_continuum_limit_of_the_exact_values(n: int) -> None:
    eps = 80
    assert abs(expected_top_noise(eps, n) - top_signal_overestimate(eps, n)) < 1
    assert abs(expected_second_noise(eps, n) - naive_winner_overpay(eps, n)) < 1
    assert abs(exact_tied_noise(eps, n) - equilibrium_shading(eps, n)) < 1


EPS_INTEGER = 40  # the noise of a blind box of tier 200


@pytest.mark.parametrize(("n", "lots"), [(2, 1_000_000), (4, 2_000_000), (5, 2_000_000)])
def test_the_integer_noise_matches_its_exact_expectations(n: int, lots: int) -> None:
    rng = np.random.default_rng(SEED + 100 + n)
    noises = rng.integers(-EPS_INTEGER, EPS_INTEGER + 1, size=(lots, n))
    ranked = np.sort(noises, axis=1)
    assert_within_five_errors(
        ranked[:, -1], expected_top_noise(EPS_INTEGER, n), "highest appraisal minus the value"
    )
    assert_within_five_errors(
        ranked[:, -2], expected_second_noise(EPS_INTEGER, n), "second-highest minus the value"
    )
    tied = noises[:, 1:].max(axis=1) == noises[:, 0]
    assert_within_five_errors(
        -noises[tied, 0],
        -exact_tied_noise(EPS_INTEGER, n),
        "value minus an appraisal that ties the best rival",
    )


# ------------------------------------------------------------------- 3. through the engine

MECH = make_mechanism("blind_box")
BIG = 10**6
WIDE_PRIOR = (2_000, 98_000)  # values of V; far from the clamps of a prior that runs 0..100000


def settled(n: int, lots: int, shaded: bool, seed: int):
    """Hammers of blind-box lots settled by the engine, with the true value of each lot.

    Appraisals are ``V + e`` for whole-number noise on ``-40..40``. Every seat bids its
    appraisal, or with ``shaded`` the reference bid of the theory module.
    """
    rng = np.random.default_rng(seed)
    bounds = BlindBounds(low=0, high=100_000, noise=EPS_INTEGER)
    seats = range(1, n + 1)
    budgets = dict.fromkeys(seats, BIG)
    keys = {seat: seat - 1 for seat in seats}
    values = rng.integers(WIDE_PRIOR[0], WIDE_PRIOR[1] + 1, size=lots).tolist()
    noise = rng.integers(-EPS_INTEGER, EPS_INTEGER + 1, size=(lots, n)).tolist()
    out = []
    for value, row in zip(values, noise, strict=True):
        signals = {seat: value + e for seat, e in zip(seats, row, strict=True)}
        if shaded:
            bids = {
                seat: reference_bid_int(
                    MECH, tier=200, n=n, budget=BIG, signal=signal, blind=bounds
                )
                for seat, signal in signals.items()
            }
        else:
            bids = dict(signals)
        hammer = settle_sealed(MECH, bids, budgets, keys, true_value=value).hammer
        out.append((value, signals, bids, hammer))
    return out


@pytest.mark.parametrize("n", [4, 8])
def test_a_winner_who_bids_the_appraisal_pays_the_curse_the_theory_names(n: int) -> None:
    lots = settled(n, 6_000, shaded=False, seed=SEED + 200 + n)
    for value, signals, bids, hammer in lots:
        ranked = sorted(bids.values(), reverse=True)
        assert bids == signals and hammer.bids == bids
        assert hammer.true_value == value
        assert hammer.price == ranked[1], "second price: the second-highest bid"
        assert hammer.winner is not None and bids[hammer.winner] == ranked[0]
    top = np.array([max(signals.values()) - value for value, signals, _, _ in lots])
    paid = np.array([hammer.price - value for value, _, _, hammer in lots])
    assert_within_five_errors(top, expected_top_noise(EPS_INTEGER, n), "highest bid minus value")
    assert_within_five_errors(paid, expected_second_noise(EPS_INTEGER, n), "price minus value")
    assert float(naive_winner_overpay(EPS_INTEGER, n)) > 0
    assert paid.mean() > 0, "the winner of a naive blind box pays more than the box is worth"


@pytest.mark.parametrize("n", [4, 8])
def test_the_reference_shading_turns_the_curse_into_a_profit(n: int) -> None:
    shading = equilibrium_shading(EPS_INTEGER, n)
    assert shading.denominator == 1, "a whole number of coins for these table sizes"
    lots = settled(n, 6_000, shaded=True, seed=SEED + 300 + n)
    for _value, signals, bids, hammer in lots:
        assert bids == {seat: s - int(shading) for seat, s in signals.items()}
        assert hammer.price == sorted(bids.values(), reverse=True)[1]
    paid = np.array([hammer.price - value for value, _, _, hammer in lots])
    assert_within_five_errors(
        paid,
        expected_second_noise(EPS_INTEGER, n) - shading,
        "price minus value of a shaded winner",
    )
    assert paid.mean() < 0, "the winner of a shaded blind box pays less than the box is worth"
