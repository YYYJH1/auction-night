"""tools/check_leaderboard.py rebuilds the committed boards from the logs and catches any change."""

import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from season_support import (
    copy_season,
    rewrite_status,
    specs,
    write_night,
    write_rotation_season,
)
from typer.testing import CliRunner

import check_leaderboard
from auction_night import season
from auction_night.cli import app
from auction_night.contracts import dumps_events, loads_events
from auction_night.contracts.events import NightEndEvent

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tools" / "check_leaderboard.py"
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
runner = CliRunner()


def publish(root: Path, name: str = "s1") -> Path:
    """A small rotation season of written nights and the board that `season --out` would write."""
    directory = root / name
    write_rotation_season(
        directory,
        lambda model, deck, rotation: (
            {"mock-honest": 12, "mock-collude": 3, "mock-backstab": -7}[model]
            + 5 * rotation
            - (20 if deck == "d2" else 0)
        ),
        decks=("d1", "d2", "d3"),
        bot=lambda deck, rotation: 4,
    )
    board = season.leaderboard_for([directory])
    (directory / "leaderboard.json").write_text(
        season.leaderboard_json(board), encoding="utf-8", newline="\n"
    )
    return directory


def run_check(root: Path, *extra: str) -> int:
    return check_leaderboard.main(["--seasons", str(root), *extra])


def change_surplus(night: Path, seat: int, delta: int) -> None:
    """Add ``delta`` to the surplus and the total of a seat in a log that stays valid."""
    path = night / "events.jsonl"
    events = loads_events(path.read_text(encoding="utf-8"))
    for index, event in enumerate(events):
        if isinstance(event, NightEndEvent):
            scores = [
                s.model_copy(update={"surplus": s.surplus + delta, "total": s.total + delta})
                if s.seat == seat
                else s
                for s in event.data.scores
            ]
            data = event.data.model_copy(update={"scores": scores})
            events[index] = event.model_copy(update={"data": data})
    path.write_text(dumps_events(events), encoding="utf-8")


# ------------------------------------------------------------------------------ skipping


def test_without_a_season_directory_it_says_so_in_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_check(tmp_path / "none") == 0
    assert capsys.readouterr().out == (
        f"check_leaderboard: no season directory under {tmp_path / 'none'}, nothing to check\n"
    )
    (tmp_path / "seasons").mkdir()
    (tmp_path / "seasons" / "README.md").write_text("notes\n")
    (tmp_path / "seasons" / "empty").mkdir()
    assert run_check(tmp_path / "seasons") == 0
    assert capsys.readouterr().out.count("\n") == 1


def test_the_tool_and_the_module_use_the_same_file_names() -> None:
    """The tool looks for seasons before it imports the package, so it keeps its own copies."""
    assert check_leaderboard.BOARD_FILE == season.LEADERBOARD_FILE
    assert check_leaderboard.EVENTS_FILE == season.EVENTS_FILE


def test_the_repository_itself_passes() -> None:
    """There is no published season yet, so this skips; once there is one, it must match."""
    assert check_leaderboard.main([]) == 0


# ---------------------------------------------------------------------------- matching


def test_a_board_that_comes_from_its_logs_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    publish(tmp_path / "seasons")
    assert run_check(tmp_path / "seasons") == 0
    out = capsys.readouterr()
    assert "s1: leaderboard.json matches the 9 nights counted in its logs" in out.out
    assert out.err == ""


def test_each_season_is_checked(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    publish(tmp_path / "seasons", "s1")
    publish(tmp_path / "seasons", "s2")
    change_surplus(tmp_path / "seasons" / "s2" / "syn-d1-r0", 1, 1)
    assert run_check(tmp_path / "seasons") == 1
    out = capsys.readouterr()
    assert "s1: leaderboard.json matches" in out.out
    assert "s2: leaderboard.json differs from the logs" in out.err


# ----------------------------------------------------------------------------- tampering


def drop_night(root: Path) -> None:
    night = root / "syn-d3-r2"
    for name in ("events.jsonl", "config.yaml"):
        (night / name).unlink()
    night.rmdir()


def add_night(root: Path) -> None:
    seats = specs(0, lambda model: 1)
    spec = {"id": "syn", "format": "rotation", "deck": "d3", "rotation": 5}
    write_night(root / "syn-d3-r5", "syn-d3-r5", seats, season=spec)


def edit_board(root: Path) -> None:
    path = root / "leaderboard.json"
    text = path.read_text(encoding="utf-8")
    assert '"rank": 1,' in text
    path.write_text(text.replace('"rank": 1,', '"rank": 2,', 1), encoding="utf-8")


def board_with(seed: str = season.DEFAULT_SEED, resamples: int = season.DEFAULT_RESAMPLES):
    def rewrite(root: Path) -> None:
        board = season.leaderboard_for([root], seed=seed, resamples=resamples)
        (root / "leaderboard.json").write_text(season.leaderboard_json(board), encoding="utf-8")

    return rewrite


def crlf(root: Path) -> None:
    path = root / "leaderboard.json"
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))


