"""The truthful bot: it bids what the lot is worth to it.

In every bid phase it bids its own value, or its appraisal in a blind box, clamped to the
remaining budget. It does not shade for first-price rules or all-pay, which is exactly why it is
at the table: it shows what plain honesty earns under each rule. In an English revision round it
keeps its exit price. It ignores the channel, has no mission and talks in fixed lines.
"""

from dataclasses import dataclass
from typing import ClassVar

from auction_night.bots.base import RuleBot
from auction_night.contracts.config import BotPolicy
from auction_night.engine.night import Request


@dataclass(frozen=True)
class TruthfulBot(RuleBot):
    """Bids the value, or the appraisal."""

    policy: ClassVar[BotPolicy] = "truthful"

    def bid(self, request: Request) -> int:
        facts = request.facts_of(self.seat)
        own = facts.signal if facts.signal is not None else facts.value
        if own is None:
            raise ValueError("a bid request must carry the seat's value or appraisal")
        return max(0, min(own, facts.budget_left))
