"""The footer: the smallest difference between models that the nights played can tell apart."""

import math
import statistics
from collections.abc import Mapping, Sequence
from itertools import combinations
from pathlib import Path
from statistics import NormalDist
from typing import Any

from season_support import write_rotation_season, write_single_season

from auction_night import season

MODELS = ("mock-honest", "mock-collude", "mock-backstab")


def expected_gap_milli(unit_scores: Mapping[str, Sequence[float]]) -> tuple[int, int]:
    """``(sigma, gap)`` in thousandths, from the formula of the module text and the standard
    library only: the pooled standard deviation of the paired differences between models over
    the units, times (z at 0.975 plus z at 0.80), divided by the square root of the units."""
    normal = NormalDist()
    z = normal.inv_cdf(0.975) + normal.inv_cdf(0.80)
    units = len(next(iter(unit_scores.values())))
    variances = [
        statistics.variance([a - b for a, b in zip(unit_scores[x], unit_scores[y], strict=True)])
        for x, y in combinations(MODELS, 2)
    ]
    sigma = math.sqrt(sum(variances) / len(variances))
    return round(sigma * 1000), round(z * sigma / math.sqrt(units) * 1000)


def close(actual: int | None, expected: int) -> bool:
    return actual is not None and abs(actual - expected) <= 1  # float noise in the last place


def test_the_z_values_are_the_normal_quantiles_of_the_stated_levels() -> None:
    normal = NormalDist()
    assert season.ALPHA_PER_MILLE == 50 and season.POWER_PER_MILLE == 800
    assert abs(season.Z_ALPHA - normal.inv_cdf(1 - season.ALPHA_PER_MILLE / 2000)) < 1e-12
    assert abs(season.Z_POWER - normal.inv_cdf(season.POWER_PER_MILLE / 1000)) < 1e-12


def deck_scores() -> dict[str, dict[str, int]]:
    """Six decks: what each model scored on every night of the deck."""
    table = {}
    state = 99
    for deck in ("d1", "d2", "d3", "d4", "d5", "d6"):
        row = {}
        for model in MODELS:
            state = (state * 1103515245 + 12345) % 2**31
            row[model] = state % 120 - 60
        table[deck] = row
    return table


def test_the_gap_of_a_rotation_season_counts_decks(tmp_path: Path) -> None:
    table = deck_scores()
    write_rotation_season(
        tmp_path / "s",
        lambda model, deck, rotation: table[deck][model],
        decks=tuple(table),
    )
    board = season.leaderboard_for([tmp_path / "s"], resamples=50)
    footer = board["footer"]
    per_deck = {m: [table[d][m] for d in table] for m in MODELS}  # every night of a deck is alike
    sigma, gap = expected_gap_milli(per_deck)
    assert (footer["unit"], footer["units"], footer["model_pairs"]) == ("deck", 6, 3)
    assert close(footer["sigma_milli"], sigma) and close(footer["gap_milli"], gap)
    assert board["season"]["nights"] == 18  # eighteen nights, but six units


def test_the_gap_of_a_single_night_season_counts_nights(tmp_path: Path) -> None:
    nights = [
        {m: v for m, v in zip(MODELS, row, strict=True)}
        for row in [(40, -10, 3), (-5, 22, 8), (17, 17, -30), (0, 9, 61), (-44, 5, 2), (12, 31, -7)]
    ]
    write_single_season(tmp_path / "s", nights)
    footer = season.leaderboard_for([tmp_path / "s"], resamples=50)["footer"]
    sigma, gap = expected_gap_milli({m: [night[m] for night in nights] for m in MODELS})
    assert (footer["unit"], footer["units"]) == ("night", 6)
    assert close(footer["sigma_milli"], sigma) and close(footer["gap_milli"], gap)


