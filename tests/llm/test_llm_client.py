"""The SDK wrappers: normalised replies, usage, failures, credentials and scrubbing.

Replies are built with the SDKs' own response types and failures with their own exception
classes. No client is ever created against a real endpoint.
"""

from typing import Any

import anthropic
import openai
import pytest
from sdk_fakes import (
    FAKE_ANTHROPIC_KEY,
    FAKE_LOCAL_KEY,
    FAKE_OPENAI_KEY,
    REPO,
    ScriptedSdk,
    anthropic_message,
    api_config,
    make_call,
    openai_completion,
    request_object,
    sdk_status_error,
    valid_args,
)

from auction_night.contracts import load_config
from auction_night.contracts.config import EndpointConfig
from auction_night.llm.client import (
    REDACTED,
    AnthropicClient,
    CallFailure,
    MissingCredentialError,
    OpenAIClient,
    Scrubber,
    Usage,
    classify_exception,
    endpoints_in_use,
    make_client,
    make_clients,
    merged_scrubber,
    normalise_anthropic,
    normalise_openai,
    reply_from_response,
    scrub_json,
    scrub_text,
)

# ------------------------------------------------------------------------------- usage


def test_anthropic_usage_keeps_cache_reads_and_writes_apart() -> None:
    message = anthropic_message(
        valid_args("sealed"),
        usage={
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 80,
            "cache_creation_input_tokens": 10,
            "output_tokens_details": {"thinking_tokens": 20},
        },
    )
    reply = normalise_anthropic(message, latency_ms=7)
    assert reply.usage == Usage(
        input_tokens=100,
        output_tokens=50,
        cache_read_input_tokens=80,
        cache_write_input_tokens=10,
        reasoning_tokens=20,
    )
    assert reply.usage is not None and reply.usage.prompt_tokens == 190
    assert reply.latency_ms == 7


def test_anthropic_usage_without_reasoning_details_says_unknown() -> None:
    reply = normalise_anthropic(anthropic_message(valid_args("sealed")), latency_ms=0)
    assert reply.usage is not None and reply.usage.reasoning_tokens is None
    assert reply.usage.cache_read_input_tokens == 800


def test_openai_prompt_total_includes_cached_tokens() -> None:
    completion = openai_completion(
        valid_args("sealed"),
        usage={
            "prompt_tokens": 1000,
            "completion_tokens": 90,
            "total_tokens": 1090,
            "prompt_tokens_details": {"cached_tokens": 600, "cache_write_tokens": 100},
            "completion_tokens_details": {"reasoning_tokens": 40},
        },
    )
    usage = normalise_openai(completion, latency_ms=0).usage
    assert usage == Usage(
        input_tokens=300,
        output_tokens=90,
        cache_read_input_tokens=600,
        cache_write_input_tokens=100,
        reasoning_tokens=40,
    )
    assert usage is not None and usage.prompt_tokens == 1000


def test_openai_style_servers_with_hit_counters_are_read() -> None:
    reply = normalise_openai(
        {
            "model": "m",
            "choices": [],
            "usage": {"prompt_tokens": 500, "completion_tokens": 5, "prompt_cache_hit_tokens": 450},
        },
        latency_ms=0,
    )
    assert reply.usage == Usage(input_tokens=50, output_tokens=5, cache_read_input_tokens=450)


def test_a_reply_without_usage_has_none() -> None:
    reply = normalise_openai({"model": "m", "choices": []}, latency_ms=0)
    assert reply.usage is None and reply.model == "m"


def test_usage_adds_and_round_trips() -> None:
    one = Usage(1, 2, 3, 4, 5)
    two = Usage(10, 20, 30, 40, None)
    total = one + two
    assert total == Usage(11, 22, 33, 44, 5)
    assert (Usage() + Usage()).reasoning_tokens is None
    assert Usage.from_dict(total.as_dict()) == total
    assert list(total.as_dict()) == [
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_write_input_tokens",
        "reasoning_tokens",
    ]


# ------------------------------------------------------------------------------ replies


def test_an_anthropic_tool_call_is_normalised() -> None:
    message = anthropic_message(valid_args("sealed", bid=77), model="claude-haiku-5-5")
    reply = normalise_anthropic(message, latency_ms=12)
    assert [c.name for c in reply.reply.tool_calls] == ["act"]
    assert reply.reply.tool_calls[0].arguments["bid"] == 77
    assert reply.stop_reason == "tool_use" and reply.refusal is False
    assert reply.model == "claude-haiku-5-5" and reply.response_id == "msg_test"
    assert reply.response is not None and reply.response["content"][0]["type"] == "tool_use"


