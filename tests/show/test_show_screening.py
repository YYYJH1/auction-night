"""The bid statistic for collusion and the threshold that rule bots set."""

import itertools

import pytest
from night_support import all_bots_config, play_seed

from auction_night.show import screening
from auction_night.show.screening import (
    CALIBRATION_NIGHTS,
    CALIBRATION_PERCENTILE,
    CALIBRATION_SEED,
    CALIBRATION_VERSION,
    NULL_THRESHOLD_MILLI,
    LotScreen,
    calibrate_threshold,
    nearest_rank,
    null_statistics,
    play_bot_night,
    screen_state,
)
from auction_night.simulate import night_seed, play_night, preset_config


def screen(
    *,
    tier: int = 100,
    winner: int | None = 1,
    counterfactual: int = 80,
    observed: int = 50,
    ref: dict[int, int] | None = None,
    bids: dict[int, int] | None = None,
) -> LotScreen:
    return LotScreen(
        lot=1,
        tier=tier,
        winner=winner,
        counterfactual=counterfactual,
        observed=observed,
        ref={1: 70, 2: 60, 4: 50, 5: 40} if ref is None else ref,
        bids={1: 70, 2: 0, 4: 0, 5: 40} if bids is None else bids,
    )


# ------------------------------------------------------------------------------ discount


def test_the_discount_is_how_far_the_lot_sold_below_the_textbook_lot_in_thousandths() -> None:
    assert screen(counterfactual=80, observed=50).discount_milli == 300
    assert screen(counterfactual=80, observed=80).discount_milli == 0
    assert screen(counterfactual=50, observed=80).discount_milli == -300  # sold above it
    assert screen(counterfactual=80, observed=50, tier=400).discount_milli == 75
    assert screen(counterfactual=1, observed=0, tier=3).discount_milli == 333  # floor
    assert screen(counterfactual=0, observed=1, tier=3).discount_milli == -334  # floor, not trunc


def test_an_unsold_lot_has_the_whole_textbook_takings_as_its_discount() -> None:
    unsold = screen(winner=None, counterfactual=60, observed=0)
    assert unsold.discount_milli == 600


# --------------------------------------------------------------------------------- cover


def test_cover_members_are_the_ring_seats_that_were_asked_and_did_not_win() -> None:
    lot = screen(winner=1)
    assert lot.cover_members([4, 2]) == [2, 4]  # in seat order
    assert lot.cover_members([1, 2]) == [2]  # the winner is not covering anybody
    assert lot.cover_members([2, 3, 6]) == [2]  # seats 3 and 6 were not asked
    assert lot.cover_members([]) == []
    assert screen(winner=None).cover_members([1, 2]) == [1, 2]  # nobody won


def test_cover_is_how_far_below_the_textbook_the_members_stayed_on_average() -> None:
    lot = screen(tier=100)
    # seat 2 stayed 60 below its textbook bid and seat 4 stayed 50 below: 110 / 2 = 55 coins
    assert lot.cover_milli([2, 4]) == 550
    assert lot.cover_milli([2]) == 600
    assert lot.cover_milli([2, 1]) == 600  # the winner (seat 1) is left out of the average
    assert lot.cover_milli([5]) == 0  # seat 5 bid exactly its textbook bid
    assert lot.cover_milli([]) == 0 and lot.cover_milli([1]) == 0 and lot.cover_milli([3]) == 0


def test_bidding_above_the_textbook_is_no_cover_and_a_missing_bid_is_a_zero() -> None:
    above = screen(ref={2: 30, 4: 50}, bids={2: 90, 4: 0}, winner=5)
    assert above.cover_milli([2, 4]) == 250  # seat 2 counts 0, seat 4 counts 50: 25 coins
    silent = screen(ref={2: 40}, bids={}, winner=1)
    assert silent.cover_milli([2]) == 400


def test_the_statistic_is_the_discount_plus_the_cover() -> None:
    lot = screen(counterfactual=80, observed=50)
    assert lot.statistic_milli() == lot.discount_milli == 300
    assert lot.statistic_milli([2, 4]) == 300 + 550
    assert lot.statistic_milli([5]) == 300
    assert screen(counterfactual=50, observed=80).statistic_milli([2, 4]) == -300 + 550


def test_every_number_is_a_whole_number() -> None:
    lot = screen(tier=300, counterfactual=77, observed=41)
    for value in (lot.discount_milli, lot.cover_milli([2, 4]), lot.statistic_milli([2, 4])):
        assert type(value) is int


# --------------------------------------------------------------------------- real nights


def test_a_night_in_which_everybody_bids_the_textbook_has_nothing_to_screen() -> None:
    config = all_bots_config("economist", pacing=False)
    for seed in ("screen-a", "screen-b", "screen-c"):
        screens = screen_state(play_seed(config, seed).state)
        assert len(screens) == 12
        for lot in screens:
            assert lot.counterfactual == lot.observed, (seed, lot.lot)
            assert lot.discount_milli == 0 and lot.statistic_milli([1, 2]) == 0


def test_bidding_the_value_is_dear_in_first_price_lots_and_right_in_second_price_ones() -> None:
    state = play_seed(all_bots_config("truthful"), "screen-d").state
    formats = {lot.lot: lot.format for lot in state.deal.lots}
    first = [s.discount_milli for s in screen_state(state) if formats[s.lot] == "sealed_first"]
    second = [s.discount_milli for s in screen_state(state) if formats[s.lot] == "sealed_second"]
    assert first and all(d <= 0 for d in first) and any(d < 0 for d in first)  # sold above it
    assert second and all(d == 0 for d in second)  # the value is the textbook bid there


