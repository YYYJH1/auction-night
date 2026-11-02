"""Replies that go wrong: the parsing ladder, the retries and the abstentions, end to end.

One mock night is played with a plan of faults, each aimed at one seat, phase and lot. The
cases are read from that single night, so the whole file costs one night. Every fault goes through
the same path as a real reply: the request is built, the mock answers badly, the ladder reads the
reply, the runner retries or gives up, and the engine records an action. The bid faults are all
placed on lots after the first English lot, so the English lot plays as in a clean night.
"""

import pytest
from e2e_support import MODEL_SEATS, Played, lots_of, make_config, play

from auction_night.contracts import NightConfig, validate_log
from auction_night.engine.deal import deal
from auction_night.engine.scoring import conservation_gap
from auction_night.llm.mock import Fault
from auction_night.llm.nightrun import play_night

# ================================================================================ the plan


class Plan:
    """Where the faults of the night sit. Lot numbers follow the deal of the configuration."""

    def __init__(self, config: NightConfig) -> None:
        self.english = lots_of(config, "english")[0]
        after = self.english

        def later(format_key: str) -> int:
            return next(lot for lot in lots_of(config, format_key) if lot > after)

        self.sealed = later("sealed_first")
        self.blind = later("blind_box")
        self.dutch = later("dutch")

    def faults(self) -> tuple[Fault, ...]:
        e, s, b, d = self.english, self.sealed, self.blind, self.dutch
        return (
            Fault("prose", seat=1, phase="talk", lot=1),
            Fault("refusal_text", seat=1, phase="talk", lot=e),
            Fault("long_text", seat=5, phase="talk", lot=e),
            Fault("bad_value", seat=2, phase="talk", lot=b, times=2),
            Fault("wrong_phase", seat=3, phase="talk", lot=b),
            Fault("over_budget", seat=4, phase="sealed", lot=b),
            Fault("text_amount", seat=6, phase="sealed", lot=b),
            Fault("timeout", seat=2, phase="sealed", lot=s),
            Fault("refusal", seat=3, phase="sealed", lot=s),
            Fault("bad_value", seat=4, phase="sealed", lot=s),
            Fault("prose", seat=5, phase="sealed", lot=s, times=2),
            Fault("fenced", seat=1, phase="dutch", lot=d),
        )


@pytest.fixture(scope="module")
def plan(config: NightConfig) -> Plan:
    return Plan(config)


@pytest.fixture(scope="module")
def faulty(config: NightConfig, plan: Plan) -> Played:
    return play(config, faults=plan.faults())


def call_of(played: Played, seat: int, phase: str, lot: int) -> list:  # noqa: ANN401
    """The recorded attempts of one call, in order."""
    return played.entries(seat, phase, lot)


def action_of(played: Played, seat: int, phase: str, lot: int):  # noqa: ANN201
    (action,) = played.actions(seat, phase, lot)
    return action


def repairs_of(played: Played, seat: int, phase: str, lot: int) -> list:  # noqa: ANN401
    call_id = f"l{lot}.{phase}."
    return [
        r
        for r in played.repairs(seat, phase)
        if r.data.call_id.startswith(call_id) and r.lot == lot  # type: ignore[union-attr]
    ]


# ========================================================================== the night holds


def test_a_night_full_of_faults_still_plays_to_the_end(faulty: Played, config: NightConfig) -> None:
    assert faulty.result.status == "completed"
    assert validate_log(faulty.events) == []
    hammers = [e.data for e in faulty.of_type("hammer")]
    assert len(hammers) == 12
    assert conservation_gap(faulty.result.scores, deal(config).lots, hammers) == 0


def test_every_fault_found_its_call(faulty: Played, plan: Plan) -> None:
    for fault in plan.faults():
        assert fault.seat is not None and fault.phase is not None
        assert call_of(faulty, fault.seat, fault.phase, fault.lot), fault  # type: ignore[arg-type]
    spoiled = {
        (e.key.seat, e.key.phase, e.key.lot) for e in faulty.cassette.entries if e.outcome != "ok"
    }
    assert {(2, "sealed", plan.sealed), (3, "sealed", plan.sealed)} <= spoiled


