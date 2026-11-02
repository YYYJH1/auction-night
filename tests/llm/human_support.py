"""Helpers for the tests of a person at a seat: a scripted keyboard, a recording screen, a clock
that moves when the test says so, and a typist that plays a whole night.

Nothing here needs a terminal. The keyboard is a script, the screen keeps what it is shown, and
time is a number that the test advances.
"""

import asyncio
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from auction_night.contracts import AnyEvent, NightConfig
from auction_night.contracts.act import ActContext
from auction_night.engine.night import Answer
from auction_night.llm.cassette import Cassette
from auction_night.llm.driver import NightResult
from auction_night.llm.human import (
    HumanSeat,
    HumanTurn,
    InputClosed,
    InputTimeout,
    Tape,
    TapeEntry,
    seat_a_person,
)
from auction_night.llm.nightrun import NightRig, build_rig
from auction_night.llm.parse import ParseContext
from auction_night.llm.sanitize import TextLimits

TIMEOUT = object()  # a script item: nothing is typed before the time runs out


class FakeClock:
    """Time that moves only when the test moves it."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass(frozen=True)
class Slow:
    """A line that takes ``delay`` seconds to be typed."""

    text: str
    delay: float


class ScriptedInput:
    """The keyboard of a test: returns the lines of a script, one per question.

    ``Slow`` items move the clock before they arrive. A source that enforces the limit (like the
    terminal) gives up with :class:`InputTimeout` when the delay is longer than the time it was
    given; with ``enforce=False`` it hands over the late line anyway, which shows that the seat
    checks the clock itself. ``limits`` records the time left that each question was given.
    """

    def __init__(
        self,
        lines: Iterable[Any] = (),
        *,
        clock: FakeClock | None = None,
        enforce: bool = True,
    ) -> None:
        self._lines: deque[Any] = deque(lines)
        self.clock = clock
        self.enforce = enforce
        self.limits: list[float | None] = []
        self.flushes = 0
        self.asked = 0

    def push(self, *lines: Any) -> None:
        self._lines.extend(lines)

    @property
    def left(self) -> int:
        return len(self._lines)

    def read(self, timeout_s: float | None) -> str:
        self.limits.append(timeout_s)
        self.asked += 1
        if not self._lines:
            raise InputClosed("the script has run out")
        item = self._lines.popleft()
        if item is TIMEOUT:
            raise InputTimeout("nothing typed")
        if isinstance(item, Slow):
            if self.clock is not None:
                self.clock.advance(item.delay)
            if self.enforce and timeout_s is not None and item.delay > timeout_s:
                raise InputTimeout("too slow")
            return item.text
        return str(item)

    def flush(self) -> None:
        self.flushes += 1


@dataclass
class Shown:
    """One thing the person was shown: what kind of thing, the text, and its frame."""

    kind: str  # briefing, turn, prompt, notice, problem or summary
    text: str
    frame: str = ""  # the title, the hint or the label


@dataclass
class RecordingPresenter:
    """The screen of a test: keeps everything that is shown, in order."""

    shown: list[Shown] = field(default_factory=list)

    def briefing(self, title: str, text: str) -> None:
        self.shown.append(Shown("briefing", text, title))

    def turn(self, title: str, observation: str, hint: str) -> None:
        self.shown.append(Shown("turn", observation, f"{title}\n{hint}"))

    def prompt(self, label: str, remaining_s: float | None) -> None:
        self.shown.append(Shown("prompt", label, "" if remaining_s is None else f"{remaining_s:g}"))

    def notice(self, text: str, *, problem: bool = False) -> None:
        self.shown.append(Shown("problem" if problem else "notice", text))

    def summary(self, title: str, lines: list[str]) -> None:
        self.shown.append(Shown("summary", "\n".join(lines), title))

    # ---------------------------------------------------------------------- readers

    def of(self, kind: str) -> list[Shown]:
        return [item for item in self.shown if item.kind == kind]

    def texts(self, kind: str) -> list[str]:
        return [item.text for item in self.of(kind)]

    def since(self, mark: int) -> str:
        """All text that reached the person after the first ``mark`` things, frames included."""
        return "\n".join(f"{item.frame}\n{item.text}" for item in self.shown[mark:])


class TapeList:
    """A tape sink that keeps the entries in a list."""

    def __init__(self) -> None:
        self.entries: list[TapeEntry] = []

    def append(self, entry: TapeEntry) -> None:
        self.entries.append(entry)

    def tape(self, night: str, seat: int) -> Tape:
        return Tape(night, seat, tuple(self.entries))


# ---------------------------------------------------------------------------- single turns


def make_turn(
    phase: str = "sealed",
    *,
    seat: int = 3,
    lot: int | None = 4,
    index: int = 17,
    n_lots: int = 12,
    n_seats: int = 8,
    budget_left: int | None = None,
    current_price: int | None = None,
    reserve: int = 0,
    own_exit: int | None = None,
    guess_target: int | None = None,
    ladder: tuple[int, ...] = (),
    limits: TextLimits | None = None,
    observation: str = "【测试观察】",
    system: str = "【测试系统】",
) -> HumanTurn:
    """A request for the person at ``seat``, built by hand. The remaining budget defaults to 300
    in the bid phases and to nothing in the plan and talk phases, as in the real context."""
    bidding = phase not in ("plan", "talk")
    if budget_left is None and bidding:
        budget_left = 300
    ctx = ParseContext(
        phase,  # type: ignore[arg-type]
        act=ActContext(
            n_lots=n_lots,
            n_seats=n_seats,
            self_seat=seat,
            current_lot=None if phase == "plan" else lot,
        ),
        budget_left=budget_left,
        current_price=current_price,
        limits=limits or TextLimits(),
    )
    return HumanTurn(
        seat=seat,
        codename="小满",
        index=index,
        lot=None if phase == "plan" else lot,
        ctx=ctx,
        observation=observation,
        system=system,
        reserve=reserve,
        own_exit=own_exit,
        guess_target=guess_target,
        ladder=ladder,
    )


def make_seat(
    lines: Iterable[Any] = (),
    *,
    timeout_s: float | None = None,
    clock: FakeClock | None = None,
    enforce: bool = True,
    seat: int = 3,
) -> tuple[HumanSeat, ScriptedInput, RecordingPresenter, TapeList]:
    """A person at seat 3 with a script, a screen that records, and a tape in a list."""
    clock = clock or FakeClock()
    source = ScriptedInput(lines, clock=clock, enforce=enforce)
    screen = RecordingPresenter()
    tape = TapeList()
    person = HumanSeat(seat, source, screen, timeout_s=timeout_s, clock=clock, tape=tape)
    return person, source, screen, tape


# ---------------------------------------------------------------------------- whole nights


def typed_lines(turn: HumanTurn) -> list[str]:
    """What a sensible person would type for a request: complete answers, never skipped.

    Only the fields of the turn are used (the remaining budget, the price, the levels), so the
    same policy works for any night. The numbers move with the position of the request, so the
    night has some variety. Texts are clean: nothing in them is cut by the cleaning.
    """
    phase = turn.phase
    n = turn.ctx.act.n_lots
    left = turn.ctx.budget_left or 0
    if phase == "plan":
        return [" ".join(["1"] * n), " ".join(["30"] * n), "稳扎稳打"]
    if phase == "talk":
        lines = [f"第{turn.lot}件我先看看", ""]
        if turn.guess_target is not None:
            lines.append(str(100 + turn.index))
        return lines
    if phase == "english_init":
        return [str(min(left, max(turn.reserve, 1) + 20 + turn.index % 30))]
    if phase == "english_revise":
        price = turn.ctx.current_price or 0
        return [""] if turn.index % 2 else [str(min(left, price + 5))]
    if phase == "dutch":
        steps = [price for price in turn.ladder if price <= left]
        return [str(steps[min(len(steps) - 1, 3 + turn.index % 3)])] if steps else ["0"]
    return [str(min(left, 10 + turn.index * 7 % 60))]


class Typist:
    """A person for the driver: before each request, puts the lines of ``policy`` on the script
    and lets the real :class:`HumanSeat` read them. ``before`` and ``after`` are called around
    each turn (a test uses them to look at the state of the night)."""

    def __init__(
        self,
        seat: HumanSeat,
        source: ScriptedInput,
        policy: Callable[[HumanTurn], list[Any]] = typed_lines,
        *,
        before: Callable[[HumanTurn], None] | None = None,
        after: Callable[[HumanTurn, Answer], None] | None = None,
    ) -> None:
        self.seat = seat
        self.source = source
        self.policy = policy
        self.before = before
        self.after = after
        self.turns: list[HumanTurn] = []

    def answer(self, turn: HumanTurn) -> Answer:
        self.turns.append(turn)
        if self.before is not None:
            self.before(turn)
        self.source.push(*self.policy(turn))
        answer = self.seat.answer(turn)
        if self.after is not None:
            self.after(turn, answer)
        return answer


@dataclass
class Sitting:
    """A night played with a person at one seat."""

    result: NightResult
    cassette: Cassette
    rig: NightRig
    typist: Typist
    screen: RecordingPresenter
    tape: TapeList
    config: NightConfig

    @property
    def events(self) -> tuple[AnyEvent, ...]:
        return self.result.events

    def recorded(self) -> Tape:
        return self.tape.tape(self.config.night.id, self.typist.seat.seat)


def sit(
    config: NightConfig,
    *,
    seat: int = 3,
    policy: Callable[[HumanTurn], list[Any]] = typed_lines,
    timeout_s: float | None = None,
    clock: FakeClock | None = None,
    before: Callable[[HumanTurn], None] | None = None,
    after: Callable[[HumanTurn, Answer], None] | None = None,
    prepare: Callable[[NightRig, Typist], None] | None = None,
    **rig_options: Any,
) -> Sitting:
    """Play the night of ``config`` with a person at ``seat``, typing by ``policy``.

    ``prepare`` is called with the rig and the typist before the night starts (a test uses it to
    give the typist a look at the driver). Other options go to :func:`build_rig`.
    """
    seated, number = seat_a_person(config, seat)
    person, source, screen, tape = make_seat(timeout_s=timeout_s, clock=clock, seat=number)
    typist = Typist(person, source, policy, before=before, after=after)
    cassette = Cassette.record()
    rig = build_rig(seated, cassette, humans={number: typist}, **rig_options)
    if prepare is not None:
        prepare(rig, typist)
    runner = rig.runner()
    try:
        result = asyncio.run(rig.play(runner))
    finally:
        runner.close()
    return Sitting(result, cassette, rig, typist, screen, tape, seated)
