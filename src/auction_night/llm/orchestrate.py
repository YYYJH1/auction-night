"""Running the model calls of a night: phases, retries, timeouts, the cassette and the cost gate.

The night state machine asks for *phases*: a list of seats that must act now. This module answers
a phase. It sends one request per seat, concurrently under per-endpoint limits, reads the replies
with the parsing ladder, and hands back one :class:`SeatOutcome` per seat, sorted by seat number
whatever the order in which the replies arrived. The sequential talk phase is run one seat after
the other, because a later speaker reads what the earlier ones said.

The night driver
----------------
The state machine is not imported here. It plugs in by implementing :class:`NightDriver`::

    class NightDriver(Protocol):
        night: str
        def next_phase(self) -> PhaseRequest | None: ...     # None: the night is over
        def submit(self, result: PhaseResult) -> None: ...

``next_phase`` renders the prompts of the seats that are asked, builds their requests
(:func:`~auction_night.llm.request.build_request`) and returns them as :class:`ModelCall` objects.
Seats that no model plays (bots, mocks, a human) are the driver's own business. ``submit`` turns
every :class:`SeatOutcome` into an answer: ``args`` is the validated ``act`` call of an ``ok``
outcome, ``reason`` the abstention reason otherwise; ``action_attempts``, ``call_id``,
``sanitized_chars`` and ``text_sanitized_chars`` are the numbers an action event carries. The
``events`` of the result are system events (``parse_repair`` and ``cost_event``) in a fixed order
for the driver to number and append to its log (:meth:`SystemEvent.to_event`).

What happens to one call
------------------------
Every request that is sent is an *attempt*, and every attempt is a cassette entry:

* a reply that reads well ends the call (outcome ``ok``);
* a reply that cannot be read is retried up to ``limits.syntax_retries`` times, with a follow-up
  message that names the violations; the model sees its own earlier reply and the follow-up
  appended to the same request, so the cached prefix stays untouched (outcome ``parse_failed``);
* a rate limit, a server error or a lost connection is retried up to
  ``limits.transient_retries`` times after an exponential back-off (outcome ``error``);
* a request the API refuses (the 400 family) is not retried (outcome ``error``);
* a timeout is not retried: a model that took the whole call timeout would only be late again
  (outcome ``timeout``);
* a refusal, whether the API reports it or the reply reads like one, is never retried (outcome
  ``refusal``).

The call that fails in the end is an abstention: the reason is ``timeout``, ``refusal``,
``parse_failed``, or ``missing`` when nothing readable ever came back. Every attempt that had
something to report (a repair, a failure) also leaves a ``parse_repair`` event whose ``status``
says how the attempt ended: ``ok``, ``retry`` or ``abstain``.

Attempt numbers count all attempts of a call, retries of both kinds, so a call has at most
``1 + syntax_retries + transient_retries`` attempts, and never more than nine.

Determinism
-----------
Replies arrive in any order, yet nothing the night produces depends on it. Outcomes are sorted by
seat; system events and cassette batches are sorted by call key; the costs of a phase are added in
key order and the cost gate is checked between phases (and between the calls of a sequential
phase), never inside a concurrent phase. A replay, which reads every attempt from the cassette
instead of calling a model, therefore yields the same events byte for byte.
"""

import asyncio
import copy
import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from typing import Any, Final, Literal, Protocol

from auction_night.contracts.config import (
    EndpointConfig,
    EndpointKind,
    LimitsConfig,
    NightConfig,
    StructuredOutput,
)
from auction_night.contracts.events import (
    AbstainReason,
    CostEvent,
    CostEventData,
    NightStatus,
    ParseRepairData,
    ParseRepairEvent,
    RepairCode,
    RepairStatus,
)
from auction_night.llm.accounting import Charge, Ledger, PriceTable
from auction_night.llm.cassette import (
    MAX_ATTEMPTS,
    CallKey,
    Cassette,
    CassetteEntry,
    Outcome,
    stored_request,
)
from auction_night.llm.client import (
    CallFailure,
    ClientReply,
    LlmClient,
    SdkFactory,
    Usage,
    classify_exception,
    make_clients,
)
from auction_night.llm.counters import CounterBook, repair_data
from auction_night.llm.parse import ParseContext, ParseResult, RawReply, parse_reply
from auction_night.llm.request import BuiltRequest, request_hash

