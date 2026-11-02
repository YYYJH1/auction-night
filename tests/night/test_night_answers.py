"""What the engine does with answers: abstentions, clamping, counts, and what it refuses."""

import pytest
from night_support import make_config, play, players_for, replay

from auction_night.contracts.act import normalize_act, validate_act
from auction_night.contracts.events import CostEvent, CostEventData, Pledge, dumps_events
from auction_night.engine.deal import deal
from auction_night.engine.night import Answer, AnswerError, append_events, start, step

CONFIG = make_config()
BID_PHASES = ("english_init", "english_revise", "dutch", "sealed")


def run(seed: str, override=None):
    """Play the mock night with some seats replaced: ``override`` maps a seat to a player."""
    the_deal = deal(CONFIG, seed=seed)
    players = players_for(CONFIG) | (override or {})
    return play(CONFIG, the_deal, players)


def actions(transcript, seat: int, *phases: str):
    return [
        e
        for e in transcript.events
        if e.type == "action" and e.data.seat == seat and e.data.phase in phases
    ]


# ------------------------------------------------------------------------------ abstaining


def test_an_abstention_keeps_its_reason_and_is_never_turned_into_a_price() -> None:
    base = players_for(CONFIG)[3]

    def flaky(request):
        if request.phase in ("english_init", "dutch", "sealed"):
            return Answer.abstained(3, "timeout", thought="没来得及")
        return base(request)

    transcript = run("ans-abstain", {3: flaky})
    bids = actions(transcript, 3, "english_init", "dutch", "sealed")
    assert len(bids) >= 6
    for event in bids:
        data = event.data
        assert (data.status, data.reason, data.value, data.raw_value) == (
            "abstain",
            "timeout",
            None,
            None,
        )
        assert data.thought == "没来得及"
    for record in transcript.state.records:
        hammer = record.outcome.hammer
        if hammer.bids is not None:
            assert hammer.bids[3] == 0, "an abstention counts as a bid of 0, nothing else"
        assert 3 not in hammer.payments


def test_a_failed_english_revision_keeps_the_exit_price_in_force() -> None:
    base = players_for(CONFIG)

    def sleepy(seat: int):
        def player(request):
            if request.phase == "english_revise":
                return Answer.abstained(seat, "timeout")
            return base[seat](request)

        return player

    transcript = run("ans-english", {seat: sleepy(seat) for seat in range(1, 9)})
    seen = 0
    revisions = [r for r in transcript.requests if r.phase == "english_revise"]
    assert revisions
    for request in revisions:
        for facts in request.facts:
            mine = [
                e
                for e in actions(transcript, facts.seat, "english_revise")
                if e.lot == request.lot and e.data.value == facts.exit_price
            ]
            assert mine, "the exit price in force is what the record keeps"
            assert all(e.data.status == "kept_previous" and e.data.reason is None for e in mine)
            seen += 1
    assert seen


def test_a_seat_that_sends_nothing_counts_as_missing() -> None:
    def mute(request):
        return None

    transcript = run("ans-missing", {2: mute})
    assert transcript.state.done
    plan = actions(transcript, 2, "plan")
    talk = actions(transcript, 2, "talk")
    bids = actions(transcript, 2, "english_init", "dutch", "sealed")
    assert len(plan) == 1 and len(talk) == 12 and bids
    for event in plan + talk + bids:
        assert (event.data.status, event.data.reason) == ("abstain", "missing")
    assert not [e for e in transcript.events if e.type == "plan" and e.data.seat == 2]
    assert not [e for e in transcript.events if e.type == "guess" and e.data.seat == 2]
    silent = [e for e in transcript.events if e.type == "talk" and e.data.seat == 2]
    assert len(silent) == 12 and all(e.data.text == "" and e.data.pledge is None for e in silent)


def test_a_seat_that_abstains_in_talk_says_nothing_and_guesses_nothing() -> None:
    base = players_for(CONFIG)[4]

    def quiet(request):
        if request.phase == "talk":
            return Answer.abstained(4, "refusal")
        return base(request)

    transcript = run("ans-quiet", {4: quiet})
    talk = actions(transcript, 4, "talk")
    assert len(talk) == 12 and {e.data.reason for e in talk} == {"refusal"}
    assert all(
        e.data.text == "" for e in transcript.events if e.type == "talk" and e.data.seat == 4
    )
    assert not [e for e in transcript.events if e.type == "guess" and e.data.seat == 4]
    assert all(g.seat != 4 for g in transcript.state.guesses)


