"""The act tool: strict-compatible schema, phase table and server-side validation."""

import copy
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from auction_night.contracts import (
    FORMAT_KEYS,
    MECHANISMS,
    PHASE_TABLE,
    PHASES,
    ActContext,
    act_input_schema,
    act_tool_anthropic,
    act_tool_fingerprint,
    act_tool_openai,
    canonical_json,
    sha256_digest,
    validate_act,
)
from auction_night.contracts.act import (
    FIELD_NAMES,
    MAX_AMOUNT,
    PLEDGE_FIELDS,
    extract_pledge,
    lot_phases,
    normalize_act,
)

ALWAYS_PRESENT = {"phase", "thought"}  # the only properties that are never null

VALID = {
    "plan": {
        "plan_priorities": [0, 1, 2, 3, 0, 1, 2, 3, 0, 1, 2, 3],
        "plan_budget": [0, 10, 20, 30, 0, 10, 20, 30, 0, 10, 20, 30],
        "plan_persona": "装作只对甜品感兴趣",
    },
    "talk": {"say": "", "guess_budget": 180},
    "english_init": {"exit_price": 120},
    "english_revise": {"exit_price": 140},
    "dutch": {"grab_price": 130},
    "sealed": {"bid": 0},
}


def call(phase: str, /, **fields: Any) -> dict[str, Any]:
    """A complete call for a phase: valid defaults, overridden by ``fields``."""
    args: dict[str, Any] = {"phase": phase, "thought": "好。", **copy.deepcopy(VALID[phase])}
    args.update(fields)
    return args


def codes(errors: list) -> dict[str, str]:
    return {error.field: error.code for error in errors}


def allowed_types(prop: dict[str, Any]) -> set[str]:
    """JSON types a property admits, whichever way nullability is written."""
    if "anyOf" in prop:
        return set().union(*(allowed_types(branch) for branch in prop["anyOf"]))
    kind = prop["type"]
    return set(kind) if isinstance(kind, list) else {kind}


# ------------------------------------------------------------------------------ schema

STYLES = ("any_of", "type_array")

# Keywords that strict schemas do not support: numeric, string and array constraints, recursion
# and the combinators other than anyOf and allOf. Property names are never keywords.
UNSUPPORTED_KEYWORDS = {
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern", "format",
    "minItems", "maxItems", "uniqueItems", "contains", "minContains", "maxContains",
    "prefixItems", "minProperties", "maxProperties", "patternProperties", "propertyNames",
    "oneOf", "not", "if", "then", "else", "dependentRequired", "dependentSchemas",
    "$ref", "$defs", "definitions",
}  # fmt: skip
SUPPORTED_KEYWORDS = {
    "type", "properties", "required", "additionalProperties", "enum", "items", "anyOf", "const",
}  # fmt: skip


def subschemas(node: Any, path: str = "#") -> Any:
    """Yield (path, schema) for every schema inside a schema. Property names are not keywords."""
    if not isinstance(node, dict):
        return
    yield path, node
    for name, child in node.get("properties", {}).items():
        yield from subschemas(child, f"{path}/properties/{name}")
    for key in ("items", "additionalProperties"):
        yield from subschemas(node.get(key), f"{path}/{key}")
    for key in ("anyOf", "allOf"):
        for index, child in enumerate(node.get(key, [])):
            yield from subschemas(child, f"{path}/{key}/{index}")


def strict_subset_problems(schema: dict[str, Any]) -> list[str]:
    """Why a schema leaves the subset that strict tool use supports. Empty when it is inside."""
    problems = []
    for path, node in subschemas(schema):
        for keyword in sorted(set(node) & UNSUPPORTED_KEYWORDS):
            problems.append(f"{path}: unsupported keyword {keyword}")
        for keyword in sorted(set(node) - UNSUPPORTED_KEYWORDS - SUPPORTED_KEYWORDS):
            problems.append(f"{path}: unknown keyword {keyword}")
        kind = node.get("type")
        kinds = kind if isinstance(kind, list) else [kind]
        if ("object" in kinds or "properties" in node) and node.get(
            "additionalProperties"
        ) is not False:
            problems.append(f"{path}: an object must set additionalProperties to false")
    return problems


