"""Counters of the ladder: legal clamps, invalid input and syntax repairs by native mode."""

import json
from typing import Any

import pytest
from llm_replies import (
    BID_120_TEXTS,
    REFUSALS,
    ctx,
    dumps,
    sealed,
    strict_call,
    text_reply,
    tool_reply,
)

from auction_night.contracts import parse_event
from auction_night.contracts.events import ParseRepairData
from auction_night.llm.counters import (
    CounterBook,
    ParseCounters,
    coerce_kinds,
    repair_codes,
    repair_data,
    repair_detail,
    repair_fields,
    syntax_kinds,
)
from auction_night.llm.parse import ParseResult, RawReply, parse_reply

MODES = ("tools", "json_schema", "text")


def read(text: str, **kw: Any) -> ParseResult:
    return parse_reply(text_reply(text), ctx(**kw))


def tool_ok() -> ParseResult:
    return parse_reply(tool_reply(strict_call("sealed", bid=120)), ctx())


# ----------------------------------------------------- syntax repairs by native mode


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("clean", {"tools": ("not_a_tool_call",), "json_schema": (), "text": ()}),
        (
            "fenced",
            {"tools": ("not_a_tool_call", "fenced"), "json_schema": ("fenced",), "text": ()},
        ),
        (
            "prose_both_and_fence",
            {
                "tools": ("not_a_tool_call", "fenced", "prose"),
                "json_schema": ("fenced", "prose"),
                "text": (),
            },
        ),
        (
            "prose_after",
            {"tools": ("not_a_tool_call", "prose"), "json_schema": ("prose",), "text": ()},
        ),
        (
            "trailing_comma",
            {
                "tools": ("trailing_comma", "not_a_tool_call"),
                "json_schema": ("trailing_comma",),
                "text": ("trailing_comma",),
            },
        ),
        (
            "wrapper",
            {
                "tools": ("wrapper", "not_a_tool_call"),
                "json_schema": ("wrapper",),
                "text": ("wrapper",),
            },
        ),
    ],
)
def test_syntax_repairs_are_relative_to_the_native_mode(
    name: str, expected: dict[str, tuple[str, ...]]
) -> None:
    trace = read(BID_120_TEXTS[name]).trace
    for mode in MODES:
        assert syntax_kinds(trace, mode) == expected[mode], (name, mode)  # type: ignore[arg-type]


def test_a_clean_tool_call_costs_nothing_in_any_mode() -> None:
    for mode in MODES:
        assert syntax_kinds(tool_ok().trace, mode) == ()  # type: ignore[arg-type]
        assert repair_codes(tool_ok(), mode) == ()  # type: ignore[arg-type]


def test_json_text_inside_a_tool_call_is_native_but_broken_json_is_not() -> None:
    clean = parse_reply(tool_reply(dumps(strict_call("sealed", bid=1))), ctx())
    assert syntax_kinds(clean.trace, "tools") == ()
    broken = parse_reply(tool_reply(dumps(strict_call("sealed", bid=1))[:-1] + ",}"), ctx())
    assert syntax_kinds(broken.trace, "tools") == ("trailing_comma",)


# ---------------------------------------------------------------------- repair codes


def codes(result: ParseResult, mode: str = "tools") -> tuple[str, ...]:
    return repair_codes(result, mode)  # type: ignore[arg-type]


def test_repair_codes_follow_the_event_contract_order() -> None:
    assert codes(tool_ok()) == ()
    assert codes(read(BID_120_TEXTS["clean"])) == ("syntax",)  # text where a tool call is native
    assert codes(read(BID_120_TEXTS["clean"]), "text") == ()
    assert codes(read(BID_120_TEXTS["chinese_numeral"]), "text") == ("coerce",)
    over = read(dumps(sealed(bid=500)), budget_left=100)
    assert codes(over, "text") == ("clamp",)
    messy = read("```json\n" + dumps(sealed(bid="一百二十", thought="好\n的")) + "\n```")
    assert codes(messy) == ("syntax", "coerce", "sanitize")


def test_invalid_input_retry_fallback_and_refusal_codes() -> None:
    first = read(dumps(sealed(bid=-1)))
    assert codes(first, "text") == ("invalid_input", "retry")
    last = parse_reply(text_reply(dumps(sealed(bid=-1))), ctx(), attempt=2)
    assert codes(last, "text") == ("invalid_input", "fallback")
    nothing = parse_reply(text_reply(""), ctx(), attempt=2)
    assert codes(nothing, "text") == ("fallback",)
    assert codes(read(REFUSALS["zh_apology"]), "text") == ("refusal",)
    assert codes(read(REFUSALS["zh_apology"]), "tools") == ("refusal",)