def test_an_abstained_plan_leaves_no_plan_event() -> None:
    base = players_for(CONFIG)[5]

    def no_plan(request):
        if request.phase == "plan":
            return Answer.abstained(5, "parse_failed", thought="写不出")
        return base(request)

    transcript = run("ans-plan", {5: no_plan})
    (action,) = actions(transcript, 5, "plan")
    assert (action.data.status, action.data.reason, action.data.thought) == (
        "abstain",
        "parse_failed",
        "写不出",
    )
    assert [e.data.seat for e in transcript.events if e.type == "plan"] == [1, 2, 3, 4, 6]


def test_a_night_with_budget_aborted_actions_ends_as_budget_aborted() -> None:
    base = players_for(CONFIG)

    def player(seat: int):
        def act(request):
            if request.lot is not None and request.lot > 6:
                return Answer.abstained(seat, "budget_aborted")
            return base[seat](request)

        return act

    transcript = run("ans-abort", {seat: player(seat) for seat in range(1, 7)})
    end = transcript.events[-1]
    assert end.type == "night_end" and end.data.status == "budget_aborted"
    assert run("ans-abort").events[-1].data.status == "completed"


# ------------------------------------------------------ the cost gate and the status of the night
#
# The model layer turns every action that its cost gate skips into an abstention with the reason
# ``budget_aborted``. The engine marks the night as aborted when it is handed one, whatever the
# log can show about it: an English revision that does not come is a ``kept_previous`` action,
# and only an abstention carries a reason.

MODEL_SEATS = range(1, 7)
GATE_SEED = "ans-last-english-043"  # lot 12 is English and the night ends on its revision round
GATE_PHASES = ("plan", "talk", "english_init", "english_revise", "dutch", "sealed")


def revisions_cut(reason: str | None) -> dict:
    """The model seats fail every English revision (with ``reason``, or with no answer at all
    for None) and play everything else as usual."""
    base = players_for(CONFIG)

    def player(seat: int):
        def act(request):
            if request.phase == "english_revise":
                return None if reason is None else Answer.abstained(seat, reason)
            return base[seat](request)

        return act

    return {seat: player(seat) for seat in MODEL_SEATS}


def model_revisions(transcript) -> list:
    return [
        e
        for e in transcript.events
        if e.type == "action" and e.data.phase == "english_revise" and e.data.seat in MODEL_SEATS
    ]


def test_a_night_whose_only_skipped_actions_are_english_revisions_is_budget_aborted() -> None:
    the_deal = deal(CONFIG, seed="ans-gate-revise")
    cut = play(CONFIG, the_deal, players_for(CONFIG) | revisions_cut("budget_aborted"))
    revisions = model_revisions(cut)
    assert revisions
    # the log cannot name the cause: a revision that does not come keeps the exit price in force
    assert {(e.data.status, e.data.reason) for e in revisions} == {("kept_previous", None)}
    assert not [e for e in cut.events if e.type == "action" and e.data.reason == "budget_aborted"]
    end = cut.events[-1]
    assert end.type == "night_end" and end.data.status == "budget_aborted"
    assert cut.state.budget_aborted

    # no event changes its shape: the log is the one of a night whose revisions timed out, apart
    # from the status in night_end
    late = play(CONFIG, the_deal, players_for(CONFIG) | revisions_cut("timeout"))
    assert late.events[-1].data.status == "completed" and not late.state.budget_aborted
    assert cut.events[:-1] == late.events[:-1]
    assert cut.events[-1].data.scores == late.events[-1].data.scores

    # the status is a function of the answers: handing the same ones back gives the same night
    again = replay(CONFIG, the_deal, cut)
    assert dumps_events(again.events) == dumps_events(cut.events)
    assert again.budget_aborted and again.events[-1].data.status == "budget_aborted"