def exported_schema(export: str) -> dict[str, Any]:
    if export == "anthropic":
        return act_tool_anthropic()["input_schema"]
    return act_tool_openai()["function"]["parameters"]


@pytest.mark.parametrize("style", STYLES)
def test_schema_is_strict_compatible(style: str) -> None:
    schema = act_input_schema(style)  # type: ignore[arg-type]
    props = schema["properties"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert list(schema["required"]) == list(props) == list(FIELD_NAMES), "all required, in order"
    for name, prop in props.items():
        types = allowed_types(prop)
        assert ("null" in types) == (name not in ALWAYS_PRESENT), name
        assert "object" not in types, "no nested objects"
    assert props["phase"]["enum"] == list(PHASES)
    for name in ("plan_priorities", "plan_budget"):
        assert allowed_types(props[name]) == {"array", "null"}
    assert strict_subset_problems(schema) == []


def test_nullable_forms_follow_the_endpoint_family() -> None:
    anthropic = act_tool_anthropic()["input_schema"]["properties"]
    openai = act_tool_openai()["function"]["parameters"]["properties"]
    for name in FIELD_NAMES:
        if name in ALWAYS_PRESENT:
            assert anthropic[name] == openai[name] and "anyOf" not in anthropic[name]
            continue
        assert set(anthropic[name]) == {"anyOf"}, "Anthropic: anyOf with a null branch"
        assert anthropic[name]["anyOf"][-1] == {"type": "null"}
        assert isinstance(openai[name]["type"], list), "OpenAI-compatible: a type array"
        assert openai[name]["type"][-1] == "null" and "anyOf" not in openai[name]
    assert anthropic["pledge_kind"]["anyOf"][0] == {
        "type": "string",
        "enum": ["no_bid", "max_bid", "let_win"],
    }
    assert openai["pledge_kind"]["enum"] == ["no_bid", "max_bid", "let_win", None]
    assert anthropic["plan_budget"]["anyOf"][0] == {"type": "array", "items": {"type": "integer"}}
    for _, node in subschemas(act_tool_anthropic()["input_schema"]):
        assert not isinstance(node.get("type"), list), "no type arrays in the Anthropic export"


@pytest.mark.parametrize("export", ["anthropic", "openai"])
def test_exports_stay_inside_the_strict_schema_subset(export: str, repo_root: Any) -> None:
    import json

    assert strict_subset_problems(exported_schema(export)) == []
    on_disk = json.loads((repo_root / "schemas" / f"act_tool.{export}.v1.json").read_text("utf-8"))
    committed = (
        on_disk["input_schema"] if export == "anthropic" else on_disk["function"]["parameters"]
    )
    assert strict_subset_problems(committed) == []
    assert committed == exported_schema(export)
    assert "$ref" not in json.dumps(committed), "the schema is flat: no references, no recursion"


@pytest.mark.parametrize("keyword", sorted(UNSUPPORTED_KEYWORDS))
def test_the_checker_flags_every_unsupported_keyword(keyword: str) -> None:
    schema = {"type": "object", "additionalProperties": False, "required": ["a"],
              "properties": {"a": {"type": "integer", keyword: 1}}}  # fmt: skip
    assert any(keyword in problem for problem in strict_subset_problems(schema))


def test_the_checker_flags_open_objects_and_ignores_property_names() -> None:
    open_object = {"type": "object", "additionalProperties": False, "required": ["o"],
                   "properties": {"o": {"type": "object", "properties": {}}}}  # fmt: skip
    assert strict_subset_problems(open_object) == [
        "#/properties/o: an object must set additionalProperties to false"
    ]
    nullable_object = {"type": ["object", "null"], "properties": {}}
    assert len(strict_subset_problems(nullable_object)) == 1
    in_any_of = {"anyOf": [{"type": "object", "properties": {}}, {"type": "null"}]}
    assert len(strict_subset_problems(in_any_of)) == 1
    named_like_keywords = {"type": "object", "additionalProperties": False,
                           "required": ["format", "pattern"],
                           "properties": {"format": {"type": "string"},
                                          "pattern": {"type": "integer"}}}  # fmt: skip
    assert strict_subset_problems(named_like_keywords) == []


def test_property_order_is_the_documented_one() -> None:
    assert FIELD_NAMES == (
        "phase", "say", "pledge_kind", "pledge_lot", "pledge_to", "pledge_amount",
        "guess_budget", "exit_price", "grab_price", "bid", "plan_priorities", "plan_budget",
        "plan_persona", "thought",
    )  # fmt: skip


def test_any_of_style_admits_the_same_types() -> None:
    plain = act_input_schema("type_array")["properties"]
    wrapped = act_input_schema("any_of")["properties"]
    assert list(plain) == list(wrapped)
    for name in plain:
        assert allowed_types(plain[name]) == allowed_types(wrapped[name]), name
    assert wrapped["pledge_kind"]["anyOf"][0]["enum"] == ["no_bid", "max_bid", "let_win"]
    assert wrapped["say"] == {"anyOf": [{"type": "string"}, {"type": "null"}]}
    assert wrapped["phase"] == plain["phase"], "non-null properties are untouched"


@pytest.mark.parametrize("style", STYLES)
def test_schema_functions_return_independent_copies(style: str) -> None:
    first = act_input_schema(style)  # type: ignore[arg-type]
    first["properties"]["bid"].clear()
    first["required"].clear()
    second = act_input_schema(style)  # type: ignore[arg-type]
    assert allowed_types(second["properties"]["bid"]) == {"integer", "null"}
    assert second["required"] == list(FIELD_NAMES)


def test_the_style_must_be_stated() -> None:
    with pytest.raises(TypeError):
        act_input_schema()  # type: ignore[call-arg]


# ---------------------------------------------------------------------------- tool defs


def test_anthropic_definition() -> None:
    tool = act_tool_anthropic()
    assert list(tool) == ["name", "description", "strict", "input_schema"]
    assert tool["name"] == "act" and tool["strict"] is True
    assert tool["input_schema"] == act_input_schema("any_of")
    assert act_tool_anthropic("type_array")["input_schema"] == act_input_schema("type_array")


def test_openai_definition() -> None:
    tool = act_tool_openai()
    assert tool["type"] == "function"
    function = tool["function"]
    assert function["name"] == "act" and function["parameters"] == act_input_schema("type_array")
    assert "strict" not in function, "strict follows the endpoint capability"
    assert act_tool_openai(strict=True)["function"]["strict"] is True
    assert act_tool_openai(nullable_style="any_of")["function"]["parameters"] == act_input_schema(
        "any_of"
    )


def test_fingerprint_is_the_digest_of_the_canonical_definition() -> None:
    expected = sha256_digest(canonical_json(act_tool_anthropic()))
    assert act_tool_fingerprint() == expected
    changed = act_tool_anthropic("type_array")
    assert sha256_digest(canonical_json(changed)) != expected


# ----------------------------------------------------------------------------- phase table


def test_phase_table_covers_every_phase() -> None:
    assert set(PHASE_TABLE) == set(PHASES)
    assert set(PHASES) == {"plan", "talk", "english_init", "english_revise", "dutch", "sealed"}
    for phase, spec in PHASE_TABLE.items():
        assert spec.phase == phase
        assert spec.instruction_key == f"phase.{phase}"
        assert "thought" in spec.required
        assert not set(spec.required) & set(spec.optional)
        assert set(spec.required) | set(spec.optional) <= set(FIELD_NAMES) - {"phase"}


def test_every_field_is_used_by_some_phase() -> None:
    used = {"phase"}
    for spec in PHASE_TABLE.values():
        used |= set(spec.required) | set(spec.optional)
    assert used == set(FIELD_NAMES)


def test_phase_table_covers_the_phases_of_all_six_formats() -> None:
    seen = {"plan"}
    for key in FORMAT_KEYS:
        phases = lot_phases(key)
        assert phases[0] == "talk" and phases[1:] == MECHANISMS[key].bid_phases
        assert set(phases) <= set(PHASE_TABLE), key
        seen |= set(phases)
    assert seen == set(PHASES), "no phase is unused and none is missing"


@pytest.mark.parametrize(
    ("phase", "field"),
    [
        ("english_init", "exit_price"),
        ("english_revise", "exit_price"),
        ("dutch", "grab_price"),
        ("sealed", "bid"),
    ],
)
def test_each_bid_phase_needs_its_own_value_field(phase: str, field: str) -> None:
    assert PHASE_TABLE[phase].required == (field, "thought")  # type: ignore[index]


def test_plan_and_talk_requirements() -> None:
    assert PHASE_TABLE["plan"].required == (
        "plan_priorities", "plan_budget", "plan_persona", "thought",
    )  # fmt: skip
    assert PHASE_TABLE["talk"].required == ("say", "guess_budget", "thought")
    assert PHASE_TABLE["talk"].optional == PLEDGE_FIELDS


def test_formats_use_the_expected_bid_phases() -> None:
    assert lot_phases("english") == ("talk", "english_init", "english_revise")
    assert lot_phases("dutch") == ("talk", "dutch")
    for key in ("sealed_first", "sealed_second", "all_pay", "blind_box"):
        assert lot_phases(key) == ("talk", "sealed")


# ---------------------------------------------------------------------------- validation


@pytest.mark.parametrize("phase", PHASES)
def test_a_complete_call_is_valid(phase: str) -> None:
    assert validate_act(call(phase), phase) == []  # type: ignore[arg-type]


@pytest.mark.parametrize("phase", PHASES)
def test_every_required_field_is_checked(phase: str) -> None:
    for name in PHASE_TABLE[phase].required:  # type: ignore[index]
        for broken in ({name: None}, {}):
            args = call(phase)
            args.pop(name)
            args.update(broken)
            assert codes(validate_act(args, phase)) == {name: "missing_required"}  # type: ignore[arg-type]


def test_phase_must_match() -> None:
    assert codes(validate_act(call("sealed", phase="dutch"), "sealed")) == {
        "phase": "phase_mismatch"
    }
    assert codes(validate_act(call("sealed", phase="auction"), "sealed")) == {
        "phase": "phase_mismatch"
    }
    assert codes(validate_act(call("sealed", phase=None), "sealed")) == {
        "phase": "missing_required"
    }
    assert codes(validate_act(call("sealed", phase=5), "sealed")) == {"phase": "wrong_type"}
    args = call("sealed")
    del args["phase"]
    assert codes(validate_act(args, "sealed")) == {"phase": "missing_required"}


@pytest.mark.parametrize(
    ("phase", "fields", "expected"),
    [
        ("sealed", {"bid": -1}, {"bid": "out_of_range"}),
        ("sealed", {"bid": MAX_AMOUNT + 1}, {"bid": "out_of_range"}),
        ("sealed", {"bid": "120"}, {"bid": "wrong_type"}),
        ("sealed", {"bid": 120.0}, {"bid": "wrong_type"}),
        ("sealed", {"bid": True}, {"bid": "wrong_type"}),
        ("sealed", {"thought": 5}, {"thought": "wrong_type"}),
        ("dutch", {"grab_price": [130]}, {"grab_price": "wrong_type"}),
        ("english_revise", {"exit_price": -5}, {"exit_price": "out_of_range"}),
        ("talk", {"say": None}, {"say": "missing_required"}),
        ("talk", {"say": 7}, {"say": "wrong_type"}),
        ("talk", {"guess_budget": -2}, {"guess_budget": "out_of_range"}),
        ("plan", {"plan_priorities": [0] * 11}, {"plan_priorities": "out_of_range"}),
        ("plan", {"plan_priorities": [4] + [0] * 11}, {"plan_priorities": "out_of_range"}),
        ("plan", {"plan_priorities": [-1] + [0] * 11}, {"plan_priorities": "out_of_range"}),
        ("plan", {"plan_priorities": [0.5] * 12}, {"plan_priorities": "wrong_type"}),
        ("plan", {"plan_priorities": "0123"}, {"plan_priorities": "wrong_type"}),
        ("plan", {"plan_priorities": [True] * 12}, {"plan_priorities": "wrong_type"}),
        ("plan", {"plan_budget": [-1] + [0] * 11}, {"plan_budget": "out_of_range"}),
        ("plan", {"plan_budget": [0] * 13}, {"plan_budget": "out_of_range"}),
        ("plan", {"plan_persona": None}, {"plan_persona": "missing_required"}),
    ],
)
def test_wrong_values_are_reported_per_field(
    phase: str, fields: dict[str, Any], expected: dict[str, str]
) -> None:
    assert codes(validate_act(call(phase, **fields), phase)) == expected  # type: ignore[arg-type]


def test_a_bid_above_the_budget_is_not_an_error() -> None:
    assert validate_act(call("sealed", bid=450), "sealed") == []
    assert validate_act(call("english_revise", exit_price=10**5), "english_revise") == []


def test_long_strings_are_the_sanitisers_business() -> None:
    assert validate_act(call("talk", say="长" * 5000), "talk") == []
    assert validate_act(call("sealed", thought="x" * 100000), "sealed") == []


def test_fields_of_other_phases_are_ignored() -> None:
    noisy = call("sealed", say=5, exit_price="x", plan_budget="y", pledge_kind=7)
    assert validate_act(noisy, "sealed") == []
    assert normalize_act(noisy, "sealed") == {
        **dict.fromkeys(FIELD_NAMES),
        "phase": "sealed",
        "bid": 0,
        "thought": "好。",
    }
    assert list(normalize_act(noisy, "sealed")) == list(FIELD_NAMES)


def test_errors_come_in_field_order() -> None:
    args = call("talk", say=None, guess_budget=-1, thought=None)
    assert [e.field for e in validate_act(args, "talk")] == ["say", "guess_budget", "thought"]


def test_unknown_phase_argument_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        validate_act({}, "bidding")  # type: ignore[arg-type]


# ------------------------------------------------------------------------------ pledges


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, {}),
        ({"pledge_kind": "no_bid", "pledge_lot": 3}, {}),
        ({"pledge_kind": "max_bid", "pledge_lot": 3, "pledge_amount": 80}, {}),
        ({"pledge_kind": "let_win", "pledge_lot": 3, "pledge_to": 4}, {}),
        ({"pledge_kind": "no_bid"}, {"pledge_lot": "inconsistent_pledge"}),
        ({"pledge_kind": "max_bid", "pledge_lot": 3}, {"pledge_amount": "inconsistent_pledge"}),
        ({"pledge_kind": "let_win", "pledge_lot": 3}, {"pledge_to": "inconsistent_pledge"}),
        (
            {"pledge_kind": "no_bid", "pledge_lot": 3, "pledge_amount": 5},
            {"pledge_amount": "inconsistent_pledge"},
        ),
        (
            {"pledge_kind": "no_bid", "pledge_lot": 3, "pledge_to": 5},
            {"pledge_to": "inconsistent_pledge"},
        ),
        ({"pledge_lot": 3}, {"pledge_kind": "inconsistent_pledge"}),
        ({"pledge_kind": "swear", "pledge_lot": 3}, {"pledge_kind": "out_of_range"}),
        ({"pledge_kind": 7, "pledge_lot": 3}, {"pledge_kind": "wrong_type"}),
        ({"pledge_kind": "no_bid", "pledge_lot": 13}, {"pledge_lot": "out_of_range"}),
        ({"pledge_kind": "no_bid", "pledge_lot": 0}, {"pledge_lot": "out_of_range"}),
        (
            {"pledge_kind": "let_win", "pledge_lot": 3, "pledge_to": 9},
            {"pledge_to": "out_of_range"},
        ),
    ],
)
def test_pledge_fields(fields: dict[str, Any], expected: dict[str, str]) -> None:
    assert codes(validate_act(call("talk", **fields), "talk")) == expected


