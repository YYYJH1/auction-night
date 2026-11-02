"""The reference numbers of a lot and of a night, collected."""

import math
from dataclasses import fields, replace

from night_support import all_bots_config, bot_seats, make_config, play, play_seed

from auction_night.bots import make_bot
from auction_night.contracts.config import BlindConfig
from auction_night.engine.deal import blind_bounds, deal
from auction_night.engine.night import Answer
from auction_night.theory import analyse_lot, analyse_night, expected_revenue
from auction_night.theory.pacing import alpha_milli, pacing_alpha

MIXED = make_config()


def test_every_lot_gets_its_numbers_for_the_seats_that_were_asked() -> None:
    transcript = play_seed(MIXED, "lot-mixed")
    state = transcript.state
    results = analyse_night(state)
    assert [r.lot for r in results] == list(range(1, 13))
    for theory, record in zip(results, state.records, strict=True):
        asked = sorted(record.outcome.asked)
        assert theory.bidders == len(asked)
        assert sorted(theory.ref_bids) == sorted(theory.excess) == sorted(theory.regret) == asked
        lot = state.deal.lot(record.lot)
        for seat, ref in theory.ref_bids.items():
            assert 0 <= ref <= record.budgets_before[seat]
            if lot.is_blind:
                bounds = blind_bounds(lot.tier, MIXED.night.blind)
                assert min(bounds.low, record.budgets_before[seat]) <= ref
        assert all(r >= 0 for r in theory.regret.values())


def test_every_number_is_an_integer_a_flag_or_absent() -> None:
    results = analyse_night(play_seed(MIXED, "lot-types").state)
    for theory in results:
        for field in fields(theory):
            value = getattr(theory, field.name)
            if isinstance(value, dict):
                assert all(isinstance(k, int) and type(v) is int for k, v in value.items())
            else:
                assert value is None or type(value) in (int, bool), field.name


def test_there_is_no_efficiency_style_number_in_a_lot() -> None:
    banned = ("efficiency", "welfare", "ratio", "poa", "anarchy", "total", "sum")
    for field in fields(analyse_night(play_seed(MIXED, "lot-names").state)[0]):
        assert not any(word in field.name.lower() for word in banned), field.name


def test_the_pacing_factor_is_reported_for_pacing_economists_only() -> None:
    seats = bot_seats(
        [
            ("economist", True),
            ("economist", False),
            ("truthful", False),
            ("economist", True),
        ]
    )
    config = make_config(seats, missions={"collectors": 0, "shills": 0}, budget_c=60)
    transcript = play_seed(config, "lot-alpha")
    state = transcript.state
    results = analyse_night(state)
    low = 1000
    for theory, record in zip(results, state.records, strict=True):
        assert set(theory.alpha_milli) == {s for s in (1, 4) if s in record.outcome.asked}
        n = max(len(record.outcome.asked), 2)
        schedule = [(item.tier, item.is_blind) for item in state.deal.lots[record.lot - 1 :]]
        for seat, milli in theory.alpha_milli.items():
            left = record.budgets_before[seat]
            assert milli == alpha_milli(pacing_alpha(left, schedule, n))
            assert 0 < milli <= 1000
            low = min(low, milli)
    assert low < 1000, "small budgets must make the economists pace"


def test_the_expected_revenue_is_the_exact_value_for_the_number_of_bidders() -> None:
    config = all_bots_config("truthful", n_seats=8, budget_c=20_000)
    transcript = play_seed(config, "lot-revenue")
    for theory, record in zip(
        analyse_night(transcript.state), transcript.state.records, strict=True
    ):
        lot = transcript.state.deal.lot(record.lot)
        mech = config.night.mechanism_for(lot.format)
        exact = expected_revenue(mech, lot.tier, 8, config.night.blind)
        assert exact is not None
        assert theory.expected_revenue_milli == math.floor(exact * 1000)
        assert theory.bidders == 8
    # fewer bidders earn less, on average
    five = all_bots_config("truthful", n_seats=5, budget_c=20_000)
    state = play_seed(five, "lot-revenue").state
    for theory, record in zip(analyse_night(state), state.records, strict=True):
        lot = state.deal.lot(record.lot)
        mech = five.night.mechanism_for(lot.format)
        assert theory.bidders == 5
        assert theory.expected_revenue_milli == math.floor(
            expected_revenue(mech, lot.tier, 5, five.night.blind) * 1000  # type: ignore[operator]
        )


def test_the_expected_revenue_is_absent_where_there_is_no_closed_form() -> None:
    reserve = make_config(reserve=10)
    results = analyse_night(play_seed(reserve, "lot-reserve").state)
    by_format = {}
    state = play_seed(reserve, "lot-reserve").state
    for theory, record in zip(results, state.records, strict=True):
        by_format.setdefault(record.outcome.hammer.format, set()).add(theory.expected_revenue_milli)
    assert by_format["sealed_second"] == {None} and by_format["english"] == {None}
    assert by_format["all_pay"] != {None}  # an all-pay lot never has a reserve
    two = all_bots_config("truthful", n_seats=2, budget_c=20_000)
    state = play_seed(two, "lot-two").state
    assert all(t.expected_revenue_milli is not None for t in analyse_night(state))  # two is enough