@pytest.mark.parametrize(
    "reason", [None, "missing", "parse_failed", "timeout", "refusal", "not_asked"]
)
def test_a_revision_that_fails_for_another_reason_leaves_the_night_completed(reason) -> None:
    transcript = play(
        CONFIG, deal(CONFIG, seed="ans-gate-revise"), players_for(CONFIG) | revisions_cut(reason)
    )
    revisions = model_revisions(transcript)
    assert revisions and {e.data.status for e in revisions} == {"kept_previous"}
    end = transcript.events[-1]
    assert end.type == "night_end" and end.data.status == "completed"
    assert not transcript.state.budget_aborted


@pytest.mark.parametrize("phase", GATE_PHASES)
def test_one_budget_aborted_answer_in_any_phase_marks_the_night(phase: str) -> None:
    the_deal = deal(CONFIG, seed="ans-gate-phase")
    base = players_for(CONFIG)
    plain = play(CONFIG, the_deal, base)
    assert plain.events[-1].data.status == "completed"
    first = next(r for r in plain.requests if r.phase == phase and r.seats[0] in MODEL_SEATS)
    seat = first.seats[0]

    def lone(request):
        if request.index == first.index:
            return Answer.abstained(seat, "budget_aborted")
        return base[seat](request)

    transcript = play(CONFIG, the_deal, base | {seat: lone})
    assert (transcript.requests[first.index].phase, transcript.requests[first.index].seats) == (
        phase,
        first.seats,
    )
    (skipped, *_) = [e for e in actions(transcript, seat, phase) if e.lot == first.lot]
    # an English revision that does not come keeps the exit price and names no reason
    named = ("kept_previous", None) if phase == "english_revise" else ("abstain", "budget_aborted")
    assert (skipped.data.status, skipped.data.reason) == named
    assert transcript.events[-1].data.status == "budget_aborted"


def test_a_budget_aborted_answer_in_the_last_request_of_the_night_still_counts() -> None:
    """The mark is taken before the night is closed, so the answers of the request that ends
    the night are part of it."""
    the_deal = deal(CONFIG, seed=GATE_SEED)
    base = players_for(CONFIG)
    plain = play(CONFIG, the_deal, base)
    last = plain.requests[-1]
    model = [seat for seat in last.seats if seat in MODEL_SEATS]
    assert last.phase == "english_revise" and last.lot == CONFIG.night.n_lots and model
    assert plain.events[-1].data.status == "completed"

    def player(seat: int):
        def act(request):
            if request.index == last.index:
                return Answer.abstained(seat, "budget_aborted")
            return base[seat](request)

        return act

    transcript = play(CONFIG, the_deal, base | {seat: player(seat) for seat in model})
    assert transcript.requests[-1].index == last.index
    kept = actions(transcript, model[0], "english_revise")[-1]
    assert (kept.data.status, kept.data.reason) == ("kept_previous", None)
    end = transcript.events[-1]
    assert end.type == "night_end" and end.data.status == "budget_aborted"


def test_the_mark_belongs_to_the_state_and_outlasts_events_from_outside_and_later_answers() -> None:
    the_deal = deal(CONFIG, seed="ans-gate-mark")
    state, request = start(CONFIG, the_deal)
    assert state.budget_aborted is False
    skipped = {seat: Answer.abstained(seat, "budget_aborted") for seat in request.seats}
    after, _, request = step(state, skipped)
    assert after.budget_aborted is True
    assert state.budget_aborted is False  # the state that was handed in is left as it was

    cap = CostEvent(
        seq=1,
        lot=None,
        vis="system",
        data=CostEventData(kind="cap_reached", spent_microusd=5, cap_microusd=5, calls=1),
    )
    state, _ = append_events(after, [cap])
    assert state.budget_aborted is True

    players = players_for(CONFIG)
    while request is not None:  # every later answer is an ordinary one, none of them clears it
        replies = {seat: players[seat](request) for seat in request.seats}
        state, _, request = step(state, {seat: r for seat, r in replies.items() if r is not None})
    assert state.budget_aborted is True
    assert state.events[-1].type == "night_end" and state.events[-1].data.status == "budget_aborted"


# ----------------------------------------------------------------------------- clamping


