"""The replays in ``docs/demo``, which the READMEs show, must keep working.

A change of a contract or of the content gate can make such a file invalid or refused without any
other test noticing. The replay page reads the same files in ``tests/viewer/demo.test.js``.
"""

from pathlib import Path

import pytest

from auction_night.contracts import Replay
from auction_night.show.content_scan import ContentGate

DEMO = Path(__file__).resolve().parents[1] / "docs" / "demo"
FILES = sorted(DEMO.glob("*.json"))


def load(path: Path) -> Replay:
    return Replay.model_validate_json(path.read_text(encoding="utf-8"))


def test_the_demo_folder_holds_a_replay() -> None:
    assert FILES, "docs/demo holds no replay, and the READMEs link to one"


@pytest.mark.parametrize("path", FILES, ids=lambda path: path.name)
def test_a_demo_replay_is_valid_and_named_after_its_night(path: Path) -> None:
    replay = load(path)
    assert path.stem == replay.night.id


@pytest.mark.parametrize("path", FILES, ids=lambda path: path.name)
def test_a_demo_replay_passes_the_word_list_of_the_project(path: Path) -> None:
    report = ContentGate.builtin().scan(load(path))
    assert report.ok, "\n".join(report.lines())
