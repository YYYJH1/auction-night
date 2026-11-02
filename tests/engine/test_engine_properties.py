"""Property tests of the settlement: invariants that hold for every table and every answer."""

from collections.abc import Mapping
from typing import Any

import pytest
from engine_support import (
    SEATS,
    active,
    answers_for,
    clamp,
    mock_config,
    oracle_dutch,
    oracle_english,
    oracle_sealed,
    seeds,
    tables,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from auction_night.contracts import canonical_json, make_mechanism
from auction_night.engine.deal import deal
from auction_night.engine.formats import (
    LotOutcome,
    dutch_ladder,
    open_dutch,
    open_english,
    play_lot,
    settle_dutch,
    settle_sealed,
    step_english,
)

PROFILE = settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.too_slow])
SEALED = ["sealed_first", "sealed_second", "all_pay", "blind_box"]
reserves = st.one_of(st.just(0), st.integers(0, 130))


def check_records(
    outcome: LotOutcome, answers: Mapping[int, Any], budgets: Mapping[int, int], phase: str
) -> None:
    """One record per asked seat; clamping and abstention are recorded as such."""
    asked = active(budgets)
    assert [r.seat for r in outcome.records] == asked
    for record in outcome.records:
        raw = answers.get(record.seat)
        assert record.phase == phase
        if raw is None:
            assert (record.status, record.value, record.raw_value) == ("abstain", None, None)
            assert record.reason == "missing"
        elif raw > budgets[record.seat]:
            assert (record.status, record.value, record.raw_value) == (
                "clamped",
                budgets[record.seat],
                raw,
            )
        else:
            assert (record.status, record.value, record.raw_value) == ("ok", raw, None)
        record.to_action()  # always a valid contract record


def check_newly_out(budgets: Mapping[int, int], outcome: LotOutcome) -> None:
    paid = outcome.hammer.payments
    expected = sorted(s for s, p in paid.items() if budgets[s] >= 1 and budgets[s] - p == 0)
    assert outcome.hammer.newly_out == expected


# -------------------------------------------------------------------------- sealed


@PROFILE
@given(st.sampled_from(SEALED), tables(), st.data(), reserves)
def test_sealed_settlement(key: str, table: Any, data: st.DataObject, reserve: int) -> None:
    budgets, keys = table
    reserve = 0 if key == "all_pay" else reserve
    asked = active(budgets)
    answers = data.draw(answers_for(asked))
    hidden = data.draw(st.integers(0, 600)) if key == "blind_box" else None
    mech = make_mechanism(key, reserve=reserve)

    outcome = settle_sealed(mech, answers, budgets, keys, true_value=hidden)
    hammer = outcome.hammer
    effective = {s: clamp(answers.get(s), budgets[s]) for s in asked}

    # the settlement is what an independent reading says it is
    winner, payments = oracle_sealed(mech.pricing, effective, reserve, keys)
    assert (hammer.winner, hammer.payments) == (winner, payments)
    assert hammer.bids == effective
    assert hammer.true_value == hidden
    assert hammer.unsold == (winner is None)
    check_records(outcome, answers, budgets, "sealed")
    check_newly_out(budgets, outcome)

    # at most one winner, nobody pays more than the remaining budget
    assert hammer.winner is None or hammer.winner in asked
    assert all(0 <= paid <= budgets[seat] for seat, paid in hammer.payments.items())
    assert set(hammer.payments) <= set(asked)
    if hammer.unsold:
        assert (hammer.price, hammer.payments, hammer.tied, hammer.tie_broken) == (0, {}, [], False)
        return
    assert hammer.winner is not None
    assert hammer.payments[hammer.winner] == hammer.price
    top = effective[hammer.winner]
    assert top == max(effective.values())
    assert top >= max(reserve, 1)
    leaders = [s for s in asked if effective[s] == top]
    assert hammer.tie_broken == (len(leaders) > 1)
    assert hammer.tied == (leaders if len(leaders) > 1 else [])
    assert hammer.winner == min(leaders, key=lambda s: (keys[s], s))

    if mech.pricing == "first":
        assert hammer.price == top, "first price pays its own bid"
        assert set(hammer.payments) == {hammer.winner}
    elif mech.pricing == "second":
        rivals = [b for s, b in effective.items() if s != hammer.winner]
        assert hammer.price == max(max(rivals, default=0), reserve)
        assert hammer.price <= top, "second price never pays more than its own bid"
        assert set(hammer.payments) == {hammer.winner}
    else:
        assert hammer.payments == {s: b for s, b in effective.items() if b >= 1}
        assert hammer.price == top, "all-pay: the winner pays its bid like everyone else"


