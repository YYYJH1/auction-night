"""The terminal side of a person: a keyboard with a time limit, and a screen that cannot be driven.

The keyboard is tested on real file descriptors (pipes, and a pseudo terminal where there is one),
so the claims about buffering, the time limit and flushing are about the code that runs at a
terminal. The screen is tested with hostile text: escape sequences, rich markup, control and
bidirectional characters.
"""

import asyncio
import io
import os
import re
import signal
import threading
import time
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from hypothesis import given
from hypothesis import strategies as st
from rich.console import Console

from auction_night.llm.human import InputClosed, InputTimeout, interruptible
from auction_night.llm.terminal import RichPresenter, StdinSource, open_input, terminal_safe

# ============================================================================ terminal_safe


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a\x1b[31mred\x1b[0mb", "aredb"),  # colours
        ("a\x1b[2J\x1b[Hb", "ab"),  # clear screen, cursor home
        ("x\x1b]0;a title\x07y", "xy"),  # window title
        ("x\x1b]8;;http://example.com\x1b\\link\x1b]8;;\x1b\\y", "xlinky"),  # hyperlink
        ("abc\rXYZ", "abcXYZ"),  # a carriage return would overwrite the line
        ("a\tb", "a b"),
        ("a\nb", "a\nb"),
        ("a\u2028b\u2029c", "a\nb\nc"),
        ("a\u200bb\u202ec\u2066d", "abcd"),  # zero-width and bidirectional marks
        ("a\x00b\x07c\x7fd\x85e", "abcde"),
        ("a\x9b31mred\x9b0mb", "aredb"),  # the one-byte form of the escape sequence
        ("第 1 件 🍬 [x] <y>", "第 1 件 🍬 [x] <y>"),  # nothing else is touched
    ],
)
def test_terminal_safe_removes_what_could_drive_a_terminal(raw: str, expected: str) -> None:
    assert terminal_safe(raw) == expected


@given(st.text())
def test_terminal_safe_leaves_only_printable_text_and_line_feeds(raw: str) -> None:
    safe = terminal_safe(raw)
    assert all(
        char == "\n" or unicodedata.category(char) not in ("Cc", "Cf", "Cs") for char in safe
    )
    assert terminal_safe(safe) == safe


# =================================================================================== the screen

SGR = re.compile(r"\x1b\[[0-9;]*m")
HOSTILE = (
    "[red]红色[/red] [bold]粗[/bold] [link=http://example.com]点我[/link] :smile: "
    "\x1b[31m染色\x1b[0m \x1b]0;标题\x07 \u202e反转 \r盖住"
)


def console(*, color: bool = False, width: int = 60) -> tuple[Console, io.StringIO]:
    out = io.StringIO()
    return Console(
        file=out,
        force_terminal=color,
        color_system="standard" if color else None,
        width=width,
        highlight=False,
    ), out


def test_the_request_is_drawn_between_its_title_and_its_hint() -> None:
    screen, out = console()
    RichPresenter(screen).turn("第 3/12 件 · 密封出价", "第一行\n第二行", "输入出价。")
    lines = out.getvalue().splitlines()
    assert "第 3/12 件 · 密封出价" in lines[0] and set(lines[0]) - {"─", " "} <= set(
        "第3/12件·密封出价"
    )
    assert lines[1:] == ["第一行", "第二行", "输入出价。"]


def test_a_long_line_is_not_wrapped_by_the_presenter() -> None:
    screen, out = console(width=20)
    long = "很长的一行" * 20
    RichPresenter(screen).turn("t", long, "h")
    assert long in out.getvalue().splitlines()


@pytest.mark.parametrize("method", ["briefing", "turn", "notice", "summary"])
def test_text_is_never_taken_for_markup_or_for_commands(method: str) -> None:
    screen, out = console()
    presenter = RichPresenter(screen)
    if method == "briefing":
        presenter.briefing(HOSTILE, HOSTILE)
    elif method == "turn":
        presenter.turn(HOSTILE, HOSTILE, HOSTILE)
    elif method == "notice":
        presenter.notice(HOSTILE, problem=True)
    else:
        presenter.summary(HOSTILE, [HOSTILE])
    shown = out.getvalue()
    for literal in (
        "[red]红色[/red]",
        "[bold]粗[/bold]",
        "[link=http://example.com]点我[/link]",
        ":smile:",
    ):
        assert literal in shown
    assert "染色" in shown and "盖住" in shown  # the words stay, only the commands go
    for control in ("\x1b", "\x07", "\r", "\u202e"):
        assert control not in shown


def test_even_with_colours_on_only_the_presenter_uses_escape_sequences() -> None:
    screen, out = console(color=True)
    presenter = RichPresenter(screen)
    presenter.turn(HOSTILE, HOSTILE, HOSTILE)
    presenter.notice(HOSTILE, problem=True)
    shown = out.getvalue()
    assert SGR.search(shown)  # the frame is coloured...
    bare = SGR.sub("", shown)
    assert "\x1b" not in bare and "\x07" not in bare  # ...and nothing else is an escape
    assert "[red]红色[/red]" in bare and "[link=http://example.com]" in bare


def test_the_prompt_counts_down_without_ending_the_line() -> None:
    screen, out = console()
    presenter = RichPresenter(screen)
    presenter.prompt("出价> ", 73.9)
    presenter.prompt("出价> ", None)
    presenter.prompt("出价> ", -3)
    assert out.getvalue() == "（还剩 73 秒）出价> 出价> （还剩 0 秒）出价> "


def test_a_summary_lists_its_lines_under_a_rule() -> None:
    screen, out = console()
    RichPresenter(screen).summary("这一夜", ["甲", "", "乙"])
    assert out.getvalue().splitlines()[1:] == ["甲", "", "乙"]
    assert "这一夜" in out.getvalue().splitlines()[0]


