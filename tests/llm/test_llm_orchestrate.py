"""The runner: retries, timeouts, refusals, abstentions, concurrency and parallel nights.

Everything goes through fake SDK clients that raise the SDKs' own exceptions. Back-off sleeps
are recorded instead of slept.
"""

import asyncio
import threading
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

import pytest
from llm_night import Night, make_settings
from llm_replies import strict_call
from sdk_fakes import (
    FIXED_TIME,
    ArrivalGate,
    FakeNight,
    ScriptedSdk,
    Step,
    api_config,
    api_refusal,
    bad_request,
    clients_for,
    connection_error,
    example_prices,
    failing,
    hang,
    make_call,
    ok,
    prose,
    rate_limit,
    raw,
    sdk_status_error,
    server_error,
    timeout_error,
    valid_args,
)

from auction_night.contracts import AnyEvent, NightConfig
from auction_night.llm.cassette import CallKey, Cassette
from auction_night.llm.client import AnthropicClient
from auction_night.llm.orchestrate import (
    EndpointLimiter,
    ModelCall,
    ModelRunner,
    NightSession,
    PhaseRequest,
    PhaseResult,
    RunnerSettings,
    follow_up_request,
)
from auction_night.llm.render import parse_context, render_observation, render_system
from auction_night.llm.request import build_request, user_message

FAST = RunnerSettings(
    timeout_s=5.0,
    syntax_retries=1,
    transient_retries=2,
    backoff_base_s=1.0,
    backoff_cap_s=30.0,
    wall_clock_slack_s=0.5,
)


@dataclass
class Played:
    result: PhaseResult
    session: NightSession
    sdk: ScriptedSdk
    cassette: Cassette
    sleeps: list[float]
    runner: ModelRunner

    @property
    def outcome(self):  # noqa: ANN201 - the first (usually only) outcome
        return self.result.outcomes[0]

    @property
    def entries(self):  # noqa: ANN201
        return self.cassette.entries

    def codes(self) -> list[list[str]]:
        return [list(e.data.codes) for e in self.result.events if e.type == "parse_repair"]  # type: ignore[union-attr]


def play(
    calls: Sequence[ModelCall],
    script: dict[int, list[Step]] | None = None,
    *,
    settings: RunnerSettings = FAST,
    sequential: bool = False,
    config: NightConfig | None = None,
    gate: ArrivalGate | None = None,
) -> Played:
    config = config or api_config()
    sdk = ScriptedSdk(script, gate=gate)
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    runner = ModelRunner(clients_for(config, sdk), clock=lambda: FIXED_TIME, sleep=fake_sleep)
    cassette = Cassette.record()
    session = NightSession.open(config, cassette, prices=example_prices(), settings=settings)
    result = asyncio.run(runner.run_phase(session, PhaseRequest(tuple(calls), sequential)))
    runner.close()
    return Played(result, session, sdk, cassette, sleeps, runner)


def sealed(seat: int = 2, **kw: Any) -> ModelCall:
    return make_call(api_config(), seat, "sealed", **kw)


# ============================================================ one call: the happy path


def test_a_valid_reply_is_one_attempt_and_one_entry() -> None:
    played = play([sealed()])
    outcome = played.outcome
    assert outcome.ok and outcome.attempts == 1 and outcome.reason is None
    assert outcome.args is not None and outcome.args["bid"] == 60
    assert played.result.events == () and played.result.cap_reached is False
    (entry,) = played.entries
    assert entry.key.as_tuple() == (api_config().night.id, 1, "sealed", 1, 2, 1)
    assert entry.outcome == "ok" and entry.role == "seat:2" and entry.kind == "anthropic"
    assert entry.model == entry.model_returned == "claude-haiku-5-5"
    assert entry.started_at == entry.finished_at == FIXED_TIME
    # 120 x 0.10 + 60 x 0.50 + 800 x 0.01 micro-dollars
    assert entry.cost_microusd == 50 == outcome.cost_microusd
    assert entry.usage is not None and entry.usage["cache_read_input_tokens"] == 800
    assert entry.parsed is not None and entry.parsed["status"] == "ok"


