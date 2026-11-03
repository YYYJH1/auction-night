"""Night configuration: the example files, capability rules and safety checks."""

import copy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from auction_night.contracts import (
    NightConfig,
    PricesFile,
    SeedResolutionError,
    canonical_json,
    config_hash,
    find_placeholders,
    load_config,
    load_prices,
    seed_commitment,
)
from auction_night.contracts.mechanism import FORMAT_KEYS

CLAUDE_IDS = {"claude-haiku-5-5", "claude-sonnet-5-5"}
HAIKU, SONNET = 1, 2  # list positions of the Claude seats in the API example


def rejects(raw: dict[str, Any], match: str) -> None:
    with pytest.raises((ValidationError, ValueError), match=match):
        NightConfig.model_validate(raw)


# ----------------------------------------------------------------------- example files


def test_example_configs_validate(mock_config: NightConfig, api_config: NightConfig) -> None:
    for config in (mock_config, api_config):
        assert config.night.n_lots == 12
        assert len(config.seats) == 8
        assert config.night.formats == FORMAT_KEYS
        assert config.night.budget.c == 300
        assert config.night.missions.collectors == config.night.missions.shills == 3
        assert config.seats[6].policy == "truthful"
        assert config.seats[7].policy == "economist" and config.seats[7].pacing


def test_mock_night_is_offline_and_complete(mock_config: NightConfig) -> None:
    assert mock_config.is_offline
    assert mock_config.endpoints == {}
    assert find_placeholders(mock_config) == []
    kinds = [seat.kind for seat in mock_config.seats]
    assert kinds == ["mock"] * 6 + ["bot"] * 2
    assert all(seat.mock_policy for seat in mock_config.seats[:6])
    assert mock_config.show.host_offline.kind == "mock"
    assert [judge.kind for judge in mock_config.show.judges] == ["mock", "mock"]


def test_api_example_uses_placeholders_except_for_claude(api_config: NightConfig) -> None:
    assert not api_config.is_offline
    llm_models = [seat.model for seat in api_config.seats if seat.kind == "llm"]
    real = {model for model in llm_models if not str(model).startswith("<")}
    assert real == CLAUDE_IDS
    placeholders = find_placeholders(api_config)
    assert placeholders, "the example must flag what has to be filled in"
    assert all("claude" not in path for path in placeholders)
    for index, seat in enumerate(api_config.seats):
        if seat.kind == "llm" and seat.model not in CLAUDE_IDS:
            assert f"seats[{index}].model" in placeholders
    assert "show.host_offline.model" in placeholders
    assert "night.deal_seed_commitment" in placeholders, "the commitment is published later"


def test_api_example_claude_seats_pin_thinking_and_carry_no_sampling(
    api_config: NightConfig,
) -> None:
    endpoint = api_config.endpoints["anthropic"]
    assert endpoint.sampling is None
    for index in (HAIKU, SONNET):
        seat = api_config.seats[index]
        assert seat.endpoint == "anthropic" and seat.model in CLAUDE_IDS
        assert seat.sampling is None
        assert seat.thinking is not None and seat.thinking.type == "adaptive"
        assert seat.effort == "medium" and seat.max_tokens
        assert not endpoint.capabilities_for(seat.model).supports_sampling_params


def test_claude_capabilities_differ_by_model(api_config: NightConfig) -> None:
    endpoint = api_config.endpoints["anthropic"]
    haiku = endpoint.capabilities_for("claude-haiku-5-5")
    sonnet = endpoint.capabilities_for("claude-sonnet-5-5")
    assert haiku.supports_forced_tool_choice and not sonnet.supports_forced_tool_choice
    assert "disabled" in haiku.thinking_modes and "disabled" not in sonnet.thinking_modes
    assert "between_tools" in sonnet.thinking_modes and "between_tools" not in haiku.thinking_modes
    assert endpoint.capabilities_for("some-unlisted-model") == endpoint.capabilities


# ------------------------------------------------------------- sampling and capabilities


