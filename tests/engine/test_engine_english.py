"""The English button auction: exits, revisions, non-participation, ties, purity."""

import copy
from typing import Any

import pytest

from auction_night.contracts import make_mechanism
from auction_night.contracts.events import EnglishDropoutData
from auction_night.engine.formats import LotOutcome, LotStep, open_english, step_english

KEYS = {s: s - 1 for s in range(1, 9)}
RICH = dict.fromkeys(range(1, 9), 1000)


class Run:
    """A lot driven with scripted answers. ``trace`` lists (phase, asked seats) per question."""

    def __init__(self, outcome: LotOutcome, trace: list[tuple[str | None, tuple[int, ...]]]):
        self.outcome = outcome
        self.trace = trace
        self.hammer = outcome.hammer
        self.records = outcome.records

    def dropouts(self) -> list[tuple[int, int, list[int], bool]]:
        return [(d.seat, d.price, d.remaining, d.participated) for d in self.outcome.dropouts]


def run(
    init: dict[int, int | None],
    revisions: list[dict[int, int | None]] | None = None,
    *,
    budgets: dict[int, int] | None = None,
    keys: dict[int, int] | None = None,
    reserve: int = 0,
    theta: int = 4,
) -> Run:
    mech = make_mechanism("english", reserve=reserve, theta=theta)
    table = budgets if budgets is not None else {seat: 1000 for seat in init}
    step = open_english(mech, table, keys or KEYS)
    scripts = [init, *(revisions or [])]
    trace: list[tuple[str | None, tuple[int, ...]]] = []
    asked = 0
    while not step.done:
        trace.append((step.phase, step.ask))
        answers = scripts[asked] if asked < len(scripts) else {}
        asked += 1
        step = step_english(step.state, answers)
    assert step.outcome is not None
    return Run(step.outcome, trace)


# ----------------------------------------------------------------------- the example


def test_the_worked_example() -> None:
    r = run({1: 120, 2: 90, 3: 40})
    assert r.trace == [("english_init", (1, 2, 3)), ("english_revise", (1, 2))]
    assert (r.hammer.winner, r.hammer.price, r.hammer.payments) == (1, 90, {1: 90})
    assert r.outcome.winner_exit == 120
    assert r.dropouts() == [(3, 40, [1, 2], True), (2, 90, [1], True)]
    assert r.hammer.bids is None
    assert not r.hammer.unsold and not r.hammer.tie_broken
    assert r.outcome.asked == (1, 2, 3)


def test_records_follow_the_questions() -> None:
    r = run({1: 120, 2: 90, 3: 40})
    got = [(x.seat, x.phase, x.status, x.value) for x in r.records]
    assert got == [
        (1, "english_init", "ok", 120),
        (2, "english_init", "ok", 90),
        (3, "english_init", "ok", 40),
        (1, "english_revise", "kept_previous", 120),
        (2, "english_revise", "kept_previous", 90),
    ]
    action = r.records[-1].to_action("timed out")
    assert (action.status, action.phase, action.value) == ("kept_previous", "english_revise", 90)


def test_each_step_reports_only_what_is_new() -> None:
    mech = make_mechanism("english")
    first = open_english(mech, RICH | {4: 1000}, KEYS)
    assert first.records == () and first.dropouts == ()
    second = step_english(first.state, {1: 120, 2: 90, 3: 40, 4: 0, 5: 0, 6: 0, 7: 0, 8: 0})
    assert [r.seat for r in second.records] == list(range(1, 9))
    assert [d.seat for d in second.dropouts] == [4, 5, 6, 7, 8] + [3]
    third = step_english(second.state, {})
    assert [r.phase for r in third.records] == ["english_revise"] * 2
    assert [d.seat for d in third.dropouts] == [2]
    assert third.outcome is not None
    assert len(third.outcome.records) == 8 + 2
    assert len(third.outcome.dropouts) == 5 + 1 + 1


# ------------------------------------------------------------------------- revisions