def test_the_outcome_carries_what_an_action_event_needs() -> None:
    call = make_call(api_config(), 3, "sealed", lot=4, step=9)
    played = play([call], {3: [ok(bid=500, thought="想\n好了")]})
    outcome = played.outcome
    assert outcome.call_id == "l4.sealed.9.s3" and outcome.action_attempts == 1
    assert outcome.result is not None
    assert (outcome.result.raw_value, outcome.result.value) == (500, 300)
    assert outcome.thought == "想 好了" or "\n" not in outcome.thought
    assert outcome.sanitized_chars > 0 and outcome.text_sanitized_chars == 0
    assert [c for c in played.codes()] == [["clamp", "sanitize"]]
    assert played.result.events[0].data.status == "ok"  # type: ignore[union-attr]
    assert "status=" not in played.result.events[0].data.detail  # type: ignore[union-attr]


# ================================================================= retries: bad replies


def test_a_reply_that_breaks_a_rule_is_retried_once_with_the_violations() -> None:
    call = sealed()
    bad = strict_call("sealed", bid=-4)
    played = play([call], {2: [raw(bad), ok(bid=70)]})
    outcome = played.outcome
    assert outcome.ok and outcome.attempts == 2 and outcome.args is not None
    assert outcome.args["bid"] == 70
    first, second = played.entries
    assert [e.outcome for e in played.entries] == ["parse_failed", "ok"]
    assert [e.key.attempt for e in played.entries] == [1, 2]
    assert first.parsed is not None and first.parsed["status"] == "retry"
    assert first.parsed["errors"] and first.parsed["errors"][0]["field"] == "bid"
    # the follow-up: observation, the model's own reply, and the message naming the violation
    messages = played.sdk.requests[1]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert "bid" in messages[2]["content"] and "act" in messages[2]["content"]
    assert '"bid": -4' in messages[1]["content"]
    # only the messages change: the cached prefix is untouched
    for key in ("model", "system", "tools", "tool_choice", "output_config", "max_tokens"):
        assert played.sdk.requests[0][key] == played.sdk.requests[1][key]
    assert first.request_hash != second.request_hash
    assert played.codes() == [["invalid_input", "retry"]]
    assert played.result.events[0].data.status == "retry"  # type: ignore[union-attr]
    counters = played.session.counters.get("seat:2")
    assert (counters.attempts, counters.actions, counters.retries) == (2, 1, 1)
    assert counters.invalid_inputs == 1 and counters.rule_compliance == 0.0


def test_the_retry_request_is_the_request_that_the_builder_makes_for_all_turns() -> None:
    config = api_config()
    call = sealed()
    played = play([call], {2: [raw(strict_call("sealed", bid=-4)), ok()]})
    seat = next(s for s in config.seats if s.seat == 2)
    payload = call.request.payload
    turns = [
        user_message(payload["messages"][0]["content"]),
        {"role": "assistant", "content": played.sdk.requests[1]["messages"][1]["content"]},
        user_message(played.sdk.requests[1]["messages"][2]["content"]),
    ]
    rebuilt = build_request(
        config.endpoints["anthropic"], seat, system=payload["system"][0]["text"], messages=turns
    )
    assert played.entries[1].request_hash == rebuilt.request_hash


@pytest.mark.parametrize("seat", [2, 1], ids=["anthropic", "openai"])
def test_follow_up_request_matches_the_builder_for_both_kinds(seat: int) -> None:
    config = api_config()
    call = make_call(config, seat, "sealed")
    seat_cfg = next(s for s in config.seats if s.seat == seat)
    extended = follow_up_request(call.request, "earlier reply", "please redo")
    system = (
        call.request.payload["system"][0]["text"]
        if seat == 2
        else call.request.payload["messages"][0]["content"]
    )
    first_user = call.request.payload["messages"][-1]["content"]
    turns = [
        user_message(first_user),
        {"role": "assistant", "content": "earlier reply"},
        user_message("please redo"),
    ]
    rebuilt = build_request(
        config.endpoints[call.endpoint], seat_cfg, system=system, messages=turns
    )
    assert extended.payload == rebuilt.payload and extended.request_hash == rebuilt.request_hash
    assert len(call.request.payload["messages"]) == (1 if seat == 2 else 2)  # not mutated


def test_two_bad_replies_end_in_an_abstention_with_a_repair_event() -> None:
    bad = raw(strict_call("sealed", bid=-4))
    played = play([sealed()], {2: [bad, bad]})
    outcome = played.outcome
    assert not outcome.ok and outcome.status == "abstain" and outcome.args is None
    assert outcome.reason == "parse_failed" and outcome.attempts == 2
    assert [e.outcome for e in played.entries] == ["parse_failed", "parse_failed"]
    assert played.codes() == [["invalid_input", "retry"], ["invalid_input", "fallback"]]
    final = played.result.events[-1].data
    assert final.status == "abstain" and "reason=parse_failed" in final.detail  # type: ignore[union-attr]
    assert "status=" not in final.detail  # type: ignore[union-attr]
    assert [e.data.status for e in played.result.events] == ["retry", "abstain"]  # type: ignore[union-attr]
    assert final.attempt == 2 and final.call_id == "l1.sealed.1.s2"  # type: ignore[union-attr]
    counters = played.session.counters.get("seat:2")
    assert dict(counters.abstentions) == {"parse_failed": 1} and counters.actions == 1


