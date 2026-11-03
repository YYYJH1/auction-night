"""replay.json v1: a hand-built night validates, round-trips and rejects broken data."""

import copy
import json
from typing import Any

import pytest
from pydantic import ValidationError

from auction_night.contracts import AnyEvent, Replay, canonical_json
from auction_night.contracts.events import HammerData, SeatScore, TalkData, seed_commitment
from auction_night.contracts.replay import REPLAY_SCHEMA_ID, ReplayScore, TimelineLot

COMMITMENT = seed_commitment("mini-seed-0001")


def _catalog(log: list[AnyEvent]) -> list[dict[str, Any]]:
    start = next(event for event in log if event.type == "night_start")
    return [lot.model_dump() for lot in start.data.lots]


def minimal_night(log: list[AnyEvent]) -> dict[str, Any]:
    """The smallest replay: only the parts that every producer must supply."""
    return {
        "schema": REPLAY_SCHEMA_ID,
        "night": {
            "id": "mini-0001", "lang": "zh", "mode": "plain", "status": "completed",
            "ranked": True, "n_lots": 3,
            "mechanisms": {"sealed_first": "v1", "english": "v1", "dutch": "v1"},
            "budget_lo": 150, "budget_hi": 450, "collectors": 1, "shills": 1,
            "mission_bonus": 150,
        },
        "seats": [
            {"seat": 1, "codename": "鲸落", "kind": "llm", "vendor": "vendor-a",
             "endpoint": "vendor_a", "model": "<model-a1>", "model_returned": "model-a-1"},
            {"seat": 2, "codename": "阿铁", "kind": "llm", "vendor": "anthropic",
             "endpoint": "anthropic", "model": "claude-haiku-5-5",
             "model_returned": "claude-haiku-5-5", "thinking": "adaptive", "effort": "medium"},
            {"seat": 3, "codename": "老实人", "kind": "bot", "policy": "truthful"},
            {"seat": 4, "codename": "经济学家", "kind": "bot", "policy": "economist"},
        ],
        "catalog": _catalog(log),
        "timeline": [
            {
                "lot": 1, "format": "sealed_first", "mechanism": "sealed_first@v1",
                "first_seq": 7, "last_seq": 14,
                "active": [1, 2, 3, 4], "talk_order": [1, 2, 3, 4],
                "talk": [
                    {"seat": 1, "text": "方糖我不碰，2 号要不要分一分后面的海洋？",
                     "pledge": {"kind": "no_bid", "lot": 1}, "sanitized_chars": 0}
                ],
                "actions": [
                    {"seq": 12, "seat": 1, "phase": "sealed", "status": "ok", "value": 60},
                    {"seq": 13, "seat": 2, "phase": "sealed", "status": "ok", "value": 70},
                ],
                "hammer": {
                    "format": "sealed_first", "winner": 2, "price": 70, "payments": {2: 70},
                    "bids": {1: 60, 2: 70, 3: 55, 4: 50},
                },
            },
            {
                "lot": 2, "format": "english", "mechanism": "english@v1",
                "first_seq": 15, "last_seq": 21,
                "active": [1, 2, 3, 4], "talk_order": [2, 3, 4, 1],
                "actions": [
                    {"seq": 16, "seat": 1, "phase": "english_init", "status": "clamped",
                     "value": 342, "raw_value": 500},
                    {"seq": 17, "seat": 2, "phase": "english_init", "status": "ok", "value": 120},
                ],
                "dropouts": [
                    {"seat": 3, "price": 64, "remaining": [1, 2, 4]},
                    {"seat": 4, "price": 90, "remaining": [1, 2]},
                    {"seat": 2, "price": 120, "remaining": [1]},
                ],
                "hammer": {"format": "english", "winner": 1, "price": 120, "payments": {1: 120}},
            },
            {
                "lot": 3, "format": "dutch", "mechanism": "dutch@v1",
                "first_seq": 22, "last_seq": 26,
                "active": [1, 2, 4], "sitting_out": [3], "talk_order": [3, 4, 1, 2],
                "actions": [
                    {"seq": 23, "seat": 1, "phase": "dutch", "status": "ok", "value": 130},
                    {"seq": 24, "seat": 2, "phase": "dutch", "status": "ok", "value": 200},
                ],
                "dutch": {
                    "ladder": [400, 360, 320, 280, 240, 200, 160, 120, 80, 40],
                    "level": 7, "price": 160, "buyer": 2,
                },
                "hammer": {"format": "dutch", "winner": 2, "price": 160, "payments": {2: 160}},
            },
        ],
        "reveal": {
            "budgets": {1: 342, 2: 260, 3: 300, 4: 410},
            "values": {
                1: {1: 37, 2: 158, 3: 133},
                2: {1: 80, 2: 64, 3: 300},
                3: {1: 55, 2: 64, 3: 210},
                4: {1: 50, 2: 90, 3: 200},
            },
        },
        "scores": [
            {"seat": 1, "surplus": 38, "mission_bonus": 0, "total": 38, "rank_tonight": 2,
             "spent": 120, "lots_won": [2]},
            {"seat": 2, "surplus": 150, "mission_bonus": 0, "total": 150, "rank_tonight": 1,
             "spent": 230, "lots_won": [1, 3]},
            {"seat": 3, "surplus": 0, "mission_bonus": 0, "total": 0},
            {"seat": 4, "surplus": 0, "mission_bonus": 0, "total": 0},
        ],
        "manifest": {
            "config_hash": "sha256:" + "cd" * 32,
            "prompt_template_version": "zh-v1",
            "engine_version": "0.0.1",
        },
    }  # fmt: skip


