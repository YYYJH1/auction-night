"""The parsing ladder: malformed output, repairs, coercion, validation, retry and abstention."""

import json
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from llm_replies import (
    ACT,
    BID_120_TEXTS,
    NOT_REFUSALS,
    NOTHING_TO_READ,
    REFUSALS,
    ctx,
    dumps,
    sealed,
    strict_call,
    text_reply,
    tool_reply,
)

from auction_night.contracts.act import FIELD_NAMES, PHASE_TABLE, validate_act
from auction_night.contracts.events import Pledge
from auction_night.contracts.mechanism import PHASES
from auction_night.llm.amounts import amount_from_text, chinese_to_int
from auction_night.llm.jsonish import read_object
from auction_night.llm.parse import (
    ParseContext,
    RawReply,
    ToolCall,
    abstain_status,
    explain,
    looks_like_refusal,
    parse_reply,
    reply_from_anthropic,
    reply_from_openai,
    retry_message,
)
from auction_night.llm.sanitize import TextLimits, clean_say, has_structure

# --------------------------------------------------------------- one bid, many spellings


@pytest.mark.parametrize("name", BID_120_TEXTS)
def test_every_spelling_of_the_same_bid_is_read_as_120(name: str) -> None:
    result = parse_reply(text_reply(BID_120_TEXTS[name]), ctx())
    assert result.status == "ok", (name, result.errors)
    assert result.args is not None
    assert result.args["bid"] == 120 and result.value == 120
    assert result.args["phase"] == "sealed"
    assert result.args["thought"].startswith(("按估值", "他说"))
    assert not result.adjustments and not result.errors
    assert list(result.args) == list(FIELD_NAMES)  # tool field order, unused fields null
    assert result.args["say"] is None and result.args["exit_price"] is None


def test_clean_json_needs_no_repair() -> None:
    trace = parse_reply(text_reply(BID_120_TEXTS["clean"]), ctx()).trace
    assert (trace.source, trace.syntax_fixes, trace.coercions) == ("text", (), ())
    assert not trace.fenced and not trace.prose and not trace.unwrapped


@pytest.mark.parametrize(
    ("name", "fixes"),
    [
        ("fullwidth", {"fullwidth", "smart_quotes"}),
        ("fullwidth_braces_only", {"fullwidth"}),
        ("smart_quotes", {"smart_quotes"}),
        ("single_quotes", {"single_quotes"}),
        ("trailing_comma", {"trailing_comma"}),
        ("missing_closing_brace", {"unclosed"}),
        ("missing_closing_quote_and_brace", {"unclosed"}),
        ("unquoted_keys", {"unquoted"}),
        ("python_literals", {"python_literal"}),
        ("line_comment", {"comment"}),
        ("equals_instead_of_colon", {"equals_colon"}),
        ("inner_quotes", {"stray_quote"}),
        ("bare_unit_value", {"bare_value"}),
    ],
)
def test_syntax_repairs_are_named(name: str, fixes: set[str]) -> None:
    trace = parse_reply(text_reply(BID_120_TEXTS[name]), ctx()).trace
    assert set(trace.syntax_fixes) == fixes


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("chinese_numeral", "text_number"),
        ("unit_suffix", "text_number"),
        ("number_string", "text_number"),
        ("float", "float"),
        ("float_rounds_down", "float"),
        ("fullwidth_digits", "text_number"),
        ("currency", "text_number"),
        ("colloquial_numeral", "text_number"),
    ],
)
def test_amount_coercions_are_named(name: str, kind: str) -> None:
    trace = parse_reply(text_reply(BID_120_TEXTS[name]), ctx()).trace
    assert [(c.field, c.kind) for c in trace.coercions] == [("bid", kind)]


def test_fences_prose_wrappers_and_filled_fields_are_traced() -> None:
    fenced = parse_reply(text_reply(BID_120_TEXTS["fenced"]), ctx()).trace
    assert fenced.fenced and not fenced.prose
    both = parse_reply(text_reply(BID_120_TEXTS["prose_both_and_fence"]), ctx()).trace
    assert both.fenced and both.prose
    after = parse_reply(text_reply(BID_120_TEXTS["prose_after"]), ctx()).trace
    assert after.prose and not after.fenced
    assert parse_reply(text_reply(BID_120_TEXTS["wrapper"]), ctx()).trace.unwrapped
    assert parse_reply(
        text_reply(BID_120_TEXTS["wrapper_with_text_arguments"]), ctx()
    ).trace.unwrapped
    left_out = parse_reply(text_reply(BID_120_TEXTS["phase_left_out"]), ctx()).trace
    assert left_out.filled == ("phase",)
    keys = parse_reply(text_reply(BID_120_TEXTS["capitalised_keys"]), ctx()).trace
    assert {(c.field, c.kind) for c in keys.coercions} == {
        ("phase", "key_name"),
        ("bid", "key_name"),
        ("thought", "key_name"),
    }