@pytest.mark.parametrize("param", [{"temperature": 0.7}, {"top_p": 0.9}, {"top_k": 40}])
@pytest.mark.parametrize("index", [HAIKU, SONNET])
def test_claude_seat_with_sampling_is_rejected(
    api_raw: dict[str, Any], param: dict[str, Any], index: int
) -> None:
    api_raw["seats"][index]["sampling"] = param
    rejects(api_raw, "sampling parameters are not supported")


def test_claude_endpoint_with_default_sampling_is_rejected(api_raw: dict[str, Any]) -> None:
    api_raw["endpoints"]["anthropic"]["sampling"] = {"temperature": 1.0}
    rejects(api_raw, "sampling parameters are not supported")


def test_a_bare_temperature_key_is_an_unknown_field(api_raw: dict[str, Any]) -> None:
    api_raw["seats"][HAIKU]["temperature"] = 0.7
    rejects(api_raw, "Extra inputs are not permitted")


def test_empty_sampling_block_is_allowed_on_claude(api_raw: dict[str, Any]) -> None:
    api_raw["seats"][HAIKU]["sampling"] = {}
    NightConfig.model_validate(api_raw)


def test_sampling_is_allowed_where_the_capability_says_so(api_config: NightConfig) -> None:
    assert api_config.endpoints["vendor_a"].sampling is not None
    assert api_config.endpoints["vendor_a"].capabilities_for("<model-a1>").supports_sampling_params


@pytest.mark.parametrize(
    ("index", "thinking", "effort", "accepted"),
    [
        (HAIKU, "disabled", "medium", True),
        (HAIKU, "disabled", "high", True),
        (HAIKU, "disabled", "xhigh", False),
        (HAIKU, "disabled", "max", False),
        (HAIKU, "between_tools", "medium", False),
        (HAIKU, "adaptive", "max", True),
        (SONNET, "disabled", "medium", False),
        (SONNET, "between_tools", "high", True),
        (SONNET, "between_tools", "xhigh", False),
        (SONNET, "adaptive", "xhigh", True),
    ],
)
def test_thinking_and_effort_follow_the_capability_record(
    api_raw: dict[str, Any], index: int, thinking: str, effort: str, accepted: bool
) -> None:
    api_raw["seats"][index]["thinking"] = {"type": thinking}
    api_raw["seats"][index]["effort"] = effort
    if accepted:
        NightConfig.model_validate(api_raw)
    else:
        rejects(api_raw, "thinking")


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("thinking", "pinned explicitly"),
        ("effort", "pinned explicitly"),
        ("max_tokens", "required"),
    ],
)
def test_claude_seats_must_pin_their_request_parameters(
    api_raw: dict[str, Any], field: str, message: str
) -> None:
    del api_raw["seats"][SONNET][field]
    rejects(api_raw, message)


def test_forced_tool_choice_is_rejected_when_sonnet_is_on_the_endpoint(
    api_raw: dict[str, Any],
) -> None:
    api_raw["endpoints"]["anthropic"]["tool_choice"] = "any"
    rejects(api_raw, "forced tool_choice is not supported")


def test_strict_tools_need_the_capability(api_raw: dict[str, Any]) -> None:
    api_raw["endpoints"]["vendor_a"]["strict_tools"] = True
    rejects(api_raw, "strict tools are not supported")


def test_unknown_endpoint_is_rejected(api_raw: dict[str, Any]) -> None:
    api_raw["seats"][0]["endpoint"] = "nowhere"
    rejects(api_raw, "unknown endpoint")


def test_default_capabilities_are_conservative() -> None:
    from auction_night.contracts.config import default_capabilities

    claude = default_capabilities("anthropic")
    assert not claude.supports_sampling_params and not claude.supports_forced_tool_choice
    assert claude.supports_strict_tools and claude.thinking_modes == ("adaptive",)
    generic = default_capabilities("openai_compatible")
    assert generic.supports_sampling_params and not generic.supports_strict_tools
    assert generic.thinking_modes == () and generic.efforts == ()


# ----------------------------------------------------------------------- secrets


@pytest.mark.parametrize("field", ["api_key_env", "base_url_env"])
def test_environment_variable_names_only(api_raw: dict[str, Any], field: str) -> None:
    api_raw["endpoints"]["vendor_a"][field] = "not-an-env-name-123"
    with pytest.raises(ValidationError) as excinfo:
        NightConfig.model_validate(api_raw)
    assert "not-an-env-name-123" not in str(excinfo.value), "inputs must not be echoed"