def test_raising_the_exit_price_in_a_revision() -> None:
    r = run({1: 120, 2: 90, 3: 40}, [{1: None, 2: 150}])
    assert (r.hammer.winner, r.hammer.price) == (2, 120)
    assert r.outcome.winner_exit == 150
    assert r.dropouts()[-1] == (1, 120, [2], True)
    assert [(x.seat, x.status, x.value) for x in r.records[-2:]] == [
        (1, "kept_previous", 120),
        (2, "ok", 150),
    ]


def test_lowering_the_exit_price_but_not_below_the_current_price() -> None:
    r = run({1: 120, 2: 90, 3: 40}, [{1: 60, 2: None}])
    assert (r.hammer.winner, r.hammer.price) == (2, 60)
    assert r.dropouts()[-1] == (1, 60, [2], True)


def test_a_revision_below_the_current_price_means_leaving_at_once() -> None:
    r = run({1: 120, 2: 90, 3: 40}, [{1: None, 2: 30}])
    assert r.dropouts() == [(3, 40, [1, 2], True), (2, 40, [1], True)]
    assert (r.hammer.winner, r.hammer.price) == (1, 40)
    assert r.outcome.winner_exit == 120
    assert r.records[-1].status == "ok"
    assert r.records[-1].value == 30, "the record keeps what the seat answered"


def test_a_revision_equal_to_the_current_price_leaves_at_the_next_step() -> None:
    r = run({1: 120, 2: 90, 3: 40}, [{1: None, 2: 40}])
    assert r.dropouts()[-1] == (2, 40, [1], True)
    assert (r.hammer.winner, r.hammer.price) == (1, 40)


def test_a_revision_above_the_budget_is_clamped() -> None:
    budgets = {1: 130, 2: 1000, 3: 1000}
    r = run({1: 120, 2: 90, 3: 40}, [{1: 900, 2: None}], budgets=budgets)
    clamped = [x for x in r.records if x.status == "clamped"]
    assert [(x.seat, x.value, x.raw_value) for x in clamped] == [(1, 130, 900)]
    assert r.outcome.winner_exit == 130
    assert (r.hammer.winner, r.hammer.price) == (1, 90)


def test_missing_and_none_revisions_keep_the_previous_exit_price() -> None:
    for answers in ({}, {1: None, 2: None}, {1: None}):
        r = run({1: 120, 2: 90, 3: 40}, [answers])
        assert (r.hammer.winner, r.hammer.price, r.outcome.winner_exit) == (1, 90, 120)
        kept = [x for x in r.records if x.status == "kept_previous"]
        assert [(x.seat, x.value) for x in kept] == [(1, 120), (2, 90)]


def test_leaving_at_once_can_open_another_round() -> None:
    r = run(
        {1: 100, 2: 90, 3: 80, 4: 20},
        [{1: None, 2: 10, 3: None}, {1: None, 3: None}],
    )
    assert r.trace == [
        ("english_init", (1, 2, 3, 4)),
        ("english_revise", (1, 2, 3)),
        ("english_revise", (1, 3)),
    ]
    assert r.dropouts() == [(4, 20, [1, 2, 3], True), (2, 20, [1, 3], True), (3, 80, [1], True)]
    assert (r.hammer.winner, r.hammer.price) == (1, 80)


def test_everyone_leaving_at_once_ties_at_the_current_price() -> None:
    # Both remaining seats answer below the current price 20: they share the exit price 20,
    # so the tie key picks the buyer, who pays 20.
    r = run({1: 100, 2: 90, 3: 20}, [{1: 5, 2: 5}], keys={1: 1, 2: 0, 3: 2})
    assert r.trace == [("english_init", (1, 2, 3)), ("english_revise", (1, 2))]
    assert r.dropouts() == [(3, 20, [1, 2], True), (1, 20, [2], True)]
    assert (r.hammer.winner, r.hammer.price) == (2, 20)
    assert r.hammer.tie_broken and r.hammer.tied == [1, 2]
    assert r.outcome.winner_exit == 20


