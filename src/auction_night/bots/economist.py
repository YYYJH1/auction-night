"""The economist: the textbook bid of the lot, scaled so that the budget lasts.

In every bid phase the economist bids the single-lot reference bid for its value (or its
appraisal in a blind box) and the number of bidders in the lot, see
:func:`auction_night.theory.bne.reference_bid`. With pacing on, which is the default of this
class, the bid is multiplied by the pacing factor ``alpha`` (see
:mod:`auction_night.theory.pacing`) of the money it has left. The result is rounded down and
clamped to the remaining budget. In an English revision round it keeps its exit price. It
ignores the channel, has no mission and talks in fixed lines.
"""

import math
from dataclasses import dataclass
from typing import ClassVar

from auction_night.bots.base import RuleBot
from auction_night.contracts.config import BotPolicy
from auction_night.contracts.mechanism import MECHANISMS
from auction_night.engine.night import Request
from auction_night.theory.bne import MIN_BIDDERS, reference_bid
from auction_night.theory.pacing import pacing_alpha


@dataclass(frozen=True)
class EconomistBot(RuleBot):
    """The textbook bidder, with the pacing switch (on by default)."""

    pacing: bool = True
    policy: ClassVar[BotPolicy] = "economist"

    def bid(self, request: Request) -> int:
        assert request.format is not None and request.tier is not None
        facts = request.facts_of(self.seat)
        n = max(request.bidders, MIN_BIDDERS)
        exact = reference_bid(
            MECHANISMS[request.format],
            tier=request.tier,
            n=n,
            value=facts.value,
            signal=facts.signal,
            blind=request.blind,
        )
        if self.pacing:
            schedule = [(slot.tier, slot.is_blind) for slot in request.upcoming]
            exact *= pacing_alpha(facts.budget_left, schedule, n)
        return max(0, min(math.floor(exact), facts.budget_left))
