"""Event contract: envelope, payload rules, visibility and canonical serialisation."""

import copy
import json
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from auction_night.contracts import (
    AnyEvent,
    canonical_json,
    dumps_events,
    event_line,
    loads_events,
    parse_event,
    sha256_digest,
    validate_log,
    visible_to,
)
from auction_night.contracts.events import (
    ALLOWED_VIS,
    EVENT_CLASSES,
    EVENT_TYPES,
    DealPrivateData,
    HammerData,
    HammerEvent,
    seat_vis,
    seed_commitment,
    vis_kind,
    vis_seat,
)

REQUIRED_TYPES = {
    "night_start", "seed_commit", "deal_private", "plan", "lot_open", "talk", "guess",
    "action", "english_dropout", "dutch_result", "hammer", "mission_result", "night_end",
    "host_line", "judge_extract", "judge_verdict", "award", "cost_event", "parse_repair",
}  # fmt: skip

# Digest of the canonical log of the miniature night. Update it only on purpose: a changed
# digest means that the wire format of some event changed.
# Changed on 2026-11-01 when parse_repair gained the optional `status` field (an addition), and
# again when extractions, verdicts and awards gained their optional fields (additions).
MINI_LOG_DIGEST = "sha256:1c52c351eabaaf5fb1eb7c59c1d99122bf2dcde8ab12deb95df7d9594b403b30"


def bad(raw: dict[str, Any], match: str | None = None) -> None:
    with pytest.raises(ValidationError, match=match):
        parse_event(raw)


def first(log: list[dict[str, Any]], type_: str, **where: Any) -> dict[str, Any]:
    for raw in log:
        if raw["type"] == type_ and all(raw["data"].get(k) == v for k, v in where.items()):
            return copy.deepcopy(raw)
    raise LookupError(type_)


# ------------------------------------------------------------------------------- types


def test_all_required_event_types_exist() -> None:
    assert set(EVENT_TYPES) == REQUIRED_TYPES
    assert len(EVENT_TYPES) == 19
    assert set(EVENT_CLASSES) == REQUIRED_TYPES


def test_miniature_night_uses_every_type(mini_log: list[AnyEvent]) -> None:
    assert {event.type for event in mini_log} == REQUIRED_TYPES
    assert validate_log(mini_log) == []


def test_discriminator_selects_the_class(mini_raw: list[dict[str, Any]]) -> None:
    for raw in mini_raw:
        event = parse_event(json.dumps(raw))
        assert type(event) is EVENT_CLASSES[raw["type"]]
        assert event.seq == raw["seq"] and event.lot == raw["lot"]


def test_unknown_type_and_missing_type_are_rejected(mini_raw: list[dict[str, Any]]) -> None:
    raw = copy.deepcopy(mini_raw[0])
    raw["type"] = "bid_placed"
    bad(raw)
    del raw["type"]
    bad(raw)


@pytest.mark.parametrize("where", ["envelope", "data"])
def test_unknown_fields_are_rejected(mini_raw: list[dict[str, Any]], where: str) -> None:
    raw = first(mini_raw, "talk")
    (raw if where == "envelope" else raw["data"])["surprise"] = 1
    bad(raw, "Extra inputs")


# ----------------------------------------------------------------- canonical JSON bytes


def test_round_trip_is_byte_stable(mini_log: list[AnyEvent]) -> None:
    for event in mini_log:
        line = event_line(event)
        again = parse_event(line)
        assert again == event
        assert event_line(again) == line
    text = dumps_events(mini_log)
    assert text.endswith("\n") and text.count("\n") == len(mini_log)
    assert dumps_events(loads_events(text)) == text


