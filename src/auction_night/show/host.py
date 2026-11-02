"""The offline host: reads the fact cards, writes the commentary, and is checked on every number.

After the night the host sees one fact card per lot and one for the night, with the hole cards
open, and writes one or two sentences per lot and a recap of the night. It is not trusted with
numbers. The flow for each text is the same:

1. The host writes. The text is cleaned (control characters, line breaks in per-lot lines).
2. Every number of the text must be on the card it was written from
   (:mod:`auction_night.show.numbers_check`). The text must also be non-empty, not absurdly long and
   free of the banned vocabulary.
3. If something is wrong, the host is told what (the numbers that are not on the card, for
   example) and writes again, **once**.
4. If the second text is wrong too, or the host gave no answer, the show plays the fact card
   itself: its headline, with ``fallback`` set. Nothing the host got wrong reaches the audience.

A text that passes carries ``numbers_checked``. A fallback text is built from the card, and its
numbers are checked as well, so it carries the flag too.

Who is the host is a matter of configuration. ``show.host_offline.kind == "mock"`` is
:class:`MockHost`, a deterministic template writer that needs no network; it can be told to put
a number that is not on the card into its first answer (or every answer) so that the check and
the rewrite can be exercised. Any other host is a :class:`ModelHost`: one request per text
through the model client, every attempt recorded in the cassette. A replay serves the recorded
attempts and compares the request hash, so it needs no network and no client, and a changed
prompt shows up as a drift.

The prompts live in ``prompts/zh/host.toml``. They carry the rules of the host, among them the
three bans: no comment on whether different players' strategies are alike, no jokes about real
people, brands or works, and no invented motives.
"""

import copy
import re
import time
import tomllib
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import cache
from importlib import resources
from types import MappingProxyType
from typing import Final, Literal, Protocol

from auction_night.contracts.config import EndpointConfig, NightConfig, RoleConfig
from auction_night.contracts.events import HostLineData, HostLineEvent
from auction_night.contracts.replay import HostBlock, HostLine
from auction_night.llm.accounting import Charge, Ledger
from auction_night.llm.cassette import CallKey, Cassette, CassetteEntry, Outcome, stored_request
from auction_night.llm.client import CallFailure, ClientReply, LlmClient, classify_exception
from auction_night.llm.request import BuiltRequest, build_request, request_hash, user_message
from auction_night.prompts import TemplateError, TemplateSet
from auction_night.show.cards import LotData
from auction_night.show.factcard import FactCard, FactCards, NightData
from auction_night.show.numbers_check import unlisted_keys, unlisted_numbers
from auction_night.show.predict import prediction_text, result_text

__all__ = [
    "BANNED_WORDS",
    "Commentary",
    "HostBackend",
    "HostJob",
    "HostOutput",
    "LineResult",
    "MockHost",
    "ModelHost",
    "Problem",
    "Rejection",
    "Rewrite",
    "clean_block",
    "clean_line",
    "find_problems",
    "host_block",
    "host_events",
    "host_templates_digest",
    "load_host_templates",
    "make_host",
    "recap_job",
    "lot_job",
    "run_host",
    "write_commentary",
]

# ----------------------------------------------------------------------------- limits

LOT_CHARS: Final = 80  # asked of the host for one lot
LOT_HARD_CHARS: Final = 160  # a longer text is rewritten
RECAP_LOW: Final = 300
RECAP_HIGH: Final = 500
RECAP_HARD_CHARS: Final = 1000
MAX_ECHO_CHARS: Final = 2000