@PROFILE
@given(st.sampled_from(SEALED), tables(), st.data())
def test_a_missing_bid_is_exactly_a_bid_of_zero(key: str, table: Any, data: st.DataObject) -> None:
    budgets, keys = table
    asked = active(budgets)
    answers = data.draw(answers_for(asked))
    zeroed = {s: (0 if answers.get(s) is None else answers[s]) for s in asked}
    hidden = 50 if key == "blind_box" else None
    mech = make_mechanism(key)
    a = settle_sealed(mech, answers, budgets, keys, true_value=hidden)
    b = settle_sealed(mech, zeroed, budgets, keys, true_value=hidden)
    assert a.hammer == b.hammer


@PROFILE
@given(st.sampled_from(SEALED), tables(), st.data(), st.randoms(use_true_random=False))
def test_the_order_of_the_inputs_does_not_matter(
    key: str, table: Any, data: st.DataObject, rnd: Any
) -> None:
    budgets, keys = table
    asked = active(budgets)
    answers = data.draw(answers_for(asked))
    hidden = 70 if key == "blind_box" else None
    mech = make_mechanism(key)

    def shuffled(mapping: Mapping[int, Any]) -> dict[int, Any]:
        items = list(mapping.items())
        rnd.shuffle(items)
        return dict(items)

    a = settle_sealed(mech, answers, budgets, keys, true_value=hidden)
    b = settle_sealed(mech, shuffled(answers), shuffled(budgets), shuffled(keys), true_value=hidden)
    assert a == b


# --------------------------------------------------------------------------- English


def drive_english(
    table: Any, data: st.DataObject, reserve: int, theta: int
) -> tuple[LotOutcome, list[tuple[str | None, tuple[int, ...], int, dict[int, int]]], list[dict]]:
    budgets, keys = table
    mech = make_mechanism("english", reserve=reserve, theta=theta)
    step = open_english(mech, budgets, keys)
    log, given_answers = [], []
    while not step.done:
        log.append((step.phase, step.ask, step.state.price, dict(step.state.exits)))
        answers = data.draw(answers_for(step.ask))
        given_answers.append(answers)
        step = step_english(step.state, answers)
    assert step.outcome is not None
    return step.outcome, log, given_answers


@PROFILE
@given(tables(), st.data(), reserves, st.integers(2, 6))
def test_english_settlement(table: Any, data: st.DataObject, reserve: int, theta: int) -> None:
    budgets, keys = table
    outcome, log, answers = drive_english(table, data, reserve, theta)
    hammer = outcome.hammer
    asked = active(budgets)
    floor = max(reserve, 1)

    assert outcome.asked == tuple(asked)
    assert all(0 <= paid <= budgets[seat] for seat, paid in hammer.payments.items())
    check_newly_out(budgets, outcome)

    # who was asked, and when
    if asked:
        assert log[0][0] == "english_init"
    else:
        assert not log
    for phase, ask, price, exits in log[1:]:
        assert phase == "english_revise"
        assert 2 <= len(ask) <= theta, "questions only while 2 to theta seats remain"
        assert set(ask) == set(exits)
        assert all(exit_price >= price for exit_price in exits.values())

    # announcements: non-participants first at price 0, then exits on a rising clock
    dropped = list(outcome.dropouts)
    outsiders = [d for d in dropped if not d.participated]
    insiders = [d for d in dropped if d.participated]
    assert dropped == outsiders + insiders
    assert all(d.price == 0 for d in outsiders)
    assert all(d.price >= floor for d in insiders)
    prices = [d.price for d in insiders]
    assert prices == sorted(prices), "the clock never goes down"
    sizes = [len(d.remaining) for d in insiders]
    assert sizes == sorted(sizes, reverse=True)
    assert len({d.seat for d in dropped}) == len(dropped)
    final = [d.seat for d in dropped]
    if hammer.winner is not None:
        assert hammer.winner not in final
        assert sorted(final + [hammer.winner]) == asked
    else:
        assert sorted(final) == asked

    if hammer.unsold:
        assert (hammer.price, hammer.payments) == (0, {})
        assert outcome.winner_exit is None
        return
    winner, price = hammer.winner, hammer.price
    assert winner is not None and outcome.winner_exit is not None
    assert hammer.payments == {winner: price}
    assert price >= reserve
    assert price <= outcome.winner_exit <= budgets[winner], "never charges above the winner's exit"
    if insiders:
        assert price == max(insiders[-1].price, reserve)
    else:
        assert price == reserve, "a lone participant pays the reserve"

    # records are valid contract records and the revisions follow the questions
    for record in outcome.records:
        action = record.to_action()
        assert action.phase in ("english_init", "english_revise")
    revise_asks = [ask for phase, ask, _, _ in log if phase == "english_revise"]
    kept = [r for r in outcome.records if r.status == "kept_previous"]
    assert all(r.phase == "english_revise" for r in kept)
    assert len(outcome.records) == len(asked) + sum(len(ask) for ask in revise_asks)


