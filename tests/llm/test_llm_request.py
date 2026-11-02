"""Request payloads: shapes, forbidden parameters, pinned thinking and effort, hash, prefix."""

import inspect
import json
from pathlib import Path
from typing import Any

import pytest
from llm_night import Night, make_settings, seat_of, with_endpoint, with_seat

from auction_night.contracts import (
    AnyEvent,
    NightConfig,
    act_tool_anthropic,
    act_tool_fingerprint,
    act_tool_openai,
)
from auction_night.contracts.config import (
    Capabilities,
    EndpointConfig,
    SamplingParams,
    SeatConfig,
    ThinkingConfig,
)
from auction_night.llm.capabilities import (
    CapabilityError,
    anthropic_thinking,
    merged_sampling,
)
from auction_night.llm.render import render_observation, render_system
from auction_night.llm.request import (
    BuiltRequest,
    build_anthropic_request,
    build_openai_request,
    build_request,
    request_hash,
    stable_prefix,
    user_message,
)

REPO = Path(__file__).resolve().parents[2]
SYSTEM = "系统提示：这一段整晚不变。"
OBS = "【现在要做】阶段 sealed：写出价 bid。"
FORBIDDEN = ("temperature", "top_p", "top_k", "seed")


def build(config: NightConfig, number: int, **kw: Any) -> BuiltRequest:
    endpoint, seat = seat_of(config, number)
    return build_request(
        endpoint, seat, system=kw.pop("system", SYSTEM), messages=[user_message(OBS)], **kw
    )


# --------------------------------------------------------------------- Anthropic shape