def _check_sorted(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    assert keys == sorted(keys), f"keys not sorted: {keys}"
    return dict(pairs)


def test_keys_are_sorted_at_every_level(mini_log: list[AnyEvent]) -> None:
    for event in mini_log:
        line = event_line(event)
        json.loads(line, object_pairs_hook=_check_sorted)
        assert "\n" not in line


def test_compact_separators_and_utf8_text(mini_log: list[AnyEvent]) -> None:
    line = event_line(next(e for e in mini_log if e.type == "talk"))
    assert '", "' not in line and '": ' not in line
    assert "方糖我不碰" in line, "non-ASCII text is written as is"
    assert "\\u" not in line


def test_log_digest_pins_the_wire_format(mini_log: list[AnyEvent]) -> None:
    assert sha256_digest(dumps_events(mini_log)) == MINI_LOG_DIGEST


def test_int_keys_become_strings_and_come_back_as_ints() -> None:
    data = DealPrivateData(seat=1, budget=300, values={2: 5, 10: 7, 1: 3})
    event = parse_event(
        {"seq": 1, "lot": None, "type": "deal_private", "vis": "seat:1", "data": data.model_dump()}
    )
    line = event_line(event)
    assert '"values":{"1":3,"10":7,"2":5}' in line, "string order, stable for any producer"
    again = parse_event(line)
    assert again.data.values == {1: 3, 2: 5, 10: 7}, "keys are ints again"
    assert event_line(again) == line


@settings(deadline=None)
@given(
    bids=st.dictionaries(st.integers(1, 8), st.integers(0, 10**6), min_size=1, max_size=8),
    lot=st.integers(1, 12),
)
def test_construction_order_never_changes_the_bytes(bids: dict[int, int], lot: int) -> None:
    winner = max(bids, key=lambda seat: (bids[seat], -seat))
    lines = set()
    for order in (sorted(bids), sorted(bids, reverse=True)):
        data = HammerData(
            format="sealed_first",
            winner=winner,
            price=bids[winner],
            payments={winner: bids[winner]},
            bids={seat: bids[seat] for seat in order},
        )
        lines.add(event_line(HammerEvent(seq=1, lot=lot, vis="public", data=data)))
    assert len(lines) == 1
    line = lines.pop()
    assert event_line(parse_event(line)) == line


def test_floats_are_refused() -> None:
    with pytest.raises(ValueError, match="float"):
        canonical_json({"price": 1.5})
    with pytest.raises(ValueError, match="float"):
        canonical_json([{"nested": [0.1]}])
    assert canonical_json({"ratio": 0.5}, allow_float=True) == '{"ratio":0.5}'
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")}, allow_float=True)


def test_logs_hold_no_floats(mini_log: list[AnyEvent]) -> None:
    def walk(node: Any) -> None:
        assert not isinstance(node, float)
        if isinstance(node, dict):
            list(map(walk, node.values()))
        elif isinstance(node, list):
            list(map(walk, node))

    for event in mini_log:
        walk(json.loads(event_line(event)))


# ------------------------------------------------------------- strictness of payloads


@pytest.mark.parametrize(
    "price", ["120.0", '"120"', "true", "-1", str(2**53), "1e2"], ids=lambda p: p
)
def test_amounts_are_strict_non_negative_integers(
    mini_raw: list[dict[str, Any]], price: str
) -> None:
    raw = first(mini_raw, "hammer", format="sealed_first")
    line = json.dumps(raw).replace('"price": 70', f'"price": {price}')
    assert f'"price": {price}' in line
    with pytest.raises(ValidationError):
        parse_event(line)


def test_python_values_are_equally_strict(mini_raw: list[dict[str, Any]]) -> None:
    for value in (70.0, "70", True):
        raw = first(mini_raw, "hammer", format="sealed_first")
        raw["data"]["price"] = value
        bad(raw)


@pytest.mark.parametrize("char", ["\n", "\r", "\x1b", "\x00", "\x85", " "])
def test_single_line_text_has_no_control_characters(
    mini_raw: list[dict[str, Any]], char: str
) -> None:
    raw = first(mini_raw, "talk")
    raw["data"]["text"] = f"hello{char}world"
    bad(raw, "pattern")


def test_block_text_allows_newlines_but_not_escape_sequences(
    mini_raw: list[dict[str, Any]],
) -> None:
    raw = first(mini_raw, "host_line")
    raw["data"]["text"] = "line one\nline two\tindented"
    parse_event(raw)
    raw["data"]["text"] = "red \x1b[31mtext"
    bad(raw, "pattern")


# ------------------------------------------------------------------------- visibility

VIS_EXPECTED = {
    "night_start": {"public"}, "seed_commit": {"public"}, "deal_private": {"seat"},
    "plan": {"seat"}, "lot_open": {"public"}, "talk": {"public"}, "guess": {"seat"},
    "action": {"seat"}, "english_dropout": {"public"}, "dutch_result": {"public"},
    "hammer": {"public"}, "mission_result": {"public"}, "night_end": {"public"},
    "host_line": {"audience"}, "judge_extract": {"audience"}, "judge_verdict": {"audience"},
    "award": {"audience"}, "cost_event": {"system"}, "parse_repair": {"system"},
}  # fmt: skip


def test_each_type_has_one_visibility_level() -> None:
    assert {name: set(kinds) for name, kinds in ALLOWED_VIS.items()} == VIS_EXPECTED


def test_every_wrong_visibility_is_rejected(mini_raw: list[dict[str, Any]]) -> None:
    alternatives = {
        "public": "public",
        "seat": "seat:1",
        "audience": "audience",
        "system": "system",
    }
    for type_, allowed in VIS_EXPECTED.items():
        sample = first(mini_raw, type_)
        for kind, vis in alternatives.items():
            raw = copy.deepcopy(sample)
            raw["vis"] = vis
            if kind in allowed:
                if kind == "seat" and raw["data"].get("seat") not in (None, 1):
                    continue
                parse_event(raw)
            else:
                bad(raw, "cannot have visibility")


@pytest.mark.parametrize(
    "vis", ["seat:0", "seat:9", "seat:", "seat:01", "seat:1x", "Public", "public ", "private", ""]
)
def test_only_the_four_visibility_strings_exist(mini_raw: list[dict[str, Any]], vis: str) -> None:
    raw = first(mini_raw, "talk")
    raw["vis"] = vis
    bad(raw)


def test_vis_seat_must_match_the_seat_in_the_data(mini_raw: list[dict[str, Any]]) -> None:
    raw = first(mini_raw, "plan")
    raw["vis"] = seat_vis(2)
    bad(raw, "does not match")


def test_vis_helpers() -> None:
    assert seat_vis(3) == "seat:3"
    assert vis_kind("seat:3") == "seat" and vis_seat("seat:3") == 3
    assert vis_kind("public") == "public" and vis_seat("public") is None
    for number in (0, 9):
        with pytest.raises(ValueError):
            seat_vis(number)
    with pytest.raises(ValueError):
        vis_kind("seat:12")
    with pytest.raises(ValueError):
        vis_kind("everyone")


def test_viewers_see_only_what_their_level_allows(mini_log: list[AnyEvent]) -> None:
    def seen(viewer: str) -> set[str]:
        return {e.vis for e in mini_log if visible_to(e.vis, viewer)}

    assert seen("seat:1") == {"public", "seat:1"}
    assert seen("seat:2") == {"public", "seat:2"}
    assert seen("seat:4") == {"public"}
    assert seen("audience") == {"public", "audience"}
    assert seen("god") == {"public", "audience", "seat:1", "seat:2"}
    assert seen("system") == {"public", "audience", "seat:1", "seat:2", "system"}


def test_prompt_material_never_contains_another_seats_secrets(mini_log: list[AnyEvent]) -> None:
    canaries = {2: "K9ZP3M", 1: "7QX2KD"}
    for seat, other in ((1, 2), (2, 1)):
        material = "\n".join(event_line(e) for e in mini_log if visible_to(e.vis, seat_vis(seat)))
        assert canaries[other] not in material
    own = "\n".join(event_line(e) for e in mini_log if visible_to(e.vis, "seat:1"))
    assert canaries[1] in own


def test_unknown_viewer_is_an_error() -> None:
    with pytest.raises(ValueError):
        visible_to("public", "everyone")


# ------------------------------------------------------------------------ lot envelope


def test_lot_rules(mini_raw: list[dict[str, Any]]) -> None:
    raw = first(mini_raw, "hammer")
    raw["lot"] = None
    bad(raw, "needs a lot")
    raw = first(mini_raw, "night_start")
    raw["lot"] = 1
    bad(raw, "has no lot")
    raw = first(mini_raw, "action", phase="plan")
    raw["lot"] = 1
    bad(raw, "plan action")
    raw = first(mini_raw, "action", phase="talk")
    raw["lot"] = None
    bad(raw, "plan action")
    raw = first(mini_raw, "host_line")
    raw["lot"] = None
    bad(raw, "per-lot host line")
    raw["data"]["kind"] = "recap"
    raw["lot"] = 1
    bad(raw, "per-lot host line")
    raw["lot"] = None
    parse_event(raw)


def test_lot_numbers_and_seq_are_bounded(mini_raw: list[dict[str, Any]]) -> None:
    raw = first(mini_raw, "hammer")
    for lot in (0, 37, 1.5):
        raw["lot"] = lot
        bad(raw)
    raw["lot"] = 1
    raw["seq"] = 0
    bad(raw)


# --------------------------------------------------------------------- payload coherence


def _mutated(log: list[dict[str, Any]], type_: str, update: dict[str, Any], **where: Any) -> None:
    raw = first(log, type_, **where)
    raw["data"].update(update)
    bad(raw)


@pytest.mark.parametrize(
    ("type_", "where", "update"),
    [
        # actions
        ("action", {"phase": "sealed"}, {"status": "abstain"}),
        ("action", {"phase": "sealed"}, {"status": "abstain", "reason": "timeout", "value": 5}),
        ("action", {"phase": "sealed"}, {"reason": "timeout"}),
        ("action", {"phase": "sealed"}, {"value": None}),
        ("action", {"phase": "sealed"}, {"raw_value": 99}),
        ("action", {"phase": "sealed"}, {"status": "kept_previous"}),
        ("action", {"phase": "plan"}, {"value": 5}),
        ("action", {"phase": "english_init"}, {"raw_value": 342}),
        ("action", {"phase": "english_init"}, {"raw_value": 100}),
        ("action", {"phase": "sealed"}, {"attempts": 0}),
        # hammer
        ("hammer", {"format": "sealed_first"}, {"payments": {1: 70}}),
        ("hammer", {"format": "sealed_first"}, {"payments": {2: 70, 1: 60}}),
        ("hammer", {"format": "sealed_first"}, {"winner": None}),
        ("hammer", {"format": "sealed_first"}, {"unsold": True}),
        ("hammer", {"format": "sealed_first"}, {"price": 71}),
        # dutch
        ("dutch_result", {}, {"price": 120}),
        ("dutch_result", {}, {"level": 11}),
        ("dutch_result", {}, {"unsold": True}),
        ("dutch_result", {}, {"buyer": None}),
        # english, talk, guess
        ("english_dropout", {"seat": 3}, {"participated": False}),
        ("talk", {}, {"pledge": {"kind": "let_win", "lot": 1, "to": 1}}),
        ("talk", {}, {"pledge": {"kind": "max_bid", "lot": 1}}),
        ("talk", {}, {"pledge": {"kind": "no_bid", "lot": 1, "amount": 5}}),
        ("talk", {}, {"pledge": {"kind": "let_win", "lot": 1}}),
        ("talk", {}, {"pledge": {"kind": "no_bid", "lot": 40}}),
        ("guess", {}, {"target": 1}),
        # plan, lot_open, night_start, night_end
        ("plan", {}, {"priorities": [0, 1, 4]}),
        ("plan", {}, {"budget_plan": [0, 40]}),
        ("lot_open", {"format": "english"}, {"sitting_out": [1]}),
        ("lot_open", {"format": "english"}, {"mechanism": "dutch@v1"}),
        ("night_start", {}, {"n_lots": 4}),
        ("night_start", {}, {"budget_lo": 500}),
        ("night_end", {}, {"status": "lost"}),
        # seeds, missions, scores, awards
        ("seed_commit", {"phase": "commit"}, {"seed": "oops"}),
        ("seed_commit", {"phase": "reveal"}, {"seed": None}),
        ("seed_commit", {"phase": "commit"}, {"commitment": "sha256:XYZ"}),
        ("mission_result", {"kind": "collector"}, {"tag": None}),
        ("mission_result", {"kind": "shill"}, {"target_lot": None}),
        ("mission_result", {"kind": "shill"}, {"bonus": 150}),
        ("award", {"award": "backstabber"}, {"award": "best_cook"}),
        ("award", {"award": "backstabber"}, {"vacant": True}),
        ("award", {"award": "keeper"}, {"vacant": False}),
        ("parse_repair", {}, {"codes": ["magic"]}),
        ("judge_verdict", {}, {"verdict": "hunch"}),
    ],
)
def test_payload_coherence(
    mini_raw: list[dict[str, Any]], type_: str, where: dict[str, Any], update: dict[str, Any]
) -> None:
    _mutated(mini_raw, type_, update, **where)


def test_scores_must_add_up(mini_raw: list[dict[str, Any]]) -> None:
    raw = first(mini_raw, "night_end")
    raw["data"]["scores"][0]["total"] = 39
    bad(raw, "total must equal")
    raw = first(mini_raw, "night_end")
    raw["data"]["scores"].append(copy.deepcopy(raw["data"]["scores"][0]))
    bad(raw, "duplicate seat")


def test_mission_kind_selects_the_shape(mini_raw: list[dict[str, Any]]) -> None:
    raw = first(mini_raw, "deal_private", seat=1)
    raw["data"]["mission"]["kind"] = "shill"
    bad(raw)
    raw = first(mini_raw, "deal_private", seat=1)
    raw["data"]["mission"] = None
    parse_event(raw)


def test_clamped_actions_keep_both_numbers(mini_log: list[AnyEvent]) -> None:
    clamped = [e for e in mini_log if e.type == "action" and e.data.status == "clamped"]
    assert len(clamped) == 1
    assert clamped[0].data.value == 342 and clamped[0].data.raw_value == 500


# --------------------------------------------------------------------------- log checks


def test_validate_log_finds_structural_problems(mini_log: list[AnyEvent]) -> None:
    assert validate_log([]) == ["empty log"]
    gap = mini_log[:3] + mini_log[4:]
    assert any("seq" in problem for problem in validate_log(gap))
    assert any("night_start" in p for p in validate_log(mini_log[1:]))
    doubled = [mini_log[0], mini_log[0].model_copy(update={"seq": 2})]
    assert any("more than one night_start" in p for p in validate_log(doubled))


# ------------------------------------------------------------------------ seeds and ties


def test_seed_commitment_is_the_sha256_of_the_text() -> None:
    known = "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert seed_commitment("abc") == known == sha256_digest("abc")
    assert seed_commitment("abd") != known


def test_a_revealed_seed_must_open_its_commitment(mini_raw: list[dict[str, Any]]) -> None:
    raw = first(mini_raw, "seed_commit", phase="reveal")
    parse_event(raw)
    raw["data"]["seed"] = "another-seed"
    bad(raw, "does not open the commitment")
    raw["data"]["seed"] = "bad seed with spaces"
    bad(raw, "pattern")


def test_ties_are_listed_and_the_winner_is_one_of_them(mini_raw: list[dict[str, Any]]) -> None:
    raw = first(mini_raw, "hammer", format="sealed_first")
    raw["data"].update(tie_broken=True, tied=[2, 3], bids={1: 60, 2: 70, 3: 70, 4: 50})
    event = parse_event(raw)
    assert event.data.tied == [2, 3]
    assert event_line(parse_event(event_line(event))) == event_line(event)
    for update in (
        {"tied": [1, 3]},  # the winner (seat 2) did not tie
        {"tied": []},  # tie_broken without the seats
        {"tie_broken": False},  # seats without tie_broken
    ):
        broken = copy.deepcopy(raw)
        broken["data"].update(update)
        bad(broken)
    unsold = copy.deepcopy(raw)
    unsold["data"].update(unsold=True, winner=None, price=0, payments={})
    bad(unsold, "unsold lot")


def test_parse_repair_status_is_optional_and_closed(mini_raw: list[dict[str, Any]]) -> None:
    raw = first(mini_raw, "parse_repair")
    assert parse_event(raw).data.status is None
    for status in ("ok", "retry", "abstain"):
        changed = copy.deepcopy(raw)
        changed["data"]["status"] = status
        assert parse_event(changed).data.status == status
    changed = copy.deepcopy(raw)
    changed["data"]["status"] = "maybe"
    bad(changed)


def test_the_later_judge_and_award_fields_default_to_empty(mini_raw: list[dict[str, Any]]) -> None:
    extract = parse_event(first(mini_raw, "judge_extract")).data
    assert all(x.promise_class is None and x.partner is None for x in extract.extractions)
    verdict = parse_event(first(mini_raw, "judge_verdict")).data
    assert (verdict.topic, verdict.gap_milli, verdict.threshold_milli) == (None, None, None)
    assert verdict.statistic_milli == {}
    assert parse_event(first(mini_raw, "award")).data.basis is None


def test_the_later_judge_and_award_fields_are_checked(mini_raw: list[dict[str, Any]]) -> None:
    verdict = first(mini_raw, "judge_verdict")
    ok = copy.deepcopy(verdict)
    ok["data"].update(topic="budget", gap_milli=250, statistic_milli={3: -40, 4: 310})
    ok["data"]["threshold_milli"] = 265
    assert parse_event(ok).data.statistic_milli == {3: -40, 4: 310}
    for update in ({"topic": "mood"}, {"gap_milli": -1}, {"statistic_milli": {3: 0.5}}):
        broken = copy.deepcopy(verdict)
        broken["data"].update(update)
        bad(broken)
    award = first(mini_raw, "award")
    for basis in ("public", "private"):
        changed = copy.deepcopy(award)
        changed["data"]["basis"] = basis
        assert parse_event(changed).data.basis == basis
    broken = copy.deepcopy(award)
    broken["data"]["basis"] = "secret"
    bad(broken)