def test_an_openai_tool_call_keeps_its_arguments_as_text() -> None:
    reply = normalise_openai(openai_completion(valid_args("sealed", bid=5)), latency_ms=0)
    call = reply.reply.tool_calls[0]
    assert call.name == "act" and isinstance(call.arguments, str) and '"bid": 5' in call.arguments
    assert reply.stop_reason == "tool_calls" and reply.model == "local-model"


def test_refusals_are_flagged_for_both_apis() -> None:
    assert normalise_anthropic(anthropic_message(stop="refusal"), latency_ms=0).refusal
    blocked = openai_completion(finish="content_filter", refusal="blocked")
    assert normalise_openai(blocked, latency_ms=0).refusal
    assert not normalise_openai(openai_completion(content="hello"), latency_ms=0).refusal


def test_thinking_blocks_lose_their_opaque_payload_but_keep_the_text() -> None:
    message = anthropic_message(
        valid_args("sealed"),
        blocks=[{"type": "thinking", "thinking": "let me think", "signature": "opaque-blob"}],
    )
    response = normalise_anthropic(message, latency_ms=0).response
    assert response is not None
    thinking = response["content"][0]
    assert thinking == {"type": "thinking", "thinking": "let me think"}
    assert "opaque-blob" not in repr(response)


def test_the_stored_response_reads_back_exactly_like_the_live_one() -> None:
    for live, kind in (
        (normalise_anthropic(anthropic_message(valid_args("talk")), latency_ms=3), "anthropic"),
        (
            normalise_openai(openai_completion(valid_args("talk")), latency_ms=3),
            "openai_compatible",
        ),
    ):
        assert live.response is not None
        again = reply_from_response(kind, live.response, live.usage, latency_ms=3)  # type: ignore[arg-type]
        assert again.reply == live.reply and again.usage == live.usage
        assert (again.model, again.response_id) == (live.model, live.response_id)


# ------------------------------------------------------------------------------ the call


def _clock(*ticks: float) -> Any:
    values = iter(ticks)
    return lambda: next(values)


def test_a_call_sends_the_payload_unchanged_plus_the_timeout() -> None:
    config = api_config()
    sdk = ScriptedSdk()
    client = AnthropicClient(sdk, clock=_clock(10.0, 10.25))
    call = make_call(config, 2, "sealed")
    reply = client.complete(call.request, timeout_s=12.5)
    sent = sdk.requests[0]
    assert sent.pop("timeout") == 12.5 and sent == call.request.payload
    assert reply.latency_ms == 250
    assert reply.usage is not None and reply.usage.cache_read_input_tokens == 800


def test_an_openai_call_goes_to_chat_completions() -> None:
    config = api_config()
    sdk = ScriptedSdk()
    reply = OpenAIClient(sdk, clock=_clock(0.0, 0.5)).complete(
        make_call(config, 1, "sealed").request, timeout_s=30
    )
    assert sdk.requests[0]["timeout"] == 30 and "messages" in sdk.requests[0]
    assert reply.latency_ms == 500 and reply.model == "<model-a1>"


def test_a_client_refuses_a_request_of_the_other_kind() -> None:
    config = api_config()
    with pytest.raises(ValueError, match="cannot send"):
        AnthropicClient(ScriptedSdk()).complete(make_call(config, 1, "sealed").request, timeout_s=1)
    with pytest.raises(ValueError, match="cannot send"):
        OpenAIClient(ScriptedSdk()).complete(make_call(config, 2, "sealed").request, timeout_s=1)


def test_sdk_exceptions_propagate_to_the_caller() -> None:
    class Failing:
        messages = type("M", (), {"create": staticmethod(_raise_rate_limit)})()

    with pytest.raises(anthropic.RateLimitError):
        AnthropicClient(Failing()).complete(
            make_call(api_config(), 2, "sealed").request, timeout_s=1
        )


def _raise_rate_limit(**_: Any) -> Any:
    raise sdk_status_error(anthropic.RateLimitError, 429, "slow down")


# --------------------------------------------------------------------------- failures

TRANSIENT_STATUSES = [
    (429, "RateLimitError"),
    (500, "InternalServerError"),
    (502, "InternalServerError"),
    (503, "InternalServerError"),
    (504, "InternalServerError"),
    (529, "OverloadedError"),
]
FINAL_STATUSES = [
    (400, "BadRequestError"),
    (401, "AuthenticationError"),
    (403, "PermissionDeniedError"),
    (404, "NotFoundError"),
    (422, "UnprocessableEntityError"),
]


