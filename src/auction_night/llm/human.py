"""A person at a seat of the night, and the record of what the person typed.

The night driver (:mod:`auction_night.llm.driver`) answers a ``bot`` seat on the spot and a model
seat through a call. A seat of kind ``human`` is answered on the spot too, but by a person:
:class:`HumanSeat` shows the engine's request and reads the answer from an :class:`InputSource`.

What the person sees
--------------------
The text of a request is :attr:`HumanTurn.observation`: the very string that a model at the same
seat would get as its user message, made by the same renderer from the same templates. The first
turn also shows the system text of the seat (the rules and the private cards), once. Nothing else
about the game is shown: a turn carries the seat's own numbers and public facts, never the state
of the engine, the cards of other seats or the reference values of the theory layer.

What the person types
---------------------
Numbers and keys, read by small parsers of this module. The parsing ladder of the model layer is
not involved, and a wrong entry costs nothing: it is refused on the spot with a reason and asked
for again, with no retry counted. The rules are the engine's own:

* a bid, an exit price or a grab price is a whole number within the remaining budget;
* an English opening price is 0 (not taking part) or at least the reserve, and a revision is not
  below the current price (type the current price to leave now; an empty line keeps the exit price
  in force);
* a Dutch grab price is 0 or the price of a level the seat can afford;
* a chat line goes through the same cleaning as a model's, a pledge is checked like a model's, and
  a guess or the plan may be skipped with an empty line.

Each phase has its own clock (``timeout_s``, none by default). A phase that is not answered in
time is an abstention with the reason ``timeout``, exactly like a model call that timed out. The
clock and the input source are injected, so nothing here needs a terminal.

The record
----------
Every answer is also written as one line of a *tape* (``human.jsonl``): the request it belongs to
(position, lot, phase), the answer as the engine took it, and every line that was typed for it, the
refused ones included. A replay needs no person: :class:`ReplayedHuman` hands the recorded answers
back in place of the person, so the log comes out byte for byte the same. The typed lines are the
raw material of the answer: typed again at the same request they give the same answer.
"""

import json
import re
import signal
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType, TracebackType
from typing import IO, Any, Final, Protocol, Self, TypeVar

from auction_night.contracts.config import NightConfig
from auction_night.contracts.events import (
    PRIORITY_MAX,
    AnyEvent,
    MissionResultEvent,
    NightEndEvent,
    NightStartEvent,
    Pledge,
    canonical_json,
)
from auction_night.contracts.mechanism import Phase
from auction_night.engine.night import Answer, AnswerError
from auction_night.llm.cassette import CassetteError
from auction_night.llm.parse import ParseContext
from auction_night.llm.render import RenderSettings, render_results, visible_log
from auction_night.llm.sanitize import clean_persona, clean_say

__all__ = [
    "PHASE_NAMES",
    "TAPE_FORMAT",
    "HumanInputError",
    "HumanPlayer",
    "HumanSeat",
    "HumanSeatError",
    "HumanTapeError",
    "HumanTurn",
    "InputClosed",
    "InputSource",
    "InputTimeout",
    "Presenter",
    "ReplayedHuman",
    "Tape",
    "TapeEntry",
    "TapeSink",
    "TapeWriter",
    "answer_from_json",
    "answer_to_json",
    "bid_problem",
    "hint_for",
    "interruptible",
    "load_tape",
    "parse_whole",
    "recap_lines",
    "seat_a_person",
]

TAPE_FORMAT: Final = "auction-night/human-tape@1"
PHASE_NAMES: Final[Mapping[Phase, str]] = {
    "plan": "开场计划",
    "talk": "喊话",
    "english_init": "英式 · 初报退出价",
    "english_revise": "英式 · 修改退出价",
    "dutch": "荷式 · 报出手价",
    "sealed": "密封出价",
}
_NUMBER_NAMES: Final[Mapping[str, str]] = {
    "english_init": "退出价",
    "english_revise": "退出价",
    "dutch": "出手价",
    "sealed": "出价",
}
_TYPED_KEPT: Final = 200  # lines kept per answer on the tape
_TYPED_CHARS: Final = 2000  # and how much of each
_WHOLE: Final = re.compile(r"[0-9]{1,9}")
_SEPARATORS: Final = re.compile(r"[\s,，、;；]+")
_PLEDGE_KEYS: Final[Mapping[str, str]] = {"1": "no_bid", "2": "max_bid", "3": "let_win"}

