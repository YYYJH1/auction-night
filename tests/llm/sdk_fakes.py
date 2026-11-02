"""Fakes and builders for the tests of the API clients, the cassette and the orchestrator.

No network is ever touched.

* ``anthropic_message`` and ``openai_completion`` build replies with the SDKs' own response
  types, so the wrappers under test read real objects.
* ``ScriptedSdk`` stands in for both SDK clients. It checks every call against the real method
  signature, plays a script per seat (success, malformed output, refusal, timeout, rate limit,
  server error, ...) and raises the SDKs' typed exceptions.
* ``FakeNight`` is a small night driver that builds a real event log from the outcomes, so a
  recorded night and its replay can be compared byte for byte.
* Keys are assembled from pieces, so that this file does not look like it holds one.
"""

import inspect
import json
import re
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import openai
from llm_replies import strict_call

from auction_night.contracts import AnyEvent, NightConfig, dumps_events, load_config
from auction_night.contracts.act import ActContext
from auction_night.contracts.config import ModelPrice
from auction_night.contracts.events import (
    ActionData,
    ActionEvent,
    TalkData,
    TalkEvent,
    seat_vis,
)
from auction_night.contracts.mechanism import BID_PHASES
from auction_night.llm.accounting import PriceTable
from auction_night.llm.cassette import CallKey
from auction_night.llm.client import AnthropicClient, OpenAIClient
from auction_night.llm.orchestrate import (
    ModelCall,
    PhaseRequest,
    PhaseResult,
    SeatOutcome,
)
from auction_night.llm.parse import ParseContext
from auction_night.llm.request import build_request, user_message

REPO = Path(__file__).resolve().parents[2]
FIXED_TIME = "2026-11-01T00:00:00.000Z"

# Fake keys. The first two are key-shaped, the third is only caught by exact-value scrubbing.
FAKE_ANTHROPIC_KEY = "sk-" + "ant-api03-" + "Zq7" * 10
FAKE_OPENAI_KEY = "sk-" + "proj-" + "Ab3" * 10
FAKE_LOCAL_KEY = "local" + "-secret-" + "0123456789"

VALID_FIELDS: dict[str, dict[str, Any]] = {
    "plan": {"plan_priorities": [1] * 12, "plan_budget": [20] * 12, "plan_persona": "稳"},
    "talk": {"say": "大家好", "guess_budget": 150},
    "english_init": {"exit_price": 80},
    "english_revise": {"exit_price": 120},
    "dutch": {"grab_price": 150},
    "sealed": {"bid": 60},
}


def valid_args(phase: str, **fields: Any) -> dict[str, Any]:
    """A complete, valid ``act`` call for a phase, with some fields replaced."""
    return strict_call(phase, **{**VALID_FIELDS[phase], **fields})


def api_config() -> NightConfig:
    return load_config(REPO / "configs" / "night_api.example.yaml")


def example_prices() -> PriceTable:
    """Prices for every model of the example line-up (the placeholders included)."""

    def price(inp: float, out: float, read: float | None = None) -> ModelPrice:
        return ModelPrice(
            input_per_mtok=inp,
            output_per_mtok=out,
            cache_read_per_mtok=read,
            as_of=date(2026, 11, 1),
            source="https://example.com/prices",
        )

    return PriceTable(
        {
            "claude-haiku-5-5": price(0.10, 0.50, 0.01),
            "claude-sonnet-5-5": price(2, 10, 0.20),
            "<model-a1>": price(0.30, 1.20),
            "<model-a2>": price(0.30, 1.20),
            "<model-b1>": price(0.50, 2),
            "<model-b2>": price(0.50, 2),
            "<model-c1>": price(0, 0),
            "<model-d1>": price(0, 0),
        },
        source="test prices",
    )


# ------------------------------------------------------------------- SDK-shaped replies

ANTHROPIC_USAGE = {
    "input_tokens": 120,
    "output_tokens": 60,
    "cache_read_input_tokens": 800,
    "cache_creation_input_tokens": 0,
}
OPENAI_USAGE = {
    "prompt_tokens": 920,
    "completion_tokens": 60,
    "total_tokens": 980,
    "prompt_tokens_details": {"cached_tokens": 800},
}


