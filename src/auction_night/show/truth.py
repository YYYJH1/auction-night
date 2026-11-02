"""The hole cards of a finished night, read from its log, for the checks that need the truth.

The judges never see these numbers. The program does, after the night: a claim about one's own
budget, valuation or mission is held against what the deal gave the seat, and a judge's guess of a
mission is held against the mission itself. Everything here is read from the ``seat:N`` events
that the log of the night holds (``deal_private``) and from the public hammers, so the same checks
run on a log loaded from a file as on a night that has just been played.

A budget is what the seat had *at that moment*: the opening budget minus what it paid in the lots
before. :meth:`Truth.remaining` works that out from the hammers.
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from auction_night.contracts.events import (
    AnyEvent,
    CollectorMission,
    DealPrivateEvent,
    HammerEvent,
    Mission,
    NightStartEvent,
    ShillMission,
)

__all__ = ["Truth", "mission_kind_of"]


def mission_kind_of(mission: Mission | None) -> str:
    """``collector``, ``shill`` or ``none``: the words a claim about one's mission is checked in."""
    return "none" if mission is None else mission.kind


@dataclass(frozen=True, slots=True)
class Truth:
    """What the deal gave each seat, and what the lots looked like.

    ``values`` holds a seat's value for every ordinary lot and ``signals`` its appraisal of every
    blind box (a seat's own number is never the hidden value of the box). ``paid`` maps a lot to
    what each seat paid in it, for the budget at a later moment.
    """

    players: tuple[int, ...]
    budgets: dict[int, int]
    values: dict[int, dict[int, int]]
    signals: dict[int, dict[int, int]]
    missions: dict[int, Mission]
    tiers: dict[int, int]
    tags: dict[int, str]
    formats: dict[int, str]
    paid: dict[int, dict[int, int]]

    @classmethod
    def of_events(cls, events: Iterable[AnyEvent]) -> "Truth":
        """Read the truth from a whole log. It needs ``night_start`` and the private deals."""
        players: tuple[int, ...] = ()
        tiers: dict[int, int] = {}
        tags: dict[int, str] = {}
        formats: dict[int, str] = {}
        budgets: dict[int, int] = {}
        values: dict[int, dict[int, int]] = {}
        signals: dict[int, dict[int, int]] = {}
        missions: dict[int, Mission] = {}
        paid: dict[int, dict[int, int]] = defaultdict(dict)
        seen_start = False
        for event in events:
            if isinstance(event, NightStartEvent):
                seen_start = True
                players = tuple(s.seat for s in event.data.seats if s.role == "player")
                for plan in event.data.lots:
                    tiers[plan.lot] = plan.tier
                    tags[plan.lot] = plan.tag
                    formats[plan.lot] = plan.format
            elif isinstance(event, DealPrivateEvent):
                card = event.data
                budgets[card.seat] = card.budget
                values[card.seat] = dict(card.values)
                signals[card.seat] = dict(card.signals)
                if card.mission is not None:
                    missions[card.seat] = card.mission
            elif isinstance(event, HammerEvent) and event.lot is not None:
                paid[event.lot] = dict(event.data.payments)
        if not seen_start:
            raise ValueError("the log has no night_start event")
        if not budgets:
            raise ValueError("the log has no private deals: it is not the whole log of a night")
        return cls(
            players=players,
            budgets=budgets,
            values=values,
            signals=signals,
            missions=missions,
            tiers=tiers,
            tags=tags,
            formats=formats,
            paid=dict(paid),
        )

    def remaining(self, seat: int, lot: int) -> int:
        """The budget of a seat just before ``lot``: the opening budget less earlier payments."""
        spent = sum(payments.get(seat, 0) for number, payments in self.paid.items() if number < lot)
        return self.budgets[seat] - spent

    def own_number(self, seat: int, lot: int) -> int | None:
        """The seat's value for an ordinary lot, its appraisal of a blind box, else None."""
        mine = self.values.get(seat, {})
        if lot in mine:
            return mine[lot]
        return self.signals.get(seat, {}).get(lot)

    def mission_of(self, seat: int) -> Mission | None:
        """The secret mission of a seat, or None if it holds none."""
        return self.missions.get(seat)

    def collector_of(self, seat: int) -> CollectorMission | None:
        """The seat's mission if it is a collector's."""
        mission = self.missions.get(seat)
        return mission if isinstance(mission, CollectorMission) else None

    def shill_of(self, seat: int) -> ShillMission | None:
        """The seat's mission if it is a shill's."""
        mission = self.missions.get(seat)
        return mission if isinstance(mission, ShillMission) else None
