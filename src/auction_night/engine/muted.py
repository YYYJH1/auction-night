"""The actions that the cost gate took away from the model seats.

When a night reaches its cost cap, the model layer stops calling the models. Every action that is
still to come reaches the engine as an abstention with the reason ``budget_aborted``, and the night
is played to its end by the rule bots (see :mod:`auction_night.llm.accounting`). The log of such a
night holds many ``abstain`` actions that no seat decided on.

This module reads them off the log. A seat is **muted** in a lot when the gate skipped its line in
the channel (the ``talk`` phase) or its action in a bid phase. What the log shows for a muted seat
in that lot is the cap's doing and not the seat's, so the show layer keeps it out of everything that
measures behaviour: pledges, the collusion screen, the awards, the highlights, the predictions and
the reference numbers of the theory module. The other reasons of an abstention (``missing``,
``parse_failed``, ``timeout`` and ``refusal``) are calls that went wrong. They stay with the seat.

Only an action that the log names counts. An English revision that the gate skips is logged as
``kept_previous``, which has no reason (see the engine module), so it is not read as muted. The exit
price that stays in force is the one the seat named in the round before.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final

from auction_night.contracts.events import ActionEvent, AnyEvent
from auction_night.contracts.mechanism import BID_PHASES

__all__ = ["MUTED_REASON", "Muted", "muted_actions"]

MUTED_REASON: Final = "budget_aborted"


@dataclass(frozen=True, slots=True)
class Muted:
    """The seats that the gate skipped, by lot.

    ``talks`` maps a lot to the seats whose line was skipped and ``bids`` to the seats whose
    action in a bid phase was skipped. A lot that the gate did not touch has no entry.
    """

    talks: Mapping[int, frozenset[int]] = field(default_factory=dict)
    bids: Mapping[int, frozenset[int]] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.talks or self.bids)

    @property
    def first_lot(self) -> int | None:
        """The first lot in which the gate skipped a seat, or None for a night it never cut."""
        lots = {*self.talks, *self.bids}
        return min(lots) if lots else None

    def seats_in(self, lot: int) -> tuple[int, ...]:
        """The seats that the gate skipped in a lot, in seat order (a line or a bid)."""
        return tuple(sorted(self.talks.get(lot, frozenset()) | self.bids.get(lot, frozenset())))

    def bid_muted(self, lot: int, seat: int) -> bool:
        """Whether the gate skipped the action of ``seat`` in a bid phase of ``lot``."""
        return seat in self.bids.get(lot, frozenset())

    def lot_muted(self, lot: int) -> bool:
        """Whether the gate skipped any bid of ``lot``: the models did not play it."""
        return bool(self.bids.get(lot))


def muted_actions(events: Iterable[AnyEvent]) -> Muted:
    """The actions of a log that the cost gate skipped. Events of other kinds are ignored."""
    talks: defaultdict[int, set[int]] = defaultdict(set)
    bids: defaultdict[int, set[int]] = defaultdict(set)
    for event in events:
        if not isinstance(event, ActionEvent) or event.lot is None:
            continue
        data = event.data
        if data.status != "abstain" or data.reason != MUTED_REASON:
            continue
        if data.phase == "talk":
            talks[event.lot].add(data.seat)
        elif data.phase in BID_PHASES:
            bids[event.lot].add(data.seat)
    return Muted(
        talks={lot: frozenset(seats) for lot, seats in talks.items()},
        bids={lot: frozenset(seats) for lot, seats in bids.items()},
    )