def test_without_syntax_retries_a_bad_reply_abstains_at_once() -> None:
    settings = replace(FAST, syntax_retries=0)
    played = play([sealed()], {2: [raw(strict_call("sealed", bid=-4)), ok()]}, settings=settings)
    assert played.outcome.reason == "parse_failed" and played.outcome.attempts == 1
    assert played.sdk.call_count == 1


def test_a_reply_with_nothing_to_read_is_missing() -> None:
    played = play([sealed()], {2: [prose("我再想想。"), prose("还是想不好。")]})
    assert played.outcome.reason == "missing" and played.outcome.attempts == 2
    messages = played.sdk.requests[1]["messages"]
    assert "没有找到 act 工具调用" in messages[2]["content"]
    assert messages[1]["content"] == "我再想想。"
    assert [e.outcome for e in played.entries] == ["parse_failed", "parse_failed"]


def test_a_tool_call_with_no_text_is_echoed_as_its_arguments() -> None:
    played = play([sealed()], {2: [raw({"phase": "sealed"}), ok()]})
    echoed = played.sdk.requests[1]["messages"][1]["content"]
    assert echoed and echoed.startswith("{") and "sealed" in echoed


def test_text_that_holds_the_action_is_read_and_the_repair_is_recorded() -> None:
    reply = prose('好的：```json\n{"phase": "sealed", "bid": 55, "thought": "按估值"}\n```')
    played = play([sealed()], {2: [reply]})
    assert (
        played.outcome.ok and played.outcome.args is not None and played.outcome.args["bid"] == 55
    )
    assert played.codes() == [["syntax"]]  # JSON in text is a repair for a tool-call endpoint


def test_text_arguments_of_an_openai_endpoint_are_repaired_not_retried() -> None:
    call = make_call(api_config(), 1, "sealed")
    text = '{"phase": "sealed", "bid": 50, "thought": "x",}'
    played = play([call], {1: [raw(text)]})
    assert played.outcome.ok and played.outcome.attempts == 1
    assert played.outcome.args is not None and played.outcome.args["bid"] == 50
    assert played.codes() == [["syntax"]]


# ================================================================= refusals


def test_an_api_refusal_is_never_retried() -> None:
    played = play([sealed()], {2: [api_refusal(), ok()]})
    outcome = played.outcome
    assert not outcome.ok and outcome.reason == "refusal" and outcome.attempts == 1
    assert played.sdk.call_count == 1
    (entry,) = played.entries
    assert entry.outcome == "refusal" and entry.refusal is True and entry.stop_reason == "refusal"
    assert entry.cost_microusd == 50  # a refusal can be billed, and is counted
    assert played.codes() == [["refusal"]]
    assert played.result.events[0].data.status == "abstain"  # type: ignore[union-attr]
    assert played.result.events[0].data.detail == "reason=refusal"  # type: ignore[union-attr]
    counters = played.session.counters.get("seat:2")
    assert counters.refusals == 1 and counters.other_abstentions == 0


def test_a_reply_that_reads_like_a_refusal_is_not_retried_either() -> None:
    played = play([sealed()], {2: [prose("抱歉，我无法协助这个请求。"), ok()]})
    assert played.outcome.reason == "refusal" and played.outcome.attempts == 1
    assert played.entries[0].outcome == "refusal" and played.entries[0].refusal is False


def test_an_openai_refusal_is_flagged_too() -> None:
    call = make_call(api_config(), 4, "sealed")
    played = play([call], {4: [api_refusal()]})
    assert played.outcome.reason == "refusal" and played.entries[0].refusal is True


# ================================================================= timeouts


