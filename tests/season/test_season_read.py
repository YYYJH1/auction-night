"""Reading one night: who sat where, the scores, the abstentions, the missions and the status."""

from collections import Counter
from pathlib import Path

import pytest
import yaml
from season_support import (
    SeatSpec,
    play_real_night,
    real_config,
    rewrite_mode,
    rewrite_status,
    specs,
    write_calls,
    write_night,
)

from auction_night import season
from auction_night.contracts import NightConfig, dumps_events, loads_events
from auction_night.contracts.events import ActionEvent, MissionResultEvent, NightEndEvent
from auction_night.llm import nightrun


def test_the_file_names_are_those_of_the_run_command() -> None:
    assert season.EVENTS_FILE == nightrun.EVENTS_FILE
    assert season.CALLS_FILE == nightrun.CALLS_FILE
    assert season.HUMAN_FILE == nightrun.HUMAN_FILE


# ------------------------------------------------------------------------- a real night


def test_a_real_night_is_read_from_its_log_and_its_configuration(real_season: Path) -> None:
    night = season.read_night(real_season / "s1-d1-r1")
    assert (night.night, night.status, night.counts) == ("s1-d1-r1", "completed", True)
    assert (night.season_id, night.season_format) == ("s1", "rotation")
    assert (night.deck, night.rotation, night.players) == ("d1", 1, 4)
    seats = {seat.seat: seat for seat in night.seats}
    # in rotation 1 the model list has moved up by one place
    assert [seats[n].entry for n in (1, 2, 3, 4)] == [
        "mock-collude",
        "mock-backstab",
        "mock-random",
        "mock-honest",
    ]
    assert [(seats[n].entry, seats[n].label, seats[n].kind) for n in (5, 6)] == [
        ("bot:truthful", "老实人", "bot"),
        ("bot:economist", "经济学家", "bot"),
    ]
    assert [seats[n].kind for n in (1, 2, 3, 4)] == ["model"] * 4


def test_scores_missions_and_actions_agree_with_the_log(real_season: Path) -> None:
    directory = real_season / "s1-d2-r3"
    night = season.read_night(directory)
    events = loads_events((directory / "events.jsonl").read_text(encoding="utf-8"))
    end = next(e for e in events if isinstance(e, NightEndEvent))
    scores = {score.seat: score for score in end.data.scores}
    results = {e.data.seat: e.data for e in events if isinstance(e, MissionResultEvent)}
    actions = Counter(e.data.seat for e in events if isinstance(e, ActionEvent))
    for seat in night.seats:
        assert seat.surplus == scores[seat.seat].surplus
        assert seat.bonus == scores[seat.seat].mission_bonus
        assert seat.actions == actions[seat.seat]
        held = results.get(seat.seat)
        assert seat.mission == (None if held is None else held.kind)
        assert seat.mission_done == (held is not None and held.completed)
    assert sum(1 for seat in night.seats if seat.mission) == 4  # two collectors and two shills
    assert all(seat.mission is None for seat in night.seats if seat.kind == "bot")


def test_the_entry_ids_are_those_of_the_cassette(real_season: Path, tmp_path: Path) -> None:
    """A configuration without model fields is named ``mock-<policy>``, as the cassette does."""
    directory = real_season / "s1-d1-r2"
    from_config = {seat.seat: seat.entry for seat in season.read_night(directory).seats}
    bare = tmp_path / "bare"
    bare.mkdir()
    for name in ("events.jsonl", "calls.jsonl"):
        (bare / name).write_bytes((directory / name).read_bytes())
    from_calls = {seat.seat: seat.entry for seat in season.read_night(bare).seats}
    for seat in (1, 2, 3, 4):
        assert from_calls[seat] == from_config[seat]


