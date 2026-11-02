"""The board: ranking by mean surplus, the mission board, the reference columns, what is left
out, and the shape of the JSON."""

import json
from collections import Counter
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
import yaml
from season_support import (
    SeatSpec,
    copy_season,
    play_real_night,
    real_config,
    rewrite_status,
    specs,
    write_calls,
    write_night,
    write_rotation_season,
    write_single_season,
)

from auction_night import season
from auction_night.contracts import loads_events
from auction_night.contracts.events import MissionResultEvent, NightEndEvent

HONEST, COLLUDE, BACKSTAB, BOT = "mock-honest", "mock-collude", "mock-backstab", "bot:truthful"


def entry_ids(config_path: Path) -> dict[int, str]:
    """Seat to entry id, read from a configuration snapshot without the module under test."""
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    return {
        s["seat"]: f"mock-{s['mock_policy']}" if s["kind"] == "mock" else f"bot:{s['policy']}"
        for s in raw["seats"]
    }


def by_entry(board: dict[str, Any], key: str = "entries") -> dict[str, dict[str, Any]]:
    return {row["entry"]: row for row in board[key]}


# ------------------------------------------------------------------- a real season


def test_the_board_ranks_the_mean_surplus_per_night(real_season: Path) -> None:
    totals: Counter[str] = Counter()
    seat_nights: Counter[str] = Counter()
    for night in sorted(real_season.iterdir()):
        ids = entry_ids(night / "config.yaml")
        events = loads_events((night / "events.jsonl").read_text(encoding="utf-8"))
        end = next(e for e in events if isinstance(e, NightEndEvent))
        for score in end.data.scores:
            totals[ids[score.seat]] += score.surplus
            seat_nights[ids[score.seat]] += 1
    board = season.leaderboard_for([real_season], resamples=200)
    rows = by_entry(board)
    assert set(rows) == set(totals) and len(rows) == 6
    for key, row in rows.items():
        assert row["mean_milli"] == round(1000 * Fraction(totals[key], seat_nights[key]))
        assert (row["nights"], row["seat_nights"]) == (8, 8)
    means = [(-Fraction(totals[r["entry"]], 8), r["entry"]) for r in board["entries"]]
    assert means == sorted(means)  # best first, equal scores in the order of the names
    assert [r["kind"] for r in board["entries"] if r["entry"].startswith("bot:")] == ["bot", "bot"]
    assert {r["label"] for r in board["entries"] if r["kind"] == "bot"} == {"老实人", "经济学家"}
    assert board["season"] == {
        "id": "s1",
        "format": "rotation",
        "format_known": True,
        "unit": "deck",
        "units": 2,
        "nights": 8,
        "left_out": [],
        "decks": [
            {"deck": "d1", "nights": 4, "rotations": [0, 1, 2, 3], "complete": True},
            {"deck": "d2", "nights": 4, "rotations": [0, 1, 2, 3], "complete": True},
        ],
    }


def test_the_mission_board_is_kept_apart_from_the_ranking(real_season: Path) -> None:
    held: Counter[str] = Counter()
    done: Counter[str] = Counter()
    bonus: Counter[str] = Counter()
    for night in sorted(real_season.iterdir()):
        ids = entry_ids(night / "config.yaml")
        events = loads_events((night / "events.jsonl").read_text(encoding="utf-8"))
        for event in events:
            if isinstance(event, MissionResultEvent):
                key = ids[event.data.seat]
                held[key] += 1
                done[key] += event.data.completed
                bonus[key] += event.data.bonus
    board = season.leaderboard_for([real_season], resamples=100)
    rows = by_entry(board, "missions")
    assert set(rows) == {HONEST, COLLUDE, BACKSTAB, "mock-random"}  # the bots hold no mission
    for key, row in rows.items():
        assert (row["held"], row["completed"], row["bonus_total"]) == (
            held[key],
            done[key],
            bonus[key],
        )
        assert row["bonus_mean_milli"] == round(1000 * Fraction(bonus[key], 8))
        assert sum(k["held"] for k in row["by_kind"].values()) == held[key]
        assert sum(k["completed"] for k in row["by_kind"].values()) == done[key]
        assert row["completion_milli"] == round(1000 * Fraction(done[key], held[key]))
    order = [(-r["bonus_mean_milli"], -r["completed"], r["entry"]) for r in board["missions"]]
    assert order == sorted(order)
    # the bonus is not in the surplus: the ranking does not change with it
    assert all("bonus" not in key for key in board["entries"][0])


