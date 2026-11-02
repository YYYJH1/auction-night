"""A night with real endpoints, offline: the client classes are real, the SDKs behind them fakes.

The example API configuration has six model seats on four endpoints. Here every placeholder is
filled, the keys come from a made-up environment, and the SDK constructors are replaced by a
stand-in whose endpoint is down. That is enough to follow a night from the configuration through
the real clients, and to see that no key reaches a file. The mock model is not involved.
"""

import re
from itertools import cycle
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from e2e_support import MODEL_SEATS, concrete_api_config

from auction_night.contracts import NightConfig, loads_events
from auction_night.engine.deal import deal
from auction_night.llm.cassette import Cassette
from auction_night.llm.driver import EngineDriver, llm_routes
from auction_night.llm.nightrun import (
    CALLS_FILE,
    EVENTS_FILE,
    NightRunError,
    load_night_config,
    replay_directory,
    run_directory,
)
from auction_night.llm.personas import PERSONAS

# Keys are assembled from pieces, so that this file does not look like it holds one.
KEY_CLAUDE = "sk-" + "ant-api03-" + "Zq7" * 10
KEY_A = "sk-" + "proj-" + "Ab3" * 10
KEY_B = "local" + "-secret-" + "0123456789"
ENVIRON = {
    "ANTHROPIC_API_KEY": KEY_CLAUDE,
    "VENDOR_A_BASE_URL": "http://vendor-a.invalid/v1",
    "VENDOR_A_API_KEY": KEY_A,
    "VENDOR_B_BASE_URL": "http://vendor-b.invalid/v1",
    "VENDOR_B_API_KEY": KEY_B,
}


class DownSdk:
    """A stand-in for an SDK client whose endpoint is down: every call is noted and fails."""

    def __init__(self, notes: list[tuple[str, dict[str, Any], dict[str, Any]]], **options: Any):
        self.options = options
        self._notes = notes
        self.messages = SimpleNamespace(create=lambda **kw: self._fail("anthropic", kw))
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kw: self._fail("openai", kw))
        )

    def _fail(self, kind: str, kwargs: dict[str, Any]) -> Any:
        self._notes.append((kind, self.options, kwargs))
        raise RuntimeError("the endpoint is down")


@pytest.fixture(scope="module")
def notes() -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    return []


@pytest.fixture(scope="module")
def factories(notes: list) -> dict[str, Any]:
    def make(**options: Any) -> DownSdk:
        return DownSdk(notes, **options)

    return {"anthropic": make, "openai_compatible": make}


@pytest.fixture(scope="module")
def night(tmp_path_factory: pytest.TempPathFactory, factories: dict[str, Any]) -> Path:
    out = tmp_path_factory.mktemp("api") / "night"
    run_directory(concrete_api_config(), out, environ=ENVIRON, factories=factories)
    return out


# ==================================================================================== the calls


