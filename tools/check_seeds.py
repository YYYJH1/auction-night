"""Check the seed commitments of the published seasons, and that no private seed file is committed.

A published season is a directory ``seasons/<name>/`` that holds ``commitments.json`` (published
before the first night), ``seeds.json`` (published after the last one), the night directories and
the planned configurations. For each season the script runs
``auction_night.seeds.verify_season_dir``:

* ``commitments.json`` must be well formed, and every night and every planned configuration below
  the directory must carry the commitment that was published for its deck.
* Without ``seeds.json`` that is all there is to check, and the script does not complain about the
  missing file: a season that is still being played has not revealed its seeds.
* With ``seeds.json``, every seed must open its commitment, and every night must bring a
  configuration and a log (or a replay) from which its cards are drawn again from the seed of its
  deck and compared with the cards in the log.

A season directory with nights but without ``commitments.json`` fails: its seeds were not committed.

Whatever the seasons, the script also looks at every file that git tracks, or would add, for the
private seed file of a season (by its name, or by the ``schema`` it declares), because that file
must never leave the machine of the person who runs the season. With ``--private FILE`` it looks
for the seeds of that file inside those files, which is the check to run before the reveal.

Without a season directory there is nothing to check; the script says so in one line and exits
with status 0.

Usage: python tools/check_seeds.py [--seasons DIR] [--root DIR] [--private FILE]
Exit status: 0 everything holds (or there is no season), 1 a check failed, 2 usage error or a file
that cannot be read. It needs git, and the package for the seasons: run it with ``uv run``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

import scan_text

TOOLS_DIR = Path(__file__).resolve().parent
REPO = TOOLS_DIR.parent
DEFAULT_SEASONS = "seasons"
COMMITMENTS_FILE = "commitments.json"
REVEAL_FILE = "seeds.json"
EVENTS_FILE = "events.jsonl"
# The same strings as auction_night.seeds.PRIVATE_SCHEMA and PRIVATE_SUFFIX (a test compares them).
PRIVATE_SCHEMA = "auction-night/season-seeds-private@1"
PRIVATE_NAME = "seeds-private"  # the .gitignore pattern is *seeds-private*
MAX_BYTES = 1 << 20


def find_seasons(root: Path) -> list[Path]:
    """The season directories under ``root``: its subdirectories that hold a commitments file, a
    seeds file or night logs."""
    if not root.is_dir():
        return []
    return sorted(
        child
        for child in root.iterdir()
        if child.is_dir()
        and (
            (child / COMMITMENTS_FILE).is_file()
            or (child / REVEAL_FILE).is_file()
            or any(child.rglob(EVENTS_FILE))
        )
    )


def find_private_files(root: Path, names: Iterable[str]) -> list[tuple[str, str]]:
    """The files among ``names`` (paths relative to ``root``) that are private seed files.

    A file counts if its name says so, or if it is a JSON object whose ``schema`` is that of the
    private file, whatever it is called. Returns ``(path, reason)`` pairs.
    """
    found = []
    for name in names:
        if PRIVATE_NAME in Path(name).name:
            found.append((name, "is named like a private seed file"))
            continue
        text = scan_text.read_text(root / name)
        if text is None or len(text) > MAX_BYTES or not text.lstrip().startswith("{"):
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("schema") == PRIVATE_SCHEMA:
            found.append((name, "is a private seed file"))
    return found


def find_seeds(root: Path, names: Iterable[str], private: Path) -> list[tuple[str, str]]:
    """The files among ``names`` that contain a seed of the private file. Returns ``(path, deck)``
    pairs; the seeds themselves are never shown."""
    from auction_night import seeds

    secret = seeds.read_private(private)
    found = []
    for name in names:
        text = scan_text.read_text(root / name)
        if text is None:
            continue
        found += [(name, deck) for deck, seed in secret.by_deck.items() if seed in text]
    return found


def check_season(season_dir: Path) -> tuple[int, list[str]]:
    """Check one season. Returns an exit status (0, 1 or 2) and the lines to show."""
    from auction_night import seeds

    if not (season_dir / COMMITMENTS_FILE).is_file():
        why = "the seeds of this season were not committed before it was played"
        return 1, [f"no {COMMITMENTS_FILE}: {why}"]
    try:
        report = seeds.verify_season_dir(season_dir)
    except seeds.SeedsError as exc:
        return 2, [str(exc)]
    if report.failed:
        return 1, report.lines()
    state = "seeds revealed" if (season_dir / REVEAL_FILE).is_file() else "seeds not revealed yet"
    return 0, [f"{report.summary()} ({state})"]


def main(argv: Sequence[str] | None = None) -> int:
    """Run the checks and return the exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--seasons", default=DEFAULT_SEASONS, help="directory of the seasons (default: seasons)"
    )
    parser.add_argument("--root", type=Path, default=REPO, help="repository root")
    parser.add_argument(
        "--private", type=Path, help="private seed file: look for its seeds in the repository"
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()

    try:
        names = scan_text.list_files(root, include_untracked=True)
    except RuntimeError as exc:
        print(f"check_seeds: {exc}", file=sys.stderr)
        return 2
    worst = 0
    leaks = find_private_files(root, names)
    for name, reason in leaks:
        print(f"check_seeds: {name} {reason}: it must never be committed", file=sys.stderr)
    if leaks:
        worst = 1
    else:
        print(f"check_seeds: no private seed file among {len(names)} files")
    if args.private is not None:
        try:
            hits = find_seeds(root, names, args.private)
        except (ImportError, ValueError) as exc:
            print(f"check_seeds: {exc}", file=sys.stderr)
            return 2
        for name, deck in hits:
            print(f"check_seeds: {name} holds the seed of deck {deck}", file=sys.stderr)
        worst = max(worst, 1 if hits else 0)

    seasons_dir = Path(args.seasons)
    seasons_dir = seasons_dir if seasons_dir.is_absolute() else root / seasons_dir
    seasons = find_seasons(seasons_dir)
    if not seasons:
        print(f"check_seeds: no season directory under {args.seasons}, nothing to check")
        return worst
    try:
        import auction_night  # noqa: F401
    except ImportError:
        print(
            "check_seeds: the auction_night package is not importable; "
            "run `uv run python tools/check_seeds.py`",
            file=sys.stderr,
        )
        return 2
    for season_dir in seasons:
        status, lines = check_season(season_dir)
        stream = sys.stdout if status == 0 else sys.stderr
        label = f"{args.seasons}/{season_dir.name}"
        for line in lines:
            print(f"check_seeds: {label}: {line}", file=stream)
        worst = max(worst, status)
    return worst


if __name__ == "__main__":
    sys.exit(main())