# ----------------------------------------------------------------------- the theta rule


def test_questions_start_only_when_theta_seats_remain() -> None:
    init = {s: 10 * s for s in range(1, 7)}
    r = run(init)
    assert r.trace == [
        ("english_init", (1, 2, 3, 4, 5, 6)),
        ("english_revise", (3, 4, 5, 6)),
        ("english_revise", (4, 5, 6)),
        ("english_revise", (5, 6)),
    ]
    assert (r.hammer.winner, r.hammer.price) == (6, 50)
    assert [d[:2] for d in r.dropouts()] == [(1, 10), (2, 20), (3, 30), (4, 40), (5, 50)]


@pytest.mark.parametrize(
    ("theta", "first_round"), [(2, (5, 6)), (3, (4, 5, 6)), (5, (2, 3, 4, 5, 6))]
)
def test_theta_comes_from_the_mechanism(theta: int, first_round: tuple[int, ...]) -> None:
    r = run({s: 10 * s for s in range(1, 7)}, theta=theta)
    revise = [ask for phase, ask in r.trace if phase == "english_revise"]
    assert revise[0] == first_round
    assert all(2 <= len(ask) <= theta for ask in revise)


def test_no_question_before_the_first_exit() -> None:
    # three seats with theta 4: still nobody is asked before somebody has left
    mech = make_mechanism("english")
    first = open_english(mech, {1: 500, 2: 500, 3: 500}, KEYS)
    assert first.phase == "english_init"
    after = step_english(first.state, {1: 100, 2: 90, 3: 80})
    assert after.phase == "english_revise"
    assert after.state.price == 80
    assert after.dropouts == (EnglishDropoutData(seat=3, price=80, remaining=[1, 2]),)


def test_the_clock_state_during_a_revision_round() -> None:
    mech = make_mechanism("english", reserve=10)
    first = open_english(mech, dict.fromkeys(range(1, 5), 500), KEYS)
    assert first.state.price == 10, "the clock starts at the reserve"
    after = step_english(first.state, {1: 100, 2: 90, 3: 80, 4: 20})
    state = after.state
    assert (after.ask, after.phase) == ((1, 2, 3), "english_revise")
    assert state.price == 20
    assert state.exits == {1: 100, 2: 90, 3: 80}
    assert state.remaining == (1, 2, 3)
    assert state.phase == "english_revise"
    assert state.asked == (1, 2, 3)


# -------------------------------------------------------------- non-participation, reserve


def test_prices_below_the_floor_are_not_taking_part() -> None:
    r = run({1: 120, 2: 49, 3: 0, 4: None}, reserve=50, budgets=dict.fromkeys(range(1, 5), 1000))
    assert r.dropouts() == [(2, 0, [1], False), (3, 0, [1], False), (4, 0, [1], False)]
    assert r.trace == [("english_init", (1, 2, 3, 4))]
    assert (r.hammer.winner, r.hammer.price) == (1, 50), "a lone participant pays the reserve"
    got = {(x.seat, x.status, x.value, x.reason) for x in r.records}
    assert (2, "ok", 49, None) in got
    assert (3, "ok", 0, None) in got
    assert (4, "abstain", None, "missing") in got


def test_an_exit_price_of_one_takes_part_without_a_reserve() -> None:
    r = run({1: 1, 2: 0})
    assert r.dropouts() == [(2, 0, [1], False)]
    assert (r.hammer.winner, r.hammer.price) == (1, 0)


def test_a_lone_participant_pays_nothing_without_a_reserve() -> None:
    r = run({1: 77})
    assert (r.hammer.winner, r.hammer.price, r.hammer.payments) == (1, 0, {1: 0})
    assert r.outcome.winner_exit == 77


def test_the_floor_applies_after_clamping() -> None:
    budgets = {1: 1000, 2: 30}
    r = run({1: 80, 2: 500}, reserve=50, budgets=budgets)
    assert r.dropouts() == [(2, 0, [1], False)]
    assert [(x.seat, x.status, x.value, x.raw_value) for x in r.records] == [
        (1, "ok", 80, None),
        (2, "clamped", 30, 500),
    ]