# The vocabulary that public copy must not contain. The repository scan looks for the same
# words, and tools/scan_allow.txt exempts the lines of this list from it.
BANNED_WORDS: Final[tuple[str, ...]] = (
    "效率",  # banned-word list
    "福利",  # banned-word list
    "PoA",  # banned-word list
    "welfare",  # banned-word list
    "price of anarchy",  # banned-word list
    "efficiency",  # banned-word list
)
_BANNED: Final = re.compile(
    "|".join(
        rf"\b{re.escape(word)}\b" if word.isascii() else re.escape(word) for word in BANNED_WORDS
    ),
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------- templates

_TEMPLATE_FILE: Final = "host.toml"


@cache
def load_host_templates(lang: str = "zh") -> TemplateSet:
    """The host's templates, addressed as ``host.<key>``.

    They are read from ``prompts/<lang>/host.toml`` and are kept apart from the seat templates:
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
        texts[f"host.{key}"] = value.strip("\n")
    return TemplateSet(lang=lang, version=version, texts=MappingProxyType(texts))


def host_templates_digest(lang: str = "zh") -> str:
    """Digest of the host's templates, for the run manifest."""
    return load_host_templates(lang).digest


# ------------------------------------------------------------------------------ cleaning

_ANSI = re.compile(
    r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]|(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)?"
    r"|\x1b[PX^_][^\x1b]*(?:\x1b\\)?|\x1b[ -/]*[0-~]"
)


def _drop_controls(text: str, keep: str) -> str:
    text = _ANSI.sub("", text)
    return "".join(
        ch for ch in text if ch in keep or unicodedata.category(ch) not in {"Cc", "Cf", "Cs"}
    )


_LINE_BREAKS = dict.fromkeys(map(ord, "\t\n\v\f\r\x85\u2028\u2029"), " ")


def clean_line(text: str) -> str:
    """One line of host text: line breaks and tabs become spaces, other control characters
    go, and every run of white space is one space."""
    return " ".join(_drop_controls(text.translate(_LINE_BREAKS), keep="").split())


