"""Rank intervals: the unit of the resampling, the quantile rule, ties and determinism.

The intervals of small seasons are checked against an exact enumeration of every possible
resample, written here independently of the module. A margin check makes sure the data keep clear
of the 2.5% and 97.5% lines, so that the sampling noise of 10 000 resamples cannot decide the test.
"""

import os
import subprocess
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from fractions import Fraction
from itertools import product
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from season_support import (
    copy_season,
    write_rotation_season,
    write_single_season,
)

from auction_night import season
from auction_night.engine.rng import Stream

HONEST, COLLUDE, BACKSTAB, BOT = "mock-honest", "mock-collude", "mock-backstab", "bot:truthful"
Table = Sequence[Mapping[str, tuple[int, int]]]  # per unit: entry -> (surplus, seat-nights)


def exact_intervals(table: Table) -> tuple[dict[str, tuple[int, int]], Fraction]:
    """The rank interval of every entry when each of the m^m resamples is equally likely.

    Returns the intervals and the smallest distance of a cumulative share to 1/40 or 39/40.
    """
    entries = sorted(table[0])
    counts: dict[str, Counter[int]] = {entry: Counter() for entry in entries}
    draws = list(product(range(len(table)), repeat=len(table)))
    for draw in draws:
        mean = {
            e: Fraction(sum(table[u][e][0] for u in draw), sum(table[u][e][1] for u in draw))
            for e in entries
        }
        for e in entries:
            counts[e][1 + sum(1 for other in entries if mean[other] > mean[e])] += 1
    out: dict[str, tuple[int, int]] = {}
    margin = Fraction(1)
    for e in entries:
        cumulative, low, high = 0, None, None
        for rank in range(1, len(entries) + 1):
            cumulative += counts[e][rank]
            share = Fraction(cumulative, len(draws))
            margin = min(margin, abs(share - Fraction(1, 40)), abs(share - Fraction(39, 40)))
            if low is None and share >= Fraction(1, 40):
                low = rank
            if high is None and share >= Fraction(39, 40):
                high = rank
        assert low is not None and high is not None
        out[e] = (low, high)
    return out, margin


def intervals_of(board: Mapping[str, Any]) -> dict[str, tuple[int, int]]:
    return {e["entry"]: (e["rank_low"], e["rank_high"]) for e in board["entries"]}


# Two decks of three nights. The honest model wins the first deck and loses the second, the
# colluder always scores 0, the backstabber -9 and the bot 5.
DECKS = {
    "d1": {HONEST: 10, COLLUDE: 0, BACKSTAB: -9, BOT: 5},
    "d2": {HONEST: -10, COLLUDE: 0, BACKSTAB: -9, BOT: 5},
}


def deck_season(root: Path) -> Path:
    write_rotation_season(
        root,
        lambda model, deck, rotation: DECKS[deck][model],
        bot=lambda deck, rotation: DECKS[deck][BOT],
    )
    return root


def deck_table() -> Table:
    return [{e: (3 * score, 3) for e, score in DECKS[deck].items()} for deck in sorted(DECKS)]


def night_table() -> Table:
    return [{e: (score, 1) for e, score in DECKS[deck].items()} for deck in DECKS for _ in range(3)]


def test_a_rotation_season_resamples_whole_decks(tmp_path: Path) -> None:
    board = season.leaderboard_for([deck_season(tmp_path / "s")])
    assert (board["season"]["unit"], board["season"]["units"]) == ("deck", 2)
    expected, margin = exact_intervals(deck_table())
    assert margin > Fraction(8, 1000)
    assert intervals_of(board) == expected
    # resampling the six nights one by one would give the backstabber another interval, so the
    # test would notice if the unit were the night
    by_night, _ = exact_intervals(night_table())
    assert by_night[BACKSTAB] != expected[BACKSTAB]
    assert expected[BACKSTAB] == (3, 4) and by_night[BACKSTAB] == (4, 4)


