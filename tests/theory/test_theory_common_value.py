"""The blind box: selection and payment effects of the winner's curse."""

import math
from fractions import Fraction
from itertools import product

import pytest
from night_support import make_config

from auction_night.engine.deal import blind_bounds, deal
from auction_night.theory.common_value import (
    equilibrium_shading,
    expected_second_noise,
    expected_top_noise,
    naive_winner_overpay,
    top_signal_overestimate,
)


def test_the_closed_forms() -> None:
    assert top_signal_overestimate(40, 8) == Fraction(40 * 7, 9)
    assert equilibrium_shading(40, 8) == Fraction(40 * 6, 8) == 30
    assert naive_winner_overpay(40, 8) == Fraction(40 * 5, 9)
    # the one-line summary the show tells: with eight bidders the naive winner pays 0.111 tier
    assert float(naive_winner_overpay(40, 8) / 200) == pytest.approx(5 / 9 * 0.2)
    assert naive_winner_overpay(40, 3) == 0 and naive_winner_overpay(40, 2) < 0
    assert equilibrium_shading(40, 2) == 0


@pytest.mark.parametrize("n", [2, 3, 4, 8])
def test_a_shaded_bidder_wins_more_than_a_naive_one_on_average(n: int) -> None:
    gap = naive_winner_overpay(40, n) - equilibrium_shading(40, n)
    assert gap == Fraction(40 * (2 - 2 * n), n * (n + 1))
    assert gap < 0


@pytest.mark.parametrize("noise", [2, 3])
@pytest.mark.parametrize("n", [2, 3, 4])
def test_the_exact_order_statistics_equal_a_listing_of_every_case(noise: int, n: int) -> None:
    tops, seconds = [], []
    for draw in product(range(-noise, noise + 1), repeat=n):
        ranked = sorted(draw, reverse=True)
        tops.append(ranked[0])
        seconds.append(ranked[1])
    assert expected_top_noise(noise, n) == Fraction(sum(tops), len(tops))
    assert expected_second_noise(noise, n) == Fraction(sum(seconds), len(seconds))


@pytest.mark.parametrize("noise", [20, 40, 80])
@pytest.mark.parametrize("n", [2, 4, 8])
def test_the_closed_forms_are_the_limit_of_the_exact_values(noise: int, n: int) -> None:
    assert abs(expected_top_noise(noise, n) - top_signal_overestimate(noise, n)) < 1
    if n >= 3:
        # the second-highest noise; for n = 2 it is the lowest and the form is the same
        assert abs(expected_second_noise(noise, n) - naive_winner_overpay(noise, n)) < 1


def test_the_noise_of_the_dealt_appraisals_has_the_expected_selection_effect() -> None:
    """The deal's appraisals are the hidden value plus a whole-number noise. Over many dealt
    blind boxes, the highest of eight appraisals overshoots by the exact expected top noise and
    the second-highest by the exact expected second noise, within five standard errors."""
    config = make_config()
    noise = blind_bounds(200, config.night.blind).noise
    top, second = [], []
    for index in range(450):
        for lot in deal(config, seed=f"curse-{index}").lots:
            if lot.is_blind and lot.tier == 200:
                signals = sorted(lot.signals.values(), reverse=True)
                assert lot.blind_value is not None
                top.append(signals[0] - lot.blind_value)
                second.append(signals[1] - lot.blind_value)
    assert len(top) > 150
    for sample, exact in (
        (top, expected_top_noise(noise, 8)),
        (second, expected_second_noise(noise, 8)),
    ):
        mean = sum(sample) / len(sample)
        sd = math.sqrt(sum((x - mean) ** 2 for x in sample) / (len(sample) - 1))
        assert abs(mean - float(exact)) < 5 * sd / math.sqrt(len(sample))