def test_the_first_object_wins_and_reasoning_is_set_aside() -> None:
    assert parse_reply(text_reply(BID_120_TEXTS["two_objects_first_wins"]), ctx()).value == 120
    assert parse_reply(text_reply(BID_120_TEXTS["think_block_first"]), ctx()).value == 120
    assert parse_reply(text_reply(BID_120_TEXTS["closing_think_only"]), ctx()).value == 120


def test_camel_case_keys_are_read() -> None:
    reply = text_reply(dumps({"phase": "english_init", "exitPrice": 140, "thought": "x"}))
    result = parse_reply(reply, ctx("english_init"))
    assert result.ok and result.args is not None and result.args["exit_price"] == 140


# ------------------------------------------------------------------ tool call arguments


def test_tool_call_arguments_are_the_native_path() -> None:
    result = parse_reply(tool_reply(strict_call("sealed", bid=120)), ctx())
    assert result.ok and result.value == 120
    trace = result.trace
    assert (trace.source, trace.act_calls) == ("tool_call", 1)
    assert not trace.syntax_fixes and not trace.coercions and not trace.arguments_as_text


def test_tool_call_arguments_as_json_text() -> None:
    clean = parse_reply(tool_reply(dumps(strict_call("sealed", bid=120))), ctx())
    assert clean.ok and clean.trace.arguments_as_text and not clean.trace.syntax_fixes
    broken = dumps(strict_call("sealed", bid=120))[:-1] + ",}"
    repaired = parse_reply(tool_reply(broken), ctx())
    assert repaired.ok and repaired.trace.syntax_fixes == ("trailing_comma",)


def test_a_tool_call_wins_over_text() -> None:
    reply = RawReply(
        tool_calls=(ToolCall("act", strict_call("sealed", bid=77)),),
        text=dumps(sealed(bid=999)),
    )
    assert parse_reply(reply, ctx()).value == 77


def test_other_tools_are_ignored_and_the_text_is_used() -> None:
    reply = RawReply(tool_calls=(ToolCall("search", {"query": "x"}),), text=dumps(sealed(bid=55)))
    result = parse_reply(reply, ctx())
    assert result.value == 55 and result.trace.source == "text" and result.trace.act_calls == 0


def test_the_first_usable_of_several_act_calls_is_taken() -> None:
    reply = RawReply(
        tool_calls=(
            ToolCall("act", "not json at all"),
            ToolCall("act", strict_call("sealed", bid=31)),
            ToolCall("act", strict_call("sealed", bid=32)),
        )
    )
    result = parse_reply(reply, ctx())
    assert result.value == 31 and result.trace.act_calls == 3


def test_unusable_tool_arguments_fall_back_to_the_text() -> None:
    reply = RawReply(tool_calls=(ToolCall("act", None),), text=dumps(sealed(bid=44)))
    assert parse_reply(reply, ctx()).value == 44


# ---------------------------------------------------------------------------- legality


def test_a_bid_above_the_budget_is_a_legal_clamp_not_an_error() -> None:
    result = parse_reply(text_reply(dumps(sealed(bid=500))), ctx(budget_left=342))
    assert result.ok and not result.errors
    assert (result.raw_value, result.value) == (500, 342)
    [adjustment] = result.adjustments
    assert (adjustment.kind, adjustment.field, adjustment.raw, adjustment.effective) == (
        "budget_clamp",
        "bid",
        500,
        342,
    )
    assert result.args is not None and result.args["bid"] == 500  # the model's number is kept


def test_a_bid_at_the_budget_is_not_clamped() -> None:
    result = parse_reply(text_reply(dumps(sealed(bid=342))), ctx(budget_left=342))
    assert result.ok and result.adjustments == ()


def test_clamps_need_a_known_budget() -> None:
    result = parse_reply(text_reply(dumps(sealed(bid=900000))), ctx(budget_left=None))
    assert result.ok and result.adjustments == () and result.value == 900000


@pytest.mark.parametrize("phase", ["english_init", "english_revise"])
def test_english_exit_prices_are_clamped_to_the_budget(phase: str) -> None:
    call = {"phase": phase, "exit_price": 400, "thought": "x"}
    result = parse_reply(text_reply(dumps(call)), ctx(phase, budget_left=250))  # type: ignore[arg-type]
    assert [a.kind for a in result.adjustments] == ["budget_clamp"] and result.value == 250


def test_a_dutch_grab_price_above_the_budget_is_not_clamped() -> None:
    call = {"phase": "dutch", "grab_price": 380, "thought": "x"}
    result = parse_reply(text_reply(dumps(call)), ctx("dutch", budget_left=250))
    assert result.ok and result.adjustments == () and result.value == 380


def test_an_english_revision_below_the_price_means_leaving() -> None:
    call = {"phase": "english_revise", "exit_price": 80, "thought": "x"}
    result = parse_reply(text_reply(dumps(call)), ctx("english_revise", current_price=98))
    assert result.ok and not result.errors
    [adjustment] = result.adjustments
    assert (adjustment.kind, adjustment.raw, adjustment.effective) == (
        "below_current_price",
        80,
        80,
    )
    same = parse_reply(
        text_reply(dumps({**call, "exit_price": 98})), ctx("english_revise", current_price=98)
    )
    assert same.adjustments == ()