def _status_class(module: Any, name: str) -> type[Exception]:
    return getattr(module, name, None) or module.InternalServerError


@pytest.mark.parametrize("module", [anthropic, openai], ids=["anthropic", "openai"])
@pytest.mark.parametrize(("status", "name"), TRANSIENT_STATUSES)
def test_rate_limits_and_server_errors_can_be_retried(module: Any, status: int, name: str) -> None:
    exc = sdk_status_error(_status_class(module, name), status, "try later", retry_after="3")
    failure = classify_exception(exc)
    assert (failure.kind, failure.retryable, failure.status) == ("error", True, status)
    assert failure.retry_after_s == 3.0 and failure.message == "try later"


@pytest.mark.parametrize("module", [anthropic, openai], ids=["anthropic", "openai"])
@pytest.mark.parametrize(("status", "name"), FINAL_STATUSES)
def test_requests_the_api_refuses_are_not_retried(module: Any, status: int, name: str) -> None:
    failure = classify_exception(sdk_status_error(_status_class(module, name), status, "no"))
    assert (failure.kind, failure.retryable, failure.status) == ("error", False, status)
    assert failure.error_type == _status_class(module, name).__name__


@pytest.mark.parametrize("module", [anthropic, openai], ids=["anthropic", "openai"])
def test_timeouts_are_timeouts_and_lost_connections_are_transient(module: Any) -> None:
    timed_out = classify_exception(module.APITimeoutError(request=request_object()))
    assert (timed_out.kind, timed_out.retryable) == ("timeout", False)
    lost = classify_exception(module.APIConnectionError(request=request_object()))
    assert (lost.kind, lost.retryable, lost.status) == ("error", True, None)


def test_builtin_timeouts_and_unknown_errors() -> None:
    assert classify_exception(TimeoutError("late")).kind == "timeout"
    other = classify_exception(ValueError("odd"))
    assert (other.kind, other.retryable, other.error_type) == ("error", False, "ValueError")


def test_an_error_message_is_scrubbed_and_cut_short() -> None:
    long_text = "x" * 1000 + FAKE_OPENAI_KEY
    exc = sdk_status_error(
        openai.AuthenticationError, 401, f"bad key {FAKE_OPENAI_KEY} {long_text}"
    )
    failure = classify_exception(exc)
    assert FAKE_OPENAI_KEY not in failure.message and len(failure.message) <= 300
    assert REDACTED in failure.message


def test_a_failure_round_trips_through_its_dict() -> None:
    failure = classify_exception(
        sdk_status_error(anthropic.RateLimitError, 429, "slow", retry_after="2")
    )

    assert CallFailure.from_dict(failure.as_dict()) == failure
    assert failure.api_type == "api_error"


# ------------------------------------------------------------------------- credentials


def _endpoint(config_name: str) -> EndpointConfig:
    return api_config().endpoints[config_name]


