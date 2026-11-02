"""A whole mock night, from the first deal to the final scores, through the model path.

Six scripted mock seats and two rule bots play twelve lots in all six formats. Every model call
is rendered from the event log, built into a request, answered by the mock client, read by the
parsing ladder and recorded in the cassette. Nothing touches a network.
"""

import asyncio
from collections import Counter

import pytest
from e2e_support import BOT_SEATS, MODEL_SEATS, Played, make_config, play

from auction_night.contracts import (
    NightConfig,
    dumps_events,
    loads_events,
    validate_log,
    visible_to,
)
from auction_night.contracts.events import HammerEvent
from auction_night.engine.deal import deal
from auction_night.engine.scoring import conservation_gap
from auction_night.llm.cassette import Cassette
from auction_night.llm.nightrun import build_rig
from auction_night.llm.orchestrate import ModelRunner

# ============================================================================== the log


def test_the_night_plays_to_the_end_and_the_log_is_valid(night: Played) -> None:
    events = night.events
    assert night.result.status == "completed"
    assert validate_log(events) == []
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert events[0].type == "night_start"
    assert [e.type for e in events if e.type == "night_end"] == ["night_end"]
    # the closing tally of costs follows night_end and is the last event of the log
    assert events[-2].type == "night_end"
    assert events[-1].type == "cost_event" and events[-1].data.kind == "tally"  # type: ignore[union-attr]


def test_the_log_survives_a_round_trip_through_the_contract(night: Played) -> None:
    text = dumps_events(night.events)
    again = loads_events(text)
    assert again == list(night.events)
    assert dumps_events(again) == text
    assert text == night.result.events_text()


def test_the_log_holds_what_a_twelve_lot_night_has(night: Played) -> None:
    counts = Counter(e.type for e in night.events)
    assert counts["night_start"] == 1 and counts["night_end"] == 1
    assert counts["deal_private"] == 8 and counts["plan"] == 6
    assert counts["lot_open"] == 12 and counts["hammer"] == 12
    assert counts["talk"] == 8 * 12  # bots speak too
    assert counts["mission_result"] == 6
    assert counts["guess"] == 6 * 12 and counts["cost_event"] == 1
    show_layer = {"host_line", "judge_extract", "judge_verdict", "award"}
    assert not show_layer & set(counts)  # no show layer is involved yet
    assert {e.vis for e in night.events if e.type == "hammer"} == {"public"}


def test_the_log_uses_three_of_the_four_visibility_levels(night: Played) -> None:
    kinds = {e.vis if e.vis in ("public", "system") else "seat" for e in night.events}
    assert kinds == {"public", "seat", "system"}  # nothing is written for the audience yet
    system = [e for e in night.events if e.vis == "system"]
    assert system and not any(
        visible_to(e.vis, viewer) for e in system for viewer in ("seat:1", "audience", "god")
    )


def test_system_events_come_from_the_model_layer_only(night: Played) -> None:
    system = [e for e in night.events if e.vis == "system"]
    assert {e.type for e in system} <= {"parse_repair", "cost_event"}
    assert all(e.data.seat in MODEL_SEATS for e in system if e.type == "parse_repair")  # type: ignore[union-attr]
    # a repair stands just before the engine events of the step it belongs to
    actions = {a.data.call_id: a.seq for a in night.of_type("action") if a.data.call_id}
    repairs = [e for e in system if e.type == "parse_repair"]
    assert repairs
    for repair in repairs:
        assert 0 < actions[repair.data.call_id] - repair.seq < 20  # type: ignore[union-attr]


# ============================================================================ the scores


def test_the_scores_in_the_log_are_the_scores_of_the_state(night: Played) -> None:
    end = night.of_type("night_end")[0]
    assert end.data.scores == list(night.result.scores)
    assert night.of_type("mission_result")
    assert [m.data for m in night.of_type("mission_result")] == list(night.result.missions)


def test_the_books_balance(night: Played, config: NightConfig) -> None:
    hammers = [e.data for e in night.events if isinstance(e, HammerEvent)]
    the_deal = deal(config)
    assert len(hammers) == 12
    assert conservation_gap(night.result.scores, the_deal.lots, hammers) == 0
    bonuses = sum(s.mission_bonus for s in night.result.scores)
    assert bonuses == 150 * sum(m.completed for m in night.result.missions)