def test_a_negative_bid_is_invalid_not_a_clamp() -> None:
    result = parse_reply(text_reply(dumps(sealed(bid=-5))), ctx(), max_attempts=1)
    assert result.status == "abstain" and result.reason == "parse_failed"
    assert [(e.field, e.code) for e in result.errors] == [("bid", "out_of_range")]
    assert result.adjustments == () and result.args is None


# ----------------------------------------------------------------------- never a default


@pytest.mark.parametrize(
    "call",
    [
        {"phase": "sealed", "thought": "忘了出价"},
        {"phase": "sealed", "bid": None, "thought": "x"},
        {"phase": "sealed", "bid": "null", "thought": "x"},
        {"phase": "sealed", "bid": "", "thought": "x"},
        {"phase": "sealed", "bid": "抢", "thought": "x"},
        {"phase": "sealed", "bid": True, "thought": "x"},
        {"phase": "sealed", "bid": "很多", "thought": "x"},
        {"phase": "sealed", "bid": [120], "thought": "x"},
        {"phase": "sealed", "bid": {"price": 120}, "thought": "x"},
        {"phase": "sealed", "bid": "100到120", "thought": "x"},
        {"phase": "sealed", "bid": "NaN", "thought": "x"},
    ],
)
def test_a_missing_or_hopeless_bid_never_becomes_a_price(call: dict[str, Any]) -> None:
    first = parse_reply(text_reply(dumps(call)), ctx())
    assert first.status == "retry" and first.args is None
    last = parse_reply(text_reply(dumps(call)), ctx(), attempt=2)
    assert last.status == "abstain" and last.reason == "parse_failed"
    assert last.args is None and last.value is None


@pytest.mark.parametrize(
    "word", ["不出", "不出价", "否", "等", "放弃", "弃权", "pass", "no", False]
)
def test_an_explicit_no_is_zero_which_means_not_taking_part(word: Any) -> None:
    result = parse_reply(text_reply(dumps(sealed(bid=word))), ctx())
    assert result.ok and result.value == 0
    assert [c.kind for c in result.trace.coercions] == ["zero_word"]


def test_empty_output_leads_to_a_retry_and_then_an_abstention() -> None:
    first = parse_reply(text_reply(""), ctx())
    assert (first.status, first.retry_message) == (
        "retry",
        "上一次回复里没有找到 act 工具调用。请重新调用一次 act 工具：phase 填 sealed，"
        "只填该阶段需要的字段，其余填 null。",
    )
    last = parse_reply(text_reply(""), ctx(), attempt=2)
    assert (last.status, last.reason) == ("abstain", "missing")


@pytest.mark.parametrize("name", NOTHING_TO_READ)
def test_replies_without_an_action_end_in_an_abstention(name: str) -> None:
    text = NOTHING_TO_READ[name]
    assert parse_reply(text_reply(text), ctx()).status == "retry"
    last = parse_reply(text_reply(text), ctx(), attempt=2)
    assert last.status == "abstain" and last.reason in ("missing", "parse_failed")
    assert last.args is None and last.value is None


def test_an_empty_object_is_a_failed_parse_and_prose_is_a_missing_action() -> None:
    empty = parse_reply(text_reply("{}"), ctx(), attempt=2)
    assert empty.reason == "parse_failed" and {e.field for e in empty.errors} >= {"bid", "thought"}
    prose = parse_reply(text_reply(NOTHING_TO_READ["prose_only"]), ctx(), attempt=2)
    assert prose.reason == "missing"


def test_retries_are_bounded() -> None:
    reply = text_reply("")
    assert parse_reply(reply, ctx(), attempt=1, max_attempts=1).status == "abstain"
    assert parse_reply(reply, ctx(), attempt=1, max_attempts=3).status == "retry"
    assert parse_reply(reply, ctx(), attempt=2, max_attempts=3).status == "retry"
    assert parse_reply(reply, ctx(), attempt=3, max_attempts=3).status == "abstain"


# ---------------------------------------------------------------------------- refusals


@pytest.mark.parametrize("name", REFUSALS)
def test_a_refusal_abstains_at_once_without_a_retry(name: str) -> None:
    result = parse_reply(text_reply(REFUSALS[name]), ctx())
    assert (result.status, result.reason) == ("abstain", "refusal")
    assert result.retry_message is None


def test_an_api_level_refusal_abstains_even_when_text_follows() -> None:
    reply = RawReply(text=dumps(sealed()), refusal=True, stop_reason="refusal")
    result = parse_reply(reply, ctx())
    assert (result.status, result.reason) == ("abstain", "refusal")