def anthropic_message(
    args: Mapping[str, Any] | None = None,
    *,
    text: str | None = None,
    stop: str | None = None,
    model: str = "claude-haiku-5-5",
    usage: Mapping[str, Any] | None = None,
    blocks: Sequence[Mapping[str, Any]] = (),
) -> anthropic.types.Message:
    """A real ``anthropic.types.Message`` with an optional text and an optional ``act`` call."""
    content: list[Mapping[str, Any]] = list(blocks)
    if text is not None:
        content.append({"type": "text", "text": text})
    if args is not None:
        content.append({"type": "tool_use", "id": "toolu_test", "name": "act", "input": args})
    body: dict[str, Any] = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop or ("tool_use" if args is not None else "end_turn"),
        "stop_sequence": None,
        "usage": dict(usage or ANTHROPIC_USAGE),
    }
    if stop == "refusal":
        body["stop_details"] = {"type": "refusal", "category": "general_harms"}
    return anthropic.types.Message.model_validate(body)


def openai_completion(
    arguments: Mapping[str, Any] | str | None = None,
    *,
    content: str | None = None,
    finish: str | None = None,
    refusal: str | None = None,
    model: str = "local-model",
    usage: Mapping[str, Any] | None = None,
) -> openai.types.chat.ChatCompletion:
    """A real chat completion with an optional text and an optional ``act`` tool call."""
    message: dict[str, Any] = {"role": "assistant", "content": content, "refusal": refusal}
    if arguments is not None:
        text = (
            arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
        )
        message["tool_calls"] = [
            {"id": "call_test", "type": "function", "function": {"name": "act", "arguments": text}}
        ]
    return openai.types.chat.ChatCompletion.model_validate(
        {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish or ("tool_calls" if arguments is not None else "stop"),
                    "message": message,
                }
            ],
            "usage": dict(usage or OPENAI_USAGE),
        }
    )


def _http(status: int, headers: Mapping[str, str] | None = None, message: str = "x") -> Any:
    request = httpx2.Request("POST", "https://api.invalid/v1/messages")
    body = {"type": "error", "error": {"type": "api_error", "message": message}}
    return httpx2.Response(status, request=request, headers=dict(headers or {}), json=body), body


def sdk_status_error(
    cls: type[Exception], status: int, message: str = "failed", *, retry_after: str | None = None
) -> Exception:
    """An instance of an SDK status exception class, built the way the SDK builds it."""
    headers = {"retry-after": retry_after} if retry_after else {}
    response, body = _http(status, headers, message)
    return cls(message, response=response, body=body)  # type: ignore[call-arg]


def request_object() -> httpx2.Request:
    return httpx2.Request("POST", "https://api.invalid/v1/messages")


# ------------------------------------------------------------------------ script steps


@dataclass
class Step:
    """One scripted behaviour of the fake SDK. ``make`` gets the kind and the call context."""

    make: Callable[[str, "CallContext"], Any]
    seconds: float = 0.0


@dataclass(frozen=True)
class CallContext:
    seat: int
    phase: str
    lot: int
    model: str = ""


def ok(*, usage: Mapping[str, Any] | None = None, model: str | None = None, **fields: Any) -> Step:
    """A valid call for the phase asked, with some fields replaced."""

    def make(kind: str, ctx: CallContext) -> Any:
        args = valid_args(ctx.phase, **fields)
        if kind == "anthropic":
            return anthropic_message(args, usage=usage, model=model or ctx.model)
        return openai_completion(args, usage=usage, model=model or ctx.model)

    return Step(make)


def raw(arguments: Any) -> Step:
    """A tool call with arbitrary arguments (a dict, or text for the OpenAI form)."""

    def make(kind: str, ctx: CallContext) -> Any:
        if kind == "anthropic":
            return anthropic_message(
                arguments if isinstance(arguments, Mapping) else {}, model=ctx.model
            )
        return openai_completion(arguments, model=ctx.model)

    return Step(make)


def prose(text: str) -> Step:
    """A reply with text only: no tool call."""

    def make(kind: str, ctx: CallContext) -> Any:
        if kind == "anthropic":
            return anthropic_message(text=text, model=ctx.model)
        return openai_completion(content=text, model=ctx.model)

    return Step(make)