T = TypeVar("T")


class HumanInputError(RuntimeError):
    """Base class of the ways the input of a person can end without an answer."""


class InputTimeout(HumanInputError):
    """Nothing was typed within the time that was left."""


class InputClosed(HumanInputError):
    """The input ended (end of file, or Ctrl-D at a terminal): the person has left the table."""


class HumanSeatError(ValueError):
    """A person cannot be seated as asked. The message is meant for the person at the command
    line."""


class HumanTapeError(CassetteError):
    """The record of a person's inputs cannot be read, or does not fit the night."""


# ------------------------------------------------------------------------------ interfaces


class InputSource(Protocol):
    """Where the person's lines come from: a terminal, or a script in a test."""

    def read(self, timeout_s: float | None) -> str:
        """One line without its line break. ``timeout_s`` is the time that is left (None: no
        limit). Raises :class:`InputTimeout` when it runs out and :class:`InputClosed` at the end
        of the input."""

    def flush(self) -> None:
        """Forget whatever was typed ahead of the next question."""


class Presenter(Protocol):
    """What the person sees. Every text that reaches it is the person's own or the game's: the
    presenter must still treat it as untrusted and never as markup."""

    def briefing(self, title: str, text: str) -> None:
        """The rules and the private cards of the seat, shown once before the first question."""

    def turn(self, title: str, observation: str, hint: str) -> None:
        """One request: its title, the text a model would be given, and how to answer."""

    def prompt(self, label: str, remaining_s: float | None) -> None:
        """The question that is asked now; ``remaining_s`` is the time left for the phase."""

    def notice(self, text: str, *, problem: bool = False) -> None:
        """A remark: an entry that was refused (``problem``), or a confirmation."""

    def summary(self, title: str, lines: list[str]) -> None:
        """The account of the night, shown once at the end."""


@dataclass(frozen=True)
class HumanTurn:
    """One request of the engine, as the person at the seat is asked it.

    ``observation`` is the user message that a model at this seat would be given and ``system``
    its system text. ``ctx`` holds what the parsing ladder knows about a model's call: the phase,
    the ranges (lots, seats), the remaining budget of a bid phase, the current price of an English
    revision and the length limits of the texts. The other fields are the seat's own numbers and
    public facts: ``reserve`` of the lot, ``own_exit`` (the exit price in force in an English
    revision), ``guess_target`` (the seat to read, None when there is nobody) and ``ladder`` (the
    prices of the Dutch levels, highest first).
    """

    seat: int
    codename: str
    index: int
    lot: int | None
    ctx: ParseContext
    observation: str
    system: str
    reserve: int = 0
    own_exit: int | None = None
    guess_target: int | None = None
    ladder: tuple[int, ...] = ()

    @property
    def phase(self) -> Phase:
        return self.ctx.phase


class HumanPlayer(Protocol):
    """What the driver needs from a person at a seat."""

    def answer(self, turn: HumanTurn) -> Answer:
        """The answer to one request. May raise :class:`HumanInputError` when the person is gone."""


class TapeSink(Protocol):
    """Where a seat puts the record of each answer (a :class:`TapeWriter`, or a list in a test)."""

    def append(self, entry: "TapeEntry") -> None:
        """Take one answer."""


# ------------------------------------------------------------------------------ parsing


def parse_whole(text: str) -> int | None:
    """A whole number typed by a person, or None. Full-width digits, as an input method types
    them, count as the plain digits they stand for. Signs, decimals, separators, units and number
    words are refused."""
    folded = unicodedata.normalize("NFKC", text).strip()
    return int(folded) if _WHOLE.fullmatch(folded) else None


def _whole_list(text: str) -> list[int] | None:
    """Whole numbers separated by spaces or commas, or None when one of them is not."""
    tokens = [token for token in _SEPARATORS.split(text.strip()) if token]
    numbers = [parse_whole(token) for token in tokens]
    if not tokens or any(number is None for number in numbers):
        return None
    return [number for number in numbers if number is not None]


