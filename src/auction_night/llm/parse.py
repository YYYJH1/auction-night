"""The parsing ladder: from one model reply to one validated action, or to an abstention.

A reply is turned into an ``act`` call in seven steps. Each step only runs when the one before
it did not give a usable result:

1. the arguments of the ``act`` tool call;
2. else the first ``{...}`` object in the text, after code fences and ``<think>`` blocks are set
   aside;
3. syntax repair of that object: full-width punctuation and quotes, single quotes, trailing
   commas, unquoted keys, missing closing brackets, a wrapper such as ``{"arguments": {...}}``;
4. type coercion per field: ``"120"``, ``"120 金币"``, ``"一百二十"``, ``120.0``, an explicit
   "no" such as ``"不出"`` or ``false`` for an amount, a pledge kind written in Chinese;
5. validation against the phase table (``contracts.act.validate_act``);
6. one retry signal that names the concrete violations;
7. otherwise an abstention.

A missing action is always an abstention and never a default price. The only things the ladder
fills in are metadata that the request already fixes: the ``phase`` name, and an empty ``say``
when the model leaves it null (staying silent is allowed).

Amounts above the remaining budget and English revisions below the current price are *legal*:
they are reported as adjustments, never as invalid input. Everything the ladder repaired is
recorded in a :class:`ParseTrace`. How the repairs are counted, relative to the native mode of
the endpoint, is decided in :mod:`auction_night.llm.counters`.

No text is ever evaluated or executed here. Repairs are string transformations followed by a
small hand-written reader that builds plain JSON values.
"""

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from auction_night.contracts.act import (
    ACT_TOOL_NAME,
    FIELD_NAMES,
    PHASE_TABLE,
    PLEDGE_KINDS,
    ActContext,
    FieldError,
    extract_pledge,
    normalize_act,
    validate_act,
)
from auction_night.contracts.events import AbstainReason, Pledge
from auction_night.contracts.mechanism import Phase
from auction_night.llm.amounts import NO_WORDS, NULL_WORDS, amount_from_text
from auction_night.llm.jsonish import LenientReader, Unparseable, read_object
from auction_night.llm.sanitize import TextLimits, clean_persona, clean_say, clean_thought

Status = Literal["ok", "retry", "abstain"]
Source = Literal["tool_call", "text", "none"]
AdjustmentKind = Literal["budget_clamp", "below_current_price"]

MAX_REPLY_CHARS: Final = 100_000
# The field that holds the amount of each bidding phase, and the phases whose amount the budget
# can clamp. A Dutch grab price is never clamped: levels above the budget simply cannot fire.
_VALUE_FIELDS: Final = {
    "english_init": "exit_price",
    "english_revise": "exit_price",
    "dutch": "grab_price",
    "sealed": "bid",
}
_BID_FIELDS: Final = {name: field for name, field in _VALUE_FIELDS.items() if name != "dutch"}
_AMOUNT_FIELDS: Final = frozenset(
    {
        "pledge_lot",
        "pledge_to",
        "pledge_amount",
        "guess_budget",
        "exit_price",
        "grab_price",
        "bid",
    }
)
_LIST_FIELDS: Final = frozenset({"plan_priorities", "plan_budget"})
_TEXT_FIELDS: Final = frozenset({"say", "thought", "plan_persona"})
_WRAPPER_KEYS: Final = ("arguments", "input", "parameters", "params", "args", "act")


# ----------------------------------------------------------------------------- the reply


@dataclass(frozen=True)
class ToolCall:
    """One tool call of a reply. ``arguments`` is a mapping, or JSON text (OpenAI style)."""

    name: str
    arguments: Any


@dataclass(frozen=True)
class RawReply:
    """What came back from one call, reduced to what the ladder looks at.

    ``refusal`` is set when the API itself declined (a refusal stop reason or field).
    """

    tool_calls: tuple[ToolCall, ...] = ()
    text: str = ""
    refusal: bool = False
    stop_reason: str | None = None