class Recorder:
    """A stand-in for an SDK constructor that keeps what it was called with."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **options: Any) -> ScriptedSdk:
        self.calls.append(options)
        return ScriptedSdk()


def test_keys_and_urls_come_from_the_environment_the_config_names() -> None:
    environ = {
        "ANTHROPIC_API_KEY": FAKE_ANTHROPIC_KEY,
        "VENDOR_A_API_KEY": FAKE_LOCAL_KEY,
        "VENDOR_A_BASE_URL": "http://127.0.0.1:8000/v1",
    }
    made = {"anthropic": Recorder(), "openai_compatible": Recorder()}
    anthropic_client = make_client(
        _endpoint("anthropic"), name="anthropic", environ=environ, factories=made
    )
    local_client = make_client(
        _endpoint("vendor_a"), name="vendor_a", environ=environ, factories=made
    )
    assert isinstance(anthropic_client, AnthropicClient) and isinstance(local_client, OpenAIClient)
    assert made["anthropic"].calls == [{"api_key": FAKE_ANTHROPIC_KEY, "max_retries": 0}]
    assert made["openai_compatible"].calls == [
        {"api_key": FAKE_LOCAL_KEY, "max_retries": 0, "base_url": "http://127.0.0.1:8000/v1"}
    ]


def test_a_missing_variable_is_named_but_no_value_is_shown() -> None:
    with pytest.raises(MissingCredentialError) as caught:
        make_client(
            _endpoint("anthropic"),
            name="anthropic",
            environ={"OTHER": FAKE_OPENAI_KEY},
            factories={},
        )
    message = str(caught.value)
    assert "ANTHROPIC_API_KEY" in message and FAKE_OPENAI_KEY not in message
    with pytest.raises(MissingCredentialError, match="VENDOR_A_BASE_URL"):
        make_client(
            _endpoint("vendor_a"),
            name="vendor_a",
            environ={"VENDOR_A_API_KEY": FAKE_LOCAL_KEY},
            factories={"openai_compatible": Recorder()},
        )
    with pytest.raises(MissingCredentialError, match="empty"):
        make_client(
            _endpoint("anthropic"),
            name="anthropic",
            environ={"ANTHROPIC_API_" + "KEY": "   "},
            factories={"anthropic": Recorder()},
        )


def test_a_local_endpoint_without_a_key_variable_still_gets_a_placeholder() -> None:
    endpoint = EndpointConfig(kind="openai_compatible", base_url="http://127.0.0.1:9000/v1")
    made = Recorder()
    client = make_client(endpoint, environ={}, factories={"openai_compatible": made})
    assert made.calls[0]["api_key"] == "none" and made.calls[0]["max_retries"] == 0
    assert isinstance(client, OpenAIClient)


def test_a_client_never_shows_its_key() -> None:
    client = make_client(
        _endpoint("anthropic"),
        name="anthropic",
        environ={"ANTHROPIC_API_KEY": FAKE_LOCAL_KEY},
        factories={"anthropic": Recorder()},
    )
    for text in (repr(client), repr(client.scrubber), str(client.scrubber)):
        assert FAKE_LOCAL_KEY not in text
    assert FAKE_LOCAL_KEY not in repr(vars(client).keys())
    assert client.scrubber.text(f"the key is {FAKE_LOCAL_KEY}") == f"the key is {REDACTED}"


def test_the_endpoints_in_use_follow_the_seats_and_roles() -> None:
    assert endpoints_in_use(api_config()) == [
        "anthropic",
        "vendor_a",
        "vendor_b",
        "vendor_c",
        "vendor_d",
    ]
    assert endpoints_in_use(load_config(REPO / "configs" / "night_mock.yaml")) == []


def test_make_clients_builds_one_client_per_endpoint_in_use() -> None:
    config = api_config()
    environ = {
        name: "value-" + name.lower()
        for name in (
            "ANTHROPIC_API_KEY",
            "VENDOR_A_API_KEY",
            "VENDOR_A_BASE_URL",
            "VENDOR_B_API_KEY",
            "VENDOR_B_BASE_URL",
            "VENDOR_C_API_KEY",
            "VENDOR_C_BASE_URL",
            "VENDOR_D_API_KEY",
            "VENDOR_D_BASE_URL",
        )
    }
    environ.update({n: "http://127.0.0.1:1/v1" for n in environ if n.endswith("BASE_URL")})
    made = {"anthropic": Recorder(), "openai_compatible": Recorder()}
    clients = make_clients(config, environ=environ, factories=made)
    assert sorted(clients) == endpoints_in_use(config)
    assert len(made["anthropic"].calls) == 1 and len(made["openai_compatible"].calls) == 4
    scrubber = merged_scrubber(clients.values())
    assert scrubber.text("x value-vendor_c_api_key y") == f"x {REDACTED} y"


def test_the_real_sdk_constructors_accept_what_make_client_passes() -> None:
    """Builds the genuine SDK clients (no request is sent) with an explicit HTTP client."""
    import httpx2

    environ = {
        "ANTHROPIC" + "_API_" + "KEY": FAKE_ANTHROPIC_KEY,
        "VENDOR_A" + "_API_" + "KEY": FAKE_LOCAL_KEY,
        "VENDOR_A_BASE_URL": "http://127.0.0.1:8000/v1",
    }
    http = httpx2.Client(trust_env=False)
    first = make_client(_endpoint("anthropic"), name="anthropic", environ=environ, http_client=http)
    second = make_client(_endpoint("vendor_a"), name="vendor_a", environ=environ, http_client=http)
    assert isinstance(first._sdk, anthropic.Anthropic) and first._sdk.max_retries == 0
    assert isinstance(second._sdk, openai.OpenAI) and second._sdk.max_retries == 0
    assert str(second._sdk.base_url).startswith("http://127.0.0.1:8000/v1")
    http.close()


def test_the_real_sdk_clients_can_be_made_behind_a_socks_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a SOCKS proxy in ALL_PROXY the SDKs set up a SOCKS transport, which needs socksio."""
    monkeypatch.setenv("ALL_PROXY", "socks5h://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    environ = {
        "ANTHROPIC" + "_API_" + "KEY": FAKE_ANTHROPIC_KEY,
        "VENDOR_A" + "_API_" + "KEY": FAKE_LOCAL_KEY,
        "VENDOR_A_BASE_URL": "http://127.0.0.1:8000/v1",
    }
    first = make_client(_endpoint("anthropic"), name="anthropic", environ=environ)
    second = make_client(_endpoint("vendor_a"), name="vendor_a", environ=environ)
    assert isinstance(first._sdk, anthropic.Anthropic)
    assert isinstance(second._sdk, openai.OpenAI)
    first._sdk.close()
    second._sdk.close()


