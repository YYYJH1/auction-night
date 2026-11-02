"""Helpers of the export tests: the mock night played once with everything the export needs, a
way to change its log, and a fake model behind the client interface for the content classifier.

Nothing here touches a network. The mock night is the one of ``configs/night_mock.yaml``: eight
seats, twelve lots, all six formats, six scripted mock seats and two rule bots.
"""

import copy
import dataclasses
import json
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from auction_night.contracts import NightConfig, Replay
from auction_night.contracts.events import AnyEvent
from auction_night.engine.night import NightState
from auction_night.llm.cassette import Cassette
from auction_night.llm.client import ClientReply, normalise_openai
from auction_night.llm.request import BuiltRequest
from auction_night.show.factcard import FactCards, build_fact_cards
from auction_night.show.host import HostOutput, make_host, run_host
from auction_night.show.replay_export import GitInfo, play_offline
from auction_night.theory import LotTheory, analyse_night

REPO = Path(__file__).resolve().parents[2]
MOCK_CONFIG = REPO / "configs" / "night_mock.yaml"
SAMPLE = REPO / "tests" / "fixtures" / "replays" / "mock_night.json"
STAMP = "2026-11-01T00:00:00Z"
NO_GIT = GitInfo()


def raw_config() -> dict[str, Any]:
    """The mock configuration as plain data, ready to be changed and validated."""
    with MOCK_CONFIG.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def config_with(**changes: Any) -> NightConfig:
    """The mock configuration with some top-level sections replaced (``night`` keys merge)."""
    raw = raw_config()
    for section, value in changes.items():
        if section == "night":
            raw["night"].update(copy.deepcopy(value))
        else:
            raw[section] = copy.deepcopy(value)
    return NightConfig.model_validate(raw)


@dataclass(frozen=True)
class Night:
    """A night that was played, with everything that comes after it."""

    config: NightConfig
    state: NightState
    cassette: Cassette
    theory: list[LotTheory]
    cards: FactCards
    host: HostOutput


def play(config: NightConfig) -> Night:
    """Play ``config`` offline and run the fact cards and the mock host over it."""
    state, cassette = play_offline(config, clock=lambda: STAMP)
    theory = analyse_night(state)
    cards = build_fact_cards(state, theory=theory)
    return Night(config, state, cassette, theory, cards, run_host(cards, make_host(config)))


@cache
def mock_night() -> Night:
    """The mock night, played once for the whole test session. Treat it as read-only."""
    return play(config_with())


def with_events(night: Night, events: list[AnyEvent] | tuple[AnyEvent, ...]) -> NightState:
    """The state of ``night`` with another log (the rest of the state is the same)."""
    return dataclasses.replace(night.state, events=tuple(events))


def replay_json(replay: Replay) -> dict[str, Any]:
    """A replay as plain JSON data."""
    return json.loads(replay.model_dump_json(by_alias=True))


def sample_raw() -> dict[str, Any]:
    """The committed sample as plain data, free to be changed by the test."""
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def completion(
    text: str, *, model: str = "scan-model-0001", finish_reason: str = "stop"
) -> dict[str, Any]:
    """A chat completion as a plain mapping."""
    return {
        "id": "fake-1",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 200, "completion_tokens": 30, "total_tokens": 230},
    }


Responder = Callable[[BuiltRequest, int], "str | tuple[str, str] | BaseException"]


class FakeClient:
    """A model behind the client interface: ``respond(request, call_number)`` gives its answer.

    The answer is a text, a pair of a text and a finish reason (``content_filter`` is a refusal),
    or an exception, which is raised as the SDK would raise it. Every request is kept in
    ``requests``.
    """

    def __init__(self, respond: Responder) -> None:
        self.respond = respond
        self.requests: list[BuiltRequest] = []

    def complete(self, request: BuiltRequest, *, timeout_s: float) -> ClientReply:
        self.requests.append(request)
        answer = self.respond(request, len(self.requests))
        if isinstance(answer, BaseException):
            raise answer
        text, finish = (answer, "stop") if isinstance(answer, str) else answer
        return normalise_openai(completion(text, finish_reason=finish), latency_ms=7)


def user_text(request: BuiltRequest) -> str:
    """The text of the last user message of a request."""
    messages = request.payload["messages"]
    return str(next(m["content"] for m in reversed(messages) if m["role"] == "user"))


def probes_of(request: BuiltRequest) -> list[dict[str, Any]]:
    """The texts that a classifier request carries, as the model reads them."""
    first = next(m["content"] for m in request.payload["messages"] if m["role"] == "user")
    return json.loads(str(first).split("\n", 1)[1])