def test_base_url_cannot_carry_credentials(api_raw: dict[str, Any]) -> None:
    endpoint = api_raw["endpoints"]["vendor_a"]
    del endpoint["base_url_env"]
    endpoint["base_url"] = "https://user:pass@example.com/v1"
    rejects(api_raw, "pattern")
    endpoint["base_url"] = "https://example.com/v1?key=abc"
    rejects(api_raw, "pattern")
    endpoint["base_url"] = "https://example.com/v1"
    NightConfig.model_validate(api_raw)


def test_endpoint_access_rules(api_raw: dict[str, Any]) -> None:
    api_raw["endpoints"]["vendor_a"]["base_url"] = "https://example.com/v1"
    rejects(api_raw, "not both")
    del api_raw["endpoints"]["vendor_a"]["base_url"]
    del api_raw["endpoints"]["vendor_a"]["base_url_env"]
    rejects(api_raw, "needs base_url")


# -------------------------------------------------------------------------- structure


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("night", "tiers"), {100: 5, 200: 4, 400: 4}, "tier counts"),
        (("night", "dutch", "levels"), 8, "divisible"),
        (("night", "tags"), ["海洋", "机械", "甜品", "怪谈", "古风"], "evenly"),
        (("night", "tags"), ["海洋", "海洋", "甜品", "怪谈"], "without repeats"),
        (("night", "lots_per_format"), 2.0, "valid integer"),
        (("night", "missions", "shills"), 5, "eligible lots"),
        (("night", "missions", "collectors"), 4, "model seats"),
        (("night", "reserve"), 150, "smallest tier"),
        (("night", "budget", "spread"), [1.5, 0.5], "spread"),
        (("night", "blind", "noise"), 1.5, "less than or equal"),
        (("night", "missions", "shill_threshold"), 0.9512345, "decimal"),
        (("night", "missions", "shill_formats"), ["blind_box"], "english"),
        (("night", "lang"), "en", "zh"),
        (("night", "memory"), "everything", "plan_pledges"),
        (("night", "memory"), "Plan_Pledges", "off"),
        (("night", "memory"), True, "off"),
        (("night", "memory"), None, "off"),
        (("show", "judges"), [{"kind": "mock"}], "exactly two"),
    ],
)
def test_night_rules_are_validated(
    mock_raw: dict[str, Any], path: tuple[str, ...], value: Any, message: str
) -> None:
    node = mock_raw
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    rejects(mock_raw, message)


def test_the_memory_of_a_night_is_off_unless_the_file_says_otherwise(
    mock_raw: dict[str, Any],
) -> None:
    assert "memory" not in mock_raw["night"]
    assert NightConfig.model_validate(mock_raw).night.memory == "off"
    for value in ("off", "plan_pledges"):
        mock_raw["night"]["memory"] = value
        assert NightConfig.model_validate(mock_raw).night.memory == value


def test_a_small_night_is_a_valid_variant(mock_raw: dict[str, Any]) -> None:
    mock_raw["night"].update(
        formats=["sealed_first", "english", "sealed_second", "dutch"],
        lots_per_format=1,
        tiers={100: 2, 200: 1, 400: 1},
    )
    mock_raw["night"]["missions"].update(collectors=1, shills=1, collector_need=1)
    mock_raw["seats"] = [
        {"seat": 1, "codename": "甲", "kind": "mock", "mock_policy": "honest"},
        {"seat": 2, "codename": "乙", "kind": "mock", "mock_policy": "random"},
        {"seat": 3, "codename": "老实人", "kind": "bot", "policy": "truthful"},
        {"seat": 4, "codename": "经济学家", "kind": "bot", "policy": "economist", "pacing": True},
    ]
    config = NightConfig.model_validate(mock_raw)
    assert config.night.n_lots == 4
    assert [config.night.mechanism_for(key).ref for key in config.night.formats] == [
        "sealed_first@v1",
        "english@v1",
        "sealed_second@v1",
        "dutch@v1",
    ]


