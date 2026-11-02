"""The structured data of a fact card: one lot, seat by seat.

A fact card has three faces. This module defines the first, the data: frozen models that hold
every fact about one lot, taken from the event log, the private cards of the deal and the
reference numbers of the theory module. The second face is the Chinese text that the host reads,
and the third is the set of numbers that text contains (see :mod:`auction_night.show.factcard`).

Everything here is an integer, a short string or a flag. There are no floats, so the canonical
JSON of a card is the same on every machine. The reference numbers of one lot (``ref_bid``,
``excess``, ``regret``, ``alpha_milli``, ``expected_revenue_milli``, ``curse_gap``) belong to that
lot.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from auction_night.contracts.events import (
    AbstainReason,
    ActionStatus,
    DutchResultData,
    EnglishDropoutData,
    MissionKind,
    Pledge,
)
from auction_night.contracts.mechanism import FormatKey, Phase
from auction_night.contracts.replay import HighlightCategory, PledgeRecord

__all__ = [
    "ActionLine",
    "Candidate",
    "CardModel",
    "GuessLine",
    "LotData",
    "MissionLine",
    "PriorLine",
    "SeatLine",
]


class CardModel(BaseModel):
    """Base of the card models: immutable, and unknown fields are errors."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ActionLine(CardModel):
    """One bid-phase action of a seat, as the log records it (value after the clamp)."""

    phase: Phase
    status: ActionStatus
    reason: AbstainReason | None = None
    value: int | None = None
    raw_value: int | None = None


class GuessLine(CardModel):
    """A mind-reading guess of a seat with the truth at the moment it was asked."""

    target: int
    guess: int
    truth: int
    abs_error: int


class SeatLine(CardModel):
    """One seat in one lot.

    ``played`` is the number the seat's decision came to: the bid, the final English exit price
    or the Dutch grab price. It is None for a seat that was not asked and for a seat that gave
    no usable action (``abstained``). ``muted`` is set when the cost gate skipped the seat's bid
    in this lot: the seat is ``abstained`` too, but it did not choose that. ``value`` and
    ``signal`` are shown for seats that were asked. The reference numbers are the lot's, per seat
    that bid.
    """

    seat: int
    codename: str
    role: Literal["player", "bot"]
    asked: bool
    budget_before: int
    budget_after: int
    value: int | None = None
    signal: int | None = None
    plan_stars: int | None = None
    plan_budget: int | None = None
    said: str = ""
    pledge: Pledge | None = None
    actions: tuple[ActionLine, ...] = ()
    abstained: bool = False
    muted: bool = False
    played: int | None = None
    paid: int = 0
    won: bool = False
    ref_bid: int | None = None
    excess: int | None = None
    regret: int | None = None
    alpha_milli: int | None = None
    guess: GuessLine | None = None


class PriorLine(CardModel):
    """The public prior of a blind box: the true value lies in ``low..high``, every appraisal
    is within ``noise`` of it."""

    low: int
    high: int
    noise: int


class MissionLine(CardModel):
    """The state of a secret mission after this lot, for missions this lot can move.

    A collector appears in lots of its tag, a shill in its target lot. ``outcome`` is
    ``advanced`` (the collector won this lot), ``done`` (the collector has all it needs),
    ``missed`` (the collector did not win a lot of its tag), ``reached`` (the shill's lot sold
    at or above the threshold to someone else), ``stuck`` (the shill bought its own lot),
    ``short`` (sold below the threshold) or ``unsold``.
    """

    seat: int
    kind: MissionKind
    outcome: Literal["advanced", "done", "missed", "reached", "stuck", "short", "unsold"]
    tag: str | None = None
    need: int | None = None
    won_so_far: int | None = None
    tag_lots_left: int | None = None
    target_lot: int | None = None
    threshold: int | None = None
    price: int | None = None
    buyer: int | None = None


class Candidate(CardModel):
    """A moment that may deserve a highlight. Nothing is scored here.

    ``facts`` holds the numbers that made the lot a candidate. ``reason`` is the sentence the
    host and the card text use, built from the same numbers.
    """

    category: HighlightCategory
    lot: int
    seats: tuple[int, ...]
    facts: dict[str, int]
    reason: str


class LotData(CardModel):
    """Every fact about one decided lot. See the module text.

    ``muted`` lists the seats whose line or bid the cost gate skipped in this lot, and
    ``muted_from`` the first lot of the night in which it skipped anything. Both are empty for a
    lot that the gate did not touch.
    """

    lot: int
    item: str
    name: str
    emoji: str
    tag: str
    tier: int
    format: FormatKey
    title: str
    mechanism: str
    reserve: int
    rule: str
    bidders: tuple[int, ...]
    sitting_out: tuple[int, ...]
    seats: tuple[SeatLine, ...]
    muted: tuple[int, ...] = ()
    muted_from: int | None = None
    winner: int | None = None
    price: int = 0
    unsold: bool = False
    payments: dict[int, int] = {}
    tied: tuple[int, ...] = ()
    newly_out: tuple[int, ...] = ()
    true_value: int | None = None
    prior: PriorLine | None = None
    curse_gap: int | None = None
    top_signal_error: int | None = None
    to_highest_value: bool | None = None
    top_value: int | None = None
    top_value_seats: tuple[int, ...] = ()
    winner_exit: int | None = None
    dropouts: tuple[EnglishDropoutData, ...] = ()
    dutch: DutchResultData | None = None
    expected_revenue_milli: int | None = None
    pledges_made: tuple[PledgeRecord, ...] = ()
    pledges_due: tuple[PledgeRecord, ...] = ()
    missions: tuple[MissionLine, ...] = ()
    candidates: tuple[Candidate, ...] = ()

    @property
    def bids_muted(self) -> bool:
        """Whether the cost gate skipped the bid of any seat in this lot."""
        return any(line.muted for line in self.seats)

    def seat_line(self, seat: int) -> SeatLine:
        """The line of one seat."""
        for line in self.seats:
            if line.seat == seat:
                return line
        raise KeyError(seat)