def test_a_single_night_season_resamples_nights(tmp_path: Path) -> None:
    nights = [
        {HONEST: 9, COLLUDE: 3, BACKSTAB: 0},
        {HONEST: 0, COLLUDE: 6, BACKSTAB: 3},
        {HONEST: 3, COLLUDE: 1, BACKSTAB: 8},
    ]
    write_single_season(tmp_path / "s", nights)
    board = season.leaderboard_for([tmp_path / "s"])
    assert (board["season"]["unit"], board["season"]["units"]) == ("night", 3)
    table = [{**{e: (score, 1) for e, score in night.items()}, BOT: (0, 1)} for night in nights]
    expected, margin = exact_intervals(table)
    assert margin > Fraction(8, 1000)
    assert intervals_of(board) == expected


def test_unequal_decks_are_weighed_by_their_nights(tmp_path: Path) -> None:
    """Dropping one rotation of a deck leaves a deck of two nights: the board is still the mean
    per night, and the resampling still draws decks."""
    root = deck_season(tmp_path / "s")
    for victim in ("syn-d2-r2",):
        season_dir = root / victim
        (season_dir / "events.jsonl").unlink()
        (season_dir / "config.yaml").unlink()
        season_dir.rmdir()
    board = season.leaderboard_for([root])
    assert board["season"]["decks"] == [
        {"deck": "d1", "nights": 3, "rotations": [0, 1, 2], "complete": True},
        {"deck": "d2", "nights": 2, "rotations": [0, 1], "complete": False},
    ]
    honest = next(e for e in board["entries"] if e["entry"] == HONEST)
    assert honest["mean_milli"] == round(1000 * Fraction(30 - 20, 5))
    table: Table = [
        {e: (3 * s, 3) for e, s in DECKS["d1"].items()},
        {e: (2 * s, 2) for e, s in DECKS["d2"].items()},
    ]
    expected, margin = exact_intervals(table)
    assert margin > Fraction(8, 1000)
    assert intervals_of(board) == expected


def test_models_that_score_alike_share_the_best_rank(tmp_path: Path) -> None:
    """Equal scores in every resample are tied in every resample, and a tie is the better rank."""
    scores = {"d1": 7, "d2": 3}
    write_rotation_season(
        tmp_path / "s",
        lambda model, deck, rotation: -20 if model == BACKSTAB else scores[deck],
        bot=lambda deck, rotation: 50,
    )
    board = season.leaderboard_for([tmp_path / "s"])
    assert intervals_of(board) == {BOT: (1, 1), HONEST: (2, 2), COLLUDE: (2, 2), BACKSTAB: (4, 4)}
    assert [(e["entry"], e["rank"]) for e in board["entries"]] == [
        (BOT, 1),
        (COLLUDE, 2),
        (HONEST, 2),
        (BACKSTAB, 4),
    ]


def test_the_interval_of_the_mean_of_two_units_spans_them(tmp_path: Path) -> None:
    write_rotation_season(tmp_path / "s", lambda model, deck, rotation: 10 if deck == "d1" else -30)
    board = season.leaderboard_for([tmp_path / "s"])
    honest = next(e for e in board["entries"] if e["entry"] == HONEST)
    # with two units the resampled mean is the first, the second or their midpoint
    assert (honest["mean_low_milli"], honest["mean_milli"], honest["mean_high_milli"]) == (
        -30_000,
        -10_000,
        10_000,
    )


def test_one_unit_gives_no_intervals(tmp_path: Path) -> None:
    write_rotation_season(tmp_path / "s", lambda model, deck, rotation: 1, decks=("d1",))
    board = season.leaderboard_for([tmp_path / "s"])
    assert board["season"]["units"] == 1
    for entry in board["entries"]:
        assert (entry["rank_low"], entry["rank_high"]) == (None, None)
        assert (entry["mean_low_milli"], entry["mean_high_milli"]) == (None, None)
    assert board["footer"]["gap_milli"] is None
    assert "n/a" in season.leaderboard_text(board).splitlines()[5]


# ------------------------------------------------------------------- the quantile rule


def test_a_rank_quantile_is_the_smallest_rank_with_enough_resamples() -> None:
    rank_of = season._quantile_rank
    # 40 resamples: the 2.5% line is one resample, the 97.5% line is the 39th
    assert rank_of([0, 1, 39, 0], 40, 25) == 1
    assert rank_of([0, 0, 40, 0], 40, 25) == 2
    assert rank_of([0, 38, 2, 0], 40, 975) == 2
    assert rank_of([0, 39, 1, 0], 40, 975) == 1
    assert rank_of([0, 0, 0, 40], 40, 975) == 3


