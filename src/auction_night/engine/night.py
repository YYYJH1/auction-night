"""The night: one pure state machine from the first deal to the final scores.

``start(config, deal)`` opens the night and ``step(state, answers)`` moves it on. Both are pure
functions of their arguments: nothing is read from a clock, an environment or a random
generator, and no argument is changed. The driver (model layer, bots, a human at a terminal, a
test) only does what a :class:`Request` says::

    state, request = start(config, deal)
    log = list(state.events)                # night_start, deal_private, ...
    while request is not None:
        answers = {seat: decide(request, seat) for seat in request.seats}
        state, events, request = step(state, answers)
        log.extend(events)

The order of play:

1. **plan**: every model seat hands in its opening plan, all at once (bots make none).
2. **lots**: for each lot the engine opens it (``lot_open``), then every seat speaks once,
   *one at a time* in the speaking order (``talk``), because later speakers read what earlier
   ones said. The talk call also carries the mind-reading guess and an optional pledge. Then
   the seats that can still bid are asked, all at once, in the phases of the lot's format (an
   English lot has a second kind of round while few seats remain), and the hammer falls.
3. **close**: after the last lot the missions are decided (``mission_result``) and the scores
   are written (``night_end``).

The speaking order moves by one seat with every lot. Budgets are tracked here: payments are
subtracted after each hammer, and a seat whose budget is used up is *out*: it still speaks but
is no longer asked to bid.

The status of the night
-----------------------
``night_end`` says ``completed``, or ``budget_aborted`` when the cost gate of the model layer
skipped an action. The model layer hands such a seat an abstention with the reason
``budget_aborted``, and the engine marks the night as soon as it is handed one, in any phase
(:attr:`NightState.budget_aborted`). The status is read off that mark and not off the log,
because the log cannot always show the cause: an English revision that does not come is logged as
``kept_previous``, which carries no reason, so a skipped revision looks like a timed-out one. The
mark is a function of the answers alone, so the same answers give the same status and the same
bytes.

What may leave the engine, and where
------------------------------------
Each event has the visibility the contract gives its type: ``deal_private``, ``plan``,
``guess`` and ``action`` are ``seat:N``, everything else the engine writes is ``public``. The
:class:`Request` of a seat carries only that seat's own numbers (budget, value or appraisal,
its own English exit price, its guess target) next to public facts. The state itself holds
everything (it is the engine's memory), so it is never shown to a seat and its ``repr`` leaves
the secrets out. The English exit prices of the other seats and the true value of a blind box
leave the engine only through the events that announce them: drop-outs, and the hammer.

Events that the engine does not write (the ``system`` events of the model layer, the
``audience`` events of the show layer) are added to the log with :func:`append_events`, which
numbers them after the last event so that ``seq`` stays gapless.

Texts are cleaned by the caller. The caller hands in text that already went through the
sanitiser (the parsing ladder removes control and structure characters and counts what it
removed); the engine refuses text that the event contract would refuse, records the counts it is
given, and enforces the lengths of the channel as a last guard: a line, a thought or a persona
longer than the configured cap is cut to it, and the characters cut are added to the counts.
Plain-text pledges are stored as written and not judged here.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal, get_args

from pydantic import TypeAdapter, ValidationError

from auction_night.contracts.act import extract_pledge
from auction_night.contracts.config import NightConfig, NightSettings
from auction_night.contracts.events import (
    MAX_COINS,
    PRIORITY_MAX,
    AbstainReason,
    ActionData,
    ActionEvent,
    AnyEvent,
    DealPrivateEvent,
    DutchResultEvent,
    EnglishDropoutEvent,
    EventBase,
    GuessData,
    GuessEvent,
    HammerData,
    HammerEvent,
    Line,
    LotOpenData,
    LotOpenEvent,
    LotPlan,
    MissionResultData,
    MissionResultEvent,
    Mode,
    Name,
    NightEndData,
    NightEndEvent,
    NightStartData,
    NightStartEvent,
    PlanData,
    PlanEvent,
    Pledge,
    SeatPublic,
    SeatScore,
    SeedCommitData,
    SeedCommitEvent,
    TalkData,
    TalkEvent,
    seat_vis,
    vis_kind,
)
from auction_night.contracts.mechanism import MAX_SEATS, MECHANISMS, FormatKey, Phase
from auction_night.engine.deal import BlindBounds, Deal, blind_bounds, budget_range
from auction_night.engine.formats import (
    BidRecord,
    EnglishState,
    LotOutcome,
    LotStep,
    open_lot,
    step_lot,
)
from auction_night.engine.missions import evaluate_missions, lot_results
from auction_night.engine.scoring import seat_scores, tally_night

__all__ = [
    "Answer",
    "AnswerError",
    "GuessTruth",
    "ItemInfo",
    "LotRecord",
    "NightState",
    "Request",
    "SeatFacts",
    "Slot",
    "append_events",
    "guess_target",
    "placeholder_items",
    "start",
    "step",
    "talk_order",
]

Stage = Literal["plan", "talk", "bid", "done"]

_LINE: Final = TypeAdapter(Line)
_NAME: Final = TypeAdapter(Name)
_ABSTAIN_REASONS: Final = frozenset(get_args(AbstainReason))
_BID_FIELDS: Final = {
    "english_init": "exit_price",
    "english_revise": "exit_price",
    "dutch": "grab_price",
    "sealed": "bid",
}


class AnswerError(ValueError):
    """An answer that the engine cannot use. It is a bug of the driver, not a move of a seat:
    a seat that cannot give a valid action abstains (see :meth:`Answer.abstained`)."""


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# -------------------------------------------------------------------------------- the lots


@dataclass(frozen=True, slots=True)
class ItemInfo:
    """What is on the block: the deal fixes a lot's format, tier and tag, and the catalogue
    gives it a name, a picture and a blurb."""

    item: str
    name: str
    emoji: str
    blurb: str = ""


def placeholder_items(n_lots: int) -> tuple[ItemInfo, ...]:
    """Plain stand-ins for a night without a catalogue."""
    return tuple(
        ItemInfo(item=f"lot-{k:02d}", name=f"拍品 {k} 号", emoji="📦") for k in range(1, n_lots + 1)
    )


@dataclass(frozen=True, slots=True)
class Slot:
    """One lot of the public schedule: everybody knows it from the preview."""

    lot: int
    format: FormatKey
    tier: int

    @property
    def is_blind(self) -> bool:
        """True for a lot of common value (the blind box)."""
        return MECHANISMS[self.format].value_model == "common"


# ------------------------------------------------------------------------------- the asking


@dataclass(frozen=True, slots=True)
class SeatFacts:
    """What one asked seat may use in its prompt, besides the public facts of the request.

    ``value`` is the seat's own value for the lot in play (ordinary lots), ``signal`` its
    appraisal (blind boxes). ``exit_price`` is the exit price the seat has in force in an
    English revision round. ``guess_target`` is the opponent whose remaining budget the seat
    is asked to guess in the talk phase (None when there is nothing to guess).
    """

    seat: int
    budget_left: int
    value: int | None = None
    signal: int | None = None
    exit_price: int | None = None
    guess_target: int | None = None


@dataclass(frozen=True, slots=True)
class Request:
    """The next thing the engine needs: which seats to ask, in which phase, and the facts.

    ``seats`` are asked all at once, except in the talk phase, where one seat is asked at a
    time. ``facts[i]`` belongs to ``seats[i]`` and holds that seat's own numbers only. The
    rest is public: the schedule of the lots still to play (``upcoming``, the current lot
    first), the rules of the lot in play, the number of ``bidders`` (seats asked to bid in this
    lot) and, in an English revision round, the ``price`` of the clock and the ``remaining``
    seats. ``blind`` holds the public prior bounds of a blind box.
    """

    index: int
    phase: Phase
    lot: int | None
    seats: tuple[int, ...]
    facts: tuple[SeatFacts, ...]
    n_lots: int
    n_seats: int
    mode: Mode
    upcoming: tuple[Slot, ...]
    format: FormatKey | None = None
    tier: int | None = None
    reserve: int = 0
    bidders: int = 0
    blind: BlindBounds | None = None
    price: int | None = None
    remaining: tuple[int, ...] = ()

    def facts_of(self, seat: int) -> SeatFacts:
        """The facts of one asked seat."""
        for facts in self.facts:
            if facts.seat == seat:
                return facts
        raise KeyError(seat)


@dataclass(frozen=True, slots=True)
class Answer:
    """What one seat did when it was asked.

    * ``value``: the number of a bid phase (exit price, grab price or bid), as written. The
      engine floors nothing (it is an integer), clamps it to the remaining budget and records
      the raw value when it clamps. ``None`` is an abstention.
    * ``say``, ``pledge``, ``guess``: the talk phase. The line may be empty.
    * ``priorities``, ``budget_plan``, ``persona``: the plan phase.
    * ``thought``: the inner monologue of any phase.
    * ``abstain``: the reason when the seat gave no usable action (``timeout``, ``refusal``,
      ``parse_failed``, ``budget_aborted``, ``missing``). An abstention carries no action. One
      with the reason ``budget_aborted`` also marks the night as aborted for the cost cap,
      whichever way the action ends up in the log (see the module text).
    * ``sanitized_chars`` counts the characters the sanitiser removed in the whole call and goes
      to the action event. ``text_sanitized_chars`` counts them in the public text (the line
      or the persona) and goes to the talk or plan event.
    """

    seat: int
    value: int | None = None
    say: str = ""
    pledge: Pledge | None = None
    guess: int | None = None
    priorities: tuple[int, ...] | None = None
    budget_plan: tuple[int, ...] | None = None
    persona: str = ""
    thought: str = ""
    abstain: AbstainReason | None = None
    sanitized_chars: int = 0
    text_sanitized_chars: int = 0
    attempts: int = 1
    call_id: str | None = None

    def __post_init__(self) -> None:
        if not _is_int(self.seat) or not 1 <= self.seat <= MAX_SEATS:
            raise AnswerError("seat out of range")
        for name in ("value", "guess"):
            number = getattr(self, name)
            if number is not None and (not _is_int(number) or not 0 <= number <= MAX_COINS):
                raise AnswerError(f"{name} must be a non-negative integer")
        for name in ("say", "persona", "thought"):
            text = getattr(self, name)
            try:
                _LINE.validate_python(text, strict=True)
            except ValidationError:
                raise AnswerError(f"{name} is not a valid single line of text") from None
        if self.call_id is not None:
            try:
                _NAME.validate_python(self.call_id, strict=True)
            except ValidationError:
                raise AnswerError("call_id is not a valid name") from None
        for name in ("sanitized_chars", "text_sanitized_chars"):
            if not _is_int(getattr(self, name)) or getattr(self, name) < 0:
                raise AnswerError(f"{name} must be a non-negative integer")
        if not _is_int(self.attempts) or not 1 <= self.attempts <= 10:
            raise AnswerError("attempts must lie between 1 and 10")
        for name in ("priorities", "budget_plan"):
            numbers = getattr(self, name)
            if numbers is not None and (
                not isinstance(numbers, tuple) or not all(_is_int(n) for n in numbers)
            ):
                raise AnswerError(f"{name} must be a tuple of integers")
        if self.pledge is not None and not isinstance(self.pledge, Pledge):
            raise AnswerError("pledge must be a Pledge")
        if self.abstain is not None:
            if self.abstain not in _ABSTAIN_REASONS:
                raise AnswerError("unknown abstention reason")
            acts = (self.value, self.pledge, self.guess, self.priorities, self.budget_plan)
            if any(item is not None for item in acts) or self.say or self.persona:
                raise AnswerError("an abstention carries no action")

    @classmethod
    def abstained(
        cls, seat: int, reason: AbstainReason = "missing", *, thought: str = "", **extra: Any
    ) -> "Answer":
        """The answer of a seat that gave no usable action."""
        return cls(seat=seat, abstain=reason, thought=thought, **extra)

    @classmethod
    def from_act(
        cls,
        seat: int,
        args: Mapping[str, Any],
        *,
        sanitized_chars: int = 0,
        text_sanitized_chars: int = 0,
        attempts: int = 1,
        call_id: str | None = None,
    ) -> "Answer":
        """The answer for a validated ``act`` call (the arguments in tool field order)."""
        phase = args["phase"]
        extra: dict[str, Any] = {
            "thought": args.get("thought") or "",
            "sanitized_chars": sanitized_chars,
            "text_sanitized_chars": text_sanitized_chars,
            "attempts": attempts,
            "call_id": call_id,
        }
        if phase == "plan":
            extra.update(
                priorities=tuple(args["plan_priorities"]),
                budget_plan=tuple(args["plan_budget"]),
                persona=args.get("plan_persona") or "",
            )
        elif phase == "talk":
            extra.update(
                say=args.get("say") or "",
                pledge=extract_pledge(args),
                guess=args.get("guess_budget"),
            )
        else:
            extra["value"] = args.get(_BID_FIELDS[phase])
        return cls(seat=seat, **extra)


# --------------------------------------------------------------------------- what is kept


@dataclass(frozen=True, slots=True)
class LotRecord:
    """A decided lot, with what the audit and the show layer need: the budgets before it, and
    the outcome of the format (hammer, records, drop-outs, the winner's English exit price)."""

    lot: int
    budgets_before: dict[int, int]
    outcome: LotOutcome


@dataclass(frozen=True, slots=True)
class GuessTruth:
    """A mind-reading guess with the truth at the moment it was asked: the remaining budget
    of the target before the lot in play. The guess event leaves the truth out on purpose."""

    lot: int
    seat: int
    target: int
    guess: int
    truth: int

    @property
    def abs_error(self) -> int:
        """How far the guess was from the truth."""
        return abs(self.guess - self.truth)


@dataclass(frozen=True, slots=True)
class NightState:
    """Everything the engine remembers. Treat it as read-only and as secret.

    ``events`` is the whole log so far, ``pending`` the request waiting for answers (None once
    the night is over). ``budgets`` are the remaining budgets, ``records`` the decided lots,
    ``guesses`` the mind-reading guesses with their truth. After the night, ``missions`` and
    ``scores`` hold what the last events say. ``budget_aborted`` is True once the engine has been
    handed an abstention with the reason ``budget_aborted`` (an action that the cost gate of the
    model layer skipped), and it decides the status that ``night_end`` gets. The fields that hold
    hidden information are left out of ``repr``.
    """

    config: NightConfig = field(repr=False)
    deal: Deal = field(repr=False)
    items: tuple[ItemInfo, ...] = field(repr=False)
    talk_offset: int
    stage: Stage
    lot: int
    events: tuple[AnyEvent, ...] = field(repr=False)
    budgets: dict[int, int] = field(repr=False)
    pending: Request | None = field(repr=False)
    order: tuple[int, ...]
    talked: int
    bidders: int
    lot_step: LotStep | None = field(repr=False)
    records: tuple[LotRecord, ...] = field(repr=False)
    guesses: tuple[GuessTruth, ...] = field(repr=False)
    missions: tuple[MissionResultData, ...] = field(repr=False)
    scores: tuple[SeatScore, ...] = field(repr=False)
    n_requests: int
    budget_aborted: bool = False

    @property
    def done(self) -> bool:
        """True once the scores are written."""
        return self.stage == "done"

    @property
    def out(self) -> tuple[int, ...]:
        """The seats whose budget is used up: they still speak but no longer bid."""
        return tuple(sorted(seat for seat, left in self.budgets.items() if left < 1))

    @property
    def hammers(self) -> tuple[HammerData, ...]:
        """The hammers of the lots decided so far, in lot order."""
        return tuple(record.outcome.hammer for record in self.records)


# ------------------------------------------------------------------------ small functions


def talk_order(seats: Sequence[int], lot: int, offset: int = 0) -> tuple[int, ...]:
    """The speaking order of a lot: the order of the previous lot moved by one seat.

    Lot 1 starts at ``seats[offset]``. Over twelve lots at an eight-seat table, no seat opens
    more than twice.
    """
    shift = (offset + lot - 1) % len(seats)
    return tuple(seats[shift:]) + tuple(seats[:shift])


def guess_target(seat: int, lot: int, budgets: Mapping[int, int]) -> int | None:
    """The opponent that ``seat`` is asked to read in ``lot``, or None if nobody is left.

    The target rotates over ``seat + lot`` through the opponents (bots included) whose budget
    is not used up: the budget of a seat that is out is public, so guessing it would teach
    nothing and would flatter the error.
    """
    rivals = [other for other in sorted(budgets) if other != seat and budgets[other] >= 1]
    return rivals[(seat + lot) % len(rivals)] if rivals else None


def _check_for_phase(request: Request, answer: Answer) -> None:
    """Refuse an answer that cannot be recorded for the phase that was asked."""
    if answer.abstain is not None:
        return
    if request.phase == "plan":
        for name in ("priorities", "budget_plan"):
            numbers = getattr(answer, name)
            if numbers is None or len(numbers) != request.n_lots:
                raise AnswerError(f"{name} needs one entry per lot")
        assert answer.priorities is not None and answer.budget_plan is not None
        if any(not 0 <= p <= PRIORITY_MAX for p in answer.priorities):
            raise AnswerError(f"priorities run from 0 to {PRIORITY_MAX}")
        if any(not 0 <= b <= MAX_COINS for b in answer.budget_plan):
            raise AnswerError("budget_plan entries must be non-negative amounts")
    elif request.phase == "talk" and answer.pledge is not None:
        pledge = answer.pledge
        assert request.lot is not None
        if not request.lot <= pledge.lot <= request.n_lots:
            raise AnswerError("a pledge names a lot that is not still to come")
        if pledge.to is not None and (pledge.to == answer.seat or pledge.to > request.n_seats):
            raise AnswerError("a pledge names a seat that cannot receive it")


def _cut(text: str, cap: int) -> str:
    return text[:cap].rstrip(" ") if len(text) > cap else text


def _capped(answer: Answer, night: NightSettings) -> Answer:
    """The answer with its texts cut to the lengths of the channel (see the module text)."""
    say = _cut(answer.say, night.chat.max_chars)
    persona = _cut(answer.persona, night.chat.persona_max_chars)
    thought = _cut(answer.thought, night.thought.max_chars)
    in_text = len(answer.say) - len(say) + len(answer.persona) - len(persona)
    total = in_text + len(answer.thought) - len(thought)
    if total == 0:
        return answer
    return replace(
        answer,
        say=say,
        persona=persona,
        thought=thought,
        sanitized_chars=answer.sanitized_chars + total,
        text_sanitized_chars=answer.text_sanitized_chars + in_text,
    )


def _check_answers(
    request: Request, answers: Mapping[int, Answer], night: NightSettings
) -> dict[int, Answer]:
    given: dict[int, Answer] = {}
    for seat, answer in answers.items():
        if seat not in request.seats:
            raise AnswerError(f"seat {seat} was not asked")
        if not isinstance(answer, Answer) or answer.seat != seat:
            raise AnswerError(f"the answer filed under seat {seat} is not that seat's answer")
        _check_for_phase(request, answer)
        given[seat] = _capped(answer, night)
    return given


def _check_inputs(config: NightConfig, deal: Deal, items: Sequence[ItemInfo] | None) -> None:
    night = config.night
    seats = sorted(seat.seat for seat in config.seats)
    if deal.n_seats != len(seats) or seats != list(range(1, deal.n_seats + 1)):
        raise ValueError("the deal was drawn for a different table")
    if len(deal.lots) != night.n_lots:
        raise ValueError("the deal has a different number of lots than the configuration")
    for lot in deal.lots:
        if lot.format not in night.formats or lot.mechanism != night.mechanism_for(lot.format).ref:
            raise ValueError(f"lot {lot.lot} does not match the mechanisms of the configuration")
    if items is not None and len(items) != night.n_lots:
        raise ValueError("there must be one item per lot")


# ---------------------------------------------------------------------- the working copy


class _Build:
    """A mutable working copy of a :class:`NightState` that lives for one call.

    The public functions copy the state into it, advance it and freeze the result, so the
    state that was passed in is never touched.
    """

    def __init__(
        self,
        config: NightConfig,
        deal: Deal,
        items: tuple[ItemInfo, ...],
        talk_offset: int,
    ) -> None:
        self.config = config
        self.deal = deal
        self.items = items
        self.talk_offset = talk_offset
        self.night = config.night
        self.seats = tuple(range(1, deal.n_seats + 1))
        self.bots = frozenset(seat.seat for seat in config.seats if seat.kind == "bot")
        self.stage: Stage = "plan"
        self.lot = 0
        self.events: list[AnyEvent] = []
        self.budgets = dict(deal.budgets)
        self.pending: Request | None = None
        self.order: tuple[int, ...] = ()
        self.talked = 0
        self.bidders = 0
        self.lot_step: LotStep | None = None
        self.records: list[LotRecord] = []
        self.guesses: list[GuessTruth] = []
        self.missions: tuple[MissionResultData, ...] = ()
        self.scores: tuple[SeatScore, ...] = ()
        self.n_requests = 0
        self.budget_aborted = False

    @classmethod
    def thaw(cls, state: NightState) -> "_Build":
        build = cls(state.config, state.deal, state.items, state.talk_offset)
        build.stage = state.stage
        build.lot = state.lot
        build.events = list(state.events)
        build.budgets = dict(state.budgets)
        build.pending = state.pending
        build.order = state.order
        build.talked = state.talked
        build.bidders = state.bidders
        build.lot_step = state.lot_step
        build.records = list(state.records)
        build.guesses = list(state.guesses)
        build.missions = state.missions
        build.scores = state.scores
        build.n_requests = state.n_requests
        build.budget_aborted = state.budget_aborted
        return build

    def freeze(self) -> NightState:
        return NightState(
            config=self.config,
            deal=self.deal,
            items=self.items,
            talk_offset=self.talk_offset,
            stage=self.stage,
            lot=self.lot,
            events=tuple(self.events),
            budgets=dict(self.budgets),
            pending=self.pending,
            order=self.order,
            talked=self.talked,
            bidders=self.bidders,
            lot_step=self.lot_step,
            records=tuple(self.records),
            guesses=tuple(self.guesses),
            missions=self.missions,
            scores=self.scores,
            n_requests=self.n_requests,
            budget_aborted=self.budget_aborted,
        )

    def note_answers(self, given: Mapping[int, Answer]) -> None:
        """Mark the night as aborted for the cost cap if one of the answers says so.

        This reads the answers and not the events they lead to: an English revision that is
        skipped becomes a ``kept_previous`` action, which has no reason to look for.
        """
        if any(answer.abstain == "budget_aborted" for answer in given.values()):
            self.budget_aborted = True

    # ------------------------------------------------------------------------- events

    def emit(self, cls: type[EventBase], vis: str, data: Any, lot: int | None = None) -> None:
        self.events.append(cls(seq=len(self.events) + 1, lot=lot, vis=vis, data=data))

    def opening(self) -> None:
        """``night_start``, the seed commitment if there is one, and every private card."""
        night, deal = self.night, self.deal
        low, high = budget_range(night)
        lots = [
            LotPlan(
                lot=lot.lot,
                item=info.item,
                name=info.name,
                emoji=info.emoji,
                tag=lot.tag,
                tier=lot.tier,
                format=lot.format,
                mechanism=lot.mechanism,
                reserve=lot.reserve,
                blurb=info.blurb,
            )
            for lot, info in zip(deal.lots, self.items, strict=True)
        ]
        seats = [
            SeatPublic(
                seat=seat.seat,
                codename=seat.codename,
                role="bot" if seat.kind == "bot" else "player",
            )
            for seat in sorted(self.config.seats, key=lambda seat: seat.seat)
        ]
        self.emit(
            NightStartEvent,
            "public",
            NightStartData(
                night_id=night.id,
                lang=night.lang,
                mode=night.mode,
                n_lots=night.n_lots,
                seats=seats,
                lots=lots,
                budget_lo=low,
                budget_hi=high,
                collectors=night.missions.collectors,
                shills=night.missions.shills,
                mission_bonus=night.missions.bonus,
            ),
        )
        commitment = night.deal_seed_commitment
        if commitment is not None and commitment.startswith("sha256:"):
            self.emit(
                SeedCommitEvent,
                "public",
                SeedCommitData(
                    phase="commit", commitment=commitment, deck_id=self.config.season.deck
                ),
            )
        for seat in self.seats:
            self.emit(DealPrivateEvent, seat_vis(seat), deal.card(seat))

    def action(
        self, seat: int, phase: Phase, answer: Answer | None, *, record: BidRecord | None = None
    ) -> ActionData:
        """The action event data of one seat in one phase. ``record`` is the engine's reading
        of a bid; the plan and talk phases have none."""
        extras: dict[str, Any] = {}
        if answer is not None:
            extras = {
                "thought": answer.thought,
                "sanitized_chars": answer.sanitized_chars,
                "attempts": answer.attempts,
                "call_id": answer.call_id,
            }
        if record is None:
            if answer is None or answer.abstain is not None:
                reason = answer.abstain if answer is not None else None
                return ActionData(
                    seat=seat, phase=phase, status="abstain", reason=reason or "missing", **extras
                )
            return ActionData(seat=seat, phase=phase, status="ok", **extras)
        reason = record.reason
        if record.status == "abstain" and answer is not None and answer.abstain is not None:
            reason = answer.abstain  # a caller that knows why replaces the engine's default
        return ActionData(
            seat=seat,
            phase=record.phase,
            status=record.status,
            reason=reason,
            value=record.value,
            raw_value=record.raw_value,
            **extras,
        )

    # ------------------------------------------------------------------------ requests

    def request(
        self,
        phase: Phase,
        seats: tuple[int, ...],
        facts: tuple[SeatFacts, ...],
        *,
        price: int | None = None,
        remaining: tuple[int, ...] = (),
    ) -> Request:
        lot = self.deal.lot(self.lot) if phase != "plan" else None
        index = self.n_requests
        self.n_requests += 1
        return Request(
            index=index,
            phase=phase,
            lot=lot.lot if lot is not None else None,
            seats=seats,
            facts=facts,
            n_lots=self.night.n_lots,
            n_seats=self.deal.n_seats,
            mode=self.night.mode,
            upcoming=tuple(
                Slot(item.lot, item.format, item.tier)
                for item in self.deal.lots[max(self.lot, 1) - 1 :]
            ),
            format=lot.format if lot is not None else None,
            tier=lot.tier if lot is not None else None,
            reserve=lot.reserve if lot is not None else 0,
            bidders=self.bidders if lot is not None else 0,
            blind=blind_bounds(lot.tier, self.night.blind)
            if lot is not None and lot.is_blind
            else None,
            price=price,
            remaining=remaining,
        )

    def facts(
        self,
        seat: int,
        *,
        exit_price: int | None = None,
        target: int | None = None,
    ) -> SeatFacts:
        lot = self.deal.lot(self.lot)
        own = lot.signals[seat] if lot.is_blind else lot.values[seat]
        return SeatFacts(
            seat=seat,
            budget_left=self.budgets[seat],
            value=None if lot.is_blind else own,
            signal=own if lot.is_blind else None,
            exit_price=exit_price,
            guess_target=target,
        )

    def plan_request(self) -> Request:
        seats = tuple(seat for seat in self.seats if seat not in self.bots)
        facts = tuple(SeatFacts(seat=seat, budget_left=self.budgets[seat]) for seat in seats)
        return self.request("plan", seats, facts)

    def talk_request(self) -> Request:
        speaker = self.order[self.talked]
        target = None
        if self.night.plan_and_mindreading and speaker not in self.bots:
            target = guess_target(speaker, self.lot, self.budgets)
        return self.request("talk", (speaker,), (self.facts(speaker, target=target),))

    def bid_request(self) -> Request:
        step = self.lot_step
        assert step is not None and step.phase is not None
        english = step.state if isinstance(step.state, EnglishState) else None
        revising = english is not None and step.phase == "english_revise"
        facts = tuple(
            self.facts(seat, exit_price=english.exits[seat] if revising and english else None)
            for seat in step.ask
        )
        return self.request(
            step.phase,
            step.ask,
            facts,
            price=english.price if revising and english else None,
            remaining=tuple(sorted(english.exits)) if revising and english else (),
        )

    # ----------------------------------------------------------------------- the phases

    def begin_lot(self, number: int) -> Request:
        """Open a lot: announce it, then ask the first speaker."""
        deal_lot = self.deal.lot(number)
        mech = self.night.mechanism_for(deal_lot.format)
        self.lot = number
        step = open_lot(
            mech,
            tier=deal_lot.tier,
            budgets=self.budgets,
            tie_key=deal_lot.tie_key,
            true_value=deal_lot.blind_value,
        )
        self.lot_step = step
        self.bidders = len(step.ask)
        self.order = talk_order(self.seats, number, self.talk_offset)
        self.talked = 0
        self.stage = "talk"
        self.emit(
            LotOpenEvent,
            "public",
            LotOpenData(
                format=deal_lot.format,
                item=self.items[number - 1].item,
                mechanism=deal_lot.mechanism,
                tier=deal_lot.tier,
                reserve=deal_lot.reserve,
                active=list(step.ask),
                sitting_out=[seat for seat in self.seats if seat not in step.ask],
                talk_order=list(self.order),
            ),
            number,
        )
        return self.talk_request()

    def on_plan(self, request: Request, given: Mapping[int, Answer]) -> Request:
        for seat in request.seats:
            answer = given.get(seat)
            if answer is not None and answer.abstain is None:
                assert answer.priorities is not None and answer.budget_plan is not None
                self.emit(
                    PlanEvent,
                    seat_vis(seat),
                    PlanData(
                        seat=seat,
                        priorities=list(answer.priorities),
                        budget_plan=list(answer.budget_plan),
                        persona=answer.persona,
                        sanitized_chars=answer.text_sanitized_chars,
                    ),
                )
            self.emit(ActionEvent, seat_vis(seat), self.action(seat, "plan", answer))
        return self.begin_lot(1)

    def on_talk(self, request: Request, given: Mapping[int, Answer]) -> Request | None:
        seat = request.seats[0]
        target = request.facts[0].guess_target
        answer = given.get(seat)
        spoke = answer is not None and answer.abstain is None
        self.emit(
            TalkEvent,
            "public",
            TalkData(
                seat=seat,
                text=answer.say if answer is not None and spoke else "",
                pledge=answer.pledge if answer is not None and spoke else None,
                sanitized_chars=answer.text_sanitized_chars if answer is not None and spoke else 0,
            ),
            self.lot,
        )
        if answer is not None and spoke and target is not None and answer.guess is not None:
            self.emit(
                GuessEvent,
                seat_vis(seat),
                GuessData(seat=seat, target=target, guess=answer.guess),
                self.lot,
            )
            self.guesses.append(
                GuessTruth(self.lot, seat, target, answer.guess, truth=self.budgets[target])
            )
        self.emit(ActionEvent, seat_vis(seat), self.action(seat, "talk", answer), self.lot)
        self.talked += 1
        if self.talked < len(self.order):
            return self.talk_request()
        self.stage = "bid"
        step = self.lot_step
        assert step is not None
        return self.close_lot(step) if step.done else self.bid_request()

    def on_bid(self, request: Request, given: Mapping[int, Answer]) -> Request | None:
        step = self.lot_step
        assert step is not None
        raw = {seat: answer.value for seat, answer in given.items() if answer.abstain is None}
        new = step_lot(step, raw)
        for record in new.records:
            data = self.action(record.seat, record.phase, given.get(record.seat), record=record)
            self.emit(ActionEvent, seat_vis(record.seat), data, self.lot)
        for dropout in new.dropouts:
            self.emit(EnglishDropoutEvent, "public", dropout, self.lot)
        self.lot_step = new
        return self.close_lot(new) if new.done else self.bid_request()

    def close_lot(self, step: LotStep) -> Request | None:
        """Announce the result, charge the payments, then open the next lot or end the night."""
        outcome = step.outcome
        assert outcome is not None
        if outcome.dutch is not None:
            self.emit(DutchResultEvent, "public", outcome.dutch, self.lot)
        self.emit(HammerEvent, "public", outcome.hammer, self.lot)
        self.records.append(LotRecord(self.lot, dict(self.budgets), outcome))
        for seat, paid in outcome.hammer.payments.items():
            self.budgets[seat] -= paid
        self.lot_step = None
        if self.lot < self.night.n_lots:
            return self.begin_lot(self.lot + 1)
        self.finish()
        return None

    def finish(self) -> None:
        """Decide the missions, write the scores and close the night."""
        deal = self.deal
        hammers = [record.outcome.hammer for record in self.records]
        missions = evaluate_missions(deal.missions, lot_results(deal.lots, hammers))
        for mission in missions:
            self.emit(MissionResultEvent, "public", mission)
        scores = seat_scores(
            tally_night(deal.lots, hammers, self.seats),
            {m.seat: m.bonus for m in missions},
            [seat for seat in self.seats if seat not in self.bots],
        )
        self.emit(
            NightEndEvent,
            "public",
            NightEndData(
                status="budget_aborted" if self.budget_aborted else "completed", scores=scores
            ),
        )
        self.stage = "done"
        self.missions = tuple(missions)
        self.scores = tuple(scores)


# ----------------------------------------------------------------------------- the interface


def start(
    config: NightConfig,
    deal: Deal,
    *,
    items: Sequence[ItemInfo] | None = None,
    talk_offset: int = 0,
) -> tuple[NightState, Request]:
    """Open the night described by ``config`` and dealt as ``deal``.

    Returns the state and the first request: the opening plan of the model seats, or, with
    ``plan_and_mindreading`` off or no model seat, the first speaker of lot 1. The opening
    events (``night_start``, the commitment to the seed if the configuration has one, and a
    ``deal_private`` event for every seat, bots included) are in ``state.events``.

    ``items`` names the lots (one per lot, in lot order); without it the lots get plain
    stand-in names. ``talk_offset`` moves the start of the speaking order: a driver that wants
    a seeded start draws it from its own seed.
    """
    _check_inputs(config, deal, items)
    night = config.night
    named = tuple(items) if items is not None else placeholder_items(night.n_lots)
    build = _Build(config, deal, named, talk_offset % deal.n_seats)
    build.opening()
    has_models = any(seat not in build.bots for seat in build.seats)
    if night.plan_and_mindreading and has_models:
        request = build.plan_request()
    else:
        request = build.begin_lot(1)
    build.pending = request
    return build.freeze(), request


def append_events(
    state: NightState, events: Iterable[AnyEvent]
) -> tuple[NightState, tuple[AnyEvent, ...]]:
    """Add events from outside the engine to the log, numbered after its last event.

    Only ``system`` and ``audience`` events are accepted, because every other kind is written
    by the engine alone. The events keep their content and are given the next sequence numbers
    in the order they come; the numbered copies are returned with the new state. The pending
    request, if any, is unchanged, so this can be called between any two steps.
    """
    added: list[AnyEvent] = []
    for event in events:
        if vis_kind(event.vis) not in ("system", "audience"):
            raise ValueError(f"a {event.type} event is written by the engine, not added to it")
        added.append(event.model_copy(update={"seq": len(state.events) + len(added) + 1}))
    if not added:
        return state, ()
    return replace(state, events=state.events + tuple(added)), tuple(added)


def step(
    state: NightState, answers: Mapping[int, Answer]
) -> tuple[NightState, tuple[AnyEvent, ...], Request | None]:
    """Take the answers to the pending request and move the night on.

    ``answers`` maps a seat to its :class:`Answer`. A seat that was asked and is missing counts
    as an abstention (``missing``); answering a seat that was not asked is an error. An
    abstention with the reason ``budget_aborted`` marks the night (see "The status of the
    night" in the module text). Returns the new state, the events this call added (the end of
    ``state.events``) and the next request, which is None once the night is over.
    """
    request = state.pending
    if request is None:
        raise ValueError("the night is over")
    given = _check_answers(request, answers, state.config.night)
    build = _Build.thaw(state)
    build.note_answers(given)
    if request.phase == "plan":
        nxt: Request | None = build.on_plan(request, given)
    elif request.phase == "talk":
        nxt = build.on_talk(request, given)
    else:
        nxt = build.on_bid(request, given)
    build.pending = nxt
    return build.freeze(), tuple(build.events[len(state.events) :]), nxt