def test_one_bidder_has_no_competition_to_forecast() -> None:
    config = all_bots_config("truthful", budget_c=20_000)
    base = deal(config, seed="lot-one")
    alone = replace(base, budgets={seat: 100_000 if seat == 1 else 0 for seat in range(1, 9)})
    players = {seat.seat: make_bot(seat).answer for seat in config.seats}
    state = play(config, alone, players).state
    for theory, record in zip(analyse_night(state), state.records, strict=True):
        assert record.outcome.asked == (1,) and theory.bidders == 1
        assert theory.expected_revenue_milli is None
        assert list(theory.regret) == [1]
        if record.outcome.hammer.format in ("english", "sealed_second"):
            assert theory.regret == {1: 0}  # nobody to outbid and the price is the reserve


# ------------------------------------------------------------ to the highest value, curse


def private_night(winner_hole: int | None = None):
    """Eight truthful bots in a night where seat s values every ordinary lot at 10 * s."""
    config = all_bots_config("truthful", budget_c=20_000)
    base = deal(config, seed="lot-highest")
    lots = tuple(
        lot if lot.is_blind else replace(lot, values={s: 10 * s for s in range(1, 9)})
        for lot in base.lots
    )
    the_deal = replace(base, lots=lots, budgets=dict.fromkeys(range(1, 9), 100_000))
    players = {seat.seat: make_bot(seat).answer for seat in config.seats}
    if winner_hole is not None:
        honest = players[winner_hole]

        def silent(request):
            if request.phase in ("english_init", "dutch", "sealed"):
                return Answer.abstained(winner_hole, "timeout")
            return honest(request)

        players[winner_hole] = silent
    return play(config, the_deal, players)


def test_the_lot_goes_to_the_highest_value_when_the_highest_value_bids() -> None:
    transcript = private_night()
    state = transcript.state
    for theory, record in zip(analyse_night(state), state.records, strict=True):
        lot = state.deal.lot(record.lot)
        if lot.is_blind:
            assert theory.to_highest_value is None
        else:
            assert record.outcome.hammer.winner == 8 and theory.to_highest_value is True


def test_it_does_not_when_the_highest_value_stays_silent() -> None:
    transcript = private_night(winner_hole=8)
    state = transcript.state
    seen = 0
    for theory, record in zip(analyse_night(state), state.records, strict=True):
        lot = state.deal.lot(record.lot)
        if not lot.is_blind:
            assert record.outcome.hammer.winner == 7 and theory.to_highest_value is False
            seen += 1
    assert seen == 10


def test_an_unsold_lot_is_given_to_nobody() -> None:
    config = make_config()
    the_deal = replace(deal(config, seed="lot-unsold"), budgets=dict.fromkeys(range(1, 9), 0))
    state = play(config, the_deal).state
    for theory in analyse_night(state):
        assert (theory.to_highest_value, theory.curse_gap, theory.top_signal_error) == (
            None,
            None,
            None,
        )
        assert theory.ref_bids == {} and theory.regret == {} and theory.bidders == 0
        assert theory.expected_revenue_milli is None


def test_a_blind_box_shows_the_gap_between_price_and_value_and_the_luck_of_the_top_appraisal() -> (
    None
):
    transcript = play_seed(MIXED, "lot-curse")
    state = transcript.state
    boxes = 0
    for theory, record in zip(analyse_night(state), state.records, strict=True):
        lot = state.deal.lot(record.lot)
        hammer = record.outcome.hammer
        if not lot.is_blind:
            assert theory.curse_gap is None and theory.top_signal_error is None
            continue
        boxes += 1
        assert lot.blind_value is not None
        assert theory.curse_gap == hammer.price - lot.blind_value
        top = max(lot.signals[seat] for seat in record.outcome.asked)
        assert theory.top_signal_error == top - lot.blind_value
    assert boxes == 2


def test_analyse_lot_takes_the_pieces_it_needs() -> None:
    transcript = play_seed(MIXED, "lot-pieces")
    state = transcript.state
    record = state.records[3]
    mech = MIXED.night.mechanism_for(state.deal.lot(record.lot).format)
    one = analyse_lot(state.deal, record, mech, MIXED.night.blind, pacing_seats=[8])
    assert one == analyse_night(state)[3]
    assert one.alpha_milli.keys() <= {8}
    assert isinstance(MIXED.night.blind, BlindConfig)


def test_the_overlay_of_the_replay_can_be_built_from_a_lot() -> None:
    from auction_night.contracts.replay import LotOverlay

    results = analyse_night(play_seed(MIXED, "lot-overlay").state)
    for theory in results:
        overlay = theory.overlay()
        assert isinstance(overlay, LotOverlay)
        assert overlay.ref_bids == theory.ref_bids and overlay.regret == theory.regret
        assert overlay.alpha_milli == theory.alpha_milli
        assert overlay.to_highest_value == theory.to_highest_value
        assert overlay.excess == theory.excess and overlay.curse_gap == theory.curse_gap
        assert overlay.expected_revenue_milli == theory.expected_revenue_milli
