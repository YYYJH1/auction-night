"""Laws of a lot that hold for every format, whatever the rule parameters.

``test_engine_properties`` checks each format against its own oracle. This module states what
the formats have in common and what the rules promise about missing answers and about the
button clock, with random reserve prices, ``theta``, numbers of levels, budgets and answers:

* **Any format.** At most one winner, nobody pays for an unsold lot, nobody pays more than the
  budget left, and only the winner pays unless the format is all-pay.
* **Abstention is absence.** A seat that gives no answer in any round changes nothing about the
  winner, the price or the payments, in any of the six formats: the lot plays out exactly as if
  the seat had not been at the table. No default price takes its place.
* **The button asks only the participants.** After an exit, the seats still in are asked if
  there are between two and ``theta`` of them. A seat whose exit price is below
  ``max(reserve, 1)`` is not counted and is not asked.
* **A revision never goes below the clock.** The exit price in force after a revision is the
  answer, clamped to the budget and raised to the price of the clock. A seat that gives no
  answer keeps the exit price it had.
* **The blind box** prices like the second-price auction.
"""

from collections.abc import Collection, Mapping
from typing import Any

from engine_support import active, clamp, edge_answers, tables
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from auction_night.contracts import FORMAT_KEYS, make_mechanism
from auction_night.contracts.mechanism import MechanismDef
from auction_night.engine.formats import (
    LotOutcome,
    LotStep,
    open_english,
    play_lot,
    settle_sealed,
    step_english,
)

PROFILE = settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.too_slow])
SHAPES = [(100, 10), (200, 10), (400, 10), (100, 5), (200, 4), (400, 20), (200, 2)]
HIDDEN = 120  # the true value of a blind box in these lots