def test_pledges_are_only_looked_at_in_the_talk_phase() -> None:
    assert validate_act(call("sealed", pledge_kind="swear", pledge_lot=99), "sealed") == []


def test_context_narrows_the_ranges() -> None:
    pledge = {"pledge_kind": "let_win", "pledge_lot": 5, "pledge_to": 2}
    here = ActContext(current_lot=5, self_seat=2)
    assert codes(validate_act(call("talk", **pledge), "talk", here)) == {
        "pledge_to": "out_of_range"
    }
    past = ActContext(current_lot=6, self_seat=1)
    assert codes(validate_act(call("talk", **pledge), "talk", past)) == {
        "pledge_lot": "out_of_range"
    }
    assert (
        validate_act(call("talk", **pledge), "talk", ActContext(current_lot=5, self_seat=1)) == []
    )
    small = ActContext(n_lots=4, n_seats=4)
    assert codes(validate_act(call("talk", pledge_kind="no_bid", pledge_lot=5), "talk", small)) == {
        "pledge_lot": "out_of_range"
    }
    plan = call("plan", plan_priorities=[1] * 18, plan_budget=[5] * 18)
    assert validate_act(plan, "plan", ActContext(n_lots=18)) == []
    assert codes(validate_act(plan, "plan")) == {
        "plan_priorities": "out_of_range",
        "plan_budget": "out_of_range",
    }


