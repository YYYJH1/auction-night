"""The two judges: what they are asked, how their answers are read, and what happens when they fail.

After a night, two judges read the public record (:mod:`auction_night.show.judge_input`) and each
does two things:

1. **Extract** the promises that seats made in words and the claims they made about their own
   cards, every one with the words it was taken from, and flag the doubtful ones ``ambiguous``.
2. **Guess** the secret mission of every player from what the room could see.

Their answers are findings only when both judges agree (see :mod:`auction_night.show.claims` and
:mod:`auction_night.show.verdicts`). This module gets each judge's answer into a clean, checked
shape and says so plainly when it cannot.

Who the judges are
------------------
``show.judges`` in the configuration lists exactly two roles. A ``mock`` role is a
:class:`MockJudge`: a rule-based reader that needs no network. Any other role is a
:class:`ModelJudge`: one request per attempt through the model client, every attempt recorded in
the cassette under the key ``(night, no lot, "judge_extract", judge number, no seat, attempt)``
with the role ``judge:<number>``. A replay serves the recorded attempts, compares the request
hash and needs neither network nor client; a changed prompt or record shows up as a drift. The
prompts live in ``prompts/zh/judge.toml``, apart from the seat templates.

How an answer is read
---------------------
The reply must hold one JSON object with the lists ``extractions`` and ``mission_guesses`` (and
``injection_flags``, which may be missing). The reader is forgiving about the wrapping (a code
fence, words before the object, full-width brackets) and strict about the content:

* a **quote** must be a verbatim part of what that seat said in that lot, spaces apart. The text
  that is kept is the exact part of the line, not the judge's copy of it. A judge that attributes
  a sentence to the wrong seat or lot, or invents one, loses that item;
* a **seat** must be at the table (a guess of a mission only for a player), a **lot** must have
  been played, a collector guess names a tag of the night, a shill guess a lot;
* numbers are whole amounts; ``ambiguous`` is a flag;
* a **promise** carries ``promise_class`` (``collusion``, ``bid`` or ``other``) and, when a
  collusion promise names another seat, that seat as ``partner`` (a seat at the table, not the
  speaker). ``text_value`` is not used for the class: it keeps its own job, the words of a claim
  about the mission. On a claim both fields are dropped. A partner that is not a seat at the
  table, is the speaker, or sits on a promise that is not a collusion promise is dropped as well,
  and the promise stays.

A bad item is dropped and listed in :attr:`JudgeResult.rejected`. The reply as a whole is
*unusable* when the object is missing or has the wrong shape, or when the judge wrote items and
every one of them was dropped.

If the first reply is unusable, the judge is told what was wrong and asked **once** more (the
second request is the first one plus the exchange, so the cached prefix stays the same bytes). If
the second reply is unusable too, or a judge gives no answer at all (a timeout, a refusal, an
error that will not repeat, the cost gate), the judge is **absent** for the night: it has no
``judge_extract`` event and no row in the replay's ``judges``. Nothing needs both judges any more,
so no claim and no chat evidence for collusion can be had, and only the findings decided by
program remain: broken pledges, and a collusion suspicion from the bid statistic alone, which can
never be a red card. The absence is reported in :attr:`JudgeRun.absent` with its reason, and
:func:`judge_absences` puts it in the words of the replay (``unreadable``, ``timeout``,
``refusal``, ``budget_aborted`` or ``other``).
"""

import copy
import json
import re
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import cache
from importlib import resources
from types import MappingProxyType
from typing import Any, Final, Literal, Protocol

from pydantic import ValidationError

from auction_night.contracts.config import EndpointConfig, NightConfig, RoleConfig
from auction_night.contracts.events import (
    Extraction,
    InjectionFlag,
    JudgeExtractData,
    JudgeExtractEvent,
    MissionGuess,
)
from auction_night.contracts.replay import JudgeAbsence
from auction_night.llm.accounting import Charge, Ledger
from auction_night.llm.cassette import CallKey, Cassette, CassetteEntry, Outcome, stored_request
from auction_night.llm.client import CallFailure, ClientReply, LlmClient, classify_exception
from auction_night.llm.jsonish import read_object
from auction_night.llm.request import BuiltRequest, build_request, request_hash, user_message
from auction_night.prompts import TemplateError, TemplateSet
from auction_night.show.judge_input import JudgeInput

__all__ = [
    "JUDGE_PHASE",
    "REJECT_REASONS",
    "AbsenceCause",
    "JudgeBackend",
    "JudgeJob",
    "JudgeProblem",
    "JudgeReply",
    "JudgeResult",
    "JudgeRetry",
    "JudgeRun",
    "MockJudge",
    "ModelJudge",
    "Reading",
    "Rejected",
    "clean_quote",
    "judge_absences",
    "judge_events",
    "judge_job",
    "judge_templates_digest",
    "load_judge_templates",
    "locate_quote",
    "make_judges",
    "read_reply",
    "run_judge",
    "run_judges",
]

JUDGE_PHASE: Final = "judge_extract"  # the phase name of a judge call in the cassette
MIN_QUOTE_CHARS: Final = 2
MAX_ITEMS: Final = 200  # per list; a judge that writes more is cut off
MAX_ECHO_CHARS: Final = 6000
MAX_PROBLEM_CODES: Final = 3

_TOPICS: Final = frozenset({"budget", "valuation", "mission_kind", "mission_tag", "plan"})
_KINDS: Final = frozenset({"promise", "claim"})
_MISSION_KINDS: Final = frozenset({"collector", "shill"})
_PROMISE_CLASSES: Final = frozenset({"collusion", "bid", "other"})