def test_a_bare_run_directory_is_taken_as_a_single_night_season(tmp_path: Path) -> None:
    """What ``auction-night run`` writes today: the log and the cassette, no configuration."""
    raw = real_config("d1", 0)
    nightrun.run_directory(NightConfig.model_validate(raw), tmp_path / "run")
    night = season.read_night(tmp_path / "run")
    assert (night.season_id, night.season_format, night.deck, night.rotation) == (
        None,
        None,
        None,
        None,
    )
    assert [seat.entry for seat in night.seats] == [
        "mock-honest",
        "mock-collude",
        "mock-backstab",
        "mock-random",
        "bot:老实人",
        "bot:经济学家",
    ]


def test_a_seat_that_abstains_is_counted_by_reason(tmp_path: Path) -> None:
    raw = real_config("d1", 0)
    raw["seats"][2]["mock_policy"] = "abstain"
    play_real_night(tmp_path / "n", raw)
    night = season.read_night(tmp_path / "n")
    events = loads_events((tmp_path / "n" / "events.jsonl").read_text(encoding="utf-8"))
    reasons: Counter[str] = Counter()
    for event in events:
        if isinstance(event, ActionEvent) and event.data.seat == 3:
            if event.data.status == "abstain":
                reasons[event.data.reason or ""] += 1
            elif event.data.status == "kept_previous":
                reasons["kept_previous"] += 1
    seat = next(s for s in night.seats if s.seat == 3)
    assert reasons["missing"] > 0
    assert {k: v for k, v in seat.abstentions.items() if v} == dict(reasons)
    assert seat.entry == "mock-abstain"
    assert all(sum(s.abstentions.values()) == 0 for s in night.seats if s.seat != 3)


# ----------------------------------------------------------------- a night written by hand


def hand_seats(**changes: object) -> list[SeatSpec]:
    seats = specs(0, lambda model: 10)
    return [SeatSpec(**{**vars(seat), **changes}) if seat.seat == 1 else seat for seat in seats]


def test_actions_abstentions_and_kept_prices_are_counted(tmp_path: Path) -> None:
    seats = hand_seats(
        actions=4, abstain={"missing": 2, "refusal": 1, "timeout": 1, "not_asked": 3}, kept=2
    )
    directory = write_night(tmp_path / "n", "n", seats)
    first = season.read_night(directory).seats[0]
    # a seat that was never asked is not an action; a kept English exit price is an abstention
    assert first.actions == 4 + 2 + 1 + 1 + 2
    assert first.abstentions == {
        "missing": 2,
        "parse_failed": 0,
        "timeout": 1,
        "refusal": 1,
        "budget_aborted": 0,
        "not_asked": 3,
        "kept_previous": 2,
    }
    assert tuple(first.abstentions) == season.ABSTAIN_REASONS


def test_a_mission_is_read_from_mission_result(tmp_path: Path) -> None:
    seats = hand_seats(mission="collector", done=True, bonus=150)
    directory = write_night(tmp_path / "n", "n", seats)
    first = season.read_night(directory).seats[0]
    assert (first.mission, first.mission_done, first.bonus) == ("collector", True, 150)
    assert first.surplus == 10


def test_the_model_is_read_from_the_cassette_without_a_configuration(tmp_path: Path) -> None:
    directory = write_night(tmp_path / "n", "n", hand_seats(), config=False)
    write_calls(directory, {1: "model-a", 2: "model-b", 3: "model-c"})
    night = season.read_night(directory)
    assert [(s.entry, s.kind) for s in night.seats] == [
        ("model-a", "model"),
        ("model-b", "model"),
        ("model-c", "model"),
        ("bot:老实人", "bot"),
    ]


def test_a_night_without_configuration_or_cassette_says_what_to_add(tmp_path: Path) -> None:
    directory = write_night(tmp_path / "n", "n", hand_seats(), config=False)
    with pytest.raises(season.SeasonError, match="which model sat at seat 1.*config.yaml"):
        season.read_night(directory)
    write_calls(directory, {1: "model-a", 3: "model-c"})
    with pytest.raises(season.SeasonError, match="which model sat at seat 2"):
        season.read_night(directory)


