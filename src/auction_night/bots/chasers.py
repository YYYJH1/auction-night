"""Bots that chase a secret mission, for simulations and tests.

At a real table the rule bots have no mission, and these classes never sit there. They stand in
for model seats in simulations, to find out what the economy does when every mission holder goes
after its bonus. Two behaviours are modelled, one per kind of mission:

* A **collector** bids more for a lot that carries its tag. The extra is a *premium* added to the
  value of the lot. Winning the last lot it needs is worth the whole bonus. Winning an earlier one
  is worth the bonus times the chance of getting the rest: it expects to win each later lot of
  its tag with probability ``reach``, independently. The default ``reach`` of 0.15 is only a
  little above the one in eight that any seat of an eight-seat table wins a lot, so the
  collector chases with restraint.
* A **shill** bids exactly the threshold price on its target lot, so that its own bid or exit
  price decides the second-highest price, and bids as its style says everywhere else. It only
  pushes the price up and never means to buy. If the threshold is more than its budget it cannot
  do that and bids as its style says.

A bot that chases needs to know what is public after the preview, the tag of every lot, and how
many lots it has won. The tags are fixed at construction. The wins come through :meth:`observe`,
which a driver calls with the hammer of every lot: it returns the updated bot, because bots are
immutable.
"""

import math
from dataclasses import dataclass, replace
from fractions import Fraction
from typing import Final

from auction_night.bots.styles import StyleBot
from auction_night.contracts.events import CollectorMission, HammerData, Mission, ShillMission
from auction_night.engine.night import Request

REACH: Final = Fraction(3, 20)


def chance_of_at_least(needed: int, trials: int, p: Fraction) -> Fraction:
    """The probability of at least ``needed`` successes in ``trials`` independent tries of ``p``."""
    if needed <= 0:
        return Fraction(1)
    if needed > trials:
        return Fraction(0)
    return sum(
        (math.comb(trials, k) * p**k * (1 - p) ** (trials - k) for k in range(needed, trials + 1)),
        Fraction(0),
    )


def collector_premium(
    mission: CollectorMission,
    *,
    lot: int,
    tags: tuple[str, ...],
    wins: int,
    reach: Fraction = REACH,
) -> Fraction:
    """What winning ``lot`` is worth to a collector that has ``wins`` lots of its tag already.

    Nothing if the lot has another tag or the mission is done. Otherwise the bonus times the
    chance of winning the rest: certain when this lot completes the mission, and for an earlier
    lot the chance of at least the missing number of wins in the later lots of the tag.
    """
    if tags[lot - 1] != mission.tag or wins >= mission.need:
        return Fraction(0)
    missing = mission.need - wins - 1
    later = sum(1 for tag in tags[lot:] if tag == mission.tag)
    return mission.bonus * chance_of_at_least(missing, later, reach)


@dataclass(frozen=True)
class ChaserBot(StyleBot):
    """A style bot that goes after its mission.

    ``tags[k]`` is the tag of lot ``k + 1``. ``won`` lists the lots this seat has won so far.
    Without a ``mission`` the bot bids as its style says.
    """

    mission: Mission | None = None
    tags: tuple[str, ...] = ()
    won: tuple[int, ...] = ()
    reach: Fraction = REACH

    def __post_init__(self) -> None:
        super().__post_init__()
        if isinstance(self.mission, CollectorMission) and not self.tags:
            raise ValueError("a collector needs the tag of every lot")
        if not 0 <= self.reach <= 1:
            raise ValueError("reach is a probability")

    def wins(self) -> int:
        """How many lots of the collector's tag this seat has won."""
        mission = self.mission
        if not isinstance(mission, CollectorMission):
            return 0
        return sum(1 for lot in self.won if self.tags[lot - 1] == mission.tag)

    def observe(self, lot: int, hammer: HammerData) -> "ChaserBot":
        """The bot after the hammer of ``lot``: it remembers a lot it has won."""
        if hammer.winner != self.seat:
            return self
        return replace(self, won=self.won + (lot,))

    def bid(self, request: Request) -> int:
        facts = request.facts_of(self.seat)
        mission = self.mission
        premium = Fraction(0)
        if isinstance(mission, ShillMission):
            if request.lot == mission.lot and mission.threshold_price <= facts.budget_left:
                return mission.threshold_price
        elif isinstance(mission, CollectorMission):
            assert request.lot is not None
            premium = collector_premium(
                mission, lot=request.lot, tags=self.tags, wins=self.wins(), reach=self.reach
            )
        return max(0, min(math.floor(self.styled(request, premium=premium)), facts.budget_left))