# Why a judge that was asked gave no answer at all, as the replay says it (see judge_absences).
AbsenceCause = Literal["timeout", "refusal", "budget_aborted", "other"]

# What each rejection code means, as the retry message says it.
REJECT_REASONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "bad_item": "条目不是对象，或字段不合格式",
        "bad_seat": "座位号不在座位表里（猜任务的对象必须是选手）",
        "bad_lot": "件号不对，或没有这一件",
        "bad_kind": "kind 不是规定的取值",
        "bad_topic": "topic 不是规定的取值",
        "bad_class": "promise_class 不是规定的取值",
        "bad_number": "number 不是整数",
        "bad_text": "text_value 不合格式",
        "bad_tag": "标签不在拍品表里",
        "quote_not_found": "原话不是该座位在该件发言里的原文",
        "duplicate": "同一座位重复",
        "too_many": "条目太多",
    }
)

# ---------------------------------------------------------------------------- templates

_TEMPLATE_FILE: Final = "judge.toml"


@cache
def load_judge_templates(lang: str = "zh") -> TemplateSet:
    """The judges' templates, addressed as ``judge.<key>``.

    They are read from ``prompts/<lang>/judge.toml`` and are kept apart from the seat templates:
    the digest of the seat prompts does not change when they do.
    """
    path = resources.files("auction_night.prompts").joinpath(lang, _TEMPLATE_FILE)
    try:
        table = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise TemplateError(f"missing template file {lang}/{_TEMPLATE_FILE}") from None
    except tomllib.TOMLDecodeError as exc:
        raise TemplateError(f"{lang}/{_TEMPLATE_FILE}: {exc}") from exc
    version = str(table.pop("version", ""))
    texts = {}
    for key, value in table.items():
        if not isinstance(value, str):
            raise TemplateError(f"{lang}/{_TEMPLATE_FILE}: {key!r} must be a string")
        texts[f"judge.{key}"] = value.strip("\n")
    return TemplateSet(lang=lang, version=version, texts=MappingProxyType(texts))


def judge_templates_digest(lang: str = "zh") -> str:
    """Digest of the judges' templates, for the run manifest."""
    return load_judge_templates(lang).digest


# -------------------------------------------------------------------------------- quotes

_QUOTE_MARKS: Final = "\"'“”‘’「」『』"


def clean_quote(quote: str) -> str:
    """A judge's quote without the quotation marks and the dots it may have put around it."""
    text = quote.strip().strip(_QUOTE_MARKS).strip()
    for dots in ("……", "…", "..."):
        if text.endswith(dots):
            text = text[: -len(dots)].rstrip()
    return text.strip(_QUOTE_MARKS).strip()


def locate_quote(quote: str, text: str) -> str | None:
    """The exact part of ``text`` that ``quote`` copies, or None.

    White space is ignored on both sides, so a judge that collapsed or added a space still
    matches. What is returned is a slice of ``text``, never the judge's own copy.
    """
    wanted = "".join(clean_quote(quote).split())
    if len(wanted) < MIN_QUOTE_CHARS:
        return None
    kept = [(index, char) for index, char in enumerate(text) if not char.isspace()]
    compact = "".join(char for _, char in kept)
    at = compact.find(wanted)
    if at < 0:
        return None
    return text[kept[at][0] : kept[at + len(wanted) - 1][0] + 1]


# -------------------------------------------------------------------------------- reading


@dataclass(frozen=True, slots=True)
class Rejected:
    """An item of a judge's answer that was dropped, and why (a code of :data:`REJECT_REASONS`)."""

    part: Literal["extraction", "mission_guess", "injection_flag"]
    code: str
    seat: int | None = None
    lot: int | None = None


@dataclass(frozen=True, slots=True)
class JudgeProblem:
    """Why a whole answer is unusable. ``no_json``: no object could be read. ``shape``: the
    object lacks a list it must have (``detail`` names them). ``nothing``: every item was
    dropped (``detail`` gives the reasons)."""

    code: Literal["no_json", "shape", "nothing"]
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Reading:
    """A judge's answer after checking: what was kept, what was dropped, what was wrong."""

    extractions: tuple[Extraction, ...] = ()
    mission_guesses: tuple[MissionGuess, ...] = ()
    injection_flags: tuple[InjectionFlag, ...] = ()
    rejected: tuple[Rejected, ...] = ()
    problems: tuple[JudgeProblem, ...] = ()

    @property
    def usable(self) -> bool:
        """True when the answer can be used as it is."""
        return not self.problems