def test_the_deck_is_found_in_the_seed_commitment_of_the_log(tmp_path: Path) -> None:
    directory = write_night(tmp_path / "n", "n", hand_seats(), config=False, log_deck="deck-9")
    write_calls(directory, {1: "a", 2: "b", 3: "c"})
    assert season.read_night(directory).deck == "deck-9"


def test_a_log_that_names_two_decks_is_refused(tmp_path: Path) -> None:
    directory = write_night(tmp_path / "n", "n", hand_seats(), config=False, log_deck="d1")
    write_calls(directory, {1: "a", 2: "b", 3: "c"})
    path = directory / "events.jsonl"
    events = loads_events(path.read_text(encoding="utf-8"))
    commit = events[1]
    other = commit.model_copy(update={"seq": len(events) + 1})
    other = other.model_copy(update={"data": commit.data.model_copy(update={"deck_id": "d2"})})
    path.write_text(dumps_events([*events, other]), encoding="utf-8")
    with pytest.raises(season.SeasonError, match="more than one deck"):
        season.read_night(directory)


def test_the_deck_of_the_configuration_must_be_the_deck_of_the_log(tmp_path: Path) -> None:
    rotation = {"id": "s", "format": "rotation", "deck": "d1", "rotation": 0}
    directory = write_night(tmp_path / "n", "n", hand_seats(), season=rotation, log_deck="d2")
    with pytest.raises(season.SeasonError, match="different decks"):
        season.read_night(directory)


# ------------------------------------------------------------------------ which nights count


def test_only_a_completed_night_without_a_person_counts(tmp_path: Path) -> None:
    done = write_night(tmp_path / "done", "done", hand_seats())
    assert season.read_night(done).exclusion is None

    aborted = write_night(tmp_path / "aborted", "aborted", hand_seats())
    rewrite_status(aborted, "budget_aborted")
    night = season.read_night(aborted)
    assert (night.exclusion, night.counts, night.seats) == ("budget_aborted", False, ())

    person = write_night(tmp_path / "person", "person", hand_seats())
    (person / "human.jsonl").write_text("", encoding="utf-8")
    assert season.read_night(person).exclusion == "human"

    unfinished = write_night(tmp_path / "unfinished", "unfinished", hand_seats())
    lines = (unfinished / "events.jsonl").read_text(encoding="utf-8").splitlines(keepends=True)
    (unfinished / "events.jsonl").write_text("".join(lines[:-1]), encoding="utf-8")
    assert season.read_night(unfinished).exclusion == "no_night_end"


