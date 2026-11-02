"""The terminal side of a person at a seat: a keyboard with a time limit and a screen made by rich.

:class:`StdinSource` is the :class:`~auction_night.llm.human.InputSource` of a real terminal (or a
pipe). It reads the file descriptor itself, with ``select`` for the time limit, so a line that is
already buffered is never missed and a phase that runs out of time does not leave a thread behind.
Where that is not possible (no descriptor, or not a POSIX system) it falls back to a plain
blocking read, which cannot be interrupted: the seat still counts a line that comes too late as a
timeout, it only cannot stop waiting for it.

:class:`RichPresenter` is the :class:`~auction_night.llm.human.Presenter` that draws with rich.
Every text it is given is cleaned of control characters (:func:`terminal_safe`) and printed as a
:class:`rich.text.Text`, never as markup, so a line of the night that looks like ``[red]`` is
shown as those characters and an escape sequence in it is dropped. A person sees exactly the text
of the request; only the frame around it (rules, colours, hints) is the interface's own.
"""

import os
import select
import sys
import time
import unicodedata
from typing import IO, Final

try:
    import termios
except ImportError:  # not a POSIX system
    termios = None  # type: ignore[assignment]

from rich.console import Console
from rich.text import Text

from auction_night.llm.human import InputClosed, InputTimeout
from auction_night.llm.sanitize import strip_ansi

__all__ = ["RichPresenter", "StdinSource", "open_input", "terminal_safe"]

_CHUNK: Final = 65536
_LINE_BREAKS: Final = frozenset({"\u2028", "\u2029"})  # Zl and Zp: shown as a line break


def terminal_safe(text: str) -> str:
    """``text`` without anything that could drive a terminal.

    Escape sequences (colours, cursor moves, titles, links) go first. Then every control
    character goes except the line feed, which is kept; a tab becomes a space, and the line and
    paragraph separators become line feeds. Format characters (zero-width and bidirectional
    marks) and surrogates go too. A carriage return is dropped as well, because it would let a
    line overwrite what is already on the screen.
    """
    kept: list[str] = []
    for char in strip_ansi(text):
        if char == "\n" or char in _LINE_BREAKS:
            kept.append("\n")
        elif char == "\t":
            kept.append(" ")
        elif unicodedata.category(char) not in ("Cc", "Cf", "Cs"):
            kept.append(char)
    return "".join(kept)


# ---------------------------------------------------------------------------- the keyboard


class StdinSource:
    """Lines from a stream's file descriptor, with a time limit. See the module text.

    ``stream`` defaults to ``sys.stdin``. A stream without a usable descriptor (for example the
    stand-in that a test harness puts there) is read with ``readline`` and no time limit.
    """

    def __init__(self, stream: IO[str] | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdin
        self._fd = self._descriptor(self._stream)
        self._buffer = b""
        self._ended = False

    @staticmethod
    def _descriptor(stream: IO[str]) -> int | None:
        if os.name != "posix":
            return None
        try:
            return stream.fileno()
        except (AttributeError, ValueError, OSError):
            return None

    def _take(self) -> str | None:
        """A complete line from the buffer (or the last, unfinished one at the end of input)."""
        cut = self._buffer.find(b"\n")
        if cut < 0:
            if not (self._ended and self._buffer):
                return None
            raw, self._buffer = self._buffer, b""
        else:
            raw, self._buffer = self._buffer[:cut], self._buffer[cut + 1 :]
        return raw.decode("utf-8", errors="replace").rstrip("\r")

    def read(self, timeout_s: float | None) -> str:
        if self._fd is None:
            line = self._stream.readline()
            if not line:
                raise InputClosed("the input has ended")
            return line.rstrip("\r\n")
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        while True:
            line = self._take()
            if line is not None:
                return line
            if self._ended:
                raise InputClosed("the input has ended")
            wait = None if deadline is None else max(0.0, deadline - time.monotonic())
            ready, _, _ = select.select([self._fd], [], [], wait)  # a wait of 0 still looks once
            if not ready:
                raise InputTimeout("no input in time")
            chunk = os.read(self._fd, _CHUNK)
            if chunk:
                self._buffer += chunk
            else:
                self._ended = True

    def flush(self) -> None:
        """Drop what was typed ahead of the next question, a half-typed line included. Only a
        terminal is flushed: lines on a pipe or in a file are the answers that were meant to
        come."""
        if self._fd is None or not os.isatty(self._fd):
            return
        self._buffer = b""
        if termios is not None:
            try:
                termios.tcflush(self._fd, termios.TCIFLUSH)
                return
            except (OSError, termios.error):
                pass
        while select.select([self._fd], [], [], 0)[0]:
            if not os.read(self._fd, _CHUNK):
                break


def open_input() -> StdinSource:
    """The keyboard of the process: standard input."""
    return StdinSource()


# ------------------------------------------------------------------------------ the screen


class RichPresenter:
    """Draws the night for a person with rich. See the module text.

    ``console`` is the console to draw on; by default a plain one on standard output.
    """

    def __init__(self, console: Console | None = None) -> None:
        self.console = console if console is not None else Console(highlight=False)

    def _print(self, text: str, style: str = "", *, end: str = "\n") -> None:
        self.console.print(Text(terminal_safe(text), style=style), end=end, soft_wrap=True)

    def _rule(self, title: str, style: str) -> None:
        self.console.rule(Text(terminal_safe(title), style=style))

    def briefing(self, title: str, text: str) -> None:
        self._rule(title, "bold")
        self._print(text, "dim")

    def turn(self, title: str, observation: str, hint: str) -> None:
        self._rule(title, "bold cyan")
        self._print(observation)
        self._print(hint, "yellow")

    def prompt(self, label: str, remaining_s: float | None) -> None:
        clock = "" if remaining_s is None else f"（还剩 {max(0, int(remaining_s))} 秒）"
        self._print(clock + label, "bold", end="")
        self.console.file.flush()

    def notice(self, text: str, *, problem: bool = False) -> None:
        self._print(text, "red" if problem else "green")

    def summary(self, title: str, lines: list[str]) -> None:
        self._rule(title, "bold")
        for line in lines:
            self._print(line)