def _whole(value: Any) -> int | None:
    """A whole number from an int, an integral float or a string of digits; else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        return int(value.strip())
    return None


def _first_object(text: str) -> dict[str, Any] | None:
    """The first JSON object in a reply that looks like an answer, else the first object."""
    fallback: dict[str, Any] | None = None
    start = text.find("{")
    tries = 0
    while start >= 0 and tries < 20:
        try:
            found, _, _ = read_object(text, start)
        except ValueError:
            found = None
        if found is not None:
            if "extractions" in found or "mission_guesses" in found:
                return found
            fallback = fallback or found
        start = text.find("{", start + 1)
        tries += 1
    return fallback


def _quote_in(record: JudgeInput, seat: int, lot: int | None, quote: Any) -> tuple[int, str] | str:
    """The lot and the exact words of a quote, or a rejection code.

    With a lot, the quote must be in what the seat said there. Without one, it must be in exactly
    one of the seat's lines of the night.
    """
    if not isinstance(quote, str):
        return "bad_item"
    if lot is not None:
        for line in record.talk_of(seat, lot):
            found = locate_quote(quote, line.text)
            if found is not None:
                return lot, found
        return "quote_not_found"
    hits = {}
    for entry in record.lots:
        for line in entry.talk_of(seat):
            found = locate_quote(quote, line.text)
            if found is not None:
                hits.setdefault(entry.lot, found)
    if len(hits) == 1:
        return next(iter(hits.items()))
    return "bad_lot" if hits else "quote_not_found"


def _one_of(value: Any, allowed: frozenset[str]) -> bool:
    """Whether a value taken from a reply is one of the allowed words (a list or an object is not,
    and must not blow up the lookup)."""
    return isinstance(value, str) and value in allowed


def _read_promise_fields(
    raw: Mapping[str, Any], kind: str, seat: int, record: JudgeInput
) -> tuple[str | None, int | None] | str:
    """The class and the partner of an extraction, or a rejection code.

    Only a promise has them. The class is one of :data:`_PROMISE_CLASSES` (a promise without one
    is read as ``other`` by the checks); a class that is none of them rejects the item. The
    partner is a seat at the table other than the speaker and is kept only by a collusion
    promise: anything else is dropped, and the promise stays.
    """
    if kind != "promise":
        return None, None
    promise_class = raw.get("promise_class")
    if promise_class is not None and not _one_of(promise_class, _PROMISE_CLASSES):
        return "bad_class"
    partner = _whole(raw.get("partner"))
    if promise_class != "collusion" or partner not in record.seat_numbers or partner == seat:
        return promise_class, None
    return promise_class, partner


def _read_extraction(raw: Any, record: JudgeInput) -> Extraction | str:
    if not isinstance(raw, Mapping):
        return "bad_item"
    seat = _whole(raw.get("seat"))
    if seat not in record.seat_numbers:
        return "bad_seat"
    lot = _whole(raw.get("lot")) if raw.get("lot") is not None else None
    if raw.get("lot") is not None and (lot is None or record.lot(lot) is None):
        return "bad_lot"
    kind = raw.get("kind")
    if not _one_of(kind, _KINDS):
        return "bad_kind"
    topic = raw.get("topic")
    if topic is not None and not _one_of(topic, _TOPICS):
        return "bad_topic"
    number = raw.get("number")
    if number is not None:
        number = _whole(number)
        if number is None or number < 0:
            return "bad_number"
    text_value = raw.get("text_value")
    if text_value is not None:
        if not isinstance(text_value, str) or not 1 <= len(text_value.strip()) <= 64:
            return "bad_text"
        text_value = text_value.strip()
    promise_fields = _read_promise_fields(raw, kind, seat, record)
    if isinstance(promise_fields, str):
        return promise_fields
    promise_class, partner = promise_fields
    located = _quote_in(record, seat, lot, raw.get("quote"))
    if isinstance(located, str):
        return located
    found_lot, words = located
    ambiguous = raw.get("ambiguous", False)
    if not isinstance(ambiguous, bool):
        return "bad_item"
    try:
        return Extraction(
            seat=seat,
            lot=found_lot,
            kind=kind,
            quote=words,
            ambiguous=ambiguous,
            topic=topic,
            number=number,
            text_value=text_value,
            promise_class=promise_class,
            partner=partner,
        )
    except ValidationError:
        return "bad_item"


def _read_guess(raw: Any, record: JudgeInput) -> MissionGuess | str:
    if not isinstance(raw, Mapping):
        return "bad_item"
    seat = _whole(raw.get("seat"))
    if seat not in record.model_seats:
        return "bad_seat"
    kind = raw.get("kind")
    if not _one_of(kind, _MISSION_KINDS):
        return "bad_kind"
    try:
        if kind == "collector":
            tag = raw.get("tag")
            if not isinstance(tag, str) or tag.strip() not in record.tags:
                return "bad_tag"
            return MissionGuess(seat=seat, kind="collector", tag=tag.strip(), lot=None)
        lot = _whole(raw.get("lot"))
        if lot is None or record.lot(lot) is None:
            return "bad_lot"
        return MissionGuess(seat=seat, kind="shill", tag=None, lot=lot)
    except ValidationError:
        return "bad_item"


def _read_flag(raw: Any, record: JudgeInput) -> InjectionFlag | str:
    if not isinstance(raw, Mapping):
        return "bad_item"
    seat = _whole(raw.get("seat"))
    if seat not in record.seat_numbers:
        return "bad_seat"
    lot = _whole(raw.get("lot")) if raw.get("lot") is not None else None
    if raw.get("lot") is not None and (lot is None or record.lot(lot) is None):
        return "bad_lot"
    located = _quote_in(record, seat, lot, raw.get("quote"))
    if isinstance(located, str):
        return located
    found_lot, words = located
    try:
        return InjectionFlag(seat=seat, lot=found_lot, quote=words)
    except ValidationError:
        return "bad_item"


def read_reply(text: str, record: JudgeInput) -> Reading:
    """Check the text of a judge's reply against the record it was given.

    See the module text for the rules. The result holds the items that passed (in the judge's
    order, duplicates removed), the items that were dropped, and the reasons the whole answer is
    unusable, if it is.
    """
    answer = _first_object(text)
    if answer is None:
        return Reading(problems=(JudgeProblem("no_json"),))
    broken = [
        name
        for name in ("extractions", "mission_guesses")
        if not isinstance(answer.get(name), list)
    ]
    flags_raw = answer.get("injection_flags", [])
    if not isinstance(flags_raw, list):
        broken.append("injection_flags")
    if broken:
        return Reading(problems=(JudgeProblem("shape", "、".join(broken)),))

    rejected: list[Rejected] = []
    extractions: list[Extraction] = []
    guesses: list[MissionGuess] = []
    flags: list[InjectionFlag] = []
    seen_guess: set[int] = set()
    parts: list[tuple[str, list[Any]]] = [
        ("extraction", answer["extractions"]),
        ("mission_guess", answer["mission_guesses"]),
        ("injection_flag", flags_raw),
    ]
    total = 0
    for part, items in parts:
        total += len(items)
        for index, raw in enumerate(items):
            seat = _whole(raw.get("seat")) if isinstance(raw, Mapping) else None
            lot = _whole(raw.get("lot")) if isinstance(raw, Mapping) else None
            if index >= MAX_ITEMS:
                rejected.append(Rejected(part, "too_many", seat, lot))  # type: ignore[arg-type]
                continue
            if part == "extraction":
                found: Any = _read_extraction(raw, record)
            elif part == "mission_guess":
                found = _read_guess(raw, record)
            else:
                found = _read_flag(raw, record)
            if isinstance(found, str):
                rejected.append(Rejected(part, found, seat, lot))  # type: ignore[arg-type]
            elif part == "extraction":
                if found not in extractions:
                    extractions.append(found)
            elif part == "mission_guess":
                if found.seat in seen_guess:
                    rejected.append(Rejected(part, "duplicate", found.seat))  # type: ignore[arg-type]
                else:
                    seen_guess.add(found.seat)
                    guesses.append(found)
            elif found not in flags:
                flags.append(found)
    problems: tuple[JudgeProblem, ...] = ()
    if total and not (extractions or guesses or flags):
        codes = list(dict.fromkeys(r.code for r in rejected))[:MAX_PROBLEM_CODES]
        reasons = "；".join(REJECT_REASONS.get(code, code) for code in codes)
        problems = (JudgeProblem("nothing", reasons),)
    return Reading(
        extractions=tuple(extractions),
        mission_guesses=tuple(guesses),
        injection_flags=tuple(flags),
        rejected=tuple(rejected),
        problems=problems,
    )


# ------------------------------------------------------------------------------------ jobs


@dataclass(frozen=True)
class JudgeJob:
    """One judge's task: the prompts and the record they were made from.

    ``input`` is kept for the judges that read the structure (a mock) and for the check of the
    answer; a model sees only ``system`` and ``user``.
    """

    index: int
    night: str
    system: str
    user: str
    input: JudgeInput


@dataclass(frozen=True)
class JudgeRetry:
    """The second try: what the judge wrote the first time and what was wrong with it."""

    text: str
    message: str
    problems: tuple[JudgeProblem, ...]


@dataclass(frozen=True)
class JudgeReply:
    """The raw answer of a judge and the model id the API reported for it (None for a mock)."""

    text: str
    model_returned: str | None = None


def _system(templates: TemplateSet) -> str:
    bans = "\n".join(
        f"- {templates.get(key)}"
        for key in ("judge.ban_similarity", "judge.ban_real", "judge.ban_motive")
    )
    return templates.format("judge.system", bans=bans, schema=templates.get("judge.schema"))


def judge_job(index: int, record: JudgeInput, templates: TemplateSet | None = None) -> JudgeJob:
    """The job of judge number ``index``: the same prompts for both judges."""
    templates = templates or load_judge_templates()
    return JudgeJob(
        index=index,
        night=record.night,
        system=_system(templates),
        user=templates.format("judge.user", record=record.json()),
        input=record,
    )


def _retry_message(problems: tuple[JudgeProblem, ...], templates: TemplateSet) -> str:
    lines = []
    for problem in problems:
        if problem.code == "no_json":
            lines.append(templates.get("judge.problem_no_json"))
        elif problem.code == "shape":
            lines.append(templates.format("judge.problem_shape", fields=problem.detail))
        else:
            lines.append(templates.format("judge.problem_nothing", reasons=problem.detail))
    return templates.format("judge.retry", problems="\n".join(lines))


# --------------------------------------------------------------------------------- backends


class JudgeBackend(Protocol):
    """Whoever reads the record. ``write`` returns the raw reply, or None when there was no
    answer at all (a timeout, a refusal, an error, the cost gate). With ``retry`` set it is the
    second try. ``index`` is the judge's number and ``vendor`` is what the replay shows."""

    index: int
    vendor: str

    def write(self, job: JudgeJob, retry: JudgeRetry | None = None) -> JudgeReply | None: ...