def full_night(log: list[AnyEvent]) -> dict[str, Any]:
    """The minimal night plus every optional part the show layer fills in."""
    replay = minimal_night(log)
    replay["night"].update(season_id="s1", deck_id="d01", rotation=0)
    replay["timeline"][0]["overlay"] = {
        "ref_bids": {1: 37, 2: 80},
        "alpha_milli": {4: 910},
        "regret": {1: 0, 2: 10},
        "to_highest_value": True,
        "excess": {1: -5, 2: 3},
        "expected_revenue_milli": 77960,
        "curse_gap": -4,
    }
    replay["timeline"][0]["host"] = "1 号说不碰方糖，转头就出了 60。"
    replay["reveal"].update(
        signals={},
        true_values={},
        missions=[
            {
                "seat": 1,
                "kind": "collector",
                "completed": False,
                "bonus": 0,
                "tag": "海洋",
                "need": 1,
                "won_lots": [],
            },
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
        ],
        plans=[
            {
                "seat": 1,
                "priorities": [0, 1, 3],
                "budget_plan": [0, 40, 160],
                "persona": "装作只对甜品感兴趣",
            }
        ],
        guesses=[{"lot": 1, "seat": 1, "target": 2, "guess": 180, "truth": 260, "abs_error": 80}],
        thoughts=[{"seq": 12, "lot": 1, "seat": 1, "phase": "sealed", "text": "只出一点点。"}],
    )
    replay["pledges"] = [
        {
            "seq": 8,
            "lot": 1,
            "seat": 1,
            "pledge": {"kind": "no_bid", "lot": 1},
            "outcome": "violated",
            "intent": "unknown",
            "detail": "bid 60 on a no_bid lot",
        }
    ]
    replay["judges"] = [
        {
            "judge": 0,
            "vendor": "vendor-c",
            "model_returned": "model-c-1",
            "extractions": [{"seat": 1, "lot": 1, "kind": "promise", "quote": "方糖我不碰"}],
            "mission_guesses": [{"seat": 1, "kind": "collector", "tag": "海洋"}],
        }
    ]
    replay["verdicts"] = [
        {"verdict": "pledge_violation", "seats": [1], "lots": [1], "evidence_seqs": [8, 12]}
    ]
    replay["judge_accuracy"] = [
        {"judge": 0, "vendor": "vendor-c", "guesses": 1, "correct": 0, "targets": 2}
    ]
    replay["awards"] = [
        {"award": "backstabber", "winners": [1], "metric": 1, "metric_unit": "count"},
        {"award": "keeper", "vacant": True},
    ]
    replay["bots_reference"] = [
        {"award": "most_textbook", "winners": [3], "metric_unit": "milli", "metric": 0}
    ]
    replay["highlights"] = [
        {
            "id": 1,
            "category": "backstab",
            "score": 9,
            "lot": 1,
            "seats": [1],
            "first_seq": 8,
            "last_seq": 14,
            "text": "说好不碰，转头出价。",
        }
    ]
    replay["scores"][0]["stats"] = {
        "actions": 6,
        "invalid_inputs": 0,
        "abstentions": 0,
        "refusals": 0,
        "clamps": 1,
        "syntax_repairs": 1,
        "valid_guesses": 1,
        "mind_error_milli": 267,
    }
    replay["host"] = {
        "per_lot": [{"lot": 1, "text": "1 号说不碰方糖，转头就出了 60。"}],
        "recap": "三件拍品，两个座位分了钱。\n托儿没能抬过门槛。",
        "prediction_card": {
            "items": [
                {
                    "format": "sealed_first",
                    "claim": "bid_shading",
                    "text": "一价密封会压价。",
                    "hit": True,
                }
            ],
            "hits": 1,
        },
    }
    replay["usage"] = {
        "by_role": {
            "seat:1": {
                "calls": 8,
                "input_tokens": 9000,
                "output_tokens": 800,
                "cache_read_tokens": 6000,
                "latency_ms": 12000,
            }
        },
        "total": {"calls": 8, "input_tokens": 9000, "output_tokens": 800},
        "cost_microusd": 4321,
    }
    replay["manifest"].update(
        prompt_hash="sha256:" + "ef" * 32,
        tool_schema_hash="sha256:" + "12" * 32,
        git_commit="0123456789abcdef0123456789abcdef01234567",
        git_dirty=False,
        mechanism_versions={"english": "v1", "dutch": "v1", "sealed_first": "v1"},
        seed_commitment=COMMITMENT,
        revealed_seed="mini-seed-0001",
        models_returned={"seat:1": "model-a-1", "seat:2": "claude-haiku-5-5"},
    )
    return replay


