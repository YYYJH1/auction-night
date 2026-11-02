"""The simulation repeats the numbers of the calibration it was written for.

The economy of a night (budget scale 300, mission bonus 150, shill threshold 0.95, a pacing
economist) was tuned with an earlier script that played the same rules with rule bots. These
tests run the presets again through the engine and compare the new numbers with the old ones.

A reproduction cannot agree digit for digit. The cards come from another random generator, so
each side is a sample of 3000 nights with a sampling error of its own. Two differences of
convention remain, both small: the number of bidders in a Dutch lot counts only the seats that
can afford the lowest level, and the old script rounded bids in floating point where the
bots round exactly. Played on the very same cards, the old rules and the engine differ by less
than one coin in the pair differences (measured once, 4000 cards).

A number passes when it lies within ``Z`` standard errors of the reference, taking the error of
both samples together. ``Z`` is 4, so that about one faithful run in a thousand fails any of the
twenty comparisons. The reference errors come from the published intervals, or, where none was
published, from the same calibration run.
"""

import math
import os
from dataclasses import dataclass

import pytest

from auction_night.simulate import PRESETS, Interval, Report, preset_config, run_simulation

Z = 4.0
JOBS = min(4, os.cpu_count() or 1)


@dataclass(frozen=True)
class Reference:
    """A published number, the standard error of its sample and half its last digit."""

    value: float
    error: float
    rounding: float


def deviation(ours: Interval, reference: Reference) -> float:
    """How many combined standard errors our mean is from the reference."""
    assert ours.half is not None
    spread = math.hypot(ours.half / 1.96, reference.error)
    gap = max(0.0, abs(ours.mean - reference.value) - reference.rounding)
    return gap / spread


def run(preset_name: str, *, chase: bool = True, nights: int | None = None) -> Report:
    preset = PRESETS[preset_name]
    return run_simulation(
        preset_config(preset.c),
        nights=nights or preset.nights,
        seed=preset.seed,
        jobs=JOBS,
        chase=chase,
        source=preset_name,
    )


def verdict(rows: dict[str, tuple[Interval, Reference]]) -> str:
    lines = []
    for name, (ours, reference) in rows.items():
        lines.append(
            f"{name:<22} ours {ours.mean:8.3f}   reference {reference.value:8.3f}   "
            f"deviation {deviation(ours, reference):4.1f}"
        )
    return "\n".join(lines)


def assert_close(rows: dict[str, tuple[Interval, Reference]]) -> None:
    worst = max(deviation(ours, reference) for ours, reference in rows.values())
    assert worst <= Z, f"more than {Z} standard errors from the reference:\n{verdict(rows)}"


# Budget scale 300, 3000 nights, every mission holder chases its mission.
SURPLUS = {
    "E": Reference(35.5, 1.25, 0.05),
    "O": Reference(-4.5, 1.09, 0.05),
    "S": Reference(47.9, 1.37, 0.05),
    "P": Reference(62.1, 1.73, 0.05),
    "T": Reference(0.4, 1.65, 0.05),
}
SPENT_90 = {
    "E": Reference(0.44, 0.0061, 0.005),
    "O": Reference(0.61, 0.0061, 0.005),
    "S": Reference(0.12, 0.0041, 0.005),
    "P": Reference(0.32, 0.0087, 0.005),
    "T": Reference(0.73, 0.0082, 0.005),
}
PAIRS = {"P-E": Reference(26.6, 2.2, 0.05), "P-S": Reference(14.3, 2.3, 0.05)}

# The same table, read for its missions: completion rate and mean bonus per holder.
COMPLETED = {
    "collector": Reference(0.173, 0.0040, 0.0005),
    "shill": Reference(0.177, 0.0040, 0.0005),
}
BONUS = {"collector": Reference(25.9, 0.61, 0.05), "shill": Reference(26.5, 0.61, 0.05)}

# Holders that ignore their mission (1500 nights): how often the mission is completed by chance.
BY_CHANCE = {
    "collector": Reference(0.032, 0.0026, 0.0005),
    "shill": Reference(0.031, 0.0026, 0.0005),
}


@pytest.fixture(scope="module")
def s1_report() -> Report:
    return run("s1-c300")


@pytest.fixture(scope="module")
def s2_report() -> Report:
    return run("s2")


def test_budget_scale_300_repeats_the_surplus_of_every_style(s1_report: Report) -> None:
    rows = {f"surplus {row.style}": (row.surplus, SURPLUS[row.style]) for row in s1_report.styles}
    assert set(SURPLUS) == {row.style for row in s1_report.styles}
    assert_close(rows)


def test_budget_scale_300_repeats_how_often_a_style_spends_its_budget(s1_report: Report) -> None:
    rows = {
        f"spent 90% {row.style}": (row.spent_90, SPENT_90[row.style]) for row in s1_report.styles
    }
    assert_close(rows)


def test_budget_scale_300_repeats_the_paired_differences(s1_report: Report) -> None:
    rows = {
        f"{pair.first}-{pair.second}": (pair.difference, PAIRS[f"{pair.first}-{pair.second}"])
        for pair in s1_report.pairs
    }
    assert set(rows) == set(PAIRS)
    assert_close(rows)


def test_the_pacing_economist_does_better_than_both_the_textbook_and_the_timid_style(
    s1_report: Report,
) -> None:
    """The finding the pacing switch rests on: both intervals lie clear above zero."""
    for pair in s1_report.pairs:
        assert pair.difference.low is not None and pair.difference.low > 0, pair


def test_mission_completion_repeats_the_calibration(s2_report: Report) -> None:
    rows = {
        f"completed {row.kind}": (row.completed, COMPLETED[row.kind]) for row in s2_report.missions
    }
    rows |= {f"bonus {row.kind}": (row.bonus, BONUS[row.kind]) for row in s2_report.missions}
    assert set(COMPLETED) == {row.kind for row in s2_report.missions}
    assert_close(rows)


def test_both_kinds_of_mission_have_about_the_same_bonus_to_win(s2_report: Report) -> None:
    """The threshold was set so that the two kinds of mission are completed about equally often."""
    bonus = {row.kind: row.bonus for row in s2_report.missions}
    gap = abs(bonus["collector"].mean - bonus["shill"].mean)
    assert bonus["collector"].half is not None and bonus["shill"].half is not None
    assert gap < 2 * math.hypot(bonus["collector"].half, bonus["shill"].half)


def test_holders_that_ignore_their_mission_complete_it_by_chance_alone() -> None:
    report = run("s2", chase=False, nights=1500)
    rows = {
        f"by chance {row.kind}": (row.completed, BY_CHANCE[row.kind]) for row in report.missions
    }
    assert not report.chase
    assert_close(rows)
