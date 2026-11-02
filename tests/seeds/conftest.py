"""Fixtures of the seed commitment tests: one season, played once and shared by copy."""

from pathlib import Path

import pytest
from seeds_support import Season, play_season, reveal_season

from auction_night.seeds import work_tree_of


@pytest.fixture(scope="session")
def mid_season(tmp_path_factory: pytest.TempPathFactory) -> Season:
    """Two nights played and exported, the commitments published, the seeds still secret."""
    return play_season(tmp_path_factory.mktemp("mid"))


@pytest.fixture(scope="session")
def end_of_season(mid_season: Season, tmp_path_factory: pytest.TempPathFactory) -> Season:
    """The same season after the reveal: seeds.json, and replays that carry the seed."""
    season = mid_season.copy_to(tmp_path_factory.mktemp("end") / "tree")
    reveal_season(season)
    return season


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """A scratch directory that is not inside a git work tree, as the private file must be."""
    if work_tree_of(tmp_path) is not None:
        pytest.skip("the temporary directory of pytest is inside a git work tree")
    return tmp_path