@pytest.fixture
def minimal(mini_log: list[AnyEvent]) -> dict[str, Any]:
    return minimal_night(mini_log)


@pytest.fixture
def full(mini_log: list[AnyEvent]) -> dict[str, Any]:
    return full_night(mini_log)


# -------------------------------------------------------------------------------- accepts


def test_minimal_hand_built_night_is_accepted(minimal: dict[str, Any]) -> None:
    replay = Replay.model_validate(minimal)
    assert replay.schema_id == REPLAY_SCHEMA_ID == "auction-night/replay@1"
    assert [lot.lot for lot in replay.timeline] == [1, 2, 3]
    assert replay.pledges == replay.judges == replay.verdicts == replay.awards == []
    assert replay.highlights == [] and replay.host.recap is None
    assert replay.usage.cost_microusd is None and replay.usage.by_role == {}


def test_full_night_is_accepted(full: dict[str, Any]) -> None:
    replay = Replay.model_validate(full)
    assert replay.reveal.guesses[0].abs_error == 80
    assert replay.pledges[0].outcome == "violated"
    assert replay.awards[1].vacant and not replay.awards[1].winners
    assert replay.scores[0].stats.clamps == 1 and replay.scores[2].stats.actions == 0
    assert replay.manifest.revealed_seed == "mini-seed-0001"