@PROFILE
@given(tables(), st.data(), reserves, st.integers(2, 6))
def test_english_is_deterministic(
    table: Any, data: st.DataObject, reserve: int, theta: int
) -> None:
    budgets, keys = table
    first, _, scripted = drive_english(table, data, reserve, theta)
    mech = make_mechanism("english", reserve=reserve, theta=theta)
    step = open_english(mech, budgets, keys)
    for answers in scripted:
        step = step_english(step.state, answers)
    assert step.outcome == first


@PROFILE
@given(tables(), st.data(), reserves)
def test_english_without_revisions_is_a_second_price_auction(
    table: Any, data: st.DataObject, reserve: int
) -> None:
    budgets, keys = table
    asked = active(budgets)
    answers = data.draw(answers_for(asked))
    mech = make_mechanism("english", reserve=reserve)
    step = open_english(mech, budgets, keys)
    if not step.done:
        step = step_english(step.state, answers)
    while not step.done:
        step = step_english(step.state, {})  # nobody revises
    assert step.outcome is not None
    effective = {s: clamp(answers.get(s), budgets[s]) for s in asked}
    winner, payments = oracle_sealed("second", effective, reserve, keys)
    assert (step.outcome.hammer.winner, step.outcome.hammer.payments) == (winner, payments)


@PROFILE
@given(tables(), st.data(), reserves, st.integers(2, 6))
def test_english_matches_an_independent_tick_by_tick_simulation(
    table: Any, data: st.DataObject, reserve: int, theta: int
) -> None:
    budgets, keys = table
    asked = active(budgets)
    init = data.draw(answers_for(asked))
    scripts = [data.draw(answers_for(asked)) for _ in range(8)]
    winner, price, log = oracle_english(init, budgets, keys, reserve, theta, scripts)

    mech = make_mechanism("english", reserve=reserve, theta=theta)
    step = open_english(mech, budgets, keys)
    rounds = iter(scripts)
    if not step.done:
        step = step_english(step.state, init)
    while not step.done:
        answers = {s: v for s, v in next(rounds, {}).items() if s in step.ask}
        step = step_english(step.state, answers)
    assert step.outcome is not None
    hammer = step.outcome.hammer
    assert hammer.winner == winner
    assert hammer.price == price
    assert [(d.seat, d.price) for d in step.outcome.dropouts if d.participated] == log


# ----------------------------------------------------------------------------- Dutch