def test_a_sentence_that_starts_like_a_refusal_but_holds_an_action_is_read() -> None:
    result = parse_reply(text_reply(NOT_REFUSALS["action_follows"]), ctx())
    assert result.ok and result.value == 120


@pytest.mark.parametrize("text", ["我出 120", "好的，成交", "Sure, here is my bid", ""])
def test_ordinary_text_does_not_look_like_a_refusal(text: str) -> None:
    assert not looks_like_refusal(text)


# --------------------------------------------------------------------- phase checks


def test_a_wrong_phase_is_invalid_and_named_in_the_retry() -> None:
    result = parse_reply(text_reply(dumps(sealed(phase="talk"))), ctx())
    assert result.status == "retry"
    assert [(e.field, e.code) for e in result.errors] == [("phase", "phase_mismatch")]
    assert result.retry_message is not None and "phase" in result.retry_message


def test_the_retry_message_names_every_violation() -> None:
    reply = text_reply(dumps({"phase": "sealed", "bid": -3}))
    message = parse_reply(reply, ctx()).retry_message
    assert message is not None
    assert "bid 超出范围" in message and "thought 缺失，本阶段必填" in message
    assert message.endswith(
        "请重新调用一次 act 工具：phase 填 sealed，只填该阶段需要的字段，其余填 null。"
    )


def test_retry_message_without_errors_or_with_nothing_found() -> None:
    assert "无法读成一个 JSON 对象" in retry_message("sealed", [], nothing_found=False)
    assert "没有找到 act 工具调用" in retry_message("dutch", [], nothing_found=True)


def test_explain_adds_the_validators_detail() -> None:
    result = parse_reply(text_reply(dumps(sealed(bid=-3))), ctx())
    [error] = result.errors
    assert explain(error) == f"bid 超出范围（{error.message}）"


def test_abstaining_in_an_english_revision_keeps_the_previous_exit_price() -> None:
    assert abstain_status("english_revise") == "kept_previous"
    for phase in ("plan", "talk", "english_init", "dutch", "sealed"):
        assert abstain_status(phase) == "abstain"  # type: ignore[arg-type]


# ----------------------------------------------------------------------------- talk


def talk(**fields: Any) -> dict[str, Any]:
    call = {"phase": "talk", "say": "大家好", "guess_budget": 180, "thought": "试探。"}
    call.update(fields)
    return call


def test_a_talk_call_with_a_pledge() -> None:
    call = talk(pledge_kind="max_bid", pledge_lot=6, pledge_amount=80)
    result = parse_reply(text_reply(dumps(call)), ctx("talk"))
    assert result.ok
    assert result.pledge == Pledge(kind="max_bid", lot=6, amount=80)
    assert result.args is not None and result.args["guess_budget"] == 180


def test_pledge_kinds_may_be_written_in_chinese() -> None:
    cases = {
        "不出价": "no_bid",
        "出价不超过": "max_bid",
        "让某人拿下": "let_win",
        "No Bid": "no_bid",
    }
    for written, kind in cases.items():
        extra = {"max_bid": {"pledge_amount": 50}, "let_win": {"pledge_to": 5}}.get(kind, {})
        call = talk(pledge_kind=written, pledge_lot=6, **extra)
        result = parse_reply(text_reply(dumps(call)), ctx("talk"))
        assert result.ok, (written, result.errors)
        assert result.pledge is not None and result.pledge.kind == kind
        assert ("pledge_kind", "enum") in {(c.field, c.kind) for c in result.trace.coercions}


def test_an_inconsistent_pledge_is_invalid() -> None:
    result = parse_reply(
        text_reply(dumps(talk(pledge_kind="max_bid", pledge_lot=6))), ctx("talk"), attempt=2
    )
    assert result.reason == "parse_failed"
    assert [(e.field, e.code) for e in result.errors] == [("pledge_amount", "inconsistent_pledge")]


def test_a_pledge_to_oneself_and_a_past_lot_are_invalid() -> None:
    selfish = talk(pledge_kind="let_win", pledge_lot=7, pledge_to=3)
    assert parse_reply(text_reply(dumps(selfish)), ctx("talk")).errors[0].field == "pledge_to"
    past = talk(pledge_kind="no_bid", pledge_lot=2)
    assert parse_reply(text_reply(dumps(past)), ctx("talk")).errors[0].field == "pledge_lot"


def test_staying_silent_is_allowed() -> None:
    for say in (None, ""):
        result = parse_reply(text_reply(dumps(talk(say=say))), ctx("talk"))
        assert result.ok and result.args is not None and result.args["say"] == ""
    no_key = {k: v for k, v in talk().items() if k != "say"}
    result = parse_reply(text_reply(dumps(no_key)), ctx("talk"))
    assert result.ok and result.trace.filled == ("say",)


def test_a_guess_may_be_text() -> None:
    result = parse_reply(text_reply(dumps(talk(guess_budget="约 180 金币"))), ctx("talk"))
    assert result.ok and result.args is not None and result.args["guess_budget"] == 180


