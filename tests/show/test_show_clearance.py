"""Releasing findings of the content gate by hand: the file, its rules and the export.

Nothing here touches a network. The replays are the committed sample with a few texts changed, the
classifier is the scripted mock, and a word list needs nothing at all. The same release with a model
classifier on a cassette is tested with the other tests of that classifier, in
``test_show_content_scan.py``.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest
from export_support import NO_GIT, mock_night, sample_raw

from auction_night.contracts import Replay
from auction_night.contracts.replay import ClearedFinding
from auction_night.show.clearance import (
    MAX_REASON_CHARS,
    Clearance,
    ClearanceError,
    load_clearances,
    release,
)
from auction_night.show.content_scan import (
    CLEARABLE,
    ContentGate,
    ContentScanRejected,
    MockClassifier,
    Wordlist,
)
from auction_night.show.replay_export import export_replay

WELFARE = "welfare"
EFFICIENCY = "efficiency"
OPENING = "祝大家好运"  # the first words of six lines of the mock night
SIX = [
    "timeline[0].talk[0].text",
    "timeline[2].talk[0].text",
    "timeline[4].talk[4].text",
    "timeline[6].talk[4].text",
    "timeline[8].talk[0].text",
    "timeline[10].talk[0].text",
]


def changed(edit: Any) -> Replay:
    """The sample with a change made to its plain data."""
    raw = sample_raw()
    edit(raw)
    return Replay.model_validate(raw)


def planes() -> Replay:
    """Three findings for the gate below: a banned word and a plane in one line, and a plane in
    the name of a lot."""

    def edit(raw: dict[str, Any]) -> None:
        raw["timeline"][5]["talk"][0]["text"] = f"{WELFARE} 和 飞机"
        raw["catalog"][0]["name"] = "一架飞机"

    return changed(edit)


PLANE_GATE = ContentGate(Wordlist.builtin(), MockClassifier({"飞机": "brand_or_work"}))
LINE = "timeline[5].talk[0].text"
NAME = "catalog[0].name"


def table(**fields: str) -> str:
    return "[[clear]]\n" + "".join(f'{key} = "{value}"\n' for key, value in fields.items()) + "\n"


def sheet(tmp_path: Path, *tables: str, name: str = "clear.toml") -> Path:
    path = tmp_path / name
    path.write_text("".join(tables), encoding="utf-8")
    return path


GOOD = {"path": LINE, "category": "banned_word", "reason": "座位在引用这个词"}

# ================================================================================= the file


def test_a_clearance_file_is_read_table_by_table_and_comments_are_free(tmp_path: Path) -> None:
    path = sheet(
        tmp_path,
        "# read by a person, 2026-11-01\n",
        table(
            path="host.per_lot[0].text", category="real_person", reason="  「鲸落」是座位代号。 "
        ),
        "# a word list\n",
        table(**GOOD),
    )
    assert load_clearances(path) == (
        Clearance("host.per_lot[0].text", "real_person", "「鲸落」是座位代号。"),
        Clearance(LINE, "banned_word", "座位在引用这个词"),
    )


def test_a_byte_order_mark_from_an_editor_is_no_harm(tmp_path: Path) -> None:
    path = tmp_path / "clear.toml"
    path.write_bytes(b"\xef\xbb\xbf" + table(**GOOD).encode("utf-8"))
    assert load_clearances(path) == (Clearance(LINE, "banned_word", "座位在引用这个词"),)


def test_every_category_a_scan_can_report_is_one_a_person_can_name(tmp_path: Path) -> None:
    assert set(CLEARABLE) == {
        "real_person", "brand_or_work", "hate", "sexual", "violence", "personal_data",
        "credential", "other", "banned_word", "wordlist",
    }  # fmt: skip
    body = [
        table(path=f"seats[{i}].codename", category=c, reason="r") for i, c in enumerate(CLEARABLE)
    ]
    assert [c.category for c in load_clearances(sheet(tmp_path, *body))] == list(CLEARABLE)


BAD_FILES = [
    ("[[clear\n", "is not valid TOML"),
    ("", "found none"),
    ("# nothing to clear\n", "found none"),
    ("cleared = []\n", "unknown key 'cleared'"),
    ('[clear]\npath = "a"\n', "found none"),
    ('clear = "yes"\n', "found none"),
    ("clear = [1]\n", "it is not a table"),
    (table(path=LINE, category="banned_word"), "reason is missing"),
    (table(category="banned_word", reason="r"), "path is missing"),
    (table(path=LINE, reason="r"), "category is missing"),
    (table(**GOOD, extra="x"), "unknown key 'extra'"),
    ('[[clear]]\npath = 3\ncategory = "wordlist"\nreason = "r"\n', "path is not a string"),
    (table(path=LINE, category="wordlist", reason="   "), "reason must be one line of text"),
    (table(path=" ", category="wordlist", reason="r"), "path must be one line of text"),
    (table(path="x" * 65, category="wordlist", reason="r"), "path must be one line of text"),
    (
        f'[[clear]]\npath = "{LINE}"\ncategory = "wordlist"\nreason = """a\nb"""\n',
        "reason must be one line of text",
    ),
    (
        table(path=LINE, category="wordlist", reason="字" * (MAX_REASON_CHARS + 1)),
        f"more than {MAX_REASON_CHARS} characters",
    ),
    (table(path=LINE, category="weird", reason="r"), "category 'weird' is none of"),
    (table(path="batch 1", category="classifier_failed", reason="r"), "nobody has read its texts"),
    (table(**GOOD) + table(**GOOD), "is already in table 1"),
]


@pytest.mark.parametrize(("body", "message"), BAD_FILES)
def test_a_file_that_is_not_as_described_is_refused_with_the_file_and_the_table(
    tmp_path: Path, body: str, message: str
) -> None:
    with pytest.raises(ClearanceError, match="clear.toml") as raised:
        load_clearances(sheet(tmp_path, body))
    assert message in str(raised.value)


def test_a_file_that_cannot_be_read_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ClearanceError, match="cannot read the clearance file none.toml"):
        load_clearances(tmp_path / "none.toml")
    with pytest.raises(ClearanceError, match="cannot read the clearance file"):
        load_clearances(tmp_path)  # a directory
    binary = tmp_path / "binary.toml"
    binary.write_bytes(b"\xff\xfe[[clear]]")
    with pytest.raises(ClearanceError, match="is not UTF-8 text"):
        load_clearances(binary)


def test_the_table_that_is_wrong_is_named_by_its_number(tmp_path: Path) -> None:
    path = sheet(tmp_path, table(**GOOD), table(path=NAME, category="brand_or_work"))
    with pytest.raises(ClearanceError, match=r"\[\[clear\]\] table 2: reason is missing"):
        load_clearances(path)


# ============================================================================== the release


def test_a_released_finding_leaves_the_findings_and_is_recorded_for_the_manifest() -> None:
    report = PLANE_GATE.scan(planes())
    assert [(f.path, f.category) for f in report.findings] == [
        (LINE, "banned_word"), (NAME, "brand_or_work"), (LINE, "brand_or_work"),
    ]  # fmt: skip
    left, records = release(report, [Clearance(LINE, "banned_word", "座位在引用这个词")])
    assert [(f.path, f.category) for f in left.findings] == [
        (NAME, "brand_or_work"), (LINE, "brand_or_work"),
    ]  # fmt: skip
    assert [f.path for f in left.cleared] == [LINE] and not left.ok
    assert left.scanned == report.scanned and left.batches == report.batches
    assert records == (
        ClearedFinding(
            path=LINE, category="banned_word", detail=WELFARE, reason="座位在引用这个词"
        ),
    )
    assert report.cleared == () and len(report.findings) == 3, "the report itself is not changed"
    assert (
        left.summary() == f"content scan: 2 finding(s) in {report.scanned} texts, 1 cleared by hand"
    )


def test_when_every_finding_is_released_the_report_is_ok_and_the_records_keep_the_order() -> None:
    report = PLANE_GATE.scan(planes())
    entries = [
        Clearance(LINE, "brand_or_work", "三"),
        Clearance(NAME, "brand_or_work", "二"),
        Clearance(LINE, "banned_word", "一"),
    ]
    left, records = release(report, entries)
    assert left.ok and left.findings == () and len(left.cleared) == 3
    assert [(r.path, r.category, r.reason) for r in records] == [
        (LINE, "banned_word", "一"), (NAME, "brand_or_work", "二"), (LINE, "brand_or_work", "三"),
    ]  # fmt: skip
    assert [r.detail for r in records] == [WELFARE, "contains '飞机'", "contains '飞机'"]
    assert (
        left.summary() == f"content scan: {report.scanned} texts read, 3 finding(s) cleared by hand"
    )


def test_one_table_releases_every_finding_of_its_category_at_its_place() -> None:
    both = changed(
        lambda raw: raw["timeline"][0]["talk"][0].update(text=f"{WELFARE} 和 {EFFICIENCY}")
    )
    report = ContentGate.builtin().scan(both)
    assert [f.note for f in report.findings] == [WELFARE, EFFICIENCY]
    left, records = release(report, [Clearance(SIX[0], "banned_word", "原话")])
    assert left.ok and [r.detail for r in records] == [WELFARE, EFFICIENCY]
    assert {r.reason for r in records} == {"原话"}


def test_a_word_list_finding_can_be_released_like_a_finding_of_the_classifier() -> None:
    gate = ContentGate(Wordlist.of([OPENING]))
    report = gate.scan(Replay.model_validate(sample_raw()))
    assert {f.category for f in report.findings} == {"wordlist"} and len(report.findings) == 6
    left, records = release(report, [Clearance(path, "wordlist", "开场祝词") for path in SIX])
    assert left.ok and [r.path for r in records] == SIX
    assert {r.detail for r in records} == {OPENING}


def test_a_table_that_matches_no_finding_is_refused_and_nothing_is_released() -> None:
    report = PLANE_GATE.scan(planes())
    good = Clearance(LINE, "banned_word", "r")
    with pytest.raises(ClearanceError, match=r"lists 1 finding\(s\) that the scan of this night"):
        release(report, [good, Clearance("host.recap", "real_person", "r")])
    with pytest.raises(ClearanceError, match=r"host.recap \(real_person\)"):
        release(report, [good, Clearance("host.recap", "real_person", "r")])
    # the place is right and the category is not: that is not the finding that was read
    with pytest.raises(ClearanceError, match=rf"{re.escape(NAME)} \(hate\)"):
        release(report, [Clearance(NAME, "hate", "r")])
    # nothing is reported at all
    with pytest.raises(ClearanceError, match="did not report"):
        release(ContentGate.builtin().scan(Replay.model_validate(sample_raw())), [good])


def test_the_message_for_many_unreported_tables_lists_the_first_ten() -> None:
    report = PLANE_GATE.scan(planes())
    stale = [Clearance(f"seats[{i}].codename", "hate", "r") for i in range(12)]
    with pytest.raises(ClearanceError) as raised:
        release(report, stale)
    message = str(raised.value)
    assert "lists 12 finding(s)" in message and "seats[9].codename (hate)" in message
    assert "seats[10]" not in message and message.endswith("and 2 more")


def test_a_batch_that_could_not_be_classified_cannot_be_released() -> None:
    gate = ContentGate(Wordlist.builtin(), MockClassifier(fail=frozenset({1})))
    report = gate.scan(Replay.model_validate(sample_raw()))
    (failed,) = report.findings
    assert (failed.rule, failed.path, failed.category) == ("classifier_failed", "batch 1", "")
    with pytest.raises(ClearanceError, match="did not report"):
        release(report, [Clearance("batch 1", "other", "r")])
    # with another finding released, the failed batch still stands
    both = ContentGate(
        Wordlist.builtin(), MockClassifier({"飞机": "brand_or_work"}, fail=frozenset({1}))
    ).scan(planes())
    left, _ = release(both, [Clearance(LINE, "banned_word", "r")])
    assert "classifier_failed" in {f.rule for f in left.findings}


# ============================================================================== the export


def run_export(clearances: list[Clearance], gate: ContentGate | None = None) -> Any:
    night = mock_night()
    return export_replay(
        night.config, night.state, night.host.block,
        gate=gate or ContentGate(Wordlist.builtin(), MockClassifier({OPENING: "real_person"})),
        theory=night.theory, cards=night.cards, cassettes=[night.cassette], git=NO_GIT,
        clearances=clearances,
    )  # fmt: skip


def all_six(reason: str = "开场祝词，不是真实人物") -> list[Clearance]:
    return [Clearance(path, "real_person", reason) for path in SIX]


def test_a_night_with_findings_is_refused_without_a_clearance() -> None:
    with pytest.raises(ContentScanRejected) as raised:
        run_export([])
    assert [f.path for f in raised.value.report.findings] == SIX
    assert raised.value.report.cleared == ()


def test_a_night_whose_findings_are_all_released_is_exported_with_them_in_the_manifest() -> None:
    done = run_export(all_six())
    records = done.replay.manifest.cleared_findings
    assert [r.path for r in records] == SIX
    assert {r.category for r in records} == {"real_person"}
    assert {r.detail for r in records} == {f"contains {OPENING!r}"}
    assert {r.reason for r in records} == {"开场祝词，不是真实人物"}
    assert done.report.ok and len(done.report.cleared) == 6
    assert done.report.summary().endswith("6 finding(s) cleared by hand")
    assert Replay.model_validate_json(done.text) == done.replay
    written = json.loads(done.text)["manifest"]["cleared_findings"]
    assert written[0] == {
        "category": "real_person",
        "detail": f"contains {OPENING!r}",
        "path": SIX[0],
        "reason": "开场祝词，不是真实人物",
    }


def test_releasing_findings_changes_the_manifest_and_nothing_else_of_the_replay() -> None:
    released = run_export(all_six()).replay
    clean = run_export([], ContentGate.builtin()).replay
    assert clean.manifest.cleared_findings == []
    bare = released.manifest.model_copy(update={"cleared_findings": []})
    assert released.model_copy(update={"manifest": bare}) == clean


def test_the_same_clearance_gives_the_same_bytes() -> None:
    assert run_export(all_six()).text == run_export(all_six()).text


def test_a_night_with_a_finding_left_is_refused_with_that_finding_alone() -> None:
    with pytest.raises(ContentScanRejected) as raised:
        run_export(all_six()[:5])
    report = raised.value.report
    assert [f.path for f in report.findings] == SIX[5:] and len(report.cleared) == 5
    assert "1 finding(s) in" in str(raised.value) and "5 cleared by hand" in str(raised.value)


def test_a_clearance_that_the_scan_did_not_report_stops_the_export_whatever_else_is_found() -> None:
    extra = [*all_six(), Clearance("host.recap", "real_person", "旧的放行单")]
    with pytest.raises(ClearanceError, match=r"host.recap \(real_person\)"):
        run_export(extra)
    with pytest.raises(ClearanceError, match="lists 1 finding"):
        run_export([Clearance("host.recap", "real_person", "旧的放行单"), *all_six()[:2]])
    with pytest.raises(ClearanceError, match="lists 6 finding"):
        run_export(all_six(), ContentGate.builtin())  # a night with nothing found


def test_the_words_of_a_record_are_not_texts_to_be_scanned_again() -> None:
    """The detail of a word-list finding is the word, and a reason may quote it."""
    gate = ContentGate(Wordlist.of([OPENING]))
    clearances = [Clearance(path, "wordlist", f"座位的原话里有「{OPENING}」") for path in SIX]
    done = run_export(clearances, gate)
    assert {r.detail for r in done.replay.manifest.cleared_findings} == {OPENING}
    again = {f.path for f in gate.scan(done.replay).findings}
    assert again == set(SIX), "the lines of the night are found again, the record is not"
    quoted = run_export(all_six(f"原话里有 {WELFARE} 这个词"))
    assert ContentGate.builtin().scan(quoted.replay).ok
