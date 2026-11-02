"""One call from the event log to a parsed action, with fake clients and no network."""

import inspect
import json
from types import SimpleNamespace
from typing import Any

import pytest
from llm_night import CANARIES, LLM_SEATS, Night, make_settings
from llm_replies import dumps, strict_call

from auction_night.contracts import AnyEvent, NightConfig
from auction_night.contracts.mechanism import PHASES
from auction_night.llm.counters import ParseCounters, repair_data
from auction_night.llm.parse import (
    ParseContext,
    parse_reply,
    reply_from_anthropic,
    reply_from_openai,
)
from auction_night.llm.render import parse_context, render_observation, render_system
from auction_night.llm.request import BuiltRequest, build_request, user_message

MOMENTS = {
    "plan": "opening_done",
    "talk": "lot6_open",
    "english_init": "lot2_open",
    "english_revise": "lot2_before_revise",
    "dutch": "lot5_open",
    "sealed": "lot6_open",
}
FIELDS = {
    "plan": {"plan_priorities": [1] * 12, "plan_budget": [20] * 12, "plan_persona": "稳"},
    "talk": {"say": "大家好", "guess_budget": 150},
    "english_init": {"exit_price": 80},
    "english_revise": {"exit_price": 120},
    "dutch": {"grab_price": 150},
    "sealed": {"bid": 60},
}


class FakeAnthropic:
    """Records the keyword arguments of ``messages.create`` and checks them against the SDK."""

    def __init__(self, *replies: Any) -> None:
        from anthropic.resources.messages import Messages

        self._signature = inspect.signature(Messages.create)
        self._replies = list(replies)
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self._signature.bind(None, **kwargs)
        self.calls.append(kwargs)
        return self._replies.pop(0)


class FakeOpenAI:
    """Records the keyword arguments of ``chat.completions.create`` and checks them."""

    def __init__(self, *replies: Any) -> None:
        from openai.resources.chat.completions import Completions

        self._signature = inspect.signature(Completions.create)
        self._replies = list(replies)
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        self._signature.bind(None, **kwargs)
        self.calls.append(kwargs)
        return self._replies.pop(0)


def claude_message(**tool_input: Any) -> Any:
    block = SimpleNamespace(type="tool_use", id="toolu_1", name="act", input=tool_input)
    return SimpleNamespace(content=[block], stop_reason="tool_use")


def claude_text(text: str) -> Any:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn"
    )


def chat_completion(arguments: str) -> Any:
    call = SimpleNamespace(function=SimpleNamespace(name="act", arguments=arguments))
    message = SimpleNamespace(content=None, tool_calls=[call], refusal=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="tool_calls")])


def endpoint_and_seat(config: NightConfig, number: int) -> tuple[Any, Any]:
    seat = next(s for s in config.seats if s.seat == number)
    return config.endpoints[seat.endpoint], seat


def prepare(
    config: NightConfig, night: Night, events: list[AnyEvent], seat: int, phase: str
) -> tuple[BuiltRequest, ParseContext, str]:
    """Render, build the request and make the parse context of one call."""
    settings = make_settings()
    seq = night.marks[MOMENTS[phase]]
    system = render_system(events, seat, settings)
    observation = render_observation(
        events,
        seat,
        phase,
        settings,
        guess_target=1 if seat != 1 else 2,
        as_of_seq=seq,  # type: ignore[arg-type]
    )
    endpoint, ref = endpoint_and_seat(config, seat)
    built = build_request(endpoint, ref, system=system, messages=[user_message(observation)])
    ctx = parse_context(events, seat, phase, settings, as_of_seq=seq)  # type: ignore[arg-type]
    return built, ctx, observation


def test_a_claude_call_end_to_end(
    api_config: NightConfig, night: Night, events: list[AnyEvent]
) -> None:
    built, ctx, observation = prepare(api_config, night, events, 3, "sealed")
    client = FakeAnthropic(claude_message(**strict_call("sealed", bid=500)))
    reply = reply_from_anthropic(client.messages.create(**built.payload))
    result = parse_reply(reply, ctx)

    assert client.calls == [built.payload]
    assert result.ok and result.raw_value == 500 and result.value == 142  # seat 3 has 142 left
    assert [a.kind for a in result.adjustments] == ["budget_clamp"]
    counters = ParseCounters()
    counters.record(result, "tools")
    assert counters.rule_compliance == 1.0 and dict(counters.legal_clamps) == {"budget_clamp": 1}
    data = repair_data(result, "tools", role="seat:3", seat=3)
    assert data is not None and data.codes == ["clamp"]
    assert built.payload["messages"] == [user_message(observation)]


