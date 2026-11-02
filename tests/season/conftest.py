"""Fixtures of the season tests: one real rotation season, played once and shared."""

from pathlib import Path

import pytest
from season_support import play_real_season


@pytest.fixture(scope="session")
def real_season(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Two decks in four rotations: eight real mock nights, each with its config.yaml."""
    return play_real_season(tmp_path_factory.mktemp("real") / "s1")