def api_refusal() -> Step:
    """A reply that the API itself marks as a refusal."""

    def make(kind: str, ctx: CallContext) -> Any:
        if kind == "anthropic":
            return anthropic_message(stop="refusal", model=ctx.model)
        return openai_completion(finish="content_filter", refusal="blocked", model=ctx.model)

    return Step(make)


def failing(factory: Callable[[str], Exception]) -> Step:
    """A call that raises. ``factory`` gets ``"anthropic"`` or ``"openai"``."""

    def make(kind: str, ctx: CallContext) -> Any:
        raise factory("anthropic" if kind == "anthropic" else "openai")

    return Step(make)


def _module(kind: str) -> Any:
    return anthropic if kind == "anthropic" else openai


def timeout_error() -> Step:
    return failing(lambda kind: _module(kind).APITimeoutError(request=request_object()))


def connection_error() -> Step:
    return failing(lambda kind: _module(kind).APIConnectionError(request=request_object()))


def rate_limit(retry_after: str | None = None) -> Step:
    return failing(
        lambda kind: sdk_status_error(
            _module(kind).RateLimitError, 429, "slow down", retry_after=retry_after
        )
    )


def server_error(status: int = 500) -> Step:
    return failing(
        lambda kind: sdk_status_error(_module(kind).InternalServerError, status, "server broke")
    )


def bad_request() -> Step:
    return failing(
        lambda kind: sdk_status_error(_module(kind).BadRequestError, 400, "invalid request")
    )


def hang(seconds: float) -> Step:
    """A call that takes ``seconds`` and then succeeds."""
    inner = ok()
    return Step(inner.make, seconds=seconds)


# ------------------------------------------------------------------------- the fake SDK

_SEAT = re.compile(r"\[\[seat (\d+)\]\]")
_PHASE = re.compile(r"\[\[phase (\w+) lot (\d+)\]\]")


def _texts(kwargs: Mapping[str, Any]) -> str:
    parts: list[str] = []
    system = kwargs.get("system")
    if isinstance(system, list):
        parts += [block.get("text", "") for block in system]
    for message in kwargs.get("messages", []):
        content = message.get("content")
        parts.append(content if isinstance(content, str) else json.dumps(content))
    return "\n".join(parts)


class ScriptedSdk:
    """One object that answers ``messages.create`` and ``chat.completions.create``.

    The seat is read from a ``[[seat N]]`` marker in the system text and the phase and lot from
    a ``[[phase NAME lot N]]`` marker in the first observation. ``script`` maps a seat to the
    steps for its successive calls; a seat without steps (or with none left) gets a valid
    call. ``gate`` can force the order in which concurrent replies are finished.
    """

    def __init__(
        self,
        script: Mapping[int, Sequence[Step]] | None = None,
        *,
        gate: "ArrivalGate | None" = None,
        check_signatures: bool = True,
    ) -> None:
        from anthropic.resources.messages import Messages
        from openai.resources.chat.completions import Completions

        self._signatures = {
            "anthropic": inspect.signature(Messages.create),
            "openai": inspect.signature(Completions.create),
        }
        self._check = check_signatures
        self._queues = {seat: deque(steps) for seat, steps in (script or {}).items()}
        self._lock = threading.Lock()
        self._gate = gate
        self.requests: list[dict[str, Any]] = []
        self.arrivals: list[tuple[int, str, int]] = []
        self.starts: list[tuple[int, str, int]] = []
        self.in_flight = 0
        self.peak_in_flight = 0
        self.messages = SimpleNamespace(create=lambda **kw: self._create("anthropic", kw))
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kw: self._create("openai", kw))
        )

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def _context(self, kwargs: Mapping[str, Any]) -> CallContext:
        text = _texts(kwargs)
        seat = _SEAT.search(text)
        phase = _PHASE.search(text)
        if seat is None or phase is None:
            raise AssertionError("the request carries no [[seat]] or [[phase]] marker")
        model = str(kwargs.get("model", ""))
        return CallContext(int(seat.group(1)), phase.group(1), int(phase.group(2)), model)

    def _create(self, kind: str, kwargs: dict[str, Any]) -> Any:
        if self._check:
            self._signatures[kind].bind(None, **kwargs)
        ctx = self._context(kwargs)
        with self._lock:
            self.requests.append(kwargs)
            self.starts.append((ctx.seat, ctx.phase, ctx.lot))
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
            queue = self._queues.get(ctx.seat)
            step = queue.popleft() if queue else ok()
        try:
            if step.seconds:
                threading.Event().wait(step.seconds)
            if self._gate is not None:
                self._gate.wait_turn(ctx)
            return step.make(kind, ctx)
        finally:
            with self._lock:
                self.in_flight -= 1
                self.arrivals.append((ctx.seat, ctx.phase, ctx.lot))
            if self._gate is not None:
                self._gate.finished(ctx)