def test_the_price_is_never_below_the_reserve() -> None:
    r = run({1: 100, 2: 70, 3: 20}, reserve=60)
    assert r.dropouts() == [(3, 0, [1, 2], False), (2, 70, [1], True)]
    assert (r.hammer.winner, r.hammer.price) == (1, 70)
    assert run({1: 100, 2: 60}, reserve=60).hammer.price == 60
    assert run({1: 100, 2: 59}, reserve=60).hammer.price == 60


@pytest.mark.parametrize(
    ("init", "reserve"),
    [({1: 0, 2: None}, 0), ({}, 0), ({1: 10, 2: 10}, 50), ({1: 49, 2: 3}, 50)],
)
def test_nobody_taking_part_leaves_the_lot_unsold(init: dict, reserve: int) -> None:
    r = run(init, reserve=reserve, budgets={1: 100, 2: 100})
    assert r.hammer.unsold
    assert (r.hammer.winner, r.hammer.price, r.hammer.payments) == (None, 0, {})
    assert r.outcome.winner_exit is None
    assert r.dropouts() == [(1, 0, [], False), (2, 0, [], False)]
    assert r.trace == [("english_init", (1, 2))]


def test_a_table_where_nobody_has_budget_is_decided_at_once() -> None:
    step = open_english(make_mechanism("english"), {1: 0, 2: 0}, KEYS)
    assert step.done and step.ask == ()
    assert step.outcome is not None and step.outcome.hammer.unsold


# ---------------------------------------------------------------------------- ties


def test_all_remaining_seats_with_the_same_exit_price_go_to_the_tie_key() -> None:
    keys = {1: 2, 2: 0, 3: 1}
    r = run({1: 80, 2: 80, 3: 80}, keys=keys)
    assert r.trace == [("english_init", (1, 2, 3))], "no exit happened, so there is no question"
    assert (r.hammer.winner, r.hammer.price) == (2, 80)
    assert r.hammer.tie_broken and r.hammer.tied == [1, 2, 3]
    # the worst tie rank is announced first
    assert r.dropouts() == [(1, 80, [2, 3], True), (3, 80, [2], True)]
    assert r.outcome.winner_exit == 80


def test_a_tie_for_the_win_after_a_revision_round() -> None:
    r = run({1: 100, 2: 100, 3: 30}, keys={1: 0, 2: 1, 3: 2})
    assert r.trace == [("english_init", (1, 2, 3)), ("english_revise", (1, 2))]
    assert (r.hammer.winner, r.hammer.price, r.hammer.tied) == (1, 100, [1, 2])
    assert r.dropouts() == [(3, 30, [1, 2], True), (2, 100, [1], True)]


def test_a_revision_can_break_a_tie() -> None:
    r = run({1: 100, 2: 100, 3: 30}, [{1: None, 2: 130}])
    assert (r.hammer.winner, r.hammer.price, r.hammer.tie_broken) == (2, 100, False)


def test_a_tie_below_the_top_does_not_use_the_tie_key_for_the_win() -> None:
    keys = {1: 3, 2: 0, 3: 1, 4: 2}
    r = run({1: 100, 2: 50, 3: 50, 4: 10}, keys=keys)
    assert r.dropouts() == [(4, 10, [1, 2, 3], True), (3, 50, [1, 2], True), (2, 50, [1], True)]
    assert (r.hammer.winner, r.hammer.price, r.hammer.tie_broken) == (1, 50, False)


def test_equal_tie_keys_fall_back_to_the_lower_seat() -> None:
    r = run({2: 80, 3: 80}, keys={2: 4, 3: 4})
    assert r.hammer.winner == 2


# ---------------------------------------------------------------------------- budgets


