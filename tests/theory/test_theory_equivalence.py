"""Revenue equivalence of the private-value rules: twenty thousand lots per rule.

Eight bidders sit at every lot, the tier of a lot is drawn from the mix of a night (five lots
of tier 100, four of 200 and three of 400 in every twelve), values are uniform on ``0..tier``,
and every bidder bids the textbook bid of its rule (:func:`reference_bid_int`, no budget
limit). The seller's revenue per lot, as a share of the tier, then has an *exact* expectation
in the integer engine (:mod:`auction_night.theory.exact`), and the mean over twenty thousand
lots must lie within five standard errors of it.

The five rows below are the fixed targets of the equivalence test. Each is checked twice:

* the exact expectation, computed here from the closed forms of the theory module, rounds to
  the quoted number and the quoted tolerance is five standard errors of twenty thousand lots;
* the engine, driven through the settlement functions with a fixed seed, lands inside it.

A check that cannot fail proves nothing, so the last test points the same machinery at
bidders who do not follow the textbook and expects it to notice.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction
from functools import cache

import numpy as np
import pytest

from auction_night.contracts.config import BlindConfig
from auction_night.contracts.mechanism import FormatKey, MechanismDef, make_mechanism
from auction_night.engine.formats import play_lot, settle_dutch, settle_sealed
from auction_night.theory import reference_bid_int, revenue_moments

LOTS = 20_000
N = 8
SEATS = tuple(range(1, N + 1))
BIG = 10**6  # budgets that never bind
BUDGETS = dict.fromkeys(SEATS, BIG)
TIE_KEYS = {seat: seat - 1 for seat in SEATS}
MIX = ((100, 5), (200, 4), (400, 3))  # lots of each tier in a night of twelve
TIER_VALUES = [tier for tier, _ in MIX]
TIER_WEIGHTS = [count / 12 for _, count in MIX]
SEED = 20261101


@dataclass(frozen=True)
class Row:
    """One line of the table of targets."""

    name: str
    key: FormatKey
    target: float  # exact expectation of revenue / tier, to four places
    tolerance: float  # five standard errors of the mean of twenty thousand lots
    seed_offset: int = 0


ROWS = (
    Row("second price", "sealed_second", 0.7796, 0.0047),
    Row("English button", "english", 0.7796, 0.0047),
    Row("first price, bids rounded down", "sealed_first", 0.7771, 0.0031, 2),
    Row("all-pay, bids rounded down", "all_pay", 0.7805, 0.0193, 3),
    Row("Dutch, ten levels, one grab price", "dutch", 0.7303, 0.0032, 4),
)


# ------------------------------------------------------------------------ the exact side


def exact_share(mech: MechanismDef) -> tuple[Fraction, Fraction]:
    """Mean and variance of revenue / tier for a lot drawn from the tier mix."""
    mean = second = Fraction(0)
    for tier, count in MIX:
        moments = revenue_moments(mech, tier, N, BlindConfig())
        assert moments is not None
        weight = Fraction(count, 12)
        mean += weight * moments.mean / tier
        second += weight * (moments.variance / tier**2 + (moments.mean / tier) ** 2)
    return mean, second - mean * mean


@pytest.mark.parametrize("row", ROWS, ids=lambda row: row.name)
def test_the_exact_expectation_rounds_to_the_target_and_fixes_the_tolerance(row: Row) -> None:
    mean, variance = exact_share(make_mechanism(row.key))
    assert round(float(mean), 4) == row.target
    standard_error = math.sqrt(float(variance)) / math.sqrt(LOTS)
    assert round(5 * standard_error, 4) == row.tolerance


def test_the_english_button_and_the_second_price_share_one_exact_expectation() -> None:
    assert exact_share(make_mechanism("english")) == exact_share(make_mechanism("sealed_second"))


# ------------------------------------------------------------------------ the engine side


def draw_lots(seed: int, lots: int = LOTS) -> tuple[list[int], list[list[int]]]:
    """Tiers and the eight values of each lot, from a fixed seed."""
    rng = np.random.default_rng(seed)
    tiers = rng.choice(TIER_VALUES, size=lots, p=TIER_WEIGHTS)
    values = rng.integers(0, tiers[:, None] + 1, size=(lots, N))
    return tiers.tolist(), values.tolist()


def revenue(mech: MechanismDef, tier: int, bids: dict[int, int]) -> int:
    """What the seller takes in at one lot: the engine's own settlement of the bids."""
    if mech.clock == "descending_levels":
        outcome = settle_dutch(mech, tier, bids, BUDGETS, TIE_KEYS)
    elif mech.clock == "none":
        outcome = settle_sealed(mech, bids, BUDGETS, TIE_KEYS)
    else:  # the button auction: everybody keeps the exit price named at the start
        outcome = play_lot(
            mech,
            tier=tier,
            budgets=BUDGETS,
            tie_key=TIE_KEYS,
            answer=lambda step: bids if step.phase == "english_init" else {},
        )
    return sum(outcome.hammer.payments.values())


