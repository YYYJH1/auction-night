"""Fixtures of the end-to-end tests: one mock night, played once and shared."""

import pytest
from e2e_support import Played, make_config, mock_prices, play

from auction_night.contracts import NightConfig


@pytest.fixture(scope="session")
def config() -> NightConfig:
    return make_config()


@pytest.fixture(scope="session")
def night(config: NightConfig) -> Played:
    """The mock night with nothing going wrong, recorded in memory."""
    return play(config)


@pytest.fixture(scope="session")
def priced(config: NightConfig) -> Played:
    """The same night with every mock token priced at one micro-dollar."""
    return play(config, prices=mock_prices())