def test_a_number_above_the_budget_is_clamped_and_the_raw_value_is_kept() -> None:
    base = players_for(CONFIG)[4]

    def greedy(request):
        reply = base(request)
        if request.phase in ("english_init", "dutch", "sealed"):
            return Answer(seat=4, value=10**6, thought="全都要")
        return reply

    transcript = run("ans-clamp", {4: greedy})
    bids = actions(transcript, 4, "english_init", "dutch", "sealed")
    assert bids
    for event in bids:
        left = transcript.state.records[event.lot - 1].budgets_before[4]
        data = event.data
        assert (data.status, data.value, data.raw_value) == ("clamped", left, 10**6)
    for record in transcript.state.records:
        if record.outcome.hammer.bids is not None and 4 in record.outcome.asked:
            assert record.outcome.hammer.bids[4] == record.budgets_before[4]


def test_counts_and_call_ids_are_recorded_on_the_events() -> None:
    base = players_for(CONFIG)[1]

    def counted(request):
        reply = base(request)
        if reply is None:
            return reply
        return Answer(
            seat=1,
            value=reply.value,
            say=reply.say,
            pledge=reply.pledge,
            guess=reply.guess,
            priorities=reply.priorities,
            budget_plan=reply.budget_plan,
            persona=reply.persona,
            thought=reply.thought,
            sanitized_chars=5,
            text_sanitized_chars=3,
            attempts=2,
            call_id=f"call-{request.index}",
        )

    transcript = run("ans-counts", {1: counted})
    mine = [e for e in transcript.events if e.type == "action" and e.data.seat == 1]
    assert mine and all(e.data.attempts == 2 and e.data.sanitized_chars == 5 for e in mine)
    assert all(e.data.call_id and e.data.call_id.startswith("call-") for e in mine)
    texts = [e for e in transcript.events if e.type in ("talk", "plan") and e.data.seat == 1]
    assert texts and all(e.data.sanitized_chars == 3 for e in texts)
    others = [e for e in transcript.events if e.type == "action" and e.data.seat == 2]
    assert all(e.data.attempts == 1 and e.data.sanitized_chars == 0 for e in others)


def test_pledges_are_stored_as_given_and_not_judged_here() -> None:
    pledges = {
        1: Pledge(kind="no_bid", lot=1),
        2: Pledge(kind="max_bid", lot=2, amount=1),
        3: Pledge(kind="let_win", lot=3, to=2),
    }
    base = players_for(CONFIG)[1]

    def oath_breaker(request):
        if request.phase == "talk":
            pledge = pledges.get(request.lot)
            return Answer(seat=1, say="我保证" if pledge else "继续", pledge=pledge)
        if request.phase in ("english_init", "dutch", "sealed"):
            return Answer(seat=1, value=5)  # more than the 1 coin it swore, and a bid at all
        return base(request)

    transcript = run("ans-oath", {1: oath_breaker})
    talks = {e.lot: e.data for e in transcript.events if e.type == "talk" and e.data.seat == 1}
    assert all(talks[lot].pledge == pledge for lot, pledge in pledges.items())
    assert all(talks[lot].pledge is None for lot in range(4, 13))
    # the engine added nothing about it: no verdict, no flag, no change to the bids
    assert {e.type for e in transcript.events} <= {
        "night_start",
        "deal_private",
        "plan",
        "lot_open",
        "talk",
        "guess",
        "action",
        "english_dropout",
        "dutch_result",
        "hammer",
        "mission_result",
        "night_end",
    }
    first = [e for e in actions(transcript, 1, *BID_PHASES) if e.lot in pledges]
    assert len(first) >= 3 and all(e.data.status in ("ok", "clamped") for e in first)
    assert all(e.data.value == 5 for e in first)