def _get(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def reply_from_anthropic(message: Any) -> RawReply:
    """Reduce an Anthropic ``Message`` (or the same shape as a mapping) to a :class:`RawReply`.

    Blocks are read by type, never by position: a reply can start with thinking blocks.
    """
    texts: list[str] = []
    calls: list[ToolCall] = []
    for block in _get(message, "content") or []:
        kind = _get(block, "type")
        if kind == "text":
            texts.append(_get(block, "text") or "")
        elif kind == "tool_use":
            calls.append(ToolCall(_get(block, "name") or "", _get(block, "input")))
    stop = _get(message, "stop_reason")
    return RawReply(tuple(calls), "\n".join(texts), refusal=stop == "refusal", stop_reason=stop)


def reply_from_openai(completion: Any) -> RawReply:
    """Reduce a chat completion (or the same shape as a mapping) to a :class:`RawReply`."""
    choices = _get(completion, "choices") or []
    if not choices:
        return RawReply()
    choice = choices[0]
    message = _get(choice, "message")
    content = _get(message, "content") or ""
    if isinstance(content, list):  # multi-part content
        content = "".join(_get(part, "text") or "" for part in content)
    calls = []
    for call in _get(message, "tool_calls") or []:
        function = _get(call, "function")
        calls.append(ToolCall(_get(function, "name") or "", _get(function, "arguments")))
    finish = _get(choice, "finish_reason")
    refused = bool(_get(message, "refusal")) or finish == "content_filter"
    return RawReply(tuple(calls), str(content), refusal=refused, stop_reason=finish)


# --------------------------------------------------------------------- finding the object

_FENCE: Final = re.compile(r"(`{3,}|~{3,})[ \t]*[\w+.-]*[ \t]*\n?(.*?)(?:\1|\Z)", re.DOTALL)
_THINK_OPEN: Final = re.compile(r"<(think|thinking|reasoning)>", re.IGNORECASE)
_THINK_CLOSE: Final = re.compile(r"</(?:think|thinking|reasoning)>", re.IGNORECASE)
_OBJECT_START: Final = re.compile(r"[{｛]")


def _without_reasoning(text: str) -> str:
    """Drop ``<think>`` blocks, including a lone closing tag whose opening is in the prompt.

    An opening tag without a closing one is left alone. The scan is linear in the text.
    """
    kept: list[str] = []
    position = 0
    while (opening := _THINK_OPEN.search(text, position)) is not None:
        closing = re.compile(rf"</{opening.group(1)}>", re.IGNORECASE).search(text, opening.end())
        if closing is None:
            break
        kept.append(text[position : opening.start()])
        position = closing.end()
    text = "".join(kept) + text[position:]
    parts = _THINK_CLOSE.split(text)
    return parts[-1] if len(parts) > 1 else text


@dataclass(frozen=True)
class _Found:
    obj: dict[str, Any]
    source: Source
    fenced: bool = False
    prose: bool = False
    arguments_as_text: bool = False
    fixes: tuple[str, ...] = ()


_MAX_TRIES: Final = 20


def _first_object(segment: str) -> tuple[dict[str, Any], int, int, tuple[str, ...]] | None:
    """The first ``{...}`` of a text that can be read as an object.

    Braces that do not open a readable object (a "{low range}" in prose) are skipped. Returns the
    object, where it starts and ends and the repairs that were needed.
    """
    for tries, found in enumerate(_OBJECT_START.finditer(segment)):
        if tries >= _MAX_TRIES:
            return None
        try:
            obj, end, fixes = read_object(segment, found.start())
        except ValueError:
            continue
        return obj, found.start(), end, fixes
    return None


def _object_in_text(text: str) -> _Found | None:
    """The first object of a text, after fences are stripped (step 2 of the ladder)."""
    text = _without_reasoning(text[:MAX_REPLY_CHARS])
    fenced = False
    segment, outside = text, ""
    for match in _FENCE.finditer(text):
        if _OBJECT_START.search(match.group(2)):
            segment, fenced = match.group(2), True
            outside = text[: match.start()] + text[match.end() :]
            break
    read = _first_object(segment)
    if read is None:
        return None
    obj, start, end, fixes = read
    prose = bool(segment[:start].strip() or segment[end:].strip() or outside.strip())
    return _Found(obj, "text", fenced=fenced, prose=prose, fixes=fixes)


def _object_from_call(call: ToolCall) -> _Found | None:
    arguments = call.arguments
    if isinstance(arguments, Mapping):
        return _Found(dict(arguments), "tool_call")
    if isinstance(arguments, str):
        read = _first_object(arguments.strip()[:MAX_REPLY_CHARS])
        if read is None:
            return None
        return _Found(read[0], "tool_call", arguments_as_text=True, fixes=read[3])
    return None


def _find(reply: RawReply) -> tuple[_Found | None, int]:
    """The candidate object of a reply and the number of ``act`` calls it held."""
    calls = [call for call in reply.tool_calls if call.name == ACT_TOOL_NAME]
    for call in calls:
        found = _object_from_call(call)
        if found is not None:
            return found, len(calls)
    return _object_in_text(reply.text), len(calls)


_REFUSAL: Final = re.compile(
    r"^\W*(?:(?:很?抱歉|对不起|不好意思)[，,。.\s]*)?(?:但是?[，,]?\s*)?我?"
    r"(?:无法|不能|没法|不可以|不便|拒绝)"
    r"|^\W*(?:(?:i[’']?m|i am) sorry|sorry|i apologi[sz]e)\b[^.]{0,40}"
    r"\b(?:can[’']?t|cannot|unable|won[’']?t)\b"
    r"|^\W*i (?:can[’']?t|cannot|won[’']?t|am unable|am not able|[’']m unable)\b",
    re.IGNORECASE,
)


def looks_like_refusal(text: str) -> bool:
    """True when a reply without any action reads like a refusal ("I cannot help with that")."""
    return bool(_REFUSAL.search(text.strip()[:300]))


# ------------------------------------------------------------------------- coercion


def _snake(key: str) -> str:
    key = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key.strip())
    return re.sub(r"[\s-]+", "_", key).lower()