MAX_ECHO_CHARS: Final = 2000


# ---------------------------------------------------------------------------- settings


@dataclass(frozen=True)
class RunnerSettings:
    """Timeouts and retries of a night. ``from_limits`` reads them from the configuration.

    ``timeout_s`` is the call timeout. It is handed to the SDK, and a wall-clock check
    ``wall_clock_slack_s`` later catches a call that the SDK failed to stop. The back-off before
    transient retry ``n`` is ``min(backoff_cap_s, backoff_base_s * 2 ** (n - 1))`` (or the
    server's ``retry-after`` if that is longer), plus up to a quarter of it as a jitter that is
    derived from the call, so it is the same on every run.
    """

    timeout_s: float = 90.0
    syntax_retries: int = 1
    transient_retries: int = 2
    backoff_base_s: float = 1.0
    backoff_cap_s: float = 30.0
    wall_clock_slack_s: float = 5.0

    def __post_init__(self) -> None:
        if self.timeout_s <= 0 or self.syntax_retries < 0 or self.transient_retries < 0:
            raise ValueError("timeouts must be positive and retries cannot be negative")
        if 1 + self.syntax_retries + self.transient_retries > MAX_ATTEMPTS:
            raise ValueError(f"a call may have at most {MAX_ATTEMPTS} attempts")

    @classmethod
    def from_limits(cls, limits: LimitsConfig, **overrides: Any) -> "RunnerSettings":
        return cls(
            timeout_s=float(limits.call_timeout_s),
            syntax_retries=limits.syntax_retries,
            transient_retries=limits.transient_retries,
            **overrides,
        )


# ------------------------------------------------------------------------ the requests


@dataclass(frozen=True)
class ModelCall:
    """One model action to carry out.

    ``key`` identifies the call (its ``attempt`` is ignored: the runner numbers the attempts).
    ``endpoint`` is the name of the endpoint in the configuration, which selects the client and
    the limiter. ``request`` is the built request, ``ctx`` the context of the parsing ladder
    and ``native`` the structured-output mode of the endpoint, which decides what counts as a
    syntax repair. ``role`` labels the caller in events and counters (``seat:N`` by default).
    """

    key: CallKey
    endpoint: str
    request: BuiltRequest
    ctx: ParseContext
    native: StructuredOutput = "tools"
    role: str = ""

    def __post_init__(self) -> None:
        if not self.role:
            seat = self.key.seat
            object.__setattr__(self, "role", f"seat:{seat}" if seat is not None else "role")

    @property
    def seat(self) -> int | None:
        return self.key.seat


@dataclass(frozen=True)
class PhaseRequest:
    """The model calls of one phase.

    With ``sequential`` the calls run one after another in the given order (the talk phase),
    else all at once. Each seat appears at most once.
    """

    calls: tuple[ModelCall, ...]
    sequential: bool = False


@dataclass(frozen=True)
class SystemEvent:
    """A system event without its sequence number, which the driver assigns."""

    type: Literal["parse_repair", "cost_event"]
    lot: int | None
    data: ParseRepairData | CostEventData

    def to_event(self, seq: int) -> ParseRepairEvent | CostEvent:
        """The event of the log: visibility ``system``, numbered ``seq``."""
        if isinstance(self.data, ParseRepairData):
            return ParseRepairEvent(seq=seq, lot=self.lot, vis="system", data=self.data)
        return CostEvent(seq=seq, lot=self.lot, vis="system", data=self.data)


@dataclass(frozen=True)
class SeatOutcome:
    """What one model call came to.

    ``status`` is ``ok`` (``args`` holds the validated ``act`` call, with the text fields
    cleaned) or ``abstain`` (``reason`` says why). ``attempts`` is the number of requests that
    were sent: zero for an action skipped by the cost gate. ``action_attempts`` is at least one,
    as an action event requires. ``cost_microusd`` is None when no attempt could be priced.
    """

    seat: int | None
    role: str
    key: CallKey
    status: Literal["ok", "abstain"]
    args: Mapping[str, Any] | None
    reason: AbstainReason | None
    attempts: int
    result: ParseResult | None = None
    usage: Usage = field(default_factory=Usage)
    cost_microusd: int | None = None
    model: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def call_id(self) -> str:
        return self.key.call_id

    @property
    def action_attempts(self) -> int:
        return max(1, self.attempts)

    @property
    def thought(self) -> str:
        """The cleaned inner monologue of an ``ok`` call, else an empty string."""
        return str(self.args.get("thought") or "") if self.args else ""

    @property
    def sanitized_chars(self) -> int:
        """Characters of model text that cleaning removed or cut, over the whole reply."""
        return self.result.sanitized_chars if self.ok and self.result else 0

    @property
    def text_sanitized_chars(self) -> int:
        """The same for the public texts only: the chat line and the persona."""
        if not (self.ok and self.result):
            return 0
        return self.result.sanitized_in("say") + self.result.sanitized_in("plan_persona")


