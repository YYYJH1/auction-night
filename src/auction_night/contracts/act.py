"""The single fixed ``act`` tool used in every phase of a night.

All phases share one tool whose fields are the union over phases. A field that a phase does
not use is sent as null, so one tool definition (and therefore one cached prompt prefix)
serves the whole night, and a strict schema can list every property as required.

Nullable properties are written in one of two ways. The Anthropic export uses ``anyOf`` with
a null branch, because the strict tool use documentation lists ``anyOf`` and null as supported
and says nothing about type arrays. The OpenAI-compatible export keeps type arrays such as
``["integer", "null"]``. Both forms admit the same values, and both stay inside the keyword
subset that strict schemas support: no numeric, string or array constraints, no recursion.
"""

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Literal

from auction_night.contracts.events import PRIORITY_MAX, Pledge, canonical_json, sha256_digest
from auction_night.contracts.mechanism import (
    MAX_SEATS,
    MECHANISMS,
    PHASES,
    ContractModel,
    FormatKey,
    Phase,
)

ACT_TOOL_NAME: Final = "act"
ACT_TOOL_DESCRIPTION: Final = (
    "提交本次行动。phase 必须等于消息里【现在要做】给出的阶段；用不到的字段填 null。"
)
PLEDGE_KINDS: Final[tuple[str, ...]] = ("no_bid", "max_bid", "let_win")
MAX_AMOUNT: Final = 1_000_000  # sanity ceiling for any coin amount accepted from a model
DEFAULT_LOTS: Final = 12

NullableStyle = Literal["type_array", "any_of"]
ANTHROPIC_NULLABLE_STYLE: Final[NullableStyle] = "any_of"
OPENAI_NULLABLE_STYLE: Final[NullableStyle] = "type_array"
FieldKind = Literal["phase", "string", "integer", "integer_list", "enum"]


@dataclass(frozen=True)
class FieldSpec:
    """One property of the tool: its name, value kind and non-null JSON schema fragment."""

    name: str
    kind: FieldKind
    nullable: bool
    base: Mapping[str, Any]


def _integer(name: str) -> FieldSpec:
    return FieldSpec(name, "integer", True, MappingProxyType({"type": "integer"}))


def _integer_list(name: str) -> FieldSpec:
    base = {"type": "array", "items": {"type": "integer"}}
    return FieldSpec(name, "integer_list", True, MappingProxyType(base))


# The order is part of the contract: it fixes the bytes of the tool definition, and therefore
# the cached prompt prefix.
ACT_FIELDS: Final[tuple[FieldSpec, ...]] = (
    FieldSpec("phase", "phase", False, MappingProxyType({"type": "string", "enum": list(PHASES)})),
    FieldSpec("say", "string", True, MappingProxyType({"type": "string"})),
    FieldSpec(
        "pledge_kind",
        "enum",
        True,
        MappingProxyType({"type": "string", "enum": list(PLEDGE_KINDS)}),
    ),
    _integer("pledge_lot"),
    _integer("pledge_to"),
    _integer("pledge_amount"),
    _integer("guess_budget"),
    _integer("exit_price"),
    _integer("grab_price"),
    _integer("bid"),
    _integer_list("plan_priorities"),
    _integer_list("plan_budget"),
    FieldSpec("plan_persona", "string", True, MappingProxyType({"type": "string"})),
    FieldSpec("thought", "string", False, MappingProxyType({"type": "string"})),
)
FIELD_NAMES: Final[tuple[str, ...]] = tuple(field.name for field in ACT_FIELDS)
_FIELD_INDEX: Final[Mapping[str, int]] = {name: i for i, name in enumerate(FIELD_NAMES)}
_FIELDS_BY_NAME: Final[Mapping[str, FieldSpec]] = {field.name: field for field in ACT_FIELDS}
PLEDGE_FIELDS: Final[tuple[str, ...]] = ("pledge_kind", "pledge_lot", "pledge_to", "pledge_amount")


class PhaseSpec(ContractModel):
    """Which fields a phase needs. Every other field is sent as null and ignored."""

    phase: Phase
    required: tuple[str, ...]
    optional: tuple[str, ...] = ()
    instruction_key: str