def bid_problem(turn: HumanTurn, value: int) -> str | None:
    """Why ``value`` cannot be the number of the bid phase of ``turn``, or None when it can.

    The rules are the engine's: an amount is within the remaining budget, an English opening
    price is 0 (not taking part) or reaches the reserve, an English revision is not below the
    current price, and a Dutch grab price is 0 or the price of a level. Where the engine would
    quietly turn an entry into something else (a price below the reserve into "not taking part", a
    price between two levels into the lower one) the person is asked to say what is meant.
    """
    left = turn.ctx.budget_left
    if left is not None and value > left:
        return f"超过你的剩余预算 {left}。"
    phase = turn.phase
    if phase == "english_init":
        if 0 < value < max(turn.reserve, 1):
            return f"低于保留价 {turn.reserve} 等于不参加；不想参加请填 0。"
    elif phase == "english_revise":
        price = turn.ctx.current_price
        if price is not None and value < price:
            return f"不能低于当前价 {price}；想现在退出就填 {price}。"
    elif phase == "dutch" and value != 0 and value not in turn.ladder:
        steps = [price for price in turn.ladder if left is None or price <= left]
        if not steps:
            return "你买不起任何一档，只能填 0。"
        return f"出手价要么填 0（不参加），要么是某一档的价格。你买得起的档位：{_listed(steps)}。"
    return None


def _listed(numbers: Iterable[int]) -> str:
    return "、".join(str(number) for number in numbers)


# ------------------------------------------------------------------------------ the tape


@dataclass(frozen=True)
class TapeEntry:
    """One answer of a person: which request it belongs to, what it was, and what was typed.

    ``typed`` lists every line that was read for the request in order, the refused ones and the
    ones that came too late included. A replay does not look at them; they show how the answer
    came about, and typed again at the same request they give the same answer (unless the time
    ran out).
    """

    index: int
    lot: int | None
    phase: str
    seat: int
    answer: Answer
    typed: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "index": self.index,
            "lot": self.lot,
            "phase": self.phase,
            "seat": self.seat,
            "answer": answer_to_json(self.answer),
        }
        if self.typed:
            data["typed"] = list(self.typed)
        return data


_ANSWER_FIELDS: Final = frozenset(
    {
        "abstain",
        "value",
        "say",
        "pledge",
        "guess",
        "priorities",
        "budget_plan",
        "persona",
        "thought",
        "sanitized_chars",
        "text_sanitized_chars",
    }
)


def answer_to_json(answer: Answer) -> dict[str, Any]:
    """An answer as plain JSON. Fields at their defaults are left out."""
    data: dict[str, Any] = {}
    if answer.abstain is not None:
        data["abstain"] = answer.abstain
    for name in ("value", "guess"):
        number = getattr(answer, name)
        if number is not None:
            data[name] = number
    for name in ("say", "persona", "thought"):
        text = getattr(answer, name)
        if text:
            data[name] = text
    if answer.pledge is not None:
        data["pledge"] = answer.pledge.model_dump(mode="json", exclude_none=True)
    for name in ("priorities", "budget_plan"):
        numbers = getattr(answer, name)
        if numbers is not None:
            data[name] = list(numbers)
    for name in ("sanitized_chars", "text_sanitized_chars"):
        count = getattr(answer, name)
        if count:
            data[name] = count
    return data


def answer_from_json(seat: int, data: Mapping[str, Any]) -> Answer:
    """The answer of ``seat`` that :func:`answer_to_json` wrote. Unknown fields are refused."""
    unknown = sorted(set(data) - _ANSWER_FIELDS)
    if unknown:
        raise HumanTapeError(f"an answer has unknown fields: {', '.join(unknown)}")
    fields: dict[str, Any] = dict(data)
    for name in ("priorities", "budget_plan"):
        if name in fields:
            fields[name] = tuple(fields[name])
    try:
        if "pledge" in fields:
            fields["pledge"] = Pledge.model_validate(fields["pledge"])
        return Answer(seat=seat, **fields)
    except (AnswerError, TypeError, ValueError) as exc:
        raise HumanTapeError(f"an answer on the tape cannot be used: {exc}") from None


@dataclass(frozen=True)
class Tape:
    """The inputs of a person during one night, in the order they were given."""

    night: str
    seat: int
    entries: tuple[TapeEntry, ...]