class ArrivalGate:
    """Makes the calls of a concurrent phase finish in a chosen order of seats.

    Only the phases named in ``phases`` (default: all) are gated, and only phases in which all
    the seats of ``order`` are asked at once: a gated seat waits for the one before it.
    """

    def __init__(
        self, order: Sequence[int], *, phases: Sequence[str] | None = None, timeout_s: float = 10.0
    ) -> None:
        self._order = list(order)
        self._phases = None if phases is None else set(phases)
        self._done: set[tuple[str, int, int]] = set()
        self._cond = threading.Condition()
        self._timeout = timeout_s

    def _gated(self, ctx: CallContext) -> bool:
        in_phase = self._phases is None or ctx.phase in self._phases
        return in_phase and ctx.seat in self._order

    def wait_turn(self, ctx: CallContext) -> None:
        if not self._gated(ctx):
            return
        index = self._order.index(ctx.seat)
        if index == 0:
            return
        previous = (ctx.phase, ctx.lot, self._order[index - 1])
        with self._cond:
            if not self._cond.wait_for(lambda: previous in self._done, self._timeout):
                raise AssertionError(f"arrival order deadlock before seat {ctx.seat}")

    def finished(self, ctx: CallContext) -> None:
        with self._cond:
            self._done.add((ctx.phase, ctx.lot, ctx.seat))
            self._cond.notify_all()


def clients_for(config: NightConfig, sdk: Any) -> dict[str, Any]:
    """Real client wrappers around one fake SDK, with a fixed clock so latency is always 0."""
    clients: dict[str, Any] = {}
    for name, endpoint in config.endpoints.items():
        wrapper = AnthropicClient if endpoint.kind == "anthropic" else OpenAIClient
        clients[name] = wrapper(sdk, clock=lambda: 0.0)
    return clients


# ------------------------------------------------------------------------- requests


def make_call(
    config: NightConfig,
    seat: int,
    phase: str,
    *,
    lot: int | None = 1,
    step: int = 1,
    night: str | None = None,
    extra: str = "",
    system_extra: str = "",
    budget: int = 300,
) -> ModelCall:
    """A model call for a seat of the example configuration, with marker texts the fake reads."""
    seat_cfg = next(s for s in config.seats if s.seat == seat)
    assert seat_cfg.endpoint is not None
    endpoint = config.endpoints[seat_cfg.endpoint]
    shown_lot = 0 if lot is None else lot
    system = f"[[seat {seat}]] You bid in a test night. Fixed rules go here. {system_extra}"
    observation = f"[[phase {phase} lot {shown_lot}]] Your move now. {extra}"
    built = build_request(endpoint, seat_cfg, system=system, messages=[user_message(observation)])
    ctx = ParseContext(
        phase,  # type: ignore[arg-type]
        act=ActContext(n_lots=12, n_seats=8, self_seat=seat, current_lot=lot),
        budget_left=budget if phase in BID_PHASES else None,
    )
    return ModelCall(
        key=CallKey(night or config.night.id, lot, phase, step, seat),
        endpoint=seat_cfg.endpoint,
        request=built,
        ctx=ctx,
        native=endpoint.structured_output,
    )


# ------------------------------------------------------------------------ the fake night


