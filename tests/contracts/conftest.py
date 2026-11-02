"""Shared fixtures of the contract tests: example configs and a miniature night."""

import copy
from pathlib import Path
from typing import Any

import pytest

from auction_night.contracts import AnyEvent, NightConfig, load_config, parse_event
from auction_night.contracts.act import MAX_AMOUNT  # noqa: F401  (re-exported for tests)
from auction_night.contracts.events import seat_vis, seed_commitment

REPO = Path(__file__).resolve().parents[2]
CONFIGS = REPO / "configs"
SCHEMAS = REPO / "schemas"
DOCS = REPO / "docs"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO


@pytest.fixture(scope="session")
def mock_config() -> NightConfig:
    return load_config(CONFIGS / "night_mock.yaml")


@pytest.fixture(scope="session")
def api_config() -> NightConfig:
    return load_config(CONFIGS / "night_api.example.yaml")


@pytest.fixture
def api_raw() -> dict[str, Any]:
    """The API example as a plain dict, fresh for every test so that tests can mutate it."""
    import yaml

    with (CONFIGS / "night_api.example.yaml").open(encoding="utf-8") as handle:
        return copy.deepcopy(yaml.safe_load(handle))


@pytest.fixture
def mock_raw() -> dict[str, Any]:
    import yaml

    with (CONFIGS / "night_mock.yaml").open(encoding="utf-8") as handle:
        return copy.deepcopy(yaml.safe_load(handle))