def test_a_missing_guess_is_invalid() -> None:
    call = {k: v for k, v in talk().items() if k != "guess_budget"}
    result = parse_reply(text_reply(dumps(call)), ctx("talk"))
    assert [(e.field, e.code) for e in result.errors] == [("guess_budget", "missing_required")]


def test_a_missing_thought_is_invalid() -> None:
    call = {k: v for k, v in talk().items() if k != "thought"}
    result = parse_reply(text_reply(dumps(call)), ctx("talk"))
    assert [(e.field, e.code) for e in result.errors] == [("thought", "missing_required")]


# ----------------------------------------------------------------------------- plan


def plan(**fields: Any) -> dict[str, Any]:
    call = {
        "phase": "plan",
        "plan_priorities": [0, 1, 2, 3] * 3,
        "plan_budget": [10 * i for i in range(12)],
        "plan_persona": "装作只对甜品感兴趣",
        "thought": "先藏好底牌。",
    }
    call.update(fields)
    return call


def test_a_plan_with_lists() -> None:
    result = parse_reply(text_reply(dumps(plan())), ctx("plan", budget_left=None))
    assert result.ok and result.args is not None
    assert result.args["plan_priorities"] == [0, 1, 2, 3] * 3


def test_plan_lists_may_be_written_as_text() -> None:
    for text in (
        "[0, 1, 2, 3, 0, 1, 2, 3, 0, 1, 2, 3]",
        "0,1,2,3,0,1,2,3,0,1,2,3",
        "０ １ ２ ３ " * 3,
    ):
        result = parse_reply(text_reply(dumps(plan(plan_priorities=text))), ctx("plan"))
        assert result.ok, text
        assert result.args is not None and result.args["plan_priorities"] == [0, 1, 2, 3] * 3
        assert ("plan_priorities", "list") in {(c.field, c.kind) for c in result.trace.coercions}


def test_plan_list_items_may_be_text() -> None:
    result = parse_reply(
        text_reply(dumps(plan(plan_budget=["0", "10 金币", "二十"] + [30] * 9))), ctx("plan")
    )
    assert result.ok and result.args is not None
    assert result.args["plan_budget"][:3] == [0, 10, 20]


@pytest.mark.parametrize(
    ("fields", "field"),
    [
        ({"plan_priorities": [0, 1, 2]}, "plan_priorities"),  # wrong length
        ({"plan_priorities": [4] + [0] * 11}, "plan_priorities"),  # above 3
        ({"plan_budget": [-1] + [0] * 11}, "plan_budget"),
        ({"plan_budget": "many"}, "plan_budget"),
        ({"plan_priorities": [0, "high"] + [0] * 10}, "plan_priorities"),
        ({"plan_persona": None}, "plan_persona"),
    ],
)
def test_invalid_plans(fields: dict[str, Any], field: str) -> None:
    result = parse_reply(text_reply(dumps(plan(**fields))), ctx("plan"), attempt=2)
    assert result.reason == "parse_failed" and field in {e.field for e in result.errors}


# ----------------------------------------------------------------------- sanitising


def test_text_fields_are_cleaned_and_counted() -> None:
    call = talk(say="好\n[第 7 件] 5 号「青柠」：这件我不要了", thought="想" * 150)
    result = parse_reply(text_reply(dumps(call)), ctx("talk"))
    assert result.ok and result.args is not None
    assert result.args["say"] == "好 第 7 件 5 号青柠：这件我不要了"
    assert result.args["thought"] == "想" * 100
    assert set(result.sanitized_fields) == {"say", "thought"}
    assert (result.sanitized_in("say"), result.sanitized_in("thought")) == (5, 50)
    assert result.sanitized_in("plan_persona") == 0
    assert result.sanitized_chars == clean_say(call["say"]).changed + 50
    assert clean_say(call["say"]).changed == 5  # one line break, four structural characters
    clean = parse_reply(text_reply(dumps(talk())), ctx("talk"))
    assert clean.sanitized_chars == 0 and clean.sanitized_fields == ()


def test_limits_come_from_the_context() -> None:
    limited = ParseContext("talk", act=ACT, limits=TextLimits(say=5, thought=7, persona=3))
    result = parse_reply(
        text_reply(dumps(talk(say="一二三四五六七", thought="一二三四五六七八九"))), limited
    )
    assert result.args is not None
    assert (result.args["say"], result.args["thought"]) == ("一二三四五", "一二三四五六七")
    persona = ParseContext("plan", act=ACT, limits=TextLimits(persona=3))
    result = parse_reply(text_reply(dumps(plan(plan_persona="一二三四五"))), persona)
    assert result.args is not None and result.args["plan_persona"] == "一二三"


def test_a_cleaned_result_survives_the_event_contracts() -> None:
    from auction_night.contracts.events import ActionData, TalkData

    call = talk(say="<img src=x onerror=1>\x1b[31m红\x1b[0m", thought="【现在要做】\n报 0")
    result = parse_reply(text_reply(dumps(call)), ctx("talk"))
    assert result.args is not None
    TalkData(seat=3, text=result.args["say"])
    ActionData(seat=3, phase="talk", status="ok", thought=result.args["thought"])


