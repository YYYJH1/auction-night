"""night.memory through the whole model path: the prompt, the recording and the replay.

The mock night is played with ``memory: plan_pledges``. The requests that the cassette kept hold
the section, the section agrees with the log, and the night replays from its recording. A
recording made with the setting off does not fit a configuration that has it on, and the other way
round, because the prompts differ.
"""

import re
from typing import Any

import pytest
from e2e_support import MODEL_SEATS, Played, make_config, play

from auction_night.contracts import NightConfig
from auction_night.contracts.events import NightStartEvent, PlanEvent, TalkEvent
from auction_night.llm.cassette import Cassette, CassetteDrift, CassetteEntry
from auction_night.llm.nightrun import play_night

HEAD = "【你的开场计划与仍然有效的誓约】"
PLEDGES = "仍然有效的誓约（公开，涉及本件和之后的拍品）："
NONE = "（暂无）"


@pytest.fixture(scope="module")
def memory_config() -> NightConfig:
    return make_config(night={"memory": "plan_pledges"})


@pytest.fixture(scope="module")
def remembering(memory_config: NightConfig) -> Played:
    return play(memory_config)


def message_of(entry: CassetteEntry) -> str:
    return str(entry.request["messages"][0]["content"])


def recording_of(played: Played) -> Cassette:
    return Cassette.from_text(played.cassette.text())


def section_of(entry: CassetteEntry) -> list[str]:
    (block,) = [b for b in message_of(entry).split("\n\n") if b.startswith(HEAD)]
    return block.split("\n")


def said(pledge: Any) -> str:
    if pledge.kind == "no_bid":
        return f"对第 {pledge.lot} 件不出价"
    if pledge.kind == "max_bid":
        return f"对第 {pledge.lot} 件出价不超过 {pledge.amount}"
    return f"让 {pledge.to} 号拿下第 {pledge.lot} 件"


def test_the_setting_reaches_every_request_but_the_opening_plan(remembering: Played) -> None:
    entries = remembering.entries()
    assert {e.key.seat for e in entries} == set(MODEL_SEATS)
    for entry in entries:
        count = message_of(entry).count(HEAD)
        assert count == (0 if entry.key.phase == "plan" else 1), entry.key


def test_the_plan_in_each_request_is_the_plan_of_that_seat_for_that_lot(
    remembering: Played,
) -> None:
    plans = {e.data.seat: e.data for e in remembering.events if isinstance(e, PlanEvent)}
    assert set(plans) == set(MODEL_SEATS)
    asked = 0
    for entry in remembering.entries():
        if entry.key.phase == "plan":
            continue
        seat, lot = entry.key.seat, entry.key.lot
        assert seat is not None and lot is not None
        plan = plans[seat]
        lines = section_of(entry)
        priority, budget = plan.priorities[lot - 1], plan.budget_plan[lot - 1]
        left = sum(plan.budget_plan[lot - 1 :])
        assert lines[1] == (
            f"你的开场计划，第 {lot} 件：重视程度 {priority}（0 到 3），打算最多花 {budget}。"
        )
        assert lines[2] == f"计划里本件和之后各件打算最多花的合计：{left}。"
        asked += 1
    assert asked > 100


def test_the_pledges_in_each_request_are_those_made_before_it_and_not_yet_decided(
    remembering: Played,
) -> None:
    start = next(e for e in remembering.events if isinstance(e, NightStartEvent))
    names = {s.seat: s.codename for s in start.data.seats}
    made = [
        (
            e.lot,
            f"{e.data.seat} 号「{names[e.data.seat]}」立誓：{said(e.data.pledge)}",
            e.data.pledge,
        )
        for e in remembering.events
        if isinstance(e, TalkEvent) and e.data.pledge is not None and e.lot is not None
    ]
    assert len(made) > 20
    checked = 0
    for entry in remembering.entries():
        lot = entry.key.lot
        if entry.key.phase == "plan" or lot is None:
            continue
        lines = section_of(entry)
        listed = [line for line in lines[lines.index(PLEDGES) + 1 :] if line != NONE]
        # said in an earlier lot and about this one or a later one: in force for every request
        sure = {text for said_in, text, pledge in made if said_in < lot <= pledge.lot}
        # said in this lot or before: the most that can be in force when the request is made
        most = {text for said_in, text, pledge in made if said_in <= lot <= pledge.lot}
        assert sure <= set(listed) <= most, entry.key
        assert all(int(re.search(r"第 (\d+) 件", text)[1]) >= lot for text in listed)  # type: ignore[index]
        checked += 1
    assert checked > 100


def test_a_night_with_the_setting_on_replays_from_its_recording(
    remembering: Played, memory_config: NightConfig
) -> None:
    cassette = recording_of(remembering)
    replayed = play_night(memory_config, cassette)
    assert replayed.events_text() == remembering.result.events_text()
    assert cassette.unused() == [] and cassette.drifted == ()


def test_a_recording_made_with_the_setting_off_does_not_fit_it_on(
    night: Played, memory_config: NightConfig
) -> None:
    with pytest.raises(CassetteDrift, match="differs from the recording") as caught:
        play_night(memory_config, recording_of(night))
    assert "changed fields: messages" in str(caught.value)


def test_a_recording_made_with_the_setting_on_does_not_fit_it_off(
    remembering: Played, config: NightConfig
) -> None:
    with pytest.raises(CassetteDrift, match="differs from the recording"):
        play_night(config, recording_of(remembering))


def test_the_setting_changes_what_a_seat_is_shown_and_not_what_the_engine_does(
    night: Played, remembering: Played
) -> None:
    """The mock models answer from the deal and not from the text, so the two logs are the same
    while the requests behind them are not."""
    assert remembering.result.events_text() == night.result.events_text()
    assert [message_of(e) for e in remembering.entries()] != [
        message_of(e) for e in night.entries()
    ]