def test_a_timeout_is_not_retried_and_ends_in_an_abstention() -> None:
    played = play([sealed()], {2: [timeout_error(), ok()]})
    outcome = played.outcome
    assert not outcome.ok and outcome.reason == "timeout" and outcome.attempts == 1
    assert played.sdk.call_count == 1 and played.sleeps == []
    (entry,) = played.entries
    assert entry.outcome == "timeout" and entry.response is None and entry.usage is None
    assert entry.error is not None and entry.error["type"] == "APITimeoutError"
    assert entry.cost_microusd == 0
    assert played.codes() == [["timeout", "fallback"]]
    detail = played.result.events[0].data.detail  # type: ignore[union-attr]
    assert played.result.events[0].data.status == "abstain"  # type: ignore[union-attr]
    assert detail.startswith("reason=timeout; error=APITimeoutError")
    assert dict(played.session.counters.get("seat:2").abstentions) == {"timeout": 1}


def test_a_call_that_the_sdk_fails_to_stop_is_cut_off_by_the_wall_clock() -> None:
    settings = replace(FAST, timeout_s=0.1, wall_clock_slack_s=0.05)
    played = play([sealed()], {2: [hang(0.6)]}, settings=settings)
    assert played.outcome.reason == "timeout" and played.outcome.attempts == 1
    assert played.entries[0].error is not None and played.entries[0].error["type"] == "TimeoutError"


def test_the_call_timeout_is_handed_to_the_sdk() -> None:
    played = play([sealed()], settings=replace(FAST, timeout_s=33.0))
    assert played.sdk.requests[0]["timeout"] == 33.0


# ================================================================= transient errors


def test_a_rate_limit_is_retried_after_a_back_off() -> None:
    played = play([sealed()], {2: [rate_limit(), ok(bid=44)]})
    outcome = played.outcome
    assert outcome.ok and outcome.attempts == 2 and outcome.args is not None
    assert outcome.args["bid"] == 44
    assert [e.outcome for e in played.entries] == ["error", "ok"]
    # the same request is sent again, byte for byte
    assert played.entries[0].request_hash == played.entries[1].request_hash
    assert played.sdk.requests[0]["messages"] == played.sdk.requests[1]["messages"]
    assert len(played.sleeps) == 1 and 1.0 <= played.sleeps[0] <= 1.25
    assert played.codes() == [["retry"]]
    assert played.result.events[0].data.status == "retry"  # type: ignore[union-attr]
    assert played.result.events[0].data.detail == "error=RateLimitError; http=429"  # type: ignore[union-attr]
    assert played.entries[0].cost_microusd == 0 and outcome.cost_microusd == 50


def test_the_back_off_doubles_and_honours_retry_after() -> None:
    played = play([sealed()], {2: [rate_limit(), rate_limit(), ok()]})
    first, second = played.sleeps
    assert 1.0 <= first <= 1.25 and 2.0 <= second <= 2.5
    played = play([sealed()], {2: [rate_limit(retry_after="7"), ok()]})
    assert 7.0 <= played.sleeps[0] <= 8.75
    played = play([sealed()], {2: [rate_limit(retry_after="900"), ok()]})
    assert played.sleeps[0] <= FAST.backoff_cap_s * 1.25  # capped


def test_retries_run_out_and_the_action_becomes_an_abstention() -> None:
    played = play([sealed()], {2: [rate_limit(), rate_limit(), rate_limit(), ok()]})
    outcome = played.outcome
    assert not outcome.ok and outcome.reason == "missing" and outcome.attempts == 3
    assert [e.outcome for e in played.entries] == ["error", "error", "error"]
    assert played.codes() == [["retry"], ["retry"], ["fallback"]]
    assert len(played.sleeps) == 2 and played.sdk.call_count == 3
    last = played.result.events[-1].data
    assert last.status == "abstain"  # type: ignore[union-attr]
    assert last.detail == "reason=missing; error=RateLimitError; http=429"  # type: ignore[union-attr]
    assert [e.data.status for e in played.result.events] == ["retry", "retry", "abstain"]  # type: ignore[union-attr]
    assert dict(played.session.counters.get("seat:2").abstentions) == {"missing": 1}


@pytest.mark.parametrize("status", [500, 502, 503, 504, 529])
def test_server_errors_are_retried(status: int) -> None:
    played = play([sealed()], {2: [server_error(status), ok()]})
    assert played.outcome.ok and played.outcome.attempts == 2
    assert played.entries[0].error is not None and played.entries[0].error["status"] == status


def test_a_lost_connection_is_retried() -> None:
    played = play([sealed()], {2: [connection_error(), ok()]})
    assert played.outcome.ok and played.outcome.attempts == 2
    assert played.entries[0].error is not None and played.entries[0].error["status"] is None