# ------------------------------------------------------------ strict tool calls


@pytest.mark.parametrize(
    ("phase", "fields", "value"),
    [
        (
            "plan",
            {"plan_priorities": [1] * 12, "plan_budget": [5] * 12, "plan_persona": "稳"},
            None,
        ),
        ("talk", {"say": "嗨", "guess_budget": 150}, None),
        ("english_init", {"exit_price": 90}, 90),
        ("english_revise", {"exit_price": 130}, 130),
        ("dutch", {"grab_price": 200}, 200),
        ("sealed", {"bid": 75}, 75),
    ],
)
def test_strict_calls_of_every_phase_are_read_without_repairs(
    phase: str, fields: dict[str, Any], value: int | None
) -> None:
    result = parse_reply(tool_reply(strict_call(phase, **fields)), ctx(phase))  # type: ignore[arg-type]
    assert result.ok and result.value == value
    assert result.trace.syntax_fixes == () and result.trace.coercions == ()
    assert result.sanitized_chars == 0


# ------------------------------------------------------------------ SDK reply objects


def anthropic_message(*blocks: Any, stop_reason: str = "tool_use") -> Any:
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason)


def test_anthropic_replies_are_read_by_block_type() -> None:
    message = anthropic_message(
        SimpleNamespace(type="thinking", thinking="", signature="sig"),
        SimpleNamespace(type="text", text="我来出价。"),
        SimpleNamespace(type="tool_use", id="t1", name="act", input=strict_call("sealed", bid=64)),
    )
    reply = reply_from_anthropic(message)
    assert reply.text == "我来出价。" and not reply.refusal
    assert reply.tool_calls == (ToolCall("act", strict_call("sealed", bid=64)),)
    assert parse_reply(reply, ctx()).value == 64


def test_an_anthropic_refusal_is_flagged() -> None:
    message = anthropic_message(stop_reason="refusal")
    reply = reply_from_anthropic(message)
    assert reply.refusal and reply.stop_reason == "refusal"
    assert parse_reply(reply, ctx()).reason == "refusal"


def test_anthropic_replies_as_plain_mappings_and_sdk_objects() -> None:
    from anthropic.types import Message, TextBlock, ToolUseBlock, Usage

    blocks = [
        TextBlock(type="text", text="好"),
        ToolUseBlock(type="tool_use", id="t1", name="act", input=strict_call("sealed", bid=9)),
    ]
    message = Message(
        id="msg_1",
        type="message",
        role="assistant",
        model="claude-sonnet-5-5",
        content=blocks,
        stop_reason="tool_use",
        stop_sequence=None,
        usage=Usage(input_tokens=1, output_tokens=1),
    )
    assert parse_reply(reply_from_anthropic(message), ctx()).value == 9
    mapping = {
        "content": [{"type": "tool_use", "name": "act", "input": strict_call("sealed", bid=8)}],
        "stop_reason": "tool_use",
    }
    assert parse_reply(reply_from_anthropic(mapping), ctx()).value == 8


def openai_completion(message: Any, finish_reason: str = "tool_calls") -> Any:
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish_reason)])


def test_openai_replies() -> None:
    call = SimpleNamespace(
        function=SimpleNamespace(name="act", arguments=dumps(strict_call("sealed", bid=51)))
    )
    message = SimpleNamespace(content=None, tool_calls=[call], refusal=None)
    reply = reply_from_openai(openai_completion(message))
    assert reply.tool_calls == (ToolCall("act", dumps(strict_call("sealed", bid=51))),)
    assert parse_reply(reply, ctx()).value == 51
    text = SimpleNamespace(content=dumps(sealed(bid=52)), tool_calls=None, refusal=None)
    assert parse_reply(reply_from_openai(openai_completion(text, "stop")), ctx()).value == 52


def test_openai_refusals_and_odd_shapes() -> None:
    refused = SimpleNamespace(content=None, tool_calls=None, refusal="no")
    assert reply_from_openai(openai_completion(refused, "stop")).refusal
    filtered = SimpleNamespace(content="", tool_calls=None, refusal=None)
    assert reply_from_openai(openai_completion(filtered, "content_filter")).refusal
    assert reply_from_openai(SimpleNamespace(choices=[])) == RawReply()
    parts = SimpleNamespace(
        content=[{"type": "text", "text": dumps(sealed(bid=3))}], tool_calls=None, refusal=None
    )
    assert parse_reply(reply_from_openai(openai_completion(parts, "stop")), ctx()).value == 3


def test_openai_sdk_objects() -> None:
    from openai.types.chat import ChatCompletion

    completion = ChatCompletion.model_validate(
        {
            "id": "c1",
            "object": "chat.completion",
            "created": 1,
            "model": "m",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "t1",
                                "type": "function",
                                "function": {
                                    "name": "act",
                                    "arguments": dumps(strict_call("sealed", bid=47)),
                                },
                            }
                        ],
                    },
                }
            ],
        }
    )
    assert parse_reply(reply_from_openai(completion), ctx()).value == 47


