"""Recompute the published season leaderboards from their logs and compare them byte for byte.

A published season is a directory ``seasons/<name>/`` that holds ``leaderboard.json`` and, below
it, the night directories: the ``events.jsonl`` of each night and the ``config.yaml`` it was run
with. The board is rebuilt from those logs with the fixed method of ``auction_night.season`` (its
default seed and number of resamples) and written the way ``auction-night season --out`` writes
it. Any difference from the committed file is a failure: a log or a score that was changed after
the board was made, a night that was added or removed, a board made with other settings, or a
board that was edited by hand.

Without a season directory there is nothing to check; the script says so in one line and exits
with status 0.

Usage: python tools/check_leaderboard.py [--seasons DIR] [--resamples N] [--seed TEXT]
Exit status: 0 every board matches (or there is no season), 1 a board differs or is missing,
2 usage error, or a log that cannot be read. It needs the package: run it with ``uv run``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
REPO = TOOLS_DIR.parent
DEFAULT_SEASONS = "seasons"
BOARD_FILE = "leaderboard.json"
EVENTS_FILE = "events.jsonl"


def find_seasons(root: Path) -> list[Path]:
    """The season directories under ``root``: its subdirectories that hold a leaderboard or logs."""
    if not root.is_dir():
        return []
    return sorted(
        child
        for child in root.iterdir()
        if child.is_dir() and ((child / BOARD_FILE).is_file() or any(child.rglob(EVENTS_FILE)))
    )


def first_difference(committed: str, recomputed: str) -> str:
    """Where two texts part, as a short sentence."""
    old, new = committed.splitlines(), recomputed.splitlines()
    for number, (a, b) in enumerate(zip(old, new, strict=False), start=1):
        if a != b:
            return f"line {number} is {a.strip()!r} in the file and {b.strip()!r} from the logs"
    if len(old) != len(new):
        return f"the file has {len(old)} lines and the logs give {len(new)}"
    return "the lines agree, but the line endings or the final newline do not"


def check_season(
    season_dir: Path, *, resamples: int | None = None, seed: str | None = None
) -> tuple[int, str]:
    """Check one season. Returns an exit status (0, 1 or 2) and a one-line message."""
    from auction_night import season

    published = season_dir / BOARD_FILE
    if not published.is_file():
        return 1, f"the logs have no {BOARD_FILE}: write it with `auction-night season --out`"
    try:
        board = season.leaderboard_for(
            [season_dir],
            resamples=season.DEFAULT_RESAMPLES if resamples is None else resamples,
            seed=season.DEFAULT_SEED if seed is None else seed,
        )
    except season.SeasonError as exc:
        return 2, f"cannot rebuild the board from the logs: {exc}"
    recomputed = season.leaderboard_json(board)
    committed = published.read_bytes()
    if committed == recomputed.encode("utf-8"):
        nights = board["season"]["nights"]
        return 0, f"{BOARD_FILE} matches the {nights} nights counted in its logs"
    text = committed.decode("utf-8", errors="replace")
    return 1, f"{BOARD_FILE} differs from the logs: {first_difference(text, recomputed)}"


def main(argv: Sequence[str] | None = None) -> int:
    """Check every published season and return the exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--seasons", default=DEFAULT_SEASONS, help="directory of the seasons (default: seasons)"
    )
    parser.add_argument("--resamples", type=int, help="number of resamples (default: the module's)")
    parser.add_argument("--seed", help="seed of the resamples (default: the module's)")
    args = parser.parse_args(argv)

    root = Path(args.seasons)
    root = root if root.is_absolute() else REPO / root
    seasons = find_seasons(root)
    if not seasons:
        print(f"check_leaderboard: no season directory under {args.seasons}, nothing to check")
        return 0
    try:
        import auction_night  # noqa: F401
    except ImportError:
        print(
            "check_leaderboard: the auction_night package is not importable; "
            "run `uv run python tools/check_leaderboard.py`",
            file=sys.stderr,
        )
        return 2

    worst = 0
    for season_dir in seasons:
        status, message = check_season(season_dir, resamples=args.resamples, seed=args.seed)
        label = f"{args.seasons}/{season_dir.name}"
        stream = sys.stdout if status == 0 else sys.stderr
        print(f"check_leaderboard: {label}: {message}", file=stream)
        worst = max(worst, status)
    return worst


if __name__ == "__main__":
    sys.exit(main())
