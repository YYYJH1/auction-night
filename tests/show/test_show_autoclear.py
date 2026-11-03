"""Findings that a rule of the gate releases: the two rules, what they leave alone, the record.

Nothing here touches a network. The replays are the committed sample with a few texts changed. The
reports are written by hand where the rules alone are tested, and come from a fake model behind
the client interface and a cassette where the export is.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from export_support import (
    NO_GIT,
    FakeClient,
    config_with,
    mock_night,
    probes_of,
    sample_raw,
)

from auction_night.contracts import NightConfig, Replay
from auction_night.contracts.replay import ClearedFinding
from auction_night.llm.cassette import Cassette
from auction_night.show.autoclear import auto_release, game_word, names_quoted
from auction_night.show.clearance import Clearance
from auction_night.show.content_scan import (
    ContentGate,
    ContentScanRejected,
    Finding,
    ScanReport,
    public_texts,
)
from auction_night.show.replay_export import build_replay, export_replay

REPO = Path(__file__).resolve().parents[2]
HOST = "timeline[0].host"  # 「第 1 件拍品 1 号，6 号麦芽以 108 拍得。5 号青柠违背誓约：...」
TAGS = frozenset({"海洋", "机械", "甜品", "怪谈"})


def extraction(**fields: Any) -> dict[str, Any]:
    """A claim of a judge about the mission of seat 2, as plain data."""
    base: dict[str, Any] = {
        "seat": 2,
        "lot": 3,
        "kind": "claim",
        "quote": "我是托儿",
        "ambiguous": False,
        "topic": "mission_kind",
        "number": None,
        "text_value": None,
        "promise_class": None,
        "partner": None,
    }
    return {**base, **fields}


def with_extractions(*items: dict[str, Any]) -> Replay:
    """The sample whose first judge starts with these extractions."""
    raw = sample_raw()
    raw["judges"][0]["extractions"][: len(items)] = [extraction(**item) for item in items]
    return Replay.model_validate(raw)


def place(index: int) -> str:
    return f"judges[0].extractions[{index}].text_value"


def flagged(
    path: str, category: str, note: str, rule: str = "classifier", where: str = ""
) -> Finding:
    detail = f"{category}: {note}" if rule == "classifier" else note
    return Finding(path, where, rule, detail, category=category, note=note)


def text_at(replay: Replay, path: str) -> str:
    return next(item.text for item in public_texts(replay) if item.path == path)


def sample() -> Replay:
    return Replay.model_validate(sample_raw())


# ===================================================================== a word of the game


def test_a_word_of_the_game_is_released_whatever_category_the_classifier_chose() -> None:
    replay = with_extractions(
        {"topic": "mission_kind", "text_value": "shill"},
        {"topic": "mission_kind", "text_value": " Collector "},
        {"topic": "mission_kind", "text_value": "none"},
        {"topic": "mission_tag", "text_value": "海洋"},
    )
    categories = ["other", "brand_or_work", "credential", "other"]
    report = ScanReport(500, tuple(flagged(place(i), c, "疑似") for i, c in enumerate(categories)))
    left, records = auto_release(replay, report)
    assert left.ok and left.findings == () and left.auto_cleared == report.findings
    assert [r.path for r in records] == [place(i) for i in range(4)]
    assert [r.category for r in records] == categories
    assert {r.detail for r in records} == {"疑似"}
    assert (
        records[0].reason == "text_value 是任务类型的取值「shill」，属于游戏枚举，按枚举校验通过。"
    )
    assert (
        records[1].reason
        == "text_value 是任务类型的取值「Collector」，属于游戏枚举，按枚举校验通过。"
    )
    assert (
        records[3].reason == "text_value 是本夜拍品的标签「海洋」，属于游戏枚举，按枚举校验通过。"
    )
    assert report.auto_cleared == () and left.cleared == (), "the report itself is not changed"
    assert left.summary() == "content scan: 500 texts read, 4 finding(s) cleared by rule"


def test_a_value_outside_the_list_of_its_topic_stands() -> None:
    replay = with_extractions(
        {"topic": "mission_kind", "text_value": "不碰"},
        {"topic": "mission_tag", "text_value": "古风"},
        {"kind": "promise", "topic": "plan", "text_value": "shill"},
        {"topic": "budget", "text_value": "shill"},
        {"topic": "mission_tag", "text_value": "shill"},
        {"topic": "mission_kind", "text_value": "海洋"},
    )
    report = ScanReport(500, tuple(flagged(place(i), "other", "疑似") for i in range(6)))
    left, records = auto_release(replay, report)
    assert left.findings == report.findings and records == () and left.auto_cleared == ()


def test_only_the_classifier_is_overruled_and_only_at_the_text_value() -> None:
    replay = with_extractions({"topic": "mission_kind", "text_value": "shill"})
    findings = (
        flagged(place(0), "wordlist", "shill", rule="wordlist"),
        flagged(place(0), "banned_word", "shill", rule="banned_word"),
        Finding("batch 3", "", "classifier_failed", "texts 1 to 40 could not be classified"),
        flagged("judges[0].extractions[0].quote", "other", "疑似"),
        flagged("catalog[0].tag", "brand_or_work", "疑似"),
        flagged("judges[1].extractions[0].text_value", "other", "疑似"),
        flagged("judges[7].extractions[0].text_value", "other", "疑似"),
    )
    left, records = auto_release(replay, ScanReport(500, findings))
    assert left.findings == findings and records == ()


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"topic": "mission_kind", "text_value": "collector"}, "mission_kind"),
        ({"topic": "mission_kind", "text_value": "shill"}, "mission_kind"),
        ({"topic": "mission_kind", "text_value": "none"}, "mission_kind"),
        ({"topic": "mission_kind", "text_value": " SHILL "}, "mission_kind"),
        ({"topic": "mission_tag", "text_value": "怪谈"}, "mission_tag"),
        ({"topic": "mission_tag", "text_value": " 海洋 "}, "mission_tag"),
        ({"topic": "mission_kind", "text_value": "托儿"}, None),
        ({"topic": "mission_kind", "text_value": "海洋"}, None),
        ({"topic": "mission_tag", "text_value": "collector"}, None),
        ({"topic": "mission_tag", "text_value": "海"}, None),
        ({"topic": "mission_kind", "text_value": None}, None),
        ({"topic": "plan", "text_value": "shill"}, None),
        ({"topic": None, "text_value": "shill"}, None),
        ({"kind": "promise", "topic": "mission_kind", "text_value": "shill"}, None),
    ],
)
def test_the_lists_are_the_mission_kinds_and_the_tags_of_the_night(
    fields: dict[str, Any], expected: str | None
) -> None:
    replay = with_extractions(fields)
    claim = replay.judges[0].extractions[0]
    assert game_word(claim, TAGS) == expected


# ======================================================================== a code name


@pytest.mark.parametrize(
    ("note", "names"),
    [
        ("使用真实人物姓名「麦芽」指代座位", ["麦芽"]),
        ("「麦芽」和「青柠」像真人姓名", ["麦芽", "青柠"]),
        ("「青柠」「麦芽」「青柠」", ["青柠", "麦芽"]),
        ("“麦芽”是网络昵称", ["麦芽"]),
        ('"麦芽"像真名', ["麦芽"]),
        ("『麦芽』像真名", ["麦芽"]),
    ],
)
def test_a_code_name_that_was_taken_for_a_person_is_released(note: str, names: list[str]) -> None:
    replay = sample()
    assert all(name in text_at(replay, HOST) for name in names)
    left, records = auto_release(replay, ScanReport(500, (flagged(HOST, "real_person", note),)))
    listed = "".join(f"「{name}」" for name in names)
    assert left.ok
    assert records == (
        ClearedFinding(
            path=HOST,
            category="real_person",
            detail=note,
            reason=f"分类器指出的名字{listed}只是本夜的座位代号。",
        ),
    )


@pytest.mark.parametrize(
    "note",
    [
        "「周杰伦」是真实人物",
        "「麦芽」和「周杰伦」都像真人姓名",
        "座位代号被当成真人姓名",
        "疑似真实网络人物",
        "「」",
        "「 」",
        "「麦芽们」像真名",
        "「麦」像真名",
        "「鲸落」像真名",  # a code name of the night, but not in this text
        "「麦芽」「鲸落」像真名",
    ],
)
def test_a_reason_that_names_someone_else_or_no_one_leaves_the_finding_standing(note: str) -> None:
    report = ScanReport(500, (flagged(HOST, "real_person", note),))
    left, records = auto_release(sample(), report)
    assert left.findings == report.findings and records == () and not left.ok


def test_a_code_name_is_released_only_as_a_real_person_finding_of_the_classifier() -> None:
    note = "「麦芽」像真名"
    findings = (
        flagged(HOST, "brand_or_work", note),
        flagged(HOST, "other", note),
        flagged(HOST, "real_person", note, rule="wordlist"),
        flagged("batch 2", "real_person", note),
    )
    left, records = auto_release(sample(), ScanReport(500, findings))
    assert left.findings == findings and records == ()


def test_the_names_must_be_code_names_of_this_night() -> None:
    raw = sample_raw()
    for seat in raw["seats"]:
        if seat["codename"] == "麦芽":
            seat["codename"] = "麦穗"
    report = ScanReport(500, (flagged(HOST, "real_person", "「麦芽」像真名"),))
    left, records = auto_release(Replay.model_validate(raw), report)
    assert left.findings == report.findings and records == ()


@pytest.mark.parametrize(
    ("reason", "names"),
    [
        ("使用真实人物姓名「阿铁」指代座位", ["阿铁"]),
        ("「甲」「乙」「甲」", ["甲", "乙"]),
        ('“a” 和 "b"', ["a", "b"]),
        ("『x』", ["x"]),
        ("「 」「」", []),
        ("没有引号", []),
        ("「没有结尾", []),
        ("", []),
    ],
)
def test_the_names_of_a_reason_are_those_in_quotation_marks(reason: str, names: list[str]) -> None:
    assert names_quoted(reason) == names


# ================================================================ the report and its order


def test_released_and_standing_findings_keep_their_order_and_earlier_releases_are_kept() -> None:
    replay = with_extractions({"topic": "mission_kind", "text_value": "shill"})
    keep = flagged("catalog[0].name", "brand_or_work", "品牌")
    first = flagged(place(0), "other", "疑似暗号")
    second = flagged(HOST, "real_person", "「青柠」像真名")
    earlier = flagged("seats[1].codename", "real_person", "「阿铁」")
    report = ScanReport(500, (first, keep, second), 12, (), (earlier,))
    left, records = auto_release(replay, report)
    assert left.findings == (keep,) and left.auto_cleared == (earlier, first, second)
    assert [r.path for r in records] == [place(0), HOST]
    assert (left.scanned, left.batches) == (500, 12) and report.auto_cleared == (earlier,)


def test_the_summary_counts_what_a_rule_released_beside_what_a_person_released() -> None:
    one = flagged(HOST, "real_person", "x")
    assert ScanReport(500).summary() == "content scan: 500 texts read, nothing found"
    assert ScanReport(500, auto_cleared=(one,)).summary() == (
        "content scan: 500 texts read, 1 finding(s) cleared by rule"
    )
    assert ScanReport(500, cleared=(one,), auto_cleared=(one, one)).summary() == (
        "content scan: 500 texts read, 1 finding(s) cleared by hand, 2 finding(s) cleared by rule"
    )
    assert ScanReport(500, (one,), auto_cleared=(one,)).summary() == (
        "content scan: 1 finding(s) in 500 texts, 1 cleared by rule"
    )
    assert ScanReport(500, (one,), cleared=(one,), auto_cleared=(one,)).summary() == (
        "content scan: 1 finding(s) in 500 texts, 1 cleared by hand, 1 cleared by rule"
    )


def test_the_places_the_rules_look_up_are_the_places_the_scan_reports() -> None:
    replay = with_extractions({"topic": "mission_kind", "text_value": "shill"})
    paths = [item.path for item in public_texts(replay)]
    assert place(0) in paths and HOST in paths


# ================================================================================ the export


def classifier_config() -> NightConfig:
    return config_with(
        show={
            "host_offline": {"kind": "mock"},
            "judges": [{"kind": "mock"}, {"kind": "mock"}],
            "content_scan": {
                "wordlist": None,
                "classifier": {
                    "kind": "llm",
                    "endpoint": "scan_ep",
                    "model": "scan-model",
                    "vendor": "vendor-s",
                },
            },
        },
        endpoints={"scan_ep": {"kind": "openai_compatible", "base_url": "http://localhost:9/v1"}},
        limits={
            "call_timeout_s": 5,
            "syntax_retries": 1,
            "transient_retries": 2,
            "max_cost_per_night_usd": None,
            "prices_file": None,
        },
    )


def model_gate(cassette: Cassette, client: FakeClient | None) -> ContentGate:
    return ContentGate.from_config(
        classifier_config(),
        cassette=cassette,
        client=client,
        clock=lambda: "2026-11-01T00:00:00.000Z",
        sleep=lambda s: None,
    )


def about_the_first_lot(text: str) -> bool:
    return text.startswith("第 1 件拍品") and "麦芽" in text


def classify(request: Any, call_number: int) -> str:
    """A model that takes a code name for a person in the host's line of lot 1, and the name of
    the first seat for a person in a way that names no one."""
    rows = []
    for row in probes_of(request):
        if about_the_first_lot(row["text"]):
            reason = "使用真实人物姓名「麦芽」指代座位"
            rows.append({"id": row["id"], "category": "real_person", "reason": reason})
        elif row["text"] == "鲸落":
            rows.append({"id": row["id"], "category": "real_person", "reason": "疑似真实网络人物"})
    return json.dumps({"flagged": rows}, ensure_ascii=False)


def export_from(heard: Cassette, clearances: list[Clearance]) -> Any:
    night = mock_night()
    return export_replay(
        night.config, night.state, night.host.block, gate=model_gate(heard, None),
        theory=night.theory, cards=night.cards, git=NO_GIT, cassettes=[night.cassette, heard],
        clearances=clearances,
    )  # fmt: skip


def recording() -> tuple[Cassette, ScanReport, list[str]]:
    """The recording that ``auction-night show`` would make of the mock night: a plain scan."""
    night = mock_night()
    cassette = Cassette.record()
    scanned = build_replay(
        night.config, night.state, night.host.block, theory=night.theory, cards=night.cards,
        git=NO_GIT, cassettes=[night.cassette],
    )  # fmt: skip
    report = model_gate(cassette, FakeClient(classify)).scan(scanned)
    named = [item.path for item in public_texts(scanned) if about_the_first_lot(item.text)]
    return Cassette.from_text(cassette.text()), report, named


def test_the_scan_is_what_it_was_and_the_recording_of_the_classifier_still_fits() -> None:
    heard, report, named = recording()
    assert [f.path for f in report.findings] == ["seats[0].codename", *named] and len(named) == 2
    assert report.auto_cleared == ()
    done = export_from(heard, [Clearance("seats[0].codename", "real_person", "它是座位代号")])
    assert heard.unused() == [], "the export asked for every recorded call, and no other"
    assert done.report.ok


def test_a_rule_releases_after_the_clearance_file_and_the_manifest_lists_the_two_apart() -> None:
    heard, _, named = recording()
    kept = [Clearance("seats[0].codename", "real_person", "它是座位代号")]
    done = export_from(heard, kept)
    manifest = done.replay.manifest
    assert [c.path for c in manifest.cleared_findings] == ["seats[0].codename"]
    assert manifest.cleared_findings[0].reason == "它是座位代号"
    assert [c.path for c in manifest.auto_cleared_findings] == named
    assert {c.category for c in manifest.auto_cleared_findings} == {"real_person"}
    assert {c.detail for c in manifest.auto_cleared_findings} == {
        "使用真实人物姓名「麦芽」指代座位"
    }
    assert {c.reason for c in manifest.auto_cleared_findings} == {
        "分类器指出的名字「麦芽」只是本夜的座位代号。"
    }
    assert done.report.ok and len(done.report.cleared) == 1 and len(done.report.auto_cleared) == 2
    assert done.report.summary().endswith(
        "1 finding(s) cleared by hand, 2 finding(s) cleared by rule"
    )
    written = json.loads(done.text)["manifest"]
    assert written["auto_cleared_findings"][0] == {
        "category": "real_person",
        "detail": "使用真实人物姓名「麦芽」指代座位",
        "path": named[0],
        "reason": "分类器指出的名字「麦芽」只是本夜的座位代号。",
    }
    assert Replay.model_validate_json(done.text) == done.replay
    assert export_from(heard, kept).text == done.text, "the same recording, the same bytes"


def test_a_finding_that_no_rule_releases_still_refuses_the_night() -> None:
    heard, _, named = recording()
    with pytest.raises(ContentScanRejected) as raised:
        export_from(heard, [])
    report = raised.value.report
    assert [f.path for f in report.findings] == ["seats[0].codename"]
    assert [f.path for f in report.auto_cleared] == named
    assert "1 finding(s) in" in str(raised.value) and "2 cleared by rule" in str(raised.value)


def test_a_person_may_list_a_finding_that_a_rule_would_release() -> None:
    heard, _, named = recording()
    mine = [
        Clearance("seats[0].codename", "real_person", "它是座位代号"),
        Clearance(named[0], "real_person", "主持人的话里是座位代号"),
    ]
    done = export_from(heard, mine)
    manifest = done.replay.manifest
    assert [c.path for c in manifest.cleared_findings] == ["seats[0].codename", named[0]]
    assert [c.path for c in manifest.auto_cleared_findings] == named[1:]
    assert manifest.cleared_findings[1].reason == "主持人的话里是座位代号"


def test_releasing_by_rule_changes_the_manifest_and_nothing_else_of_the_replay() -> None:
    heard, _, _ = recording()
    kept = [Clearance("seats[0].codename", "real_person", "它是座位代号")]
    ruled = export_from(heard, kept).replay
    by_hand = export_from(
        heard,
        [
            *kept,
            *(
                Clearance(c.path, "real_person", c.reason)
                for c in ruled.manifest.auto_cleared_findings
            ),
        ],
    ).replay
    assert by_hand.manifest.auto_cleared_findings == []
    assert len(by_hand.manifest.cleared_findings) == 3
    bare = ruled.manifest.model_copy(update={"auto_cleared_findings": []})
    other = by_hand.manifest.model_copy(update={"cleared_findings": bare.cleared_findings})
    assert ruled.model_copy(update={"manifest": bare}) == by_hand.model_copy(
        update={"manifest": other}
    )


def test_a_night_without_any_of_these_findings_has_an_empty_list() -> None:
    done = export_replay(
        mock_night().config, mock_night().state, mock_night().host.block,
        gate=ContentGate.builtin(), theory=mock_night().theory, cards=mock_night().cards,
        git=NO_GIT, cassettes=[mock_night().cassette],
    )  # fmt: skip
    assert done.replay.manifest.auto_cleared_findings == []
    assert done.report.auto_cleared == ()
    assert json.loads(done.text)["manifest"]["auto_cleared_findings"] == []


# ===================================================================== data written before


def test_a_replay_published_before_the_list_existed_still_validates() -> None:
    text = (REPO / "seasons" / "s1" / "s1-d01-r1" / "replay.json").read_text(encoding="utf-8")
    assert '"auto_cleared_findings"' not in text
    replay = Replay.model_validate_json(text)
    assert replay.manifest.auto_cleared_findings == []
    assert [c.path for c in replay.manifest.cleared_findings] == [
        "judges[1].extractions[33].text_value"
    ]