def test_a_mean_quantile_is_the_order_statistic_of_the_same_rule() -> None:
    index = season._order_index
    assert (index(25, 40), index(975, 40)) == (0, 38)
    assert (index(25, 10_000), index(975, 10_000)) == (249, 9_749)
    assert (index(25, 1), index(975, 1)) == (0, 0)
    assert (index(25, 41), index(975, 41)) == (1, 39)


# ------------------------------------------------------------------------- determinism


def synthetic_nights(count: int = 12) -> list[dict[str, int]]:
    """Scores from a small linear congruential generator, so they are the same everywhere."""
    state, out = 12345, []
    for _ in range(count):
        row = {}
        for model in (HONEST, COLLUDE, BACKSTAB):
            state = (state * 1103515245 + 12345) % 2**31
            row[model] = state % 200 - 100
        out.append(row)
    return out


def test_the_same_logs_seed_and_resamples_give_the_same_bytes(tmp_path: Path) -> None:
    write_single_season(tmp_path / "a", synthetic_nights())
    first = season.leaderboard_json(season.leaderboard_for([tmp_path / "a"]))
    again = season.leaderboard_json(season.leaderboard_for([tmp_path / "a"]))
    assert first == again
    moved = copy_season(tmp_path / "a", tmp_path / "elsewhere" / "b")
    assert season.leaderboard_json(season.leaderboard_for([moved])) == first  # no path inside
    names = sorted(p.name for p in (tmp_path / "a").iterdir())
    shuffled = [tmp_path / "a" / name for name in reversed(names)]
    assert season.leaderboard_json(season.leaderboard_for(shuffled)) == first  # order of arguments