def test_the_json_has_no_floats_and_a_fixed_shape(real_season: Path) -> None:
    board = season.leaderboard_for([real_season], resamples=100)
    text = season.leaderboard_json(board)

    def refuse(number: str) -> float:
        raise AssertionError(f"a float in the JSON: {number}")

    assert json.loads(text, parse_float=refuse) == board
    assert text.endswith("}\n") and not text.endswith("\n\n")
    assert list(board) == [
        "schema",
        "season",
        "method",
        "columns",
        "entries",
        "missions",
        "nights",
        "footer",
    ]
    assert "老实人" in text and "\\u" not in text  # written as UTF-8, not as escapes
    assert board["schema"] == season.SCHEMA_ID == "auction-night/leaderboard@1"
    assert board["nights"][0] == {"night": "s1-d1-r0", "unit": "d1", "deck": "d1", "rotation": 0}
    assert board["method"] == {
        "metric": "surplus",
        "resamples": 100,
        "seed": "season-leaderboard",
        "interval_per_mille": [25, 975],
        "ties": "shared_best_rank",
        "gap": {"alpha_per_mille": 50, "power_per_mille": 800},
    }
    with pytest.raises(ValueError, match="float"):
        season.leaderboard_json({**board, "extra": 0.5})


def test_nights_that_do_not_count_are_listed_and_change_nothing(
    real_season: Path, tmp_path: Path
) -> None:
    plain = season.leaderboard_for([real_season], resamples=200)
    root = copy_season(real_season, tmp_path / "s1")
    stopped = play_real_night(
        root / "s1-d3-r0", real_config("d3", 0, cost_cap=0.000001), priced=True
    )
    assert season.read_night(stopped).status == "budget_aborted"
    person = play_real_night(root / "s1-d3-r1", real_config("d3", 1))
    (person / "human.jsonl").write_text("", encoding="utf-8")
    cut = play_real_night(root / "s1-d3-r2", real_config("d3", 2))
    lines = (cut / "events.jsonl").read_text(encoding="utf-8").splitlines(keepends=True)
    (cut / "events.jsonl").write_text("".join(lines[:-2]), encoding="utf-8")  # night_end is gone
    costume = real_config("d3", 3)
    costume["night"]["mode"] = "variety"
    for seat in costume["seats"][:4]:
        seat["persona"] = "影帝"
    play_real_night(root / "s1-d3-r3", costume)  # played to the end, in variety mode

    board = season.leaderboard_for([root], resamples=200)
    assert board["season"]["left_out"] == [
        {"night": "s1-d3-r0", "reason": "budget_aborted"},
        {"night": "s1-d3-r1", "reason": "human"},
        {"night": "s1-d3-r2", "reason": "no_night_end"},
        {"night": "s1-d3-r3", "reason": "variety"},
    ]
    board["season"]["left_out"] = []
    assert board == plain  # the deck d3 has no night that counts, so it is not a unit either
    text = season.leaderboard_text(season.leaderboard_for([root], resamples=200))
    assert "4 left out" in text and "s1-d3-r0: budget_aborted" in text
    assert "s1-d3-r3: variety" in text


def test_a_deck_that_loses_a_night_is_flagged_not_dropped(
    real_season: Path, tmp_path: Path
) -> None:
    root = copy_season(real_season, tmp_path / "s1")
    rewrite_status(root / "s1-d2-r3", "budget_aborted")
    board = season.leaderboard_for([root], resamples=100)
    assert board["season"]["decks"][1] == {
        "deck": "d2",
        "nights": 3,
        "rotations": [0, 1, 2],
        "complete": False,
    }
    assert (board["season"]["nights"], board["season"]["units"]) == (7, 2)
    text = season.leaderboard_text(board)
    assert "decks that do not hold every rotation" in text
    assert "s1-d2-r3: budget_aborted" in text


def test_a_season_without_a_night_that_counts_has_no_board(tmp_path: Path) -> None:
    paths = write_single_season(tmp_path / "s", [{HONEST: 1, COLLUDE: 2, BACKSTAB: 3}] * 2)
    for path in paths:
        rewrite_status(path, "budget_aborted")
    with pytest.raises(
        season.SeasonError, match=r"no night counts: one-n00 \(budget_aborted\), one-n01"
    ):
        season.leaderboard_for([tmp_path / "s"])


# --------------------------------------------------------------- ties and the reference columns


def test_equal_means_share_a_rank_and_are_listed_by_name(tmp_path: Path) -> None:
    nights = [{"mock-honest": 10, "mock-collude": 10, "mock-backstab": 0}] * 3
    write_single_season(tmp_path / "s", nights, bot=10)
    board = season.leaderboard_for([tmp_path / "s"], resamples=50)
    assert [(r["entry"], r["rank"]) for r in board["entries"]] == [
        (BOT, 1),
        (COLLUDE, 1),
        (HONEST, 1),
        (BACKSTAB, 4),
    ]


