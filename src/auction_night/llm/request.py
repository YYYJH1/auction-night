"""Exact request payloads for the Anthropic and OpenAI SDKs, and the request hash.

``build_request`` turns an endpoint, a seat (or show role) and the rendered texts into the keyword
arguments of one SDK call:

* Anthropic: ``client.messages.create(**payload)``;
* OpenAI-compatible (vLLM and others): ``client.chat.completions.create(**payload)``.

Nothing is sent from here. The payload follows the capability record of the model (see
:mod:`auction_night.llm.capabilities`): no sampling parameters where they are unsupported,
thinking and effort pinned, ``tool_choice`` ``auto`` unless the record allows forcing. The one
``act`` tool of the contract is sent in every phase and never changes.

Anthropic prompt caching is a prefix match over ``tools``, ``system`` and ``messages`` in this
order. The tool definition and the system text are therefore the same bytes for the whole night,
and a single ``cache_control`` breakpoint on the system block caches both. Only the user message
changes from call to call. Thinking and effort are part of the prefix too, so they are pinned per
seat and never vary between phases.

The request hash covers the whole payload plus what the payload leaves implicit: the endpoint
kind, the API version, the pinned thinking mode (an ``adaptive`` mode is not sent, but it is
recorded) and the pinned effort. Sampling parameters of local endpoints are part of the payload
and so of the hash.
"""

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from auction_night.contracts.act import (
    ACT_TOOL_NAME,
    ANTHROPIC_NULLABLE_STYLE,
    OPENAI_NULLABLE_STYLE,
    NullableStyle,
    act_tool_anthropic,
    act_tool_fingerprint,
    act_tool_openai,
)
from auction_night.contracts.config import EndpointConfig, EndpointKind, ModelFields
from auction_night.contracts.events import canonical_json, sha256_digest
from auction_night.llm.capabilities import (
    CapabilityError,
    anthropic_thinking,
    ensure_buildable,
    merged_sampling,
)

CacheTtl = Literal["5m", "1h"]
Message = Mapping[str, Any]

# Request fields that are the same for every call of a seat during a night. The Anthropic cache
# covers tools and system; the other fields are pinned so that they cannot vary either.
ANTHROPIC_PREFIX_KEYS: Final = (
    "model",
    "max_tokens",
    "system",
    "tools",
    "tool_choice",
    "thinking",
    "output_config",
    "extra_headers",
)

# vLLM convention for models whose chat template has a thinking switch. Whether a given model
# honours it is up to the model, which is why thinking modes of an OpenAI-compatible endpoint
# must be listed in its capability record before "disabled" can be pinned.
OPENAI_THINKING_DISABLED_EXTRA_BODY: Final[Mapping[str, Any]] = {
    "chat_template_kwargs": {"enable_thinking": False}
}


@dataclass(frozen=True)
class BuiltRequest:
    """One request: the SDK keyword arguments and their hash.

    ``request_hash`` is :func:`request_hash` of the payload. ``thinking`` and ``effort`` are the
    pinned values that went into it. For an ``adaptive`` thinking mode the payload has no
    ``thinking`` field, but ``thinking`` here is ``"adaptive"``.
    """

    kind: EndpointKind
    model: str
    payload: dict[str, Any]
    request_hash: str
    api_version: str | None = None
    thinking: str | None = None
    effort: str | None = None

    def payload_json(self) -> str:
        """The payload as canonical JSON (sorted keys, UTF-8 text), for logs and recordings."""
        return canonical_json(self.payload, allow_float=True)


def user_message(text: str) -> dict[str, str]:
    """A plain user turn."""
    return {"role": "user", "content": text}


def request_hash(
    payload: Mapping[str, Any],
    *,
    kind: EndpointKind,
    api_version: str | None = None,
    thinking: str | None = None,
    effort: str | None = None,
) -> str:
    """``sha256:<hex>`` of the canonical JSON of the payload and what it leaves implicit."""
    body = {
        "kind": kind,
        "api_version": api_version,
        "thinking": thinking,
        "effort": effort,
        "tool": act_tool_fingerprint(),
        "payload": payload,
    }
    return sha256_digest(canonical_json(body, allow_float=True))


def _plain_messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    if not messages:
        raise ValueError("a request needs at least one message")
    return [copy.deepcopy(dict(message)) for message in messages]


def build_anthropic_request(
    endpoint: EndpointConfig,
    ref: ModelFields,
    *,
    system: str,
    messages: Sequence[Message],
    cache_ttl: CacheTtl = "5m",
    nullable_style: NullableStyle | None = None,
) -> BuiltRequest:
    """Keyword arguments of ``client.messages.create`` for one call."""
    if endpoint.kind != "anthropic":
        raise CapabilityError([f"endpoint kind {endpoint.kind!r} is not 'anthropic'"])
    ensure_buildable(endpoint, ref)
    assert ref.model is not None and ref.max_tokens is not None

    tool = act_tool_anthropic(nullable_style or ANTHROPIC_NULLABLE_STYLE)
    if not endpoint.strict_tools:
        tool.pop("strict", None)
    cache_control: dict[str, str] = {"type": "ephemeral"}
    if cache_ttl != "5m":
        cache_control["ttl"] = cache_ttl

    payload: dict[str, Any] = {
        "model": ref.model,
        "max_tokens": ref.max_tokens,
        "system": [{"type": "text", "text": system, "cache_control": cache_control}],
        "tools": [tool],
        "tool_choice": _anthropic_tool_choice(endpoint),
    }
    thinking = anthropic_thinking(ref.thinking)
    if thinking is not None:
        payload["thinking"] = thinking
    if ref.effort is not None:
        payload["output_config"] = {"effort": ref.effort}
    if endpoint.api_version is not None:
        payload["extra_headers"] = {"anthropic-version": endpoint.api_version}
    payload["messages"] = _plain_messages(messages)
    return _built(endpoint, ref, payload)


