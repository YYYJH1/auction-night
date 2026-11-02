"""Capability records: what a model accepts, the checks that follow, and the documented facts."""

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from llm_night import seat_of, with_endpoint, with_seat
from pydantic import ValidationError

from auction_night.contracts import NightConfig
from auction_night.contracts.config import (
    EndpointConfig,
    SamplingParams,
    SeatConfig,
    ThinkingConfig,
)
from auction_night.llm.capabilities import (
    DOCUMENTED_CLAUDE_RECORDS,
    CapabilityError,
    anthropic_thinking,
    ensure_buildable,
    merged_sampling,
    record_conflicts,
    request_problems,
)
from auction_night.llm.request import build_request, user_message

REPO = Path(__file__).resolve().parents[2]
SYSTEM = "系统提示"
OBS = "观察"


@pytest.mark.parametrize(
    ("number", "update", "fragment"),
    [
        (3, {"thinking": ThinkingConfig(type="disabled")}, "'disabled' is not supported"),
        (2, {"thinking": ThinkingConfig(type="between_tools")}, "'between_tools' is not supported"),
        (
            3,
            {"thinking": ThinkingConfig(type="between_tools"), "effort": "xhigh"},
            "needs effort up to 'high'",
        ),
        (
            2,
            {"thinking": ThinkingConfig(type="disabled"), "effort": "max"},
            "needs effort up to 'high'",
        ),
        (
            3,
            {"thinking": ThinkingConfig(type="between_tools"), "effort": None},
            "needs effort up to 'high'",
        ),
        (3, {"thinking": None}, "thinking must be pinned"),
        (3, {"effort": None}, "effort must be pinned"),
        (3, {"max_tokens": None}, "max_tokens is required"),
        (3, {"sampling": SamplingParams(temperature=0.7)}, "sampling parameters are not supported"),
        (2, {"sampling": SamplingParams(top_k=5)}, "sampling parameters are not supported"),
        (3, {"model": None}, "model id is required"),
    ],
)
def test_conflicts_with_the_record_are_refused(
    api_config: NightConfig, number: int, update: dict[str, Any], fragment: str
) -> None:
    endpoint, seat = seat_of(api_config, number)
    bad = with_seat(seat, **update)
    with pytest.raises(CapabilityError, match=fragment) as caught:
        build_request(endpoint, bad, system=SYSTEM, messages=[user_message(OBS)])
    assert caught.value.problems and fragment in "; ".join(caught.value.problems)


def test_an_endpoint_default_sampling_is_refused_too(api_config: NightConfig) -> None:
    endpoint, seat = seat_of(api_config, 3)
    hot = with_endpoint(endpoint, sampling=SamplingParams(temperature=0.5))
    with pytest.raises(CapabilityError, match="sampling parameters"):
        build_request(hot, seat, system=SYSTEM, messages=[user_message(OBS)])
    empty = with_endpoint(endpoint, sampling=SamplingParams())
    assert build_request(empty, seat, system=SYSTEM, messages=[user_message(OBS)]).payload


def test_example_records_agree_with_the_documented_facts(api_config: NightConfig) -> None:
    endpoint = api_config.endpoints["anthropic"]
    for model, documented in DOCUMENTED_CLAUDE_RECORDS.items():
        record = endpoint.capabilities_for(model)
        assert record == documented
        assert record_conflicts(model, record) == []


def test_a_drifting_record_is_reported() -> None:
    sonnet = DOCUMENTED_CLAUDE_RECORDS["claude-sonnet-5-5"]
    drifted = sonnet.model_copy(
        update={"supports_forced_tool_choice": True, "thinking_modes": ("adaptive", "disabled")}
    )
    conflicts = record_conflicts("claude-sonnet-5-5", drifted)
    assert len(conflicts) == 2
    assert any("supports_forced_tool_choice" in c for c in conflicts)
    assert record_conflicts("some-other-model", drifted) == []


def _raw_config() -> dict[str, Any]:
    with (REPO / "configs" / "night_api.example.yaml").open(encoding="utf-8") as handle:
        return copy.deepcopy(yaml.safe_load(handle))


def _mutate_haiku_seat(raw: dict[str, Any], **update: Any) -> None:
    raw["seats"][1].update(update)


def _mutate_sonnet_seat(raw: dict[str, Any], **update: Any) -> None:
    raw["seats"][2].update(update)


