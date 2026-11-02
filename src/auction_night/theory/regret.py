"""Regret: what the best single-lot deviation would have earned, other seats held fixed.

For one seat and one lot, the regret is the largest gain over its real payoff that any other
number would have brought, when everybody else keeps theirs (see
:mod:`auction_night.theory.counterfactual` for what the others' numbers are, and for the
English approximation). It is never negative, because the seat's own number is among the
candidates. It is the same bookkeeping that a poker broadcast does when it shows what a folded
hand would have made.

The best deviation is found exactly, without trying every number. The payoff of a seat is a
step function of its own number: it changes only where the number crosses another seat's
number, or the reserve price. So it is enough to try 0 (stay out), the largest affordable
number, the lowest number that can win (the reserve or 1), and every other seat's number and
that number plus one. In a Dutch lot a grab price matters only through the level it falls on,
so the candidates are the level prices the seat can afford, and 0.

A seat that was not asked has no regret (it could not act) and is left out of the result.
"""

from auction_night.engine.formats import dutch_ladder
from auction_night.theory.counterfactual import LotSetup


def candidates(setup: LotSetup, seat: int) -> list[int]:
    """The numbers worth trying for ``seat``: a superset of every breakpoint of its payoff."""
    budget = setup.budgets[seat]
    if setup.mech.clock == "descending_levels":
        assert setup.mech.levels is not None
        ladder = dutch_ladder(setup.tier, setup.mech.levels, setup.mech.reserve)
        return sorted({0, *(price for price in ladder if price <= budget)})
    others = [bid for other, bid in setup.bids.items() if other != seat]
    points = {0, budget, max(setup.mech.reserve, 1), *others, *(bid + 1 for bid in others)}
    return sorted(x for x in points if 0 <= x <= budget)


def best_deviation(setup: LotSetup, seat: int) -> tuple[int, int]:
    """The best payoff ``seat`` could have reached and the smallest number that reaches it."""
    best_payoff, best_bid = None, 0
    for own in candidates(setup, seat):
        payoff = setup.payoff(seat, own)
        if best_payoff is None or payoff > best_payoff:
            best_payoff, best_bid = payoff, own
    assert best_payoff is not None
    return best_payoff, best_bid


def regret(setup: LotSetup, seat: int) -> int:
    """The regret of one asked seat in one lot."""
    return max(0, best_deviation(setup, seat)[0] - setup.actual[seat])


def lot_regrets(setup: LotSetup) -> dict[int, int]:
    """The regret of every asked seat."""
    return {seat: regret(setup, seat) for seat in sorted(setup.bids)}
