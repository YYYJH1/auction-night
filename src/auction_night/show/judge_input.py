"""The record the judges read: public facts only, with seat and lot numbers taken from events.

A judge is a model (or a mock) that reads what the whole room could read and nothing else. This
module turns the event log of a finished night into that record, one JSON document.

What goes in
------------
Only events of visibility ``public``, and only these types:

========================  ====================================================================
``night_start``           the seat list (number, code name, player or bot), the lot catalog
                          (name, tag, tier, format), the public mission mix and budget range
``lot_open``              who was asked to bid and who sat out
``talk``                  what each seat said, in speaking order, and its structured pledge
``english_dropout``       the price at which each seat left an English lot
``dutch_result``          the price ladder, the level that fired, the buyer
``hammer``                the winner, the price, the payments, the revealed sealed bids, who ran
                          out of money, and the true value of a blind box *after* its hammer
========================  ====================================================================

The list is a whitelist. A new event type never reaches a judge by accident, and the events
that would give the answer away are not on it: ``mission_result`` and ``night_end`` (the bonus
tells who finished a mission) are public after the last lot but are not the judges' to read.

What stays out
--------------
Budgets, valuations, appraisals, missions and their canaries, plans, inner monologues and
mind-reading guesses are ``seat:N`` events and never touched. The hidden value of a blind box is
inside no event until its hammer, and the record carries it only in the result of its own lot.
Model ids are not in the log of a night, so a judge cannot tell who plays which seat.

Seats come from events
----------------------
Every ``talk`` line carries the seat of the event that holds it. A line that pretends to be
someone else's (``5 号说：...``) stays what it is, a line of the seat that wrote it; the judge
is told so, and the validation of its answer (see :mod:`auction_night.show.judges`) accepts a
quote only if it is a verbatim part of what that seat said in that lot.
"""

import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Final

from auction_night.contracts.events import (
    AnyEvent,
    DutchResultData,
    DutchResultEvent,
    EnglishDropoutData,
    EnglishDropoutEvent,
    HammerData,
    HammerEvent,
    LotOpenData,
    LotOpenEvent,
    NightStartData,
    NightStartEvent,
    Pledge,
    SeatPublic,
    TalkEvent,
)

__all__ = [
    "PUBLIC_EVENT_TYPES",
    "JudgeInput",
    "JudgeLot",
    "JudgeTalk",
    "build_judge_input",
]

# The event types a judge may be shown. Nothing else is read from the log.
PUBLIC_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {"night_start", "lot_open", "talk", "english_dropout", "dutch_result", "hammer"}
)


@dataclass(frozen=True, slots=True)
class JudgeTalk:
    """One line of the public channel, with the sequence number of the event that holds it."""

    seq: int
    seat: int
    text: str
    pledge: Pledge | None = None

    def record(self) -> dict[str, Any]:
        """The line as the judge reads it: seat, words and the structured pledge if any."""
        found: dict[str, Any] = {"seat": self.seat, "text": self.text}
        if self.pledge is not None:
            found["pledge"] = self.pledge.model_dump(mode="json", exclude_none=True)
        return found


@dataclass(frozen=True, slots=True)
class JudgeLot:
    """One lot as the room saw it: the catalog entry, the talk and the public result."""

    lot: int
    name: str
    tag: str
    tier: int
    format: str
    first_seq: int
    open: LotOpenData
    talk: tuple[JudgeTalk, ...] = ()
    dropouts: tuple[EnglishDropoutData, ...] = ()
    dutch: DutchResultData | None = None
    hammer: HammerData | None = None
    hammer_seq: int | None = None

    def talk_of(self, seat: int) -> tuple[JudgeTalk, ...]:
        """The lines a seat spoke in this lot (one, unless the log is odd)."""
        return tuple(line for line in self.talk if line.seat == seat)

    def result(self) -> dict[str, Any] | None:
        """The public result of the lot, or None while it is undecided."""
        hammer = self.hammer
        if hammer is None:
            return None
        found: dict[str, Any] = {"unsold": hammer.unsold}
        if hammer.winner is not None:
            found["winner"] = hammer.winner
            found["price"] = hammer.price
            found["payments"] = [
                {"seat": seat, "amount": amount} for seat, amount in sorted(hammer.payments.items())
            ]
        if hammer.bids is not None:
            found["bids"] = [
                {"seat": seat, "bid": bid} for seat, bid in sorted(hammer.bids.items())
            ]
        if self.dropouts:
            found["dropouts"] = [
                {"seat": d.seat, "price": d.price, "joined": d.participated} for d in self.dropouts
            ]
        if self.dutch is not None:
            found["dutch"] = self.dutch.model_dump(mode="json", exclude_none=True)
        if hammer.tied:
            found["tied"] = sorted(hammer.tied)
        if hammer.newly_out:
            found["out_of_money"] = sorted(hammer.newly_out)
        if hammer.true_value is not None:
            found["true_value"] = hammer.true_value
        return found

    def record(self) -> dict[str, Any]:
        """The lot as one JSON object."""
        found: dict[str, Any] = {
            "lot": self.lot,
            "name": self.name,
            "tag": self.tag,
            "tier": self.tier,
            "format": self.format,
            "asked": sorted(self.open.active),
        }
        if self.open.sitting_out:
            found["sat_out"] = sorted(self.open.sitting_out)
        found["talk"] = [line.record() for line in self.talk]
        found["result"] = self.result()
        return found