def test_mechanisms_take_night_parameters(mock_raw: dict[str, Any]) -> None:
    mock_raw["night"]["reserve"] = 10
    mock_raw["night"]["english"]["revise_when_remaining_le"] = 3
    mock_raw["night"]["dutch"]["levels"] = 5
    night = NightConfig.model_validate(mock_raw).night
    assert night.mechanism_for("all_pay").reserve == 0
    assert night.mechanism_for("sealed_first").reserve == 10
    assert night.mechanism_for("english").theta == 3
    assert night.mechanism_for("dutch").levels == 5


@pytest.mark.parametrize(
    ("index", "change", "message"),
    [
        (0, {"endpoint": "x"}, "model fields"),
        (6, {"policy": None}, "policy is required"),
        (6, {"mock_policy": "honest"}, "mock_policy"),
        (0, {"mock_policy": None}, "mock_policy is required"),
        (0, {"pacing": True}, "pacing"),
        (6, {"persona": "x"}, "persona"),
        (1, {"codename": "鲸落"}, "unique"),
        (1, {"codename": "a b"}, "pattern"),
        (1, {"codename": "【甲】"}, "pattern"),
        (1, {"seat": 7}, "numbered"),
    ],
)
def test_seat_rules(
    mock_raw: dict[str, Any], index: int, change: dict[str, Any], message: str
) -> None:
    mock_raw["seats"][index].update(change)
    rejects(mock_raw, message)


def test_human_seat_is_allowed_once(mock_raw: dict[str, Any]) -> None:
    mock_raw["seats"][2] = {"seat": 3, "codename": "小满", "kind": "human"}
    config = NightConfig.model_validate(mock_raw)
    assert config.has_human and config.is_offline
    mock_raw["seats"][3] = {"seat": 4, "codename": "拾光", "kind": "human"}
    rejects(mock_raw, "at most one human")


def test_personas_belong_to_variety_mode(mock_raw: dict[str, Any], api_raw: dict[str, Any]) -> None:
    mock_raw["seats"][0]["persona"] = "iron-abacus"
    rejects(mock_raw, "plain mode has no personas")
    api_raw["night"]["mode"] = "variety"
    rejects(api_raw, "variety mode needs a persona")
    for seat in api_raw["seats"]:
        if seat["kind"] == "llm":
            seat["persona"] = "iron-abacus"
    NightConfig.model_validate(api_raw)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["seats"][4].update(vendor="vendor-a"), "limit 2"),
        (lambda d: d["show"]["judges"][1].update(vendor="vendor-c"), "different vendors"),
        (lambda d: d["show"]["judges"][1].update(vendor="vendor-a"), "judge shares a vendor"),
        (lambda d: d["show"]["host_offline"].update(vendor="anthropic"), "host must not share"),
        (
            lambda d: d["show"]["content_scan"]["classifier"].update(vendor="vendor-b"),
            "classifier must not share",
        ),
        (lambda d: d["season"].pop("rotation"), "rotation index"),
        (lambda d: d["season"].update(deck="d99"), "not listed"),
        (lambda d: d["season"].update(format="single"), "rotation only applies"),
    ],
)
def test_lineup_and_season_rules(api_raw: dict[str, Any], mutate: Any, message: str) -> None:
    mutate(api_raw)
    rejects(api_raw, message)


def test_overlap_of_judge_vendor_needs_an_explicit_opt_in(api_raw: dict[str, Any]) -> None:
    api_raw["show"]["judges"][1].update(vendor="vendor-a")
    api_raw["show"]["allow_judge_vendor_overlap"] = True
    NightConfig.model_validate(api_raw)


def test_models_are_frozen(mock_config: NightConfig) -> None:
    with pytest.raises(ValidationError):
        mock_config.night.id = "other"  # type: ignore[misc]


def test_unknown_section_is_rejected(mock_raw: dict[str, Any]) -> None:
    mock_raw["extras"] = {}
    rejects(mock_raw, "Extra inputs")


