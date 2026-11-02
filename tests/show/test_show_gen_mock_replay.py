"""The script ``tools/gen_mock_replay.py``, which writes the sample ``mock_night.json``.

The sample ``tests/fixtures/replays/mock_night.json`` must be exactly what the generator writes,
and the generator must write the same bytes every time.
"""

from pathlib import Path

import pytest
from export_support import SAMPLE, sample_raw

import gen_mock_replay
from auction_night.contracts import Replay
from auction_night.show.content_scan import ContentGate


def test_the_committed_sample_is_what_the_generator_writes() -> None:
    made = gen_mock_replay.generate().encode("utf-8")
    if made != SAMPLE.read_bytes():
        pytest.fail(
            "tests/fixtures/replays/mock_night.json is out of date (a template, the engine, the "
            "show layer or the version changed): run `uv run python tools/gen_mock_replay.py`"
        )


def test_the_generator_writes_the_same_bytes_every_time_and_reads_nothing_from_outside() -> None:
    assert gen_mock_replay.generate() == gen_mock_replay.generate()
    sample = sample_raw()
    assert sample["manifest"]["git_commit"] is None and sample["manifest"]["created_utc"] is None
    assert sample["manifest"]["revealed_seed"] == "mock-seed-0001"


def test_the_sample_is_a_valid_replay_of_the_mock_night_with_every_show_layer_part() -> None:
    replay = Replay.model_validate(sample_raw())
    assert replay.night.id == "mock-0001" and len(replay.timeline) == 12
    assert replay.host.recap and len(replay.host.per_lot) == 12 and replay.pledges
    assert [e.judge for e in replay.judges] == [0, 1] and replay.judges_absent == []
    assert replay.verdicts and replay.highlights and len(replay.awards) == 10
    assert len(replay.bots_reference) == 4 and len(replay.judge_accuracy) == 2
    assert any(score.stats.mind_error_milli is not None for score in replay.scores)
    assert all(lot.talk_seqs and lot.hammer_seq == lot.last_seq for lot in replay.timeline)
    assert replay.manifest.judge_prompt_hash and replay.manifest.host_prompt_hash
    assert replay.manifest.scan_prompt_hash
    assert ContentGate.builtin().scan(replay).ok


def test_the_generator_checks_and_writes_a_file_of_its_own(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "sample.json"
    assert gen_mock_replay.main(["--check", "--out", str(target)]) == 1
    assert "out of date" in capsys.readouterr().err
    assert gen_mock_replay.main(["--out", str(target)]) == 0
    assert target.read_bytes() == SAMPLE.read_bytes()
    assert gen_mock_replay.main(["--check", "--out", str(target)]) == 0
    assert "up to date" in capsys.readouterr().out
    target.write_text(target.read_text(encoding="utf-8").replace("鲸落", "鲸跌"), encoding="utf-8")
    assert gen_mock_replay.main(["--check", "--out", str(target)]) == 1