def _anthropic_tool_choice(endpoint: EndpointConfig) -> dict[str, str]:
    if endpoint.tool_choice == "auto":
        return {"type": "auto"}
    if endpoint.tool_choice == "any":
        return {"type": "any"}
    return {"type": "tool", "name": ACT_TOOL_NAME}


def _openai_tool_choice(endpoint: EndpointConfig) -> str | dict[str, Any]:
    if endpoint.tool_choice == "auto":
        return "auto"
    if endpoint.tool_choice == "any":
        return "required"
    return {"type": "function", "function": {"name": ACT_TOOL_NAME}}


def build_openai_request(
    endpoint: EndpointConfig,
    ref: ModelFields,
    *,
    system: str,
    messages: Sequence[Message],
    nullable_style: NullableStyle | None = None,
) -> BuiltRequest:
    """Keyword arguments of ``client.chat.completions.create`` for one call."""
    if endpoint.kind != "openai_compatible":
        raise CapabilityError([f"endpoint kind {endpoint.kind!r} is not 'openai_compatible'"])
    ensure_buildable(endpoint, ref)
    assert ref.model is not None

    tool = act_tool_openai(
        strict=endpoint.strict_tools, nullable_style=nullable_style or OPENAI_NULLABLE_STYLE
    )
    payload: dict[str, Any] = {
        "model": ref.model,
        "messages": [{"role": "system", "content": system}, *_plain_messages(messages)],
        "tools": [tool],
        "tool_choice": _openai_tool_choice(endpoint),
    }
    if ref.max_tokens is not None:
        payload["max_tokens"] = ref.max_tokens
    extra_body: dict[str, Any] = {}
    for name, value in merged_sampling(endpoint, ref).items():
        if name == "top_k":  # not a parameter of the OpenAI SDK; vLLM reads it from the body
            extra_body["top_k"] = value
        else:
            payload[name] = value
    if ref.effort is not None:
        payload["reasoning_effort"] = ref.effort
    if ref.thinking is not None and ref.thinking.type == "disabled":
        extra_body.update(copy.deepcopy(dict(OPENAI_THINKING_DISABLED_EXTRA_BODY)))
    if extra_body:
        payload["extra_body"] = extra_body
    return _built(endpoint, ref, payload)


def _built(endpoint: EndpointConfig, ref: ModelFields, payload: dict[str, Any]) -> BuiltRequest:
    assert ref.model is not None
    thinking = ref.thinking.type if ref.thinking is not None else None
    return BuiltRequest(
        kind=endpoint.kind,
        model=ref.model,
        payload=payload,
        request_hash=request_hash(
            payload,
            kind=endpoint.kind,
            api_version=endpoint.api_version,
            thinking=thinking,
            effort=ref.effort,
        ),
        api_version=endpoint.api_version,
        thinking=thinking,
        effort=ref.effort,
    )


def build_request(
    endpoint: EndpointConfig,
    ref: ModelFields,
    *,
    system: str,
    messages: Sequence[Message],
    cache_ttl: CacheTtl = "5m",
    nullable_style: NullableStyle | None = None,
) -> BuiltRequest:
    """The request for whichever kind of endpoint the seat uses.

    ``cache_ttl`` applies to Anthropic only. ``nullable_style`` overrides the contract's default
    way of writing nullable tool properties (``any_of`` for Anthropic, ``type_array`` for
    OpenAI-compatible endpoints), for example to try the other form on an endpoint that refuses
    the default.
    """
    if endpoint.kind == "anthropic":
        return build_anthropic_request(
            endpoint,
            ref,
            system=system,
            messages=messages,
            cache_ttl=cache_ttl,
            nullable_style=nullable_style,
        )
    return build_openai_request(
        endpoint, ref, system=system, messages=messages, nullable_style=nullable_style
    )


def stable_prefix(request: BuiltRequest) -> dict[str, Any]:
    """The part of a request that must not change between the calls of one seat.

    For Anthropic these are the fields of ``ANTHROPIC_PREFIX_KEYS``; for OpenAI-compatible
    endpoints everything except the messages after the system message.
    """
    payload = request.payload
    if request.kind == "anthropic":
        return {key: payload[key] for key in ANTHROPIC_PREFIX_KEYS if key in payload}
    prefix = {key: value for key, value in payload.items() if key != "messages"}
    prefix["system_message"] = payload["messages"][0]
    return prefix
