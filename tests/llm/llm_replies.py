"""Reply fixtures of the parsing ladder: well-formed, malformed and adversarial model output."""

import json
from typing import Any

from auction_night.contracts.act import ActContext
from auction_night.contracts.mechanism import Phase
from auction_night.llm.parse import ParseContext, RawReply, ToolCall

ACT = ActContext(n_lots=12, n_seats=8, self_seat=3, current_lot=6)


def ctx(phase: Phase = "sealed", **kw: Any) -> ParseContext:
    """A context for seat 3 on lot 6 with a budget of 342, unless told otherwise."""
    kw.setdefault("budget_left", 342)
    return ParseContext(phase, act=ACT, **kw)


def text_reply(text: str) -> RawReply:
    return RawReply(text=text)


def tool_reply(arguments: Any, name: str = "act") -> RawReply:
    return RawReply(tool_calls=(ToolCall(name, arguments),))


def sealed(**fields: Any) -> dict[str, Any]:
    """A complete sealed-phase call, with the fields overridden by ``fields``."""
    call: dict[str, Any] = {"phase": "sealed", "bid": 120, "thought": "按估值出价。"}
    call.update(fields)
    return call


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


# One valid call per phase, with every unused field null, as a strict tool would return it.
FULL_NULL = {
    "say": None,
    "pledge_kind": None,
    "pledge_lot": None,
    "pledge_to": None,
    "pledge_amount": None,
    "guess_budget": None,
    "exit_price": None,
    "grab_price": None,
    "bid": None,
    "plan_priorities": None,
    "plan_budget": None,
    "plan_persona": None,
}


def strict_call(phase: str, **fields: Any) -> dict[str, Any]:
    """The arguments of a strict tool call: all 14 fields, unused ones null."""
    return {"phase": phase, **FULL_NULL, "thought": "想好了。", **fields}


# (id, reply text, expected bid or None) for replies that must be read as a sealed bid of 120.
# Each line is one way in which models write the same answer.
BID_120_TEXTS = {
    "clean": dumps(sealed()),
    "fenced": "```json\n" + dumps(sealed()) + "\n```",
    "fenced_without_language": "```\n" + dumps(sealed()) + "\n```",
    "fenced_tilde": "~~~json\n" + dumps(sealed()) + "\n~~~",
    "unclosed_fence": "```json\n" + dumps(sealed()),
    "prose_before": "好的，我的决定如下：\n" + dumps(sealed()),
    "prose_after": dumps(sealed()) + "\n以上就是我的出价，祝好运。",
    "prose_both_and_fence": "思考完毕。\n```json\n" + dumps(sealed()) + "\n```\n希望这样可以。",
    "fullwidth": '｛"phase"：“sealed”，"bid"：１２０，"thought"：“按估值出价。”｝',
    "fullwidth_braces_only": '｛"phase": "sealed", "bid": 120, "thought": "按估值出价。"｝',
    "smart_quotes": "{“phase”: “sealed”, “bid”: 120, “thought”: “按估值出价。”}",
    "single_quotes": "{'phase': 'sealed', 'bid': 120, 'thought': '按估值出价。'}",
    "trailing_comma": '{"phase": "sealed", "bid": 120, "thought": "按估值出价。",}',
    "missing_closing_brace": '{"phase": "sealed", "bid": 120, "thought": "按估值出价。"',
    "missing_closing_quote_and_brace": '{"phase": "sealed", "bid": 120, "thought": "按估值出价。',
    "unquoted_keys": '{phase: "sealed", bid: 120, thought: "按估值出价。"}',
    "python_literals": '{"phase": "sealed", "bid": 120, "thought": "按估值出价。", "note": None}',
    "line_comment": '{"phase": "sealed", // 阶段\n "bid": 120, "thought": "按估值出价。"}',
    "equals_instead_of_colon": '{"phase"="sealed", "bid"=120, "thought"="按估值出价。"}',
    "chinese_numeral": dumps(sealed(bid="一百二十")),
    "unit_suffix": dumps(sealed(bid="120 金币")),
    "number_string": dumps(sealed(bid="120")),
    "float": dumps(sealed(bid=120.0)),
    "float_rounds_down": dumps(sealed(bid=120.9)),
    "fullwidth_digits": dumps(sealed(bid="１２０")),
    "currency": dumps(sealed(bid="¥120")),
    "colloquial_numeral": dumps(sealed(bid="一百二")),
    "bare_unit_value": '{"phase": "sealed", "bid": 120金币, "thought": "按估值出价。"}',
    "wrapper": dumps({"name": "act", "arguments": sealed()}),
    "wrapper_with_text_arguments": dumps({"name": "act", "arguments": dumps(sealed())}),
    "capitalised_keys": dumps({"Phase": "sealed", "Bid": 120, "Thought": "按估值出价。"}),
    "spaced_keys": dumps({"phase": "sealed", "bid ": 120, "thought": "按估值出价。"}),
    "think_block_first": '<think>也许 {"bid": 1} 太低</think>\n' + dumps(sealed()),
    "closing_think_only": '先想想 {"bid": 2}\n</think>\n' + dumps(sealed()),
    "two_objects_first_wins": dumps(sealed()) + "\n或者\n" + dumps(sealed(bid=999)),
    "braces_in_prose_first": "价格区间 {100到200} 都可以，我选：" + dumps(sealed()),
    "broken_brace_then_object": "{这是笔误 " + dumps(sealed()),
    "inner_quotes": '{"phase": "sealed", "bid": 120, "thought": "他说"便宜"，我信了"}',
    "phase_in_other_case": dumps(sealed(phase="Sealed")),
    "phase_left_out": dumps({"bid": 120, "thought": "按估值出价。"}),
    "extra_keys": dumps(sealed(reasoning="很长的推理", confidence=0.9)),
}

# Replies that hold no usable action at all. The reason is what the ladder reports at last.
NOTHING_TO_READ = {
    "empty": "",
    "whitespace": " \n\t ",
    "prose_only": "我觉得出 120 比较合适，你怎么看？",
    "only_braces": "{}",
    "dangling": '{"phase": ',
    "not_an_object": "[1, 2, 3]",
}

REFUSALS = {
    "zh_apology": "抱歉，我无法协助这个请求。",
    "zh_plain": "我不能帮你完成这个任务。",
    "zh_polite": "对不起，我不能参与这类游戏。",
    "en_sorry": "I'm sorry, but I can't help with that.",
    "en_cannot": "I cannot assist with this request.",
    "en_curly": "I can’t do that.",
}

# Not refusals: the sentence starts like one, but there is an action in it.
NOT_REFUSALS = {
    "action_follows": "我不能确定，先出 120。\n" + dumps(sealed()),
}