def test_exit_prices_are_clamped_to_the_budget() -> None:
    r = run({1: 900, 2: 90}, budgets={1: 300, 2: 500})
    assert r.records[0].status == "clamped"
    assert (r.records[0].value, r.records[0].raw_value) == (300, 900)
    assert r.outcome.winner_exit == 300
    assert (r.hammer.winner, r.hammer.price) == (1, 90)


def test_the_winner_going_out_of_budget_is_announced() -> None:
    # Seat 1 can only reach 90, ties with seat 2 at 90, wins on the tie key and is spent.
    r = run({1: 900, 2: 90}, budgets={1: 90, 2: 500})
    assert (r.hammer.winner, r.hammer.price, r.hammer.newly_out) == (1, 90, [1])
    r = run({1: 900, 2: 80}, budgets={1: 90, 2: 500})
    assert (r.hammer.winner, r.hammer.price, r.hammer.newly_out) == (1, 80, [])


def test_floats_and_fractions_are_floored() -> None:
    r = run({1: 120.9, 2: 90.1})
    assert (r.hammer.winner, r.hammer.price, r.outcome.winner_exit) == (1, 90, 120)


# ----------------------------------------------------------------------- misuse, purity


def test_answers_from_seats_that_were_not_asked_are_refused() -> None:
    mech = make_mechanism("english")
    first = open_english(mech, {1: 500, 2: 500, 3: 500}, KEYS)
    with pytest.raises(ValueError, match="not asked"):
        step_english(first.state, {1: 10, 2: 10, 3: 10, 4: 10})
    revise = step_english(first.state, {1: 100, 2: 90, 3: 80})
    assert revise.ask == (1, 2)
    with pytest.raises(ValueError, match="not asked"):
        step_english(revise.state, {3: 100})


def test_a_decided_lot_cannot_be_stepped() -> None:
    mech = make_mechanism("english")
    done = step_english(open_english(mech, {1: 500}, KEYS).state, {1: 100})
    assert done.done
    with pytest.raises(ValueError, match="decided"):
        step_english(done.state, {})


@pytest.mark.parametrize("bad", [-1, True, "120", float("nan")])
def test_bad_answers_are_refused(bad: Any) -> None:
    mech = make_mechanism("english")
    first = open_english(mech, {1: 500, 2: 500}, KEYS)
    with pytest.raises((ValueError, TypeError)):
        step_english(first.state, {1: bad, 2: 10})


@pytest.mark.parametrize("key", ["sealed_first", "dutch", "blind_box"])
def test_only_the_english_format_opens_here(key: str) -> None:
    with pytest.raises(ValueError, match="not an English"):
        open_english(make_mechanism(key), {1: 500}, KEYS)  # type: ignore[arg-type]


def test_stepping_is_pure() -> None:
    mech = make_mechanism("english")
    budgets = {1: 500, 2: 500, 3: 500}
    first = open_english(mech, budgets, KEYS)
    state_before = copy.deepcopy(first.state)
    answers = {1: 120, 2: 90, 3: 40}
    answers_before = dict(answers)
    one = step_english(first.state, answers)
    two = step_english(first.state, answers)
    assert one == two
    assert first.state == state_before
    assert answers == answers_before
    assert budgets == {1: 500, 2: 500, 3: 500}


def test_the_state_does_not_alias_the_callers_tables() -> None:
    budgets = {1: 500, 2: 500}
    keys = {1: 0, 2: 1}
    first = open_english(make_mechanism("english"), budgets, keys)
    budgets[1] = 0
    keys[1] = 9
    done = step_english(first.state, {1: 400, 2: 300})
    assert done.outcome is not None
    assert done.outcome.hammer.winner == 1
    assert done.outcome.records[0].value == 400


def test_open_returns_a_question_for_the_initial_round() -> None:
    step: LotStep = open_english(make_mechanism("english"), {1: 10, 3: 0, 4: 5}, KEYS)
    assert (step.ask, step.phase, step.done) == ((1, 4), "english_init", False)
    assert step.state.price == 0
