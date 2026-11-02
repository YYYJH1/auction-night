"""Exact expectations of the seller's revenue in the integer engine.

The textbook says that all four private-value formats earn the same on average, ``(n - 1) /
(n + 1)`` of the tier for ``n`` bidders with values uniform on ``[0, tier]``. The engine is
discrete: values are whole numbers, bids are rounded down and the Dutch clock has ten levels.
Its expectations are a little different (the first-price rounding costs a hair, the ten Dutch
levels cost a lot), and they are the right targets for a test of the engine, because they are
exact and not a limit.

Everything here is a finite sum over whole numbers, done with integers and fractions, so there
is no sampling error and no floating point. For each format the revenue of one lot is a
function of the bids, and the bids are the reference bids of :mod:`auction_night.theory.bne`:

* second price (and English): the second-highest value;
* first price: the highest bid ``floor((n - 1) v / n)``;
* all-pay: the *sum* of the bids ``floor((n - 1) v**n / (n tier**(n - 1)))``;
* Dutch: the highest level price at or below a bid ``floor((n - 1) v / n)`` (no sale if none);
* blind box: the second-highest bid, where a bid is the appraisal ``V + e`` shaded by
  ``ceil(noise (n - 2) / n)`` and clamped to the prior, ``V`` is uniform on the prior and ``e``
  uniform on ``-noise..noise``.

Revenue is in coins, not in shares of the tier. The first two moments of one lot are enough to
say how many lots a test needs: the variance of a mean of ``N`` independent lots is
``variance / N``.
"""

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction

from auction_night.contracts.config import BlindConfig
from auction_night.contracts.mechanism import MechanismDef
from auction_night.engine.deal import blind_bounds
from auction_night.engine.formats import dutch_ladder
from auction_night.theory.bne import MIN_BIDDERS


@dataclass(frozen=True, slots=True)
class Moments:
    """Mean and variance of the revenue of one lot, in coins, as exact fractions."""

    mean: Fraction
    variance: Fraction

    @classmethod
    def of(cls, first: Fraction, second: Fraction) -> "Moments":
        """From the first and second raw moments."""
        return cls(first, second - first * first)


def _order_statistic(
    pmf: Mapping[int, int], total: int, n: int, rank: int
) -> tuple[Fraction, Fraction]:
    """First and second raw moments of the ``rank``-th highest of ``n`` independent draws.

    ``pmf`` maps a value to its weight and ``total`` is the sum of the weights. Only the highest
    (rank 1) and the second-highest (rank 2) are needed. The distribution function of the
    highest is ``F**n``, that of the second-highest ``F**n + n F**(n-1) (1 - F)``; with
    ``F = c / total`` the numerators below are integers over ``total**n``.
    """
    if rank not in (1, 2) or n < rank:
        raise ValueError("rank must be 1 or 2 and at most n")
    first = second = previous = cumulative = 0
    for value in sorted(pmf):
        cumulative += pmf[value]
        reach = cumulative**n
        if rank == 2:
            reach += n * cumulative ** (n - 1) * (total - cumulative)
        mass = reach - previous
        first += value * mass
        second += value * value * mass
        previous = reach
    return Fraction(first, total**n), Fraction(second, total**n)


def _per_draw_moments(pmf: Mapping[int, int], total: int) -> tuple[Fraction, Fraction]:
    first = sum(Fraction(value * weight, total) for value, weight in pmf.items())
    second = sum(Fraction(value * value * weight, total) for value, weight in pmf.items())
    return first, second


def second_price(tier: int, n: int) -> Moments:
    """English and second-price lots: the revenue is the second-highest value."""
    pmf = dict.fromkeys(range(tier + 1), 1)
    return Moments.of(*_order_statistic(pmf, tier + 1, n, 2))


def _first_price_bids(tier: int, n: int) -> Counter[int]:
    return Counter(((n - 1) * v) // n for v in range(tier + 1))


def first_price(tier: int, n: int) -> Moments:
    """First-price lots: the revenue is the highest bid."""
    return Moments.of(*_order_statistic(_first_price_bids(tier, n), tier + 1, n, 1))


def all_pay(tier: int, n: int) -> Moments:
    """All-pay lots: the revenue is the sum of everybody's bid."""
    pmf = Counter(((n - 1) * v**n) // (n * tier ** (n - 1)) for v in range(tier + 1))
    first, second = _per_draw_moments(pmf, tier + 1)
    return Moments(n * first, n * (second - first * first))


def dutch(tier: int, n: int, levels: int, reserve: int = 0) -> Moments:
    """Dutch lots: the highest level price that some bidder's grab price reaches."""
    ladder = dutch_ladder(tier, levels, reserve)
    pmf: Counter[int] = Counter()
    for bid, weight in _first_price_bids(tier, n).items():
        pmf[max((price for price in ladder if price <= bid), default=0)] += weight
    return Moments.of(*_order_statistic(pmf, tier + 1, n, 1))


def blind_box(tier: int, n: int, blind: BlindConfig) -> Moments:
    """Blind-box lots: the second-highest bid of bidders who shade their appraisal."""
    bounds = blind_bounds(tier, blind)
    shade = -(-bounds.noise * (n - 2) // n)
    width = 2 * bounds.noise + 1
    first = second = Fraction(0)
    truths = range(bounds.low, bounds.high + 1)
    for truth in truths:
        pmf: Counter[int] = Counter()
        for noise in range(-bounds.noise, bounds.noise + 1):
            pmf[min(max(truth + noise - shade, bounds.low), bounds.high)] += 1
        a, b = _order_statistic(pmf, width, n, 2)
        first += a
        second += b
    count = len(truths)
    return Moments.of(first / count, second / count)


def revenue_moments(
    mech: MechanismDef, tier: int, n: int, blind: BlindConfig | None = None
) -> Moments | None:
    """Exact mean and variance of the revenue of one lot of ``mech`` with ``n`` bidders.

    Only lots without a reserve price are covered; with a reserve, or with fewer than two
    bidders, the answer is None.
    """
    if mech.reserve > 0 or n < MIN_BIDDERS:
        return None
    if mech.value_model == "common":
        if blind is None:
            raise ValueError("a blind box needs its prior")
        return blind_box(tier, n, blind)
    if mech.clock == "descending_levels":
        assert mech.levels is not None
        return dutch(tier, n, mech.levels)
    if mech.pricing == "second":
        return second_price(tier, n)
    if mech.pricing == "first":
        return first_price(tier, n)
    return all_pay(tier, n)


def expected_revenue(
    mech: MechanismDef, tier: int, n: int, blind: BlindConfig | None = None
) -> Fraction | None:
    """The exact expected revenue of one lot, or None where :func:`revenue_moments` is."""
    moments = revenue_moments(mech, tier, n, blind)
    return None if moments is None else moments.mean


def textbook_revenue(tier: int, n: int) -> Fraction:
    """The textbook limit, ``(n - 1) / (n + 1)`` of the tier, for values uniform on a continuum.

    It is what the glossary tells the audience. The engine's exact value differs a little
    (see the module text), by less than 1% of the tier for the sealed formats at eight bidders.
    """
    return Fraction((n - 1) * tier, n + 1)