def test_budget_is_never_overspent(night: Played, config: NightConfig) -> None:
    the_deal = deal(config)
    spent = dict.fromkeys(range(1, 9), 0)
    for event in night.events:
        if isinstance(event, HammerEvent):
            for seat, paid in event.data.payments.items():
                spent[seat] += paid
    assert all(spent[seat] <= the_deal.budgets[seat] for seat in spent)
    assert {s.seat: s.spent for s in night.result.scores} == spent


def test_only_model_seats_have_a_rank(night: Played) -> None:
    ranks = {s.seat: s.rank_tonight for s in night.result.scores}
    assert all(ranks[seat] is not None for seat in MODEL_SEATS)
    assert all(ranks[seat] is None for seat in BOT_SEATS)


# ===================================================================== who is called when


def test_only_the_model_seats_are_called(night: Played) -> None:
    seats = Counter(entry.key.seat for entry in night.cassette.entries)
    assert set(seats) == set(MODEL_SEATS)
    assert not set(seats) & set(BOT_SEATS)
    assert night.result.run.status == "completed" and night.result.run.calls == len(night.cassette)


def test_every_model_seat_plans_once_and_talks_on_every_lot(night: Played) -> None:
    for seat in MODEL_SEATS:
        plans = night.entries(seat, "plan")
        talks = night.entries(seat, "talk")
        assert len(plans) == 1 and plans[0].key.lot is None
        assert [e.key.lot for e in talks if e.key.attempt == 1] == list(range(1, 13))


def test_talk_is_one_seat_at_a_time_and_bids_are_all_at_once(night: Played) -> None:
    by_step: dict[int, set[int]] = {}
    phases: dict[int, str] = {}
    for entry in night.cassette.entries:
        by_step.setdefault(entry.key.step, set()).add(entry.key.seat)  # type: ignore[arg-type]
        phases[entry.key.step] = entry.key.phase
    talk_steps = [step for step, phase in phases.items() if phase == "talk"]
    bid_steps = [step for step, phase in phases.items() if phase not in ("talk", "plan")]
    assert all(len(by_step[step]) == 1 for step in talk_steps)
    assert max(len(by_step[step]) for step in bid_steps) >= 5
    assert len(by_step[min(phases)]) == 6  # the opening plan: all six at once


def test_the_steps_follow_the_requests_of_the_engine(night: Played) -> None:
    steps = sorted({entry.key.step for entry in night.cassette.entries})
    assert steps[0] == 0 and steps == sorted(set(steps))
    # every step of a lot comes after the steps of the lots before it
    last_step_of_lot = {}
    for entry in night.cassette.entries:
        if entry.key.lot is not None:
            last_step_of_lot[entry.key.lot] = max(
                last_step_of_lot.get(entry.key.lot, 0), entry.key.step
            )
    ordered = [last_step_of_lot[lot] for lot in sorted(last_step_of_lot)]
    assert ordered == sorted(ordered)


# ===================================================================== log and cassette


def test_every_model_action_names_its_call_and_counts_its_attempts(night: Played) -> None:
    attempts = Counter(entry.key.call_id for entry in night.cassette.entries)
    model_actions = [e for e in night.of_type("action") if e.data.seat in MODEL_SEATS]
    assert model_actions
    for event in model_actions:
        assert event.data.call_id in attempts
        assert event.data.attempts == attempts[event.data.call_id]
    # bots call no model: their actions carry no call id
    assert all(e.data.call_id is None for e in night.of_type("action") if e.data.seat in BOT_SEATS)


def test_every_repair_belongs_to_a_recorded_call(night: Played) -> None:
    calls = {entry.key.call_id for entry in night.cassette.entries}
    repairs = night.of_type("parse_repair")
    assert repairs  # the random seat bids above its budget now and then
    assert all(
        e.data.call_id in calls and e.data.status in ("ok", "retry", "abstain") for e in repairs
    )
    legal = [e for e in repairs if e.data.codes == ["clamp"]]
    assert legal and all(e.data.status == "ok" for e in legal)