def test_catalog_and_event_payloads_are_shared_models(
    full: dict[str, Any], mini_log: list[AnyEvent]
) -> None:
    replay = Replay.model_validate(full)
    start = next(event for event in mini_log if event.type == "night_start")
    assert replay.catalog == start.data.lots
    assert isinstance(replay.timeline[0].talk[0], TalkData)
    assert isinstance(replay.timeline[0].hammer, HammerData)
    assert isinstance(replay.scores[0], ReplayScore) and isinstance(replay.scores[0], SeatScore)
    hammers = [event.data for event in mini_log if event.type == "hammer"]
    assert [lot.hammer for lot in replay.timeline] == hammers


def test_round_trip_is_byte_stable(full: dict[str, Any]) -> None:
    replay = Replay.model_validate(full)
    text = canonical_json(replay)
    again = Replay.model_validate_json(text)
    assert again == replay
    assert canonical_json(again) == text


def test_schema_key_is_written_as_schema(minimal: dict[str, Any]) -> None:
    replay = Replay.model_validate(minimal)
    data = json.loads(canonical_json(replay))
    assert data["schema"] == REPLAY_SCHEMA_ID and "schema_id" not in data
    again = Replay.model_validate(
        {**{k: v for k, v in minimal.items() if k != "schema"}, "schema_id": REPLAY_SCHEMA_ID}
    )
    assert again == replay, "the field name is accepted too"


def test_keys_are_sorted_and_no_float_is_written(full: dict[str, Any]) -> None:
    def check(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [k for k, _ in pairs]
        assert keys == sorted(keys)
        return dict(pairs)

    text = canonical_json(Replay.model_validate(full))
    json.loads(text, object_pairs_hook=check, parse_float=_refuse)


def _refuse(text: str) -> float:
    raise AssertionError(f"float in replay: {text}")


def test_the_canary_never_reaches_a_replay(full: dict[str, Any]) -> None:
    text = canonical_json(Replay.model_validate(full))
    assert "canary" not in text and "7QX2KD" not in text and "K9ZP3M" not in text


def test_timeline_lot_is_reusable_on_its_own(full: dict[str, Any]) -> None:
    lot = TimelineLot.model_validate(full["timeline"][1])
    assert [d.seat for d in lot.dropouts] == [3, 4, 2]
    assert lot.hammer.winner == 1 and lot.overlay.ref_bids == {}


# ------------------------------------------------------------------------------- rejects


def broken(minimal: dict[str, Any], mutate: Any) -> None:
    raw = copy.deepcopy(minimal)
    mutate(raw)
    with pytest.raises(ValidationError):
        Replay.model_validate(raw)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(schema="auction-night/replay@2"),
        lambda r: r.pop("schema"),
        lambda r: r.pop("manifest"),
        lambda r: r.pop("reveal"),
        lambda r: r.update(surprise=1),
        lambda r: r["seats"].append(copy.deepcopy(r["seats"][0])),
        lambda r: r["catalog"].pop(),
        lambda r: r["catalog"].reverse(),
        lambda r: r["timeline"].reverse(),
        lambda r: r["timeline"].append(copy.deepcopy(r["timeline"][0])),
        lambda r: r["timeline"][0].update(lot=4),
        lambda r: r["timeline"][0].update(first_seq=20),
        lambda r: r["scores"].append({"seat": 5, "surplus": 0, "mission_bonus": 0, "total": 0}),
        lambda r: r["scores"][0].update(total=39),
        lambda r: r["night"].update(status="running"),
        lambda r: r["night"].update(n_lots=0),
        lambda r: r["night"]["mechanisms"].update(auction="v1"),
        lambda r: r["night"]["mechanisms"].update(english="1"),
        lambda r: r["manifest"].update(config_hash="md5:abc"),
        lambda r: r["manifest"].update(git_commit="abc123"),
        lambda r: r["manifest"].update(revealed_seed="s"),
        lambda r: r["timeline"][0]["hammer"].update(price=70.5),
        lambda r: r["timeline"][0]["hammer"].update(winner=None),
        lambda r: r["reveal"]["budgets"].update({"9": 100}),
        lambda r: r["reveal"]["values"][1].update({"37": 5}),
        lambda r: r["seats"][0].update(kind="robot"),
    ],
)
def test_broken_replays_are_rejected(minimal: dict[str, Any], mutate: Any) -> None:
    broken(minimal, mutate)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["timeline"][0].update(overlay={"alpha_milli": {4: 0.91}}),
        lambda r: r["timeline"][0].update(overlay={"excess": {1: 0.5}}),
        lambda r: r["timeline"][0].update(overlay={"expected_revenue_milli": -1}),
        lambda r: r["usage"].update(cost_microusd=1.5),
        lambda r: r["highlights"][0].update(score=11),
        lambda r: r["highlights"][0].update(category="boring"),
        lambda r: r["pledges"][0].update(outcome="maybe"),
        lambda r: r["reveal"]["guesses"][0].update(abs_error=-1),
        lambda r: r["awards"][0].update(winners=[]),
        lambda r: r["host"]["per_lot"][0].update(lot=0),
        lambda r: r["host"]["prediction_card"]["items"][0].update(format="auction"),
        lambda r: r["judge_accuracy"][0].update(judge=9),
    ],
)
def test_broken_show_layer_parts_are_rejected(full: dict[str, Any], mutate: Any) -> None:
    broken(full, mutate)


