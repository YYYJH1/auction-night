"""The simulation driver: who sits where, a played night, the statistics and the report."""

import json
import math
import statistics
from collections import Counter

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from auction_night.bots import ChaserBot, EconomistBot, StyleBot, TruthfulBot
from auction_night.contracts import NightConfig
from auction_night.engine.deal import deal
from auction_night.simulate import (
    PRESETS,
    Interval,
    NightResult,
    SeatResult,
    SimulationError,
    format_report,
    get_preset,
    interval,
    night_seed,
    play_night,
    play_nights,
    preset_config,
    run_simulation,
    seat_table,
    summarize,
)

# ------------------------------------------------------------------------------ presets


def test_the_presets_are_named_after_the_runs_they_repeat() -> None:
    assert sorted(PRESETS) == ["s1-c300", "s2"]
    for name, preset in PRESETS.items():
        assert preset.name == name and preset.c == 300 and preset.nights == 3000
    assert PRESETS["s1-c300"].seed != PRESETS["s2"].seed


def test_an_unknown_preset_lists_the_known_ones() -> None:
    with pytest.raises(SimulationError, match="s1-c300, s2"):
        get_preset("s3")


def test_the_preset_table_has_six_model_seats_and_two_bots() -> None:
    config = preset_config(300)
    kinds = [(seat.seat, seat.kind, seat.policy, seat.pacing) for seat in config.seats]
    assert kinds[:6] == [(n, "mock", None, False) for n in range(1, 7)]
    assert kinds[6:] == [(7, "bot", "truthful", False), (8, "bot", "economist", True)]
    night = config.night
    assert night.budget.c == 300 and night.n_lots == 12
    assert (night.missions.collectors, night.missions.shills, night.missions.bonus) == (3, 3, 150)
    assert night.missions.shill_threshold == 0.95
    assert preset_config(450).night.budget.c == 450


# ---------------------------------------------------------------------------- the table


def table_of(seed: str, *, chase: bool = True):
    config = preset_config(300)
    the_deal = deal(config, seed=seed)
    tags = tuple(lot.tag for lot in the_deal.lots)
    return seat_table(config, seed, tags, the_deal.missions if chase else None), the_deal


def test_the_table_has_two_of_each_style_and_the_two_bots() -> None:
    for k in range(10):
        (bots, labels), _ = table_of(f"table:{k}")
        assert sorted(labels.values()) == ["E", "E", "O", "O", "P", "S", "S", "T"]
        assert labels[7] == "T" and labels[8] == "P"
        assert isinstance(bots[7], TruthfulBot)
        assert isinstance(bots[8], EconomistBot) and bots[8].pacing


def test_the_styles_are_dealt_at_random_to_the_model_seats() -> None:
    seen: dict[int, set[str]] = {seat: set() for seat in range(1, 7)}
    orders = set()
    for k in range(40):
        (_, labels), _ = table_of(f"shuffle:{k}")
        orders.add(tuple(labels[seat] for seat in range(1, 7)))
        for seat in range(1, 7):
            seen[seat].add(labels[seat])
    assert all(styles == {"E", "O", "S"} for styles in seen.values())
    assert len(orders) > 10


def test_the_dealing_of_styles_is_a_function_of_the_seed() -> None:
    (_, first), _ = table_of("same")
    (_, second), _ = table_of("same")
    assert first == second


def test_only_the_model_seats_that_hold_a_mission_chase_it() -> None:
    (bots, _), the_deal = table_of("chasers:1")
    chasers = {seat for seat, bot in bots.items() if isinstance(bot, ChaserBot)}
    assert chasers == set(the_deal.missions) and len(chasers) == 6
    for seat, mission in the_deal.missions.items():
        bot = bots[seat]
        assert isinstance(bot, ChaserBot) and bot.mission == mission
        assert bot.tags == tuple(lot.tag for lot in the_deal.lots)
    assert all(not isinstance(bots[seat], ChaserBot) for seat in (7, 8))


def test_without_chasing_the_model_seats_only_bid_by_style() -> None:
    (bots, _), _ = table_of("chasers:2", chase=False)
    assert all(type(bots[seat]) is StyleBot for seat in range(1, 7))


