"""Exported JSON Schemas and the contract documentation stay in step with the models."""

import json
from pathlib import Path
from typing import Any

import pytest

from auction_night.contracts import (
    EVENT_TYPES,
    FORMAT_KEYS,
    PHASES,
    act_tool_anthropic,
    act_tool_openai,
    schema_texts,
    write_schemas,
)
from auction_night.contracts.act import FIELD_NAMES

HOW = (
    "run: python -c \"from auction_night.contracts import write_schemas; write_schemas('schemas')\""
)
BANNED = ("welfare", "efficiency ratio", "price of anarchy", "PoA", "效率", "福利")
SECRET_PROPERTIES = {"api_key", "apikey", "password", "secret", "token", "access_token"}
EXPECTED_FILES = {
    "config.v1.schema.json",
    "events.v1.schema.json",
    "replay.v1.schema.json",
    "prices.v1.schema.json",
    "act_tool.anthropic.v1.json",
    "act_tool.openai.v1.json",
}


def load(repo_root: Path, name: str) -> Any:
    return json.loads((repo_root / "schemas" / name).read_text(encoding="utf-8"))


def walk(node: Any, path: str = "") -> Any:
    """Yield (path, node) for every dict inside a schema."""
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from walk(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk(value, f"{path}[{index}]")


def test_committed_schemas_equal_a_fresh_export(repo_root: Path) -> None:
    fresh = schema_texts()
    assert set(fresh) == EXPECTED_FILES
    committed = {p.name for p in (repo_root / "schemas").iterdir() if p.is_file()}
    assert committed == EXPECTED_FILES, f"stale or missing schema files; {HOW}"
    for name, text in fresh.items():
        on_disk = (repo_root / "schemas" / name).read_text(encoding="utf-8")
        assert on_disk == text, f"{name} is out of date; {HOW}"


def test_write_schemas_reproduces_the_files(tmp_path: Path) -> None:
    written = write_schemas(tmp_path / "nested" / "out")
    assert {p.name for p in written} == EXPECTED_FILES
    for path in written:
        assert path.read_text(encoding="utf-8") == schema_texts()[path.name]


def test_schema_export_is_deterministic() -> None:
    assert schema_texts() == schema_texts()


@pytest.mark.parametrize("name", sorted(EXPECTED_FILES - {"act_tool.openai.v1.json"}))
def test_files_end_with_one_newline_and_are_json(repo_root: Path, name: str) -> None:
    text = (repo_root / "schemas" / name).read_text(encoding="utf-8")
    assert text.endswith("}\n") and not text.endswith("\n\n")
    json.loads(text)


def test_json_schemas_declare_the_dialect(repo_root: Path) -> None:
    for name in EXPECTED_FILES:
        if "schema.json" in name:
            assert load(repo_root, name)["$schema"].endswith("2020-12/schema")


def test_events_and_replay_schemas_contain_no_float_type(repo_root: Path) -> None:
    for name in ("events.v1.schema.json", "replay.v1.schema.json"):
        for path, node in walk(load(repo_root, name)):
            kinds = node.get("type")
            kinds = kinds if isinstance(kinds, list) else [kinds]
            assert "number" not in kinds, f"{name}{path}: amounts and ratios are integers"


def test_events_schema_lists_every_type_and_requires_the_discriminator(repo_root: Path) -> None:
    schema = load(repo_root, "events.v1.schema.json")
    assert schema["discriminator"]["propertyName"] == "type"
    assert set(schema["discriminator"]["mapping"]) == set(EVENT_TYPES)
    classes = [d for n, d in schema["$defs"].items() if n.endswith("Event")]
    assert len(classes) == len(EVENT_TYPES) == 19
    for definition in classes:
        assert definition["additionalProperties"] is False
        assert {"seq", "vis", "data", "type"} <= set(definition["required"])
        assert definition["properties"]["vis"]["pattern"].startswith(
            "^(public|audience|system|seat:"
        )


def test_replay_schema_requires_its_identity_and_main_parts(repo_root: Path) -> None:
    schema = load(repo_root, "replay.v1.schema.json")
    assert schema["properties"]["schema"]["const"] == "auction-night/replay@1"
    assert {
        "schema",
        "night",
        "seats",
        "catalog",
        "timeline",
        "reveal",
        "scores",
        "manifest",
    } <= set(schema["required"])
    assert schema["additionalProperties"] is False


def test_replay_schema_has_no_room_for_the_canary(repo_root: Path) -> None:
    assert "canary" not in (repo_root / "schemas" / "replay.v1.schema.json").read_text("utf-8")


def test_config_schema_cannot_hold_a_secret(repo_root: Path) -> None:
    schema = load(repo_root, "config.v1.schema.json")
    names = {key.lower() for _, node in walk(schema) for key in node.get("properties", {})}
    assert not names & SECRET_PROPERTIES
    for endpoint_key in ("api_key_env", "base_url_env"):
        assert endpoint_key in names
    env = schema["$defs"]["EndpointConfig"]["properties"]["api_key_env"]
    assert "pattern" in json.dumps(env)


def test_config_schema_says_exactly_one_of_seed_and_seed_env(repo_root: Path) -> None:
    night = load(repo_root, "config.v1.schema.json")["$defs"]["NightSettings"]
    assert night["oneOf"] == [{"required": ["seed"]}, {"required": ["seed_env"]}]
    assert "id" in night["required"] and not {"seed", "seed_env"} & set(night["required"])
    assert night["properties"]["seed_env"]["anyOf"][0]["pattern"] == "^[A-Z][A-Z0-9_]{0,63}$"


def test_tool_files_match_the_functions(repo_root: Path) -> None:
    anthropic = load(repo_root, "act_tool.anthropic.v1.json")
    assert anthropic == act_tool_anthropic()
    assert list(anthropic["input_schema"]["properties"]) == list(FIELD_NAMES), "order is kept"
    assert load(repo_root, "act_tool.openai.v1.json") == act_tool_openai()


def test_no_banned_vocabulary_in_exported_files() -> None:
    for name, text in schema_texts().items():
        for word in BANNED:
            assert word not in text, f"{name} contains {word!r}"


# ---------------------------------------------------------------------------------- docs


@pytest.fixture(scope="module")
def doc(repo_root: Path) -> str:
    return (repo_root / "docs" / "contracts.md").read_text(encoding="utf-8")


def test_docs_name_every_contract_item(doc: str) -> None:
    for group in (EVENT_TYPES, FORMAT_KEYS, PHASES, FIELD_NAMES):
        for name in group:
            assert f"`{name}`" in doc, f"docs/contracts.md does not mention {name}"
    for name in (
        "canonical_json",
        "visible_to",
        "validate_act",
        "find_placeholders",
        "write_schemas",
        "resolve_seed",
        "seed_env",
        "SeedResolutionError",
        "oneOf",
    ):
        assert name in doc, f"docs/contracts.md does not mention {name}"
    assert "may be added" in doc and "Nothing may be changed, renamed or removed" in doc
    assert '["integer", "null"]' in doc and "`anyOf`" in doc
    assert "Committed season configurations use `seed_env`" in doc
    assert "Inline `seed` only appears in mock or test configurations" in doc


def test_docs_are_public_copy(doc: str, repo_root: Path) -> None:
    for word in BANNED:
        assert word not in doc
    assert "/" + "data/" not in doc and "/" + "home/" not in doc
    for module in ("mechanism", "config", "events", "act", "replay"):
        assert (repo_root / "src" / "auction_night" / "contracts" / f"{module}.py").exists()
        assert f"`{module}.py`" in doc or f"({module}.py)" in doc, module