def test_the_default_screen_is_standard_output(capsys: pytest.CaptureFixture[str]) -> None:
    RichPresenter().notice("[x]你好")
    assert capsys.readouterr().out == "[x]你好\n"


# ================================================================================ the keyboard


@contextmanager
def pipe_source(data: bytes = b"", *, close: bool = True) -> Iterator[tuple[StdinSource, int]]:
    """A source on a pipe that already holds ``data``. With ``close`` the writing end is closed,
    so the input ends after the data; otherwise the caller gets the writing end to use."""
    read_end, write_end = os.pipe()
    os.write(write_end, data)
    if close:
        os.close(write_end)
    stream = os.fdopen(read_end, "r", encoding="utf-8")
    try:
        yield StdinSource(stream), write_end
    finally:
        stream.close()
        if not close:
            os.close(write_end)


def test_lines_come_out_in_order_whatever_the_line_ending() -> None:
    with pipe_source("甲\n乙\r\n丙".encode()) as (source, _):
        assert [source.read(1) for _ in range(3)] == ["甲", "乙", "丙"]
        with pytest.raises(InputClosed):
            source.read(1)


def test_lines_that_arrive_together_are_not_lost() -> None:
    with pipe_source(b"a\nb\nc\n") as (source, _):
        assert source.read(1) == "a"
        assert source.read(0) == "b"  # served from the buffer, no waiting at all
        assert source.read(0) == "c"
        with pytest.raises(InputClosed):
            source.read(0)


def test_bytes_that_are_not_text_are_replaced_not_fatal() -> None:
    with pipe_source(b"ok\xff\xfe\n") as (source, _):
        assert source.read(1) == "ok��"


def test_an_empty_line_is_a_line() -> None:
    with pipe_source(b"\n\nx\n") as (source, _):
        assert [source.read(1) for _ in range(3)] == ["", "", "x"]


def test_the_time_limit_ends_the_wait_and_the_next_line_still_arrives() -> None:
    with pipe_source(close=False) as (source, writer):
        started = time.monotonic()
        with pytest.raises(InputTimeout):
            source.read(0.1)
        assert 0.05 <= time.monotonic() - started < 20
        os.write(writer, "好\n".encode())
        assert source.read(1) == "好"


def test_a_line_typed_in_pieces_is_whole_when_it_is_returned() -> None:
    with pipe_source(b"12", close=False) as (source, writer):
        with pytest.raises(InputTimeout):
            source.read(0.05)  # "12" is not a line yet
        os.write(writer, b"3\n")
        assert source.read(1) == "123"


def test_no_limit_waits_until_the_line_comes() -> None:
    import threading

    with pipe_source(close=False) as (source, writer):
        timer = threading.Timer(0.1, lambda: os.write(writer, b"late\n"))
        timer.start()
        assert source.read(None) == "late"
        timer.join()


def test_flushing_a_pipe_keeps_the_lines_that_were_meant_to_come() -> None:
    with pipe_source(b"a\nb\n") as (source, _):
        source.flush()
        assert source.read(1) == "a"


def test_flushing_a_terminal_drops_what_was_typed_ahead() -> None:
    pty = pytest.importorskip("pty")
    try:
        master, slave = pty.openpty()
    except OSError:
        pytest.skip("no pseudo terminals on this machine")
    stream = os.fdopen(slave, "r", encoding="utf-8")
    try:
        source = StdinSource(stream)
        os.write(master, "早打的\n半行".encode())  # a line, and the start of another
        time.sleep(0.1)
        source.flush()
        os.write(master, "新打的\n".encode())
        assert source.read(2) == "新打的"
    finally:
        stream.close()
        os.close(master)


def test_a_stream_without_a_descriptor_is_read_line_by_line() -> None:
    source = StdinSource(io.StringIO("x\ny\n"))
    assert [source.read(5), source.read(None)] == ["x", "y"]
    with pytest.raises(InputClosed):
        source.read(5)
    source.flush()  # nothing to flush, and no failure


def test_the_default_keyboard_is_standard_input() -> None:
    assert isinstance(open_input(), StdinSource)


# ================================================================================== Ctrl-C


@pytest.mark.skipif(not hasattr(signal, "pthread_kill"), reason="needs POSIX signals")
def test_ctrl_c_ends_a_night_that_waits_for_a_person_at_once() -> None:
    """The night waits inside the event loop, in code that never yields. asyncio would take the
    first Ctrl-C for a request to cancel its main task, and nothing would happen until the person
    typed a line."""
    previous = signal.getsignal(signal.SIGINT)
    read_end, write_end = os.pipe()  # nothing is ever written: the person is thinking
    stream = os.fdopen(read_end, "r", encoding="utf-8")
    source = StdinSource(stream)

    async def wait_for_the_person() -> None:
        source.read(120)

    main = threading.main_thread().ident
    assert main is not None
    timer = threading.Timer(0.2, lambda: signal.pthread_kill(main, signal.SIGINT))
    started = time.monotonic()
    try:
        timer.start()
        with interruptible(), pytest.raises(KeyboardInterrupt):
            asyncio.run(wait_for_the_person())
        assert time.monotonic() - started < 60  # not the 120 seconds the person had
    finally:
        timer.cancel()
        stream.close()
        os.close(write_end)
    assert signal.getsignal(signal.SIGINT) is previous  # and the handler is put back


def test_interruptible_does_nothing_outside_the_main_thread() -> None:
    seen: list[object] = []

    def work() -> None:
        before = signal.getsignal(signal.SIGINT)
        with interruptible():
            seen.append(signal.getsignal(signal.SIGINT) is before)

    thread = threading.Thread(target=work)
    thread.start()
    thread.join()
    assert seen == [True]