def test_any_number_of_model_seats_gets_the_styles_in_turn() -> None:
    raw = preset_config(300).model_dump(mode="json")
    raw["seats"] = [
        *raw["seats"][:4],
        {"seat": 5, "codename": "老实人", "kind": "bot", "policy": "truthful"},
        {"seat": 6, "codename": "经济", "kind": "bot", "policy": "economist"},
    ]
    raw["night"]["missions"].update(collectors=2, shills=2)
    config = NightConfig.model_validate(raw)
    the_deal = deal(config, seed="four")
    _, labels = seat_table(config, "four", (), None)
    assert sorted(labels[seat] for seat in range(1, 5)) == ["E", "E", "O", "S"]
    assert labels[5] == "T" and labels[6] == "E"  # an economist that does not pace is an E
    assert len(the_deal.missions) == 4


# ----------------------------------------------------------------------------- a night


def test_a_played_night_is_complete_and_its_books_are_consistent() -> None:
    config = preset_config(300)
    for k in range(12):
        seed = night_seed("night", k)
        result = play_night(config, seed)
        assert result.seed == seed
        assert [r.seat for r in result.seats] == list(range(1, 9))
        assert Counter(r.style for r in result.seats) == Counter(
            {"E": 2, "O": 2, "S": 2, "P": 1, "T": 1}
        )
        the_deal = deal(config, seed=seed)
        for r in result.seats:
            assert r.budget == the_deal.budgets[r.seat]
            assert 0 <= r.spent <= r.budget
            assert r.bonus == (150 if r.completed else 0)
        held = [r for r in result.seats if r.mission is not None]
        assert sorted(r.mission for r in held) == ["collector"] * 3 + ["shill"] * 3
        assert {r.seat for r in held} <= set(range(1, 7))
        assert all(r.mission is None and not r.completed for r in result.seats if r.seat > 6)


def test_the_same_seed_gives_the_same_night() -> None:
    config = preset_config(300)
    assert play_night(config, "again:1") == play_night(config, "again:1")
    assert play_night(config, "again:1") != play_night(config, "again:2")
    assert play_night(config, "again:1", chase=False) != play_night(config, "again:1")


def test_chasing_raises_the_completion_of_both_missions() -> None:
    config = preset_config(300)
    seeds = [night_seed("chase", k) for k in range(40)]
    done = {}
    for chase in (True, False):
        counts: Counter[str] = Counter()
        for result in play_nights(config, seeds, chase=chase):
            for r in result.seats:
                if r.mission is not None and r.completed:
                    counts[r.mission] += 1
        done[chase] = counts
    for kind in ("collector", "shill"):
        assert done[True][kind] > 2 * done[False][kind]


def test_a_budget_spent_to_the_last_coin_counts_as_spending_most_of_it() -> None:
    def seat(spent: int, budget: int) -> SeatResult:
        return SeatResult(1, "E", budget, 0, spent, None, False, 0)

    assert seat(270, 300).spent_most and seat(300, 300).spent_most
    assert not seat(269, 300).spent_most
    assert seat(9, 10).spent_most and not seat(8, 10).spent_most


# ------------------------------------------------------------------------- processes


def test_the_number_of_processes_does_not_change_the_result() -> None:
    config = preset_config(300)
    seeds = [night_seed("par", k) for k in range(7)]
    assert play_nights(config, seeds, jobs=2) == play_nights(config, seeds, jobs=1)


def test_jobs_must_be_positive() -> None:
    with pytest.raises(SimulationError, match="jobs"):
        play_nights(preset_config(300), ["x:1"], jobs=0)


# ----------------------------------------------------------------------- statistics


def test_an_interval_is_the_mean_and_196_standard_errors() -> None:
    values = [3.0, 7.0, 8.0, 12.0, 20.0]
    got = interval(values)
    assert got.mean == statistics.mean(values) == 10.0
    assert got.half == pytest.approx(1.96 * statistics.stdev(values) / math.sqrt(5))
    assert got.low == pytest.approx(got.mean - got.half)
    assert got.high == pytest.approx(got.mean + got.half)


def test_one_night_has_a_mean_and_no_interval() -> None:
    got = interval([4.5])
    assert got == Interval(4.5, None)
    assert got.low is None and got.high is None
    assert got.to_dict() == {"mean": 4.5, "half": None, "low": None, "high": None}


