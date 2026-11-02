"""Nights that cannot form one season are refused with a message that says why, and the
reference-column interface that the theory module and the show layer plug into."""

from pathlib import Path

import pytest
from season_support import (
    SeatSpec,
    specs,
    write_calls,
    write_night,
)

from auction_night import season
from auction_night.contracts.events import ActionEvent

SINGLE = {"id": "one", "format": "single"}


def one_night(root: Path, name: str, season_config: dict[str, object] | None = SINGLE) -> Path:
    return write_night(root / name, name, specs(0, lambda model: 5), season=season_config)


def test_nights_of_two_seasons_are_refused(tmp_path: Path) -> None:
    one_night(tmp_path, "a", {"id": "spring", "format": "single"})
    one_night(tmp_path, "b", {"id": "autumn", "format": "single"})
    with pytest.raises(
        season.SeasonError, match="do not agree on the season id: 'autumn', 'spring'"
    ):
        season.leaderboard_for([tmp_path])


def test_a_single_night_and_a_rotation_night_do_not_mix(tmp_path: Path) -> None:
    one_night(tmp_path, "a")
    rotation = {"id": "one", "format": "rotation", "deck": "d1", "rotation": 0}
    one_night(tmp_path, "b", rotation)
    with pytest.raises(season.SeasonError, match="do not agree on the season format"):
        season.leaderboard_for([tmp_path])


def test_some_nights_with_a_configuration_and_some_without_are_refused(tmp_path: Path) -> None:
    one_night(tmp_path, "a")
    bare = write_night(tmp_path / "b", "b", specs(0, lambda model: 5), config=False)
    write_calls(bare, {1: "x", 2: "y", 3: "z"})
    with pytest.raises(season.SeasonError, match="night 'b' names no season id while other"):
        season.leaderboard_for([tmp_path])


def test_a_changed_lineup_is_refused(tmp_path: Path) -> None:
    one_night(tmp_path, "a")
    seats = specs(0, lambda model: 5, models=("honest", "collude", "random"))
    write_night(tmp_path / "b", "b", seats, season=SINGLE)
    with pytest.raises(season.SeasonError, match=r"lineup changes: night 'a' differs in .*random"):
        season.leaderboard_for([tmp_path])


def test_the_same_night_twice_is_refused(tmp_path: Path) -> None:
    one_night(tmp_path, "a")
    one_night(tmp_path / "copy", "a")
    with pytest.raises(season.SeasonError, match="night 'a' appears more than once"):
        season.leaderboard_for([tmp_path])


def test_a_rotation_night_needs_its_deck_and_its_rotation(tmp_path: Path) -> None:
    no_deck = {"id": "s", "format": "rotation", "rotation": 0}
    one_night(tmp_path / "x", "a", no_deck)
    with pytest.raises(season.SeasonError, match="night 'a' names no deck"):
        season.leaderboard_for([tmp_path / "x"])


def test_two_nights_of_a_deck_cannot_share_a_rotation(tmp_path: Path) -> None:
    for name in ("a", "b"):
        one_night(tmp_path, name, {"id": "s", "format": "rotation", "deck": "d1", "rotation": 0})
    with pytest.raises(season.SeasonError, match="deck 'd1' has two nights with the same rotation"):
        season.leaderboard_for([tmp_path])


def test_nights_that_share_a_deck_without_a_configuration_are_not_taken_as_single(
    tmp_path: Path,
) -> None:
    for name in ("a", "b"):
        directory = write_night(
            tmp_path / name, name, specs(0, lambda model: 5), config=False, log_deck="d1"
        )
        write_calls(directory, {1: "x", 2: "y", 3: "z"})
    with pytest.raises(season.SeasonError, match="several nights share a deck.*config.yaml"):
        season.leaderboard_for([tmp_path])


def test_a_log_that_is_not_a_night_is_reported_with_its_path(tmp_path: Path) -> None:
    directory = tmp_path / "x"
    directory.mkdir()
    (directory / "events.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(season.SeasonError, match=r"x/events.jsonl: line 1 is not a valid event"):
        season.leaderboard_for([directory])


# -------------------------------------------------------------------------- reference columns


def abstentions_column() -> season.ReferenceColumn:
    """A toy column: the number of actions each seat took, which the log can count."""

    def compute(events):  # type: ignore[no-untyped-def]
        taken: dict[int, int] = {}
        for event in events:
            if isinstance(event, ActionEvent):
                taken[event.data.seat] = taken.get(event.data.seat, 0) + 1
        return taken

    return season.ReferenceColumn("actions_taken", "actions", compute)


def test_a_reference_column_is_averaged_per_seat_and_night(tmp_path: Path) -> None:
    # two nights; the first seat takes 4 actions on one night and 6 on the other
    for k, actions in enumerate((4, 6)):
        seats = specs(0, lambda model: 1)
        seats[0] = SeatSpec(**{**vars(seats[0]), "actions": actions})
        write_night(tmp_path / "t" / f"n{k}", f"n{k}", seats, season=SINGLE)
    column = abstentions_column()
    board = season.leaderboard_for([tmp_path / "t"], resamples=20, columns=[column])
    assert board["columns"] == [{"name": "actions_taken", "label": "actions"}]
    rows = {row["entry"]: row for row in board["entries"]}
    assert rows["mock-honest"]["columns"] == {"actions_taken": 5000}  # (4 + 6) / 2, in thousandths
    assert rows["mock-collude"]["columns"] == {"actions_taken": 4000}
    text = season.leaderboard_text(board)
    header = next(line for line in text.splitlines() if "mean surplus [95% CI]" in line)
    assert header.endswith("actions") and "5.0" in text


def test_a_column_has_to_cover_every_seat(tmp_path: Path) -> None:
    one_night(tmp_path, "a")
    short = season.ReferenceColumn("c", "c", lambda events: {1: 1})
    with pytest.raises(season.SeasonError, match="column 'c' gave no value for every seat"):
        season.leaderboard_for([tmp_path], columns=[short])


def test_a_board_cannot_name_a_column_the_nights_were_not_read_with(tmp_path: Path) -> None:
    one_night(tmp_path, "a")
    nights = [season.read_night(tmp_path / "a")]
    with pytest.raises(season.SeasonError, match="read without the column 'actions_taken'"):
        season.build_leaderboard(nights, columns=[abstentions_column()])