def _spec(phase: Phase, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> PhaseSpec:
    return PhaseSpec(
        phase=phase, required=required, optional=optional, instruction_key=f"phase.{phase}"
    )


PHASE_TABLE: Final[Mapping[Phase, PhaseSpec]] = MappingProxyType(
    {
        "plan": _spec("plan", ("plan_priorities", "plan_budget", "plan_persona", "thought")),
        "talk": _spec("talk", ("say", "guess_budget", "thought"), PLEDGE_FIELDS),
        "english_init": _spec("english_init", ("exit_price", "thought")),
        "english_revise": _spec("english_revise", ("exit_price", "thought")),
        "dutch": _spec("dutch", ("grab_price", "thought")),
        "sealed": _spec("sealed", ("bid", "thought")),
    }
)


def lot_phases(key: FormatKey) -> tuple[Phase, ...]:
    """Phases a model seat acts in during one lot of the given format."""
    return ("talk", *MECHANISMS[key].bid_phases)


# --------------------------------------------------------------------- tool definition


def _nullable(base: Mapping[str, Any], style: NullableStyle) -> dict[str, Any]:
    plain = copy.deepcopy(dict(base))
    if style == "any_of":
        return {"anyOf": [plain, {"type": "null"}]}
    plain["type"] = [plain["type"], "null"]
    if "enum" in plain:
        plain["enum"] = [*plain["enum"], None]
    return plain


def act_input_schema(nullable_style: NullableStyle) -> dict[str, Any]:
    """The strict-compatible input schema: every property required, no extra properties.

    The style has no default so that every caller states which form its endpoint takes:
    ``ANTHROPIC_NULLABLE_STYLE`` or ``OPENAI_NULLABLE_STYLE``.
    """
    properties = {
        field.name: _nullable(field.base, nullable_style)
        if field.nullable
        else copy.deepcopy(dict(field.base))
        for field in ACT_FIELDS
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(FIELD_NAMES),
        "properties": properties,
    }


def act_tool_anthropic(nullable_style: NullableStyle = ANTHROPIC_NULLABLE_STYLE) -> dict[str, Any]:
    """Tool definition for the Anthropic Messages API (``input_schema``, ``strict: true``).

    Nullable properties use ``anyOf`` with a null branch by default.
    """
    return {
        "name": ACT_TOOL_NAME,
        "description": ACT_TOOL_DESCRIPTION,
        "strict": True,
        "input_schema": act_input_schema(nullable_style),
    }


def act_tool_openai(
    *, strict: bool = False, nullable_style: NullableStyle = OPENAI_NULLABLE_STYLE
) -> dict[str, Any]:
    """Tool definition for OpenAI-compatible chat endpoints (``parameters``).

    Nullable properties use type arrays by default. ``strict`` should follow the endpoint
    capability ``supports_strict_tools``.
    """
    function: dict[str, Any] = {
        "name": ACT_TOOL_NAME,
        "description": ACT_TOOL_DESCRIPTION,
        "parameters": act_input_schema(nullable_style),
    }
    if strict:
        function["strict"] = True
    return {"type": "function", "function": function}


def act_tool_fingerprint() -> str:
    """Digest of the default Anthropic tool definition. It enters the request hash."""
    return sha256_digest(canonical_json(act_tool_anthropic()))


# ------------------------------------------------------------------------- validation

ErrorCode = Literal[
    "missing_required", "wrong_type", "out_of_range", "phase_mismatch", "inconsistent_pledge"
]


@dataclass(frozen=True)
class FieldError:
    """One problem with one field of a parsed ``act`` call."""

    field: str
    code: ErrorCode
    message: str


@dataclass(frozen=True)
class ActContext:
    """Facts the validator needs about the current call. All have safe defaults."""

    n_lots: int = DEFAULT_LOTS
    n_seats: int = MAX_SEATS
    self_seat: int | None = None
    current_lot: int | None = None
    max_amount: int = MAX_AMOUNT


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_value(name: str, value: Any, ctx: ActContext) -> list[FieldError]:
    """Type and range check for one non-null value. Length limits are the sanitiser's job."""
    spec = _FIELDS_BY_NAME[name]
    if spec.kind == "string":
        if isinstance(value, str):
            return []
        return [FieldError(name, "wrong_type", "expected a string")]
    if spec.kind == "enum":
        if not isinstance(value, str):
            return [FieldError(name, "wrong_type", "expected a string")]
        if value not in PLEDGE_KINDS:
            return [FieldError(name, "out_of_range", f"expected one of {list(PLEDGE_KINDS)}")]
        return []
    if spec.kind == "integer_list":
        return _check_list(name, value, ctx)
    if not _is_int(value):
        return [FieldError(name, "wrong_type", "expected an integer")]
    low, high = 0, ctx.max_amount
    if name == "pledge_lot":
        low, high = max(1, ctx.current_lot or 1), ctx.n_lots
    elif name == "pledge_to":
        low, high = 1, ctx.n_seats
        if ctx.self_seat is not None and value == ctx.self_seat:
            return [FieldError(name, "out_of_range", "cannot pledge to yourself")]
    if not low <= value <= high:
        return [FieldError(name, "out_of_range", f"expected {low}..{high}, got {value}")]
    return []


def _check_list(name: str, value: Any, ctx: ActContext) -> list[FieldError]:
    if not isinstance(value, list | tuple):
        return [FieldError(name, "wrong_type", "expected a list of integers")]
    if not all(_is_int(item) for item in value):
        return [FieldError(name, "wrong_type", "expected a list of integers")]
    if len(value) != ctx.n_lots:
        return [FieldError(name, "out_of_range", f"expected {ctx.n_lots} entries")]
    high = PRIORITY_MAX if name == "plan_priorities" else ctx.max_amount
    if any(not 0 <= item <= high for item in value):
        return [FieldError(name, "out_of_range", f"entries must lie in 0..{high}")]
    return []


def _check_pledge(args: Mapping[str, Any], ctx: ActContext) -> list[FieldError]:
    values = {name: args.get(name) for name in PLEDGE_FIELDS}
    if all(value is None for value in values.values()):
        return []
    kind = values["pledge_kind"]
    if kind is None:
        return [FieldError("pledge_kind", "inconsistent_pledge", "pledge fields need a kind")]
    if kind not in PLEDGE_KINDS:
        return []  # already reported as out of range
    extra = {"max_bid": "pledge_amount", "let_win": "pledge_to"}.get(kind)
    needs = {"pledge_lot", extra} if extra else {"pledge_lot"}
    errors = []
    for name in PLEDGE_FIELDS[1:]:
        present = values[name] is not None
        if name in needs and not present:
            errors.append(FieldError(name, "inconsistent_pledge", f"{kind} needs {name}"))
        elif name not in needs and present:
            errors.append(FieldError(name, "inconsistent_pledge", f"{kind} does not use {name}"))
    return errors


def validate_act(
    args: Mapping[str, Any], phase: Phase, ctx: ActContext | None = None
) -> list[FieldError]:
    """Check a parsed ``act`` argument dict against the phase table.

    Returns field errors in field order, or an empty list when the call is valid. Fields a
    phase does not use are ignored whatever they hold. A bid or exit price above the
    remaining budget is not an error here: the engine clamps it. Strings are not
    length-checked here: the sanitiser truncates them.
    """
    if phase not in PHASE_TABLE:
        raise ValueError(f"unknown phase {phase!r}")
    ctx = ctx or ActContext()
    spec = PHASE_TABLE[phase]
    errors: list[FieldError] = []

    claimed = args.get("phase")
    if claimed is None:
        errors.append(FieldError("phase", "missing_required", "phase is required"))
    elif not isinstance(claimed, str):
        errors.append(FieldError("phase", "wrong_type", "expected a string"))
    elif claimed != phase:
        errors.append(FieldError("phase", "phase_mismatch", f"expected {phase!r}, got {claimed!r}"))

    for name in (*spec.required, *spec.optional):
        value = args.get(name)
        if value is None:
            if name in spec.required:
                errors.append(FieldError(name, "missing_required", f"{name} is required"))
        else:
            errors.extend(_check_value(name, value, ctx))
    if phase == "talk":
        errors.extend(_check_pledge(args, ctx))
    return sorted(errors, key=lambda error: _FIELD_INDEX[error.field])


def normalize_act(args: Mapping[str, Any], phase: Phase) -> dict[str, Any]:
    """All tool fields in schema order, with fields the phase does not use set to null."""
    spec = PHASE_TABLE[phase]
    used = {"phase", *spec.required, *spec.optional}
    return {name: (args.get(name) if name in used else None) for name in FIELD_NAMES}


def extract_pledge(args: Mapping[str, Any]) -> Pledge | None:
    """The structured pledge of a validated talk call, or None when it made no pledge."""
    kind = args.get("pledge_kind")
    if kind is None:
        return None
    return Pledge(
        kind=kind,
        lot=args["pledge_lot"],
        amount=args.get("pledge_amount"),
        to=args.get("pledge_to"),
    )