TAMPERINGS: dict[str, Callable[[Path], None]] = {
    "a score in a log": lambda root: change_surplus(root / "syn-d1-r1", 2, 1),
    "the status of a night": lambda root: rewrite_status(root / "syn-d2-r0", "budget_aborted"),
    "a night removed": drop_night,
    "a night added": add_night,
    "the board edited by hand": edit_board,
    "a board made with another seed": board_with(seed="lucky"),
    "a board made with fewer resamples": board_with(resamples=500),
    "the line endings": crlf,
}


@pytest.mark.parametrize("what", TAMPERINGS)
def test_a_changed_log_or_board_is_caught(
    what: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = publish(tmp_path / "seasons")
    assert run_check(tmp_path / "seasons") == 0
    capsys.readouterr()
    TAMPERINGS[what](root)
    assert run_check(tmp_path / "seasons") == 1
    out = capsys.readouterr()
    assert out.out == "" and "leaderboard.json differs from the logs: " in out.err


def test_the_difference_is_located(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = publish(tmp_path / "seasons")
    edit_board(root)
    run_check(tmp_path / "seasons")
    err = capsys.readouterr().err
    assert re.search(
        r"line \d+ is '\"rank\": 2,' in the file and '\"rank\": 1,' from the logs", err
    )


def test_logs_without_a_board_fail(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = publish(tmp_path / "seasons")
    (root / "leaderboard.json").unlink()
    assert run_check(tmp_path / "seasons") == 1
    assert "the logs have no leaderboard.json" in capsys.readouterr().err


def test_a_board_without_logs_cannot_be_checked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "seasons" / "s1").mkdir(parents=True)
    (tmp_path / "seasons" / "s1" / "leaderboard.json").write_text("{}\n")
    assert run_check(tmp_path / "seasons") == 2
    assert "cannot rebuild the board from the logs: no events.jsonl in or under" in (
        capsys.readouterr().err
    )


def test_a_log_that_is_broken_is_an_input_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = publish(tmp_path / "seasons")
    (root / "syn-d1-r0" / "events.jsonl").write_text("not json\n", encoding="utf-8")
    assert run_check(tmp_path / "seasons") == 2
    assert "line 1 is not a valid event" in capsys.readouterr().err


def test_the_settings_can_be_given_for_a_board_made_with_them(tmp_path: Path) -> None:
    root = publish(tmp_path / "seasons")
    board_with(seed="other", resamples=300)(root)
    assert run_check(tmp_path / "seasons") == 1
    assert run_check(tmp_path / "seasons", "--seed", "other", "--resamples", "300") == 0


# -------------------------------------------------------------------- the real thing


def test_the_script_runs_on_the_command_line(tmp_path: Path) -> None:
    root = publish(tmp_path / "seasons")

    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--seasons", str(tmp_path / "seasons")],
            capture_output=True,
            text=True,
            check=False,
        )

    good = run()
    assert (good.returncode, good.stderr) == (0, "")
    assert "matches the 9 nights counted" in good.stdout
    change_surplus(root / "syn-d1-r1", 2, 1)
    bad = run()
    assert (bad.returncode, bad.stdout) == (1, "")
    assert "differs from the logs" in bad.stderr


def test_a_season_published_with_the_command_is_checked_and_caught(
    real_season: Path, tmp_path: Path
) -> None:
    """The mock nights played through the model path, the board written by `season --out`."""
    root = copy_season(real_season, tmp_path / "seasons" / "s1")
    written = runner.invoke(app, ["season", str(root), "--out", str(root / "leaderboard.json")])
    assert written.exit_code == 0, written.output
    assert run_check(tmp_path / "seasons") == 0
    change_surplus(root / "s1-d2-r1", 3, -1)
    assert run_check(tmp_path / "seasons") == 1


# ------------------------------------------------------------------------------ the CI step


def test_ci_runs_the_check_after_the_tests_in_the_job_that_has_the_package() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    step = "run: uv run python tools/check_leaderboard.py"
    assert text.count(step) == 1
    assert text.index("uv run pytest -q") < text.index(step) < text.index("  viewer:")
    assert text.index("uv sync") < text.index(step)
