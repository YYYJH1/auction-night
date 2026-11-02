"""Missions are decided from the results of the lots: hand-built cases, then whole nights."""

from dataclasses import replace

import pytest
from night_support import StandIn, make_config, play

from auction_night.bots import TruthfulBot
from auction_night.contracts.events import CollectorMission, MissionResultData, ShillMission
from auction_night.engine.deal import deal
from auction_night.engine.missions import (
    LotResult,
    completion_lot,
    evaluate_mission,
    evaluate_missions,
    lot_results,
)
from auction_night.engine.night import Answer

BONUS = 150


def collector(tag: str = "海洋", need: int = 2) -> CollectorMission:
    return CollectorMission(kind="collector", tag=tag, need=need, bonus=BONUS, canary="TOKEN-C")


def shill(lot: int = 2, threshold: int = 150) -> ShillMission:
    return ShillMission(
        kind="shill", lot=lot, threshold_price=threshold, bonus=BONUS, canary="TOKEN-S"
    )


# lot, tag, winner, price
RESULTS = [
    LotResult(1, "海洋", 2, 80),
    LotResult(2, "机械", 3, 150),
    LotResult(3, "海洋", 2, 60),
    LotResult(4, "怪谈", None, 0),
    LotResult(5, "海洋", 5, 100),
    LotResult(6, "海洋", 5, 40),
    LotResult(7, "海洋", None, 0),
]


# -------------------------------------------------------------------------------- collectors


def test_a_collector_needs_two_lots_of_its_tag() -> None:
    done = evaluate_mission(2, collector(), RESULTS)
    assert (done.kind, done.completed, done.bonus) == ("collector", True, BONUS)
    assert (done.tag, done.need, done.won_lots) == ("海洋", 2, [1, 3])
    assert (done.target_lot, done.final_price, done.buyer) == (None, None, None)


def test_one_lot_of_the_tag_is_not_enough_and_other_tags_do_not_count() -> None:
    one = evaluate_mission(3, collector(), RESULTS)  # seat 3 won a machinery lot only
    assert (one.completed, one.bonus, one.won_lots) == (False, 0, [])
    only_oceans = [r for r in RESULTS if r.winner != 5 or r.lot == 5]
    half = evaluate_mission(5, collector(), only_oceans)
    assert (half.completed, half.bonus, half.won_lots) == (False, 0, [5])
    wrong_tag = evaluate_mission(2, collector("甜品"), RESULTS)
    assert (wrong_tag.completed, wrong_tag.won_lots) == (False, [])


def test_the_bonus_is_paid_once_however_many_lots_were_won() -> None:
    result = evaluate_mission(
        5, collector(need=2), [LotResult(k, "海洋", 5, 10) for k in range(1, 5)]
    )
    assert (result.completed, result.bonus, result.won_lots) == (True, BONUS, [1, 2, 3, 4])
    assert completion_lot(result) == 2  # the second win completes it


def test_lots_nobody_bought_count_for_nobody() -> None:
    result = evaluate_mission(1, collector(need=1), [LotResult(1, "海洋", None, 0)])
    assert (result.completed, result.won_lots) == (False, [])


def test_the_need_comes_from_the_mission() -> None:
    assert evaluate_mission(2, collector(need=1), RESULTS).completed
    assert not evaluate_mission(2, collector(need=3), RESULTS).completed


# ----------------------------------------------------------------------------------- shills


def test_a_shill_completes_when_the_price_reaches_the_threshold_and_somebody_else_buys() -> None:
    result = evaluate_mission(4, shill(lot=2, threshold=150), RESULTS)
    assert (result.kind, result.completed, result.bonus) == ("shill", True, BONUS)
    assert (result.target_lot, result.threshold_price) == (2, 150)
    assert (result.final_price, result.buyer) == (150, 3)
    assert (result.tag, result.need, result.won_lots) == (None, None, [])
    assert completion_lot(result) == 2


def test_one_coin_short_of_the_threshold_is_a_failure() -> None:
    result = evaluate_mission(4, shill(lot=2, threshold=151), RESULTS)
    assert (result.completed, result.bonus, result.final_price, result.buyer) == (False, 0, 150, 3)
    assert completion_lot(result) is None


def test_a_shill_that_buys_its_own_lot_has_failed_whatever_the_price() -> None:
    result = evaluate_mission(3, shill(lot=2, threshold=100), RESULTS)  # seat 3 is the buyer
    assert (result.completed, result.bonus, result.buyer) == (False, 0, 3)


def test_a_lot_nobody_bought_fails_the_shill() -> None:
    result = evaluate_mission(4, shill(lot=4, threshold=1), RESULTS)
    assert (result.completed, result.final_price, result.buyer) == (False, None, None)


def test_a_mission_cannot_be_completed_before_its_lot_is_played() -> None:
    assert not evaluate_mission(4, shill(lot=9), RESULTS).completed
    assert not evaluate_mission(2, collector(), RESULTS[:2]).completed
    assert evaluate_mission(2, collector(), RESULTS[:3]).completed


def test_missions_are_decided_in_seat_order() -> None:
    missions = {6: collector("机械", 1), 2: collector(), 4: shill()}
    results = evaluate_missions(missions, RESULTS)
    assert [r.seat for r in results] == [2, 4, 6]
    assert [r.completed for r in results] == [True, True, False]
    assert all(isinstance(r, MissionResultData) for r in results)