@dataclass(frozen=True)
class Coercion:
    """One field whose value was converted, and how."""

    field: str
    kind: str


def _coerce_amount(value: Any) -> tuple[Any, str | None]:
    """The amount in ``value`` and what was done to get it; ``value`` itself when hopeless."""
    if isinstance(value, bool):
        return (0, "zero_word") if value is False else (value, None)
    if isinstance(value, int):
        return value, None
    if isinstance(value, float):
        return (math.floor(value), "float") if math.isfinite(value) else (value, None)
    if isinstance(value, str):
        if value.strip().lower() in NULL_WORDS:
            return None, "null_word"
        number = amount_from_text(value)
        if number is None:
            return value, None
        return number, ("zero_word" if value.strip().lower() in NO_WORDS else "text_number")
    return value, None


def _items_from_text(text: str) -> list[Any] | None:
    body = text.strip()
    start = min((i for i in (body.find("["), body.find("［")) if i >= 0), default=-1)
    if start >= 0:
        try:
            parsed = LenientReader(body, start).value(0)
        except Unparseable:
            parsed = None
        if isinstance(parsed, list):
            return parsed
    pieces = [p for p in re.split(r"[\s,，、;；]+", body.strip("[]［］()（）")) if p]
    return pieces or None


def _coerce_list(value: Any) -> tuple[Any, str | None]:
    """A list of integers from a list, a tuple or text such as ``"[0, 1, 2]"``."""
    if isinstance(value, list | tuple):
        items: list[Any] | None = list(value)
        changed = isinstance(value, tuple)
    elif isinstance(value, str):
        items, changed = _items_from_text(value), True
    else:
        return value, None
    if items is None:
        return value, None
    converted = []
    for item in items:
        number, how = _coerce_amount(item)
        if isinstance(number, bool) or not isinstance(number, int):
            return value, None
        changed = changed or how is not None
        converted.append(number)
    return (converted, "list") if changed else (value, None)


