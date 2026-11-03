"""Summarise the memory experiment: the same decks played with night.memory off and on.

Each deck of a phase is played twice on one deal seed, with configs/night_local.yaml (memory off)
and configs/night_local_memory.yaml (memory on). The nights sit in
runs/exp-memory/<phase>/<off|memory>/dNN/. For every night this script reads events.jsonl,
calls.jsonl and config.yaml and works out, over the model seats:

- the share of pledges kept, of the pledges that were kept or broken (the primary measure),
- the share of won lots paid above the seat's own planned budget for the lot,
- the share of lots of priority 2 or 3 in the seat's plan that the seat did not bid on,
- the mean surplus of a model seat, the missions completed, the share of attempts that could not
  be parsed and the tokens read and written.

Then it pairs the two nights of each deck, takes the difference (memory on minus off) and gives
its mean over decks with a 95% bootstrap interval over decks.

Usage: uv run python tools/exp_memory.py [--phase eval] [--runs DIR] [--out FILE.json]
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections.abc import Sequence
from pathlib import Path
from statistics import fmean
from typing import Any

import yaml

from auction_night.contracts.events import (
    ActionEvent,
    HammerEvent,
    MissionResultEvent,
    NightEndEvent,
    PlanEvent,
    loads_events,
)
from auction_night.show.pledges import judge_pledges

CONDITIONS = ("off", "memory")
MEASURES = (
    "keep_rate",
    "overspend_rate",
    "skip_rate",
    "mean_surplus",
    "missions_done",
    "parse_failed_rate",
    "tokens_in",
    "tokens_out",
)
RESAMPLES = 10_000
SEED = "auction-night-exp-memory"


def model_seats(config: Path) -> set[int]:
    """The seats of the night that a language model plays."""
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    return {int(s["seat"]) for s in data["seats"] if s.get("kind") == "llm"}


def ratio(part: int, whole: int) -> float | None:
    return part / whole if whole else None


def night_measures(night: Path) -> dict[str, Any] | None:
    """The measures of one night, or None when the night did not finish."""
    events = loads_events((night / "events.jsonl").read_text(encoding="utf-8"))
    end = next((e for e in events if isinstance(e, NightEndEvent)), None)
    if end is None:
        return None
    seats = model_seats(night / "config.yaml")

    kept = broken = void = 0
    for record in judge_pledges(events):
        if record.seat not in seats:
            continue
        if record.outcome == "kept":
            kept += 1
        elif record.outcome == "violated":
            broken += 1
        else:
            void += 1

    plans = {
        e.data.seat: e.data for e in events if isinstance(e, PlanEvent) and e.data.seat in seats
    }
    bid_on = {
        (e.data.seat, e.lot)
        for e in events
        if isinstance(e, ActionEvent) and e.data.seat in seats and (e.data.value or 0) > 0
    }
    wins = over = 0
    for e in events:
        if not isinstance(e, HammerEvent) or e.data.winner not in seats or e.lot is None:
            continue
        plan = plans.get(e.data.winner)
        if plan is None or e.lot > len(plan.budget_plan):
            continue
        wins += 1
        paid = e.data.payments.get(e.data.winner, e.data.price)
        over += paid > plan.budget_plan[e.lot - 1]
    cells = skipped = 0
    for seat, plan in plans.items():
        for lot, priority in enumerate(plan.priorities, start=1):
            if priority >= 2:
                cells += 1
                skipped += (seat, lot) not in bid_on

    scores = [s for s in end.data.scores if s.seat in seats]
    missions = sum(
        1
        for e in events
        if isinstance(e, MissionResultEvent) and e.data.seat in seats and e.data.completed
    )

    attempts = failed = tokens_in = tokens_out = 0
    for line in (night / "calls.jsonl").read_text(encoding="utf-8").splitlines():
        call = json.loads(line)
        attempts += 1
        failed += call.get("outcome") == "parse_failed"
        usage = call.get("usage") or {}
        tokens_in += int(usage.get("input_tokens") or 0)
        tokens_out += int(usage.get("output_tokens") or 0)

    return {
        "status": end.data.status,
        "pledges_kept": kept,
        "pledges_broken": broken,
        "pledges_void": void,
        "keep_rate": ratio(kept, kept + broken),
        "wins": wins,
        "overspent": over,
        "overspend_rate": ratio(over, wins),
        "priority_cells": cells,
        "priority_skipped": skipped,
        "skip_rate": ratio(skipped, cells),
        "mean_surplus": fmean(s.surplus for s in scores) if scores else None,
        "missions_done": missions,
        "attempts": attempts,
        "parse_failed": failed,
        "parse_failed_rate": ratio(failed, attempts),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
    }


def bootstrap(diffs: Sequence[float], rng: random.Random) -> tuple[float, float]:
    """The 2.5th and 97.5th percentiles of the mean over resamples of the decks."""
    n = len(diffs)
    means = sorted(fmean(rng.choice(diffs) for _ in range(n)) for _ in range(RESAMPLES))
    return means[int(0.025 * RESAMPLES)], means[int(0.975 * RESAMPLES) - 1]


def summarise(root: Path) -> dict[str, Any]:
    decks = sorted(
        {p.name for c in CONDITIONS for p in (root / c).glob("d[0-9][0-9]") if p.is_dir()}
    )
    nights: list[dict[str, Any]] = []
    by_deck: dict[str, dict[str, dict[str, Any]]] = {}
    for deck in decks:
        for cond in CONDITIONS:
            path = root / cond / deck
            measures = night_measures(path) if (path / "events.jsonl").is_file() else None
            nights.append({"deck": deck, "condition": cond, "measures": measures})
            if measures is not None:
                by_deck.setdefault(deck, {})[cond] = measures
    paired = {d: m for d, m in by_deck.items() if set(m) == set(CONDITIONS)}
    rng = random.Random(SEED)
    differences: dict[str, Any] = {}
    for measure in MEASURES:
        diffs = [
            float(m["memory"][measure]) - float(m["off"][measure])
            for m in paired.values()
            if m["memory"][measure] is not None and m["off"][measure] is not None
        ]
        if not diffs:
            differences[measure] = {"n": 0}
            continue
        low, high = bootstrap(diffs, rng)
        differences[measure] = {
            "n": len(diffs),
            "mean_off": fmean(
                float(m["off"][measure]) for m in paired.values() if m["off"][measure] is not None
            ),
            "mean_memory": fmean(
                float(m["memory"][measure])
                for m in paired.values()
                if m["memory"][measure] is not None
            ),
            "mean_difference": fmean(diffs),
            "interval_95": [low, high],
        }
    unpaired = sorted(set(decks) - set(paired))
    return {
        "decks": len(decks),
        "paired_decks": len(paired),
        "unpaired_decks": unpaired,
        "resamples": RESAMPLES,
        "seed": SEED,
        "differences": differences,
        "nights": nights,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--phase", default="eval")
    parser.add_argument("--runs", default="runs/exp-memory")
    parser.add_argument("--out", help="also write the summary as JSON to this file")
    args = parser.parse_args(argv)
    root = Path(args.runs) / args.phase
    if not root.is_dir():
        print(f"exp_memory: no directory {root}", file=sys.stderr)
        return 2
    summary = summarise(root)
    print(f"{args.phase}: {summary['paired_decks']} paired decks of {summary['decks']}")
    for measure, d in summary["differences"].items():
        if d["n"]:
            low, high = d["interval_95"]
            print(
                f"  {measure:18s} n={d['n']:2d}"
                f"  off {d['mean_off']:10.3f}  memory {d['mean_memory']:10.3f}"
                f"  difference {d['mean_difference']:+10.3f}  [{low:+.3f}, {high:+.3f}]"
            )
    if args.out:
        Path(args.out).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