@pytest.mark.parametrize("seat", [2, 1], ids=["anthropic", "openai"])
def test_a_request_the_api_refuses_is_not_retried(seat: int) -> None:
    played = play([make_call(api_config(), seat, "sealed")], {seat: [bad_request(), ok()]})
    outcome = played.outcome
    assert not outcome.ok and outcome.reason == "missing" and outcome.attempts == 1
    assert played.sdk.call_count == 1 and played.sleeps == []
    assert played.codes() == [["fallback"]]
    assert played.entries[0].error is not None and played.entries[0].error["retryable"] is False


def test_authentication_errors_are_not_retried() -> None:
    import anthropic

    step = failing(lambda kind: sdk_status_error(anthropic.AuthenticationError, 401, "bad key"))
    played = play([sealed()], {2: [step, ok()]})
    assert played.outcome.attempts == 1 and played.outcome.reason == "missing"


def test_without_transient_retries_the_first_rate_limit_is_final() -> None:
    played = play(
        [sealed()], {2: [rate_limit(), ok()]}, settings=replace(FAST, transient_retries=0)
    )
    assert played.outcome.reason == "missing" and played.outcome.attempts == 1


def test_both_kinds_of_retry_share_one_attempt_counter() -> None:
    script = {2: [rate_limit(), raw(strict_call("sealed", bid=-1)), ok(bid=33)]}
    played = play([sealed()], script)
    assert played.outcome.ok and played.outcome.attempts == 3
    assert [e.key.attempt for e in played.entries] == [1, 2, 3]
    assert [e.outcome for e in played.entries] == ["error", "parse_failed", "ok"]
    # the transient retry resent the request; the syntax retry extended it
    assert played.entries[0].request_hash == played.entries[1].request_hash
    assert played.entries[1].request_hash != played.entries[2].request_hash
    assert [r["messages"][-1]["role"] for r in played.sdk.requests] == ["user", "user", "user"]
    assert len(played.sdk.requests[2]["messages"]) == 3
    # the ladder knows it has used its one syntax retry: a further bad reply would abstain
    again = play([sealed()], {2: [rate_limit(), raw(strict_call("sealed", bid=-1)), raw({})]})
    assert again.outcome.reason in ("parse_failed", "missing") and again.outcome.attempts == 3


def test_settings_keep_the_attempt_count_within_the_event_contract() -> None:
    assert RunnerSettings(syntax_retries=3, transient_retries=5).syntax_retries == 3
    assert RunnerSettings(syntax_retries=4, transient_retries=5).transient_retries == 5  # 10
    with pytest.raises(ValueError, match="at most"):
        RunnerSettings(syntax_retries=4, transient_retries=6)
    with pytest.raises(ValueError):
        RunnerSettings(timeout_s=0)
    limits = api_config().limits
    assert RunnerSettings.from_limits(limits) == RunnerSettings(timeout_s=90.0)


# ================================================================= one phase at once


def sealed_calls(seats: Sequence[int], **kw: Any) -> list[ModelCall]:
    return [make_call(api_config(), seat, "sealed", **kw) for seat in seats]


def test_answers_come_back_in_seat_order_whatever_the_arrival_order() -> None:
    seats = [3, 1, 4, 2]
    script = {seat: [ok(bid=500)] for seat in seats}  # every seat is clamped: one event each
    for arrival in ([4, 2, 3, 1], [1, 2, 3, 4], [2, 4, 1, 3]):
        played = play(sealed_calls(seats), script_copy(script), gate=ArrivalGate(arrival))
        assert [a[0] for a in played.sdk.arrivals] == arrival
        assert [o.seat for o in played.result.outcomes] == [1, 2, 3, 4]
        assert [e.data.seat for e in played.result.events] == [1, 2, 3, 4]  # type: ignore[union-attr]
        assert [e.key.seat for e in played.cassette.entries] == [1, 2, 3, 4]


def script_copy(script: dict[int, list[Step]]) -> dict[int, list[Step]]:
    return {seat: list(steps) for seat, steps in script.items()}


def test_calls_of_different_endpoints_overlap() -> None:
    script = {seat: [hang(0.2)] for seat in (1, 2, 3, 4)}
    played = play(sealed_calls([1, 2, 3, 4]), script)
    assert played.sdk.peak_in_flight == 4
    assert all(o.ok for o in played.result.outcomes)


