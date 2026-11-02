"""Shared fixtures of the model layer tests."""

from pathlib import Path

import pytest
from llm_night import Night, build_night, make_settings

from auction_night.contracts import AnyEvent, NightConfig, load_config
from auction_night.llm.render import RenderSettings

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def night() -> Night:
    return build_night()


@pytest.fixture(scope="session")
def events(night: Night) -> list[AnyEvent]:
    return night.events()


@pytest.fixture
def settings() -> RenderSettings:
    return make_settings()


@pytest.fixture(scope="session")
def mock_config() -> NightConfig:
    return load_config(REPO / "configs" / "night_mock.yaml")


@pytest.fixture(scope="session")
def api_config() -> NightConfig:
    return load_config(REPO / "configs" / "night_api.example.yaml")