def test_cost_cap_converts_to_integer_micro_dollars(mock_raw: dict[str, Any]) -> None:
    mock_raw["limits"]["max_cost_per_night_usd"] = 2.5
    assert NightConfig.model_validate(mock_raw).limits.max_cost_microusd == 2_500_000
    mock_raw["limits"]["max_cost_per_night_usd"] = 0.1234567
    rejects(mock_raw, "decimal")
    mock_raw["limits"]["max_cost_per_night_usd"] = None
    assert NightConfig.model_validate(mock_raw).limits.max_cost_microusd is None


# --------------------------------------------------------------------- hash and loading


def test_config_hash_is_stable_and_sensitive(mock_raw: dict[str, Any]) -> None:
    first = config_hash(NightConfig.model_validate(mock_raw))
    assert first.startswith("sha256:") and len(first) == 7 + 64
    again = config_hash(NightConfig.model_validate(copy.deepcopy(mock_raw)))
    assert first == again
    shuffled = {key: mock_raw[key] for key in reversed(list(mock_raw))}
    assert config_hash(NightConfig.model_validate(shuffled)) == first
    mock_raw["night"]["seed"] = "another-seed"
    assert config_hash(NightConfig.model_validate(mock_raw)) != first


def test_a_setting_added_after_the_freeze_leaves_the_hash_alone_until_it_is_used(
    mock_raw: dict[str, Any],
) -> None:
    """The hash of a published replay must stay the hash of its configuration. ``memory`` came
    after the freeze, so while it is off it is not part of the digest."""
    first = config_hash(NightConfig.model_validate(mock_raw))
    mock_raw["night"]["memory"] = "off"
    assert config_hash(NightConfig.model_validate(mock_raw)) == first
    mock_raw["night"]["memory"] = "plan_pledges"
    assert config_hash(NightConfig.model_validate(mock_raw)) != first
    config = NightConfig.model_validate(mock_raw)
    text = canonical_json(config, allow_float=True)
    assert '"memory":"plan_pledges"' in text
    assert NightConfig.model_validate_json(text) == config


def test_integer_seeds_are_text(mock_raw: dict[str, Any]) -> None:
    mock_raw["night"]["seed"] = 20261101
    assert NightConfig.model_validate(mock_raw).night.seed == "20261101"
    mock_raw["night"]["seed"] = True
    rejects(mock_raw, "seed")


def test_config_round_trips_through_json(api_config: NightConfig) -> None:
    text = canonical_json(api_config, allow_float=True)
    again = NightConfig.model_validate_json(text)
    assert again == api_config
    assert canonical_json(again, allow_float=True) == text


def test_duplicate_yaml_keys_are_rejected(tmp_path: Path, mock_raw: dict[str, Any]) -> None:
    import yaml

    text = yaml.safe_dump(mock_raw, allow_unicode=True)
    path = tmp_path / "dup.yaml"
    path.write_text(text + "limits:\n  call_timeout_s: 9\n", encoding="utf-8")
    with pytest.raises(yaml.YAMLError, match="duplicate key"):
        load_config(path)
    path.write_text(text, encoding="utf-8")
    assert load_config(path).night.id == "mock-0001"


def test_yaml_merge_keys_may_be_overridden(tmp_path: Path, mock_raw: dict[str, Any]) -> None:
    import yaml

    text = yaml.safe_dump(mock_raw, allow_unicode=True)
    merged = text.replace("night:\n", "night:\n  <<: {seed: merged-seed}\n", 1)
    path = tmp_path / "merge.yaml"
    path.write_text(merged, encoding="utf-8")
    assert load_config(path).night.seed == "mock-seed-0001", "the explicit key wins"
    without = merged.replace("  seed: mock-seed-0001\n", "", 1)
    path.write_text(without, encoding="utf-8")
    assert load_config(path).night.seed == "merged-seed"


# ------------------------------------------------------------------------------ prices


def test_prices_example_validates() -> None:
    prices = load_prices(Path(__file__).resolve().parents[2] / "configs" / "prices.example.yaml")
    assert set(prices.prices) == CLAUDE_IDS
    haiku = prices.prices["claude-haiku-5-5"]
    assert (haiku.input_per_mtok, haiku.output_per_mtok, haiku.cache_read_per_mtok) == (
        0.10,
        0.50,
        0.01,
    )
    assert prices.prices["claude-sonnet-5-5"].output_per_mtok == 10
    for price in prices.prices.values():
        assert price.currency == "USD" and price.source.startswith("https://")
        assert price.cache_read_per_mtok is not None
        assert price.cache_read_per_mtok < price.input_per_mtok < price.output_per_mtok


