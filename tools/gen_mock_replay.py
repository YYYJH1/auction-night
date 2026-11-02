"""Generate the real-output replay sample ``tests/fixtures/replays/mock_night.json``.

The sample is the mock night of ``configs/night_mock.yaml`` (eight seats, twelve lots, all six
formats) played in memory with the mock models and the rule bots, commented by the mock host, and
exported by the same code that exports a published night, content gate included. Nothing is read
from the clock, the environment or git, so the output is the same on every machine and the file is
a pure function of the code: a test regenerates it and compares the bytes.

The seed of the mock night is inline (a mock configuration has nothing to hide), so the sample
also reveals it. The sample carries the show-layer parts too, made by the same code that makes them
for a published night (``show_parts``): the mock judges' extractions, the findings, the judges'
accuracy, the awards with the bots' reference, the highlights and the statistic columns of the
scores. The judges are mock judges, so nothing is called and nothing is read from outside.

Usage: python tools/gen_mock_replay.py [--check] [--out FILE]
Without options the file is written. With ``--check`` nothing is written and the exit status says
whether the file on disk is what the code produces. Exit status: 0 done (or up to date), 1 the
file is out of date, 2 usage error or a night that cannot be exported. Run it with ``uv run``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
REPO = TOOLS_DIR.parent
CONFIG = REPO / "configs" / "night_mock.yaml"
SAMPLE = REPO / "tests" / "fixtures" / "replays" / "mock_night.json"
STAMP = "2026-11-01T00:00:00Z"  # the clock of the recorded night; it appears in no output


def generate() -> str:
    """The text of the sample replay."""
    from auction_night.contracts.config import load_config
    from auction_night.show.content_scan import ContentGate
    from auction_night.show.factcard import build_fact_cards
    from auction_night.show.host import make_host, run_host
    from auction_night.show.replay_export import GitInfo, export_replay, play_offline, show_parts
    from auction_night.theory import analyse_night

    config = load_config(CONFIG)
    state, cassette = play_offline(config, clock=lambda: STAMP)
    theory = analyse_night(state)
    cards = build_fact_cards(state, theory=theory)
    host = run_host(cards, make_host(config)).block
    done = export_replay(
        config,
        state,
        host,
        gate=ContentGate.from_config(config),
        theory=theory,
        cards=cards,
        parts=show_parts(config, state, cards),
        cassettes=[cassette],
        git=GitInfo(),
        created_utc=None,
        reveal_seed=True,
    )
    return done.text


def main(argv: Sequence[str] | None = None) -> int:
    """Write the sample, or check it, and return the exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--check", action="store_true", help="compare the file, write nothing")
    parser.add_argument("--out", type=Path, default=SAMPLE, help="the file (default: the sample)")
    args = parser.parse_args(argv)
    try:
        text = generate()
    except ImportError:
        print(
            "gen_mock_replay: the auction_night package is not importable; "
            "run `uv run python tools/gen_mock_replay.py`",
            file=sys.stderr,
        )
        return 2
    except ValueError as exc:
        print(f"gen_mock_replay: the sample cannot be exported: {exc}", file=sys.stderr)
        return 2
    target: Path = args.out
    if args.check:
        current = target.read_bytes() if target.is_file() else None
        if current == text.encode("utf-8"):
            print(f"gen_mock_replay: {target.name} is up to date")
            return 0
        print(
            f"gen_mock_replay: {target.name} is out of date: run without --check", file=sys.stderr
        )
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")
    print(f"gen_mock_replay: wrote {target.name} ({len(text.encode('utf-8'))} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
