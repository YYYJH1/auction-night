"""The pacing factor: how much to shade bids so that the budget lasts the night.

Single-lot reference bids ignore the budget. Over a night of lots with one budget, a bidder
that follows them tends to spend early and sit out the end. Multiplicative pacing scales every
reference bid by one number ``alpha``, ``0 < alpha <= 1``, chosen from the money that is left
and the money the lots still to come are expected to cost:

    planned spend = sum over the lots still to play (this one included) of tier * rho / n
    alpha         = min(1, budget left / planned spend)

where ``n`` is the number of bidders, ``rho = (n - 1) / (n + 1)`` for an ordinary lot (the
expected price as a share of the tier) and ``rho = 1`` for a blind box, whose common value is
about its tier. A seat that can afford its share of the schedule is not shaded. This is the
simple form of pacing used in budget-limited ad auctions. It is a rule of thumb and not an
equilibrium.

Everything is exact (fractions), so ``alpha`` is the same on every machine.
"""

from collections.abc import Iterable
from fractions import Fraction

from auction_night.theory.bne import MIN_BIDDERS


def expected_share(n: int, *, blind: bool) -> Fraction:
    """``rho``: the expected price of a lot as a share of its tier."""
    n = max(n, MIN_BIDDERS)
    return Fraction(1) if blind else Fraction(n - 1, n + 1)


def planned_spend(upcoming: Iterable[tuple[int, bool]], n: int) -> Fraction:
    """The money the lots still to play are expected to cost one of ``n`` equal bidders.

    ``upcoming`` lists ``(tier, is_blind_box)`` for every lot still to play, the current one
    first.
    """
    n = max(n, MIN_BIDDERS)
    return sum((tier * expected_share(n, blind=blind) for tier, blind in upcoming), Fraction(0)) / n


def pacing_alpha(budget_left: int, upcoming: Iterable[tuple[int, bool]], n: int) -> Fraction:
    """``min(1, budget_left / planned spend)``; 1 when nothing is planned."""
    plan = planned_spend(upcoming, n)
    if plan <= 0:
        return Fraction(1)
    return min(Fraction(1), Fraction(budget_left) / plan)


def alpha_milli(alpha: Fraction) -> int:
    """``alpha`` in whole thousandths, rounded down (the unit the replay uses)."""
    return (alpha.numerator * 1000) // alpha.denominator
