"""What every rule bot shares: the request and answer interface of the model seats.

A bot answers the same :class:`~auction_night.engine.night.Request` that a model seat gets and
returns the same :class:`~auction_night.engine.night.Answer`, so the driver of a night does not
tell them apart. A bot reads only the facts of its own seat and the public facts of the request.
It never calls a model, never reads the clock and never draws a random number, so the same
request always gets the same answer.
"""

from dataclasses import dataclass
from typing import ClassVar

from auction_night.bots.lines import bot_line
from auction_night.contracts.config import BotPolicy
from auction_night.engine.night import Answer, Request


@dataclass(frozen=True)
class RuleBot:
    """Base of the rule bots. A subclass says how to bid; talking and keeping an English exit
    price are the same for all of them."""

    seat: int
    policy: ClassVar[BotPolicy]

    def bid(self, request: Request) -> int:
        """The number for a bid phase (exit price, grab price or bid), already within budget."""
        raise NotImplementedError

    def answer(self, request: Request) -> Answer:
        """The answer to a request that asks this bot."""
        if self.seat not in request.seats:
            raise ValueError(f"seat {self.seat} was not asked")
        phase = request.phase
        if phase == "talk":
            assert request.lot is not None
            line = bot_line(
                self.policy, request.mode, lot=request.lot, seat=self.seat, format=request.format
            )
            return Answer(seat=self.seat, say=line)
        if phase == "english_revise":
            exit_price = request.facts_of(self.seat).exit_price
            if exit_price is None:
                raise ValueError("an English revision request must carry the exit price in force")
            return Answer(seat=self.seat, value=exit_price)
        if phase == "plan":
            raise ValueError("bots hand in no plan")
        return Answer(seat=self.seat, value=self.bid(request))