def test_one_endpoint_never_has_more_calls_in_flight_than_its_limit() -> None:
    config = api_config()
    # four openai-style seats, all sent to one endpoint that allows two calls at a time
    calls = [replace(c, endpoint="vendor_a") for c in sealed_calls([1, 4, 5, 6])]
    script = {seat: [hang(0.15)] for seat in (1, 4, 5, 6)}
    sdk = ScriptedSdk(script)
    runner = ModelRunner(clients_for(config, sdk), clock=lambda: FIXED_TIME)
    session = NightSession.open(config, Cassette.record(), prices=example_prices(), settings=FAST)
    limited = config.endpoints["vendor_a"].model_copy(update={"max_concurrency": 2})
    session.endpoints = {**config.endpoints, "vendor_a": limited}
    result = asyncio.run(runner.run_phase(session, PhaseRequest(tuple(calls))))
    runner.close()
    assert all(o.ok for o in result.outcomes) and len(result.outcomes) == 4
    assert sdk.peak_in_flight == 2 and runner.limiter("vendor_a", limited).peak == 2


def test_a_sequential_phase_asks_one_seat_after_the_other_in_the_given_order() -> None:
    order = [3, 1, 2]
    script = {seat: [hang(0.05)] for seat in order}
    calls = [make_call(api_config(), seat, "talk", step=n) for n, seat in enumerate(order, 1)]
    played = play(calls, script, sequential=True)
    assert [s[0] for s in played.sdk.starts] == order and played.sdk.peak_in_flight == 1
    assert [o.seat for o in played.result.outcomes] == [1, 2, 3]
    # each call is committed as it finishes, so the file follows the order of the calls
    assert [(e.key.step, e.key.seat) for e in played.cassette.entries] == [(1, 3), (2, 1), (3, 2)]


def test_the_rate_limit_spaces_the_starts() -> None:
    now = [0.0]
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    limiter = EndpointLimiter(4, 30, clock=lambda: now[0], sleep=sleep)  # one start per 2 s
    assert limiter.interval_s == 2.0

    async def three() -> list[float]:
        starts = []
        for _ in range(3):
            async with limiter:
                starts.append(now[0])
        return starts

    assert asyncio.run(three()) == [0.0, 2.0, 4.0] and sleeps == [2.0, 2.0]
    assert EndpointLimiter(4).interval_s == 0.0


def test_a_phase_without_calls_is_empty() -> None:
    played = play([])
    assert played.result.outcomes == () and played.result.events == ()


@pytest.mark.parametrize("sequential", [False, True])
def test_a_phase_with_calls_of_another_night_or_a_repeated_seat_is_refused(
    sequential: bool,
) -> None:
    config = api_config()
    foreign = make_call(config, 2, "sealed", night="another-night")
    with pytest.raises(ValueError, match="another night"):
        play([foreign], sequential=sequential)
    with pytest.raises(ValueError, match="twice"):
        play([sealed(), sealed(step=2)], sequential=sequential)


def test_an_unknown_endpoint_or_a_missing_client_is_a_programming_error() -> None:
    with pytest.raises(KeyError, match="unknown endpoint"):
        play([replace(sealed(), endpoint="nowhere")])
    config = api_config()
    runner = ModelRunner({})
    session = NightSession.open(config, Cassette.record(), prices=example_prices(), settings=FAST)
    with pytest.raises(KeyError, match="no client"):
        asyncio.run(runner.run_phase(session, PhaseRequest((sealed(),))))
    runner.close()


def test_a_failure_in_one_call_stops_the_phase_with_the_first_error_in_call_order() -> None:
    config = api_config()
    runner = ModelRunner({"vendor_a": AnthropicClient(ScriptedSdk())})  # no client for anthropic
    session = NightSession.open(config, Cassette.record(), prices=example_prices(), settings=FAST)
    calls = tuple(sealed_calls([3, 2]))  # both anthropic: both fail with a missing client
    with pytest.raises(KeyError, match="no client"):
        asyncio.run(runner.run_phase(session, PhaseRequest(calls)))
    runner.close()