def test_price_entries_need_a_source_and_a_date() -> None:
    entry = {"input_per_mtok": 1, "output_per_mtok": 2, "as_of": "2026-11-01"}
    with pytest.raises(ValidationError):
        PricesFile.model_validate({"prices": {"m": entry}})
    ok = PricesFile.model_validate({"prices": {"m": {**entry, "source": "https://example.com"}}})
    assert ok.prices["m"].cache_write_per_mtok is None
    with pytest.raises(ValidationError):
        PricesFile.model_validate(
            {"prices": {"m": {**entry, "source": "https://example.com", "input_per_mtok": -1}}}
        )


def test_a_commitment_in_the_config_must_match_the_seed(mock_raw: dict[str, Any]) -> None:
    mock_raw["night"]["deal_seed_commitment"] = seed_commitment("mock-seed-0001")
    assert NightConfig.model_validate(mock_raw).night.deal_seed_commitment
    mock_raw["night"]["deal_seed_commitment"] = seed_commitment("another-seed")
    rejects(mock_raw, "not the commitment of seed")
    mock_raw["night"]["deal_seed_commitment"] = "sha256:not-hex"
    rejects(mock_raw, "pattern")


# ------------------------------------------------------------------------------- seeds

SEED_VAR = "AUCTION_NIGHT_SEED_S1_D03"
SEED = "a-long-random-seed-for-the-tests-0001"


def env_seed(raw: dict[str, Any], commitment: str | None = None) -> dict[str, Any]:
    """Turn a raw config into one that takes its seed from the environment."""
    night = raw["night"]
    night.pop("seed", None)
    night["seed_env"] = SEED_VAR
    if commitment is not None:
        night["deal_seed_commitment"] = commitment
    return raw


def test_exactly_one_of_seed_and_seed_env_is_required(mock_raw: dict[str, Any]) -> None:
    both = copy.deepcopy(mock_raw)
    both["night"]["seed_env"] = SEED_VAR
    rejects(both, "exactly one of seed and seed_env")
    neither = copy.deepcopy(mock_raw)
    del neither["night"]["seed"]
    rejects(neither, "exactly one of seed and seed_env")
    NightConfig.model_validate(env_seed(mock_raw))


@pytest.mark.parametrize("name", ["lower_case", "has-dash", "1STARTS_WITH_DIGIT", "", "A B"])
def test_seed_env_is_a_variable_name(mock_raw: dict[str, Any], name: str) -> None:
    env_seed(mock_raw)["night"]["seed_env"] = name
    with pytest.raises(ValidationError) as info:
        NightConfig.model_validate(mock_raw)
    assert "seed_env" in str(info.value)


def test_the_inline_path_is_for_mock_and_test_configs(mock_config: NightConfig) -> None:
    night = mock_config.night
    assert night.seed == "mock-seed-0001" and night.seed_env is None
    assert night.resolve_seed({}) == "mock-seed-0001", "no environment is needed"
    assert night.resolve_seed({"MOCK_SEED": "other"}) == "mock-seed-0001"


def test_inline_seed_resolves_against_its_commitment(mock_raw: dict[str, Any]) -> None:
    mock_raw["night"]["deal_seed_commitment"] = seed_commitment("mock-seed-0001")
    night = NightConfig.model_validate(mock_raw).night
    assert night.resolve_seed({}) == "mock-seed-0001"


