"""Shared types and helpers of the lot formats.

One lot is a short conversation between the engine and the seats. The engine opens the lot
and says which seats to ask (and in which phase). The caller collects the answers, hands
them back, and the engine says who to ask next, or that the lot is decided. Every function
is pure: nothing is read from a clock or an environment, and no argument is changed.

Conventions shared by all formats:

* Amounts are integers. A bid may arrive as an ``int``, a ``Fraction`` or a finite ``float``
  and is floored; ``bool`` and anything else is refused.
* ``None`` (or a missing key) is an abstention. It is never turned into a default price.
* A bid above the remaining budget is clamped to the budget. This is legal and recorded as
  status ``clamped`` with the raw value.
* ``tie_key`` maps each seat to a rank; the lowest rank wins every tie. Equal ranks fall back
  to the lower seat number, so the order is always total.
"""

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from fractions import Fraction
from typing import Final, Protocol

from auction_night.contracts.events import (
    AbstainReason,
    ActionData,
    ActionStatus,
    DutchResultData,
    EnglishDropoutData,
    HammerData,
)
from auction_night.contracts.mechanism import MAX_SEATS, BidPhase, MechanismDef

type Answers = Mapping[int, int | None]
type TieKey = Mapping[int, int]

# The reason recorded for an abstention that the engine itself detects: no action was
# supplied. A caller that knows a more specific reason (timeout, refusal, ...) may replace it.
DEFAULT_ABSTAIN_REASON: Final[AbstainReason] = "missing"


def floor_amount(value: object) -> int:
    """An amount as an integer. Non-integers are floored.

    Accepts ``int``, ``Fraction`` and finite ``float``. Booleans, NaN, infinities and any
    other type are refused: a boolean is not an amount.
    """
    if isinstance(value, bool):
        raise TypeError("a boolean is not an amount")
    if isinstance(value, int):
        return value
    if isinstance(value, Fraction):
        return math.floor(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("an amount must be finite")
        return math.floor(value)
    raise TypeError(f"an amount is a number, got {type(value).__name__}")


def normalize_bid(raw: object | None, budget: int) -> tuple[ActionStatus, int | None, int | None]:
    """Status, effective value and raw value of one submitted number.

    ``None`` is an abstention (no value). A number is floored and, when it exceeds the
    remaining budget, clamped to the budget with status ``clamped`` and the raw value kept.
    A negative number is a caller error: the parsing layer rejects it before the engine.
    """
    if raw is None:
        return "abstain", None, None
    amount = floor_amount(raw)
    if amount < 0:
        raise ValueError("a bid cannot be negative")
    if amount > budget:
        return "clamped", budget, amount
    return "ok", amount, None


@dataclass(frozen=True, slots=True)
class BidRecord:
    """One submission as the engine understood it.

    ``value`` is the effective number (after flooring and clamping); ``raw_value`` is set only
    when it was clamped. An abstention has no value. A failed English revision has status
    ``kept_previous`` and carries the exit price that stays in force.
    """

    seat: int
    phase: BidPhase
    status: ActionStatus
    value: int | None = None
    raw_value: int | None = None
    reason: AbstainReason | None = None

    def to_action(self, thought: str = "") -> ActionData:
        """The contract record of this submission. The caller supplies the seat's thought."""
        return ActionData(
            seat=self.seat,
            phase=self.phase,
            status=self.status,
            reason=self.reason,
            value=self.value,
            raw_value=self.raw_value,
            thought=thought,
        )


def make_record(seat: int, phase: BidPhase, raw: object | None, budget: int) -> BidRecord:
    """Normalise one answer into a record (abstention reason ``missing`` when absent)."""
    status, value, raw_value = normalize_bid(raw, budget)
    reason = DEFAULT_ABSTAIN_REASON if status == "abstain" else None
    return BidRecord(seat, phase, status, value, raw_value, reason)


@dataclass(frozen=True, slots=True)
class LotOutcome:
    """The decided lot.

    ``hammer`` is the public result. ``records`` are all submissions in the order they were
    processed. ``asked`` are the seats that were asked at least once. ``dropouts`` (English)
    and ``dutch`` (Dutch) are the public announcements of the clock formats.
    ``winner_exit`` is the winner's final exit price (English only): it is not public.
    """

    hammer: HammerData
    records: tuple[BidRecord, ...]
    asked: tuple[int, ...]
    dropouts: tuple[EnglishDropoutData, ...] = ()
    dutch: DutchResultData | None = None
    winner_exit: int | None = None


class LotState(Protocol):
    """What the registry needs from the state of any format: its mechanism definition."""

    @property
    def mech(self) -> MechanismDef: ...


@dataclass(frozen=True, slots=True)
class LotStep:
    """What the caller must do next.

    ``ask`` lists the seats to ask now, all at once, in ``phase``. ``state`` is handed back
    to the step function together with the answers. ``records`` and ``dropouts`` are what the
    step that produced this value understood and announced (not cumulative). ``outcome`` is
    set exactly when the lot is decided; then ``ask`` is empty.

    The state holds hidden information (the exit prices of the seats still in, the true value
    of a blind box). It belongs to the engine: a prompt may only show what the events of the
    lot have made public plus the asked seat's own numbers.
    """

    ask: tuple[int, ...]
    phase: BidPhase | None
    state: LotState
    records: tuple[BidRecord, ...] = ()
    dropouts: tuple[EnglishDropoutData, ...] = ()
    outcome: LotOutcome | None = None

    @property
    def done(self) -> bool:
        """True when the lot is decided."""
        return self.outcome is not None


# ------------------------------------------------------------------------------ checks


def check_table(budgets: Mapping[int, int], tie_key: TieKey) -> None:
    """Refuse an impossible table: seats outside 1..8, bad budgets, active seats without a rank.

    Only seats that can be asked (budget of at least 1) need a tie key.
    """
    for seat, budget in budgets.items():
        if isinstance(seat, bool) or not isinstance(seat, int) or not 1 <= seat <= MAX_SEATS:
            raise ValueError(f"seat out of range: {seat!r}")
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
            raise ValueError(f"budget of seat {seat} must be a non-negative integer")
        if budget >= 1:
            key = tie_key.get(seat)
            if isinstance(key, bool) or not isinstance(key, int):
                raise ValueError(f"seat {seat} has no integer tie key")


def active_seats(budgets: Mapping[int, int]) -> tuple[int, ...]:
    """Seats still in the night: remaining budget of at least 1, in seat order."""
    return tuple(sorted(seat for seat, budget in budgets.items() if budget >= 1))


def check_answers(asked: tuple[int, ...], answers: Mapping[int, object]) -> None:
    """Refuse answers from seats that were not asked."""
    stray = sorted(set(answers) - set(asked))
    if stray:
        raise ValueError(f"answers from seats that were not asked: {stray}")


def tie_order(seats: Iterable[int], tie_key: TieKey) -> list[int]:
    """Seats ordered by tie priority: the first one wins a tie."""
    return sorted(seats, key=lambda seat: (tie_key[seat], seat))


def newly_out(budgets: Mapping[int, int], payments: Mapping[int, int]) -> list[int]:
    """Paying seats whose remaining budget falls from at least 1 to exactly 0."""
    return sorted(
        seat
        for seat, paid in payments.items()
        if paid > 0 and budgets[seat] >= 1 and budgets[seat] - paid < 1
    )