def test_extract_pledge_builds_the_public_record() -> None:
    assert extract_pledge(call("talk")) is None
    pledge = extract_pledge(call("talk", pledge_kind="max_bid", pledge_lot=4, pledge_amount=90))
    assert pledge is not None and (pledge.kind, pledge.lot, pledge.amount) == ("max_bid", 4, 90)
    gift = extract_pledge(call("talk", pledge_kind="let_win", pledge_lot=4, pledge_to=6))
    assert gift is not None and gift.to == 6 and gift.amount is None


# --------------------------------------------------------------------------- properties

JSONISH = (
    st.none()
    | st.booleans()
    | st.integers(-(10**12), 10**12)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=20)
    | st.lists(st.none() | st.booleans() | st.integers(-5, 5) | st.text(max_size=3), max_size=14)
)


@settings(max_examples=200, deadline=None)
@given(
    args=st.dictionaries(st.sampled_from(FIELD_NAMES), JSONISH, max_size=14),
    phase=st.sampled_from(PHASES),
)
def test_validation_never_raises_on_arbitrary_input(args: dict[str, Any], phase: str) -> None:
    errors = validate_act(args, phase)  # type: ignore[arg-type]
    assert all(error.field in FIELD_NAMES for error in errors)
    assert len({(error.field, error.code) for error in errors}) == len(errors)
    normal = normalize_act(args, phase)  # type: ignore[arg-type]
    assert list(normal) == list(FIELD_NAMES)


@settings(max_examples=200, deadline=None)
@given(
    phase=st.sampled_from(PHASES),
    number=st.integers(0, MAX_AMOUNT),
    priorities=st.lists(st.integers(0, 3), min_size=12, max_size=12),
    text=st.text(max_size=50),
)
def test_well_formed_calls_are_always_valid(
    phase: str, number: int, priorities: list[int], text: str
) -> None:
    values = {
        "plan": {"plan_priorities": priorities, "plan_budget": [number] * 12, "plan_persona": text},
        "talk": {"say": text, "guess_budget": number},
        "english_init": {"exit_price": number},
        "english_revise": {"exit_price": number},
        "dutch": {"grab_price": number},
        "sealed": {"bid": number},
    }[phase]
    args = {"phase": phase, "thought": text, **values}
    assert validate_act(args, phase) == []  # type: ignore[arg-type]