@dataclass(frozen=True)
class PhaseResult:
    """The answer to a phase. ``cap_reached`` is True once the cost gate has fired."""

    outcomes: tuple[SeatOutcome, ...]
    events: tuple[SystemEvent, ...]
    cap_reached: bool = False

    def outcome_of(self, seat: int) -> SeatOutcome:
        for outcome in self.outcomes:
            if outcome.seat == seat:
                return outcome
        raise KeyError(seat)


@dataclass(frozen=True)
class NightRun:
    """A finished night: status, every system event, and the totals."""

    night: str
    status: NightStatus
    events: tuple[SystemEvent, ...]
    spent_microusd: int
    calls: int
    summary: Mapping[str, Any]
    counters: Mapping[str, Any]


class NightDriver(Protocol):
    """What the runner needs from the night state machine."""

    night: str

    def next_phase(self) -> PhaseRequest | None:
        """The model calls of the next phase, or None when the night is over."""

    def submit(self, result: PhaseResult) -> None:
        """Take the answers of the phase that :meth:`next_phase` returned last."""


# --------------------------------------------------------------------------- the session


def models_in_use(config: NightConfig) -> list[str]:
    """Model ids that a seat or a show role of ``config`` calls, sorted."""
    show = config.show
    roles = [show.host_offline, *show.judges, show.content_scan.classifier]
    models = {s.model for s in config.seats if s.kind == "llm" and s.model}
    models |= {r.model for r in roles if r and r.kind == "llm" and r.model}
    return sorted(models)


@dataclass
class NightSession:
    """The state of one night in the model layer: cassette, ledger, counters and limits.

    Create it with :meth:`open` from the configuration, or directly (tests do).
    """

    night: str
    cassette: Cassette
    ledger: Ledger
    settings: RunnerSettings
    endpoints: Mapping[str, EndpointConfig]
    counters: CounterBook = field(default_factory=CounterBook)

    @property
    def replaying(self) -> bool:
        return self.cassette.mode == "replay"

    @classmethod
    def open(
        cls,
        config: NightConfig,
        cassette: Cassette,
        *,
        prices: PriceTable | None = None,
        settings: RunnerSettings | None = None,
    ) -> "NightSession":
        """A session for the night of ``config``.

        ``prices`` defaults to the list that ``limits.prices_file`` names (read relative to the
        working directory), or to an empty list. A recording of a night with a cost cap is
        refused when one of its models has no price. A replay needs no prices at all: the cost
        of every attempt comes from the recording.
        """
        limits = config.limits
        recording = cassette.mode == "record"
        if prices is None:
            use_file = recording and limits.prices_file
            prices = PriceTable.from_file(limits.prices_file) if use_file else PriceTable()
        ledger = Ledger(prices, cap_microusd=limits.max_cost_microusd)
        if recording:
            ledger.require_prices(models_in_use(config))
        return cls(
            night=config.night.id,
            cassette=cassette,
            ledger=ledger,
            settings=settings or RunnerSettings.from_limits(limits),
            endpoints=dict(config.endpoints),
        )


# ------------------------------------------------------------------------------ limits