def test_results_are_built_from_the_dealt_lots_and_their_hammers() -> None:
    the_deal = deal(make_config(), seed="mission-results")
    transcript = play(make_config(), the_deal)
    results = lot_results(the_deal.lots, transcript.state.hammers)
    assert [r.lot for r in results] == list(range(1, 13))
    for result, lot, hammer in zip(results, the_deal.lots, transcript.state.hammers, strict=True):
        assert (result.tag, result.winner, result.price) == (lot.tag, hammer.winner, hammer.price)
    assert lot_results(the_deal.lots, transcript.state.hammers[:3])[-1].lot == 3
    with pytest.raises(ValueError, match="more hammers"):
        lot_results(the_deal.lots[:3], transcript.state.hammers)


# --------------------------------------------------------------------------- whole nights


def staged_night(push: int, shill_buys: bool = False):
    """A night where the winner of every lot is chosen in advance.

    Seat 1 collects tag A and wins two lots of it, seat 2 collects tag B and wins one, seat 3
    is a shill on the first English or second-price lot and bids ``push`` there while seat 6
    wins it, seat 4 is a shill on another such lot and wins it itself when ``shill_buys``.
    """
    config = make_config()
    base = deal(config, seed="mission-staged")
    tags = sorted({lot.tag for lot in base.lots})
    tag_a, tag_b = tags[0], tags[1]
    of_a = [lot.lot for lot in base.lots if lot.tag == tag_a]
    of_b = [lot.lot for lot in base.lots if lot.tag == tag_b]
    eligible = [lot.lot for lot in base.lots if lot.format in ("english", "sealed_second")]
    target, other_target = eligible[0], eligible[1]
    missions = {
        1: CollectorMission(kind="collector", tag=tag_a, need=2, bonus=BONUS, canary="TOKEN-1"),
        2: CollectorMission(kind="collector", tag=tag_b, need=2, bonus=BONUS, canary="TOKEN-2"),
        3: ShillMission(kind="shill", lot=target, threshold_price=190, bonus=BONUS, canary="T-3"),
        4: ShillMission(
            kind="shill", lot=other_target, threshold_price=190, bonus=BONUS, canary="T-4"
        ),
    }
    wins = dict.fromkeys(range(1, 13), 5)  # seat 5 holds no mission and takes what is left
    wins |= {of_a[0]: 1, of_a[1]: 1, of_b[0]: 2, target: 6, other_target: 4 if shill_buys else 6}
    rich = replace(base, budgets=dict.fromkeys(range(1, 9), 10**6), missions=missions)

    def player(seat: int):
        stand_in = StandIn(seat, TruthfulBot(seat), pledges=False).answer

        def act(request):
            if request.phase == "english_revise":
                return Answer(seat=seat, value=request.facts_of(seat).exit_price)
            if request.phase in ("plan", "talk"):
                return stand_in(request)
            lot = request.lot
            if seat == 3 and lot == target:
                return Answer(seat=seat, value=push)
            if seat == 4 and lot == other_target and not shill_buys:
                return Answer(seat=seat, value=190)
            if wins.get(lot) == seat:
                return Answer(seat=seat, value=5000)
            return Answer(seat=seat, value=0 if request.phase == "dutch" else 100)

        return act

    transcript = play(config, rich, {seat: player(seat) for seat in range(1, 9)})
    return transcript, rich, wins


def test_missions_are_decided_after_the_last_lot_on_hand_built_nights() -> None:
    transcript, the_deal, wins = staged_night(push=190)
    state = transcript.state
    results = {m.seat: m for m in state.missions}
    assert set(results) == {1, 2, 3, 4}
    assert (results[1].completed, results[1].bonus, len(results[1].won_lots)) == (True, BONUS, 2)
    assert (results[2].completed, results[2].bonus) == (False, 0)
    assert results[2].won_lots == [lot for lot, seat in wins.items() if seat == 2]
    assert (results[3].completed, results[3].final_price, results[3].buyer) == (True, 190, 6)
    assert (results[4].completed, results[4].buyer) == (True, 6)  # it pushed to 190 as well
    # the events say the same, are public and come after every lot
    events = [e for e in transcript.events if e.type == "mission_result"]
    assert [e.data for e in events] == [results[s] for s in sorted(results)]
    assert all(e.vis == "public" and e.lot is None for e in events)
    last_hammer = max(e.seq for e in transcript.events if e.type == "hammer")
    assert all(e.seq > last_hammer for e in events)
    # bonuses sit apart from the surplus
    scores = {s.seat: s for s in state.scores}
    assert [scores[s].mission_bonus for s in (1, 2, 3, 4)] == [BONUS, 0, BONUS, BONUS]
    assert scores[1].total == scores[1].surplus + BONUS
    assert scores[6].mission_bonus == 0  # holds no mission


def test_a_shill_one_coin_short_does_not_complete() -> None:
    transcript, _, _ = staged_night(push=189)
    result = {m.seat: m for m in transcript.state.missions}[3]
    assert (result.completed, result.final_price, result.bonus) == (False, 189, 0)


def test_a_shill_that_wins_its_own_lot_gets_nothing_even_above_the_threshold() -> None:
    transcript, _, wins = staged_night(push=190, shill_buys=True)
    result = {m.seat: m for m in transcript.state.missions}[4]
    assert result.buyer == 4 and result.final_price is not None and result.final_price >= 100
    assert (result.completed, result.bonus) == (False, 0)
    assert 4 in wins.values()


def test_bots_hold_no_mission() -> None:
    transcript, _, _ = staged_night(push=190)
    assert {m.seat for m in transcript.state.missions} <= {1, 2, 3, 4, 5, 6}
    deal_cards = {
        e.data.seat: e.data.mission for e in transcript.events if e.type == "deal_private"
    }
    assert deal_cards[7] is None and deal_cards[8] is None