def test_a_person_in_the_configuration_keeps_the_night_out(tmp_path: Path) -> None:
    directory = write_night(tmp_path / "n", "n", hand_seats())
    raw = yaml.safe_load((directory / "config.yaml").read_text(encoding="utf-8"))
    raw["seats"][0] = {"seat": 1, "codename": raw["seats"][0]["codename"], "kind": "human"}
    (directory / "config.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True))
    assert season.read_night(directory).exclusion == "human"


def test_a_night_in_variety_mode_is_left_out_whatever_its_status(tmp_path: Path) -> None:
    directory = write_night(tmp_path / "n", "n", hand_seats())
    rewrite_mode(directory, "variety")
    night = season.read_night(directory)
    assert (night.exclusion, night.counts, night.seats) == ("variety", False, ())
    rewrite_status(directory, "budget_aborted")
    assert season.read_night(directory).exclusion == "variety"
    (directory / "human.jsonl").write_text("", encoding="utf-8")
    assert season.read_night(directory).exclusion == "human"  # a person comes first


def test_the_status_is_the_one_in_night_end_whatever_the_actions_say(tmp_path: Path) -> None:
    """A skipped English revision leaves no reason in the log, so only night_end can tell."""
    seats = hand_seats(abstain={"budget_aborted": 3})
    directory = write_night(tmp_path / "n", "n", seats)
    assert season.read_night(directory).counts  # actions that say budget_aborted do not exclude
    rewrite_status(directory, "budget_aborted")
    assert not season.read_night(directory).counts


# ------------------------------------------------------------------------ files that are wrong


def test_an_invalid_line_is_reported_with_its_number(tmp_path: Path) -> None:
    directory = write_night(tmp_path / "n", "n", hand_seats())
    lines = (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()
    lines[2] = '{"seq": 3, "type": "action"}'
    (directory / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(season.SeasonError, match=r"events.jsonl: line 3 is not a valid event"):
        season.read_night(directory)
    lines[2] = "not json"
    (directory / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(season.SeasonError, match=r"line 3 is not a valid event"):
        season.read_night(directory)


def test_a_log_with_a_gap_in_seq_is_refused(tmp_path: Path) -> None:
    directory = write_night(tmp_path / "n", "n", hand_seats())
    lines = (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()
    (directory / "events.jsonl").write_text("\n".join(lines[:1] + lines[2:]) + "\n")
    with pytest.raises(season.SeasonError, match="has seq 3"):
        season.read_night(directory)


def test_a_directory_without_a_log_is_refused(tmp_path: Path) -> None:
    with pytest.raises(season.SeasonError, match="cannot read"):
        season.read_night(tmp_path)


def test_a_configuration_of_another_night_is_refused(tmp_path: Path) -> None:
    one = write_night(tmp_path / "one", "one", hand_seats())
    two = write_night(tmp_path / "two", "two", hand_seats())
    (one / "config.yaml").write_bytes((two / "config.yaml").read_bytes())
    with pytest.raises(season.SeasonError, match="configuration of night 'two'.*log is of night"):
        season.read_night(one)


def test_a_configuration_with_other_seats_is_refused(tmp_path: Path) -> None:
    one = write_night(tmp_path / "one", "one", hand_seats())
    seats = hand_seats()
    renamed = [SeatSpec(**{**vars(s), "codename": s.codename + "乙"}) for s in seats]
    other = write_night(tmp_path / "other", "one", renamed)
    (one / "config.yaml").write_bytes((other / "config.yaml").read_bytes())
    with pytest.raises(season.SeasonError, match="does not describe the seats"):
        season.read_night(one)


def test_a_broken_configuration_is_reported(tmp_path: Path) -> None:
    directory = write_night(tmp_path / "n", "n", hand_seats())
    (directory / "config.yaml").write_text("night: [", encoding="utf-8")
    with pytest.raises(season.SeasonError, match="cannot read .*config.yaml"):
        season.read_night(directory)


def test_a_bonus_that_differs_between_mission_result_and_night_end_is_refused(
    tmp_path: Path,
) -> None:
    seats = hand_seats(mission="collector", done=True, bonus=150)
    directory = write_night(tmp_path / "n", "n", seats)
    path = directory / "events.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    end = next(i for i, line in enumerate(lines) if '"type":"night_end"' in line)
    assert lines[end].count('"mission_bonus":150') == 1
    lines[end] = lines[end].replace('"mission_bonus":150', '"mission_bonus":0')
    lines[end] = lines[end].replace('"total":160', '"total":10')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(season.SeasonError, match="bonus of seat 1 differs"):
        season.read_night(directory)


def test_discovery_finds_nights_under_a_season_directory(real_season: Path, tmp_path: Path) -> None:
    found = season.discover_night_dirs([real_season])
    assert [path.name for path in found] == sorted(p.name for p in real_season.iterdir())
    one = real_season / "s1-d1-r0"
    assert season.discover_night_dirs([one, real_season, one]) == found  # each night once
    with pytest.raises(season.SeasonError, match="not a directory"):
        season.discover_night_dirs([tmp_path / "missing"])
    (tmp_path / "empty").mkdir()
    with pytest.raises(season.SeasonError, match="no events.jsonl in or under"):
        season.discover_night_dirs([tmp_path / "empty"])