# ========================================================================== retries then ok


def test_nothing_to_read_is_retried_once_and_the_retry_is_the_action(
    faulty: Played, plan: Plan
) -> None:
    entries = call_of(faulty, 1, "talk", 1)
    assert [e.outcome for e in entries] == ["parse_failed", "ok"]
    assert [e.key.attempt for e in entries] == [1, 2]
    action = action_of(faulty, 1, "talk", 1)
    assert (action.data.status, action.data.attempts) == ("ok", 2)
    (repair,) = repairs_of(faulty, 1, "talk", 1)
    assert (repair.data.codes, repair.data.status, repair.data.attempt) == (["retry"], "retry", 1)
    # the retry is the same request plus the model's own words and a message that names the fix
    first, second = (e.request["messages"] for e in entries)
    assert len(first) == 1 and len(second) == 3
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    assert "act" in second[2]["content"]


def test_a_value_that_is_not_a_number_is_retried_with_the_violation(
    faulty: Played, plan: Plan
) -> None:
    entries = call_of(faulty, 4, "sealed", plan.sealed)
    assert [e.outcome for e in entries] == ["parse_failed", "ok"]
    assert "bid" in entries[1].request["messages"][2]["content"]
    assert action_of(faulty, 4, "sealed", plan.sealed).data.attempts == 2
    (repair,) = repairs_of(faulty, 4, "sealed", plan.sealed)
    assert repair.data.codes == ["invalid_input", "retry"] and repair.data.status == "retry"
    assert repair.data.fields == ["bid"] and "invalid=bid:wrong_type" in repair.data.detail


def test_a_reply_for_another_phase_is_checked_against_the_real_one(
    faulty: Played, plan: Plan
) -> None:
    entries = call_of(faulty, 3, "talk", plan.blind)
    assert [e.outcome for e in entries] == ["parse_failed", "ok"]
    assert entries[0].parsed is not None
    assert [(e["field"], e["code"]) for e in entries[0].parsed["errors"]] == [
        ("phase", "phase_mismatch")
    ]
    action = action_of(faulty, 3, "talk", plan.blind)
    assert action.data.phase == "talk" and action.data.status == "ok"


# =========================================================================== repaired, not retried


def test_json_in_a_code_fence_is_repaired_and_counted(faulty: Played, plan: Plan) -> None:
    (entry,) = call_of(faulty, 1, "dutch", plan.dutch)
    assert entry.outcome == "ok" and entry.response is not None
    assert entry.response["content"][0]["type"] == "text"  # no tool call
    action = action_of(faulty, 1, "dutch", plan.dutch)
    assert action.data.status == "ok" and action.data.attempts == 1
    (repair,) = repairs_of(faulty, 1, "dutch", plan.dutch)
    assert repair.data.status == "ok" and "syntax" in repair.data.codes
    assert "syntax=not_a_tool_call,fenced" in repair.data.detail


def test_an_amount_with_a_unit_is_coerced(faulty: Played, plan: Plan) -> None:
    (entry,) = call_of(faulty, 6, "sealed", plan.blind)
    assert entry.outcome == "ok"
    action = action_of(faulty, 6, "sealed", plan.blind)
    assert action.data.status in ("ok", "clamped") and action.data.value is not None
    (repair,) = repairs_of(faulty, 6, "sealed", plan.blind)
    assert "coerce" in repair.data.codes and repair.data.status == "ok"
    assert "bid:text_number" in repair.data.detail


def test_a_bid_above_the_budget_is_a_legal_clamp_not_a_mistake(faulty: Played, plan: Plan) -> None:
    action = action_of(faulty, 4, "sealed", plan.blind)
    assert action.data.status == "clamped"
    assert action.data.raw_value is not None and action.data.value is not None
    assert action.data.raw_value - action.data.value == 777
    (repair,) = repairs_of(faulty, 4, "sealed", plan.blind)
    assert repair.data.codes == ["clamp"] and repair.data.status == "ok"
    assert action.data.attempts == 1
    counters = faulty.result.run.counters["seat:4"]
    assert counters["legal_clamps"]["budget_clamp"] >= 1
    # the clamp is no mistake: the only invalid input of the seat is the planted wrong type
    assert counters["invalid_by_code"] == {"wrong_type": 1}