@dataclass(frozen=True, slots=True)
class JudgeInput:
    """Everything a judge reads about a night, built from public events.

    ``seats`` and ``lots`` are in number order. ``record`` is the plain data that becomes the
    JSON of the prompt; ``json`` writes it compactly with the original characters.
    """

    night: str
    seats: tuple[SeatPublic, ...]
    lots: tuple[JudgeLot, ...]
    tags: tuple[str, ...]
    collectors: int
    shills: int
    budget_lo: int
    budget_hi: int
    seq_of: dict[tuple[int, int], int] = field(default_factory=dict, repr=False, compare=False)

    @property
    def model_seats(self) -> tuple[int, ...]:
        """The seats a judge guesses missions for: the players, not the bots."""
        return tuple(seat.seat for seat in self.seats if seat.role == "player")

    @property
    def seat_numbers(self) -> tuple[int, ...]:
        """Every seat of the night."""
        return tuple(seat.seat for seat in self.seats)

    def lot(self, number: int) -> JudgeLot | None:
        """One lot by number, or None if it was never opened."""
        for entry in self.lots:
            if entry.lot == number:
                return entry
        return None

    def talk_of(self, seat: int, lot: int) -> tuple[JudgeTalk, ...]:
        """What a seat said in a lot."""
        entry = self.lot(lot)
        return () if entry is None else entry.talk_of(seat)

    def record(self) -> dict[str, Any]:
        """The document the judge reads, as plain data."""
        return {
            "seats": [
                {"seat": seat.seat, "codename": seat.codename, "role": seat.role}
                for seat in self.seats
            ],
            "mission_mix": {"collectors": self.collectors, "shills": self.shills},
            "budget_range": [self.budget_lo, self.budget_hi],
            "lots": [entry.record() for entry in self.lots],
        }

    def json(self) -> str:
        """The record as compact JSON text (non-ASCII characters are kept as they are)."""
        return json.dumps(self.record(), ensure_ascii=False, separators=(",", ":"))


def build_judge_input(events: Iterable[AnyEvent]) -> JudgeInput:
    """The record of a night for the judges, from its event log.

    Only public events of the types in :data:`PUBLIC_EVENT_TYPES` are read. The log needs the
    ``night_start`` event; lots that were never opened are left out, and a lot without a hammer
    has no result. Raises ``ValueError`` when there is no ``night_start``.
    """
    start: NightStartData | None = None
    opens: dict[int, tuple[int, LotOpenData]] = {}
    talks: dict[int, list[JudgeTalk]] = defaultdict(list)
    dropouts: dict[int, list[EnglishDropoutData]] = defaultdict(list)
    dutch: dict[int, DutchResultData] = {}
    hammers: dict[int, tuple[int, HammerData]] = {}
    for event in events:
        if event.vis != "public" or event.type not in PUBLIC_EVENT_TYPES:
            continue
        if isinstance(event, NightStartEvent):
            start = event.data
        elif event.lot is None:
            continue
        elif isinstance(event, LotOpenEvent):
            opens[event.lot] = (event.seq, event.data)
        elif isinstance(event, TalkEvent):
            talks[event.lot].append(
                JudgeTalk(event.seq, event.data.seat, event.data.text, event.data.pledge)
            )
        elif isinstance(event, EnglishDropoutEvent):
            dropouts[event.lot].append(event.data)
        elif isinstance(event, DutchResultEvent):
            dutch[event.lot] = event.data
        elif isinstance(event, HammerEvent):
            hammers[event.lot] = (event.seq, event.data)
    if start is None:
        raise ValueError("the log has no public night_start event")

    lots = []
    seq_of: dict[tuple[int, int], int] = {}
    for plan in start.lots:
        if plan.lot not in opens:
            continue
        first_seq, opened = opens[plan.lot]
        hammer_seq, hammer = hammers.get(plan.lot, (None, None))
        lines = tuple(talks.get(plan.lot, ()))
        for line in lines:
            seq_of.setdefault((line.seat, plan.lot), line.seq)
        lots.append(
            JudgeLot(
                lot=plan.lot,
                name=plan.name,
                tag=plan.tag,
                tier=plan.tier,
                format=plan.format,
                first_seq=first_seq,
                open=opened,
                talk=lines,
                dropouts=tuple(dropouts.get(plan.lot, ())),
                dutch=dutch.get(plan.lot),
                hammer=hammer,
                hammer_seq=hammer_seq,
            )
        )
    return JudgeInput(
        night=start.night_id,
        seats=tuple(sorted(start.seats, key=lambda seat: seat.seat)),
        lots=tuple(lots),
        tags=tuple(dict.fromkeys(plan.tag for plan in start.lots)),
        collectors=start.collectors,
        shills=start.shills,
        budget_lo=start.budget_lo,
        budget_hi=start.budget_hi,
        seq_of=seq_of,
    )