def test_abstentions_and_refusals_are_rates_of_the_actions(tmp_path: Path) -> None:
    nights = [{"mock-honest": 1, "mock-collude": 2, "mock-backstab": 3}] * 2
    paths = write_single_season(tmp_path / "s", nights)
    first = specs(0, lambda model: 1, actions=0)
    first[0] = SeatSpec(**{**vars(first[0]), "actions": 4, "abstain": {"missing": 1, "refusal": 1}})
    second = specs(0, lambda model: 1, actions=0)
    second[0] = SeatSpec(
        **{**vars(second[0]), "actions": 4, "kept": 1, "abstain": {"timeout": 1, "not_asked": 5}}
    )
    spec = {"id": "one", "format": "single"}
    write_night(paths[0], "one-n00", first, season=spec)
    write_night(paths[1], "one-n01", second, season=spec)
    board = season.leaderboard_for([tmp_path / "s"], resamples=50)
    honest = by_entry(board)[HONEST]
    # 12 actions (the five that were never asked are not among them); missing, a kept price and
    # a timeout are 3 of them, the refusal is 1
    assert honest["actions"] == 12
    assert honest["abstentions"] == {
        "missing": 1,
        "parse_failed": 0,
        "timeout": 1,
        "refusal": 1,
        "budget_aborted": 0,
        "not_asked": 5,
        "kept_previous": 1,
    }
    assert (honest["abstain_rate_milli"], honest["refusal_rate_milli"]) == (250, 83)
    quiet = by_entry(board)[COLLUDE]
    assert (quiet["actions"], quiet["abstain_rate_milli"], quiet["refusal_rate_milli"]) == (
        0,
        None,
        None,
    )
    text = season.leaderboard_text(board)
    assert "25.0%" in text and "8.3%" in text and "n/a" in text
    assert "abstentions by reason" in text


def test_an_entry_with_two_seats_counts_the_mean_per_seat_and_night(tmp_path: Path) -> None:
    """The mock night sits one model at two seats: the score is per seat and night."""
    seats = [
        SeatSpec(1, "鲸落", 10, "honest"),
        SeatSpec(2, "阿铁", 30, "honest"),
        SeatSpec(3, "小满", -8, "collude"),
        SeatSpec(4, "老实人", 0, "truthful", bot=True),
    ]
    for k in range(2):
        write_night(tmp_path / f"s{k}", f"n{k}", seats, season={"id": "x", "format": "single"})
    board = season.leaderboard_for([tmp_path], resamples=50)
    honest = by_entry(board)[HONEST]
    assert (honest["mean_milli"], honest["seat_nights"], honest["nights"]) == (20_000, 4, 2)


def test_the_text_shows_bots_intervals_and_the_season(tmp_path: Path) -> None:
    write_rotation_season(
        tmp_path / "s",
        lambda model, deck, rotation: (
            {HONEST: 10, COLLUDE: 0, BACKSTAB: -9}[model] * (1 if deck == "d1" else -1)
        ),
        bot=lambda deck, rotation: 5,
    )
    text = season.leaderboard_text(season.leaderboard_for([tmp_path / "s"]))
    lines = text.splitlines()
    assert lines[0] == "season syn: rotation, 6 nights counted, 2 decks"
    assert lines[1] == "ranked by mean surplus per night, mission bonus not included"
    assert lines[2].startswith("rank: 95% interval over 10000 resamples of 2 decks")
    assert "老实人 [bot]" in text and "–" in text  # a bot, and an interval such as 2-4
    assert "mean surplus [95% CI]" in text and "missions (the bonus" in text


def test_the_text_of_a_single_night_says_the_season_is_assumed(tmp_path: Path) -> None:
    write_single_season(tmp_path / "s", [{HONEST: 1, COLLUDE: 2, BACKSTAB: 3}])
    # no configuration: models come from a cassette
    for path in (tmp_path / "s").iterdir():
        (path / "config.yaml").unlink()
        write_calls(path, {1: "m-a", 2: "m-b", 3: "m-c"})
    board = season.leaderboard_for([tmp_path / "s"])
    assert board["season"]["format_known"] is False and board["season"]["id"] is None
    text = season.leaderboard_text(board)
    assert "1 night counted" in text.splitlines()[0]
    assert "no config.yaml in the nights: the season is taken as single-night" in text
    assert "rank intervals need at least two nights" in text


def test_text_columns_line_up_by_display_width() -> None:
    lines = season._table(["entry", "n"], [["老实人", "1"], ["abcdef", "22"]], "lr")
    assert lines == ["  entry    n", "  老实人   1", "  abcdef  22"]
    assert [season._width(line) for line in lines] == [12, 12, 12]