def test_the_calls_of_a_table_with_real_endpoints_follow_the_configuration() -> None:
    config = concrete_api_config()
    driver = EngineDriver(config, deal(config), llm_routes(config))
    phase = driver.next_phase()
    assert phase is not None and not phase.sequential
    calls = {call.seat: call for call in phase.calls}
    assert sorted(calls) == list(MODEL_SEATS)
    assert {seat: call.endpoint for seat, call in calls.items()} == {
        1: "vendor_a",
        2: "anthropic",
        3: "anthropic",
        4: "vendor_a",
        5: "vendor_b",
        6: "vendor_b",
    }
    haiku, sonnet, local = calls[2].request, calls[3].request, calls[1].request
    assert (haiku.kind, haiku.model) == ("anthropic", "claude-haiku-5-5")
    assert sonnet.model == "claude-sonnet-5-5"
    assert haiku.payload["output_config"] == {"effort": "medium"}
    assert haiku.payload["tools"][0]["strict"] is True and "temperature" not in haiku.payload
    assert haiku.payload["tool_choice"] == sonnet.payload["tool_choice"] == {"type": "auto"}
    assert "thinking" not in haiku.payload and "thinking" not in sonnet.payload  # adaptive
    assert not {"temperature", "top_p", "top_k"} & set(sonnet.payload)  # the API answers 400
    assert haiku.payload["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert local.kind == "openai_compatible" and local.payload["temperature"] == 0.7
    assert local.payload["messages"][0]["role"] == "system"
    assert local.payload["tools"][0]["type"] == "function"
    # every seat has a system text of its own: the cards of the seat are in it
    systems = {
        call.request.payload["system"][0]["text"]
        if call.request.kind == "anthropic"
        else call.request.payload["messages"][0]["content"]
        for call in phase.calls
    }
    assert len(systems) == 6


# ====================================================================== a night on a dead endpoint


def test_every_model_action_of_a_dead_endpoint_is_an_abstention(night: Path) -> None:
    config = concrete_api_config()
    from auction_night.contracts import loads_events

    events = loads_events((night / EVENTS_FILE).read_text(encoding="utf-8"))
    actions = [e for e in events if e.type == "action" and e.data.seat in MODEL_SEATS]  # type: ignore[union-attr]
    assert actions
    assert {
        (a.data.status, a.data.reason) for a in actions if a.data.status != "kept_previous"
    } == {  # type: ignore[union-attr]
        ("abstain", "missing")
    }
    assert {a.data.attempts for a in actions} == {1}  # type: ignore[union-attr]
    assert events[-2].type == "night_end" and events[-2].data.status == "completed"  # type: ignore[union-attr]
    assert config.night.id == events[0].data.night_id  # type: ignore[union-attr]


def test_the_sdks_were_built_from_the_environment_and_called_with_the_requests(
    night: Path, notes: list
) -> None:
    kinds = {kind for kind, _, _ in notes}
    assert kinds == {"anthropic", "openai"}
    keys = {options["api_key"] for _, options, _ in notes}
    assert keys == {KEY_CLAUDE, KEY_A, KEY_B}
    assert all(options["max_retries"] == 0 for _, options, _ in notes)  # the runner retries
    urls = {options.get("base_url") for _, options, _ in notes}
    assert urls == {None, ENVIRON["VENDOR_A_BASE_URL"], ENVIRON["VENDOR_B_BASE_URL"]}
    for kind, _, kwargs in notes:
        assert kwargs["timeout"] == 90.0 and "tools" in kwargs and "model" in kwargs
        assert ("system" in kwargs) == (kind == "anthropic")
    cassette = Cassette.replay(night / CALLS_FILE)
    assert len(notes) == len(cassette) and len(cassette) > 100


def test_the_failures_are_recorded_as_attempts_of_the_calls(night: Path) -> None:
    cassette = Cassette.replay(night / CALLS_FILE)
    assert {e.outcome for e in cassette.entries} == {"error"}
    assert {e.error["type"] for e in cassette.entries if e.error} == {"RuntimeError"}  # type: ignore[index]
    assert all(e.response is None and e.key.attempt == 1 for e in cassette.entries)
    assert {e.model for e in cassette.entries} == {
        "claude-haiku-5-5",
        "claude-sonnet-5-5",
        "model-a1",
        "model-a2",
        "model-b1",
        "model-b2",
    }


def test_no_key_reaches_a_file(night: Path) -> None:
    for name in (EVENTS_FILE, CALLS_FILE):
        text = (night / name).read_text(encoding="utf-8")
        for secret in (KEY_CLAUDE, KEY_A, KEY_B, *ENVIRON.values()):
            assert secret not in text, (name, secret[:6])


def test_such_a_night_replays_with_no_key_and_no_sdk(night: Path) -> None:
    report = replay_directory(concrete_api_config(), night)
    assert report.matches and report.unused_attempts == 0


# ====================================================================== a night in variety mode


def test_a_variety_night_with_model_seats_gives_each_seat_its_persona(
    tmp_path: Path, factories: dict[str, Any], notes: list
) -> None:
    raw = concrete_api_config().model_dump(mode="json")
    raw["night"]["mode"] = "variety"
    for seat, key in zip(raw["seats"][: len(MODEL_SEATS)], cycle(PERSONAS), strict=False):
        seat["persona"] = key
    variety = NightConfig.model_validate(raw)
    before = len(notes)
    out = tmp_path / "night"
    run_directory(variety, out, environ=ENVIRON, factories=factories)
    sent = notes[before:]
    assert sent
    named = set()
    for kind, _, kwargs in sent:
        system = (
            kwargs["system"][0]["text"] if kind == "anthropic" else kwargs["messages"][0]["content"]
        )
        found = re.search(r"【你的人设】(\S+?)：", system)
        assert found, "every call of a variety night carries the persona of its seat"
        named.add(found.group(1))
    assert named == set(PERSONAS)
    events = loads_events((out / EVENTS_FILE).read_text(encoding="utf-8"))
    assert events[0].data.mode == "variety"  # type: ignore[union-attr]
    assert replay_directory(variety, out).matches


# ================================================================================== what is refused


def test_a_key_that_is_missing_is_named_and_nothing_is_written(
    tmp_path: Path, factories: dict[str, Any]
) -> None:
    out = tmp_path / "night"
    with pytest.raises(NightRunError, match="ANTHROPIC_API_KEY"):
        run_directory(concrete_api_config(), out, environ={}, factories=factories)
    assert not out.exists()


def test_the_example_configuration_cannot_be_run_before_its_placeholders_are_filled() -> None:
    path = Path(__file__).resolve().parents[2] / "configs" / "night_api.example.yaml"
    with pytest.raises(NightRunError, match="still holds placeholders") as caught:
        load_night_config(path)
    message = str(caught.value)
    assert "night.deal_seed_commitment" in message and "seats[0].model" in message


def test_a_cost_cap_needs_a_price_for_every_model(
    tmp_path: Path, factories: dict[str, Any]
) -> None:
    config = concrete_api_config()
    capped = config.model_copy(
        update={"limits": config.limits.model_copy(update={"max_cost_per_night_usd": 1.0})}
    )
    with pytest.raises(NightRunError, match="no price"):
        run_directory(capped, tmp_path / "night", environ=ENVIRON, factories=factories)