@st.composite
def mechanisms(draw: st.DrawFn) -> tuple[MechanismDef, int, list[int]]:
    """A mechanism of any format with random parameters, the tier of the lot, and the numbers
    at which a rule of this lot changes (the reserve and the levels of the ladder)."""
    key = draw(st.sampled_from(FORMAT_KEYS))
    tier, levels = draw(st.sampled_from(SHAPES))
    prices = [(tier // levels) * k for k in range(1, levels + 1)]  # the levels of a Dutch ladder
    near_a_level = [max(0, price + shift) for price in prices for shift in (-1, 0, 1)]
    mech = make_mechanism(
        key,
        reserve=0
        if key == "all_pay"
        else draw(st.one_of(st.integers(0, tier), st.sampled_from(near_a_level))),
        theta=draw(st.integers(2, 8)) if key == "english" else None,
        levels=levels if key == "dutch" else None,
    )
    return mech, tier, [mech.reserve, *prices]


def scripted(rounds: list[Mapping[int, int | None]], silent: Collection[int] = ()):
    """An ``answer`` function for ``play_lot``: round ``k`` takes ``rounds[k]``; silent seats
    never answer."""
    counter = iter(range(10**6))

    def answer(step: LotStep) -> dict[int, int | None]:
        script = rounds[next(counter) % len(rounds)]
        return {seat: script.get(seat) for seat in step.ask if seat not in silent}

    return answer


def play(
    mech: MechanismDef,
    tier: int,
    budgets: Mapping[int, int],
    keys: Mapping[int, int],
    rounds: list[Mapping[int, int | None]],
    silent: Collection[int] = (),
) -> LotOutcome:
    return play_lot(
        mech,
        tier=tier,
        budgets=budgets,
        tie_key=keys,
        answer=scripted(rounds, silent),
        true_value=HIDDEN if mech.value_model == "common" else None,
    )


def check_laws(outcome: LotOutcome, mech: MechanismDef, budgets: Mapping[int, int]) -> None:
    hammer = outcome.hammer
    assert set(outcome.asked) <= set(active(budgets))
    assert all(0 <= paid <= budgets[seat] for seat, paid in hammer.payments.items())
    assert set(hammer.payments) <= set(outcome.asked)
    if hammer.unsold:
        assert (hammer.winner, hammer.price, hammer.payments) == (None, 0, {})
        return
    assert hammer.winner in outcome.asked
    assert hammer.payments[hammer.winner] == hammer.price >= mech.reserve
    if mech.pricing != "all_pay":
        assert set(hammer.payments) == {hammer.winner}, "only the winner pays"


# ------------------------------------------------------------------ abstention is absence


def the_sale(outcome: LotOutcome) -> dict[str, Any]:
    """What a lot decides, without the bookkeeping of who was asked or what was revealed."""
    hammer = outcome.hammer
    return {
        "winner": hammer.winner,
        "price": hammer.price,
        "payments": hammer.payments,
        "unsold": hammer.unsold,
        "tied": hammer.tied,
        "newly_out": hammer.newly_out,
        "winner_exit": outcome.winner_exit,
        "dutch": None if outcome.dutch is None else outcome.dutch.model_dump(),
    }


@PROFILE
@given(mechanisms(), st.data())
def test_a_lot_obeys_the_common_laws_and_a_silent_seat_is_an_absent_seat(
    drawn: tuple[MechanismDef, int, list[int]], data: st.DataObject
) -> None:
    mech, tier, anchors = drawn
    budgets, keys = data.draw(tables(min_seats=2, anchors=anchors))
    asked = active(budgets)
    rounds = [data.draw(edge_answers(asked, budgets, anchors)) for _ in range(8)]
    silent = set(data.draw(st.sets(st.sampled_from(sorted(budgets)), max_size=3)))

    everyone = play(mech, tier, budgets, keys, rounds)
    check_laws(everyone, mech, budgets)

    with_silent = play(mech, tier, budgets, keys, rounds, silent)
    check_laws(with_silent, mech, budgets)
    absent_budgets = {seat: left for seat, left in budgets.items() if seat not in silent}
    absent_keys = {seat: key for seat, key in keys.items() if seat not in silent}
    without = play(mech, tier, absent_budgets, absent_keys, rounds)
    assert the_sale(with_silent) == the_sale(without), "no answer is no participation, nothing else"
    assert silent.isdisjoint(with_silent.hammer.payments)
    assert with_silent.hammer.winner not in silent


@PROFILE
@given(st.data(), st.integers(0, 130))
def test_the_blind_box_prices_like_the_second_price_auction(
    data: st.DataObject, reserve: int
) -> None:
    budgets, keys = data.draw(tables(min_seats=2, anchors=[reserve]))
    raw = data.draw(edge_answers(active(budgets), budgets, [reserve]))
    blind = settle_sealed(
        make_mechanism("blind_box", reserve=reserve), raw, budgets, keys, true_value=HIDDEN
    )
    second = settle_sealed(make_mechanism("sealed_second", reserve=reserve), raw, budgets, keys)
    assert blind.hammer.model_copy(update={"format": "sealed_second", "true_value": None}) == (
        second.hammer
    )
    assert blind.hammer.true_value == HIDDEN
    assert blind.records == second.records


# ------------------------------------------------------------------------ the button clock


def question_schedule(exits: Mapping[int, int], theta: int) -> list[tuple[int, ...]]:
    """Whom the button asks if nobody ever revises, from the exit prices of the participants.

    The clock stops at each exit price in turn. After the seats at that price have left, the
    seats still in are asked when there are between two and ``theta`` of them. A lot is decided
    when one seat remains or when all remaining seats leave at once.
    """
    remaining = sorted(exits)
    rounds: list[tuple[int, ...]] = []
    for price in sorted(set(exits.values())):
        leaving = [seat for seat in remaining if exits[seat] == price]
        if len(leaving) == len(remaining):
            break
        remaining = [seat for seat in remaining if exits[seat] != price]
        if len(remaining) <= 1:
            break
        if len(remaining) <= theta:
            rounds.append(tuple(remaining))
    return rounds


@PROFILE
@given(st.data(), st.integers(0, 130), st.integers(2, 8))
def test_the_button_asks_the_participants_only_and_exactly_when_theta_allows(
    data: st.DataObject, reserve: int, theta: int
) -> None:
    budgets, keys = data.draw(tables(min_seats=2, anchors=[reserve]))
    init = data.draw(edge_answers(active(budgets), budgets, [reserve]))
    mech = make_mechanism("english", reserve=reserve, theta=theta)
    floor = max(reserve, 1)
    exits = {
        seat: clamp(init.get(seat), budgets[seat])
        for seat in active(budgets)
        if clamp(init.get(seat), budgets[seat]) >= floor
    }

    step = open_english(mech, budgets, keys)
    asked: list[tuple[int, ...]] = []
    if not step.done:
        step = step_english(step.state, init)
    while not step.done:
        asked.append(step.ask)
        step = step_english(step.state, {})  # nobody revises: every exit price stays
    assert asked == question_schedule(exits, theta)
    assert all(set(ask) <= set(exits) and 2 <= len(ask) <= theta for ask in asked)


@PROFILE
@given(st.data(), st.integers(0, 130), st.integers(2, 8))
def test_a_revision_never_leaves_an_exit_price_below_the_clock(
    data: st.DataObject, reserve: int, theta: int
) -> None:
    budgets, keys = data.draw(tables(min_seats=2, anchors=[reserve]))
    mech = make_mechanism("english", reserve=reserve, theta=theta)
    step = open_english(mech, budgets, keys)
    if step.done:
        return
    step = step_english(step.state, data.draw(edge_answers(step.ask, budgets, [reserve])))
    for _ in range(8):  # at most eight seats can leave, so eight rounds are enough
        if step.done:
            break
        before = step.state
        clock = before.price
        anchors = [clock, *before.exits.values()]
        answers = data.draw(edge_answers(step.ask, budgets, anchors))
        step = step_english(before, answers)
        for seat in before.exits:
            given = answers.get(seat)
            expected = (
                before.exits[seat]
                if given is None  # no answer: the exit price in force stays
                else max(clamp(given, budgets[seat]), clock)  # never below the clock
            )
            left = [d for d in step.dropouts if d.seat == seat]
            if seat in step.state.exits:
                assert step.state.exits[seat] == expected
            elif left:
                assert left[0].price == expected, "a seat leaves at the exit price it ended with"
            else:
                raise AssertionError(f"seat {seat} neither stays nor leaves")
            assert expected >= clock


@PROFILE
@given(st.data(), st.integers(0, 130), st.integers(2, 8))
def test_seats_that_leave_together_are_announced_worst_key_first(
    data: st.DataObject, reserve: int, theta: int
) -> None:
    """The announcements are part of the event file, so their order is fixed by the keys."""
    budgets, keys = data.draw(tables(min_seats=2, anchors=[reserve]))
    mech = make_mechanism("english", reserve=reserve, theta=theta)
    step = open_english(mech, budgets, keys)
    if step.done:
        return
    clocks = [reserve]
    for _ in range(8):
        answers = data.draw(edge_answers(step.ask, budgets, clocks))
        step = step_english(step.state, answers)
        leaving = [d for d in step.dropouts if d.participated]
        for price in {d.price for d in leaving}:  # one step: the seats that the clock took at once
            group = [(keys[d.seat], d.seat) for d in leaving if d.price == price]
            assert group == sorted(group, reverse=True), "worst key first, the better key stays"
        clocks = [reserve, *(d.price for d in leaving)]
        if step.done:
            break
