"""The night driver: bots answer on the spot, models through calls, and the layers stay in step.

Most of the night is covered end to end in ``tests/e2e``. These tests look at the driver itself:
what it hands to the runner phase by phase, how it composes a call from the renderer and the
request builder, what it refuses, and the two checks that keep the prompt and the engine honest.
"""

import asyncio
import dataclasses
import json
from typing import Any

import pytest

import auction_night.llm.driver as driver_module
from auction_night.bots import make_bot
from auction_night.contracts import NightConfig
from auction_night.engine.deal import deal
from auction_night.engine.night import Answer, AnswerError, ItemInfo, start, step
from auction_night.llm.cassette import Cassette
from auction_night.llm.driver import DriverError, EngineDriver
from auction_night.llm.nightrun import NightRig, build_rig, play_night
from auction_night.llm.orchestrate import NightRun, PhaseResult
from auction_night.llm.personas import PERSONAS
from auction_night.llm.render import (
    Persona,
    parse_context,
    render_observation,
    render_system,
)
from auction_night.llm.request import build_request, user_message
from auction_night.llm.sanitize import clean_text

MODEL_SEATS = (1, 2, 3, 4, 5, 6)


def reshaped(config: NightConfig, **changes: Any) -> NightConfig:
    """The configuration with top-level parts replaced (``seats``) or merged (``night``)."""
    raw = config.model_dump(mode="json")
    for key, value in changes.items():
        if key == "night":
            raw["night"].update(value)
        else:
            raw[key] = value
    return NightConfig.model_validate(raw)


def bot_seat(number: int, policy: str = "truthful") -> dict[str, Any]:
    return {"seat": number, "codename": f"机器人{number}", "kind": "bot", "policy": policy}


def run_rig(rig: NightRig) -> NightRun:
    async def go() -> NightRun:
        runner = rig.runner()
        try:
            return await runner.run_night(rig.driver, rig.session)
        finally:
            runner.close()

    return asyncio.run(go())


# ============================================================================= a table of bots


def test_a_table_of_bots_needs_no_model_call(mock_config: NightConfig) -> None:
    seats = [bot_seat(n, "truthful" if n % 2 else "economist") for n in range(1, 9)]
    config = reshaped(mock_config, seats=seats, night={"missions": {"collectors": 0, "shills": 0}})
    rig = build_rig(config, Cassette.record())
    assert rig.driver.next_phase() is None and rig.driver.done and rig.clients == {}
    run = run_rig(rig)
    result = rig.driver.finish(run)
    assert run.calls == 0 and result.status == "completed"

    # the same night, played by the engine and the bots alone
    the_deal = deal(config)
    bots = {s.seat: make_bot(s) for s in config.seats}
    state, request = start(config, the_deal)
    while request is not None:
        state, _, request = step(state, {s: bots[s].answer(request) for s in request.seats})
    assert result.events[:-1] == state.events
    assert result.events[-1].type == "cost_event"


# ============================================================================ phase by phase


def spy_on_phases(rig: NightRig) -> list[tuple[Any, Any]]:
    """Record every phase the driver hands out, with the engine request it answers."""
    seen: list[tuple[Any, Any]] = []
    original = rig.driver.next_phase

    def spy() -> Any:
        phase = original()
        if phase is not None:
            seen.append((phase, rig.driver.request))
        return phase

    rig.driver.next_phase = spy  # type: ignore[method-assign]
    return seen


def test_the_driver_hands_the_runner_one_phase_per_engine_request(
    mock_config: NightConfig,
) -> None:
    rig = build_rig(mock_config, Cassette.record())
    seen = spy_on_phases(rig)
    run_rig(rig)
    steps = []
    for phase, request in seen:
        seats = [call.seat for call in phase.calls]
        assert seats == [s for s in request.seats if s <= 6]  # bots are answered on the spot
        assert phase.sequential == (request.phase == "talk")
        if request.phase == "talk":
            assert len(phase.calls) == 1
        for call in phase.calls:
            key = call.key
            assert (key.night, key.lot, key.phase, key.step) == (
                mock_config.night.id,
                request.lot,
                request.phase,
                request.index,
            )
            assert call.role == f"seat:{call.seat}" and call.native == "tools"
        steps.append(request.index)
    assert steps == sorted(set(steps))  # one phase per request, in the order of the engine
    # a talk request for a bot speaker never reaches the runner: 24 of the 96 were left out
    talks = [request for _, request in seen if request.phase == "talk"]
    assert len(talks) == 72


