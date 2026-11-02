"""Bidding styles for simulations: the textbook bid, scaled up or down.

The economy of a night (budget scale, mission bonus, the pacing switch) is tuned with rule bots
of five styles:

* ``E``: the textbook bid of the lot, which is the economist without pacing;
* ``O``: five fourths of the textbook bid, a bidder bolder than the textbook;
* ``S``: seven tenths of the textbook bid, a bidder more timid than the textbook;
* ``P``: the economist with pacing (:class:`~auction_night.bots.economist.EconomistBot`);
* ``T``: the truthful bot (:class:`~auction_night.bots.truthful.TruthfulBot`).

:class:`StyleBot` plays the first three. They exist for simulations and tests: the bots at a real
table are the economist and the truthful bot only. A style bot answers the same requests as every
other seat, reads only its own numbers and the public facts of the request, never draws a random
number, and talks with the fixed lines of the economist.

The bid is exact until the end: the reference bid of :mod:`auction_night.theory.bne` times the
factor of the style (times the pacing factor, if it paces), rounded down and clamped to the
budget. A mission may raise what a lot is worth to the bidder (``premium``, see
:mod:`auction_night.bots.chasers`). How the premium enters depends on the kind of lot: in a lot of
private value it is added to the value before the textbook formula is applied, in an all-pay lot
the shifted value is also capped at the tier, and in a blind box it is added to the bid.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from typing import ClassVar, Final, Literal

from auction_night.bots.base import RuleBot
from auction_night.contracts.config import BotPolicy
from auction_night.contracts.mechanism import MECHANISMS, MechanismDef
from auction_night.engine.night import Request
from auction_night.theory.bne import MIN_BIDDERS, reference_bid
from auction_night.theory.pacing import pacing_alpha

Style = Literal["E", "O", "S"]

# What each style multiplies the textbook bid by. Read-only by convention.
STYLE_FACTORS: Final[Mapping[Style, Fraction]] = {
    "E": Fraction(1),
    "O": Fraction(5, 4),
    "S": Fraction(7, 10),
}

_ZERO: Final = Fraction(0)


def shifted_reference(mech: MechanismDef, *, tier: int, n: int, value: Fraction) -> Fraction:
    """The textbook bid of a private-value lot for a value that need not be a whole number.

    It is :func:`~auction_night.theory.bne.reference_bid` with the value left rational. A
    mission premium moves the value of a lot to such a number.
    """
    n = max(n, MIN_BIDDERS)
    if mech.pricing == "second":
        return value
    if mech.pricing == "first":
        return Fraction(n - 1, n) * value
    return Fraction(n - 1, n) * value**n / Fraction(tier) ** (n - 1)


@dataclass(frozen=True)
class StyleBot(RuleBot):
    """The textbook bidder with a style factor, and the pacing switch (off by default)."""

    style: Style = "E"
    pacing: bool = False
    policy: ClassVar[BotPolicy] = "economist"

    def __post_init__(self) -> None:
        if self.style not in STYLE_FACTORS:
            raise ValueError(f"unknown style {self.style!r}: use one of {', '.join(STYLE_FACTORS)}")

    @property
    def factor(self) -> Fraction:
        """What the style multiplies the textbook bid by."""
        return STYLE_FACTORS[self.style]

    def styled(self, request: Request, *, premium: Fraction = _ZERO) -> Fraction:
        """The exact bid of this style for the lot in play, before rounding and the budget.

        ``premium`` is what a mission adds to the lot for this seat (zero without a mission).
        """
        assert request.format is not None and request.tier is not None
        facts = request.facts_of(self.seat)
        mech = MECHANISMS[request.format]
        n = max(request.bidders, MIN_BIDDERS)
        if mech.value_model == "common":
            reference = reference_bid(
                mech, tier=request.tier, n=n, signal=facts.signal, blind=request.blind
            )
            exact = self.factor * reference + premium
        else:
            if facts.value is None:
                raise ValueError("a bid request must carry the seat's value or appraisal")
            value = Fraction(facts.value) + premium
            if mech.pricing == "all_pay":
                value = min(value, Fraction(request.tier))
            exact = self.factor * shifted_reference(mech, tier=request.tier, n=n, value=value)
        if self.pacing:
            schedule = [(slot.tier, slot.is_blind) for slot in request.upcoming]
            exact *= pacing_alpha(facts.budget_left, schedule, n)
        return exact

    def bid(self, request: Request) -> int:
        facts = request.facts_of(self.seat)
        return max(0, min(math.floor(self.styled(request)), facts.budget_left))