def clean_block(text: str) -> str:
    """Host text with paragraphs: line breaks stay, other control characters go, runs of blank
    lines shrink to one and trailing spaces are cut."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u2028", "\n")
    text = text.replace("\u2029", "\n\n")
    text = _drop_controls(text, keep="\n\t")
    lines = [" ".join(line.split()) for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# ------------------------------------------------------------------------------- checking


@dataclass(frozen=True, slots=True)
class Problem:
    """Something wrong with a host text. ``numbers`` and ``banned`` carry what was found."""

    code: Literal["numbers", "empty", "too_long", "banned"]
    detail: tuple[str, ...] = ()
    chars: int = 0


def find_problems(
    text: str | None, allowed: frozenset[str], *, hard_chars: int
) -> tuple[Problem, ...]:
    """What is wrong with a cleaned host text. An empty tuple means it may be played."""
    if not text:
        return (Problem("empty"),)
    problems = []
    outside = unlisted_keys(unlisted_numbers(text, allowed))
    if outside:
        problems.append(Problem("numbers", outside))
    if len(text) > hard_chars:
        problems.append(Problem("too_long", chars=len(text)))
    banned = tuple(dict.fromkeys(m.group(0) for m in _BANNED.finditer(text)))
    if banned:
        problems.append(Problem("banned", banned))
    return tuple(problems)


def _problem_lines(problems: tuple[Problem, ...], templates: TemplateSet, limit: int) -> str:
    lines = []
    for problem in problems:
        if problem.code == "numbers":
            numbers = "、".join(key.lstrip("?") for key in problem.detail)
            lines.append(templates.format("host.problem_numbers", numbers=numbers))
        elif problem.code == "empty":
            lines.append(templates.get("host.problem_empty"))
        elif problem.code == "too_long":
            lines.append(
                templates.format("host.problem_too_long", chars=problem.chars, limit=limit)
            )
        else:
            lines.append(templates.format("host.problem_banned", words="、".join(problem.detail)))
    return "\n".join(lines)


# ----------------------------------------------------------------------------------- jobs


@dataclass(frozen=True)
class HostJob:
    """One text to write: the card it comes from and the prompts."""

    kind: Literal["lot", "recap"]
    lot: int | None
    card: FactCard
    system: str
    user: str
    limit: int
    hard_chars: int


@dataclass(frozen=True)
class Rewrite:
    """The second try: what the host wrote the first time and what was wrong with it."""

    text: str
    message: str
    problems: tuple[Problem, ...]


def _system(templates: TemplateSet) -> str:
    bans = "\n".join(
        f"- {templates.get(key)}"
        for key in ("host.ban_similarity", "host.ban_real", "host.ban_motive")
    )
    return templates.format("host.system", bans=bans)


def lot_job(card: FactCard, templates: TemplateSet | None = None) -> HostJob:
    """The job for the commentary of one lot."""
    templates = templates or load_host_templates()
    assert card.kind == "lot" and card.lot is not None
    return HostJob(
        kind="lot",
        lot=card.lot,
        card=card,
        system=_system(templates),
        user=templates.format("host.lot_user", lot=card.lot, limit=LOT_CHARS, card=card.text),
        limit=LOT_CHARS,
        hard_chars=LOT_HARD_CHARS,
    )


def recap_job(card: FactCard, templates: TemplateSet | None = None) -> HostJob:
    """The job for the recap of the night."""
    templates = templates or load_host_templates()
    assert card.kind == "night"
    return HostJob(
        kind="recap",
        lot=None,
        card=card,
        system=_system(templates),
        user=templates.format("host.recap_user", low=RECAP_LOW, high=RECAP_HIGH, card=card.text),
        limit=RECAP_HIGH,
        hard_chars=RECAP_HARD_CHARS,
    )


def _rewrite_message(job: HostJob, problems: tuple[Problem, ...], templates: TemplateSet) -> str:
    listed = _problem_lines(problems, templates, job.limit)
    if job.kind == "lot":
        return templates.format("host.lot_retry", lot=job.lot, problems=listed, limit=job.limit)
    return templates.format("host.recap_retry", problems=listed, low=RECAP_LOW, high=RECAP_HIGH)


# --------------------------------------------------------------------------------- backends


class HostBackend(Protocol):
    """Whoever writes the commentary. ``write`` returns the raw text of the answer, or None
    when there was no answer at all (a timeout, a refusal, an error). With ``rewrite`` set it is
    the second try."""

    def write(self, job: HostJob, rewrite: Rewrite | None = None) -> str | None: ...


@dataclass
class MockHost:
    """A host without a model: deterministic template sentences made from the card data.

    ``bad_number`` puts a number that is not on the card into the answer: ``first`` only into
    the first try of a text (the rewrite is clean), ``always`` into every try (so the text falls
    back to the card). ``only`` limits that to some lots (``0`` stands for the recap); without
    it every text is affected. ``calls`` records ``(kind, lot, is_rewrite)`` of every call and
    ``rewrites`` the follow-ups the host was given.
    """

    bad_number: Literal["never", "first", "always"] = "never"
    only: frozenset[int] | None = None
    calls: list[tuple[str, int | None, bool]] = field(default_factory=list)
    rewrites: list[Rewrite] = field(default_factory=list)

    def write(self, job: HostJob, rewrite: Rewrite | None = None) -> str:
        self.calls.append((job.kind, job.lot, rewrite is not None))
        if rewrite is not None:
            self.rewrites.append(rewrite)
        data = job.card.data
        text = mock_lot_text(data) if isinstance(data, LotData) else mock_recap_text(data)
        target = job.lot if job.lot is not None else 0
        affected = self.only is None or target in self.only
        poisoned = self.bad_number == "always" or (self.bad_number == "first" and rewrite is None)
        if affected and poisoned:
            text += self._stray(job)
        return text

    @staticmethod
    def _stray(job: HostJob) -> str:
        bad = 7001
        while str(bad) in job.card.numbers:
            bad += 1
        sep = "\n\n" if job.kind == "recap" else ""
        return f"{sep}全场观众一共有 {bad} 人。"


def _who(line_seat: int, codename: str) -> str:
    return f"{line_seat} 号{codename}"


def mock_lot_text(data: LotData) -> str:
    """The sentences of the mock host for one lot: the result, a word on the cost cap when it
    silenced the models, then the first candidate or the largest regret of a model seat. Every
    number is read from the card data."""
    head = f"第 {data.lot} 件{data.name}，"
    if data.unsold or data.winner is None:
        text = head + "没人愿意出价，流拍。"
    else:
        winner = data.seat_line(data.winner)
        text = head + f"{_who(winner.seat, winner.codename)}以 {data.price} 拍得。"
    if data.muted:
        text += "费用上限已满，模型座位没有行动。"
    if data.candidates:
        return text + data.candidates[0].reason
    regrets = [
        (line.regret, line)
        for line in data.seats
        if line.role == "player" and line.regret is not None and line.regret > 0
    ]
    if regrets:
        top, line = max(regrets, key=lambda pair: (pair[0], -pair[1].seat))
        return text + f"{_who(line.seat, line.codename)}事后看，本可以多赚 {top}。"
    return text


def mock_recap_text(night: NightData) -> str:
    """The paragraphs of the mock host for the night."""
    ranked = sorted(
        (row for row in night.seats if row.rank_tonight is not None),
        key=lambda row: (row.rank_tonight or 0, row.seat),
    )
    first = [
        _who(row.seat, row.codename) + f"（总分 {row.total}）"
        for row in ranked
        if row.rank_tonight == (ranked[0].rank_tonight if ranked else None)
    ]
    paragraphs = [
        f"今晚 {night.n_lots} 件拍品，{night.n_seats} 个座位。"
        + (f"名次第一的是 {'、'.join(first)}。" if first else "")
    ]
    if night.muted_from is not None:
        paragraphs.append(f"从第 {night.muted_from} 件起，模型座位因为费用上限不再行动。")
    names = {row.seat: row.codename for row in night.seats}
    done = [r for r in night.missions if r.completed]
    paragraphs.append(
        "完成任务的有" + "、".join(_who(r.seat, names[r.seat]) for r in done) + "。"
        if done
        else "今晚没有人完成秘密任务。"
    )
    if night.candidates:
        best = night.candidates[0]
        paragraphs.append(f"最该回放的一幕在第 {best.lot} 件：{best.reason}")
    if night.lesson.topic != "none":
        paragraphs.append(f"课代表时间：{night.lesson.title}。{night.lesson.points[0]}")
    if night.predictions.hits is not None:
        paragraphs.append(
            f"赛前预测可对账 {night.predictions.judged} 条，命中 {night.predictions.hits} 条。"
        )
    return "\n\n".join(paragraphs)


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


class ModelHost:
    """A host that is a model, behind the client and the cassette.

    Each text is one request (the system prompt, then the card). The rewrite is the same request
    with the host's own answer and the follow-up appended, so the cached prefix stays the same
    bytes. Every attempt, including retries after a transient error, is a cassette entry with the
    key ``(night, lot, "host_lot" or "host_recap", 0, no seat, attempt)``.

    With a cassette in replay mode the host reads the recorded attempts, compares the request
    hash and never calls ``client`` (it may be None). A request that differs from the recording
    raises :class:`~auction_night.llm.cassette.CassetteDrift`. Cost is added to ``ledger`` under
    the label ``host`` when one is given. The host makes its calls one after another; it keeps a
    counter of attempts and is not meant to be shared between threads.
    """

    ROLE: Final = "host"

    def __init__(
        self,
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
            raise ValueError("a model host needs an llm role")
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
        self._attempts: dict[tuple[str, int | None], int] = {}

    def __repr__(self) -> str:
        return "ModelHost()"

    # ------------------------------------------------------------------------ requests

    def build(self, job: HostJob) -> BuiltRequest:
        """The first request of a job: no tools, the system prompt and the card."""
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

    # --------------------------------------------------------------------------- calls

    def write(self, job: HostJob, rewrite: Rewrite | None = None) -> str | None:
        request = self.build(job)
        if rewrite is not None:
            request = _follow_up(request, rewrite.text, rewrite.message)
        phase = "host_lot" if job.kind == "lot" else "host_recap"
        slot = (phase, job.lot)
        transient_left = self._transient_retries
        while True:
            if self._ledger is not None and self._ledger.blocked:
                self._ledger.note_aborted()
                return None
            self._attempts[slot] = self._attempts.get(slot, 0) + 1
            key = CallKey(self._night, job.lot, phase, 0, None, self._attempts[slot])
            entry, reply = self._attempt(key, request)
            failure = entry.failure
            if failure is None:
                if entry.outcome == "refusal":
                    return None
                return "" if reply is None else reply.reply.text
            if failure.kind == "error" and failure.retryable and transient_left > 0:
                transient_left -= 1
                if self._cassette.mode == "record":
                    self._sleep(min(30.0, 2.0 ** (self._transient_retries - transient_left - 1)))
                continue
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
            raise RuntimeError("recording the host needs a client")
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
            role=self.ROLE,
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
            self._ledger.fold([Charge(key, self.ROLE, usage, cost)])


def make_host(
    config: NightConfig,
    *,
    cassette: Cassette | None = None,
    client: LlmClient | None = None,
    ledger: Ledger | None = None,
    **options: object,
) -> HostBackend:
    """The host that ``config.show.host_offline`` describes.

    A ``mock`` host needs nothing else; ``options`` are its settings (``bad_number``, ``only``).
    A model host needs the cassette of the night, and a client unless the cassette is a replay;
    ``options`` go to :class:`ModelHost`, whose timeout and number of retries default to the
    ``limits`` of the configuration.
    """
    role = config.show.host_offline
    if role.kind == "mock":
        return MockHost(**options)  # type: ignore[arg-type]
    if cassette is None:
        raise ValueError("a model host needs the cassette of the night")
    assert role.endpoint is not None
    options.setdefault("timeout_s", float(config.limits.call_timeout_s))
    options.setdefault("transient_retries", config.limits.transient_retries)
    return ModelHost(
        role,
        config.endpoints[role.endpoint],
        cassette=cassette,
        night=config.night.id,
        client=client,
        ledger=ledger,
        **options,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------- the pipeline


@dataclass(frozen=True)
class Rejection:
    """A text that did not pass, kept for the audit: what it said and what was wrong."""

    text: str
    problems: tuple[Problem, ...]


@dataclass(frozen=True)
class LineResult:
    """The commentary of one lot, or the recap, as it will be played.

    ``attempts`` is 1 or 2 (the host's tries). ``rejected`` holds the texts that failed the
    check. ``fallback`` means the text is the card's headline, not the host's.
    """

    kind: Literal["lot", "recap"]
    lot: int | None
    text: str
    numbers_checked: bool
    fallback: bool
    attempts: int
    rejected: tuple[Rejection, ...] = ()


@dataclass(frozen=True)
class Commentary:
    """The commentary of a night: a line per lot, in lot order, and the recap."""

    lots: tuple[LineResult, ...]
    recap: LineResult

    @property
    def fallbacks(self) -> tuple[LineResult, ...]:
        """The texts that fell back to the fact card."""
        return tuple(r for r in (*self.lots, self.recap) if r.fallback)


def _clean(kind: str, raw: str | None) -> str | None:
    if raw is None:
        return None
    return clean_line(raw) if kind == "lot" else clean_block(raw)


def _played(job: HostJob, attempts: int, rejected: tuple[Rejection, ...]) -> LineResult:
    """The card's headline as the text to play, after the host failed the check."""
    text = _clean(job.kind, job.card.headline) or ""
    if unlisted_numbers(text, job.card.numbers):  # pragma: no cover - a bug in the card builder
        raise RuntimeError(f"the headline of the {job.kind} card has numbers that are not on it")
    return LineResult(job.kind, job.lot, text, True, True, attempts, rejected)


def _settle(job: HostJob, backend: HostBackend, templates: TemplateSet) -> LineResult:
    first = _clean(job.kind, backend.write(job))
    if first is None:
        return _played(job, 1, ())
    problems = find_problems(first, job.card.numbers, hard_chars=job.hard_chars)
    if not problems:
        return LineResult(job.kind, job.lot, first, True, False, 1)
    rejected = (Rejection(first, problems),)
    follow = Rewrite(first, _rewrite_message(job, problems, templates), problems)
    second = _clean(job.kind, backend.write(job, follow))
    if second is None:
        return _played(job, 2, rejected)
    again = find_problems(second, job.card.numbers, hard_chars=job.hard_chars)
    if not again:
        return LineResult(job.kind, job.lot, second, True, False, 2, rejected)
    return _played(job, 2, (*rejected, Rejection(second, again)))


def write_commentary(
    cards: FactCards, backend: HostBackend, *, templates: TemplateSet | None = None
) -> Commentary:
    """Run the host over every lot and the night, with the number check, one rewrite and the
    fallback to the fact card."""
    templates = templates or load_host_templates()
    lots = tuple(_settle(lot_job(card, templates), backend, templates) for card in cards.lots)
    recap = _settle(recap_job(cards.night, templates), backend, templates)
    return Commentary(lots=lots, recap=recap)


# -------------------------------------------------------------------------- events, block


@dataclass(frozen=True)
class HostOutput:
    """What the host stage produces: the commentary, the events for the log (audience only) and
    the block for the replay."""

    commentary: Commentary
    events: tuple[HostLineEvent, ...]
    block: HostBlock


def _program_line(kind: str, text: str, night: FactCard) -> HostLineData:
    if unlisted_numbers(text, night.numbers):  # pragma: no cover - a bug in the card builder
        raise RuntimeError(f"the {kind} text has numbers that are not on the night card")
    return HostLineData(kind=kind, text=text, numbers_checked=True)  # type: ignore[arg-type]


def host_events(cards: FactCards, commentary: Commentary) -> tuple[HostLineEvent, ...]:
    """The ``host_line`` events of the stage, in the order they are told: the pre-show
    prediction, the lots, the recap and the settlement of the predictions. They are numbered
    from 1; :func:`~auction_night.engine.night.append_events` renumbers them after the log."""
    lines: list[tuple[int | None, HostLineData]] = [
        (None, _program_line("prediction", prediction_text(), cards.night))
    ]
    for result in commentary.lots:
        lines.append((result.lot, _line_data(result)))
    lines.append((None, _line_data(commentary.recap)))
    night = cards.night.data
    assert isinstance(night, NightData)
    lines.append(
        (None, _program_line("prediction_result", result_text(night.predictions), cards.night))
    )
    return tuple(
        HostLineEvent(seq=index, lot=lot, vis="audience", data=data)
        for index, (lot, data) in enumerate(lines, start=1)
    )


def _line_data(result: LineResult) -> HostLineData:
    return HostLineData(
        kind=result.kind,
        text=result.text,
        numbers_checked=result.numbers_checked,
        fallback=result.fallback,
    )


def host_block(cards: FactCards, commentary: Commentary) -> HostBlock:
    """The ``host`` part of the replay: a line per lot, the recap and the prediction card."""
    night = cards.night.data
    assert isinstance(night, NightData)
    assert all(r.lot is not None for r in commentary.lots)
    return HostBlock(
        per_lot=[
            HostLine(lot=r.lot, text=r.text, fallback=r.fallback)  # type: ignore[arg-type]
            for r in commentary.lots
        ],
        recap=commentary.recap.text,
        prediction_card=night.predictions.card(),
    )


def run_host(
    cards: FactCards, backend: HostBackend, *, templates: TemplateSet | None = None
) -> HostOutput:
    """The whole host stage for a night: commentary, events and replay block."""
    commentary = write_commentary(cards, backend, templates=templates)
    return HostOutput(
        commentary=commentary,
        events=host_events(cards, commentary),
        block=host_block(cards, commentary),
    )