def textbook_bids(mech: MechanismDef, tier: int, values: list[int]) -> dict[int, int]:
    return {
        seat: reference_bid_int(mech, tier=tier, n=N, budget=BIG, value=value)
        for seat, value in zip(SEATS, values, strict=True)
    }


def played(
    mech: MechanismDef,
    seed: int,
    lots: int = LOTS,
    bidder: Callable[[MechanismDef, int, list[int]], dict[int, int]] = textbook_bids,
) -> tuple[list[int], list[int]]:
    """Tier and revenue of every lot."""
    tiers, values = draw_lots(seed, lots)
    revenues = [
        revenue(mech, tier, bidder(mech, tier, row))
        for tier, row in zip(tiers, values, strict=True)
    ]
    return tiers, revenues


def mean_share(tiers: list[int], revenues: list[int]) -> float:
    return sum(r / t for t, r in zip(tiers, revenues, strict=True)) / len(tiers)


@cache
def lots_of(row: Row) -> tuple[tuple[int, ...], tuple[int, ...]]:
    seed = SEED + (0 if row.key == "english" else row.seed_offset)  # English reuses the draws
    tiers, revenues = played(make_mechanism(row.key), seed)
    return tuple(tiers), tuple(revenues)


@pytest.mark.parametrize("row", ROWS, ids=lambda row: row.name)
def test_twenty_thousand_lots_land_within_five_standard_errors_of_the_target(row: Row) -> None:
    tiers, revenues = lots_of(row)
    assert len(tiers) == LOTS
    share = mean_share(list(tiers), list(revenues))
    assert abs(share - row.target) <= row.tolerance, (
        f"{row.name}: mean revenue / tier {share:.4f}, target {row.target} +- {row.tolerance}"
    )


def test_the_button_auction_earns_what_the_sealed_second_price_earns_lot_by_lot() -> None:
    """Same values, same textbook bids, nobody revises: the two rules are the same sale."""
    sealed = lots_of(ROWS[0])
    button = lots_of(ROWS[1])
    assert sealed == button


def test_the_tier_mix_is_the_mix_of_a_night() -> None:
    tiers, _ = lots_of(ROWS[0])
    for tier, count in MIX:
        assert abs(tiers.count(tier) / LOTS - count / 12) < 0.01


# --------------------------------------------------------------------- power of the check


def truthful_bids(mech: MechanismDef, tier: int, values: list[int]) -> dict[int, int]:
    return dict(zip(SEATS, values, strict=True))


@pytest.mark.parametrize(
    ("row", "mech", "bidder"),
    [
        (ROWS[2], make_mechanism("sealed_first"), truthful_bids),
        (ROWS[3], make_mechanism("all_pay"), truthful_bids),
        (ROWS[4], make_mechanism("dutch", levels=5), textbook_bids),
    ],
    ids=["first price without shading", "all-pay without shading", "Dutch with five levels"],
)
def test_the_check_has_the_power_to_see_a_different_game(
    row: Row, mech: MechanismDef, bidder: Callable[[MechanismDef, int, list[int]], dict[int, int]]
) -> None:
    tiers, revenues = played(mech, SEED + 9, lots=2_000, bidder=bidder)
    share = mean_share(tiers, revenues)
    assert abs(share - row.target) > row.tolerance, (
        f"a wrong game must fall outside the band, got {share:.4f}"
    )