def test_the_bytes_do_not_depend_on_the_hash_seed_of_the_interpreter(tmp_path: Path) -> None:
    """Another process with another string hash seed writes the same board."""
    write_single_season(tmp_path / "a", synthetic_nights())
    expected = season.leaderboard_json(season.leaderboard_for([tmp_path / "a"], resamples=200))
    for hash_seed in ("1", "2"):
        proc = subprocess.run(
            [sys.executable, "-m", "auction_night.cli", "season", str(tmp_path / "a")]
            + ["--json", "--resamples", "200"],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PYTHONHASHSEED": hash_seed},
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == expected


def test_the_seed_and_the_number_of_resamples_are_recorded_and_matter(tmp_path: Path) -> None:
    write_single_season(tmp_path / "a", synthetic_nights())
    base = season.leaderboard_for([tmp_path / "a"], resamples=500, seed="one")
    other = season.leaderboard_for([tmp_path / "a"], resamples=500, seed="two")
    more = season.leaderboard_for([tmp_path / "a"], resamples=700, seed="one")
    assert (base["method"]["seed"], base["method"]["resamples"]) == ("one", 500)
    assert (other["method"]["seed"], more["method"]["resamples"]) == ("two", 700)

    def numbers(board: Mapping[str, Any]) -> list[tuple[Any, ...]]:
        keys = ("rank_low", "rank_high", "mean_low_milli", "mean_high_milli")
        return [tuple(e[k] for k in keys) for e in board["entries"]]

    assert numbers(base) != numbers(other)
    assert numbers(base) != numbers(more)
    assert [e["mean_milli"] for e in base["entries"]] == [e["mean_milli"] for e in other["entries"]]


def reference_bootstrap(
    table: Table, resamples: int, seed: str
) -> dict[str, tuple[int, int, Fraction, Fraction]]:
    """The same resampling written again with exact fractions and sorted lists.

    It draws the units with the same hash-based stream. The quantile of a sorted list is the
    element at index ``ceil(p * B) - 1``.
    """
    entries = sorted(table[0])
    units = len(table)
    ranks: dict[str, list[int]] = {e: [] for e in entries}
    means: dict[str, list[Fraction]] = {e: [] for e in entries}
    for b in range(resamples):
        stream = Stream(seed, "season.resample", b)
        draw = [stream.randbelow(units) for _ in range(units)]
        mean = {
            e: Fraction(sum(table[u][e][0] for u in draw), sum(table[u][e][1] for u in draw))
            for e in entries
        }
        for e in entries:
            means[e].append(mean[e])
            ranks[e].append(1 + sum(1 for other in entries if mean[other] > mean[e]))
    low, high = -(-resamples * 25 // 1000) - 1, -(-resamples * 975 // 1000) - 1
    return {
        e: (
            sorted(ranks[e])[low],
            sorted(ranks[e])[high],
            sorted(means[e])[low],
            sorted(means[e])[high],
        )
        for e in entries
    }


@pytest.mark.parametrize(("count", "resamples"), [(12, 1000), (7, 400)])
def test_the_intervals_match_an_independent_bootstrap_with_the_same_draws(
    count: int, resamples: int, tmp_path: Path
) -> None:
    nights = synthetic_nights(count)
    write_single_season(tmp_path / "a", nights)
    board = season.leaderboard_for([tmp_path / "a"], resamples=resamples, seed="cross")
    table = [{**{e: (score, 1) for e, score in night.items()}, BOT: (0, 1)} for night in nights]
    reference = reference_bootstrap(table, resamples, "cross")
    for entry in board["entries"]:
        low, high, mean_low, mean_high = reference[entry["entry"]]
        assert (entry["rank_low"], entry["rank_high"]) == (low, high)
        assert entry["mean_low_milli"] == round(1000 * mean_low)
        assert entry["mean_high_milli"] == round(1000 * mean_high)


@settings(max_examples=60, deadline=None)
@given(
    st.integers(min_value=2, max_value=4).flatmap(
        lambda entries: st.lists(
            st.lists(
                st.tuples(st.integers(-40, 40), st.integers(1, 3)),
                min_size=entries,
                max_size=entries,
            ),
            min_size=2,
            max_size=6,
        )
    )
)
def test_any_matrix_of_units_gives_the_intervals_of_the_independent_bootstrap(
    rows: list[list[tuple[int, int]]],
) -> None:
    """Unit by entry (surplus, seat-nights), including unequal seat-nights and ties."""
    names = [f"e{i}" for i in range(len(rows[0]))]
    table = [dict(zip(names, row, strict=True)) for row in rows]
    surplus = [[row[i][0] for row in rows] for i in range(len(names))]
    seat_nights = [[row[i][1] for row in rows] for i in range(len(names))]
    got = season._bootstrap(surplus, seat_nights, 120, "property")
    reference = reference_bootstrap(table, 120, "property")
    for i, name in enumerate(names):
        low, high, mean_low, mean_high = reference[name]
        assert (got.rank_low[i], got.rank_high[i]) == (low, high)
        assert (got.mean_low[i], got.mean_high[i]) == (mean_low, mean_high)
        assert 1 <= got.rank_low[i] <= got.rank_high[i] <= len(names)
        assert got.mean_low[i] <= got.mean_high[i]


def test_the_intervals_of_a_small_season_are_pinned(tmp_path: Path) -> None:
    """A change of the resampling, the rng or the quantile rule moves these numbers."""
    write_single_season(tmp_path / "a", synthetic_nights())
    board = season.leaderboard_for([tmp_path / "a"], resamples=1000, seed="pinned")
    assert [
        [e["entry"], e["rank"], e["rank_low"], e["rank_high"], e["mean_milli"]]
        for e in board["entries"]
    ] == [
        ["mock-backstab", 1, 1, 3, 34500],
        ["mock-collude", 2, 1, 4, 26833],
        ["mock-honest", 3, 1, 4, 16500],
        ["bot:truthful", 4, 3, 4, 0],
    ]


@pytest.mark.parametrize("bad", [0, -3, True, 2.5])
def test_the_number_of_resamples_is_a_positive_whole_number(bad: Any, tmp_path: Path) -> None:
    write_single_season(tmp_path / "a", synthetic_nights(3))
    with pytest.raises(season.SeasonError, match="resamples"):
        season.leaderboard_for([tmp_path / "a"], resamples=bad)


@pytest.mark.parametrize("bad", ["", "x" * 129, "tab\there"])
def test_the_seed_is_short_printable_text(bad: str, tmp_path: Path) -> None:
    write_single_season(tmp_path / "a", synthetic_nights(3))
    with pytest.raises(season.SeasonError, match="seed"):
        season.leaderboard_for([tmp_path / "a"], seed=bad)
