"""Helpers of the seed commitment tests: a small season played through the command line.

The season has two decks (``d01`` and ``d02``) and the mock night of ``configs/night_mock.yaml`` as
its template. Only two nights are played, ``s1-d01-r0`` and ``s1-d02-r1``, because a mock night
takes a second or two. The seeds are real random seeds from ``seeds new``, and the private file
lives in a directory of its own, as it would outside the repository.
"""

import json
import re
import shlex
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from auction_night.cli import app

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "configs" / "night_mock.yaml"
STAMP = "2026-11-01T12:00:00Z"
LATER = "2026-11-02T12:00:00Z"
NIGHTS = {"s1-d01-r0": "d01", "s1-d02-r1": "d02"}  # the nights that are played, and their decks

runner = CliRunner()


def plain(text: str) -> str:
    """Output without colour codes. On CI typer styles its help and its usage errors."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def cli(*args: str | Path, input: str | None = None) -> Any:
    """Run ``auction-night`` in process and return the click result."""
    return runner.invoke(app, [str(a) for a in args], input=input)


def seeds_cli(*args: str | Path) -> Any:
    """Run ``auction-night seeds ...``."""
    return cli("seeds", *args)


def parse_exports(output: str) -> dict[str, str]:
    """The variables that the lines of ``seeds env`` set."""
    found = {}
    for line in output.splitlines():
        assert line.startswith("export "), line
        name, _, value = line.removeprefix("export ").partition("=")
        found[name] = shlex.split(value)[0]
    return found


@contextmanager
def seeds_in_environment(private: Path, *decks: str) -> Iterator[dict[str, str]]:
    """Put the seeds of some decks into the environment, as ``eval "$(seeds env ...)"`` does."""
    args: list[str | Path] = ["env", "--private", private]
    for deck in decks:
        args += ["--deck", deck]
    result = seeds_cli(*args)
    assert result.exit_code == 0, result.output
    variables = parse_exports(result.output)
    with pytest.MonkeyPatch.context() as patch:
        for name, value in variables.items():
            patch.setenv(name, value)
        yield variables


@dataclass(frozen=True)
class Season:
    """A season on disk: where its files are, and the secret seeds for the assertions."""

    root: Path
    season_dir: Path
    private: Path
    seeds: dict[str, str]

    @property
    def commitments(self) -> Path:
        return self.season_dir / "commitments.json"

    @property
    def revealed(self) -> Path:
        return self.season_dir / "seeds.json"

    def night(self, night: str) -> Path:
        return self.season_dir / night

    def copy_to(self, target: Path) -> "Season":
        """A private copy of the whole tree that a test may damage."""
        shutil.copytree(self.root, target)
        relative = self.private.relative_to(self.root)
        return Season(
            target, target / self.season_dir.relative_to(self.root), target / relative, self.seeds
        )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def edit_json(path: Path, change: Callable[[Any], None]) -> None:
    """Read a JSON file, let ``change`` modify the data in place, and write it back."""
    data = read_json(path)
    change(data)
    write_json(path, data)


def edit_events(path: Path, change: Callable[[dict[str, Any]], bool]) -> int:
    """Apply ``change`` to every event of a log that it accepts. ``change`` modifies the event in
    place and returns True for an event it changed. Returns how many it changed."""
    lines = path.read_text(encoding="utf-8").split("\n")
    changed = 0
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        event = json.loads(line)
        if change(event):
            lines[index] = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            changed += 1
    path.write_text("\n".join(lines), encoding="utf-8")
    return changed


def card_of(night: Path, seat: int, change: Callable[[dict[str, Any]], None]) -> None:
    """Edit the private card of one seat in the log of a night (it must be there exactly once)."""

    def pick(event: dict[str, Any]) -> bool:
        if event["type"] == "deal_private" and event["data"]["seat"] == seat:
            change(event["data"])
            return True
        return False

    assert edit_events(night / "events.jsonl", pick) == 1


def play_season(root: Path) -> Season:
    """New, plan and two nights played, with the replays exported (seeds not revealed yet)."""
    season_dir = root / "seasons" / "s1"
    private = root / "private" / "s1.seeds-private.json"
    made = seeds_cli(
        "new", "--season", "s1", "--decks", "2", "--private-out", private,
        "--out", season_dir / "commitments.json", "--created-utc", STAMP,
    )  # fmt: skip
    assert made.exit_code == 0, made.output
    planned = seeds_cli(
        "plan", "--template", TEMPLATE, "--commitments", season_dir / "commitments.json",
        "--rotations", "2",
    )  # fmt: skip
    assert planned.exit_code == 0, planned.output
    secret = read_json(private)
    seeds = {entry["deck"]: entry["seed"] for entry in secret["decks"]}
    with seeds_in_environment(private, "d01", "d02"):
        for night in NIGHTS:
            config = season_dir / "configs" / f"{night}.yaml"
            ran = cli("run", "--config", config, "--out", season_dir / night)
            assert ran.exit_code == 0, ran.output
            done = cli("export", season_dir / night, "--no-git", "--created-utc", STAMP)
            assert done.exit_code == 0, done.output
    return Season(root, season_dir, private, seeds)


def reveal_season(season: Season) -> None:
    """Reveal the seeds and export the replays again with the seed in the manifest."""
    result = seeds_cli(
        "reveal", "--private", season.private, "--commitments", season.commitments,
        "--revealed-utc", LATER,
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    with seeds_in_environment(season.private, "d01", "d02"):
        for night in NIGHTS:
            args = ["--no-git", "--created-utc", LATER, "--reveal-seed"]
            done = cli("export", season.night(night), *args)
            assert done.exit_code == 0, done.output


def verify_args(season: Season, *extra: str | Path, revealed: bool = False) -> list[str | Path]:
    """Arguments of ``seeds verify`` for the nights of a season."""
    args: list[str | Path] = ["verify", "--commitments", season.commitments]
    if revealed:
        args += ["--seeds", season.revealed]
    return [*args, *extra, *(season.night(n) for n in NIGHTS)]