class TapeWriter:
    """Writes the tape of a night as it grows: one header line, then one line per answer.

    The file is created with the first answer, so a night that never gets as far leaves nothing
    behind. Each line is flushed at once, so a night that is abandoned halfway keeps what the
    person had done so far. Use it as a context manager.
    """

    def __init__(self, path: Path, night: str, seat: int) -> None:
        self.path = path
        self._header = {"format": TAPE_FORMAT, "night": night, "seat": seat}
        self._handle: IO[str] | None = None

    def _write(self, data: Mapping[str, Any]) -> None:
        if self._handle is None:
            self._handle = self.path.open("w", encoding="utf-8", newline="\n")
            self._handle.write(canonical_json(self._header) + "\n")
        self._handle.write(canonical_json(data) + "\n")
        self._handle.flush()

    def append(self, entry: TapeEntry) -> None:
        """Add one answer."""
        self._write(entry.to_json())

    def close(self) -> None:
        if self._handle is not None and not self._handle.closed:
            self._handle.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def load_tape(path: Path) -> Tape:
    """Read a tape that :class:`TapeWriter` wrote. A line that cannot be read is an error."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise HumanTapeError(f"cannot read the input record {path.name}: {exc}") from None
    where = path.name
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(lines, start=1):
        try:
            row = json.loads(line)
        except ValueError:
            raise HumanTapeError(f"{where}:{number}: not valid JSON") from None
        if not isinstance(row, dict):
            raise HumanTapeError(f"{where}:{number}: expected a JSON object")
        rows.append(row)
    if not rows or rows[0].get("format") != TAPE_FORMAT:
        raise HumanTapeError(f"{where}: not an input record of format {TAPE_FORMAT}")
    header = rows[0]
    night, seat = header.get("night"), header.get("seat")
    if not isinstance(night, str) or isinstance(seat, bool) or not isinstance(seat, int):
        raise HumanTapeError(f"{where}:1: the header needs a night id and a seat")
    entries: list[TapeEntry] = []
    for number, row in enumerate(rows[1:], start=2):
        try:
            index, lot, phase = row["index"], row["lot"], row["phase"]
            if row["seat"] != seat or isinstance(index, bool) or not isinstance(index, int):
                raise HumanTapeError(f"{where}:{number}: the line does not belong to seat {seat}")
            if not isinstance(phase, str) or not (lot is None or isinstance(lot, int)):
                raise HumanTapeError(f"{where}:{number}: bad lot or phase")
            answer = answer_from_json(seat, row["answer"])
            typed = tuple(str(item) for item in row.get("typed", ()))
        except KeyError as missing:
            raise HumanTapeError(f"{where}:{number}: missing field {missing}") from None
        except (TypeError, AttributeError):
            raise HumanTapeError(f"{where}:{number}: the line is malformed") from None
        entries.append(TapeEntry(index, lot, phase, seat, answer, typed))
    indexes = [entry.index for entry in entries]
    if indexes != sorted(set(indexes)):
        raise HumanTapeError(f"{where}: the steps are not in strictly increasing order")
    return Tape(night, seat, tuple(entries))


class ReplayedHuman:
    """The person of a recorded night: hands the recorded answers back, asks nobody anything.

    An answer is looked up by the position of the request in the night and must belong to the
    same lot, phase and seat. Anything else means that the tape is not the one of this night.
    """

    def __init__(self, tape: Tape) -> None:
        self.tape = tape
        self._by_index = {entry.index: entry for entry in tape.entries}
        self._served: set[int] = set()

    def answer(self, turn: HumanTurn) -> Answer:
        entry = self._by_index.get(turn.index)
        if entry is None:
            raise HumanTapeError(
                f"the input record has no answer for step {turn.index} "
                f"({turn.phase}, lot {turn.lot}, seat {turn.seat})"
            )
        if (entry.lot, entry.phase, entry.seat) != (turn.lot, turn.phase, turn.seat):
            raise HumanTapeError(
                f"the input record does not fit step {turn.index}: it holds "
                f"{entry.phase} of lot {entry.lot} for seat {entry.seat}, the night asks "
                f"{turn.phase} of lot {turn.lot} for seat {turn.seat}"
            )
        self._served.add(turn.index)
        return entry.answer

    def unused(self) -> int:
        """Recorded answers that the replay never asked for."""
        return len(self._by_index) - len(self._served)


# ------------------------------------------------------------------------------ the seat


class _Retry(Exception):
    """An entry is refused; the message says why. The question is asked again."""


class _TimedOut(Exception):
    """The time of the phase is over."""


class _Skipped(Exception):
    """The person chose to skip the opening plan."""


class _Round:
    """The questions of one phase: one clock for all of them, and the lines typed for them."""

    def __init__(self, seat: "HumanSeat", turn: HumanTurn) -> None:
        self.seat = seat
        self.turn = turn
        limit = seat.timeout_s
        self.deadline = None if limit is None else seat.clock() + limit
        self.typed: list[str] = []

    def remaining(self) -> float | None:
        if self.deadline is None:
            return None
        left = self.deadline - self.seat.clock()
        if left <= 0:
            raise _TimedOut
        return left

    def ask(self, label: str, parse: Callable[[str], T]) -> T:
        """Ask until ``parse`` takes the entry. A refusal (:class:`_Retry`) is shown and asked
        again; running out of time ends the phase."""
        while True:
            left = self.remaining()
            self.seat.presenter.prompt(label, left)
            try:
                line = self.seat.source.read(left)
            except InputTimeout:
                raise _TimedOut from None
            if len(self.typed) < _TYPED_KEPT:
                self.typed.append(line[:_TYPED_CHARS])
            if self.deadline is not None and self.seat.clock() >= self.deadline:
                raise _TimedOut  # a line that arrives too late does not count
            try:
                return parse(line)
            except _Retry as problem:
                self.seat.presenter.notice(str(problem), problem=True)


class HumanSeat:
    """A person at a seat: shows each request, reads the answer, records it. See the module text.

    ``source`` and ``presenter`` are the person's keyboard and screen; ``timeout_s`` is the time
    for each phase (None: no limit) and ``clock`` measures it; ``tape`` (optional) receives every
    answer. Nothing in this class reads the real clock or a terminal by itself.
    """

    def __init__(
        self,
        seat: int,
        source: InputSource,
        presenter: Presenter,
        *,
        timeout_s: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        tape: TapeSink | None = None,
    ) -> None:
        if timeout_s is not None and timeout_s <= 0:
            raise ValueError("the time for a phase must be positive")
        self.seat = seat
        self.source = source
        self.presenter = presenter
        self.timeout_s = timeout_s
        self.clock = clock
        self.tape = tape
        self._briefed = False

    # -------------------------------------------------------------------------- one turn

    def answer(self, turn: HumanTurn) -> Answer:
        """Show the request, read and check the answer, record it."""
        if turn.seat != self.seat:
            raise ValueError(f"seat {turn.seat} is not the seat of this person ({self.seat})")
        if not self._briefed:
            self.presenter.briefing(
                f"你坐在 {turn.seat} 号位，代号「{turn.codename}」", turn.system
            )
            self._briefed = True
        self.source.flush()
        self.presenter.turn(_title(turn), turn.observation, hint_for(turn, self.timeout_s))
        phase_round = _Round(self, turn)
        try:
            answer = self._dispatch(phase_round)
        except InputClosed:
            self.presenter.notice("\n输入已结束。", problem=True)  # the cursor is behind a prompt
            raise
        except _TimedOut:
            self.presenter.notice("时间到。", problem=True)
            answer = Answer.abstained(turn.seat, "timeout")
        except _Skipped:
            answer = Answer.abstained(turn.seat, "missing")
        self.presenter.notice(_confirmation(turn, answer))
        if self.tape is not None:
            self.tape.append(
                TapeEntry(
                    turn.index, turn.lot, turn.phase, turn.seat, answer, tuple(phase_round.typed)
                )
            )
        return answer

    def _dispatch(self, phase_round: _Round) -> Answer:
        phase = phase_round.turn.phase
        if phase == "plan":
            return self._plan(phase_round)
        if phase == "talk":
            return self._talk(phase_round)
        return self._bid(phase_round)

    # ------------------------------------------------------------------------- the phases

    def _bid(self, phase_round: _Round) -> Answer:
        turn = phase_round.turn
        keep = turn.own_exit if turn.phase == "english_revise" else None

        def parse(text: str) -> int:
            if not text.strip():
                if keep is not None:
                    return keep
                raise _Retry("请输入一个整数；0 表示不参加。")
            value = parse_whole(text)
            if value is None:
                raise _Retry("这不是整数。请只输入数字。")
            problem = bid_problem(turn, value)
            if problem is not None:
                raise _Retry(problem)
            return value

        value = phase_round.ask(f"{_NUMBER_NAMES[turn.phase]}> ", parse)
        return Answer(seat=turn.seat, value=value)

    def _talk(self, phase_round: _Round) -> Answer:
        turn = phase_round.turn
        cap = turn.ctx.limits.say
        said = phase_round.ask(f"说一句话（至多 {cap} 字，回车 = 不说）> ", lambda text: text)
        cleaned = clean_say(said, cap)
        if cleaned.changed:
            shown = cleaned.text or "（空）"
            self.presenter.notice(
                f"和模型的发言一样，你的话按频道规则清洗过了（括号、控制字符、换行和超长部分）。"
                f"实际发言：{shown}"
            )
        pledge = self._pledge(phase_round)
        guess = None
        if turn.guess_target is not None:
            guess = self._guess(phase_round, turn.guess_target)
        return Answer(
            seat=turn.seat,
            say=cleaned.text,
            pledge=pledge,
            guess=guess,
            sanitized_chars=cleaned.changed,
            text_sanitized_chars=cleaned.changed,
        )

    def _guess(self, phase_round: _Round, target: int) -> int | None:
        ceiling = phase_round.turn.ctx.act.max_amount

        def parse(text: str) -> int | None:
            if not text.strip():
                return None
            value = parse_whole(text)
            if value is None or value > ceiling:
                raise _Retry(f"请输入 0 到 {ceiling} 的整数，或直接回车跳过。")
            return value

        return phase_round.ask(f"猜 {target} 号还剩多少预算（回车 = 不猜）> ", parse)

    def _pledge(self, phase_round: _Round) -> Pledge | None:
        turn = phase_round.turn
        act = turn.ctx.act

        def parse_kind(text: str) -> str | None:
            key = text.strip()
            if not key:
                return None
            kind = _PLEDGE_KEYS.get(key)
            if kind is None:
                raise _Retry("请按 1、2 或 3；不立誓约就直接回车。")
            return kind

        kind = phase_round.ask(
            "誓约：1 不出价　2 出价不超过　3 让某人拿下（回车 = 不立）> ", parse_kind
        )
        if kind is None:
            return None
        first = max(1, act.current_lot or 1)

        def parse_lot(text: str) -> int:
            if not text.strip():
                return first
            value = parse_whole(text)
            if value is None or not first <= value <= act.n_lots:
                raise _Retry(f"请输入 {first} 到 {act.n_lots} 的整数。")
            return value

        lot = phase_round.ask(
            f"哪一件？（{first} 到 {act.n_lots}，回车 = 本件 {first}）> ", parse_lot
        )
        amount: int | None = None
        to: int | None = None
        if kind == "max_bid":

            def parse_amount(text: str) -> int:
                value = parse_whole(text)
                if value is None or value > act.max_amount:
                    raise _Retry(f"请输入 0 到 {act.max_amount} 的整数。")
                return value

            amount = phase_round.ask("最多出价多少？> ", parse_amount)
        elif kind == "let_win":

            def parse_seat(text: str) -> int:
                value = parse_whole(text)
                if value is None or not 1 <= value <= act.n_seats:
                    raise _Retry(f"请输入 1 到 {act.n_seats} 的座位号。")
                if value == act.self_seat:
                    raise _Retry("不能让给自己。")
                return value

            to = phase_round.ask(f"让几号拿下？（1 到 {act.n_seats}，不含自己）> ", parse_seat)
        return Pledge(kind=kind, lot=lot, amount=amount, to=to)  # type: ignore[arg-type]

    def _plan(self, phase_round: _Round) -> Answer:
        turn = phase_round.turn
        act, limits = turn.ctx.act, turn.ctx.limits
        n = act.n_lots

        def numbers(high: int) -> Callable[[str], list[int] | None]:
            def parse(text: str) -> list[int] | None:
                if not text.strip():
                    return None
                found = _whole_list(text)
                if found is None or len(found) != n or any(number > high for number in found):
                    raise _Retry(
                        f"请输入 {n} 个 0 到 {high} 的整数，用空格隔开；回车跳过整份计划。"
                    )
                return found

            return parse

        priorities = phase_round.ask(
            f"每件的重视程度（{n} 个 0 到 {PRIORITY_MAX}，空格隔开；回车 = 跳过计划）> ",
            numbers(PRIORITY_MAX),
        )
        if priorities is None:
            raise _Skipped
        budget = phase_round.ask(
            f"每件最多花多少（{n} 个整数，空格隔开；回车 = 跳过计划）> ",
            numbers(turn.ctx.act.max_amount),
        )
        if budget is None:
            raise _Skipped
        said = phase_round.ask(
            f"想在频道里表现成什么样？（至多 {limits.persona} 字，回车 = 不写）> ", lambda t: t
        )
        cleaned = clean_persona(said, limits.persona)
        return Answer(
            seat=turn.seat,
            priorities=tuple(priorities),
            budget_plan=tuple(budget),
            persona=cleaned.text,
            sanitized_chars=cleaned.changed,
            text_sanitized_chars=cleaned.changed,
        )


# ------------------------------------------------------------------------------ the texts


def _title(turn: HumanTurn) -> str:
    name = PHASE_NAMES[turn.phase]
    if turn.lot is None:
        return name
    return f"第 {turn.lot}/{turn.ctx.act.n_lots} 件 · {name}"


def hint_for(turn: HumanTurn, limit_s: float | None) -> str:
    """How to answer the request of ``turn``: a line of the interface, not part of the game."""
    ctx = turn.ctx
    left = ctx.budget_left
    phase = turn.phase
    if phase == "plan":
        n = ctx.act.n_lots
        text = (
            f"依次填三项：每件的重视程度（{n} 个 0 到 {PRIORITY_MAX} 的整数）、每件最多花多少"
            f"（{n} 个整数）、一行人设。前两项直接回车就是跳过整份计划。"
        )
    elif phase == "talk":
        if turn.guess_target is None:
            text = f"说一句话（至多 {ctx.limits.say} 字），然后可以立誓约。两项都可以直接回车跳过。"
        else:
            text = (
                f"说一句话（至多 {ctx.limits.say} 字），然后可以立誓约，再猜 {turn.guess_target} 号"
                "的预算。三项都可以直接回车跳过。"
            )
    elif phase == "english_init":
        text = f"输入退出价：整数，0 表示不参加，不超过剩余预算 {left}。"
    elif phase == "english_revise":
        text = (
            f"输入新的退出价：整数，不低于当前价 {ctx.current_price}，不超过剩余预算 {left}。"
            f"填 {ctx.current_price} 就是现在退出，直接回车保持 {turn.own_exit}。"
        )
    elif phase == "dutch":
        steps = [price for price in turn.ladder if left is None or price <= left]
        text = f"输入出手价：0 表示不参加，或是你买得起的某一档价格：{_listed(steps) or '无'}。"
    else:
        text = f"输入出价：整数，0 表示不参加，不超过剩余预算 {left}。"
    if limit_s is not None:
        text += f"本阶段限时 {limit_s:g} 秒，超时按弃权算。"
    return text


def _confirmation(turn: HumanTurn, answer: Answer) -> str:
    """What was recorded, in a sentence. The models take their turns after it, which can take a
    while, so the sentence says that the others are on."""
    if answer.abstain == "timeout":
        done = "超时，本阶段按弃权算。"
    elif answer.abstain is not None:
        done = "本阶段弃权。"
    elif turn.phase == "plan":
        done = "开场计划。"
    elif turn.phase == "talk":
        said = f"你说「{answer.say}」" if answer.say else "你没有发言"
        done = f"{said}{'，附誓约' if answer.pledge is not None else ''}。"
    else:
        done = f"{_NUMBER_NAMES[turn.phase]} {answer.value}。"
    return f"已记录：{done}其他座位行动中……"


# ------------------------------------------------------------------------------ the recap


def recap_lines(events: Iterable[AnyEvent], seat: int, settings: RenderSettings) -> list[str]:
    """The account of a finished night for the person at ``seat``, as lines of text.

    It is made from the events that seat may see (public ones and its own), so it holds nothing
    that the person could not have learnt at the table: the result of every lot, the missions
    (published after the last lot) and the final scores.
    """
    log = visible_log(events, seat)
    start = next((e for e in log if isinstance(e, NightStartEvent)), None)
    end = next((e for e in log if isinstance(e, NightEndEvent)), None)
    names = {entry.seat: entry.codename for entry in start.data.seats} if start else {}

    def who(number: int) -> str:
        return f"{number} 号「{names.get(number, '?')}」"

    lines = ["每件的结果"]
    lines += render_results(log, seat, settings)
    missions = [e.data for e in log if isinstance(e, MissionResultEvent)]
    if missions:
        lines += ["", "秘密任务"]
        for m in missions:
            if m.kind == "collector":
                detail = f"收藏家，要拍到 {m.need} 件〔{m.tag}〕，拍到 {len(m.won_lots)} 件"
            else:
                price = "流拍" if m.final_price is None else f"成交价 {m.final_price}"
                buyer = "" if m.buyer is None else f"，买家 {m.buyer} 号"
                detail = f"托儿，第 {m.target_lot} 件要到 {m.threshold_price}，{price}{buyer}"
            result = f"完成，奖励 {m.bonus}" if m.completed else "没有完成"
            lines.append(f"{who(m.seat)}：{detail}，{result}。")
    if end is not None:
        lines += ["", "最终成绩"]
        if end.data.status == "budget_aborted":
            lines.append("本夜达到费用上限，之后模型座位沉默着打完，按弃权处理。")
        for score in end.data.scores:
            rank = "机器人" if score.rank_tonight is None else f"本夜第 {score.rank_tonight} 名"
            lines.append(
                f"{who(score.seat)}：盈余 {score.surplus}，任务奖励 {score.mission_bonus}，"
                f"合计 {score.total}（{rank}）"
            )
    lines += ["", "这是一场有真人的夜晚，不计入模型积分榜。"]
    return lines


# ------------------------------------------------------------------------------ the table


@contextmanager
def interruptible() -> Iterator[None]:
    """Let Ctrl-C end the program at once, also while a night waits for a person.

    ``asyncio.run`` answers the first Ctrl-C by cancelling its main task, which takes effect only
    when the event loop gets control back. A night waits for a person inside the loop, in code that
    never yields, so that first Ctrl-C would go unnoticed until the person typed something. Inside
    this context SIGINT raises :class:`KeyboardInterrupt` wherever the program is, and asyncio
    leaves the signal alone (it takes over only the default handler). Outside the main thread it
    does nothing.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def raise_interrupt(signum: int, frame: FrameType | None) -> None:
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGINT, raise_interrupt)
    try:
        yield
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)