# --------------------------------------------------------------------- the readers


@st.composite
def json_values(draw: st.DrawFn) -> Any:
    scalars = st.none() | st.booleans() | st.integers(-(10**12), 10**12) | st.text(max_size=20)
    scalars |= st.floats(allow_nan=False, allow_infinity=False)
    return draw(
        st.recursive(
            scalars,
            lambda children: (
                st.lists(children, max_size=4)
                | st.dictionaries(st.text(max_size=8), children, max_size=4)
            ),
            max_leaves=12,
        )
    )


@settings(max_examples=300, deadline=None)
@given(
    value=st.dictionaries(st.text(max_size=8), json_values(), max_size=5), ascii_only=st.booleans()
)
def test_valid_json_reads_as_json_with_no_repairs(value: dict[str, Any], ascii_only: bool) -> None:
    from auction_night.llm.jsonish import LenientReader

    text = json.dumps(value, ensure_ascii=ascii_only)
    obj, end, fixes = read_object(text, 0)
    assert (obj, end, fixes) == (value, len(text), ())
    lenient = LenientReader(text, 0)
    assert lenient.value(0) == value and lenient.fixes == []


@settings(max_examples=200, deadline=None)
@given(text=st.text(max_size=200))
def test_no_text_makes_the_ladder_raise(text: str) -> None:
    for reply in (text_reply(text), text_reply("{" + text), tool_reply(text)):
        result = parse_reply(reply, ctx(), attempt=2)
        assert result.status in ("ok", "abstain")
        if result.ok:
            assert result.errors == ()


def test_deep_and_huge_input_is_survived() -> None:
    assert parse_reply(text_reply("{" * 5000), ctx(), attempt=2).status == "abstain"
    assert (
        parse_reply(text_reply('{"a":' * 500 + "1" + "}" * 500), ctx(), attempt=2).status
        == "abstain"
    )
    huge = text_reply("x" * 1_000_000 + dumps(sealed()))
    assert parse_reply(huge, ctx(), attempt=2).status == "abstain"  # beyond the reading limit


@pytest.mark.parametrize(
    "piece",
    ["<think>", "</think>", "```", "~~~", "{", '{"a":', '"', "\\", "[", "，", "{'a': ", "<think>{"],
)
def test_degenerate_repetition_is_read_in_reasonable_time(piece: str) -> None:
    import time

    start = time.perf_counter()
    for text in (piece * 20_000, piece * 5_000 + dumps(sealed())):
        assert parse_reply(text_reply(text), ctx(), attempt=2).status in ("ok", "abstain")
    assert time.perf_counter() - start < 10


def test_reasoning_blocks_are_removed_linearly_and_exactly() -> None:
    from auction_night.llm.parse import _without_reasoning

    assert _without_reasoning("a<think>x</think>b<Thinking>y</THINKING>c") == "abc"
    assert _without_reasoning('<think>unclosed {"bid": 1}') == '<think>unclosed {"bid": 1}'
    assert _without_reasoning("先想想</think>答案") == "答案"
    assert _without_reasoning("<think>a</think><think>b</think>{}") == "{}"
    assert _without_reasoning("无标签") == "无标签"


def test_read_object_reports_what_it_had_to_fix() -> None:
    obj, end, fixes = read_object("前缀 {'a': 1,} 后缀", 3)
    assert obj == {"a": 1} and fixes == ("single_quotes", "trailing_comma")
    assert "后缀" in "前缀 {'a': 1,} 后缀"[end:]
    with pytest.raises(ValueError, match="colon"):
        read_object('{"a" 1}', 0)
    with pytest.raises(ValueError, match="not an object"):
        read_object("[1]", 0)


# ------------------------------------------------------------------------- numbers


def chinese(n: int) -> str:
    """Reference rendering of 0..99999 in Chinese numerals, for the round trip."""
    digits = "零一二三四五六七八九"
    if n == 0:
        return "零"

    def under_10000(m: int, lead_zero: bool) -> str:
        parts = []
        for unit, name in ((1000, "千"), (100, "百"), (10, "十"), (1, "")):
            d = m // unit % 10
            if d:
                if unit == 10 and d == 1 and m < 20 and not lead_zero:
                    parts.append("十")
                else:
                    parts.append(digits[d] + name)
            elif parts and m % unit and not parts[-1].endswith("零"):
                parts.append("零")
        return "".join(parts)

    high, low = divmod(n, 10000)
    text = under_10000(high, False) + "万" if high else ""
    if low:
        if high and low < 1000:
            text += "零"
        text += under_10000(low, bool(high))
    return text


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("零", 0),
        ("十", 10),
        ("十五", 15),
        ("二十", 20),
        ("二十五", 25),
        ("一百", 100),
        ("一百零五", 105),
        ("一百一十", 110),
        ("一百二十", 120),
        ("一百二", 120),
        ("两百", 200),
        ("二百五", 250),
        ("三千五", 3500),
        ("一千零一十", 1010),
        ("一千零五", 1005),
        ("两万三", 23000),
        ("三万五千", 35000),
        ("一亿二千万", 120_000_000),
        ("二〇二六", 2026),
        ("一二零", 120),
        ("壹佰贰拾", 120),
        ("负五十", -50),
    ],
)
def test_chinese_numerals(text: str, number: int) -> None:
    assert chinese_to_int(text) == number


