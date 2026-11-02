"""Replaying a night: no model, no network, and the same log byte for byte.

The cassette holds every attempt of the night under a six-part key and the hash of the request
that was sent. A replay builds the same requests from the same log, compares their hashes with the
recording and hands back the recorded replies. A request that differs is a drift, and the replay
stops.
"""

import dataclasses
import shutil
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from e2e_support import MOCK_CONFIG, Played, make_config

from auction_night.contracts import NightConfig
from auction_night.engine.rules_text import common_rule_text, rule_with_example
from auction_night.llm.cassette import Cassette, CassetteDrift, CassetteMiss
from auction_night.llm.mock import MockClient
from auction_night.llm.nightrun import (
    CALLS_FILE,
    CONFIG_FILE,
    EVENTS_FILE,
    NightRunError,
    build_rig,
    config_path_for,
    describe,
    first_difference,
    load_night_config,
    play_night,
    replay_directory,
    run_directory,
)
from auction_night.llm.render import RenderSettings
from auction_night.prompts import load_templates


def recording_of(night: Played, **kwargs: object) -> Cassette:
    """The recording of a night as it would be loaded from its calls.jsonl."""
    return Cassette.from_text(night.cassette.text(), **kwargs)  # type: ignore[arg-type]


def with_templates(config: NightConfig, **changes: str) -> RenderSettings:
    """Render settings whose templates have some texts replaced by others."""
    templates = load_templates(config.night.lang)
    texts = {**templates.texts, **changes}
    changed = dataclasses.replace(templates, texts=MappingProxyType(texts))
    return RenderSettings.from_config(
        config, rule_with_example, common_rules=common_rule_text, templates=changed
    )


# ============================================================================== replaying


def test_a_replay_gives_back_the_log_byte_for_byte(night: Played, config: NightConfig) -> None:
    cassette = recording_of(night)
    replayed = play_night(config, cassette)
    assert replayed.events_text() == night.result.events_text()
    assert replayed.scores == night.result.scores
    assert cassette.unused() == [] and cassette.drifted == ()