def test_text_over_its_limit_is_cut_and_counted(faulty: Played, plan: Plan) -> None:
    (talk,) = [e for e in faulty.of_type("talk") if e.data.seat == 5 and e.lot == plan.english]
    assert 0 < len(talk.data.text) <= 60 and talk.data.sanitized_chars > 100
    action = action_of(faulty, 5, "talk", plan.english)
    assert len(action.data.thought) <= 100
    assert action.data.sanitized_chars > talk.data.sanitized_chars  # the thought was cut too
    (repair,) = repairs_of(faulty, 5, "talk", plan.english)
    assert repair.data.codes == ["sanitize"] and repair.data.sanitized_chars == (
        action.data.sanitized_chars
    )


# ==================================================================================== abstentions


def test_two_bad_replies_in_a_row_are_an_abstention(faulty: Played, plan: Plan) -> None:
    entries = call_of(faulty, 5, "sealed", plan.sealed)
    assert [e.outcome for e in entries] == ["parse_failed", "parse_failed"]
    action = action_of(faulty, 5, "sealed", plan.sealed)
    assert (action.data.status, action.data.reason, action.data.attempts) == (
        "abstain",
        "missing",
        2,
    )
    assert action.data.value is None
    repairs = repairs_of(faulty, 5, "sealed", plan.sealed)
    assert [(r.data.codes, r.data.status) for r in repairs] == [
        (["retry"], "retry"),
        (["fallback"], "abstain"),
    ]
    sealed = next(e for e in faulty.of_type("hammer") if e.lot == plan.sealed)
    assert sealed.data.bids is not None and sealed.data.bids[5] == 0  # never a default price


def test_two_unusable_values_end_as_parse_failed(faulty: Played, plan: Plan) -> None:
    action = action_of(faulty, 2, "talk", plan.blind)
    assert (action.data.status, action.data.reason, action.data.attempts) == (
        "abstain",
        "parse_failed",
        2,
    )
    repairs = repairs_of(faulty, 2, "talk", plan.blind)
    assert [r.data.status for r in repairs] == ["retry", "abstain"]
    assert repairs[1].data.codes == ["invalid_input", "fallback"]
    assert "reason=parse_failed" in repairs[1].data.detail
    (talk,) = [e for e in faulty.of_type("talk") if e.data.seat == 2 and e.lot == plan.blind]
    assert talk.data.text == "" and talk.data.pledge is None  # a seat that fails says nothing


def test_a_timeout_is_not_retried(faulty: Played, plan: Plan) -> None:
    (entry,) = call_of(faulty, 2, "sealed", plan.sealed)
    assert entry.outcome == "timeout" and entry.response is None and entry.error is not None
    assert entry.error["type"] == "TimeoutError"
    action = action_of(faulty, 2, "sealed", plan.sealed)
    assert (action.data.status, action.data.reason, action.data.attempts) == (
        "abstain",
        "timeout",
        1,
    )
    (repair,) = repairs_of(faulty, 2, "sealed", plan.sealed)
    assert (repair.data.codes, repair.data.status) == (["timeout", "fallback"], "abstain")
    assert "reason=timeout" in repair.data.detail
    assert dict(faulty.result.run.counters["seat:2"]["abstentions"])["timeout"] == 1


def test_a_refusal_of_the_api_is_not_retried(faulty: Played, plan: Plan) -> None:
    (entry,) = call_of(faulty, 3, "sealed", plan.sealed)
    assert entry.outcome == "refusal" and entry.refusal is True and entry.stop_reason == "refusal"
    action = action_of(faulty, 3, "sealed", plan.sealed)
    assert (action.data.status, action.data.reason) == ("abstain", "refusal")
    (repair,) = repairs_of(faulty, 3, "sealed", plan.sealed)
    assert (repair.data.codes, repair.data.status) == (["refusal"], "abstain")
    counters = faulty.result.run.counters["seat:3"]
    assert counters["abstentions"]["refusal"] == 1