def test_a_clamp_is_never_an_invalid_input() -> None:
    result = read(dumps(sealed(bid=9999)), budget_left=300)
    assert "clamp" in codes(result, "text") and "invalid_input" not in codes(result, "text")


def test_fields_and_detail_describe_the_attempt() -> None:
    result = read(dumps(sealed(bid="一百二十", thought="好\n的")), budget_left=100)
    assert repair_fields(result) == ("bid", "thought")
    detail = repair_detail(result, "text")
    assert detail == "coerce=bid:text_number; legal=bid:budget_clamp:120->100; sanitized=1"
    failed = parse_reply(text_reply(dumps(sealed(bid=-4))), ctx(), attempt=2)
    assert repair_detail(failed, "text") == "invalid=bid:out_of_range; reason=parse_failed"
    assert repair_fields(failed) == ("bid",)
    assert coerce_kinds(read(BID_120_TEXTS["phase_left_out"]).trace) == ("filled_phase",)


def test_the_repair_event_payload_is_valid() -> None:
    result = read("```json\n" + dumps(sealed(bid="一百二十")) + "\n```")
    data = repair_data(result, "tools", role="seat:3", seat=3, call_id="c-17")
    assert isinstance(data, ParseRepairData)
    assert (data.role, data.seat, data.phase, data.attempt) == ("seat:3", 3, "sealed", 1)
    assert data.codes == ["syntax", "coerce"] and data.fields == ["bid"]
    assert data.status == "ok" and "status=" not in data.detail
    assert data.call_id == "c-17" and "syntax=not_a_tool_call,fenced" in data.detail
    event = parse_event(
        {"seq": 1, "lot": 6, "type": "parse_repair", "vis": "system", "data": data.model_dump()}
    )
    assert event.type == "parse_repair"
    assert repair_data(tool_ok(), "tools", role="seat:3") is None


def test_the_repair_event_of_a_retry_and_a_sanitised_talk() -> None:
    result = read(dumps(sealed(bid=-1)))
    data = repair_data(result, "text", role="seat:3", seat=3)
    assert data is not None and data.codes == ["invalid_input", "retry"]
    assert data.status == "retry"
    assert data.fields == ["bid"] and data.sanitized_chars == 0
    talk = {"phase": "talk", "say": "好[的]", "guess_budget": 5, "thought": "x"}
    cleaned = parse_reply(text_reply(dumps(talk)), ctx("talk"))
    data = repair_data(cleaned, "text", role="seat:3", seat=3)
    assert data is not None and data.codes == ["sanitize"] and data.sanitized_chars == 2


# ------------------------------------------------------------------------- counters


def test_counts_over_a_small_night() -> None:
    counters = ParseCounters()
    native = "tools"
    # 1. A clean tool call.
    counters.record(tool_ok(), native)
    # 2. A bid above the budget: legal.
    over = parse_reply(tool_reply(strict_call("sealed", bid=500)), ctx(budget_left=342))
    counters.record(over, native)
    # 3. A negative bid, then a good one.
    counters.record(parse_reply(tool_reply(strict_call("sealed", bid=-1)), ctx()), native)
    counters.record(parse_reply(tool_reply(strict_call("sealed", bid=1)), ctx(), attempt=2), native)
    # 4. A refusal.
    counters.record(read(REFUSALS["zh_apology"]), native)
    # 5. Nothing usable twice.
    counters.record(read(""), native)
    counters.record(parse_reply(text_reply(""), ctx(), attempt=2), native)

    assert counters.attempts == 7 and counters.actions == 5 and counters.ok == 3
    assert counters.retries == 2
    assert dict(counters.legal_clamps) == {"budget_clamp": 1}
    assert counters.invalid_inputs == 1 and dict(counters.invalid_by_code) == {"out_of_range": 1}
    assert dict(counters.abstentions) == {"refusal": 1, "missing": 1}
    assert (counters.refusals, counters.other_abstentions) == (1, 1)
    assert counters.rule_compliance == pytest.approx(1 - 1 / 5)
    assert counters.abstain_rate == pytest.approx(1 / 5)
    assert counters.refusal_rate == pytest.approx(1 / 5)
    assert counters.syntax_attempts == 0 and counters.syntax_rate == 0  # all native


def test_legal_clamps_do_not_lower_the_compliance() -> None:
    counters = ParseCounters()
    for _ in range(4):
        counters.record(read(dumps(sealed(bid=900)), budget_left=100), "text")
    revision = {"phase": "english_revise", "exit_price": 10, "thought": "x"}
    counters.record(
        parse_reply(text_reply(dumps(revision)), ctx("english_revise", current_price=60)), "text"
    )
    assert dict(counters.legal_clamps) == {"budget_clamp": 4, "below_current_price": 1}
    assert counters.invalid_inputs == 0 and counters.rule_compliance == 1.0