MUTATIONS = {
    "sonnet disabled": lambda r: _mutate_sonnet_seat(r, thinking={"type": "disabled"}),
    "sonnet between_tools above high": lambda r: _mutate_sonnet_seat(
        r, thinking={"type": "between_tools"}, effort="xhigh"
    ),
    "haiku disabled above high": lambda r: _mutate_haiku_seat(
        r, thinking={"type": "disabled"}, effort="max"
    ),
    "haiku between_tools": lambda r: _mutate_haiku_seat(r, thinking={"type": "between_tools"}),
    "thinking unpinned": lambda r: r["seats"][2].pop("thinking"),
    "effort unpinned": lambda r: r["seats"][1].pop("effort"),
    "max_tokens missing": lambda r: r["seats"][2].pop("max_tokens"),
    "sampling on a seat": lambda r: _mutate_haiku_seat(r, sampling={"temperature": 0.7}),
    "sampling on the endpoint": lambda r: r["endpoints"]["anthropic"].update(
        sampling={"top_p": 0.9}
    ),
    "forced tool choice": lambda r: r["endpoints"]["anthropic"].update(tool_choice="tool"),
    "strict without support": lambda r: r["endpoints"]["anthropic"]["model_capabilities"][
        "claude-sonnet-5-5"
    ].update(supports_strict_tools=False),
}


@pytest.mark.parametrize("name", MUTATIONS)
def test_capability_checks_agree_with_the_configuration_validator(name: str) -> None:
    raw = _raw_config()
    assert not request_problems(*_pairs(raw)[0]) and not request_problems(*_pairs(raw)[1])
    MUTATIONS[name](raw)
    with pytest.raises(ValidationError):
        NightConfig.model_validate(raw)
    flagged = [request_problems(endpoint, seat) for endpoint, seat in _pairs(raw)]
    assert any(flagged), name


def _pairs(raw: dict[str, Any]) -> list[tuple[EndpointConfig, SeatConfig]]:
    endpoint = EndpointConfig.model_validate(raw["endpoints"]["anthropic"])
    return [(endpoint, SeatConfig.model_validate(raw["seats"][index])) for index in (1, 2)]


def test_valid_seats_have_no_problems_and_resolve_their_record(api_config: NightConfig) -> None:
    for number in (1, 2, 3, 4, 5, 6):
        endpoint, seat = seat_of(api_config, number)
        assert request_problems(endpoint, seat) == []
        assert ensure_buildable(endpoint, seat) == endpoint.capabilities_for(seat.model)


def test_sampling_of_a_seat_overrides_the_endpoint_per_field(api_config: NightConfig) -> None:
    endpoint, seat = seat_of(api_config, 1)  # the endpoint default is temperature 0.7
    assert merged_sampling(endpoint, seat) == {"temperature": 0.7}
    tuned = with_seat(seat, sampling=SamplingParams(top_p=0.9, seed=3))
    assert merged_sampling(endpoint, tuned) == {"temperature": 0.7, "top_p": 0.9, "seed": 3}
    hotter = with_seat(seat, sampling=SamplingParams(temperature=1.1))
    assert merged_sampling(endpoint, hotter) == {"temperature": 1.1}
    claude, sonnet = seat_of(api_config, 3)
    assert merged_sampling(claude, sonnet) == {}


def test_anthropic_thinking_sends_only_the_non_default_modes() -> None:
    assert anthropic_thinking(None) is None
    assert anthropic_thinking(ThinkingConfig(type="adaptive")) is None
    assert anthropic_thinking(ThinkingConfig(type="disabled")) == {"type": "disabled"}
    assert anthropic_thinking(ThinkingConfig(type="between_tools")) == {"type": "between_tools"}


def test_a_capability_error_lists_every_problem(api_config: NightConfig) -> None:
    endpoint, seat = seat_of(api_config, 3)
    bad = with_seat(seat, effort=None, max_tokens=None, sampling=SamplingParams(temperature=0.5))
    with pytest.raises(CapabilityError) as caught:
        ensure_buildable(endpoint, bad)
    assert len(caught.value.problems) == 3
    assert str(caught.value) == "; ".join(caught.value.problems)
    with pytest.raises(CapabilityError):
        build_request(endpoint, bad, system=SYSTEM, messages=[user_message(OBS)])