def test_a_refusal_in_words_is_a_refusal_too(faulty: Played, plan: Plan) -> None:
    (entry,) = call_of(faulty, 1, "talk", plan.english)
    assert entry.outcome == "refusal" and entry.refusal is False  # the API did not say so
    action = action_of(faulty, 1, "talk", plan.english)
    assert (action.data.status, action.data.reason) == ("abstain", "refusal")
    (talk,) = [e for e in faulty.of_type("talk") if e.data.seat == 1 and e.lot == plan.english]
    assert talk.data.text == ""


def test_a_failed_english_revision_keeps_the_exit_price_in_force(
    config: NightConfig, night: Played, plan: Plan
) -> None:
    # find a model seat that is asked to revise in a clean night, then spoil exactly that call
    revisions = [a for a in night.actions(phase="english_revise", lot=plan.english)]
    seat = next(a.data.seat for a in revisions if a.data.seat in MODEL_SEATS)
    fault = Fault("prose", seat=seat, phase="english_revise", lot=plan.english, times=2)
    played = play(config, faults=(fault,))
    kept = [
        a
        for a in played.actions(seat, "english_revise", plan.english)
        if a.data.status == "kept_previous"
    ]
    assert len(kept) == 1
    assert kept[0].data.value is not None and kept[0].data.reason is None
    init = played.actions(seat, "english_init", plan.english)[0]
    assert kept[0].data.value == init.data.value  # the earlier exit price stays
    repairs = repairs_of(played, seat, "english_revise", plan.english)
    assert [r.data.status for r in repairs] == ["retry", "abstain"]
    assert played.result.run.counters[f"seat:{seat}"]["kept_previous"] == 1
    assert validate_log(played.events) == [] and played.result.status == "completed"


def test_the_counters_of_the_night_tell_the_same_story(faulty: Played) -> None:
    counters = faulty.result.run.counters
    assert counters["seat:5"]["abstentions"] == {"missing": 1}
    assert counters["seat:2"]["abstentions"] == {"parse_failed": 1, "timeout": 1}
    assert counters["seat:3"]["abstentions"] == {"refusal": 1}
    assert counters["seat:1"]["abstentions"] == {"refusal": 1}
    # a retry is an attempt, not an action
    seat4 = counters["seat:4"]
    assert seat4["attempts"] == seat4["actions"] + seat4["retries"]
    assert seat4["retries"] == 1 and seat4["invalid_inputs"] == 1


def test_the_faulty_night_replays_byte_for_byte(faulty: Played, config: NightConfig) -> None:
    from auction_night.llm.cassette import Cassette

    cassette = Cassette.from_text(faulty.cassette.text())
    replayed = play_night(config, cassette)
    assert replayed.events_text() == faulty.result.events_text()
    assert cassette.unused() == []
    outcomes = {e.outcome for e in faulty.cassette.entries}
    assert outcomes == {"ok", "parse_failed", "timeout", "refusal"}


# ====================================================================== a seat that never answers


def test_a_seat_that_never_answers_is_out_of_the_game_but_not_the_night() -> None:
    config = make_config(policies={3: "abstain"})
    played = play(config)
    assert played.result.status == "completed"
    assert [e for e in played.of_type("plan") if e.data.seat == 3] == []
    actions = played.actions(3)
    assert actions and {a.data.status for a in actions} <= {"abstain", "kept_previous"}
    assert {a.data.reason for a in actions if a.data.status == "abstain"} == {"missing"}
    assert all(e.outcome == "parse_failed" for e in played.entries(3))
    assert all(e.data.text == "" for e in played.of_type("talk") if e.data.seat == 3)
    assert [h for h in played.of_type("hammer") if h.data.winner == 3] == []
    score = next(s for s in played.result.scores if s.seat == 3)
    assert (score.surplus, score.mission_bonus, score.spent) == (0, 0, 0)
    assert played.result.run.counters["seat:3"]["ok"] == 0