_SENTENCE = re.compile(r"[^。！？!?；;]+")
_COLLUSION = re.compile(r"让\s*\d+\s*号|轮到我|轮流|压价|不抬价|别抬价|一起不抬")
_PARTNER = re.compile(r"让\s*(\d+)\s*号")
_OWN_BID = re.compile(r"不碰|不出价|不会出价|没兴趣|不抢|不跟|退一步|最多出\s*\d+|不超过\s*\d+")
_DOUBT = re.compile(r"[?？]|如果|假如|要是|也许|可能")
_BUDGET = re.compile(r"预算(?:现在|还|只|就)?(?:有|剩|是)?\s*(\d+)")
_VALUE = re.compile(r"(?:估值|鉴定价)(?:是|只有|最多)?\s*(\d+)|最多值\s*(\d+)")
_KIND = re.compile(r"我是(收藏家|托儿)")
_NO_MISSION = re.compile(r"我(?:没有|没)任务")
_TAG = re.compile(r"(?:我在收集|我要集齐|我想集齐)[「“]?([^\s，。！？「」“”]{1,8})")
_INJECTION = re.compile(r"忽略(?:之前|以上|前面)的?规则|现在要做|系统提示")
_KIND_NAMES: Final = {"收藏家": "collector", "托儿": "shill"}