def test_a_float_anywhere_cannot_be_serialised() -> None:
    with pytest.raises(ValueError, match="float"):
        canonical_json({"timeline": [{"overlay": {"alpha": 0.91}}]})


def test_the_revealed_seed_must_open_the_commitment(full: dict[str, Any]) -> None:
    broken(full, lambda r: r["manifest"].update(revealed_seed="another-seed"))
    broken(full, lambda r: r["manifest"].update(seed_commitment=None))
    ok = copy.deepcopy(full)
    ok["manifest"].update(revealed_seed=None)
    assert Replay.model_validate(ok).manifest.revealed_seed is None


def test_manifest_and_night_agree_on_mechanism_versions(full: dict[str, Any]) -> None:
    broken(full, lambda r: r["manifest"]["mechanism_versions"].update(english="v2"))
    broken(full, lambda r: r["manifest"]["mechanism_versions"].pop("english"))
    full["manifest"]["mechanism_versions"] = {}
    Replay.model_validate(full)


def test_the_later_replay_fields_default_to_empty(full: dict[str, Any]) -> None:
    replay = Replay.model_validate(full)
    lot = replay.timeline[0]
    assert lot.talk_seqs == {} and lot.hammer_seq is None
    assert replay.judges_absent == [] and replay.highlights[0].basis is None
    manifest = replay.manifest
    hashes = (manifest.host_prompt_hash, manifest.judge_prompt_hash, manifest.scan_prompt_hash)
    assert hashes == (None, None, None)
    assert manifest.cleared_findings == [] and manifest.auto_cleared_findings == []


CLEARED = {
    "path": "host.per_lot[0].text",
    "category": "real_person",
    "detail": "为真实网络人物常用名",
    "reason": "它是座位代号，不是真人",
}


def test_the_manifest_records_the_findings_that_a_person_cleared(full: dict[str, Any]) -> None:
    ok = copy.deepcopy(full)
    ok["manifest"]["cleared_findings"] = [CLEARED, {**CLEARED, "path": "host.recap"}]
    replay = Replay.model_validate(ok)
    assert [c.path for c in replay.manifest.cleared_findings] == [
        "host.per_lot[0].text",
        "host.recap",
    ]
    assert replay.manifest.cleared_findings[0].reason == "它是座位代号，不是真人"
    assert Replay.model_validate_json(canonical_json(replay)) == replay
    written = json.loads(canonical_json(replay))["manifest"]["cleared_findings"]
    assert written[0] == CLEARED, "the canonical form writes every field"