def _coerce_text(value: Any) -> tuple[Any, str | None]:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value), "text_from_number"
    return value, None


_PLEDGE_ALIASES: Final = {
    "不出价": "no_bid",
    "不竞拍": "no_bid",
    "出价不超过": "max_bid",
    "最高出价": "max_bid",
    "让某人拿下": "let_win",
    "让他拿下": "let_win",
    "让拍": "let_win",
}


def _coerce_pledge_kind(value: Any) -> tuple[Any, str | None]:
    if not isinstance(value, str):
        return value, None
    body = value.strip()
    if body.lower() in NULL_WORDS:
        return None, "null_word"
    key = re.sub(r"[\s-]+", "_", body.lower())
    key = _PLEDGE_ALIASES.get(body, key)
    if key in PLEDGE_KINDS and key != value:
        return key, "enum"
    return value, None


def _canonical_keys(obj: Mapping[str, Any]) -> dict[str, Any]:
    return {_snake(str(key)): value for key, value in obj.items()}


def _unwrap(obj: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Peel a wrapper such as ``{"name": "act", "arguments": {...}}`` off an object.

    Returns the object that holds the act fields (or the input when there is none) and whether a
    wrapper was removed.
    """
    wrapped = False
    for _ in range(2):
        keys = _canonical_keys(obj)
        if any(key in FIELD_NAMES for key in keys):
            break
        inner = next((keys[name] for name in _WRAPPER_KEYS if name in keys), None)
        if isinstance(inner, str) and "{" in inner:
            try:
                inner, _, _ = read_object(inner, inner.index("{"))
            except ValueError:
                break
        if not isinstance(inner, Mapping):
            break
        obj, wrapped = dict(inner), True
    return obj, wrapped


def coerce_args(
    obj: dict[str, Any], phase: Phase
) -> tuple[dict[str, Any], tuple[Coercion, ...], bool]:
    """Step 4: canonical keys and per-field type coercion for the fields of one phase.

    Returns the arguments, the coercions made and whether a wrapper object was peeled off.
    """
    inner, wrapped = _unwrap(obj)
    args = _canonical_keys(inner)
    spec = PHASE_TABLE[phase]
    used = {"phase", *spec.required, *spec.optional}
    coercions = [
        Coercion(_snake(str(key)), "key_name")
        for key in inner
        if _snake(str(key)) in used and _snake(str(key)) != key
    ]

    def update(name: str, value: Any, how: str | None) -> None:
        if how is not None:
            args[name] = value
            coercions.append(Coercion(name, how))

    for name in FIELD_NAMES:
        if name not in used or name not in args:
            continue
        value = args[name]
        if name in _AMOUNT_FIELDS:
            update(name, *_coerce_amount(value))
        elif name in _LIST_FIELDS:
            update(name, *_coerce_list(value))
        elif name in _TEXT_FIELDS:
            update(name, *_coerce_text(value))
        elif name == "pledge_kind":
            update(name, *_coerce_pledge_kind(value))
        elif name == "phase" and isinstance(value, str):
            canonical = value.strip().lower()
            update(name, canonical, "enum" if canonical != value else None)
    return args, tuple(coercions), wrapped


# --------------------------------------------------------------------------- the context


@dataclass(frozen=True)
class ParseContext:
    """What the ladder needs to know about the call that was made.

    ``budget_left`` and ``current_price`` only serve to recognise legal adjustments (a bid above
    the budget, an English revision below the current price). Leave them None when they are not
    known: then nothing is reported. ``auction_night.llm.render.parse_context`` derives the whole
    context from the event log.
    """

    phase: Phase
    act: ActContext = field(default_factory=ActContext)
    budget_left: int | None = None
    current_price: int | None = None
    limits: TextLimits = field(default_factory=TextLimits)


@dataclass(frozen=True)
class Adjustment:
    """A legal difference between what the model wrote and what the rules apply."""

    kind: AdjustmentKind
    field: str
    raw: int
    effective: int


@dataclass(frozen=True)
class ParseTrace:
    """Facts about how a reply was read. Counting them is :mod:`counters`' job."""

    source: Source = "none"
    act_calls: int = 0
    fenced: bool = False
    prose: bool = False
    arguments_as_text: bool = False
    syntax_fixes: tuple[str, ...] = ()
    unwrapped: bool = False
    filled: tuple[str, ...] = ()
    coercions: tuple[Coercion, ...] = ()


@dataclass(frozen=True)
class ParseResult:
    """The outcome of one attempt.

    ``ok``: ``args`` holds the validated, coerced and sanitised call in tool field order.
    ``retry``: the reply was unusable and ``retry_message`` says why; ask once more.
    ``abstain``: no action. ``reason`` is ``refusal``, ``missing`` (nothing to read) or
    ``parse_failed`` (something was there but could not be made valid).
    """

    status: Status
    phase: Phase
    attempt: int = 1
    args: dict[str, Any] | None = None
    errors: tuple[FieldError, ...] = ()
    reason: AbstainReason | None = None
    trace: ParseTrace = field(default_factory=ParseTrace)
    adjustments: tuple[Adjustment, ...] = ()
    sanitized_by_field: tuple[tuple[str, int], ...] = ()
    retry_message: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def sanitized_chars(self) -> int:
        """Characters of model text that were removed, replaced or cut, over all fields."""
        return sum(count for _, count in self.sanitized_by_field)

    @property
    def sanitized_fields(self) -> tuple[str, ...]:
        """The text fields that were changed by cleaning."""
        return tuple(name for name, _ in self.sanitized_by_field)

    def sanitized_in(self, name: str) -> int:
        """Changed characters of one text field (``say``, ``thought`` or ``plan_persona``)."""
        return dict(self.sanitized_by_field).get(name, 0)

    @property
    def value(self) -> int | None:
        """The amount the rules apply: the model's number, after a legal budget clamp."""
        raw = self.raw_value
        if raw is None:
            return None
        for adjustment in self.adjustments:
            if adjustment.kind == "budget_clamp":
                return adjustment.effective
        return raw

    @property
    def raw_value(self) -> int | None:
        """The amount the model wrote in the bid field of the phase."""
        name = _VALUE_FIELDS.get(self.phase)
        if self.args is None or name is None:
            return None
        return self.args.get(name)

    @property
    def pledge(self) -> Pledge | None:
        """The structured pledge of a valid talk call."""
        if self.args is None or self.phase != "talk":
            return None
        return extract_pledge(self.args)

    @property
    def abstains(self) -> bool:
        return self.status == "abstain"


# ---------------------------------------------------------------------------- messages

_CODE_TEXT: Final = {
    "missing_required": "缺失，本阶段必填",
    "wrong_type": "类型不对",
    "out_of_range": "超出范围",
    "phase_mismatch": "与【现在要做】的阶段不符",
    "inconsistent_pledge": "誓约字段不一致",
}


def explain(error: FieldError) -> str:
    """One violation as a short Chinese phrase, with the validator's own detail appended."""
    base = f"{error.field} {_CODE_TEXT.get(error.code, error.code)}"
    if error.code == "missing_required":
        return base
    return f"{base}（{error.message}）"


def retry_message(phase: Phase, errors: Sequence[FieldError], *, nothing_found: bool) -> str:
    """The follow-up that asks the model to answer again, naming the concrete violations."""
    tail = f"请重新调用一次 act 工具：phase 填 {phase}，只填该阶段需要的字段，其余填 null。"
    if nothing_found:
        return f"上一次回复里没有找到 act 工具调用。{tail}"
    if not errors:
        return f"上一次回复里的 act 参数无法读成一个 JSON 对象。{tail}"
    problems = "；".join(explain(error) for error in errors)
    return f"上一次回复不能采用：{problems}。{tail}"


# ------------------------------------------------------------------------ the ladder


def _adjustments(args: Mapping[str, Any], ctx: ParseContext) -> tuple[Adjustment, ...]:
    name = _BID_FIELDS.get(ctx.phase)
    value = args.get(name) if name else None
    if name is None or not isinstance(value, int):
        return ()
    found = []
    if ctx.budget_left is not None and value > ctx.budget_left:
        found.append(Adjustment("budget_clamp", name, value, ctx.budget_left))
    if (
        ctx.phase == "english_revise"
        and ctx.current_price is not None
        and value < ctx.current_price
    ):
        found.append(Adjustment("below_current_price", name, value, value))
    return tuple(found)


def _sanitized(args: dict[str, Any], limits: TextLimits) -> tuple[tuple[str, int], ...]:
    """Clean the text fields in place and return ``(field, changed characters)`` for the changed."""
    touched = []
    for name, clean, cap in (
        ("say", clean_say, limits.say),
        ("thought", clean_thought, limits.thought),
        ("plan_persona", clean_persona, limits.persona),
    ):
        value = args.get(name)
        if isinstance(value, str):
            cleaned = clean(value, cap)
            args[name] = cleaned.text
            if cleaned.changed:
                touched.append((name, cleaned.changed))
    return tuple(touched)


def _unsuccessful(
    ctx: ParseContext,
    attempt: int,
    max_attempts: int,
    reason: AbstainReason,
    trace: ParseTrace,
    errors: tuple[FieldError, ...] = (),
) -> ParseResult:
    if attempt < max_attempts:
        message = retry_message(ctx.phase, errors, nothing_found=reason == "missing")
        return ParseResult(
            "retry", ctx.phase, attempt, errors=errors, trace=trace, retry_message=message
        )
    return ParseResult("abstain", ctx.phase, attempt, errors=errors, reason=reason, trace=trace)


def parse_reply(
    reply: RawReply, ctx: ParseContext, *, attempt: int = 1, max_attempts: int = 2
) -> ParseResult:
    """Run the ladder on one reply.

    ``attempt`` is the number of this attempt (1 for the first) and ``max_attempts`` how many the
    call may use: the configured ``limits.syntax_retries`` plus one. A failed attempt before the
    last returns ``retry`` with a message for the follow-up call; the last one returns
    ``abstain``. A refusal never retries.
    """
    if reply.refusal:
        return ParseResult("abstain", ctx.phase, attempt, reason="refusal")
    found, act_calls = _find(reply)
    if found is None:
        if looks_like_refusal(reply.text):
            return ParseResult("abstain", ctx.phase, attempt, reason="refusal")
        trace = ParseTrace(act_calls=act_calls)
        return _unsuccessful(ctx, attempt, max_attempts, "missing", trace)

    args, coercions, wrapped = coerce_args(found.obj, ctx.phase)
    filled: list[str] = []
    if args.get("phase") is None:
        args["phase"] = ctx.phase
        filled.append("phase")
    if ctx.phase == "talk" and args.get("say") is None:
        args["say"] = ""  # staying silent is allowed
        filled.append("say")
    trace = ParseTrace(
        source=found.source,
        act_calls=act_calls,
        fenced=found.fenced,
        prose=found.prose,
        arguments_as_text=found.arguments_as_text,
        syntax_fixes=found.fixes,
        unwrapped=wrapped,
        filled=tuple(filled),
        coercions=coercions,
    )
    errors = tuple(validate_act(args, ctx.phase, ctx.act))
    if errors:
        return _unsuccessful(ctx, attempt, max_attempts, "parse_failed", trace, errors)

    clean_args = normalize_act(args, ctx.phase)
    touched = _sanitized(clean_args, ctx.limits)
    return ParseResult(
        "ok",
        ctx.phase,
        attempt,
        args=clean_args,
        trace=trace,
        adjustments=_adjustments(clean_args, ctx),
        sanitized_by_field=touched,
    )


def abstain_status(phase: Phase) -> Literal["abstain", "kept_previous"]:
    """The action status of an abstention: an English revision keeps the earlier exit price."""
    return "kept_previous" if phase == "english_revise" else "abstain"