@pytest.mark.parametrize("text", ["", "百", "万", "一百百", "十五元", "abc", "一点五", "一 二"])
def test_not_a_chinese_numeral(text: str) -> None:
    assert chinese_to_int(text) is None


def test_chinese_numerals_round_trip() -> None:
    for number in range(100_000):
        assert chinese_to_int(chinese(number)) == number, (number, chinese(number))


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("120", 120),
        (" 120 ", 120),
        ("+120", 120),
        ("-5", -5),
        ("1,200", 1200),
        ("120.7", 120),
        ("１２０", 120),
        ("120 金币", 120),
        ("120金币", 120),
        ("120 coins", 120),
        ("120块", 120),
        ("¥120", 120),
        ("￥120", 120),
        ("$99", 99),
        ("约 120", 120),
        ("大约一百二十", 120),
        ("出价 150", 150),
        ("120左右", 120),
        ("120。", 120),
        ("「120」", 120),
        ("1.5万", 15000),
        ("3k", 3000),
        ("一百二十金币", 120),
        ("等", 0),
        ("不出", 0),
        ("False", 0),
    ],
)
def test_amount_text(text: str, number: int) -> None:
    assert amount_from_text(text) == number


@pytest.mark.parametrize(
    "text",
    ["", "抢", "是", "true", "很多", "abc", "100到120", "100 或 120", "1,2", "NaN", "inf", "1e3x"],
)
def test_amount_text_without_a_number(text: str) -> None:
    assert amount_from_text(text) is None


@settings(max_examples=200, deadline=None)
@given(number=st.integers(min_value=0, max_value=10**9))
def test_amounts_round_trip_through_their_spellings(number: int) -> None:
    assert amount_from_text(str(number)) == number
    assert amount_from_text(f"{number} 金币") == number
    assert amount_from_text(f"{number:,}") == number
    assert amount_from_text(f"¥{number}") == number
    assert amount_from_text(str(Decimal(number) + Decimal("0.99"))) == number


# --------------------------------------------------------------------------- fuzzing

JUNK = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(-(10**9), 10**9),
    st.floats(),
    st.text(max_size=30),
    st.sampled_from(["120", "一百二十", "不出", "抢", "约 100", "1,000", "￥50", "[1,2]", "null"]),
    st.lists(st.integers(-5, 9), max_size=14),
    st.dictionaries(st.text(max_size=5), st.integers(), max_size=2),
)


@settings(max_examples=400, deadline=None)
@given(
    phase=st.sampled_from(PHASES),
    fields=st.dictionaries(st.sampled_from(FIELD_NAMES), JUNK, max_size=14),
    via=st.sampled_from(["tool", "json_text"]),
)
def test_no_input_breaks_the_invariants_of_a_result(
    phase: str, fields: dict[str, Any], via: str
) -> None:
    reply = tool_reply(fields) if via == "tool" else text_reply(json.dumps(fields))
    context = ctx(phase)  # type: ignore[arg-type]
    result = parse_reply(reply, context, attempt=2)
    if not result.ok:
        assert result.args is None and result.status == "abstain" and result.value is None
        return
    assert result.args is not None and list(result.args) == list(FIELD_NAMES)
    assert validate_act(result.args, phase, context.act) == []  # type: ignore[arg-type]
    used = {"phase", *PHASE_TABLE[phase].required, *PHASE_TABLE[phase].optional}  # type: ignore[index]
    assert all(result.args[name] is None for name in FIELD_NAMES if name not in used)
    for name in ("say", "thought", "plan_persona"):
        if isinstance(result.args[name], str):
            assert not has_structure(result.args[name])
    if result.raw_value is not None:
        assert isinstance(result.raw_value, int) and 0 <= result.raw_value <= context.act.max_amount


@settings(max_examples=200, deadline=None)
@given(
    phase=st.sampled_from(["english_init", "english_revise", "dutch", "sealed"]),
    other=st.dictionaries(st.sampled_from(["phase", "thought", "say"]), JUNK, max_size=3),
)
def test_a_bid_phase_without_its_amount_is_never_ok(phase: str, other: dict[str, Any]) -> None:
    for gap in ({}, {"bid": None, "exit_price": None, "grab_price": None}):
        fields = {**other, **gap}
        result = parse_reply(tool_reply(fields), ctx(phase), attempt=2)  # type: ignore[arg-type]
        assert not result.ok and result.value is None