def test_the_api_example_takes_its_seed_from_the_environment(
    api_config: NightConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    night = api_config.night
    assert night.seed is None and night.seed_env == SEED_VAR
    assert night.deal_seed_commitment is not None and night.deal_seed_commitment.startswith("<")
    placeholders = find_placeholders(api_config)
    assert "night.deal_seed_commitment" in placeholders and "night.seed_env" not in placeholders
    monkeypatch.delenv(SEED_VAR, raising=False)
    load_config(Path(__file__).resolve().parents[2] / "configs" / "night_api.example.yaml")


def test_an_unrevealed_season_config_has_no_seed_in_it(api_config: NightConfig) -> None:
    text = canonical_json(api_config, allow_float=True)
    assert f'"seed_env":"{SEED_VAR}"' in text and '"seed":null' in text


def test_the_seed_is_read_from_the_named_variable(
    api_raw: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = NightConfig.model_validate(env_seed(api_raw, seed_commitment(SEED)))
    night = config.night
    assert not [p for p in find_placeholders(config) if p.startswith("night.")]
    assert night.resolve_seed({SEED_VAR: SEED}) == SEED
    assert night.resolve_seed({SEED_VAR: f"  {SEED}\n"}) == SEED, "whitespace is removed"
    monkeypatch.setenv(SEED_VAR, SEED)
    assert night.resolve_seed() == SEED, "the default environment is the process environment"


def test_a_missing_variable_names_the_field_and_the_variable(
    api_raw: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    night = NightConfig.model_validate(env_seed(api_raw, seed_commitment(SEED))).night
    with pytest.raises(SeedResolutionError) as info:
        night.resolve_seed({})
    assert isinstance(info.value, ValueError)
    assert "night.seed_env" in str(info.value) and SEED_VAR in str(info.value)
    assert "not set" in str(info.value)
    with pytest.raises(SeedResolutionError, match="night.seed_env.*is empty"):
        night.resolve_seed({SEED_VAR: "  \n"})
    monkeypatch.delenv(SEED_VAR, raising=False)
    with pytest.raises(SeedResolutionError, match=SEED_VAR):
        night.resolve_seed()


def test_a_mismatched_commitment_is_refused_without_echoing_the_seed(
    api_raw: dict[str, Any],
) -> None:
    night = NightConfig.model_validate(env_seed(api_raw, seed_commitment("another-seed"))).night
    with pytest.raises(SeedResolutionError) as info:
        night.resolve_seed({SEED_VAR: SEED})
    message = str(info.value)
    assert "night.deal_seed_commitment" in message and SEED_VAR in message
    assert SEED not in message and seed_commitment(SEED) not in message


def test_a_placeholder_commitment_is_refused_when_the_seed_is_resolved(
    api_config: NightConfig,
) -> None:
    with pytest.raises(SeedResolutionError, match="night.deal_seed_commitment.*placeholder"):
        api_config.night.resolve_seed({SEED_VAR: SEED})


@pytest.mark.parametrize("value", ["has spaces", "x" * 129, "naïve", "semi;colon", "a/b"])
def test_an_invalid_seed_in_the_environment_is_refused_without_echo(
    api_raw: dict[str, Any], value: str
) -> None:
    night = NightConfig.model_validate(env_seed(api_raw)).night
    with pytest.raises(SeedResolutionError) as info:
        night.resolve_seed({SEED_VAR: value})
    assert "night.seed_env" in str(info.value) and "not a valid seed" in str(info.value)
    assert value not in str(info.value)


def test_a_seed_in_the_environment_does_not_change_the_config_hash(
    api_raw: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = NightConfig.model_validate(env_seed(api_raw, seed_commitment(SEED)))
    before = config_hash(config)
    monkeypatch.setenv(SEED_VAR, SEED)
    config.night.resolve_seed()
    assert config_hash(config) == before
    assert SEED not in canonical_json(config, allow_float=True)


def test_only_offline_example_configs_hold_an_inline_seed() -> None:
    root = Path(__file__).resolve().parents[2] / "configs"
    examples = sorted(root.glob("night_*.yaml"))
    assert len(examples) >= 2
    for path in examples:
        config = load_config(path)
        if config.night.seed is not None:
            assert config.is_offline, f"{path.name}: an inline seed needs a mock or test config"
        else:
            assert config.night.seed_env, path.name


def test_every_example_config_keeps_the_memory_section_off_but_the_memory_night() -> None:
    root = Path(__file__).resolve().parents[2] / "configs"
    names = sorted(path.name for path in root.glob("night_*.yaml"))
    assert "night_local_memory.yaml" in names and len(names) >= 5
    for name in names:
        expected = "plan_pledges" if name == "night_local_memory.yaml" else "off"
        assert load_config(root / name).night.memory == expected, name