def test_a_clamped_bid_shows_in_the_action_and_in_the_repair(night: Played) -> None:
    clamped = [e for e in night.of_type("action") if e.data.status == "clamped"]
    assert clamped
    for event in clamped:
        assert event.data.raw_value > event.data.value
        repair = [r for r in night.repairs(event.data.seat) if r.data.call_id == event.data.call_id]
        assert repair and repair[0].data.codes == ["clamp"]


def test_the_cassette_keeps_a_unique_key_for_every_attempt(night: Played) -> None:
    keys = [entry.key.as_tuple() for entry in night.cassette.entries]
    assert len(keys) == len(set(keys))
    assert all(entry.outcome == "ok" for entry in night.cassette.entries)
    assert all(entry.model.startswith("mock-") for entry in night.cassette.entries)


# ============================================================================ the cache


@pytest.mark.parametrize("seat", MODEL_SEATS)
def test_a_seat_sends_the_same_cached_prefix_all_night(night: Played, seat: int) -> None:
    entries = night.entries(seat)
    first = entries[0].request
    for entry in entries:
        for field in ("model", "system", "tools", "tool_choice", "max_tokens", "output_config"):
            assert entry.request.get(field) == first.get(field), field
    reads = [entry.usage["cache_read_input_tokens"] for entry in entries]  # type: ignore[index]
    writes = [entry.usage["cache_write_input_tokens"] for entry in entries]  # type: ignore[index]
    assert reads[0] == 0 and writes[0] > 0
    assert all(read > 0 for read in reads[1:]) and not any(writes[1:])


def test_the_system_prompt_of_a_seat_holds_its_own_cards_only(night: Played) -> None:
    for seat in MODEL_SEATS:
        system = night.entries(seat)[0].request["system"][0]["text"]
        assert f"你的座位是 {seat} 号" in system
        for other in MODEL_SEATS:
            if other != seat:
                assert f"你的座位是 {other} 号" not in system


# ================================================================================ shapes


def test_the_mock_night_is_independent_of_the_reply_format(
    night: Played, config: NightConfig
) -> None:
    openai = play(config, kind="openai_compatible")
    assert openai.result.events_text() == night.result.events_text()
    assert {e.kind for e in openai.cassette.entries} == {"openai_compatible"}
    assert {e.kind for e in night.cassette.entries} == {"anthropic"}
    first = openai.entries(1)[0].request
    assert first["messages"][0]["role"] == "system" and "tools" in first


def test_two_runs_of_the_same_night_are_the_same_night(night: Played, config: NightConfig) -> None:
    again = play(config)
    assert again.result.events_text() == night.result.events_text()
    assert [e.request_hash for e in again.cassette.entries] == [
        e.request_hash for e in night.cassette.entries
    ]


def test_another_seed_is_another_night(night: Played) -> None:
    other = play(make_config(night={"seed": "another-seed-0002"}))
    assert other.result.events_text() != night.result.events_text()
    assert other.result.status == "completed"


def test_nights_run_in_parallel_on_one_runner_are_the_nights_they_would_be_alone() -> None:
    configs = [
        make_config(night={"id": f"mock-par-{k}", "seed": f"parallel-seed-{k}"}) for k in range(3)
    ]
    rigs = [build_rig(config, Cassette.record()) for config in configs]
    clients = {name: client for rig in rigs for name, client in rig.clients.items()}
    assert len(clients) == 18  # every seat of every night has an endpoint of its own

    async def together() -> list:
        runner = ModelRunner(clients)  # one runner for the clients of all three nights
        try:
            return await runner.run_nights([(r.driver, r.session) for r in rigs], max_parallel=2)
        finally:
            runner.close()

    runs = asyncio.run(together())
    results = [rig.driver.finish(run) for rig, run in zip(rigs, runs, strict=True)]
    for config, result in zip(configs, results, strict=True):
        assert result.status == "completed"
        assert result.events_text() == play(config).result.events_text()
    assert len({r.events_text() for r in results}) == 3