def test_a_call_is_what_the_renderer_and_the_request_builder_make(
    mock_config: NightConfig,
) -> None:
    rig = build_rig(mock_config, Cassette.record())
    driver = rig.driver
    checked = 0
    original = driver.next_phase

    def checking() -> Any:
        nonlocal checked
        phase = original()
        for call in phase.calls if phase is not None else ():
            seat, request = call.seat, driver.request
            events, settings = driver.events, driver.settings
            route = rig.mock.routes[seat]  # type: ignore[union-attr,index]
            observation = render_observation(
                events,
                seat,
                request.phase,
                settings,
                guess_target=request.facts_of(seat).guess_target,
            )
            expected = build_request(
                route.endpoint,
                route.ref,
                system=render_system(events, seat, settings),
                messages=[user_message(observation)],
            )
            assert call.request == expected
            assert call.ctx == parse_context(events, seat, request.phase, settings)
            assert call.endpoint == route.name
            checked += 1
        return phase

    driver.next_phase = checking  # type: ignore[method-assign]
    run_rig(rig)
    assert checked == len(rig.session.cassette) > 150


def test_the_system_prompt_is_rendered_once_per_seat(
    mock_config: NightConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    original = driver_module.render_system

    def counting(events: Any, seat: int, *args: Any, **kwargs: Any) -> str:
        calls.append(seat)
        return original(events, seat, *args, **kwargs)

    monkeypatch.setattr(driver_module, "render_system", counting)
    play_night(mock_config, Cassette.record())
    assert sorted(calls) == list(MODEL_SEATS)


# ================================================================================= what is refused


def test_a_human_seat_cannot_be_driven(mock_config: NightConfig) -> None:
    seats = mock_config.model_dump(mode="json")["seats"]
    seats[0] = {"seat": 1, "codename": "真人", "kind": "human"}
    config = reshaped(mock_config, seats=seats)
    with pytest.raises(DriverError, match="human seat"):
        EngineDriver(config, deal(config), {})


def test_every_model_seat_needs_a_route(mock_config: NightConfig) -> None:
    with pytest.raises(DriverError, match="seat 1 has no route"):
        EngineDriver(mock_config, deal(mock_config), {})


def test_a_persona_of_a_variety_night_must_be_known(mock_config: NightConfig) -> None:
    seats = mock_config.model_dump(mode="json")["seats"]
    seats[0]["persona"] = "铁算盘"
    config = reshaped(mock_config, seats=seats, night={"mode": "variety"})
    rig = build_rig(config, Cassette.record(), personas={"铁算盘": Persona("铁算盘", "斤斤计较")})
    phase = rig.driver.next_phase()
    assert phase is not None
    system = phase.calls[0].request.payload["system"][0]["text"]
    assert "【你的人设】铁算盘：斤斤计较。" in system
    other = phase.calls[1].request.payload["system"][0]["text"]
    assert "【你的人设】" not in other
    with pytest.raises(DriverError, match="persona '铁算盘' is not known"):
        build_rig(config, Cassette.record(), personas={})


def test_the_personas_of_the_package_are_the_default_of_a_variety_night(
    mock_config: NightConfig,
) -> None:
    seats = mock_config.model_dump(mode="json")["seats"]
    for seat, key in zip(seats[:3], PERSONAS, strict=False):
        seat["persona"] = key
    config = reshaped(mock_config, seats=seats, night={"mode": "variety"})
    phase = build_rig(config, Cassette.record()).driver.next_phase()
    assert phase is not None
    for call, key in zip(phase.calls[:3], PERSONAS, strict=False):
        persona = PERSONAS[key]
        system = call.request.payload["system"][0]["text"]
        assert f"【你的人设】{persona.name}：{persona.description}。" in system
    assert "【你的人设】" not in phase.calls[3].request.payload["system"][0]["text"]
    seats[0]["persona"] = "no-such-persona"
    unknown = reshaped(mock_config, seats=seats, night={"mode": "variety"})
    with pytest.raises(
        DriverError, match=r"persona 'no-such-persona' is not known \(known: 铁算盘"
    ):
        build_rig(unknown, Cassette.record())


def test_the_personas_of_the_package_are_short_clean_texts_about_the_tone() -> None:
    assert len(PERSONAS) == 3 and all(key == persona.name for key, persona in PERSONAS.items())
    for persona in PERSONAS.values():
        assert clean_text(persona.name, 64).text == persona.name
        assert clean_text(persona.description, 60).text == persona.description
        assert not any(word in persona.description for word in ("盈余", "目标", "预算", "估值"))


def test_the_driver_is_used_in_the_order_the_runner_uses_it(mock_config: NightConfig) -> None:
    driver = build_rig(mock_config, Cassette.record()).driver
    with pytest.raises(DriverError, match="no phase is waiting"):
        driver.submit(PhaseResult((), ()))
    assert driver.next_phase() is not None
    with pytest.raises(DriverError, match="not been submitted"):
        driver.next_phase()
    with pytest.raises(DriverError, match="was answered for"):
        driver.submit(PhaseResult((), ()))
    tally = NightRun(mock_config.night.id, "completed", (), 0, 0, {}, {})
    with pytest.raises(DriverError, match="not over"):
        driver.finish(tally)


def test_a_run_that_is_not_the_one_the_driver_played_is_refused(
    mock_config: NightConfig,
) -> None:
    rig = build_rig(mock_config, Cassette.record())
    run = run_rig(rig)
    for wrong in (
        dataclasses.replace(run, night="another-night"),
        dataclasses.replace(run, events=()),
        dataclasses.replace(run, events=run.events[:-1]),
        dataclasses.replace(run, events=(*run.events, run.events[-1])),
    ):
        with pytest.raises(DriverError):
            rig.driver.finish(wrong)
    result = rig.driver.finish(run)
    assert rig.driver.finish(run) is result  # closing twice does not write a second tally
    assert sum(e.type == "cost_event" for e in result.events) == 1
    with pytest.raises(DriverError, match="another run"):
        rig.driver.finish(dataclasses.replace(run))


# ================================================================== the two checks of the driver


def test_a_prompt_that_disagrees_with_the_engine_about_the_budget_is_an_error(
    mock_config: NightConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = driver_module.build_view

    def skewed(events: Any, seat: int, *args: Any, **kwargs: Any) -> Any:
        view = original(events, seat, *args, **kwargs)
        return dataclasses.replace(view, spent=view.spent + 1)

    monkeypatch.setattr(driver_module, "build_view", skewed)
    with pytest.raises(DriverError, match=r"shows \d+ coins left, the engine has \d+"):
        play_night(mock_config, Cassette.record())


def test_a_prompt_that_disagrees_with_the_engine_about_a_value_is_an_error(
    mock_config: NightConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = driver_module.build_view

    def skewed(events: Any, seat: int, *args: Any, **kwargs: Any) -> Any:
        view = original(events, seat, *args, **kwargs)
        data = view.deal.data
        shifted = data.model_copy(
            update={
                "values": {lot: v + 1 for lot, v in data.values.items()},
                "signals": {lot: v + 1 for lot, v in data.signals.items()},
            }
        )
        return dataclasses.replace(view, deal=view.deal.model_copy(update={"data": shifted}))

    monkeypatch.setattr(driver_module, "build_view", skewed)
    with pytest.raises(DriverError, match="disagree on lot 1"):
        play_night(mock_config, Cassette.record())


def test_a_prompt_that_disagrees_with_the_engine_about_an_exit_price_is_an_error(
    mock_config: NightConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = driver_module.english_state

    def skewed(view: Any, opening: Any) -> Any:
        return dataclasses.replace(original(view, opening), mine=original(view, opening).mine + 1)

    monkeypatch.setattr(driver_module, "english_state", skewed)
    with pytest.raises(DriverError, match="disagree on the exit price"):
        play_night(mock_config, Cassette.record())


def test_an_action_the_engine_cannot_use_becomes_an_abstention_with_a_repair_event(
    mock_config: NightConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Answer.from_act.__func__  # type: ignore[attr-defined]

    def refusing(cls: type[Answer], seat: int, args: Any, **kwargs: Any) -> Answer:
        if seat == 3 and args["phase"] == "talk" and args["say"]:
            raise AnswerError("the line cannot be recorded")
        return original(cls, seat, args, **kwargs)

    monkeypatch.setattr(Answer, "from_act", classmethod(refusing))
    result = play_night(mock_config, Cassette.record())
    assert result.status == "completed"
    talks = [
        e
        for e in result.events
        if e.type == "action" and e.data.seat == 3 and e.data.phase == "talk"
    ]  # type: ignore[union-attr]
    assert talks and {a.data.reason for a in talks} == {"parse_failed"}  # type: ignore[union-attr]
    assert {a.data.attempts for a in talks} == {1}  # type: ignore[union-attr]
    repairs = [
        e
        for e in result.events
        if e.type == "parse_repair" and e.data.detail.startswith("answer_error")
    ]  # type: ignore[union-attr]
    assert len(repairs) == len(talks)
    data = repairs[0].data  # type: ignore[union-attr]
    assert (data.codes, data.status, data.seat) == (["invalid_input", "fallback"], "abstain", 3)
    assert data.detail == "answer_error=the line cannot be recorded"
    assert data.call_id == talks[0].data.call_id  # type: ignore[union-attr]


# ==================================================================== nobody left to read


def test_the_mind_reading_question_is_left_out_when_nobody_is_left_to_read(
    mock_config: NightConfig,
) -> None:
    config = reshaped(
        mock_config,
        seats=[
            {"seat": 1, "codename": "甲", "kind": "mock", "mock_policy": "honest"},
            bot_seat(2),
        ],
        night={
            "formats": ["sealed_first"],
            "lots_per_format": 3,
            "tags": ["海洋"],
            "tiers": {"100": 3},
            "missions": {"collectors": 0, "shills": 0},
        },
    )
    base = deal(config)
    # seat 2 bids all it has, wins every lot it bids for and is out after the first one
    the_deal = dataclasses.replace(
        base,
        budgets={1: 300, 2: 5},
        lots=tuple(dataclasses.replace(lot, values={1: 0, 2: 100}) for lot in base.lots),
    )
    cassette = Cassette.record()
    result = play_night(config, cassette, the_deal=the_deal)
    assert result.status == "completed"
    hammers = [e.data for e in result.events if e.type == "hammer"]
    assert hammers[0].winner == 2 and hammers[0].newly_out == [2]
    assert all(h.winner is None or h.winner == 1 or h.payments == {2: 5} for h in hammers)

    talks = {
        e.key.lot: e.request["messages"][0]["content"]
        for e in cassette.entries
        if e.key.phase == "talk"
    }
    assert "【读心】请猜 2 号" in talks[1]
    for lot in (2, 3):
        assert "【读心】" not in talks[lot] and "guess_budget 填 0" in talks[lot]
    guesses = [e.lot for e in result.events if e.type == "guess"]
    assert guesses == [1]  # the engine asks for a guess only while there is somebody to read
    # the system text of the seat is still the same bytes for the whole night
    assert len({json.dumps(e.request["system"], sort_keys=True) for e in cassette.entries}) == 1


def test_the_items_and_the_start_of_the_speaking_order_come_from_the_caller(
    mock_config: NightConfig,
) -> None:
    items = tuple(ItemInfo(f"item-{k:02d}", f"测试拍品{k}", "🎁") for k in range(1, 13))
    cassette = Cassette.record()
    result = play_night(mock_config, cassette, items=items, talk_offset=2)
    system = cassette.entries[0].request["system"][0]["text"]
    assert "1. 🎁测试拍品1｜" in system and "12. 🎁测试拍品12｜" in system
    openings = {e.lot: e.data.talk_order for e in result.events if e.type == "lot_open"}  # type: ignore[union-attr]
    assert openings[1] == [3, 4, 5, 6, 7, 8, 1, 2] and openings[2] == [4, 5, 6, 7, 8, 1, 2, 3]