@pytest.mark.parametrize(("number", "model"), [(2, "claude-haiku-5-5"), (3, "claude-sonnet-5-5")])
def test_claude_payload_shape(api_config: NightConfig, number: int, model: str) -> None:
    built = build(api_config, number)
    payload = built.payload
    assert built.kind == "anthropic" and built.model == model
    assert list(payload) == [
        "model",
        "max_tokens",
        "system",
        "tools",
        "tool_choice",
        "output_config",
        "messages",
    ]
    assert payload["model"] == model and payload["max_tokens"] == 2048
    assert payload["system"] == [
        {"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}
    ]
    assert payload["tools"] == [act_tool_anthropic()]
    assert payload["tools"][0]["strict"] is True
    assert payload["tool_choice"] == {"type": "auto"}
    assert payload["output_config"] == {"effort": "medium"}
    assert payload["messages"] == [{"role": "user", "content": OBS}]
    assert "thinking" not in payload  # adaptive is pinned but left out of the request
    assert (built.thinking, built.effort) == ("adaptive", "medium")


@pytest.mark.parametrize("number", [2, 3])
def test_claude_requests_carry_no_sampling_parameters(api_config: NightConfig, number: int) -> None:
    text = json.dumps(build(api_config, number).payload)
    for name in FORBIDDEN + ("stop_sequences", "speed", "betas"):
        assert f'"{name}"' not in text


def test_thinking_modes_are_sent_only_when_not_adaptive(api_config: NightConfig) -> None:
    endpoint, haiku = seat_of(api_config, 2)
    _, sonnet = seat_of(api_config, 3)
    haiku_off = with_seat(haiku, thinking=ThinkingConfig(type="disabled"), effort="high")
    sonnet_low = with_seat(sonnet, thinking=ThinkingConfig(type="between_tools"), effort="high")
    off = build_request(endpoint, haiku_off, system=SYSTEM, messages=[user_message(OBS)])
    low = build_request(endpoint, sonnet_low, system=SYSTEM, messages=[user_message(OBS)])
    assert off.payload["thinking"] == {"type": "disabled"}
    assert off.payload["output_config"] == {"effort": "high"}
    assert low.payload["thinking"] == {"type": "between_tools"}
    assert (off.thinking, low.thinking) == ("disabled", "between_tools")
    assert anthropic_thinking(None) is None
    assert anthropic_thinking(ThinkingConfig(type="adaptive")) is None


def test_forced_tool_choice_follows_the_record(api_config: NightConfig) -> None:
    endpoint, haiku = seat_of(api_config, 2)
    _, sonnet = seat_of(api_config, 3)
    for kind, wire in (("any", {"type": "any"}), ("tool", {"type": "tool", "name": "act"})):
        forced = with_endpoint(endpoint, tool_choice=kind)
        built = build_request(forced, haiku, system=SYSTEM, messages=[user_message(OBS)])
        assert built.payload["tool_choice"] == wire
        with pytest.raises(CapabilityError, match="forced tool_choice"):
            build_request(forced, sonnet, system=SYSTEM, messages=[user_message(OBS)])


def test_strict_tools_follow_the_endpoint(api_config: NightConfig) -> None:
    endpoint, seat = seat_of(api_config, 3)
    lax = with_endpoint(endpoint, strict_tools=False)
    tool = build_request(lax, seat, system=SYSTEM, messages=[user_message(OBS)]).payload["tools"][0]
    assert "strict" not in tool
    assert {k: v for k, v in act_tool_anthropic().items() if k != "strict"} == tool
    no_strict = endpoint.capabilities_for("claude-sonnet-5-5").model_copy(
        update={"supports_strict_tools": False}
    )
    broken = with_endpoint(endpoint, model_capabilities={"claude-sonnet-5-5": no_strict})
    with pytest.raises(CapabilityError, match="strict tools"):
        build_request(broken, seat, system=SYSTEM, messages=[user_message(OBS)])


def test_the_cache_breakpoint_sits_after_tools_and_system(api_config: NightConfig) -> None:
    default = build(api_config, 3).payload
    assert json.dumps(default).count("cache_control") == 1
    assert default["system"][-1]["cache_control"] == {"type": "ephemeral"}
    long_lived = build(api_config, 3, cache_ttl="1h").payload
    assert long_lived["system"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert json.dumps(long_lived).count("cache_control") == 1


def test_the_api_version_is_a_header_and_part_of_the_hash(api_config: NightConfig) -> None:
    endpoint, seat = seat_of(api_config, 3)
    dated = with_endpoint(endpoint, api_version="2026-10-21")
    plain = build_request(endpoint, seat, system=SYSTEM, messages=[user_message(OBS)])
    built = build_request(dated, seat, system=SYSTEM, messages=[user_message(OBS)])
    assert built.payload["extra_headers"] == {"anthropic-version": "2026-10-21"}
    assert "extra_headers" not in plain.payload
    assert built.api_version == "2026-10-21" and built.request_hash != plain.request_hash


def test_nullable_style_can_be_overridden(api_config: NightConfig) -> None:
    default = build(api_config, 3).payload["tools"][0]
    other = build(api_config, 3, nullable_style="type_array").payload["tools"][0]
    assert other == act_tool_anthropic("type_array") != default
    assert default == act_tool_anthropic("any_of")
    assert "anyOf" in json.dumps(default) and "anyOf" not in json.dumps(other)


def test_messages_are_copied_and_must_not_be_empty(api_config: NightConfig) -> None:
    endpoint, seat = seat_of(api_config, 3)
    turn = user_message(OBS)
    built = build_request(endpoint, seat, system=SYSTEM, messages=[turn])
    built.payload["messages"][0]["content"] = "changed"
    built.payload["tools"][0]["name"] = "changed"
    assert turn["content"] == OBS
    assert build(api_config, 3).payload["tools"][0]["name"] == "act"
    with pytest.raises(ValueError, match="at least one message"):
        build_request(endpoint, seat, system=SYSTEM, messages=[])


def test_extra_turns_are_kept_in_order(api_config: NightConfig) -> None:
    endpoint, seat = seat_of(api_config, 3)
    turns = [user_message("甲"), {"role": "assistant", "content": "乙"}, user_message("丙")]
    built = build_request(endpoint, seat, system=SYSTEM, messages=turns)
    assert [m["content"] for m in built.payload["messages"]] == ["甲", "乙", "丙"]


# ------------------------------------------------------------------- OpenAI-compatible


def test_openai_payload_shape(api_config: NightConfig) -> None:
    built = build(api_config, 1)  # vendor_a: temperature 0.7 as the endpoint default
    assert built.kind == "openai_compatible"
    assert built.payload == {
        "model": "<model-a1>",
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": OBS},
        ],
        "tools": [act_tool_openai()],
        "tool_choice": "auto",
        "temperature": 0.7,
    }
    assert list(built.payload) == ["model", "messages", "tools", "tool_choice", "temperature"]
    assert built.thinking is None and built.effort is None


def test_openai_endpoint_without_sampling_sends_none(api_config: NightConfig) -> None:
    payload = build(api_config, 5).payload  # vendor_b: sampling supported but not configured
    assert not any(name in payload for name in FORBIDDEN + ("extra_body", "max_tokens"))


def test_seat_sampling_overrides_the_endpoint_and_top_k_goes_to_the_body(
    api_config: NightConfig,
) -> None:
    endpoint, seat = seat_of(api_config, 1)
    tuned = with_seat(
        seat, sampling=SamplingParams(temperature=0.2, top_k=40, seed=7), max_tokens=900
    )
    assert merged_sampling(endpoint, tuned) == {"temperature": 0.2, "top_k": 40, "seed": 7}
    payload = build_request(endpoint, tuned, system=SYSTEM, messages=[user_message(OBS)]).payload
    assert payload["temperature"] == 0.2 and payload["seed"] == 7 and payload["max_tokens"] == 900
    assert payload["extra_body"] == {"top_k": 40}
    assert "top_k" not in payload


def test_openai_tool_choice_and_strict_tools(api_config: NightConfig) -> None:
    endpoint, seat = seat_of(api_config, 5)
    strict_caps = Capabilities(supports_sampling_params=True, supports_strict_tools=True)
    strict = with_endpoint(endpoint, strict_tools=True, capabilities=strict_caps)
    payload = build_request(strict, seat, system=SYSTEM, messages=[user_message(OBS)]).payload
    assert payload["tools"] == [act_tool_openai(strict=True)]
    assert payload["tools"][0]["function"]["strict"] is True
    with pytest.raises(CapabilityError, match="strict tools"):
        build_request(
            with_endpoint(endpoint, strict_tools=True),
            seat,
            system=SYSTEM,
            messages=[user_message(OBS)],
        )
    forced_caps = Capabilities(supports_sampling_params=True, supports_forced_tool_choice=True)
    allowed = with_endpoint(endpoint, capabilities=forced_caps, tool_choice="any")
    required = build_request(allowed, seat, system=SYSTEM, messages=[user_message(OBS)])
    assert required.payload["tool_choice"] == "required"
    named = with_endpoint(allowed, tool_choice="tool")
    wire = build_request(named, seat, system=SYSTEM, messages=[user_message(OBS)]).payload
    assert wire["tool_choice"] == {"type": "function", "function": {"name": "act"}}
    with pytest.raises(CapabilityError, match="forced tool_choice"):
        build_request(
            with_endpoint(endpoint, tool_choice="any"),
            seat,
            system=SYSTEM,
            messages=[user_message(OBS)],
        )


def test_openai_thinking_and_effort_need_a_record(api_config: NightConfig) -> None:
    endpoint, seat = seat_of(api_config, 5)
    caps = Capabilities(
        supports_sampling_params=True,
        thinking_modes=("adaptive", "disabled"),
        efforts=("low", "high"),
    )
    reasoning = with_endpoint(endpoint, capabilities=caps)
    off = with_seat(seat, thinking=ThinkingConfig(type="disabled"), effort="low")
    payload = build_request(reasoning, off, system=SYSTEM, messages=[user_message(OBS)]).payload
    assert payload["reasoning_effort"] == "low"
    assert payload["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    adaptive = with_seat(seat, thinking=ThinkingConfig(type="adaptive"), effort="high")
    payload = build_request(
        reasoning, adaptive, system=SYSTEM, messages=[user_message(OBS)]
    ).payload
    assert payload["reasoning_effort"] == "high" and "extra_body" not in payload
    with pytest.raises(CapabilityError, match="cannot be controlled"):
        build_request(endpoint, off, system=SYSTEM, messages=[user_message(OBS)])
    caps_between = caps.model_copy(update={"thinking_modes": ("adaptive", "between_tools")})
    between = with_seat(seat, thinking=ThinkingConfig(type="between_tools"), effort="low")
    with pytest.raises(CapabilityError, match="only on Anthropic"):
        build_request(
            with_endpoint(endpoint, capabilities=caps_between),
            between,
            system=SYSTEM,
            messages=[user_message(OBS)],
        )


def test_kind_specific_builders_check_the_endpoint_kind(api_config: NightConfig) -> None:
    claude, claude_seat = seat_of(api_config, 3)
    local, local_seat = seat_of(api_config, 1)
    with pytest.raises(CapabilityError, match="not 'anthropic'"):
        build_anthropic_request(local, local_seat, system=SYSTEM, messages=[user_message(OBS)])
    with pytest.raises(CapabilityError, match="not 'openai_compatible'"):
        build_openai_request(claude, claude_seat, system=SYSTEM, messages=[user_message(OBS)])


def test_only_the_tools_mode_is_built(api_config: NightConfig) -> None:
    endpoint, seat = seat_of(api_config, 5)
    text_mode = with_endpoint(endpoint, structured_output="text")
    with pytest.raises(CapabilityError, match="structured_output"):
        build_request(text_mode, seat, system=SYSTEM, messages=[user_message(OBS)])


# --------------------------------------------------------------------------- SDK fit


@pytest.mark.parametrize("number", [2, 3])
def test_claude_payloads_bind_to_the_sdk_signature(api_config: NightConfig, number: int) -> None:
    from anthropic.resources.messages import Messages

    create = inspect.signature(Messages.create)
    endpoint, seat = seat_of(api_config, number)
    dated = with_endpoint(endpoint, api_version="2026-10-21")
    payload = build_request(dated, seat, system=SYSTEM, messages=[user_message(OBS)]).payload
    create.bind(None, **payload)  # raises TypeError for any keyword the SDK does not know
    assert not set(FORBIDDEN) & set(create.parameters)


def test_the_signature_check_catches_a_sampling_parameter(api_config: NightConfig) -> None:
    from anthropic.resources.messages import Messages

    payload = {**build(api_config, 3).payload, "temperature": 0.5}
    with pytest.raises(TypeError, match="temperature"):
        inspect.signature(Messages.create).bind(None, **payload)


@pytest.mark.parametrize("number", [1, 4, 5])
def test_openai_payloads_bind_to_the_sdk_signature(api_config: NightConfig, number: int) -> None:
    from openai.resources.chat.completions import Completions

    endpoint, seat = seat_of(api_config, number)
    tuned = with_seat(seat, sampling=SamplingParams(top_k=5, seed=1), max_tokens=100)
    payload = build_request(endpoint, tuned, system=SYSTEM, messages=[user_message(OBS)]).payload
    inspect.signature(Completions.create).bind(None, **payload)


# ------------------------------------------------------------------------------- hash


def test_hash_is_stable_and_a_sha256(api_config: NightConfig) -> None:
    one, two = build(api_config, 3), build(api_config, 3)
    assert one.request_hash == two.request_hash
    assert one.request_hash.startswith("sha256:") and len(one.request_hash) == 7 + 64
    assert one.request_hash == request_hash(
        one.payload, kind="anthropic", thinking="adaptive", effort="medium"
    )


def test_hash_changes_with_every_part_of_the_request(api_config: NightConfig) -> None:
    endpoint, seat = seat_of(api_config, 3)

    def hashed(ep: EndpointConfig = endpoint, st: SeatConfig = seat, **kw: Any) -> str:
        kw.setdefault("system", SYSTEM)
        kw.setdefault("messages", [user_message(OBS)])
        return build_request(ep, st, **kw).request_hash

    base = hashed()
    variants = {
        "system": hashed(system=SYSTEM + "。"),
        "observation": hashed(messages=[user_message(OBS + "。")]),
        "model": hashed(st=with_seat(seat, model="claude-haiku-5-5")),
        "max_tokens": hashed(st=with_seat(seat, max_tokens=4096)),
        "effort": hashed(st=with_seat(seat, effort="high")),
        "thinking": hashed(
            st=with_seat(seat, thinking=ThinkingConfig(type="between_tools"), effort="high")
        ),
        "api_version": hashed(ep=with_endpoint(endpoint, api_version="2026-10-21")),
        "tool_style": hashed(nullable_style="type_array"),
        "strict": hashed(ep=with_endpoint(endpoint, strict_tools=False)),
        "ttl": hashed(cache_ttl="1h"),
    }
    assert len(set(variants.values()) | {base}) == len(variants) + 1


def test_effort_and_adaptive_thinking_are_in_the_hash_though_not_in_every_field() -> None:
    payload = {"model": "m", "max_tokens": 5, "messages": [user_message("x")]}
    base = request_hash(payload, kind="anthropic", thinking="adaptive", effort="medium")
    assert base != request_hash(payload, kind="anthropic", thinking=None, effort="medium")
    assert base != request_hash(payload, kind="anthropic", thinking="adaptive", effort="high")
    assert base != request_hash(payload, kind="anthropic", thinking="adaptive", effort=None)
    assert base != request_hash(
        payload, kind="openai_compatible", thinking="adaptive", effort="medium"
    )


def test_hash_ignores_key_order_and_covers_floats() -> None:
    one = {"a": 1, "b": {"x": 0.7, "y": [1, 2]}}
    two = {"b": {"y": [1, 2], "x": 0.7}, "a": 1}
    assert request_hash(one, kind="openai_compatible") == request_hash(
        two, kind="openai_compatible"
    )
    hotter = {"a": 1, "b": {"x": 0.8, "y": [1, 2]}}
    assert request_hash(one, kind="openai_compatible") != request_hash(
        hotter, kind="openai_compatible"
    )


def test_sampling_of_local_endpoints_is_in_the_hash(api_config: NightConfig) -> None:
    endpoint, seat = seat_of(api_config, 1)
    base = build_request(endpoint, seat, system=SYSTEM, messages=[user_message(OBS)])
    warmer = with_endpoint(endpoint, sampling=SamplingParams(temperature=0.9))
    other = build_request(warmer, seat, system=SYSTEM, messages=[user_message(OBS)])
    assert base.request_hash != other.request_hash


def test_the_payload_has_a_canonical_json_form(api_config: NightConfig) -> None:
    built = build(api_config, 3)
    text = built.payload_json()
    assert json.loads(text) == built.payload
    assert text == json.dumps(
        built.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert build(api_config, 1).payload_json().count('"temperature":0.7') == 1


def test_the_tool_fingerprint_is_the_contract_digest() -> None:
    assert act_tool_fingerprint().startswith("sha256:")


# ----------------------------------------------------------------- stable prefix


def _phase_requests(
    config: NightConfig, night: Night, events: list[AnyEvent], number: int
) -> dict[str, BuiltRequest]:
    endpoint, seat = seat_of(config, number)
    settings = make_settings()
    system = render_system(events, number, settings)
    moments = {
        "plan": "opening_done",
        "talk": "lot6_open",
        "english_init": "lot2_open",
        "english_revise": "lot2_before_revise",
        "dutch": "lot5_open",
        "sealed": "lot6_open",
    }
    requests = {}
    for phase, mark in moments.items():
        observation = render_observation(
            events, number, phase, settings, guess_target=5, as_of_seq=night.marks[mark]
        )
        requests[phase] = build_request(
            endpoint, seat, system=system, messages=[user_message(observation)]
        )
    return requests


@pytest.mark.parametrize("number", [2, 3])
def test_the_cached_prefix_is_byte_identical_in_every_phase(
    api_config: NightConfig, night: Night, events: list[AnyEvent], number: int
) -> None:
    requests = _phase_requests(api_config, night, events, number)
    prefixes = {
        phase: json.dumps(stable_prefix(r), ensure_ascii=False) for phase, r in requests.items()
    }
    assert len(set(prefixes.values())) == 1
    tools = {json.dumps(r.payload["tools"], ensure_ascii=False) for r in requests.values()}
    assert tools == {json.dumps([act_tool_anthropic()], ensure_ascii=False)}
    systems = {r.payload["system"][0]["text"] for r in requests.values()}
    assert len(systems) == 1
    # Only the user message differs, and so does the hash.
    messages = {json.dumps(r.payload["messages"], ensure_ascii=False) for r in requests.values()}
    assert len(messages) == len(requests)
    assert len({r.request_hash for r in requests.values()}) == len(requests)
    for built in requests.values():
        assert set(built.payload) - set(stable_prefix(built)) == {"messages"}
        assert json.dumps(built.payload).count("cache_control") == 1


def test_the_openai_prefix_is_stable_too(
    api_config: NightConfig, night: Night, events: list[AnyEvent]
) -> None:
    requests = _phase_requests(api_config, night, events, 1)
    prefixes = {json.dumps(stable_prefix(r), ensure_ascii=False) for r in requests.values()}
    assert len(prefixes) == 1
    assert all(r.payload["messages"][0]["role"] == "system" for r in requests.values())


def test_show_roles_can_be_built_too(api_config: NightConfig) -> None:
    role = api_config.show.judges[1]
    endpoint = api_config.endpoints[role.endpoint]
    built = build_request(endpoint, role, system=SYSTEM, messages=[user_message(OBS)])
    assert built.kind == "openai_compatible" and built.payload["model"] == "<model-d1>"