def test_a_local_model_call_end_to_end(
    api_config: NightConfig, night: Night, events: list[AnyEvent]
) -> None:
    built, ctx, _ = prepare(api_config, night, events, 1, "sealed")
    client = FakeOpenAI(chat_completion(dumps(strict_call("sealed", bid=33))))
    reply = reply_from_openai(client.chat.completions.create(**built.payload))
    result = parse_reply(reply, ctx)
    assert result.ok and result.value == 33 and built.payload["temperature"] == 0.7
    assert result.trace.arguments_as_text and not result.trace.syntax_fixes


def test_a_malformed_reply_is_retried_once_with_the_violations(
    api_config: NightConfig, night: Night, events: list[AnyEvent]
) -> None:
    built, ctx, observation = prepare(api_config, night, events, 3, "sealed")
    bad = "好的！```json\n" + dumps({"phase": "sealed", "bid": -4}) + "\n```"
    client = FakeAnthropic(claude_text(bad), claude_message(**strict_call("sealed", bid=60)))
    counters = ParseCounters()

    first = parse_reply(reply_from_anthropic(client.messages.create(**built.payload)), ctx)
    counters.record(first, "tools")
    assert first.status == "retry" and first.retry_message is not None
    turns = [
        user_message(observation),
        {"role": "assistant", "content": bad},
        user_message(first.retry_message),
    ]
    again = build_request(
        api_config.endpoints["anthropic"],
        endpoint_and_seat(api_config, 3)[1],
        system=built.payload["system"][0]["text"],
        messages=turns,
    )
    second = parse_reply(
        reply_from_anthropic(client.messages.create(**again.payload)), ctx, attempt=2
    )
    counters.record(second, "tools")

    assert second.ok and second.value == 60
    assert [c["messages"][-1]["content"] for c in client.calls][0] == observation
    assert "bid 超出范围" in client.calls[1]["messages"][-1]["content"]
    # The retry changes the messages only: the cached prefix is untouched.
    for key in ("model", "system", "tools", "tool_choice", "output_config"):
        assert client.calls[0][key] == client.calls[1][key]
    assert again.request_hash != built.request_hash
    assert (counters.attempts, counters.actions, counters.retries) == (2, 1, 1)
    assert counters.invalid_inputs == 1 and counters.syntax_attempts == 1  # text, not a tool call


@pytest.mark.parametrize("seat", [1, 3])
@pytest.mark.parametrize("phase", PHASES)
def test_every_phase_goes_through_both_kinds_of_client(
    api_config: NightConfig, night: Night, events: list[AnyEvent], seat: int, phase: str
) -> None:
    built, ctx, _ = prepare(api_config, night, events, seat, phase)
    call = strict_call(phase, **FIELDS[phase])
    if built.kind == "anthropic":
        client: Any = FakeAnthropic(claude_message(**call))
        reply = reply_from_anthropic(client.messages.create(**built.payload))
    else:
        client = FakeOpenAI(chat_completion(dumps(call)))
        reply = reply_from_openai(client.chat.completions.create(**built.payload))
    result = parse_reply(reply, ctx)
    assert result.ok, (phase, result.errors)
    assert result.trace.syntax_fixes == () and result.trace.coercions == ()


@pytest.mark.parametrize("seat", LLM_SEATS)
def test_a_request_carries_no_private_text_of_another_seat(
    api_config: NightConfig, night: Night, events: list[AnyEvent], seat: int
) -> None:
    for phase in ("talk", "sealed"):
        built, _, _ = prepare(api_config, night, events, seat, phase)
        text = json.dumps(built.payload, ensure_ascii=False)
        for other in LLM_SEATS:
            if other != seat:
                assert CANARIES[other] not in text
                assert f"THOUGHT-S{other}-" not in text and f"PERSONA-S{other}" not in text
        assert CANARIES[seat] in text
        assert "api_key" not in text.lower()


def test_requests_are_reproducible_and_seat_specific(
    api_config: NightConfig, night: Night, events: list[AnyEvent]
) -> None:
    one, _, _ = prepare(api_config, night, events, 3, "sealed")
    two, _, _ = prepare(api_config, night, events, 3, "sealed")
    other, _, _ = prepare(api_config, night, events, 2, "sealed")
    assert one.request_hash == two.request_hash and one.payload == two.payload
    assert one.request_hash != other.request_hash