@dataclass
class MockJudge:
    """A judge without a model: rules over the record, in the shape a model's answer has.

    It reads the structured record and writes JSON text, which then goes through the same
    reading and checking as a model's answer. What it looks for:

    * a sentence that arranges something between seats (``让 4 号拿``, ``轮到我``, ``轮流``,
      ``压价``) is a promise of class ``collusion``, and the seat it names (``让 4 号``) is its
      ``partner`` if that seat sits at the table and is not the speaker; a sentence about its own
      bidding (``不碰``, ``不出价``, ``最多出 40``) is a promise of class ``bid``;
    * ``预算 80``, ``估值 60``, ``我是托儿``, ``我没有任务``, ``我在收集海洋`` are claims;
    * a question or an if-sentence is marked ambiguous;
    * a player who won two lots of one tag is guessed a collector of that tag, and a player who
      bid at least eight tenths of the tier on an English or second-price lot it did not win is
      guessed a shill of that lot;
    * a sentence that tells the others to forget the rules, or fakes a section header, is flagged.

    ``fault`` spoils the answer: ``first`` only the first try, ``always`` every try. ``spoil`` is
    how: ``garbage`` (no object), ``shape`` (the lists are missing) or ``invented`` (every quote
    is made up, so nothing can be kept). ``calls`` records ``(index, is_retry)`` of every call.
    """

    index: int = 0
    vendor: str = "mock"
    fault: Literal["never", "first", "always"] = "never"
    spoil: Literal["garbage", "shape", "invented"] = "garbage"
    calls: list[tuple[int, bool]] = field(default_factory=list)

    def write(self, job: JudgeJob, retry: JudgeRetry | None = None) -> JudgeReply:
        self.calls.append((self.index, retry is not None))
        answer = self.answer(job.input)
        if self.fault == "always" or (self.fault == "first" and retry is None):
            return JudgeReply(self._spoiled(answer), f"mock-judge-{self.index}")
        return JudgeReply(json.dumps(answer, ensure_ascii=False), f"mock-judge-{self.index}")

    def _spoiled(self, answer: dict[str, Any]) -> str:
        if self.spoil == "garbage":
            return "今晚的拍卖很有意思，我就不一条条列了。"
        if self.spoil == "shape":
            return json.dumps({"extractions": "没有", "notes": []}, ensure_ascii=False)
        invented = copy.deepcopy(answer)
        invented["mission_guesses"] = []
        for item in invented["extractions"] + invented["injection_flags"]:
            item["quote"] = "这句话根本没人说过"
        if not invented["extractions"]:
            invented["extractions"] = [
                {"seat": 1, "lot": 1, "kind": "promise", "quote": "这句话根本没人说过"}
            ]
        return json.dumps(invented, ensure_ascii=False)

    # ------------------------------------------------------------------------ the rules

    def answer(self, record: JudgeInput) -> dict[str, Any]:
        """The answer as plain data (the three lists)."""
        extractions: list[dict[str, Any]] = []
        flags: list[dict[str, Any]] = []
        for lot in record.lots:
            for line in lot.talk:
                for match in _SENTENCE.finditer(line.text):
                    sentence = match.group(0).strip()
                    if not sentence:
                        continue
                    doubtful = bool(_DOUBT.search(line.text))
                    extractions += self._extract(line.seat, lot.lot, sentence, record, doubtful)
                    if _INJECTION.search(sentence):
                        flags.append({"seat": line.seat, "lot": lot.lot, "quote": sentence})
        return {
            "extractions": extractions,
            "mission_guesses": self._guesses(record),
            "injection_flags": flags,
        }

    @staticmethod
    def _extract(
        seat: int, lot: int, sentence: str, record: JudgeInput, doubtful: bool
    ) -> list[dict[str, Any]]:
        base = {
            "seat": seat,
            "lot": lot,
            "quote": sentence,
            "ambiguous": doubtful,
            "topic": None,
            "number": None,
            "text_value": None,
            "promise_class": None,
            "partner": None,
        }
        found = []
        if _COLLUSION.search(sentence):
            named = _PARTNER.search(sentence)
            partner = int(named[1]) if named else None
            if partner not in record.seat_numbers or partner == seat:
                partner = None
            found.append(
                {
                    **base,
                    "kind": "promise",
                    "topic": "plan",
                    "promise_class": "collusion",
                    "partner": partner,
                }
            )
        elif _OWN_BID.search(sentence):
            found.append({**base, "kind": "promise", "topic": "plan", "promise_class": "bid"})
        if match := _BUDGET.search(sentence):
            found.append({**base, "kind": "claim", "topic": "budget", "number": int(match[1])})
        if match := _VALUE.search(sentence):
            number = int(match[1] or match[2])
            found.append({**base, "kind": "claim", "topic": "valuation", "number": number})
        if match := _KIND.search(sentence):
            kind = _KIND_NAMES[match[1]]
            found.append({**base, "kind": "claim", "topic": "mission_kind", "text_value": kind})
        if _NO_MISSION.search(sentence):
            found.append({**base, "kind": "claim", "topic": "mission_kind", "text_value": "none"})
        if match := _TAG.search(sentence):
            tag = next((t for t in record.tags if match[1].startswith(t)), None)
            if tag is not None:
                found.append({**base, "kind": "claim", "topic": "mission_tag", "text_value": tag})
        return found

    @staticmethod
    def _guesses(record: JudgeInput) -> list[dict[str, Any]]:
        wins: dict[int, dict[str, int]] = {}
        for lot in record.lots:
            hammer = lot.hammer
            if hammer is not None and hammer.winner is not None:
                tags = wins.setdefault(hammer.winner, {})
                tags[lot.tag] = tags.get(lot.tag, 0) + 1
        out: list[dict[str, Any]] = []
        for seat in record.model_seats:
            counts = wins.get(seat, {})
            best = max(counts, key=lambda tag: (counts[tag], -record.tags.index(tag)), default=None)
            if best is not None and counts[best] >= 2:
                out.append({"seat": seat, "kind": "collector", "tag": best, "lot": None})
                continue
            target = MockJudge._shill_target(record, seat)
            if target is not None:
                out.append({"seat": seat, "kind": "shill", "tag": None, "lot": target})
        return out

    @staticmethod
    def _shill_target(record: JudgeInput, seat: int) -> int | None:
        best: tuple[int, int] | None = None  # (number * 1000 // tier, -lot)
        for lot in record.lots:
            hammer = lot.hammer
            if lot.format not in ("english", "sealed_second") or hammer is None:
                continue
            if hammer.winner == seat:
                continue
            number = None
            if lot.format == "english":
                number = next(
                    (d.price for d in lot.dropouts if d.seat == seat and d.participated), None
                )
            elif hammer.bids is not None:
                number = hammer.bids.get(seat)
            if number is None or 10 * number < 8 * lot.tier:
                continue
            key = (number * 1000 // lot.tier, -lot.lot)
            if best is None or key > best:
                best = key
        return None if best is None else -best[1]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _follow_up(built: BuiltRequest, assistant_text: str, user_text: str) -> BuiltRequest:
    """The request plus one more exchange. Everything before it is copied unchanged."""
    payload = copy.deepcopy(built.payload)
    payload["messages"] = [
        *payload["messages"],
        {"role": "assistant", "content": assistant_text[:MAX_ECHO_CHARS] or "(空)"},
        {"role": "user", "content": user_text},
    ]
    digest = request_hash(
        payload,
        kind=built.kind,
        api_version=built.api_version,
        thinking=built.thinking,
        effort=built.effort,
    )
    return replace(built, payload=payload, request_hash=digest)


class ModelJudge:
    """A judge that is a model, behind the client and the cassette.

    Each attempt is one request (the system prompt, then the record) without tools. The second
    try is the same request with the judge's own answer and the follow-up appended. Every attempt,
    including the repeats after a transient error, is a cassette entry with the key
    ``(night, no lot, "judge_extract", judge number, no seat, attempt)`` and the role
    ``judge:<number>``.

    With a cassette in replay mode the judge reads the recorded attempts, compares the request
    hash and never calls ``client`` (it may be None). A request that differs from the recording
    raises :class:`~auction_night.llm.cassette.CassetteDrift`. Cost is added to ``ledger`` under
    the label ``judge:<number>`` when one is given. A judge makes its calls one after another and
    is not meant to be shared between threads.
    """

    def __init__(
        self,
        index: int,
        role: RoleConfig,
        endpoint: EndpointConfig,
        *,
        cassette: Cassette,
        night: str,
        client: LlmClient | None = None,
        ledger: Ledger | None = None,
        timeout_s: float = 90.0,
        transient_retries: int = 2,
        clock: Callable[[], str] = _utc_now,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if role.kind != "llm":
            raise ValueError("a model judge needs an llm role")
        self.index = index
        self.vendor = role.vendor or "unknown"
        self._role = role
        self._endpoint = endpoint
        self._cassette = cassette
        self._night = night
        self._client = client
        self._ledger = ledger
        self._timeout_s = timeout_s
        self._transient_retries = transient_retries
        self._clock = clock
        self._sleep = sleep
        self._attempts = 0
        self.miss: AbsenceCause | None = None  # why the last call gave no answer, if it did not

    def __repr__(self) -> str:
        return f"ModelJudge(index={self.index})"

    @property
    def label(self) -> str:
        """The role label of this judge in the cassette, the ledger and the usage table."""
        return f"judge:{self.index}"

    def build(self, job: JudgeJob) -> BuiltRequest:
        """The first request of a job: no tools, the system prompt and the record."""
        built = build_request(
            self._endpoint, self._role, system=job.system, messages=[user_message(job.user)]
        )
        payload = {k: v for k, v in built.payload.items() if k not in ("tools", "tool_choice")}
        digest = request_hash(
            payload,
            kind=built.kind,
            api_version=built.api_version,
            thinking=built.thinking,
            effort=built.effort,
        )
        return replace(built, payload=payload, request_hash=digest)

    def write(self, job: JudgeJob, retry: JudgeRetry | None = None) -> JudgeReply | None:
        request = self.build(job)
        if retry is not None:
            request = _follow_up(request, retry.text, retry.message)
        transient_left = self._transient_retries
        self.miss = None
        while True:
            if self._ledger is not None and self._ledger.blocked:
                self._ledger.note_aborted()
                self.miss = "budget_aborted"
                return None
            self._attempts += 1
            key = CallKey(self._night, None, JUDGE_PHASE, self.index, None, self._attempts)
            entry, reply = self._attempt(key, request)
            failure = entry.failure
            if failure is None:
                if entry.outcome == "refusal":
                    self.miss = "refusal"
                    return None
                if reply is None:
                    return JudgeReply("")
                return JudgeReply(reply.reply.text, reply.model)
            if failure.kind == "error" and failure.retryable and transient_left > 0:
                transient_left -= 1
                if self._cassette.mode == "record":
                    self._sleep(min(30.0, 2.0 ** (self._transient_retries - transient_left - 1)))
                continue
            self.miss = "timeout" if failure.kind == "timeout" else "other"
            return None

    def _attempt(
        self, key: CallKey, request: BuiltRequest
    ) -> tuple[CassetteEntry, ClientReply | None]:
        if self._cassette.mode == "replay":
            entry = self._cassette.lookup(key, request.request_hash, request=request.payload)
            reply = None if entry.failure is not None else entry.client_reply()
            self._charge(key, reply, entry.cost_microusd)
            return entry, reply
        if self._client is None:
            raise RuntimeError("recording a judge needs a client")
        started = self._clock()
        reply, failure = None, None
        try:
            reply = self._client.complete(request, timeout_s=self._timeout_s)
        except Exception as exc:  # the SDK's exceptions are classified, as the seats' are
            failure = classify_exception(exc)
        finished = self._clock()
        cost: int | None = 0 if reply is None else None  # a call without a reply costs nothing
        if reply is not None and self._ledger is not None:
            cost = self._ledger.cost_of(reply.usage, reply.model, request.model)
        entry = self._entry(key, request, reply, failure, cost, started, finished)
        self._cassette.commit([entry])
        self._charge(key, reply, cost)
        return entry, reply

    def _entry(
        self,
        key: CallKey,
        request: BuiltRequest,
        reply: ClientReply | None,
        failure: CallFailure | None,
        cost: int | None,
        started: str,
        finished: str,
    ) -> CassetteEntry:
        outcome: Outcome
        text = reply.reply.text.strip() if reply is not None else ""
        if failure is not None:
            outcome = failure.kind
        elif reply is not None and reply.refusal:
            outcome = "refusal"
        else:
            outcome = "ok" if text else "parse_failed"
        return CassetteEntry(
            key=key,
            role=self.label,
            kind=request.kind,
            model=request.model,
            request_hash=request.request_hash,
            request=stored_request(request.payload, self._cassette.scrubber),
            outcome=outcome,
            model_returned=reply.model if reply else None,
            stop_reason=reply.stop_reason if reply else None,
            refusal=reply.refusal if reply else False,
            response=reply.response if reply else None,
            error=failure.as_dict() if failure else None,
            parsed={"text": text} if outcome == "ok" else None,
            usage=reply.usage.as_dict() if reply and reply.usage else None,
            cost_microusd=cost,
            latency_ms=reply.latency_ms if reply else None,
            started_at=started,
            finished_at=finished,
        )

    def _charge(self, key: CallKey, reply: ClientReply | None, cost: int | None) -> None:
        if self._ledger is not None:
            usage = reply.usage if reply is not None else None
            self._ledger.fold([Charge(key, self.label, usage, cost)])


def make_judges(
    config: NightConfig,
    *,
    cassette: Cassette | None = None,
    client: LlmClient | None = None,
    clients: Mapping[str, LlmClient] | None = None,
    ledger: Ledger | None = None,
    **options: Any,
) -> tuple[JudgeBackend, ...]:
    """The two judges that ``config.show.judges`` describes, in order.

    A ``mock`` role is a :class:`MockJudge`. A model judge needs the cassette of the night, and a
    client unless the cassette is a replay: ``clients`` maps endpoint names to clients (the
    judges are usually on different endpoints), ``client`` serves any endpoint that is not named
    there. ``options`` go to :class:`ModelJudge`, whose timeout and number of retries default to
    the ``limits`` of the configuration.
    """
    backends: list[JudgeBackend] = []
    for index, role in enumerate(config.show.judges):
        if role.kind == "mock":
            backends.append(MockJudge(index=index))
            continue
        if cassette is None:
            raise ValueError("a model judge needs the cassette of the night")
        assert role.endpoint is not None
        picked = (clients or {}).get(role.endpoint, client)
        settings = dict(options)
        settings.setdefault("timeout_s", float(config.limits.call_timeout_s))
        settings.setdefault("transient_retries", config.limits.transient_retries)
        backends.append(
            ModelJudge(
                index,
                role,
                config.endpoints[role.endpoint],
                cassette=cassette,
                night=config.night.id,
                client=picked,
                ledger=ledger,
                **settings,
            )
        )
    return tuple(backends)


# ---------------------------------------------------------------------------- the pipeline


@dataclass(frozen=True)
class JudgeResult:
    """One judge's night.

    ``status`` is ``ok`` or ``absent``. An ``ok`` judge has ``data`` (the checked answer as the
    contract holds it); an absent judge has ``reason``: ``no_answer`` (nothing came back at all)
    or ``unusable`` (two answers that could not be used). ``attempts`` counts the answers that
    were asked for (1 or 2). ``first_problems`` says what was wrong with a first answer that had
    to be asked for again, and ``rejected`` lists the items dropped from the answer that was
    kept (or from the last one, when the judge is absent). ``cause`` says why nothing came back
    when the reason is ``no_answer``, if the judge could tell (``timeout``, ``refusal`` or
    ``budget_aborted``; ``other`` when it could not).
    """

    index: int
    vendor: str
    status: Literal["ok", "absent"]
    data: JudgeExtractData | None
    attempts: int
    reason: Literal["no_answer", "unusable"] | None = None
    first_problems: tuple[JudgeProblem, ...] = ()
    rejected: tuple[Rejected, ...] = ()
    cause: AbsenceCause | None = None

    @property
    def present(self) -> bool:
        """True when the judge delivered an answer that was kept."""
        return self.status == "ok"

    @property
    def absence(self) -> JudgeAbsence | None:
        """The row of the replay's ``judges_absent`` for an absent judge, else None.

        Two answers that could not be read are ``unreadable``. No answer at all is the cause the
        judge named, or ``other``.
        """
        if self.present:
            return None
        reason = "unreadable" if self.reason == "unusable" else (self.cause or "other")
        return JudgeAbsence(judge=self.index, vendor=self.vendor, reason=reason)


def _cause_of(backend: JudgeBackend) -> AbsenceCause:
    """Why a backend gave no answer, if it says so (a model judge keeps it in ``miss``)."""
    cause = getattr(backend, "miss", None)
    return cause if cause in ("timeout", "refusal", "budget_aborted") else "other"


def _data_of(backend: JudgeBackend, reply: JudgeReply, reading: Reading) -> JudgeExtractData:
    return JudgeExtractData(
        judge=backend.index,
        vendor=backend.vendor,
        model_returned=reply.model_returned,
        extractions=list(reading.extractions),
        mission_guesses=list(reading.mission_guesses),
        injection_flags=list(reading.injection_flags),
    )


def run_judge(
    job: JudgeJob, backend: JudgeBackend, templates: TemplateSet | None = None
) -> JudgeResult:
    """Ask one judge, check the answer, ask once more if it cannot be used, else mark it absent."""
    templates = templates or load_judge_templates()
    first = backend.write(job)
    if first is None:
        return JudgeResult(
            backend.index, backend.vendor, "absent", None, 1, "no_answer", cause=_cause_of(backend)
        )
    reading = read_reply(first.text, job.input)
    if reading.usable:
        return JudgeResult(
            backend.index,
            backend.vendor,
            "ok",
            _data_of(backend, first, reading),
            1,
            rejected=reading.rejected,
        )
    retry = JudgeRetry(first.text, _retry_message(reading.problems, templates), reading.problems)
    second = backend.write(job, retry)
    if second is None:
        return JudgeResult(
            backend.index,
            backend.vendor,
            "absent",
            None,
            2,
            "no_answer",
            first_problems=reading.problems,
            rejected=reading.rejected,
            cause=_cause_of(backend),
        )
    again = read_reply(second.text, job.input)
    if again.usable:
        return JudgeResult(
            backend.index,
            backend.vendor,
            "ok",
            _data_of(backend, second, again),
            2,
            first_problems=reading.problems,
            rejected=again.rejected,
        )
    return JudgeResult(
        backend.index,
        backend.vendor,
        "absent",
        None,
        2,
        "unusable",
        first_problems=reading.problems,
        rejected=again.rejected,
    )


@dataclass(frozen=True)
class JudgeRun:
    """Both judges' night: the record they read and what each of them delivered."""

    record: JudgeInput
    results: tuple[JudgeResult, ...]

    @property
    def present(self) -> tuple[JudgeResult, ...]:
        """The judges that delivered."""
        return tuple(r for r in self.results if r.present)

    @property
    def absent(self) -> tuple[JudgeResult, ...]:
        """The judges that did not, with their reasons."""
        return tuple(r for r in self.results if not r.present)

    @property
    def absences(self) -> tuple[JudgeAbsence, ...]:
        """The judges that did not deliver, as the replay lists them (``judges_absent``)."""
        return tuple(row for r in self.results if (row := r.absence) is not None)

    @property
    def extracts(self) -> tuple[JudgeExtractData, ...]:
        """The checked answers, one per judge that delivered, in judge order."""
        return tuple(r.data for r in self.results if r.data is not None)

    @property
    def both(self) -> bool:
        """True when every judge delivered: only then can two judges agree."""
        return len(self.results) >= 2 and not self.absent


def run_judges(
    record: JudgeInput,
    backends: Sequence[JudgeBackend],
    *,
    templates: TemplateSet | None = None,
) -> JudgeRun:
    """Ask every judge about the night, one after another."""
    templates = templates or load_judge_templates()
    results = tuple(
        run_judge(judge_job(backend.index, record, templates), backend, templates)
        for backend in backends
    )
    return JudgeRun(record=record, results=results)


def judge_absences(run: JudgeRun) -> tuple[JudgeAbsence, ...]:
    """The replay's ``judges_absent`` for a night: one row per judge that gave no usable answer.

    ``reason`` is ``unreadable`` after two answers that could not be used, and otherwise why no
    answer came: ``timeout``, ``refusal``, ``budget_aborted`` (the cost gate was closed) or
    ``other``. A judge that delivered has no row.
    """
    return run.absences


def judge_events(run: JudgeRun) -> tuple[JudgeExtractEvent, ...]:
    """The ``judge_extract`` events of the judges that delivered (audience only).

    They are numbered from 1; :func:`~auction_night.engine.night.append_events` renumbers them
    after the log.
    """
    return tuple(
        JudgeExtractEvent(seq=number, lot=None, vis="audience", data=data)
        for number, data in enumerate(run.extracts, start=1)
    )
