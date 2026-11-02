"""The format registry and the engine pieces working together on dealt nights."""

import pytest
from engine_support import mock_config

from auction_night.contracts import FORMAT_KEYS, MECHANISMS, make_mechanism
from auction_night.contracts.mechanism import FormatKey
from auction_night.engine.deal import deal
from auction_night.engine.formats import (
    REGISTRY,
    LotStep,
    open_dutch,
    open_english,
    open_lot,
    open_sealed,
    play_lot,
    settle_dutch,
    settle_sealed,
    step_lot,
)

KEYS = {s: s - 1 for s in range(1, 9)}
BUDGETS = {1: 400, 2: 300, 3: 200, 4: 100}
BIDS = {1: 130, 2: 90, 3: 150, 4: 20}


def test_registry_covers_every_format_by_clock() -> None:
    assert set(REGISTRY) == set(FORMAT_KEYS)
    kinds = {key: REGISTRY[key].kind for key in FORMAT_KEYS}
    assert kinds == {
        "english": "english",
        "dutch": "dutch",
        "sealed_first": "sealed",
        "sealed_second": "sealed",
        "all_pay": "sealed",
        "blind_box": "sealed",
    }
    for key, impl in REGISTRY.items():
        assert impl.key == key
        assert MECHANISMS[key].bid_phases  # every format has bid phases to ask in


def answer(step: LotStep) -> dict[int, int | None]:
    return {seat: BIDS[seat] for seat in step.ask} if step.phase != "english_revise" else {}


@pytest.mark.parametrize("key", FORMAT_KEYS)
def test_open_lot_asks_in_the_phase_of_the_format(key: FormatKey) -> None:
    mech = MECHANISMS[key]
    step = open_lot(
        mech,
        tier=200,
        budgets=BUDGETS,
        tie_key=KEYS,
        true_value=100 if key == "blind_box" else None,
    )
    assert not step.done
    assert step.phase == mech.bid_phases[0]
    assert step.ask == (1, 2, 3, 4)


@pytest.mark.parametrize("key", FORMAT_KEYS)
def test_the_generic_driver_equals_the_direct_functions(key: FormatKey) -> None:
    mech = make_mechanism(key, reserve=0 if key == "all_pay" else 15)
    hidden = 100 if key == "blind_box" else None
    outcome = play_lot(
        mech, tier=200, budgets=BUDGETS, tie_key=KEYS, answer=answer, true_value=hidden
    )
    if mech.clock == "none":
        direct = settle_sealed(mech, BIDS, BUDGETS, KEYS, true_value=hidden)
        assert outcome == direct
    elif mech.clock == "descending_levels":
        assert outcome == settle_dutch(mech, 200, BIDS, BUDGETS, KEYS)
    else:
        step = open_lot(mech, tier=200, budgets=BUDGETS, tie_key=KEYS)
        assert step == open_english(mech, BUDGETS, KEYS)
        assert outcome.hammer.winner == 3  # the highest exit price


def test_open_functions_agree_with_open_lot() -> None:
    sealed = make_mechanism("sealed_first")
    assert open_lot(sealed, tier=200, budgets=BUDGETS, tie_key=KEYS) == open_sealed(
        sealed, BUDGETS, KEYS
    )
    dutch = make_mechanism("dutch")
    assert open_lot(dutch, tier=200, budgets=BUDGETS, tie_key=KEYS) == open_dutch(
        dutch, 200, BUDGETS, KEYS
    )


def test_a_hidden_value_belongs_to_the_blind_box_only() -> None:
    for key in ("english", "dutch"):
        with pytest.raises(ValueError, match="true value"):
            open_lot(MECHANISMS[key], tier=200, budgets=BUDGETS, tie_key=KEYS, true_value=5)
    with pytest.raises(ValueError, match="true_value"):
        open_lot(MECHANISMS["sealed_first"], tier=200, budgets=BUDGETS, tie_key=KEYS, true_value=5)
    with pytest.raises(ValueError, match="true_value"):
        open_lot(MECHANISMS["blind_box"], tier=200, budgets=BUDGETS, tie_key=KEYS)


def test_a_decided_lot_cannot_be_stepped_again() -> None:
    step = open_lot(MECHANISMS["sealed_first"], tier=200, budgets=BUDGETS, tie_key=KEYS)
    done = step_lot(step, BIDS)
    assert done.done
    with pytest.raises(ValueError, match="decided"):
        step_lot(done, {})


def test_a_step_carries_its_answers_to_the_right_format() -> None:
    english = open_lot(MECHANISMS["english"], tier=200, budgets=BUDGETS, tie_key=KEYS)
    nxt = step_lot(english, BIDS)
    assert nxt.phase == "english_revise"
    assert nxt.ask == (1, 2, 3)  # seat 4 leaves at 20, three seats remain


# ----------------------------------------------------------------- whole nights


def play_night(seed: str, *, honest: bool = True) -> dict[str, object]:
    """Run all twelve lots of the mock night with value bidding and return the books."""
    config = mock_config()
    night = config.night
    d = deal(config, seed=seed)
    left = dict(d.budgets)
    paid = dict.fromkeys(left, 0)
    value = dict.fromkeys(left, 0)
    sold = unsold = 0
    for lot in d.lots:
        mech = night.mechanism_for(lot.format)
        live = {seat: budget for seat, budget in left.items() if budget >= 1}
        keys = {seat: lot.tie_key[seat] for seat in live}
        hidden = lot.blind_value

        def respond(step: LotStep, lot=lot) -> dict[int, int | None]:
            out: dict[int, int | None] = {}
            for seat in step.ask:
                if step.phase == "english_revise":
                    out[seat] = None
                elif lot.is_blind:
                    out[seat] = lot.signals[seat]
                else:
                    out[seat] = lot.values[seat] if honest else lot.values[seat] * 3
            return out

        outcome = play_lot(
            mech, tier=lot.tier, budgets=live, tie_key=keys, answer=respond, true_value=hidden
        )
        hammer = outcome.hammer
        assert set(outcome.asked) <= set(live)
        for seat, amount in hammer.payments.items():
            assert 0 <= amount <= left[seat]
            left[seat] -= amount
            paid[seat] += amount
        assert all(budget >= 0 for budget in left.values())
        if hammer.unsold:
            unsold += 1
        else:
            sold += 1
            assert hammer.winner is not None
            value[hammer.winner] += lot.values[hammer.winner]
        assert sorted(hammer.newly_out) == sorted(
            seat for seat in hammer.payments if live[seat] >= 1 and left[seat] < 1
        )
    return {"left": left, "paid": paid, "value": value, "sold": sold, "unsold": unsold}


@pytest.mark.parametrize("seed", [f"night-{i}" for i in range(40)])
def test_a_whole_night_runs_with_every_format(seed: str) -> None:
    books = play_night(seed)
    assert books["sold"] + books["unsold"] == 12  # type: ignore[operator]


def test_budgets_bind_when_everyone_overbids() -> None:
    books = play_night("greedy", honest=False)
    left: dict[int, int] = books["left"]  # type: ignore[assignment]
    assert all(budget >= 0 for budget in left.values())
    assert any(budget == 0 for budget in left.values()), "some seat should have run dry"
