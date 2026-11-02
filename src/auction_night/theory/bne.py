"""Single-lot reference bids: what a risk-neutral bidder bids in the textbook model.

The model: ``n`` bidders with independent private values drawn uniformly from ``0..tier``, no
budget limit, and (for the blind box) a common value that every bidder only sees through a noisy
appraisal. The reference bid depends on the *pricing rule* of the mechanism, so it is read off
the mechanism definition and never off the name of a format:

======================  =============================================================
second price (English)  the bidder's value (a dominant strategy)
first price (Dutch)     ``(n - 1) / n * v``; in a Dutch lot this is the grab price, and the
                        clock rounds it down to a level
all-pay                 ``(n - 1) / n * v**n / tier**(n - 1)``
blind box (common)      the appraisal ``s`` minus ``noise * (n - 2) / n``, kept inside the
                        prior bounds of the true value
======================  =============================================================

The formulas hold for the interior of the value range. Reference bids ignore the reserve price
(a bid below the reserve simply does not win), the remaining budget (except for the final
clamp of :func:`reference_bid_int`), the pacing factor and any mission. They are the yardstick
for "how far from the textbook did this seat bid".

All arithmetic is exact: bids are fractions until the caller floors them.
"""

import math
from fractions import Fraction

from auction_night.contracts.mechanism import MechanismDef
from auction_night.engine.deal import BlindBounds

MIN_BIDDERS = 2  # with fewer rivals the formulas degenerate; the reference never goes below two


def reference_bid(
    mech: MechanismDef,
    *,
    tier: int,
    n: int,
    value: int | None = None,
    signal: int | None = None,
    blind: BlindBounds | None = None,
) -> Fraction:
    """The exact textbook bid of a seat with ``value`` (or ``signal`` in a blind box).

    ``n`` is the number of bidders in the lot; it is raised to two if smaller. A blind box needs
    the ``signal`` and the public prior ``blind``; every other format needs the ``value``.
    """
    n = max(n, MIN_BIDDERS)
    if mech.value_model == "common":
        if signal is None or blind is None:
            raise ValueError("a blind box bid needs the appraisal and the prior bounds")
        shaded = Fraction(signal) - Fraction(blind.noise * (n - 2), n)
        return min(max(shaded, Fraction(blind.low)), Fraction(blind.high))
    if value is None:
        raise ValueError("a bid in a private-value lot needs the value")
    if mech.pricing == "second":
        return Fraction(value)
    if mech.pricing == "first":
        return Fraction((n - 1) * value, n)
    return Fraction((n - 1) * value**n, n * tier ** (n - 1))


def reference_bid_int(
    mech: MechanismDef,
    *,
    tier: int,
    n: int,
    budget: int,
    value: int | None = None,
    signal: int | None = None,
    blind: BlindBounds | None = None,
) -> int:
    """The reference bid as the engine takes it: rounded down and clamped to ``0..budget``."""
    exact = reference_bid(mech, tier=tier, n=n, value=value, signal=signal, blind=blind)
    return max(0, min(math.floor(exact), budget))