def test_texts_longer_than_the_channel_allows_are_cut_and_the_cut_is_counted() -> None:
    base = players_for(CONFIG)[1]
    night = CONFIG.night
    say_cap, persona_cap, thought_cap = (
        night.chat.max_chars,
        night.chat.persona_max_chars,
        night.thought.max_chars,
    )
    long_line, long_thought = "字" * 100, "思" * 150

    def chatty(request):
        reply = base(request)
        if request.phase == "plan":
            return Answer(
                seat=1,
                priorities=reply.priorities,
                budget_plan=reply.budget_plan,
                persona=long_line,
                thought=long_thought,
                sanitized_chars=1,
                text_sanitized_chars=1,
            )
        if request.phase == "talk":
            return Answer(seat=1, say=long_line + " ", thought=long_thought, guess=5)
        return Answer(seat=1, value=reply.value, thought=long_thought)

    transcript = run("ans-caps", {1: chatty})
    thought_cut = 150 - thought_cap

    plan = next(e for e in transcript.events if e.type == "plan" and e.data.seat == 1)
    assert plan.data.persona == "字" * persona_cap
    assert plan.data.sanitized_chars == 1 + 100 - persona_cap
    talks = [e for e in transcript.events if e.type == "talk" and e.data.seat == 1]
    assert all(e.data.text == "字" * say_cap for e in talks)
    assert all(e.data.sanitized_chars == 101 - say_cap for e in talks)

    mine = [e for e in transcript.events if e.type == "action" and e.data.seat == 1]
    assert all(e.data.thought == "思" * thought_cap for e in mine)
    by_phase = {e.data.phase: e.data.sanitized_chars for e in mine}
    assert by_phase["plan"] == 1 + (100 - persona_cap) + thought_cut
    assert by_phase["talk"] == (101 - say_cap) + thought_cut
    assert by_phase["sealed"] == thought_cut
    # a line that fits is left exactly as it is
    others = [e for e in transcript.events if e.type == "talk" and e.data.seat == 2]
    assert all(len(e.data.text) <= say_cap and e.data.sanitized_chars == 0 for e in others)


# ------------------------------------------------------------------------------- refusals


def plan_state():
    return start(CONFIG, deal(CONFIG, seed="ans-refuse"))


def test_answers_for_seats_that_were_not_asked_are_refused() -> None:
    state, request = plan_state()
    assert 7 not in request.seats
    with pytest.raises(AnswerError, match="was not asked"):
        step(state, {7: Answer(seat=7, say="我也说两句")})
    with pytest.raises(AnswerError, match="not that seat's answer"):
        step(state, {1: Answer.abstained(2)})
    with pytest.raises(AnswerError, match="not that seat's answer"):
        step(state, {1: "a bid"})  # type: ignore[dict-item]


def test_a_plan_must_cover_every_lot_with_valid_numbers() -> None:
    state, _ = plan_state()
    good = Answer(seat=1, priorities=(1,) * 12, budget_plan=(10,) * 12)
    step(state, {1: good})
    for bad in (
        Answer(seat=1, priorities=(1,) * 3, budget_plan=(10,) * 12),
        Answer(seat=1, priorities=(1,) * 12, budget_plan=(10,) * 11),
        Answer(seat=1, priorities=(4,) * 12, budget_plan=(10,) * 12),
        Answer(seat=1, priorities=(1,) * 12, budget_plan=(-1,) * 12),
        Answer(seat=1, priorities=None, budget_plan=(10,) * 12),
    ):
        with pytest.raises(AnswerError):
            step(state, {1: bad})


def to_first_talk(lot: int = 1):
    state, request = plan_state()
    players = players_for(CONFIG)
    while not (request.phase == "talk" and request.lot == lot):
        answers = {seat: players[seat](request) for seat in request.seats}
        state, _, request = step(state, answers)
    return state, request


def test_a_pledge_must_name_a_lot_still_to_come_and_a_seat_that_can_receive_it() -> None:
    state, request = to_first_talk(2)
    seat = request.seats[0]
    other = seat % 8 + 1
    step(state, {seat: Answer(seat=seat, say="好", pledge=Pledge(kind="no_bid", lot=2))})
    step(state, {seat: Answer(seat=seat, say="好", pledge=Pledge(kind="no_bid", lot=12))})
    step(state, {seat: Answer(seat=seat, say="好", pledge=Pledge(kind="let_win", lot=3, to=other))})
    refused = [
        Pledge(kind="no_bid", lot=1),  # the lot is over
        Pledge(kind="no_bid", lot=13),  # there is no such lot
        Pledge(kind="let_win", lot=3, to=seat),  # to oneself
    ]
    for pledge in refused:
        with pytest.raises(AnswerError):
            step(state, {seat: Answer(seat=seat, say="好", pledge=pledge)})