def test_a_runner_can_be_made_from_a_configuration() -> None:
    config = api_config()
    environ = {
        name: "value-" + name.lower()
        for name in (
            "ANTHROPIC" + "_API_" + "KEY",
            "VENDOR_A" + "_API_" + "KEY",
            "VENDOR_B" + "_API_" + "KEY",
        )
    }
    environ.update({"VENDOR_A_BASE_URL": "http://127.0.0.1:1/v1"})
    environ.update({"VENDOR_B_BASE_URL": "http://127.0.0.1:2/v1"})
    sdk = ScriptedSdk()
    made = {"anthropic": lambda **kw: sdk, "openai_compatible": lambda **kw: sdk}
    runner = ModelRunner.from_config(
        config,
        environ=environ,
        factories=made,  # type: ignore[arg-type]
        endpoints=["anthropic", "vendor_a", "vendor_b"],
        clock=lambda: FIXED_TIME,
    )
    session = NightSession.open(config, Cassette.record(), prices=example_prices(), settings=FAST)
    calls = tuple(sealed_calls([1, 2, 5]))
    result = asyncio.run(runner.run_phase(session, PhaseRequest(calls)))
    runner.close()
    assert [o.ok for o in result.outcomes] == [True, True, True] and sdk.call_count == 3


# ================================================================= the whole night


class Script:
    """A minimal driver: it hands out prepared phases and keeps what comes back."""

    def __init__(self, night: str, phases: Sequence[PhaseRequest]) -> None:
        self.night = night
        self._phases = list(phases)
        self.results: list[PhaseResult] = []
        self.on_start: Any = None

    def next_phase(self) -> PhaseRequest | None:
        if self.on_start is not None:
            self.on_start()
            self.on_start = None
        return self._phases.pop(0) if self._phases else None

    def submit(self, result: PhaseResult) -> None:
        self.results.append(result)


def test_a_night_runs_until_the_driver_has_no_more_phases() -> None:
    config = api_config()
    phases = [
        PhaseRequest(tuple(sealed_calls([2, 3], step=1))),
        PhaseRequest((make_call(config, 2, "talk", step=2),), sequential=True),
    ]
    driver = Script(config.night.id, phases)
    sdk = ScriptedSdk({2: [rate_limit(), ok(bid=500), ok()]})
    runner = ModelRunner(clients_for(config, sdk), clock=lambda: FIXED_TIME, sleep=_no_sleep)
    session = NightSession.open(config, Cassette.record(), prices=example_prices(), settings=FAST)
    run = asyncio.run(runner.run_night(driver, session))
    runner.close()
    assert len(driver.results) == 2 and run.status == "completed"
    assert run.night == config.night.id and run.calls == 4 == sdk.call_count
    final = run.events[-1]
    assert final.type == "cost_event" and final.data.kind == "tally"  # type: ignore[union-attr]
    assert final.data.spent_microusd == run.spent_microusd  # type: ignore[union-attr]
    kinds = [e.type for e in run.events]
    assert kinds.count("parse_repair") == 2 and kinds[-1] == "cost_event"
    assert run.summary["total"]["calls"] == 4 and "seat:2" in run.summary["by_role"]
    assert run.counters["seat:2"]["actions"] == 2


async def _no_sleep(seconds: float) -> None:
    return None


def test_system_events_become_log_events_with_the_system_level() -> None:
    played = play([sealed()], {2: [rate_limit(), ok()]})
    event = played.result.events[0].to_event(41)
    assert (event.seq, event.lot, event.vis, event.type) == (41, 1, "system", "parse_repair")
    assert event.data.role == "seat:2" and event.data.seat == 2


# ================================================================= several nights at once


def config_for(night: str) -> NightConfig:
    config = api_config()
    return config.model_copy(update={"night": config.night.model_copy(update={"id": night})})


def run_many(
    nights: Sequence[str],
    *,
    max_parallel: int,
    sdk: ScriptedSdk,
    drivers: dict[str, Any] | None = None,
    configs: dict[str, NightConfig] | None = None,
) -> tuple[list[Any], ModelRunner]:
    runner = ModelRunner(clients_for(api_config(), sdk), clock=lambda: FIXED_TIME, sleep=_no_sleep)
    jobs = []
    for night in nights:
        config = (configs or {}).get(night) or config_for(night)
        driver = (drivers or {}).get(night) or FakeNight(config, lots=1)
        session = NightSession.open(
            config, Cassette.record(), prices=example_prices(), settings=FAST
        )
        jobs.append((driver, session))
    results = asyncio.run(runner.run_nights(jobs, max_parallel=max_parallel))
    runner.close()
    return results, runner