def test_what_the_gate_said_may_be_left_out_of_a_cleared_finding(full: dict[str, Any]) -> None:
    ok = copy.deepcopy(full)
    ok["manifest"]["cleared_findings"] = [{k: v for k, v in CLEARED.items() if k != "detail"}]
    assert Replay.model_validate(ok).manifest.cleared_findings[0].detail == ""


@pytest.mark.parametrize(
    "change",
    [
        {"reason": ""},
        {"reason": "第一行\n第二行"},
        {"path": ""},
        {"category": ""},
        {"path": "x" * 65},
        {"extra": "x"},
        {"reason": None},
    ],
)
def test_a_cleared_finding_needs_a_place_a_category_and_one_line_of_reason(
    full: dict[str, Any], change: dict[str, Any]
) -> None:
    broken(full, lambda r: r["manifest"].update(cleared_findings=[{**CLEARED, **change}]))
    lacking = {k: v for k, v in CLEARED.items() if k != "reason"}
    broken(full, lambda r: r["manifest"].update(cleared_findings=[lacking]))


def test_the_manifest_lists_what_a_rule_released_apart_from_what_a_person_released(
    full: dict[str, Any],
) -> None:
    ok = copy.deepcopy(full)
    ok["manifest"]["auto_cleared_findings"] = [CLEARED]
    ok["manifest"]["cleared_findings"] = [{**CLEARED, "path": "host.recap"}]
    replay = Replay.model_validate(ok)
    assert [c.path for c in replay.manifest.auto_cleared_findings] == ["host.per_lot[0].text"]
    assert [c.path for c in replay.manifest.cleared_findings] == ["host.recap"]
    assert Replay.model_validate_json(canonical_json(replay)) == replay
    written = json.loads(canonical_json(replay))["manifest"]
    assert written["auto_cleared_findings"] == [CLEARED]
    without = copy.deepcopy(ok)
    del without["manifest"]["auto_cleared_findings"]
    assert Replay.model_validate(without).manifest.auto_cleared_findings == []


@pytest.mark.parametrize(
    "change",
    [{"reason": ""}, {"reason": "第一行\n第二行"}, {"path": ""}, {"category": ""}, {"extra": "x"}],
)
def test_a_finding_released_by_rule_is_checked_like_one_released_by_a_person(
    full: dict[str, Any], change: dict[str, Any]
) -> None:
    broken(full, lambda r: r["manifest"].update(auto_cleared_findings=[{**CLEARED, **change}]))
    lacking = {k: v for k, v in CLEARED.items() if k != "reason"}
    broken(full, lambda r: r["manifest"].update(auto_cleared_findings=[lacking]))


def test_the_later_replay_fields_are_checked(full: dict[str, Any]) -> None:
    ok = copy.deepcopy(full)
    ok["timeline"][0].update(talk_seqs={1: 3}, hammer_seq=9)
    ok["judges_absent"] = [{"judge": 1, "vendor": "acme", "reason": "timeout"}]
    ok["manifest"]["host_prompt_hash"] = "sha256:" + "0" * 64
    ok["highlights"][0]["basis"] = "private"
    replay = Replay.model_validate(ok)
    assert replay.timeline[0].talk_seqs == {1: 3} and replay.judges_absent[0].reason == "timeout"
    broken(full, lambda r: r["timeline"][0].update(hammer_seq=0))
    broken(full, lambda r: r["timeline"][0].update(talk_seqs={1: 1.5}))
    broken(full, lambda r: r.update(judges_absent=[{"judge": 1, "vendor": "a", "reason": "bored"}]))
    broken(full, lambda r: r["manifest"].update(judge_prompt_hash="sha256:xyz"))
    broken(full, lambda r: r["highlights"][0].update(basis="hidden"))