def test_a_pledge_cannot_name_a_seat_the_table_does_not_have() -> None:
    config = make_config(
        seats=[
            {"seat": 1, "codename": "甲", "kind": "mock", "mock_policy": "honest"},
            {"seat": 2, "codename": "乙", "kind": "mock", "mock_policy": "honest"},
            {"seat": 3, "codename": "丙", "kind": "bot", "policy": "truthful"},
        ],
        missions={"collectors": 1, "shills": 1},
    )
    state, request = start(config, deal(config, seed="ans-small"))
    state, _, request = step(state, {})  # nobody plans
    assert request.phase == "talk" and request.n_seats == 3
    seat = request.seats[0]
    step(state, {seat: Answer(seat=seat, pledge=Pledge(kind="let_win", lot=1, to=3))})
    with pytest.raises(AnswerError, match="cannot receive"):
        step(state, {seat: Answer(seat=seat, pledge=Pledge(kind="let_win", lot=1, to=5))})


def test_a_finished_night_takes_no_more_answers() -> None:
    transcript = run("ans-done")
    assert transcript.state.done and transcript.state.pending is None
    with pytest.raises(ValueError, match="over"):
        step(transcript.state, {})


# -------------------------------------------------------------------------- Answer itself


@pytest.mark.parametrize(
    "kwargs",
    [
        {"seat": 0},
        {"seat": 9},
        {"seat": True},
        {"seat": 1, "value": -1},
        {"seat": 1, "value": True},
        {"seat": 1, "value": 1.5},
        {"seat": 1, "value": 2**60},
        {"seat": 1, "guess": -3},
        {"seat": 1, "say": "两行\n文字"},
        {"seat": 1, "thought": "控制\x00字符"},
        {"seat": 1, "persona": 5},
        {"seat": 1, "attempts": 0},
        {"seat": 1, "attempts": 11},
        {"seat": 1, "sanitized_chars": -1},
        {"seat": 1, "call_id": ""},
        {"seat": 1, "priorities": [1, 2]},
        {"seat": 1, "budget_plan": (1, "2")},
        {"seat": 1, "pledge": {"kind": "no_bid", "lot": 1}},
        {"seat": 1, "abstain": "gave_up"},
        {"seat": 1, "abstain": "timeout", "value": 5},
        {"seat": 1, "abstain": "timeout", "say": "还是说一句"},
        {"seat": 1, "abstain": "timeout", "guess": 5},
    ],
)
def test_an_answer_that_could_not_be_an_event_is_refused_when_it_is_built(kwargs) -> None:
    with pytest.raises(AnswerError):
        Answer(**kwargs)


def test_an_abstention_may_carry_a_thought_and_counts() -> None:
    answer = Answer.abstained(3, "timeout", thought="想了很久", attempts=3, sanitized_chars=2)
    assert (answer.seat, answer.abstain, answer.thought, answer.attempts) == (
        3,
        "timeout",
        "想了很久",
        3,
    )
    assert Answer.abstained(3).abstain == "missing"


def act_args(phase: str, **fields):
    return normalize_act({"phase": phase, "thought": "想法", **fields}, phase)  # type: ignore[arg-type]


def test_an_answer_can_be_built_from_the_arguments_of_an_act_call() -> None:
    plan = act_args(
        "plan",
        plan_priorities=[0, 1, 2, 3] * 3,
        plan_budget=list(range(12)),
        plan_persona="装作不在乎",
    )
    assert validate_act(plan, "plan") == []
    answer = Answer.from_act(2, plan, sanitized_chars=4, text_sanitized_chars=1, call_id="c-1")
    assert answer.priorities == tuple([0, 1, 2, 3] * 3) and answer.budget_plan == tuple(range(12))
    assert (answer.persona, answer.thought, answer.sanitized_chars, answer.call_id) == (
        "装作不在乎",
        "想法",
        4,
        "c-1",
    )

    talk = act_args(
        "talk",
        say="大家好",
        guess_budget=180,
        pledge_kind="max_bid",
        pledge_lot=3,
        pledge_amount=90,
    )
    assert validate_act(talk, "talk") == []
    answer = Answer.from_act(2, talk)
    assert (answer.say, answer.guess, answer.value) == ("大家好", 180, None)
    assert answer.pledge == Pledge(kind="max_bid", lot=3, amount=90)
    assert Answer.from_act(2, act_args("talk", say="", guess_budget=0)).pledge is None

    for phase, name in (
        ("english_init", "exit_price"),
        ("english_revise", "exit_price"),
        ("dutch", "grab_price"),
        ("sealed", "bid"),
    ):
        args = act_args(phase, **{name: 77})
        assert validate_act(args, phase) == []
        assert Answer.from_act(2, args).value == 77