def test_the_screen_reads_the_real_takings_and_bids_of_the_night() -> None:
    state = play_seed(all_bots_config("truthful"), "screen-e").state
    for lot, record in zip(screen_state(state), state.records, strict=True):
        assert lot.lot == record.lot and lot.winner == record.outcome.hammer.winner
        assert lot.observed == sum(record.outcome.hammer.payments.values())
        assert set(lot.ref) == set(record.outcome.asked) == set(lot.bids)


# ------------------------------------------------------------------------ the null nights


def test_the_bot_night_is_the_night_the_simulation_plays() -> None:
    config = preset_config(300)
    for k in range(3):
        seed = night_seed("screen-sim", k)
        state = play_bot_night(config, seed)
        played = play_night(config, seed)
        assert state.done
        assert [s.surplus for s in sorted(state.scores, key=lambda s: s.seat)] == [
            s.surplus for s in sorted(played.seats, key=lambda s: s.seat)
        ]
        assert [s.spent for s in sorted(state.scores, key=lambda s: s.seat)] == [
            s.spent for s in sorted(played.seats, key=lambda s: s.seat)
        ]


def test_the_null_values_are_one_per_lot_and_pair_and_do_not_depend_on_anything_else() -> None:
    config = preset_config(300)
    values = null_statistics(config, 3, "screen-null-test")
    assert len(values) == 3 * 12 * 15  # three nights, twelve lots, the 15 pairs of 6 stand-ins
    assert values == null_statistics(config, 3, "screen-null-test")
    first = screen_state(play_bot_night(config, night_seed("screen-null-test", 0)))
    pairs = list(itertools.combinations(range(1, 7), 2))
    expected = [lot.statistic_milli(pair) for lot in first for pair in pairs]
    assert values[: len(expected)] == expected
    assert values != null_statistics(config, 3, "screen-null-test-2")


def test_the_percentile_is_the_nearest_rank() -> None:
    hundred = list(range(1, 101))
    assert nearest_rank(hundred, 95) == 95 and nearest_rank(hundred, 100) == 100
    assert nearest_rank(hundred, 1) == 1 and nearest_rank(hundred, 50) == 50
    assert nearest_rank([10, 20, 30], 50) == 20  # ceil(1.5) = 2
    assert nearest_rank([10, 20, 30], 95) == 30
    assert nearest_rank([7], 95) == 7
    with pytest.raises(ValueError, match="no values"):
        nearest_rank([], 95)
    with pytest.raises(ValueError, match="percentile"):
        nearest_rank([1], 0)
    with pytest.raises(ValueError, match="percentile"):
        nearest_rank([1], 101)


def test_a_small_calibration_reports_what_it_counted() -> None:
    result = calibrate_threshold(nights=8, seed="screen-small")
    assert (result.nights, result.seed, result.percentile) == (8, "screen-small", 95)
    assert result.version == CALIBRATION_VERSION and result.observations == 8 * 12 * 15
    values = sorted(null_statistics(preset_config(300), 8, "screen-small"))
    assert result.threshold_milli == nearest_rank(values, 95)
    assert result.over == sum(1 for v in values if v > result.threshold_milli)
    assert result.over <= result.observations * 5 // 100  # strictly above: at most 5 in 100
    assert calibrate_threshold(nights=8, seed="screen-small") == result
    with pytest.raises(ValueError, match="nights"):
        calibrate_threshold(nights=0)


# -------------------------------------------------------------------- the published constant


def test_the_published_threshold_is_what_the_rule_bots_give_when_nobody_colludes() -> None:
    result = calibrate_threshold()
    assert (result.nights, result.seed, result.percentile) == (
        CALIBRATION_NIGHTS,
        CALIBRATION_SEED,
        CALIBRATION_PERCENTILE,
    )
    assert result.version == CALIBRATION_VERSION == "screen.v1"
    assert result.threshold_milli == NULL_THRESHOLD_MILLI, (
        "the bots, the engine or the formula changed: recompute the constant with "
        "`python -m auction_night.show.screening` and raise CALIBRATION_VERSION"
    )
    assert result.observations == CALIBRATION_NIGHTS * 12 * 15
    assert 0 < result.over <= result.observations * 5 // 100
    assert type(NULL_THRESHOLD_MILLI) is int and NULL_THRESHOLD_MILLI > 0


def test_the_command_prints_the_calibration_and_checks_the_constant(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert screening.main(["--nights", "3", "--seed", "screen-cli"]) == 0
    out = capsys.readouterr().out
    assert "screen.v1: 3 nights, seed 'screen-cli'" in out and "540 placebo values" in out
    expected = calibrate_threshold(nights=3, seed="screen-cli").threshold_milli
    monkeypatch.setattr(screening, "NULL_THRESHOLD_MILLI", expected)
    assert screening.main(["--nights", "3", "--seed", "screen-cli", "--check"]) == 0
    monkeypatch.setattr(screening, "NULL_THRESHOLD_MILLI", expected + 1)
    assert screening.main(["--nights", "3", "--seed", "screen-cli", "--check"]) == 1
    assert "differs from the constant" in capsys.readouterr().err