def test_clients_can_be_made_for_some_endpoints_only() -> None:
    config = api_config()
    environ = {
        "VENDOR_C" + "_API_" + "KEY": "value-c",
        "VENDOR_C_BASE_URL": "http://127.0.0.1:3/v1",
    }
    made = {"anthropic": Recorder(), "openai_compatible": Recorder()}
    clients = make_clients(config, environ=environ, factories=made, names=["vendor_c"])
    assert sorted(clients) == ["vendor_c"] and len(made["openai_compatible"].calls) == 1
    with pytest.raises(MissingCredentialError, match="VENDOR_D_BASE_URL|VENDOR_D_API_KEY"):
        make_clients(config, environ=environ, factories=made, names=["vendor_c", "vendor_d"])


# ------------------------------------------------------------------------------ scrubbing

KEY_SHAPED = [
    FAKE_ANTHROPIC_KEY,
    FAKE_OPENAI_KEY,
    "sk-" + "A1" * 12,
    "sk-" + "proj-********" + "abcd",
    "gh" + "p_" + "C" * 36,
    "github" + "_pat_" + "D" * 30,
    "AI" + "za" + "E" * 35,
    "AK" + "IA" + "F" * 16,
]


@pytest.mark.parametrize("secret", KEY_SHAPED)
def test_key_shaped_text_is_replaced(secret: str) -> None:
    assert scrub_text(f"my key is {secret}, ok") == f"my key is {REDACTED}, ok"
    assert secret not in str(scrub_json({"deep": [{"text": f"x {secret}"}]}))


@pytest.mark.parametrize(
    "text",
    [
        "Authorization: Bearer abcdefgh12345678",
        "x-api-key: abcd1234efgh",
        "api_" + 'key="hunter22hunter"',
        "authorization=Basic dXNlcjpwYXNz",
        "-----BEGIN RSA PRIVATE " + "KEY-----\nMIIBOgIBAAJBAK\n-----END RSA PRIVATE KEY-----",
    ],
)
def test_credential_headers_and_assignments_lose_their_values(text: str) -> None:
    cleaned = scrub_text(text)
    assert REDACTED in cleaned
    for leaked in ("abcdefgh12345678", "abcd1234efgh", "hunter22hunter", "dXNlcjpwYXNz", "MIIBOg"):
        assert leaked not in cleaned


def test_ordinary_text_is_left_alone() -> None:
    text = "task-oriented risk-free sketch-a 这件拍品值 120 金币 sk 和 key 都是普通词 Bearer"
    assert scrub_text(text) == text


def test_exact_secrets_are_removed_wherever_they_appear() -> None:
    scrubber = Scrubber([FAKE_LOCAL_KEY, "tiny"])
    cleaned = scrubber.json({"a": f"x{FAKE_LOCAL_KEY}y", FAKE_LOCAL_KEY: ["tiny", FAKE_LOCAL_KEY]})
    assert FAKE_LOCAL_KEY not in str(cleaned)
    assert "tiny" in str(cleaned)  # too short to be treated as a secret
    assert repr(scrubber) == "Scrubber(1 secrets)"


def test_credential_named_keys_are_dropped_from_structures() -> None:
    cleaned = scrub_json(
        {"extra_body": {"top_k": 5}, "Authorization": "x", "headers": {"X-Api-Key": "y", "ok": 1}}
    )
    assert cleaned == {"extra_body": {"top_k": 5}, "headers": {"ok": 1}}


def test_a_reply_that_contains_a_key_is_scrubbed_before_it_is_read() -> None:
    message = anthropic_message(valid_args("sealed", thought=f"key {FAKE_ANTHROPIC_KEY}"))
    reply = normalise_anthropic(message, latency_ms=0)
    arguments = reply.reply.tool_calls[0].arguments
    assert FAKE_ANTHROPIC_KEY not in str(arguments) and REDACTED in arguments["thought"]
    assert FAKE_ANTHROPIC_KEY not in str(reply.response)