@PROFILE
@given(
    tables(),
    st.data(),
    st.sampled_from([(100, 10), (200, 10), (400, 10), (100, 5), (200, 4), (400, 20)]),
    st.integers(0, 130),
)
def test_dutch_settlement(
    table: Any, data: st.DataObject, shape: tuple[int, int], reserve: int
) -> None:
    budgets, keys = table
    tier, levels = shape
    mech = make_mechanism("dutch", reserve=reserve, levels=levels)
    ladder = dutch_ladder(tier, levels, reserve)
    step = open_dutch(mech, tier, budgets, keys)

    # seats below the lowest level are not asked at all
    lowest = ladder[-1] if ladder else None
    expected_asked = [s for s in active(budgets) if lowest is not None and budgets[s] >= lowest]
    assert list(step.ask) == expected_asked
    assert step.done == (not expected_asked)

    answers = data.draw(answers_for(expected_asked))
    outcome = settle_dutch(mech, tier, answers, budgets, keys)
    hammer = outcome.hammer
    assert outcome.asked == tuple(expected_asked)
    assert [r.seat for r in outcome.records] == expected_asked
    effective = {s: clamp(answers.get(s), budgets[s]) for s in expected_asked}
    buyer, price = oracle_dutch(tier, levels, reserve, effective, keys)
    assert (hammer.winner, hammer.price if buyer is not None else None) == (buyer, price)
    assert outcome.dutch is not None
    assert outcome.dutch.ladder == list(ladder)
    check_newly_out(budgets, outcome)
    assert hammer.bids is None
    if buyer is None:
        assert hammer.unsold and outcome.dutch.unsold
        return
    assert price in ladder and price >= reserve, "a level price at or above the reserve"
    assert hammer.payments == {buyer: price}
    assert price <= budgets[buyer], "never above the remaining budget"
    assert effective[buyer] >= price
    assert outcome.dutch.level == ladder.index(price) + 1
    assert outcome.dutch.buyer == buyer
    firing = [s for s in expected_asked if effective[s] >= price]
    assert hammer.tie_broken == (len(firing) > 1)
    higher = [level for level in ladder if level > price]
    if higher:  # nobody could have bought on an earlier level
        assert all(effective[s] < min(higher) for s in expected_asked)


@PROFILE
@given(tables(), st.data(), st.integers(0, 130))
def test_dutch_inputs_in_any_order(table: Any, data: st.DataObject, reserve: int) -> None:
    budgets, keys = table
    mech = make_mechanism("dutch", reserve=reserve)
    ladder = dutch_ladder(200, 10, reserve)
    asked = [s for s in active(budgets) if ladder and budgets[s] >= ladder[-1]]
    answers = data.draw(answers_for(asked))
    a = settle_dutch(mech, 200, answers, budgets, keys)
    reverse = {s: answers[s] for s in reversed(list(answers))}
    b = settle_dutch(
        mech,
        200,
        reverse,
        {s: budgets[s] for s in reversed(list(budgets))},
        {s: keys[s] for s in reversed(list(keys))},
    )
    assert a == b


# --------------------------------------------------------------------------- nights


def night_digest(seed: str) -> str:
    """Play the twelve dealt lots with value bidding and write down every hammer."""
    config = mock_config()
    night = config.night
    d = deal(config, seed=seed)
    left = dict(d.budgets)
    hammers = []
    for lot in d.lots:
        live = {s: b for s, b in left.items() if b >= 1}
        outcome = play_lot(
            night.mechanism_for(lot.format),
            tier=lot.tier,
            budgets=live,
            tie_key={s: lot.tie_key[s] for s in live},
            answer=lambda step, lot=lot: {
                s: (
                    None
                    if step.phase == "english_revise"
                    else (lot.signals.get(s) or lot.values[s])
                )
                for s in step.ask
            },
            true_value=lot.blind_value,
        )
        for seat, amount in outcome.hammer.payments.items():
            left[seat] -= amount
        hammers.append(outcome.hammer)
        hammers.extend(outcome.dropouts)
        if outcome.dutch is not None:
            hammers.append(outcome.dutch)
    return canonical_json(hammers)


@settings(max_examples=25, deadline=None)
@given(seeds)
def test_same_seed_and_same_actions_give_the_same_night(seed: str) -> None:
    assert night_digest(seed) == night_digest(seed)


def test_different_seeds_give_different_nights() -> None:
    assert night_digest("alpha") != night_digest("beta")


@pytest.mark.parametrize("seat", SEATS)
def test_every_seat_number_can_play(seat: int) -> None:
    mech = make_mechanism("sealed_first")
    outcome = settle_sealed(mech, {seat: 5}, {seat: 10}, {seat: 0})
    assert outcome.hammer.winner == seat