def seat_of(seat, style, surplus, spent=0, budget=300, mission=None, done=False) -> SeatResult:
    return SeatResult(seat, style, budget, surplus, spent, mission, done, 150 if done else 0)


NIGHTS = (
    NightResult(
        "a:0",
        (
            seat_of(1, "E", 10, spent=270),
            seat_of(2, "E", 30, spent=100, mission="collector", done=True),
            seat_of(3, "S", 40, mission="shill"),
            seat_of(4, "P", 60, spent=300),
            seat_of(5, "T", 0),
        ),
    ),
    NightResult(
        "a:1",
        (
            seat_of(1, "E", 0),
            seat_of(2, "E", 20, mission="collector"),
            seat_of(3, "S", 20, mission="shill", done=True),
            seat_of(4, "P", 20),
            seat_of(5, "T", 10),
        ),
    ),
)


def test_the_report_averages_over_the_seats_of_a_style_and_then_over_nights() -> None:
    report = summarize(NIGHTS, source="test", seed="a", chase=True)
    rows = {row.style: row for row in report.styles}
    assert list(rows) == ["E", "S", "P", "T"]
    assert rows["E"].seats == 2 and rows["P"].seats == 1
    assert rows["E"].surplus.mean == 15.0  # nights: (10 + 30) / 2 and (0 + 20) / 2
    assert rows["E"].surplus.half == pytest.approx(1.96 * statistics.stdev([20, 10]) / math.sqrt(2))
    assert rows["E"].spent_90.mean == 0.25  # one of two seats on the first night, none on the next
    assert rows["P"].spent_90.mean == 0.5
    assert rows["T"].surplus.mean == 5.0


def test_the_paired_differences_are_taken_within_a_night() -> None:
    report = summarize(NIGHTS, source="test", seed="a", chase=True)
    pairs = {(p.first, p.second): p.difference for p in report.pairs}
    assert list(pairs) == [("P", "E"), ("P", "S")]
    assert pairs[("P", "E")].mean == 25.0  # (60 - 20) and (20 - 10)
    assert pairs[("P", "E")].half == pytest.approx(1.96 * statistics.stdev([40, 10]) / math.sqrt(2))
    assert pairs[("P", "S")].mean == 10.0  # (60 - 40) and (20 - 20)


def test_a_pair_needs_both_styles() -> None:
    without_s = tuple(
        NightResult(n.seed, tuple(s for s in n.seats if s.style != "S")) for n in NIGHTS
    )
    report = summarize(without_s, source="test", seed="a", chase=True)
    assert [(p.first, p.second) for p in report.pairs] == [("P", "E")]


def test_the_report_counts_the_missions_by_kind() -> None:
    report = summarize(NIGHTS, source="test", seed="a", chase=True)
    rows = {row.kind: row for row in report.missions}
    assert list(rows) == ["collector", "shill"]
    assert rows["collector"].holders == 2 and rows["shill"].holders == 2
    assert rows["collector"].completed.mean == 0.5 and rows["collector"].bonus.mean == 75.0
    assert rows["shill"].completed.mean == 0.5 and rows["shill"].bonus.mean == 75.0
    assert rows["shill"].bonus.half == pytest.approx(
        1.96 * statistics.stdev([0, 150]) / math.sqrt(2)
    )


def test_a_table_without_missions_or_pairs_has_none_in_the_report() -> None:
    bare = (NightResult("b:0", (seat_of(1, "E", 5), seat_of(2, "O", 7))),)
    report = summarize(bare, source="test", seed="b", chase=False)
    assert report.pairs == () and report.missions == ()
    assert [row.style for row in report.styles] == ["E", "O"]
    assert report.styles[0].surplus == Interval(5.0, None)


def test_nothing_to_summarize_is_an_error() -> None:
    with pytest.raises(SimulationError, match="no nights"):
        summarize((), source="test", seed="a", chase=True)


# ---------------------------------------------------------------------------- the report


def test_the_report_as_json_has_the_numbers_of_the_tables() -> None:
    report = summarize(NIGHTS, source="test", seed="a", chase=False)
    data = json.loads(report.to_json())
    assert (data["source"], data["seed"], data["nights"], data["chase"]) == ("test", "a", 2, False)
    assert list(data["styles"]) == ["E", "S", "P", "T"]
    assert data["styles"]["E"]["surplus"]["mean"] == 15.0
    assert data["styles"]["E"]["seats"] == 2
    assert data["pairs"]["P-E"]["mean"] == 25.0
    assert data["missions"]["collector"]["completed"]["mean"] == 0.5
    assert data == report.to_dict()