def action_data(outcome: SeatOutcome, phase: str) -> ActionData:
    """The action event payload for an outcome (what the engine would decide)."""
    if not outcome.ok or outcome.result is None:
        return ActionData(
            seat=outcome.seat,  # type: ignore[arg-type]
            phase=phase,  # type: ignore[arg-type]
            status="abstain",
            reason=outcome.reason,
            attempts=outcome.action_attempts,
            call_id=outcome.call_id,
        )
    bidding = phase in BID_PHASES
    value = outcome.result.value if bidding else None
    raw_value = outcome.result.raw_value if bidding else None
    clamped = bidding and value is not None and raw_value is not None and raw_value > value
    return ActionData(
        seat=outcome.seat,  # type: ignore[arg-type]
        phase=phase,  # type: ignore[arg-type]
        status="clamped" if clamped else "ok",
        value=value,
        raw_value=raw_value if clamped else None,
        thought=outcome.thought,
        sanitized_chars=outcome.sanitized_chars,
        attempts=outcome.action_attempts,
        call_id=outcome.call_id,
    )


class FakeNight:
    """A small night driver: a plan, then per lot one talk call per speaker and a sealed round.

    Talk calls are separate requests, one speaker at a time, and each prompt quotes the lines
    already spoken, so a replay has to reproduce every earlier answer to match a later request.
    The event log it builds holds real action and talk events and, between them, the system
    events that the runner returns, numbered in order.
    """

    def __init__(
        self,
        config: NightConfig,
        *,
        seats: Sequence[int] = (1, 2, 3, 4),
        lots: int = 3,
        night: str | None = None,
        system_extra: str = "",
        sealed_sequential: bool = False,
    ) -> None:
        self.config = config
        self.night = night or config.night.id
        self.seats = tuple(seats)
        self.lots = lots
        self.system_extra = system_extra
        self.sealed_sequential = sealed_sequential
        self.log: list[AnyEvent] = []
        self.results: list[PhaseResult] = []
        self._step = 0
        self._queue: deque[tuple[str, int | None, int | None]] = deque()
        self._queue.append(("plan", None, None))
        for lot in range(1, lots + 1):
            for index in range(len(self.seats)):
                self._queue.append(("talk", lot, index))
            self._queue.append(("sealed", lot, None))
        self._current: tuple[str, int | None, int | None] | None = None
        self._heard: list[str] = []

    # -- the NightDriver protocol

    def _call(self, seat: int, phase: str, lot: int | None, extra: str = "") -> ModelCall:
        self._step += 1
        return make_call(
            self.config,
            seat,
            phase,
            lot=lot,
            step=self._step,
            night=self.night,
            extra=extra,
            system_extra=self.system_extra,
        )

    def next_phase(self) -> PhaseRequest | None:
        if not self._queue:
            return None
        self._current = self._queue.popleft()
        phase, lot, index = self._current
        if phase == "talk":
            assert index is not None
            speaker = self.seats[index]
            heard = " | ".join(self._heard) or "nobody yet"
            return PhaseRequest((self._call(speaker, "talk", lot, f"heard: {heard}"),))
        calls = tuple(self._call(seat, phase, lot) for seat in self.seats)
        return PhaseRequest(calls, sequential=self.sealed_sequential and phase == "sealed")

    def submit(self, result: PhaseResult) -> None:
        assert self._current is not None
        phase, lot, _ = self._current
        self.results.append(result)
        for outcome in result.outcomes:
            assert outcome.seat is not None
            if phase == "talk" and outcome.ok and outcome.args is not None:
                line = str(outcome.args.get("say") or "")
                self._heard.append(f"{outcome.seat}:{line}")
                self._add(
                    TalkEvent,
                    lot,
                    "public",
                    TalkData(seat=outcome.seat, text=line),  # type: ignore[arg-type]
                )
            action_lot = None if phase == "plan" else lot
            self._add(ActionEvent, action_lot, seat_vis(outcome.seat), action_data(outcome, phase))
        for event in result.events:
            seq = len(self.log) + 1
            self.log.append(event.to_event(seq))

    def _add(self, cls: Any, lot: int | None, vis: str, data: Any) -> None:
        self.log.append(cls(seq=len(self.log) + 1, lot=lot, vis=vis, data=data))

    # -- results

    def events_text(self) -> str:
        """The whole log as canonical JSON lines."""
        return dumps_events(self.log)
