"""Frozen data contracts of Auction Night.

Modules:

* ``mechanism``  the six auction formats as data, phases, structural limits
* ``config``     night configuration, endpoints and capabilities, price list
* ``events``     event envelope, payloads, visibility levels, canonical JSON
* ``act``        the single ``act`` tool, phase table, validation of parsed calls
* ``replay``     replay.json v1

Freeze rule: fields and values may be added, never changed or removed. New fields need a
default so that old data keeps validating. ``write_schemas`` regenerates the JSON Schemas
in ``schemas/``; a test compares the committed files with a fresh export.
"""

import json
from pathlib import Path
from typing import Any

from auction_night.contracts.act import (
    ACT_FIELDS,
    PHASE_TABLE,
    ActContext,
    FieldError,
    act_input_schema,
    act_tool_anthropic,
    act_tool_fingerprint,
    act_tool_openai,
    validate_act,
)
from auction_night.contracts.config import (
    NightConfig,
    PricesFile,
    SeedResolutionError,
    config_hash,
    find_placeholders,
    load_config,
    load_prices,
)
from auction_night.contracts.events import (
    EVENT_ADAPTER,
    EVENT_TYPES,
    AnyEvent,
    Event,
    canonical_json,
    dumps_events,
    event_line,
    loads_events,
    parse_event,
    seed_commitment,
    sha256_digest,
    validate_log,
    visible_to,
)
from auction_night.contracts.mechanism import (
    FORMAT_KEYS,
    MAX_LOTS,
    MAX_SEATS,
    MECHANISMS,
    PHASES,
    MechanismDef,
    get_mechanism,
    make_mechanism,
)
from auction_night.contracts.replay import REPLAY_SCHEMA_ID, Replay

__all__ = [
    "ACT_FIELDS",
    "EVENT_ADAPTER",
    "EVENT_TYPES",
    "FORMAT_KEYS",
    "MAX_LOTS",
    "MAX_SEATS",
    "MECHANISMS",
    "PHASES",
    "PHASE_TABLE",
    "REPLAY_SCHEMA_ID",
    "ActContext",
    "AnyEvent",
    "Event",
    "FieldError",
    "MechanismDef",
    "NightConfig",
    "PricesFile",
    "Replay",
    "SeedResolutionError",
    "act_input_schema",
    "act_tool_anthropic",
    "act_tool_fingerprint",
    "act_tool_openai",
    "canonical_json",
    "config_hash",
    "dumps_events",
    "event_line",
    "find_placeholders",
    "get_mechanism",
    "load_config",
    "load_prices",
    "loads_events",
    "make_mechanism",
    "parse_event",
    "schema_texts",
    "seed_commitment",
    "sha256_digest",
    "validate_act",
    "validate_log",
    "visible_to",
    "write_schemas",
]

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


def _with_dialect(schema: dict[str, Any]) -> dict[str, Any]:
    return {"$schema": JSON_SCHEMA_DIALECT, **schema}


def _sorted_text(schema: dict[str, Any]) -> str:
    return json.dumps(_with_dialect(schema), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _ordered_text(tool: dict[str, Any]) -> str:
    # Tool definitions keep their field order: it fixes the bytes of the cached prompt prefix.
    return json.dumps(tool, indent=2, ensure_ascii=False) + "\n"


def schema_texts() -> dict[str, str]:
    """File name to exact file text of every exported schema and tool definition."""
    return {
        "config.v1.schema.json": _sorted_text(NightConfig.model_json_schema()),
        "events.v1.schema.json": _sorted_text(EVENT_ADAPTER.json_schema()),
        "replay.v1.schema.json": _sorted_text(Replay.model_json_schema()),
        "prices.v1.schema.json": _sorted_text(PricesFile.model_json_schema()),
        "act_tool.anthropic.v1.json": _ordered_text(act_tool_anthropic()),
        "act_tool.openai.v1.json": _ordered_text(act_tool_openai()),
    }


def write_schemas(directory: str | Path) -> list[Path]:
    """Write every exported schema into ``directory`` and return the paths written."""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in schema_texts().items():
        path = target / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written