def test_invalid_input_counts_once_per_attempt_and_by_code() -> None:
    counters = ParseCounters()
    counters.record(
        read(dumps({"phase": "talk", "bid": -3})), "text"
    )  # three problems, one attempt
    assert counters.invalid_inputs == 1
    assert sum(counters.invalid_by_code.values()) >= 3
    assert set(counters.invalid_by_code) == {"phase_mismatch", "missing_required", "out_of_range"}


def test_the_same_replies_cost_different_repairs_in_different_native_modes() -> None:
    replies = [BID_120_TEXTS[name] for name in ("clean", "fenced", "prose_after", "trailing_comma")]
    by_mode = {}
    for mode in MODES:
        counters = ParseCounters()
        for text in replies:
            counters.record(read(text), mode)  # type: ignore[arg-type]
        by_mode[mode] = (counters.syntax_attempts, dict(counters.syntax_by_kind))
    assert by_mode["tools"][0] == 4
    assert by_mode["json_schema"][0] == 3  # the clean reply is native there
    assert by_mode["text"][0] == 1  # only the trailing comma is a repair
    assert by_mode["text"][1] == {"trailing_comma": 1}


def test_an_abstention_in_an_english_revision_keeps_the_previous_price() -> None:
    counters = ParseCounters()
    revision = ctx("english_revise")
    counters.record(parse_reply(text_reply(""), revision, attempt=2), "tools")
    assert counters.kept_previous == 1 and counters.abstentions["missing"] == 1
    counters.record(parse_reply(text_reply(""), ctx(), attempt=2), "tools")
    assert counters.kept_previous == 1 and counters.abstentions["missing"] == 2


def test_failures_without_a_reply_are_abstentions_too() -> None:
    counters = ParseCounters()
    counters.record(tool_ok(), "tools")
    counters.record_abstention("timeout")
    counters.record_abstention("budget_aborted")
    counters.record_abstention("not_asked")
    assert (counters.actions, counters.not_asked, counters.attempts) == (3, 1, 1)
    assert dict(counters.abstentions) == {"timeout": 1, "budget_aborted": 1}
    assert counters.abstain_rate == pytest.approx(2 / 3)


def test_rates_are_none_without_actions() -> None:
    counters = ParseCounters()
    assert counters.rates() == {
        "rule_compliance": None,
        "abstain_rate": None,
        "refusal_rate": None,
        "syntax_rate": None,
    }


def test_sanitising_and_coercion_are_counted() -> None:
    counters = ParseCounters()
    counters.record(read(dumps(sealed(bid="120", thought="a\nb"))), "text")
    counters.record(read(dumps(sealed(bid="一百"))), "text")
    assert counters.coerce_attempts == 2 and dict(counters.coerce_by_kind) == {"text_number": 2}
    assert counters.sanitize_attempts == 1 and counters.sanitized_chars == 1


def test_counters_merge_and_serialise() -> None:
    one, two = ParseCounters(), ParseCounters()
    one.record(tool_ok(), "tools")
    two.record(read(dumps(sealed(bid=-1))), "text")
    two.record(read(REFUSALS["en_sorry"]), "text")
    one.merge(two)
    assert (one.attempts, one.actions, one.retries) == (3, 2, 1)
    assert one.refusals == 1 and one.invalid_inputs == 1
    flat = one.to_dict()
    assert json.loads(json.dumps(flat)) == flat
    assert list(flat["abstentions"]) == sorted(flat["abstentions"])
    assert not any(isinstance(v, float) for v in flat.values())
    assert flat["actions"] == 2 and flat["abstentions"] == {"refusal": 1}


def test_counter_book_groups_by_label() -> None:
    book = CounterBook()
    book.record("seat:1", tool_ok(), "tools")
    book.record("seat:1", tool_ok(), "tools")
    book.record("seat:2", read(""), "tools")
    assert book.get("seat:1").actions == 2 and book.get("seat:3").actions == 0
    assert book.total().attempts == 3
    assert book.total(["seat:2"]).attempts == 1
    assert book.total(["nobody"]).attempts == 0
    assert list(book.to_dict()) == ["seat:1", "seat:2", "seat:3"]


def test_a_full_night_of_strict_calls_is_perfectly_healthy() -> None:
    counters = ParseCounters()
    for _ in range(30):
        counters.record(tool_ok(), "tools")
    assert counters.rule_compliance == 1.0 and counters.abstain_rate == 0.0
    assert counters.syntax_rate == 0.0 and counters.legal_clamps == {}


def test_results_are_plain_data() -> None:
    result = read(dumps(sealed()))
    assert isinstance(result, ParseResult) and RawReply().text == ""
    with pytest.raises(AttributeError):
        result.status = "abstain"  # type: ignore[misc]