def test_several_nights_run_in_parallel_under_a_global_limit() -> None:
    active = {"now": 0, "peak": 0}
    lock = threading.Lock()

    class Counting(FakeNight):
        def next_phase(self) -> PhaseRequest | None:
            phase = super().next_phase()
            with lock:
                if phase is not None and not getattr(self, "_began", False):
                    self._began = True
                    active["now"] += 1
                    active["peak"] = max(active["peak"], active["now"])
                elif phase is None:
                    active["now"] -= 1
            return phase

    nights = ["n1", "n2", "n3", "n4"]
    drivers = {n: Counting(config_for(n), lots=1) for n in nights}
    sdk = ScriptedSdk({seat: [hang(0.02)] * 6 for seat in (1, 2, 3, 4)})
    results, _ = run_many(nights, max_parallel=2, sdk=sdk, drivers=drivers)
    assert [r.night for r in results] == nights  # input order
    assert all(r.status == "completed" for r in results)
    assert active["peak"] == 2
    unlimited = run_many(nights, max_parallel=4, sdk=ScriptedSdk(), drivers=_fresh(nights))
    assert [r.night for r in unlimited[0]] == nights


def _fresh(nights: Sequence[str]) -> dict[str, Any]:
    return {n: FakeNight(config_for(n), lots=1) for n in nights}


def test_a_failing_night_does_not_stop_the_others() -> None:
    class Broken(FakeNight):
        def submit(self, result: PhaseResult) -> None:
            raise RuntimeError("the driver broke")

    drivers = {"n1": FakeNight(config_for("n1"), lots=1), "n2": Broken(config_for("n2"), lots=1)}
    results, _ = run_many(["n1", "n2"], max_parallel=2, sdk=ScriptedSdk(), drivers=drivers)
    assert results[0].status == "completed"
    assert isinstance(results[1], RuntimeError) and "driver broke" in str(results[1])
    with pytest.raises(ValueError, match="at least 1"):
        asyncio.run(ModelRunner().run_nights([], max_parallel=0))


def test_nights_in_parallel_share_the_limits_of_an_endpoint() -> None:
    config = api_config()
    limited = config.endpoints["vendor_a"].model_copy(update={"max_concurrency": 1})
    endpoints = {**config.endpoints, "vendor_a": limited}
    sdk = ScriptedSdk({seat: [hang(0.03)] * 6 for seat in (1, 4)})
    runner = ModelRunner(clients_for(config, sdk), clock=lambda: FIXED_TIME, sleep=_no_sleep)
    jobs = []
    for night in ("n1", "n2", "n3"):
        session = NightSession.open(
            config_for(night), Cassette.record(), prices=example_prices(), settings=FAST
        )
        session.endpoints = endpoints
        jobs.append((FakeNight(config_for(night), seats=(1, 4), lots=1), session))
    results = asyncio.run(runner.run_nights(jobs, max_parallel=3))
    runner.close()
    assert all(not isinstance(r, BaseException) for r in results)
    assert runner.limiter("vendor_a", limited).peak == 1


# ================================================================= the real renderer


def test_a_rendered_prompt_goes_through_the_runner(
    night: Night, events: list[AnyEvent], api_config: NightConfig
) -> None:
    from types import SimpleNamespace

    from sdk_fakes import anthropic_message

    settings = make_settings()
    seat, seq = 3, night.marks["lot6_open"]
    system = render_system(events, seat, settings)
    observation = render_observation(events, seat, "sealed", settings, as_of_seq=seq)  # type: ignore[arg-type]
    seat_cfg = next(s for s in api_config.seats if s.seat == seat)
    built = build_request(
        api_config.endpoints["anthropic"],
        seat_cfg,
        system=system,
        messages=[user_message(observation)],
    )
    ctx = parse_context(events, seat, "sealed", settings, as_of_seq=seq)  # type: ignore[arg-type]
    call = ModelCall(CallKey(api_config.night.id, 6, "sealed", 17, seat), "anthropic", built, ctx)
    message = anthropic_message(valid_args("sealed", bid=500), model="claude-sonnet-5-5")
    sdk = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: message))
    runner = ModelRunner({"anthropic": AnthropicClient(sdk)}, clock=lambda: FIXED_TIME)
    session = NightSession.open(
        api_config, Cassette.record(), prices=example_prices(), settings=FAST
    )
    result = asyncio.run(runner.run_phase(session, PhaseRequest((call,))))
    runner.close()
    outcome = result.outcomes[0]
    assert outcome.ok and outcome.result is not None
    assert (outcome.result.raw_value, outcome.result.value) == (500, 142)  # seat 3 has 142 left
    assert [a.kind for a in outcome.result.adjustments] == ["budget_clamp"]
    assert outcome.model == "claude-sonnet-5-5"
    # priced as sonnet, the model the reply names: 120 x 2 + 60 x 10 + 800 x 0.20
    assert outcome.cost_microusd == 1000