class EndpointLimiter:
    """At most ``max_concurrency`` calls in flight, and at most ``requests_per_minute`` starts.

    The rate limit spaces the starts ``60 / requests_per_minute`` seconds apart. Use it as an
    async context manager around a call.
    """

    def __init__(
        self,
        max_concurrency: int,
        requests_per_minute: int | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._slots = asyncio.Semaphore(max_concurrency)
        self._interval = 0.0 if requests_per_minute is None else 60.0 / requests_per_minute
        self._next_start = 0.0
        self._clock = clock
        self._sleep = sleep
        self.in_flight = 0
        self.peak = 0
        self.started = 0

    @property
    def interval_s(self) -> float:
        """The least time between two starts (zero without a rate limit)."""
        return self._interval

    async def __aenter__(self) -> "EndpointLimiter":
        if self._interval:
            now = self._clock()
            start = max(now, self._next_start)
            self._next_start = start + self._interval
            if start > now:
                await self._sleep(start - now)
        await self._slots.acquire()
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        self.started += 1
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.in_flight -= 1
        self._slots.release()


# ------------------------------------------------------------------------- the helpers


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _assistant_text(reply: RawReply) -> str:
    """The earlier reply as the text of an assistant turn (never empty)."""
    text = reply.text.strip()
    if not text:
        for call in reply.tool_calls:
            arguments = call.arguments
            if isinstance(arguments, str):
                text = arguments.strip()
            elif isinstance(arguments, Mapping):
                text = json.dumps(arguments, ensure_ascii=False)
            if text:
                break
    return (text or "(empty reply)")[:MAX_ECHO_CHARS]


def follow_up_request(built: BuiltRequest, assistant_text: str, user_text: str) -> BuiltRequest:
    """The request plus one more exchange: the model's earlier reply and a follow-up message.

    Everything before the new messages is copied unchanged, so for Anthropic the cached prefix
    (tools, system) stays the same bytes. The request hash is recomputed.
    """
    payload = copy.deepcopy(built.payload)
    payload["messages"] = [
        *payload["messages"],
        {"role": "assistant", "content": assistant_text},
        {"role": "user", "content": user_text},
    ]
    digest = request_hash(
        payload,
        kind=built.kind,
        api_version=built.api_version,
        thinking=built.thinking,
        effort=built.effort,
    )
    return replace(built, payload=payload, request_hash=digest)


def parsed_summary(result: ParseResult) -> dict[str, Any]:
    """What the cassette keeps of a parse result: plain JSON, no floats."""
    return {
        "status": result.status,
        "attempt": result.attempt,
        "reason": result.reason,
        "source": result.trace.source,
        "args": None if result.args is None else dict(result.args),
        "errors": [{"field": e.field, "code": e.code} for e in result.errors],
        "adjustments": [
            {"kind": a.kind, "field": a.field, "raw": a.raw, "effective": a.effective}
            for a in result.adjustments
        ],
        "sanitized_chars": result.sanitized_chars,
        "retry_message": result.retry_message,
    }


def _backoff_s(settings: RunnerSettings, failure: CallFailure, retry: int, call_id: str) -> float:
    delay = min(settings.backoff_cap_s, settings.backoff_base_s * 2 ** (retry - 1))
    if failure.retry_after_s is not None:
        delay = min(settings.backoff_cap_s, max(delay, failure.retry_after_s))
    jitter = int(hashlib.sha256(call_id.encode()).hexdigest()[:8], 16) % 1000 / 1000
    return delay * (1 + 0.25 * jitter)


@dataclass
class _Attempt:
    reply: ClientReply | None
    failure: CallFailure | None
    cost: int | None
    started_at: str = ""
    finished_at: str = ""


@dataclass
class _Action:
    outcome: SeatOutcome
    entries: list[CassetteEntry] = field(default_factory=list)
    events: list[tuple[CallKey, SystemEvent]] = field(default_factory=list)
    charges: list[Charge] = field(default_factory=list)


# ------------------------------------------------------------------------- the runner


class ModelRunner:
    """Carries out the model calls of nights.

    ``clients`` maps an endpoint name to a client (:class:`~auction_night.llm.client.LlmClient`);
    it may be empty or None when every night is replayed. One runner can serve many nights at
    once: the per-endpoint limiters are shared by all of them, as the real rate limits are.
    ``clock`` gives the timestamps of cassette entries and ``sleep`` the back-off, both
    replaceable for tests.
    """

    def __init__(
        self,
        clients: Mapping[str, LlmClient] | None = None,
        *,
        max_threads: int = 64,
        clock: Callable[[], str] = _utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clients: dict[str, LlmClient] = dict(clients or {})
        self._limiters: dict[str, EndpointLimiter] = {}
        self._clock = clock
        self._sleep = sleep
        self._monotonic = monotonic
        self._max_threads = max_threads
        self._pool: ThreadPoolExecutor | None = None

    @classmethod
    def from_config(
        cls,
        config: NightConfig,
        *,
        environ: Mapping[str, str] | None = None,
        factories: Mapping[EndpointKind, SdkFactory] | None = None,
        http_client: Any = None,
        endpoints: Iterable[str] | None = None,
        **options: Any,
    ) -> "ModelRunner":
        """A runner with a client for every endpoint of ``config`` that a model will call.

        Keys and URLs are read from the environment variables the configuration names
        (:func:`~auction_night.llm.client.make_clients`); ``endpoints`` restricts the clients to
        some of them. Other options go to the constructor.
        """
        clients = make_clients(
            config, environ=environ, factories=factories, http_client=http_client, names=endpoints
        )
        return cls(clients, **options)

    # ---------------------------------------------------------------------- lifecycle

    def close(self) -> None:
        """Stop the worker threads. Calls still in flight are abandoned."""
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    async def __aenter__(self) -> "ModelRunner":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.close()

    def _executor(self) -> ThreadPoolExecutor:
        if self._pool is None:
            self._pool = ThreadPoolExecutor(self._max_threads, thread_name_prefix="model-call")
        return self._pool

    def limiter(self, name: str, endpoint: EndpointConfig) -> EndpointLimiter:
        """The limiter of an endpoint, created on first use and shared by all nights."""
        found = self._limiters.get(name)
        if found is None:
            found = EndpointLimiter(
                endpoint.max_concurrency,
                endpoint.requests_per_minute,
                clock=self._monotonic,
                sleep=self._sleep,
            )
            self._limiters[name] = found
        return found

    # ------------------------------------------------------------------------- phases

    async def run_phase(self, session: NightSession, phase: PhaseRequest) -> PhaseResult:
        """Answer one phase: see the module docstring."""
        calls = list(phase.calls)
        self._check_phase(session, calls)
        ledger = session.ledger
        lot = calls[0].key.lot if calls else None
        events: list[SystemEvent] = []
        outcomes: list[SeatOutcome] = []

        def gate() -> None:
            fired = ledger.gate()
            if fired is not None:
                events.append(SystemEvent("cost_event", lot, fired))

        gate()
        if phase.sequential:
            for call in calls:
                if ledger.blocked:
                    outcomes.append(self._skipped(session, call))
                    continue
                action = await self._run_action(session, call)
                events.extend(self._settle(session, [action]))
                outcomes.append(action.outcome)
                gate()
        elif ledger.blocked:
            outcomes = [self._skipped(session, call) for call in calls]
        else:
            actions = await _gather([self._run_action(session, call) for call in calls])
            events.extend(self._settle(session, actions))
            outcomes = [action.outcome for action in actions]
            gate()
        outcomes.sort(key=lambda o: (o.seat is None, o.seat or 0))
        return PhaseResult(tuple(outcomes), tuple(events), ledger.blocked)

    @staticmethod
    def _check_phase(session: NightSession, calls: Sequence[ModelCall]) -> None:
        seen = set()
        for call in calls:
            if call.key.night != session.night:
                raise ValueError(f"{call.key}: the call belongs to another night")
            if call.key.seat in seen and call.key.seat is not None:
                raise ValueError(f"seat {call.key.seat} appears twice in one phase")
            seen.add(call.key.seat)
            if not session.replaying and call.endpoint not in session.endpoints:
                raise KeyError(f"unknown endpoint {call.endpoint!r}")

    def _skipped(self, session: NightSession, call: ModelCall) -> SeatOutcome:
        """An action that the cost gate turned into an abstention. No request is sent."""
        session.ledger.note_aborted()
        session.counters.get(call.role).record_abstention("budget_aborted")
        return SeatOutcome(
            seat=call.seat,
            role=call.role,
            key=call.key.with_attempt(1),
            status="abstain",
            args=None,
            reason="budget_aborted",
            attempts=0,
        )

    def _settle(self, session: NightSession, actions: Sequence[_Action]) -> list[SystemEvent]:
        """Commit the attempts of finished actions and add their costs, in key order."""
        entries = [entry for action in actions for entry in action.entries]
        if entries and not session.replaying:
            session.cassette.commit(entries)
        session.ledger.fold(charge for action in actions for charge in action.charges)
        keyed = [pair for action in actions for pair in action.events]
        return [event for _, event in sorted(keyed, key=lambda pair: pair[0].sort_key)]

    # ---------------------------------------------------------------------- one action

    async def _run_action(self, session: NightSession, call: ModelCall) -> _Action:
        """Carry out one call: send, read, retry, and end in an answer or an abstention."""
        settings = session.settings
        request = call.request
        syntax_left, transient_left = settings.syntax_retries, settings.transient_retries
        entries: list[CassetteEntry] = []
        events: list[tuple[CallKey, SystemEvent]] = []
        charges: list[Charge] = []
        costs: list[int | None] = []
        usage = Usage()
        returned: str | None = None
        reason: AbstainReason | None = None
        result: ParseResult | None = None
        attempts = 0

        while True:
            attempts += 1
            key = call.key.with_attempt(attempts)
            attempt = await self._attempt(session, call, request, key)
            reply, failure = attempt.reply, attempt.failure
            costs.append(attempt.cost)
            result = None
            repair: ParseRepairData | None = None
            retry_request: BuiltRequest | None = None
            wait_s: float | None = None
            outcome: Outcome

            if failure is not None:
                outcome = failure.kind
                what = f"error={failure.error_type}"
                if failure.status is not None:
                    what += f"; http={failure.status}"
                if failure.kind == "error" and failure.retryable and transient_left > 0:
                    transient_left -= 1
                    used = settings.transient_retries - transient_left
                    wait_s = _backoff_s(settings, failure, used, key.call_id)
                    codes: list[RepairCode] = ["retry"]
                    status: RepairStatus = "retry"
                    detail = what
                else:
                    reason = "timeout" if failure.kind == "timeout" else "missing"
                    codes = ["timeout", "fallback"] if failure.kind == "timeout" else ["fallback"]
                    status = "abstain"
                    detail = f"reason={reason}; {what}"
                repair = ParseRepairData(
                    role=call.role,
                    seat=call.seat,
                    phase=call.key.phase,
                    attempt=attempts,
                    codes=codes,
                    status=status,
                    detail=detail,
                    call_id=call.key.call_id,
                )
            else:
                assert reply is not None
                usage = usage + (reply.usage or Usage())
                returned = reply.model or returned
                result = parse_reply(
                    reply.reply, call.ctx, attempt=attempts, max_attempts=attempts + syntax_left
                )
                session.counters.record(call.role, result, call.native)
                if result.status == "ok":
                    outcome = "ok"
                elif result.status == "retry":
                    outcome = "parse_failed"
                    syntax_left -= 1
                    assert result.retry_message is not None
                    retry_request = follow_up_request(
                        request, _assistant_text(reply.reply), result.retry_message
                    )
                else:
                    outcome = "refusal" if result.reason == "refusal" else "parse_failed"
                    reason = result.reason
                repair = repair_data(
                    result, call.native, role=call.role, seat=call.seat, call_id=call.key.call_id
                )

            if repair is not None:
                events.append((key, SystemEvent("parse_repair", call.key.lot, repair)))
            charges.append(Charge(key, call.role, reply.usage if reply else Usage(), attempt.cost))
            if not session.replaying:
                entries.append(
                    self._entry(session, call, key, request, attempt, outcome, result, repair)
                )

            if retry_request is not None:
                request = retry_request
            elif wait_s is not None:
                if not session.replaying:
                    await self._sleep(wait_s)
            else:
                break

        if result is None and reason is not None:
            session.counters.get(call.role).record_abstention(reason)
        ok = result is not None and result.status == "ok"
        priced = [c for c in costs if c is not None]
        outcome_of_call = SeatOutcome(
            seat=call.seat,
            role=call.role,
            key=call.key.with_attempt(1),
            status="ok" if ok else "abstain",
            args=result.args if ok and result is not None else None,
            reason=None if ok else reason,
            attempts=attempts,
            result=result,
            usage=usage,
            cost_microusd=sum(priced) if priced else None,
            model=returned,
        )
        return _Action(outcome_of_call, entries, events, charges)

    def _entry(
        self,
        session: NightSession,
        call: ModelCall,
        key: CallKey,
        request: BuiltRequest,
        attempt: _Attempt,
        outcome: Outcome,
        result: ParseResult | None,
        repair: ParseRepairData | None,
    ) -> CassetteEntry:
        reply = attempt.reply
        return CassetteEntry(
            key=key,
            role=call.role,
            kind=request.kind,
            model=request.model,
            request_hash=request.request_hash,
            request=stored_request(request.payload, session.cassette.scrubber),
            outcome=outcome,
            model_returned=reply.model if reply else None,
            stop_reason=reply.stop_reason if reply else None,
            refusal=reply.refusal if reply else False,
            response=reply.response if reply else None,
            error=attempt.failure.as_dict() if attempt.failure else None,
            parsed=parsed_summary(result) if result else None,
            repair=tuple(repair.codes) if repair else (),
            usage=reply.usage.as_dict() if reply and reply.usage else None,
            cost_microusd=attempt.cost,
            latency_ms=reply.latency_ms if reply else None,
            started_at=attempt.started_at,
            finished_at=attempt.finished_at,
        )

    # ------------------------------------------------------------------------ attempts

    async def _attempt(
        self, session: NightSession, call: ModelCall, request: BuiltRequest, key: CallKey
    ) -> _Attempt:
        if session.replaying:
            return self._replayed(session, request, key)
        return await self._live(session, call, request)

    @staticmethod
    def _replayed(session: NightSession, request: BuiltRequest, key: CallKey) -> _Attempt:
        entry = session.cassette.lookup(key, request.request_hash, request=request.payload)
        failure = entry.failure
        if failure is not None:
            return _Attempt(None, failure, 0)
        return _Attempt(entry.client_reply(), None, entry.cost_microusd)

    async def _live(
        self, session: NightSession, call: ModelCall, request: BuiltRequest
    ) -> _Attempt:
        client = self._clients.get(call.endpoint)
        if client is None:
            raise KeyError(f"no client for endpoint {call.endpoint!r}")
        settings = session.settings
        limiter = self.limiter(call.endpoint, session.endpoints[call.endpoint])
        loop = asyncio.get_running_loop()
        started_at = self._clock()
        reply: ClientReply | None = None
        failure: CallFailure | None = None
        async with limiter:
            try:
                work = loop.run_in_executor(
                    self._executor(),
                    partial(client.complete, request, timeout_s=settings.timeout_s),
                )
                reply = await asyncio.wait_for(
                    work, timeout=settings.timeout_s + settings.wall_clock_slack_s
                )
            except Exception as exc:  # the SDK's own exceptions are classified, not raised
                failure = classify_exception(exc)
        cost: int | None = 0  # a call that returned nothing is not charged
        if reply is not None:
            cost = session.ledger.cost_of(reply.usage, reply.model, request.model)
        return _Attempt(reply, failure, cost, started_at, self._clock())

    # --------------------------------------------------------------------------- nights

    async def run_night(self, driver: NightDriver, session: NightSession) -> NightRun:
        """Play a night through: ask the driver for phases and answer them until none is left."""
        events: list[SystemEvent] = []
        while (phase := driver.next_phase()) is not None:
            result = await self.run_phase(session, phase)
            driver.submit(result)
            events.extend(result.events)
        ledger = session.ledger
        events.append(SystemEvent("cost_event", None, ledger.tally_event()))
        return NightRun(
            night=session.night,
            status=ledger.status,
            events=tuple(events),
            spent_microusd=ledger.spent_microusd,
            calls=ledger.calls,
            summary=ledger.summary(),
            counters=session.counters.to_dict(),
        )

    async def run_nights(
        self, jobs: Sequence[tuple[NightDriver, NightSession]], *, max_parallel: int = 4
    ) -> list[NightRun | BaseException]:
        """Play several nights at once, at most ``max_parallel`` of them at a time.

        Results are in the order of ``jobs``. A night that fails leaves its exception in the
        list and does not stop the others.
        """
        if max_parallel < 1:
            raise ValueError("max_parallel must be at least 1")
        turn = asyncio.Semaphore(max_parallel)

        async def one(driver: NightDriver, session: NightSession) -> NightRun:
            async with turn:
                return await self.run_night(driver, session)

        return await asyncio.gather(*(one(d, s) for d, s in jobs), return_exceptions=True)


async def _gather(coroutines: Sequence[Coroutine[Any, Any, _Action]]) -> list[_Action]:
    """Run coroutines at once. The first failure, in call order, cancels the rest and is raised."""
    tasks: list[asyncio.Task[_Action]] = []
    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(c) for c in coroutines]
    except ExceptionGroup as grouped:
        for task in tasks:
            if task.done() and not task.cancelled() and task.exception() is not None:
                raise task.exception() from None  # type: ignore[misc]
        raise grouped.exceptions[0] from None
    return [task.result() for task in tasks]