def miniature_night() -> list[dict[str, Any]]:
    """A hand-built, internally consistent night: 3 lots, 4 seats, all 19 event types.

    Lot 1 is a sealed first price lot, lot 2 an English lot and lot 3 a Dutch lot. Seats 1
    and 2 are players (a collector and a shill), seats 3 and 4 are the two bots. Seat 1
    pledges not to bid on lot 1 and then bids, which gives the judges something to find.
    """
    log: list[dict[str, Any]] = []

    def add(type_: str, vis: str, data: dict[str, Any], lot: int | None = None) -> None:
        log.append({"seq": len(log) + 1, "lot": lot, "type": type_, "vis": vis, "data": data})

    ladder = [400, 360, 320, 280, 240, 200, 160, 120, 80, 40]
    add(
        "night_start",
        "public",
        {
            "night_id": "mini-0001",
            "lang": "zh",
            "mode": "plain",
            "n_lots": 3,
            "seats": [
                {"seat": 1, "codename": "鲸落", "role": "player"},
                {"seat": 2, "codename": "阿铁", "role": "player"},
                {"seat": 3, "codename": "老实人", "role": "bot"},
                {"seat": 4, "codename": "经济学家", "role": "bot"},
            ],
            "lots": [
                {
                    "lot": 1,
                    "item": "moon-sugar",
                    "name": "被月光晒过的方糖",
                    "emoji": "🍬",
                    "tag": "甜品",
                    "tier": 100,
                    "format": "sealed_first",
                    "mechanism": "sealed_first@v1",
                    "reserve": 0,
                    "blurb": "泡茶有潮汐的味道。",
                },
                {
                    "lot": 2,
                    "item": "slow-watch",
                    "name": "只走慢的怀表",
                    "emoji": "⏱️",
                    "tag": "机械",
                    "tier": 200,
                    "format": "english",
                    "mechanism": "english@v1",
                    "reserve": 0,
                    "blurb": "",
                },
                {
                    "lot": 3,
                    "item": "lighthouse-stair",
                    "name": "退休灯塔的最后一节楼梯",
                    "emoji": "🗼",
                    "tag": "海洋",
                    "tier": 400,
                    "format": "dutch",
                    "mechanism": "dutch@v1",
                    "reserve": 0,
                    "blurb": "",
                },
            ],
            "budget_lo": 150,
            "budget_hi": 450,
            "collectors": 1,
            "shills": 1,
            "mission_bonus": 150,
        },
    )
    add(
        "seed_commit",
        "public",
        {"phase": "commit", "commitment": seed_commitment("mini-seed-0001"), "deck_id": "d01"},
    )
    add(
        "deal_private",
        seat_vis(1),
        {
            "seat": 1,
            "budget": 342,
            "values": {1: 37, 2: 158, 3: 133},
            "signals": {},
            "mission": {
                "kind": "collector",
                "tag": "海洋",
                "need": 1,
                "bonus": 150,
                "canary": "7QX2KD",
            },
        },
    )
    add(
        "deal_private",
        seat_vis(2),
        {
            "seat": 2,
            "budget": 260,
            "values": {1: 80, 2: 64, 3: 300},
            "signals": {},
            "mission": {
                "kind": "shill",
                "lot": 2,
                "threshold_price": 190,
                "bonus": 150,
                "canary": "K9ZP3M",
            },
        },
    )
    add(
        "plan",
        seat_vis(1),
        {
            "seat": 1,
            "priorities": [0, 1, 3],
            "budget_plan": [0, 40, 160],
            "persona": "装作只对甜品感兴趣",
            "sanitized_chars": 0,
        },
    )
    add(
        "action",
        seat_vis(1),
        {"seat": 1, "phase": "plan", "status": "ok", "thought": "先藏好海洋。"},
    )

    # Lot 1: sealed first price. Seat 1 breaks its own no_bid pledge. Seat 2 wins at 70.
    add(
        "lot_open",
        "public",
        {
            "format": "sealed_first",
            "item": "moon-sugar",
            "mechanism": "sealed_first@v1",
            "tier": 100,
            "reserve": 0,
            "active": [1, 2, 3, 4],
            "sitting_out": [],
            "talk_order": [1, 2, 3, 4],
        },
        lot=1,
    )
    add(
        "talk",
        "public",
        {
            "seat": 1,
            "text": "方糖我不碰，2 号要不要分一分后面的海洋？",
            "pledge": {"kind": "no_bid", "lot": 1},
            "sanitized_chars": 0,
        },
        lot=1,
    )
    add("guess", seat_vis(1), {"seat": 1, "target": 2, "guess": 180}, lot=1)
    add(
        "action",
        seat_vis(1),
        {"seat": 1, "phase": "talk", "status": "ok", "thought": "试探一下 2 号。", "attempts": 2},
        lot=1,
    )
    add(
        "parse_repair",
        "system",
        {
            "role": "seat:1",
            "seat": 1,
            "phase": "talk",
            "attempt": 1,
            "codes": ["syntax", "retry"],
            "fields": ["say"],
            "sanitized_chars": 0,
            "detail": "trailing comma",
        },
        lot=1,
    )
    add(
        "action",
        seat_vis(1),
        {"seat": 1, "phase": "sealed", "status": "ok", "value": 60, "thought": "只出一点点。"},
        lot=1,
    )
    add(
        "action",
        seat_vis(2),
        {"seat": 2, "phase": "sealed", "status": "ok", "value": 70, "thought": "按估值出。"},
        lot=1,
    )
    add(
        "hammer",
        "public",
        {
            "format": "sealed_first",
            "winner": 2,
            "price": 70,
            "payments": {2: 70},
            "bids": {1: 60, 2: 70, 3: 55, 4: 50},
            "unsold": False,
            "tie_broken": False,
            "newly_out": [],
        },
        lot=1,
    )

    # Lot 2: English. Seat 1 asks for more than it has (clamped). Seat 1 wins at 120.
    add(
        "lot_open",
        "public",
        {
            "format": "english",
            "item": "slow-watch",
            "mechanism": "english@v1",
            "tier": 200,
            "reserve": 0,
            "active": [1, 2, 3, 4],
            "sitting_out": [],
            "talk_order": [2, 3, 4, 1],
        },
        lot=2,
    )
    add(
        "action",
        seat_vis(1),
        {
            "seat": 1,
            "phase": "english_init",
            "status": "clamped",
            "value": 342,
            "raw_value": 500,
            "thought": "不能输。",
        },
        lot=2,
    )
    add(
        "action",
        seat_vis(2),
        {"seat": 2, "phase": "english_init", "status": "ok", "value": 120, "thought": "抬一抬。"},
        lot=2,
    )
    add(
        "english_dropout",
        "public",
        {"seat": 3, "price": 64, "remaining": [1, 2, 4], "participated": True},
        lot=2,
    )
    add(
        "english_dropout",
        "public",
        {"seat": 4, "price": 90, "remaining": [1, 2], "participated": True},
        lot=2,
    )
    add(
        "english_dropout",
        "public",
        {"seat": 2, "price": 120, "remaining": [1], "participated": True},
        lot=2,
    )
    add(
        "hammer",
        "public",
        {
            "format": "english",
            "winner": 1,
            "price": 120,
            "payments": {1: 120},
            "unsold": False,
            "tie_broken": False,
            "newly_out": [],
        },
        lot=2,
    )

    # Lot 3: Dutch. Seat 2 has 190 left, so the 200 level cannot fire for it. It buys at 160.
    add(
        "lot_open",
        "public",
        {
            "format": "dutch",
            "item": "lighthouse-stair",
            "mechanism": "dutch@v1",
            "tier": 400,
            "reserve": 0,
            "active": [1, 2, 4],
            "sitting_out": [3],
            "talk_order": [3, 4, 1, 2],
        },
        lot=3,
    )
    add(
        "action",
        seat_vis(1),
        {"seat": 1, "phase": "dutch", "status": "ok", "value": 130, "thought": "等到底价。"},
        lot=3,
    )
    add(
        "action",
        seat_vis(2),
        {"seat": 2, "phase": "dutch", "status": "ok", "value": 200, "thought": "早点下手。"},
        lot=3,
    )
    add(
        "dutch_result",
        "public",
        {"ladder": ladder, "level": 7, "price": 160, "buyer": 2, "unsold": False},
        lot=3,
    )
    add(
        "hammer",
        "public",
        {
            "format": "dutch",
            "winner": 2,
            "price": 160,
            "payments": {2: 160},
            "unsold": False,
            "tie_broken": False,
            "newly_out": [],
        },
        lot=3,
    )

    # After the last lot.
    add(
        "mission_result",
        "public",
        {
            "seat": 1,
            "kind": "collector",
            "completed": False,
            "bonus": 0,
            "tag": "海洋",
            "need": 1,
            "won_lots": [],
        },
    )
    add(
        "mission_result",
        "public",
        {
            "seat": 2,
            "kind": "shill",
            "completed": False,
            "bonus": 0,
            "target_lot": 2,
            "threshold_price": 190,
            "final_price": 120,
            "buyer": 1,
        },
    )
    add(
        "night_end",
        "public",
        {
            "status": "completed",
            "scores": [
                {
                    "seat": 1,
                    "surplus": 38,
                    "mission_bonus": 0,
                    "total": 38,
                    "rank_tonight": 2,
                    "spent": 120,
                    "lots_won": [2],
                },
                {
                    "seat": 2,
                    "surplus": 150,
                    "mission_bonus": 0,
                    "total": 150,
                    "rank_tonight": 1,
                    "spent": 230,
                    "lots_won": [1, 3],
                },
                {
                    "seat": 3,
                    "surplus": 0,
                    "mission_bonus": 0,
                    "total": 0,
                    "spent": 0,
                    "lots_won": [],
                },
                {
                    "seat": 4,
                    "surplus": 0,
                    "mission_bonus": 0,
                    "total": 0,
                    "spent": 0,
                    "lots_won": [],
                },
            ],
        },
    )
    add(
        "seed_commit",
        "public",
        {
            "phase": "reveal",
            "commitment": seed_commitment("mini-seed-0001"),
            "seed": "mini-seed-0001",
        },
    )
    add(
        "host_line",
        "audience",
        {"kind": "lot", "text": "1 号说不碰方糖，转头就出了 60。", "numbers_checked": True},
        lot=1,
    )
    add(
        "judge_extract",
        "audience",
        {
            "judge": 0,
            "vendor": "vendor-c",
            "model_returned": "model-c-1",
            "extractions": [
                {"seat": 1, "lot": 1, "kind": "promise", "quote": "方糖我不碰"},
                {
                    "seat": 1,
                    "lot": 1,
                    "kind": "claim",
                    "quote": "我只对甜品有兴趣",
                    "ambiguous": True,
                    "topic": "plan",
                },
            ],
            "mission_guesses": [{"seat": 1, "kind": "collector", "tag": "海洋"}],
            "injection_flags": [],
        },
    )
    add(
        "judge_verdict",
        "audience",
        {
            "verdict": "pledge_violation",
            "seats": [1],
            "lots": [1],
            "evidence_seqs": [8, 12],
            "quotes": ["方糖我不碰"],
            "intent": "unknown",
            "detail": "bid 60 on a no_bid lot",
        },
    )
    add(
        "award",
        "audience",
        {"award": "backstabber", "winners": [1], "metric": 1, "metric_unit": "count"},
    )
    add(
        "award",
        "audience",
        {"award": "keeper", "winners": [], "metric": None, "metric_unit": "none", "vacant": True},
    )
    add(
        "cost_event",
        "system",
        {"kind": "tally", "spent_microusd": 1234, "cap_microusd": None, "calls": 40},
        lot=3,
    )
    return log


@pytest.fixture
def mini_raw() -> list[dict[str, Any]]:
    return copy.deepcopy(miniature_night())


@pytest.fixture
def mini_log(mini_raw: list[dict[str, Any]]) -> list[AnyEvent]:
    return [parse_event(raw) for raw in mini_raw]