def test_a_replay_calls_no_model_and_needs_no_client(
    night: Played, config: NightConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(self: MockClient, request: object, *, timeout_s: float) -> None:
        raise AssertionError("a replay must not call a model")

    monkeypatch.setattr(MockClient, "complete", forbidden)
    cassette = recording_of(night)
    rig = build_rig(config, cassette)
    assert rig.clients == {}
    replayed = play_night(config, cassette)
    assert replayed.events_text() == night.result.events_text()


def test_the_costs_of_a_replay_come_from_the_recording(priced: Played, config: NightConfig) -> None:
    replayed = play_night(config, recording_of(priced))  # no price list at all
    assert replayed.run.spent_microusd == priced.result.run.spent_microusd > 0
    assert replayed.events_text() == priced.result.events_text()


# ===================================================================== a changed prompt


@pytest.mark.parametrize(
    ("key", "field"),
    [
        ("system.donts", "system"),
        ("system.reply", "system"),
        ("observe.plan_intro", "messages"),
        ("phase.plan", "messages"),
        ("observe.todo", "messages"),
    ],
)
def test_a_changed_template_is_a_hash_mismatch(
    night: Played, config: NightConfig, key: str, field: str
) -> None:
    old = load_templates(config.night.lang).texts[key]
    settings = with_templates(config, **{key: old + "再加一句。"})
    with pytest.raises(CassetteDrift, match="differs from the recording") as caught:
        play_night(config, recording_of(night), render_settings=settings)
    assert f"changed fields: {field}" in str(caught.value)


def test_a_template_used_late_in_the_night_drifts_there(night: Played, config: NightConfig) -> None:
    old = load_templates(config.night.lang).texts["result.sold"]
    settings = with_templates(config, **{"result.sold": old + "（改）"})
    cassette = recording_of(night)
    with pytest.raises(CassetteDrift):
        play_night(config, cassette, render_settings=settings)
    served = cassette.keys()
    assert any(key.lot == 1 for key in served)  # lot 1 was replayed before the first result line


def test_a_changed_rule_of_the_night_is_a_mismatch(night: Played) -> None:
    other = make_config(
        night={"chat": {"max_chars": 50, "window_lots": 2, "persona_max_chars": 60}}
    )
    with pytest.raises(CassetteDrift):
        play_night(other, recording_of(night))


def test_a_different_deal_is_a_mismatch(night: Played) -> None:
    other = make_config(night={"seed": "another-seed-0002"})
    with pytest.raises(CassetteDrift):
        play_night(other, recording_of(night))


def test_an_allowed_drift_is_served_and_listed(night: Played, config: NightConfig) -> None:
    old = load_templates(config.night.lang).texts["system.donts"]
    settings = with_templates(config, **{"system.donts": old + "再加一句。"})
    cassette = recording_of(night, allow_drift=True)
    replayed = play_night(config, cassette, render_settings=settings)
    assert replayed.events_text() == night.result.events_text()
    assert len(cassette.drifted) == len(night.cassette)


def test_a_cassette_with_missing_attempts_is_a_miss(night: Played, config: NightConfig) -> None:
    lines = night.cassette.text().splitlines(keepends=True)
    short = Cassette.from_text("".join(lines[:-3]))
    with pytest.raises(CassetteMiss):
        play_night(config, short)


# ======================================================================== the directory


@pytest.fixture(scope="module")
def rundir(tmp_path_factory: pytest.TempPathFactory, config: NightConfig) -> Path:
    """A night run into a directory once; the tests that change a directory use a copy."""
    out = tmp_path_factory.mktemp("run") / "night"
    run_directory(config, out)
    return out


def test_a_run_directory_replays_to_the_same_log(
    rundir: Path, config: NightConfig, night: Played
) -> None:
    assert sorted(p.name for p in rundir.iterdir()) == [CALLS_FILE, EVENTS_FILE]
    assert (rundir / EVENTS_FILE).read_text(encoding="utf-8") == night.result.events_text()
    assert len((rundir / CALLS_FILE).read_text(encoding="utf-8").splitlines()) == len(
        night.cassette
    )
    report = replay_directory(config, rundir)
    assert report.matches and report.difference is None and report.unused_attempts == 0
    assert report.events == len(night.events)


def test_a_run_refuses_to_replace_an_earlier_run(
    rundir: Path, tmp_path: Path, config: NightConfig
) -> None:
    out = tmp_path / "night"
    shutil.copytree(rundir, out)
    before = (out / EVENTS_FILE).read_bytes()
    with pytest.raises(NightRunError, match="already exists"):
        run_directory(config, out)
    assert (out / EVENTS_FILE).read_bytes() == before
    again = run_directory(config, out, overwrite=True)
    assert again.events_text().encode() == before
    assert replay_directory(config, out).matches


def test_a_night_that_cannot_be_set_up_is_a_usage_error_and_leaves_no_cassette(
    rundir: Path, tmp_path: Path, config: NightConfig
) -> None:
    raw = config.model_dump(mode="json")
    raw["night"]["mode"] = "variety"
    raw["seats"][0]["persona"] = "没有这个人设"
    unknown = NightConfig.model_validate(raw)
    out = tmp_path / "night"
    with pytest.raises(NightRunError, match="persona '没有这个人设' is not known"):
        run_directory(unknown, out)
    assert out.is_dir() and list(out.iterdir()) == []
    again = tmp_path / "again"
    shutil.copytree(rundir, again)
    with pytest.raises(NightRunError, match="is not known"):
        run_directory(unknown, again, overwrite=True)
    assert not (again / CALLS_FILE).exists()  # the cassette of the earlier night does not stay
    with pytest.raises(NightRunError, match="is not known"):
        replay_directory(unknown, rundir)


def test_a_log_that_was_edited_is_reported_with_its_line(
    rundir: Path, tmp_path: Path, config: NightConfig
) -> None:
    out = tmp_path / "night"
    shutil.copytree(rundir, out)
    lines = (out / EVENTS_FILE).read_text(encoding="utf-8").splitlines(keepends=True)
    lines[5] = lines[5].replace('"seq":6', '"seq":7')
    (out / EVENTS_FILE).write_text("".join(lines), encoding="utf-8")
    report = replay_directory(config, out)
    assert not report.matches and report.difference is not None
    assert report.difference.startswith("line 6 differs")


def test_a_cassette_with_attempts_the_night_never_asks_for_does_not_match(
    rundir: Path, tmp_path: Path, config: NightConfig
) -> None:
    out = tmp_path / "night"
    shutil.copytree(rundir, out)
    first = (out / CALLS_FILE).read_text(encoding="utf-8").splitlines()[0]
    extra = first.replace('"attempt":1', '"attempt":2')
    with (out / CALLS_FILE).open("a", encoding="utf-8") as handle:
        handle.write(extra + "\n")
    report = replay_directory(config, out)
    assert not report.matches and report.unused_attempts == 1
    assert report.difference == "1 recorded attempts were never asked for"


def test_a_directory_without_the_files_is_an_error(tmp_path: Path, config: NightConfig) -> None:
    with pytest.raises(NightRunError, match="not found") as caught:
        replay_directory(config, tmp_path)
    assert EVENTS_FILE in str(caught.value) and CALLS_FILE in str(caught.value)


def test_first_difference_names_the_first_line_that_parts() -> None:
    assert first_difference(b"a\nb\n", b"a\nb\n") is None
    assert first_difference(b"a\nb\nc\n", b"a\nx\nc\n") == (
        "line 2 differs: recorded 'b', replayed 'x'"
    )
    assert first_difference(b"a\nb\n", b"a\nb\nc\n") == (
        "the recorded log has 2 lines and the replayed one 3"
    )


def test_the_terminal_report_names_the_status_the_cost_and_every_seat(night: Played) -> None:
    lines = describe(night.result)
    assert lines[0] == "night mock-0001: completed"
    assert lines[1].startswith(f"events {len(night.events)}, model attempts ")
    seat_lines = lines[2:]
    assert len(seat_lines) == 8
    assert seat_lines[0].startswith("  seat 1 鲸落: surplus ")
    assert seat_lines[-1].endswith("(bot)")


def test_the_report_warns_when_the_cost_gate_fired_but_the_log_does_not_say_so(
    night: Played,
) -> None:
    """The engine marks a night as aborted as soon as the cap skips an action, so the books
    agree. Should they ever disagree, the report must not hide it."""
    run = dataclasses.replace(night.result.run, status="budget_aborted")
    odd = dataclasses.replace(night.result, run=run)
    lines = describe(odd)
    assert lines[0] == "night mock-0001: completed"
    assert lines[2].startswith("warning: the cost cap was reached")
    assert len(lines) == len(describe(night.result)) + 1


# =============================================================== the configuration of the night


def test_a_run_saves_the_configuration_file_byte_for_byte_once_the_night_is_played(
    tmp_path: Path, config: NightConfig
) -> None:
    out = tmp_path / "night"
    run_directory(config, out, config_file=MOCK_CONFIG)
    assert sorted(p.name for p in out.iterdir()) == [CALLS_FILE, CONFIG_FILE, EVENTS_FILE]
    assert (out / CONFIG_FILE).read_bytes() == MOCK_CONFIG.read_bytes()
    stored = load_night_config(config_path_for(out))  # the saved file is a configuration
    assert replay_directory(stored, out).matches


def test_a_run_without_a_configuration_file_saves_none_and_leaves_one_alone(
    tmp_path: Path, config: NightConfig
) -> None:
    out = tmp_path / "night"
    out.mkdir()
    (out / CONFIG_FILE).write_text("kept\n", encoding="utf-8")
    run_directory(config, out)
    assert (out / CONFIG_FILE).read_text(encoding="utf-8") == "kept\n"


def test_an_earlier_configuration_is_a_file_of_an_earlier_run(
    tmp_path: Path, config: NightConfig
) -> None:
    out = tmp_path / "night"
    out.mkdir()
    (out / CONFIG_FILE).write_text("stale\n", encoding="utf-8")
    with pytest.raises(NightRunError, match=f"{CONFIG_FILE} already exists.*--overwrite"):
        run_directory(config, out, config_file=MOCK_CONFIG)
    assert [p.name for p in out.iterdir()] == [CONFIG_FILE]  # nothing was played or touched
    run_directory(config, out, overwrite=True, config_file=MOCK_CONFIG)
    assert (out / CONFIG_FILE).read_bytes() == MOCK_CONFIG.read_bytes()


def test_a_night_run_again_in_its_own_directory_keeps_its_configuration(
    tmp_path: Path, config: NightConfig
) -> None:
    out = tmp_path / "night"
    run_directory(config, out, config_file=MOCK_CONFIG)
    saved = out / CONFIG_FILE
    before = saved.read_bytes()
    with pytest.raises(NightRunError, match=f"{EVENTS_FILE} already exists"):
        run_directory(config, out, config_file=saved)  # the log stops it, the file does not
    run_directory(config, out, overwrite=True, config_file=saved)
    assert saved.read_bytes() == before


def test_a_failed_rerun_leaves_no_configuration_of_the_night_before(
    tmp_path: Path, config: NightConfig
) -> None:
    out = tmp_path / "night"
    run_directory(config, out, config_file=MOCK_CONFIG)
    raw = config.model_dump(mode="json")
    raw["limits"]["max_cost_per_night_usd"] = 1.0  # a cost cap needs a price for every model
    capped = NightConfig.model_validate(raw)
    with pytest.raises(NightRunError, match="no price"):
        run_directory(capped, out, overwrite=True, config_file=MOCK_CONFIG)
    assert not (out / CONFIG_FILE).exists() and not (out / EVENTS_FILE).exists()
    assert not (out / CALLS_FILE).exists()  # the night never began, so no cassette stays


def test_a_configuration_file_that_cannot_be_read_stops_the_run_before_it_starts(
    tmp_path: Path, config: NightConfig
) -> None:
    out = tmp_path / "night"
    with pytest.raises(NightRunError, match="cannot read .*none.yaml"):
        run_directory(config, out, config_file=tmp_path / "none.yaml")
    assert not out.exists()


def test_the_configuration_that_is_saved_is_the_one_that_was_read_before_the_night(
    tmp_path: Path, config: NightConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "night.yaml"
    source.write_bytes(MOCK_CONFIG.read_bytes())
    original = source.read_bytes()
    real_play = play_night

    def edit_after_the_play(*args: Any, **kwargs: Any) -> Any:
        result = real_play(*args, **kwargs)
        source.write_text("edited while the night was played\n", encoding="utf-8")
        return result

    monkeypatch.setattr("auction_night.llm.nightrun.play_night", edit_after_the_play)
    run_directory(config, tmp_path / "night", config_file=source)
    assert source.read_bytes() != original
    assert (tmp_path / "night" / CONFIG_FILE).read_bytes() == original


def test_the_configuration_of_a_directory_is_the_one_given_or_the_one_saved(
    tmp_path: Path,
) -> None:
    given = tmp_path / "elsewhere.yaml"
    assert config_path_for(tmp_path, given) == given  # named: not looked for
    with pytest.raises(NightRunError, match="no configuration: give --config"):
        config_path_for(tmp_path)
    (tmp_path / CONFIG_FILE).write_text("x\n", encoding="utf-8")
    assert config_path_for(tmp_path) == tmp_path / CONFIG_FILE
    assert config_path_for(tmp_path, given) == given
    with pytest.raises(NightRunError, match="not a directory"):
        config_path_for(tmp_path / "nowhere")