def test_the_report_as_text_names_the_run_and_every_row() -> None:
    text = format_report(summarize(NIGHTS, source="test", seed="a", chase=True))
    lines = text.splitlines()
    assert lines[0] == "test: 2 nights, seed a, missions chased"
    assert any(line.startswith("E ") and "15.0" in line for line in lines)
    assert any(line.startswith("P-E") and "+25.0" in line for line in lines)
    legend = "E textbook bid, O 5/4 of it, S 7/10 of it, P economist with pacing, T truthful bot"
    assert lines[lines.index(legend) - 1].startswith("T ")  # the legend closes the style table
    assert any(line.startswith("collector") and "75.0" in line for line in lines)
    ignored = format_report(summarize(NIGHTS, source="test", seed="a", chase=False))
    assert ignored.splitlines()[0].endswith("missions ignored")


def test_a_single_night_prints_without_an_interval() -> None:
    text = format_report(summarize(NIGHTS[:1], source="test", seed="a", chase=True))
    assert "n/a" in text


def test_nothing_in_the_text_report_adds_up_the_rows() -> None:
    text = format_report(summarize(NIGHTS, source="test", seed="a", chase=True))
    for word in ("total", "sum", "ratio", "overall"):
        assert word not in text.lower()


# ------------------------------------------------------------------------ whole runs


def test_a_run_is_reproducible_and_independent_of_its_neighbours() -> None:
    config = preset_config(300)
    first = run_simulation(config, nights=5, seed="run")
    again = run_simulation(config, nights=5, seed="run")
    other = run_simulation(config, nights=5, seed="other")
    assert first.to_dict() == again.to_dict() != other.to_dict()
    assert first.nights == 5 and first.source == config.night.id
    assert run_simulation(config, nights=2, seed="run", source="named").source == "named"
    longer = play_nights(config, [night_seed("run", k) for k in range(8)])
    assert longer[:5] == play_nights(config, [night_seed("run", k) for k in range(5)])


def test_a_table_of_bots_only_can_be_simulated() -> None:
    raw = preset_config(300).model_dump(mode="json")
    raw["seats"] = [
        {"seat": 1, "codename": "甲", "kind": "bot", "policy": "truthful"},
        {"seat": 2, "codename": "乙", "kind": "bot", "policy": "economist", "pacing": True},
        {"seat": 3, "codename": "丙", "kind": "bot", "policy": "economist"},
    ]
    raw["night"]["missions"].update(collectors=0, shills=0)
    report = run_simulation(NightConfig.model_validate(raw), nights=6, seed="bots")
    assert [row.style for row in report.styles] == ["E", "P", "T"]
    assert [(p.first, p.second) for p in report.pairs] == [("P", "E")]
    assert report.missions == ()


@pytest.mark.parametrize("nights", [0, -3])
def test_a_run_needs_at_least_one_night(nights: int) -> None:
    with pytest.raises(SimulationError, match="at least 1"):
        run_simulation(preset_config(300), nights=nights, seed="x")


@pytest.mark.parametrize("seed", ["", "two words", "naïve", "a/b", "x" * 128])
def test_a_seed_the_deal_would_refuse_is_refused_before_any_night_is_played(seed: str) -> None:
    with pytest.raises(SimulationError, match="seed"):
        run_simulation(preset_config(300), nights=1, seed=seed)


def test_the_longest_seed_that_fits_is_accepted() -> None:
    report = run_simulation(preset_config(300), nights=1, seed="x" * 126)
    assert report.nights == 1


@given(st.integers(0, 10**9), st.sampled_from([60, 150, 300, 900]), st.booleans())
@settings(max_examples=30, deadline=None)
def test_whatever_the_cards_the_books_of_a_night_are_lawful(seed: int, c: int, chase: bool) -> None:
    result = play_night(preset_config(c), f"law:{seed}", chase=chase)
    assert len(result.seats) == 8
    for r in result.seats:
        assert 0 <= r.spent <= r.budget
        assert r.bonus == (150 if r.completed else 0)
        assert r.mission is not None or not r.completed