def test_the_bots_do_not_enter_the_standard_deviation(tmp_path: Path) -> None:
    table = deck_scores()

    def footer(bot_score: int, root: Path) -> dict[str, Any]:
        write_rotation_season(
            root,
            lambda model, deck, rotation: table[deck][model],
            decks=tuple(table),
            bot=lambda deck, rotation: bot_score * (1 if deck < "d4" else -1),
        )
        return season.leaderboard_for([root], resamples=50)["footer"]

    assert footer(0, tmp_path / "a") == footer(900, tmp_path / "b")


def test_the_text_of_the_footer_names_the_gap_in_both_languages(tmp_path: Path) -> None:
    table = deck_scores()
    write_rotation_season(
        tmp_path / "s", lambda model, deck, rotation: table[deck][model], decks=tuple(table)
    )
    board = season.leaderboard_for([tmp_path / "s"], resamples=50)
    footer = board["footer"]
    gap, sigma = f"{footer['gap_milli'] / 1000:.1f}", f"{footer['sigma_milli'] / 1000:.1f}"
    assert footer["text_zh"] == f"按现有夜数，差距小于约 {gap} 分/夜时多半分不出来。"
    assert footer["text_en"] == (
        f"With the nights played so far, differences smaller than about {gap} points per night "
        f"between models mostly cannot be told apart."
    )
    # the basis of the number is stated apart from the sentence
    assert footer["basis_en"] == (
        f"Basis: 6 decks, 80% probability at a two-sided 5% level, standard deviation of the "
        f"difference between two models {sigma} points per night."
    )
    assert (
        footer["basis_zh"]
        == f"依据：6 副牌，80% 把握，双侧 5%，两个模型之差的标准差 {sigma} 分/夜。"
    )
    lines = season.leaderboard_text(board).splitlines()
    assert lines[-4:] == [
        footer["text_en"],
        footer["basis_en"],
        footer["text_zh"],
        footer["basis_zh"],
    ]


def test_with_too_few_units_or_models_there_is_no_gap(tmp_path: Path) -> None:
    write_rotation_season(tmp_path / "one", lambda model, deck, r: 1, decks=("d1",))
    footer = season.leaderboard_for([tmp_path / "one"])["footer"]
    assert (footer["sigma_milli"], footer["gap_milli"]) == (None, None)
    assert (footer["basis_en"], footer["basis_zh"]) == (None, None)
    assert "at least two decks and two models" in footer["text_en"]
    assert (
        footer["text_zh"] == "按现有夜数，还算不出多大的差距才分得出来：至少要有两副牌和两个模型。"
    )

    write_single_season(
        tmp_path / "solo", [{"mock-honest": 1}, {"mock-honest": 5}], models=("honest",)
    )
    solo = season.leaderboard_for([tmp_path / "solo"])["footer"]
    assert solo["gap_milli"] is None and solo["model_pairs"] == 0
    assert "at least two nights and two models" in solo["text_en"]
    assert "至少要有两夜和两个模型" in solo["text_zh"]


def test_a_gap_of_zero_when_the_models_always_differ_by_the_same_amount(tmp_path: Path) -> None:
    nights = [
        {"mock-honest": 10 + k, "mock-collude": k, "mock-backstab": -10 + k} for k in range(5)
    ]
    write_single_season(tmp_path / "s", nights)
    footer = season.leaderboard_for([tmp_path / "s"], resamples=20)["footer"]
    assert footer["sigma_milli"] == 0 and footer["gap_milli"] == 0
    assert "about 0.0 points per night" in footer["text_en"]


def test_decimals_are_written_without_a_float_and_without_a_negative_zero() -> None:
    decimal = season._decimal
    assert [decimal(v, 1) for v in (0, 12_345, 12_350, 12_450, -12_350, -4, 7)] == [
        "0.0",
        "12.3",
        "12.4",
        "12.4",
        "-12.4",
        "0.0",
        "0.0",
    ]
    assert decimal(1_500, 0) == "2" and decimal(2_500, 0) == "2"
    assert season._percent(5) == "0.5%" and season._percent(1000) == "100.0%"
    assert season._percent(None) == "n/a"