def seat_a_person(config: NightConfig, seat: int | None = None) -> tuple[NightConfig, int]:
    """The configuration with a person at ``seat``, and the number of that seat.

    The seat need not be written as ``human`` in the file: the person takes the place of a model
    seat (``llm`` or ``mock``) and keeps its code name, and the seat then plays as a seat of kind
    ``human``. A bot cannot be replaced, because the engine treats bot seats differently (no plan,
    no mission, not ranked). Without ``seat`` the configuration must name its human seat itself.
    """
    declared = [s.seat for s in config.seats if s.kind == "human"]
    if seat is None:
        if not declared:
            raise HumanSeatError("say which seat the person takes: give --human-seat")
        seat = declared[0]
    found = next((s for s in config.seats if s.seat == seat), None)
    if found is None:
        raise HumanSeatError(f"the table has no seat {seat} (seats 1 to {len(config.seats)})")
    if declared and declared != [seat]:
        raise HumanSeatError(
            f"the configuration already seats a person at {declared[0]}, not at {seat}"
        )
    if found.kind == "human":
        return config, seat
    if found.kind == "bot":
        raise HumanSeatError(
            f"seat {seat} is a rule bot; a person takes the place of a model seat (llm or mock)"
        )
    raw = config.model_dump(mode="json")
    raw["seats"] = [
        {"seat": seat, "codename": found.codename, "kind": "human"} if s["seat"] == seat else s
        for s in raw["seats"]
    ]
    try:
        return NightConfig.model_validate(raw), seat
    except ValueError as exc:
        lines = str(exc).splitlines()
        raise HumanSeatError(f"cannot seat a person at {seat}:\n" + "\n".join(lines[:6])) from None
