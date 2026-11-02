"""Awards: each rule, its lower limit, ties, vacancies, the bots' list, and the log oracle.

The first half builds lots by hand, so every number of an award can be checked on paper. The
second half plays nights (the mock policies and the rule bots, and scripted seats) and compares
the awards with an oracle that reads the event log and the deal and nothing else.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from prize_support import (
    LogOracle,
    awards_night,
    mock_night,
    play_mock_night,
    viewer_awards,
    viewer_basis,
)
from show_support import make_lot, seat_line

from auction_night.contracts.events import (
    AWARD_KEYS,
    AwardData,
    JudgeVerdictData,
    MissionResultData,
    Pledge,
    canonical_json,
)
from auction_night.contracts.replay import PledgeRecord, Replay, SeatStats
from auction_night.engine.night import append_events
from auction_night.show import awards as prizes
from auction_night.show.awards import (
    AWARD_BASIS,
    AWARD_UNITS,
    BOT_AWARDS,
    Awards,
    build_awards,
    compute_awards,
)
from auction_night.show.cards import GuessLine, PriorLine

SPAN = 300


def award(result: Awards, key: str) -> AwardData:
    return result.award(key)  # type: ignore[arg-type]


def winners(result: Awards, key: str) -> tuple[list[int], int | None]:
    entry = award(result, key)
    return entry.winners, entry.metric


# ------------------------------------------------------------------ the shape of the result


def test_with_nothing_to_judge_all_ten_awards_are_vacant_in_table_order() -> None:
    result = build_awards([], budget_span=SPAN)
    assert [entry.award for entry in result.models] == list(AWARD_KEYS)
    for entry in result.models:
        assert entry.vacant and entry.winners == [] and entry.metric is None
        assert entry.metric_unit == "none"


def test_every_award_says_what_it_rests_on_whether_it_has_a_winner_or_not() -> None:
    vacant = build_awards(table(1, 2, bots=(7,)), budget_span=SPAN)
    assert all(entry.vacant for entry in (*vacant.models, *vacant.bots))
    given = build_awards(
        table(1, 2, 3, 4, 5, bots=(7,)),
        pledges=[pledge_record(1, "violated")],
        missions=[MissionResultData(seat=2, kind="shill", completed=True, bonus=50,
                                    target_lot=1, threshold_price=10, final_price=20, buyer=3)],
        budget_span=SPAN,
    )  # fmt: skip
    assert award(given, "backstabber").winners == [1] and award(given, "mission_master").winners
    for result in (vacant, given):
        for entry in (*result.models, *result.bots):
            assert entry.basis == AWARD_BASIS[entry.award], entry.award
    assert {entry.basis for entry in given.models} == {"public", "private"}


def test_the_reference_list_exists_only_when_a_bot_sits_at_the_table() -> None:
    assert build_awards(table(1, 2), budget_span=SPAN).bots == ()
    result = build_awards(table(1, 2, bots=(7,)), budget_span=SPAN)
    assert [entry.award for entry in result.bots] == list(BOT_AWARDS)
    for entry in result.bots:
        assert entry.vacant and entry.winners == [] and entry.metric is None
        assert entry.metric_unit == "none"


def test_the_ten_keys_are_the_ten_of_the_contract_and_each_has_a_unit_and_a_basis() -> None:
    assert len(AWARD_KEYS) == 10
    assert set(AWARD_UNITS) == set(AWARD_KEYS) == set(AWARD_BASIS)
    assert BOT_AWARDS == ("most_textbook", "winners_curse", "tilt", "bargain")


def test_an_award_with_winners_carries_the_unit_of_its_figure() -> None:
    pledges = [pledge_record(1, "violated")]
    result = build_awards(table(1, 2, 3, 4, 5), pledges=pledges, budget_span=SPAN)
    entry = award(result, "backstabber")
    assert (entry.winners, entry.metric, entry.metric_unit, entry.vacant) == (
        [1],
        1,
        "count",
        False,
    )
    lots = [blind_lot(1, tier=200, winner=1, price=150, true=100, rows=[1, 2])]
    entry = award(build_awards(lots, budget_span=SPAN), "winners_curse")
    assert (entry.winners, entry.metric, entry.metric_unit, entry.vacant) == (
        [1],
        50,
        "coins",
        False,
    )


# ------------------------------------------------------------------------ small builders


def plain_lot(lot: int, tier: int, rows: dict[int, tuple[int | None, int]], **extra: Any) -> Any:
    """A sealed second-price lot. ``rows`` maps a seat to (played, reference); ``None`` played
    means the seat abstained."""
    lines = [
        seat_line(seat, played=played, ref=ref, abstained=played is None)
        for seat, (played, ref) in rows.items()
    ]
    return make_lot(lot=lot, fmt="sealed_second", tier=tier, seats=lines, **extra)


def blind_lot(
    lot: int, *, tier: int, winner: int | None, price: int, true: int, rows: list[int], **extra: Any
) -> Any:
    """A blind box won by ``winner`` at ``price``, with ``true`` as its hidden value."""
    lines = [
        seat_line(seat, won=seat == winner, paid=price if seat == winner else 0) for seat in rows
    ]
    return make_lot(
        lot=lot,
        fmt="blind_box",
        tier=tier,
        seats=lines,
        winner=winner,
        price=price,
        true_value=true,
        prior=PriorLine(low=tier // 2, high=tier * 3 // 2, noise=tier // 5),
        **extra,
    )


def all_pay_lot(lot: int, *, tier: int, winner: int, bids: dict[int, int]) -> Any:
    """An all-pay lot: the highest bid wins, everybody pays their own bid."""
    lines = [seat_line(s, won=s == winner, paid=b) for s, b in bids.items()]
    return make_lot(
        lot=lot,
        fmt="all_pay",
        tier=tier,
        seats=lines,
        winner=winner,
        price=bids[winner],
        payments=dict(bids),
    )


def sold_lot(lot: int, *, tier: int, winner: int, price: int, value: int, rows: list[int]) -> Any:
    """An ordinary lot (second price) that ``winner`` took at ``price`` with ``value``."""
    lines = [
        seat_line(
            s, won=s == winner, paid=price if s == winner else 0, value=value if s == winner else 10
        )
        for s in rows
    ]
    return make_lot(
        lot=lot, fmt="sealed_second", tier=tier, seats=lines, winner=winner, price=price
    )


def bots_in(lots: list[Any], *seats: int) -> list[Any]:
    """The same lots with the given seats sitting as rule bots."""
    return [
        lot.model_copy(
            update={
                "seats": tuple(
                    line.model_copy(update={"role": "bot"}) if line.seat in seats else line
                    for line in lot.seats
                )
            }
        )
        for lot in lots
    ]


def table(*players: int, bots: tuple[int, ...] = ()) -> list[Any]:
    """One bare lot that seats the given players and bots (the awards read roles from lots)."""
    lines = [seat_line(n) for n in players] + [seat_line(n, role="bot") for n in bots]
    return [make_lot(seats=lines)]


def pledge_record(seat: int, outcome: str) -> PledgeRecord:
    """A pledge of ``seat`` not to bid in lot 3, with the outcome the program decided."""
    return PledgeRecord(
        seq=10, lot=3, seat=seat, pledge=Pledge(kind="no_bid", lot=3), outcome=outcome
    )  # type: ignore[arg-type]


def false_claim(seat: int, lot: int = 2, **extra: Any) -> JudgeVerdictData:
    return JudgeVerdictData(verdict="false_claim", seats=[seat], lots=[lot], **extra)


def collector(seat: int, won: list[int], *, need: int = 2, done: bool = True) -> MissionResultData:
    return MissionResultData(
        seat=seat,
        kind="collector",
        completed=done,
        bonus=150 if done else 0,
        tag="海洋",
        need=need,
        won_lots=won,
    )


def shill(seat: int, lot: int, *, done: bool = True) -> MissionResultData:
    return MissionResultData(
        seat=seat,
        kind="shill",
        completed=done,
        bonus=150 if done else 0,
        target_lot=lot,
        threshold_price=190,
        final_price=200 if done else 100,
        buyer=8 if done else None,
    )


# ----------------------------------------------------------------------- most textbook


def six_lots(rows_of: dict[int, list[tuple[int | None, int]]], tiers: list[int]) -> list[Any]:
    """Lots whose rows are taken from ``rows_of``: seat -> [(played, reference)] per lot."""
    return [
        plain_lot(index + 1, tier, {seat: rows[index] for seat, rows in rows_of.items()})
        for index, tier in enumerate(tiers)
    ]


TIERS = [100, 100, 100, 200, 200, 200]


def table_of(*offsets: int) -> dict[int, list[tuple[int | None, int]]]:
    """Seat n bids ``offsets[n-1]`` above a reference of 50 in each of the six lots."""
    return {seat: [(50 + off, 50)] * 6 for seat, off in enumerate(offsets, start=1)}


def test_the_textbook_figure_is_the_mean_gap_in_tiers_in_thousandths() -> None:
    result = build_awards(six_lots(table_of(0, 10, 20), TIERS), budget_span=SPAN)
    # |gap| / tier is 1/10 in the three small lots and 1/20 in the three big ones: 9/20 over 6.
    assert [m.textbook_deviation_milli for m in result.metrics.values()] == [0, 75, 150]
    assert winners(result, "most_textbook") == ([1], 0)
    assert award(result, "most_textbook").metric_unit == "milli"


def test_a_bid_below_the_reference_is_as_far_as_one_above() -> None:
    table = {1: [(40, 50)] * 6, 2: [(60, 50)] * 6}
    metrics = build_awards(six_lots(table, TIERS), budget_span=SPAN).metrics
    assert metrics[1].textbook_deviation_milli == metrics[2].textbook_deviation_milli == 75


def test_the_figure_is_rounded_down() -> None:
    table = {1: [(150, 50)] * 6, 2: [(51, 50)] * 6}
    result = build_awards(six_lots(table, [300] * 6), budget_span=SPAN)
    assert result.metrics[1].textbook_deviation_milli == 333  # 100/300
    assert result.metrics[2].textbook_deviation_milli == 3  # 1/300 is 3.33 thousandths


def test_a_seat_with_five_valid_bids_cannot_win_but_six_can() -> None:
    table = table_of(0, 10, 20)
    table[1] = [*table[1][:5], (None, 50)]  # abstains in the sixth lot
    result = build_awards(six_lots(table, TIERS), budget_span=SPAN)
    assert result.metrics[1].textbook_bids == 5
    assert winners(result, "most_textbook") == ([2], 75)
    assert winners(
        build_awards(six_lots(table_of(0, 10), TIERS), budget_span=SPAN), "most_textbook"
    )[0] == [1]


def test_the_award_is_vacant_when_nobody_has_six_valid_bids() -> None:
    result = build_awards(six_lots(table_of(0, 10), TIERS)[:5], budget_span=SPAN)
    assert award(result, "most_textbook").vacant


def test_seats_with_the_same_textbook_figure_win_together() -> None:
    result = build_awards(six_lots(table_of(5, 5, 20), TIERS), budget_span=SPAN)
    assert winners(result, "most_textbook") == ([1, 2], 37)


def test_a_bid_of_zero_is_a_bid_and_an_abstention_is_not() -> None:
    table = {1: [(0, 50)] * 6, 2: [(None, 50)] * 6}
    metrics = build_awards(six_lots(table, [100] * 6), budget_span=SPAN).metrics
    assert (metrics[1].textbook_bids, metrics[1].textbook_deviation_milli) == (6, 500)
    assert (metrics[2].textbook_bids, metrics[2].textbook_deviation_milli) == (0, None)


def test_blind_boxes_and_seats_that_were_not_asked_do_not_count_for_the_textbook() -> None:
    lots = six_lots(table_of(0, 10), TIERS)
    box = blind_lot(7, tier=100, winner=1, price=90, true=90, rows=[1, 2])
    box = box.model_copy(
        update={
            "seats": tuple(
                line.model_copy(update={"played": 500, "ref_bid": 1}) for line in box.seats
            )
        }
    )
    out = make_lot(lot=8, tier=100, seats=[seat_line(1, asked=False, played=None, ref=5)])
    metrics = build_awards([*lots, box, out], budget_span=SPAN).metrics
    assert metrics[1].textbook_bids == 6 and metrics[1].textbook_deviation_milli == 0


def test_bots_are_ranked_among_the_bots_and_never_among_the_models() -> None:
    rows = {1: (60, 50), 2: (50, 50), 7: (50, 50), 8: (80, 50)}
    lots = bots_in([plain_lot(n + 1, TIERS[n], rows) for n in range(6)], 7, 8)
    result = build_awards(lots, budget_span=SPAN)
    assert winners(result, "most_textbook") == ([2], 0)  # the bot 7 has 0 as well and is not here
    assert result.bot_award("most_textbook").winners == [7]
    assert result.bot_award("most_textbook").metric == 0


# ----------------------------------------------------------------------- winner's curse


def test_a_gap_of_a_twentieth_of_the_tier_qualifies_and_one_less_does_not() -> None:
    lots = [
        blind_lot(1, tier=200, winner=1, price=150, true=140, rows=[1, 2]),  # gap 10
        blind_lot(2, tier=200, winner=2, price=149, true=140, rows=[1, 2]),  # gap 9
    ]
    assert winners(build_awards(lots, budget_span=SPAN), "winners_curse") == ([1], 10)
    assert award(build_awards(lots[1:], budget_span=SPAN), "winners_curse").vacant


def test_the_curse_is_the_greatest_single_gap_of_a_seat_not_their_sum() -> None:
    lots = [
        blind_lot(1, tier=100, winner=1, price=120, true=100, rows=[1, 2]),  # 20
        blind_lot(2, tier=100, winner=1, price=130, true=100, rows=[1, 2]),  # 30
        blind_lot(3, tier=100, winner=2, price=140, true=100, rows=[1, 2]),  # 40
    ]
    assert winners(build_awards(lots, budget_span=SPAN), "winners_curse") == ([2], 40)
    lots[2] = blind_lot(3, tier=100, winner=2, price=130, true=100, rows=[1, 2])
    assert winners(build_awards(lots, budget_span=SPAN), "winners_curse") == ([1, 2], 30)


def test_a_box_that_sold_for_less_than_its_value_or_not_at_all_is_no_curse() -> None:
    lots = [
        blind_lot(1, tier=100, winner=1, price=80, true=100, rows=[1, 2]),
        blind_lot(2, tier=100, winner=None, price=0, true=100, rows=[1, 2]),
        sold_lot(3, tier=100, winner=2, price=95, value=10, rows=[1, 2]),
    ]
    assert award(build_awards(lots, budget_span=SPAN), "winners_curse").vacant


def test_a_bot_that_overpaid_for_a_box_is_in_the_reference_list_only() -> None:
    lots = bots_in(
        [
            blind_lot(1, tier=100, winner=7, price=140, true=100, rows=[1, 7]),
            blind_lot(2, tier=100, winner=1, price=110, true=100, rows=[1, 7]),
        ],
        7,
    )
    result = build_awards(lots, budget_span=SPAN)
    assert winners(result, "winners_curse") == ([1], 10)
    bot = result.bot_award("winners_curse")
    assert (bot.winners, bot.metric) == ([7], 40)


# ---------------------------------------------------------------------------------- tilt


def test_tilt_adds_up_what_a_seat_paid_without_winning_over_all_all_pay_lots() -> None:
    lots = [
        all_pay_lot(1, tier=100, winner=3, bids={1: 6, 2: 9, 3: 70}),
        all_pay_lot(2, tier=100, winner=3, bids={1: 7, 2: 1, 3: 60}),
    ]
    assert winners(build_awards(lots, budget_span=SPAN), "tilt") == ([1], 13)
    assert award(build_awards(lots, budget_span=SPAN), "tilt").metric_unit == "coins"


def test_ten_coins_is_enough_and_nine_is_not() -> None:
    lots = [all_pay_lot(1, tier=100, winner=3, bids={1: 10, 2: 9, 3: 70})]
    assert winners(build_awards(lots, budget_span=SPAN), "tilt") == ([1], 10)
    lots = [all_pay_lot(1, tier=100, winner=3, bids={1: 9, 2: 9, 3: 70})]
    assert award(build_awards(lots, budget_span=SPAN), "tilt").vacant


def test_what_a_winner_pays_is_not_a_loss() -> None:
    lots = [all_pay_lot(1, tier=100, winner=3, bids={1: 4, 3: 500})]
    assert award(build_awards(lots, budget_span=SPAN), "tilt").vacant


def test_seats_that_lost_the_same_amount_win_tilt_together() -> None:
    lots = [all_pay_lot(1, tier=100, winner=3, bids={1: 30, 2: 30, 3: 90})]
    assert winners(build_awards(lots, budget_span=SPAN), "tilt") == ([1, 2], 30)


def test_other_formats_never_make_a_tilt() -> None:
    lot = sold_lot(1, tier=100, winner=3, price=50, value=90, rows=[1, 2, 3])
    lot = lot.model_copy(update={"payments": {1: 30, 3: 50}})
    assert award(build_awards([lot], budget_span=SPAN), "tilt").vacant


def test_the_bots_are_ranked_on_tilt_apart_from_the_models() -> None:
    lots = bots_in([all_pay_lot(1, tier=100, winner=3, bids={1: 12, 7: 52, 3: 90})], 7)
    result = build_awards(lots, budget_span=SPAN)
    assert winners(result, "tilt") == ([1], 12)
    assert (result.bot_award("tilt").winners, result.bot_award("tilt").metric) == ([7], 52)


# ------------------------------------------------------------------------------- bargain


def test_a_gain_of_a_fifth_of_the_tier_qualifies_and_one_less_does_not() -> None:
    qualifies = sold_lot(1, tier=200, winner=1, price=60, value=100, rows=[1, 2])  # 40
    misses = sold_lot(2, tier=200, winner=2, price=61, value=100, rows=[1, 2])  # 39
    assert winners(build_awards([qualifies, misses], budget_span=SPAN), "bargain") == ([1], 40)
    assert award(build_awards([misses], budget_span=SPAN), "bargain").vacant


def test_a_box_is_a_bargain_when_it_is_worth_more_than_the_winner_paid() -> None:
    lots = [blind_lot(1, tier=200, winner=2, price=100, true=150, rows=[1, 2])]
    assert winners(build_awards(lots, budget_span=SPAN), "bargain") == ([2], 50)


def test_the_bargain_is_the_best_single_lot_of_a_seat_and_ties_win_together() -> None:
    lots = [
        sold_lot(1, tier=100, winner=1, price=10, value=50, rows=[1, 2]),  # 40
        sold_lot(2, tier=100, winner=1, price=10, value=70, rows=[1, 2]),  # 60
        sold_lot(3, tier=100, winner=2, price=20, value=80, rows=[1, 2]),  # 60
    ]
    assert winners(build_awards(lots, budget_span=SPAN), "bargain") == ([1, 2], 60)


def test_the_winner_of_an_all_pay_lot_can_have_a_bargain() -> None:
    lot = all_pay_lot(1, tier=100, winner=1, bids={1: 20, 2: 15})
    lot = lot.model_copy(
        update={
            "seats": tuple(
                line.model_copy(update={"value": 90 if line.seat == 1 else 10})
                for line in lot.seats
            )
        }
    )
    assert winners(build_awards([lot], budget_span=SPAN), "bargain") == ([1], 70)


def test_an_unsold_lot_is_no_bargain_and_the_bots_have_a_list_of_their_own() -> None:
    unsold = make_lot(seats=[seat_line(1, value=90)], winner=None)
    assert award(build_awards([unsold], budget_span=SPAN), "bargain").vacant
    lots = bots_in([sold_lot(2, tier=100, winner=7, price=10, value=90, rows=[1, 7])], 7)
    result = build_awards(lots, budget_span=SPAN)
    assert award(result, "bargain").vacant
    assert (result.bot_award("bargain").winners, result.bot_award("bargain").metric) == ([7], 80)


# ----------------------------------------------------------------------- pledges, claims


def test_the_backstabber_has_broken_the_most_pledges_and_kept_ones_do_not_count() -> None:
    pledges = [
        pledge_record(1, "violated"),
        pledge_record(1, "violated"),
        pledge_record(1, "kept"),
        pledge_record(2, "violated"),
        pledge_record(2, "kept"),
        pledge_record(2, "void"),
    ]
    result = build_awards(table(1, 2, 3, 4, 5), pledges=pledges, budget_span=SPAN)
    assert winners(result, "backstabber") == ([1], 2)
    assert award(result, "backstabber").metric_unit == "count"


def test_one_broken_pledge_is_enough_and_equal_counts_tie() -> None:
    result = build_awards(table(3, 4), pledges=[pledge_record(4, "violated")], budget_span=SPAN)
    assert winners(result, "backstabber") == ([4], 1)
    pledges = [pledge_record(3, "violated"), pledge_record(4, "violated")]
    assert winners(build_awards(table(3, 4), pledges=pledges, budget_span=SPAN), "backstabber") == (
        [3, 4],
        1,
    )
    kept = [pledge_record(3, "kept"), pledge_record(4, "void")]
    assert award(build_awards(table(3, 4), pledges=kept, budget_span=SPAN), "backstabber").vacant


def test_the_best_actor_has_the_most_false_claims_that_both_judges_found() -> None:
    verdicts = [false_claim(2), false_claim(2, 5), false_claim(3)]
    result = build_awards(table(2, 3), verdicts=verdicts, budget_span=SPAN)
    assert winners(result, "best_actor") == ([2], 2)
    assert award(result, "best_actor").metric_unit == "count"


def test_only_false_claims_make_a_best_actor() -> None:
    others = [
        JudgeVerdictData(verdict=kind, seats=[2, 3], lots=[1])  # type: ignore[arg-type]
        for kind in ("pledge_violation", "collusion_red", "collusion_suspect")
    ]
    result = build_awards(table(2, 3), verdicts=others, budget_span=SPAN)
    assert award(result, "best_actor").vacant
    tied = build_awards(table(2, 3), verdicts=[false_claim(2), false_claim(3)], budget_span=SPAN)
    assert winners(tied, "best_actor") == ([2, 3], 1)


def test_a_claim_of_a_bot_or_of_an_unknown_seat_wins_nothing() -> None:
    lots = [make_lot(seats=[seat_line(1), seat_line(7, role="bot")])]
    result = build_awards(
        lots, verdicts=[false_claim(7), false_claim(7), false_claim(5)], budget_span=SPAN
    )
    assert award(result, "best_actor").vacant


def test_the_keeper_kept_at_least_two_pledges_and_broke_none() -> None:
    pledges = [
        *(pledge_record(1, "kept") for _ in range(3)),  # three kept
        *(pledge_record(2, "kept") for _ in range(2)),  # two kept
        *(pledge_record(3, "kept") for _ in range(2)),  # two kept, one broken
        pledge_record(3, "violated"),
        pledge_record(4, "kept"),  # only one
        *(pledge_record(5, "kept") for _ in range(2)),  # two kept, three void
        *(pledge_record(5, "void") for _ in range(3)),
    ]
    result = build_awards(table(1, 2, 3, 4, 5), pledges=pledges, budget_span=SPAN)
    assert winners(result, "keeper") == ([1], 3)
    assert award(result, "keeper").metric_unit == "count"


def test_keepers_with_the_same_number_of_kept_pledges_win_together() -> None:
    pledges = [pledge_record(s, "kept") for s in (1, 1, 2, 2, 3)]
    assert winners(build_awards(table(1, 2, 3), pledges=pledges, budget_span=SPAN), "keeper") == (
        [1, 2],
        2,
    )


def test_void_pledges_count_neither_for_nor_against() -> None:
    pledges = [pledge_record(1, "kept"), pledge_record(1, "void"), pledge_record(1, "void")]
    assert award(build_awards(table(1), pledges=pledges, budget_span=SPAN), "keeper").vacant
    pledges = [pledge_record(1, "kept"), pledge_record(1, "kept"), pledge_record(1, "void")]
    assert winners(build_awards(table(1), pledges=pledges, budget_span=SPAN), "keeper") == ([1], 2)


def test_a_seat_that_never_pledged_wins_neither_pledge_award() -> None:
    result = build_awards([make_lot(seats=[seat_line(1), seat_line(2)])], budget_span=SPAN)
    assert award(result, "keeper").vacant and award(result, "backstabber").vacant


# ------------------------------------------------------------------------ mission master


def test_the_mission_master_is_the_seat_that_finished_first() -> None:
    lots = [make_lot(seats=[seat_line(n) for n in (1, 2, 3)])]
    missions = [
        collector(1, [3, 5, 9]),  # the second tag lot it won is lot 5
        shill(2, 7),  # decided at the hammer of lot 7
        collector(3, [2], done=False),
    ]
    result = build_awards(lots, missions=missions, budget_span=SPAN)
    assert winners(result, "mission_master") == ([1], None)
    assert award(result, "mission_master").metric_unit == "none"
    assert build_awards(lots, missions=missions[1:], budget_span=SPAN).award(
        "mission_master"
    ).winners == [2]


def test_missions_finished_on_the_same_lot_tie() -> None:
    lots = [make_lot(seats=[seat_line(n) for n in (1, 2, 3)])]
    missions = [collector(1, [2, 6]), shill(2, 6), collector(3, [1, 8])]
    result = build_awards(lots, missions=missions, budget_span=SPAN)
    assert result.award("mission_master").winners == [1, 2]


def test_a_collector_finishes_with_the_lot_that_brings_it_to_its_need() -> None:
    lots = [make_lot(seats=[seat_line(1), seat_line(2)])]
    one_tag = collector(1, [9, 4], need=1)  # needs one: lot 4
    two_tags = collector(2, [3, 9], need=2)  # needs two: lot 9
    result = build_awards(lots, missions=[two_tags, one_tag], budget_span=SPAN)
    assert result.award("mission_master").winners == [1]


def test_no_finished_mission_leaves_the_award_vacant() -> None:
    lots = [make_lot(seats=[seat_line(1)])]
    result = build_awards(lots, missions=[collector(1, [2], done=False)], budget_span=SPAN)
    assert award(result, "mission_master").vacant


# ------------------------------------------------------------------------- mind reader


def reading_lots(errors: dict[int, list[int]], tier: int = 100) -> list[Any]:
    """Seat -> the absolute errors of its guesses, one per lot."""
    count = max(len(found) for found in errors.values())
    lots = []
    for index in range(count):
        lines = []
        for seat, found in errors.items():
            guess = None
            if index < len(found):
                guess = GuessLine(
                    target=8, guess=100, truth=100 + found[index], abs_error=found[index]
                )
            lines.append(seat_line(seat, guess=guess))
        lots.append(make_lot(lot=index + 1, tier=tier, seats=lines))
    return lots


def test_the_mind_reader_has_the_smallest_mean_error_in_budget_spans() -> None:
    lots = reading_lots({1: [30] * 8, 2: [60] * 8, 3: [90] * 8})
    result = build_awards(lots, budget_span=SPAN)
    assert [m.mind_error_milli for m in result.metrics.values()] == [100, 200, 300]
    assert winners(result, "mind_reader") == ([1], 100)
    assert award(result, "mind_reader").metric_unit == "milli"


def test_eight_valid_guesses_are_needed_and_seven_are_not_enough() -> None:
    lots = reading_lots({1: [0] * 7, 2: [150] * 8})
    result = build_awards(lots, budget_span=SPAN)
    assert result.metrics[1].valid_guesses == 7 and result.metrics[1].mind_error_milli == 0
    assert winners(result, "mind_reader") == ([2], 500)
    assert award(build_awards(lots[:7], budget_span=SPAN), "mind_reader").vacant


def test_the_error_is_a_mean_over_all_guesses_and_is_rounded_down() -> None:
    errors = [1, 0, 0, 0, 0, 0, 0, 0]
    result = build_awards(reading_lots({1: errors}), budget_span=SPAN)
    assert result.metrics[1].mind_error_milli == 0  # 1000 / (8 * 300) is 0.42
    errors = [100, 0, 0, 0, 0, 0, 0, 0]
    result = build_awards(reading_lots({1: errors}), budget_span=SPAN)
    assert result.metrics[1].mind_error_milli == 41  # 100000 / 2400 is 41.67


def test_mind_readers_with_the_same_figure_win_together() -> None:
    result = build_awards(reading_lots({1: [30] * 8, 2: [30] * 8, 3: [90] * 8}), budget_span=SPAN)
    assert winners(result, "mind_reader") == ([1, 2], 100)


def test_a_budget_span_of_zero_is_taken_as_one() -> None:
    result = build_awards(reading_lots({1: [3] * 8}), budget_span=0)
    assert result.metrics[1].mind_error_milli == 3000


# --------------------------------------------------------------------------- consistent


def plan_lots(rows: list[dict[int, tuple[int | None, int, int, int]]], **extra: Any) -> list[Any]:
    """One lot per entry; a seat is (stars, played, value, remaining budget)."""
    lots = []
    for index, row in enumerate(rows, start=1):
        lines = []
        for seat, (stars, played, value, budget) in row.items():
            line = seat_line(
                seat,
                played=played,
                value=value,
                plan_stars=stars,
                abstained=played is None,
            )
            lines.append(line.model_copy(update={"budget_before": budget}))
        lots.append(make_lot(lot=index, tier=200, seats=lines, **extra))
    return lots


def test_a_high_plan_is_followed_by_a_bid_of_half_the_value_or_more() -> None:
    cases = [
        (3, 50, 100, True),  # exactly half is serious
        (2, 49, 100, False),
        (0, 49, 100, True),  # a low plan is followed by a small bid
        (1, 50, 100, False),
        (0, 0, 100, True),
        (3, 0, 100, False),
    ]
    for stars, played, value, followed in cases:
        lot = plan_lots([{1: (stars, played, value, 300)}])
        metrics = build_awards(lot, budget_span=SPAN).metrics[1]
        assert (metrics.judgable_lots, metrics.consistent_lots) == (1, int(followed)), (
            stars,
            played,
        )


def test_the_share_of_consistent_lots_is_in_thousandths_rounded_down() -> None:
    rows = [{1: (3, 90, 100, 300)}, {1: (3, 90, 100, 300)}, {1: (3, 1, 100, 300)}]
    metrics = build_awards(plan_lots(rows), budget_span=SPAN).metrics[1]
    assert (metrics.judgable_lots, metrics.consistent_lots, metrics.consistency_milli) == (
        3,
        2,
        666,
    )


def test_a_lot_the_seat_could_not_afford_a_serious_bid_in_is_not_judged() -> None:
    afford = plan_lots([{1: (3, 0, 100, 50)}])  # budget 50 buys the serious bid of 50
    broke = plan_lots([{1: (3, 0, 100, 49)}])
    assert build_awards(afford, budget_span=SPAN).metrics[1].judgable_lots == 1
    assert build_awards(broke, budget_span=SPAN).metrics[1].judgable_lots == 0
    odd = plan_lots([{1: (3, 0, 99, 50)}])  # the serious bid is 50, since 2 * 50 >= 99
    assert build_awards(odd, budget_span=SPAN).metrics[1].judgable_lots == 1


def test_lots_without_a_plan_a_bid_or_a_value_are_not_judged() -> None:
    rows = [
        {1: (None, 50, 100, 300)},  # no plan: handled below by dropping plan_stars
        {1: (3, None, 100, 300)},  # abstained
        {1: (3, 50, 0, 300)},  # a value of zero
    ]
    lots = plan_lots(rows)
    lots[0] = lots[0].model_copy(
        update={"seats": (lots[0].seats[0].model_copy(update={"plan_stars": None}),)}
    )
    metrics = build_awards(lots, budget_span=SPAN).metrics[1]
    assert metrics.judgable_lots == 0 and metrics.consistency_milli is None
    unasked = make_lot(seats=[seat_line(1, asked=False, plan_stars=3, value=100)])
    assert build_awards([unasked], budget_span=SPAN).metrics[1].judgable_lots == 0


def test_a_blind_box_is_judged_by_the_appraisal_of_the_seat() -> None:
    line = seat_line(1, played=40, plan_stars=3, signal=80)
    box = make_lot(fmt="blind_box", seats=[line.model_copy(update={"budget_before": 300})])
    metrics = build_awards([box], budget_span=SPAN).metrics[1]
    assert (metrics.judgable_lots, metrics.consistent_lots) == (1, 1)  # 40 is half of 80


def ten_lots(consistent: dict[int, int]) -> list[Any]:
    """Ten lots; the seat is consistent in the first ``consistent[seat]`` of them."""
    rows = []
    for index in range(10):
        row = {}
        for seat, count in consistent.items():
            row[seat] = (3, 90 if index < count else 1, 100, 300)
        rows.append(row)
    return plan_lots(rows)


def test_ten_judgable_lots_are_needed_and_nine_are_not_enough() -> None:
    result = build_awards(ten_lots({1: 4, 2: 9}), budget_span=SPAN)
    assert winners(result, "consistent") == ([2], 900)
    assert award(result, "consistent").metric_unit == "milli"
    assert award(build_awards(ten_lots({1: 10})[:9], budget_span=SPAN), "consistent").vacant


def test_seats_with_the_same_share_win_the_consistency_award_together() -> None:
    result = build_awards(ten_lots({1: 7, 2: 7, 3: 2}), budget_span=SPAN)
    assert winners(result, "consistent") == ([1, 2], 700)


# ------------------------------------------------------------------------ the reference columns


def test_the_reference_columns_fill_the_fields_of_the_leaderboard_stats() -> None:
    lots = reading_lots({1: [30] * 8})
    update = build_awards(lots, budget_span=SPAN).metrics[1].stats_update()
    assert set(update) <= set(SeatStats.model_fields)
    stats = SeatStats().model_copy(update=update)
    assert stats.valid_guesses == 8 and stats.mind_error_milli == 100
    assert SeatStats.model_validate(stats.model_dump()) == stats


def test_a_seat_with_nothing_to_average_has_empty_figures() -> None:
    metrics = build_awards([make_lot(seats=[seat_line(1)])], budget_span=SPAN).metrics[1]
    assert metrics.textbook_deviation_milli is None
    assert metrics.mind_error_milli is None
    assert metrics.consistency_milli is None


def test_the_award_events_are_the_ten_model_awards_for_the_audience() -> None:
    result = build_awards(table(1), pledges=[pledge_record(1, "violated")], budget_span=SPAN)
    events = result.events()
    assert [e.seq for e in events] == list(range(1, 11))
    assert [e.data.award for e in events] == list(AWARD_KEYS)
    assert all(e.vis == "audience" and e.lot is None and e.type == "award" for e in events)


# ---------------------------------------------------------------------- the viewer's tables


def test_the_units_follow_the_viewer() -> None:
    table = viewer_awards()
    assert list(table) == list(AWARD_KEYS)
    for key in AWARD_KEYS:
        shown = table[key]["unit"]
        assert (shown or "none") == AWARD_UNITS[key], key


def test_the_lower_limits_follow_the_viewer() -> None:
    import re

    table = viewer_awards()

    def first_number(text: str | None) -> int:
        assert text is not None
        return int(re.search(r"\d+", text).group())  # type: ignore[union-attr]

    assert first_number(table["most_textbook"]["floor"]) == prizes.MIN_TEXTBOOK_BIDS
    assert first_number(table["winners_curse"]["floor"]) == 100 // prizes.CURSE_TIER_DIVISOR
    assert first_number(table["tilt"]["floor"]) == prizes.MIN_TILT_COINS
    assert first_number(table["bargain"]["floor"]) == 100 // prizes.BARGAIN_TIER_DIVISOR
    assert first_number(table["backstabber"]["floor"]) == 1
    assert first_number(table["best_actor"]["floor"]) == 1
    assert first_number(table["mind_reader"]["floor"]) == prizes.MIN_VALID_GUESSES
    assert first_number(table["consistent"]["floor"]) == prizes.MIN_JUDGABLE_LOTS
    assert table["keeper"]["floor"] is None and table["mission_master"]["floor"] is None
    assert first_number(table["keeper"]["rule"]) == prizes.MIN_KEPT_PLEDGES


def test_what_each_award_rests_on_follows_the_viewer() -> None:
    assert viewer_basis("awards.js") == dict(AWARD_BASIS)


def test_the_basis_in_the_replay_is_the_one_the_viewer_holds_back_by() -> None:
    _, cards = mock_night("d8-awards-1")
    result = compute_awards(cards)
    viewer = viewer_basis("awards.js")
    assert [entry.basis for entry in (*result.models, *result.bots)] == [
        viewer[entry.award] for entry in (*result.models, *result.bots)
    ]
    for event in result.events():
        assert event.data.basis == viewer[event.data.award]


# ============================================================================ real nights


SEEDS = ["d8-awards-1", "d8-awards-2", "d8-awards-3", "d8-awards-4"]


@pytest.fixture(scope="module", params=SEEDS)
def night(request: pytest.FixtureRequest) -> Any:
    state, cards = mock_night(request.param)
    return state, cards, LogOracle(state)


def as_pair(entry: AwardData) -> tuple[list[int], int | None]:
    return entry.winners, entry.metric


def test_the_awards_of_a_mock_night_are_what_the_log_says(night: Any) -> None:
    state, cards, oracle = night
    result = compute_awards(cards)
    expected = oracle.awards("player")
    assert [entry.award for entry in result.models] == list(AWARD_KEYS)
    for entry in result.models:
        assert as_pair(entry) == expected[entry.award], entry.award
        assert entry.vacant is (not entry.winners)


def test_the_bots_reference_of_a_mock_night_is_what_the_log_says(night: Any) -> None:
    _, cards, oracle = night
    result = compute_awards(cards)
    expected = oracle.awards("bot")
    assert [entry.award for entry in result.bots] == list(BOT_AWARDS)
    for entry in result.bots:
        assert as_pair(entry) == expected[entry.award], entry.award


def test_only_model_seats_win_the_awards_and_only_bots_the_reference(night: Any) -> None:
    state, cards, oracle = night
    result = compute_awards(cards)
    for entry in result.models:
        assert set(entry.winners) <= oracle.group("player")
    for entry in result.bots:
        assert set(entry.winners) <= oracle.group("bot")


def test_the_reference_columns_of_a_mock_night_are_what_the_log_says(night: Any) -> None:
    _, cards, oracle = night
    metrics = compute_awards(cards).metrics
    assert sorted(metrics) == sorted(oracle.role)
    textbook, mind, plan = (
        oracle.textbook(set(oracle.role)),
        oracle.mind(set(oracle.role)),
        oracle.consistency(set(oracle.role)),
    )
    for seat, m in metrics.items():
        assert m.role == oracle.role[seat]
        assert (m.textbook_bids, m.textbook_deviation_milli) == textbook.get(seat, (0, None))
        assert (m.valid_guesses, m.mind_error_milli) == mind.get(seat, (0, None))
        assert (m.judgable_lots, m.consistency_milli) == plan.get(seat, (0, None))


def test_verdicts_decide_the_best_actor_and_nothing_else(night: Any) -> None:
    state, cards, oracle = night
    verdicts = [false_claim(2, 1), false_claim(2, 4), false_claim(5, 4), false_claim(7, 4)]
    with_claims = compute_awards(cards, verdicts)
    without = compute_awards(cards)
    assert as_pair(with_claims.award("best_actor")) == ([2], 2)
    assert (
        as_pair(with_claims.award("best_actor")) == oracle.awards("player", verdicts)["best_actor"]
    )
    for key in AWARD_KEYS:
        if key != "best_actor":
            assert as_pair(with_claims.award(key)) == as_pair(without.award(key))
    assert as_pair(with_claims.award("best_actor")) != as_pair(without.award("best_actor"))
    assert as_pair(with_claims.bot_award("tilt")) == as_pair(without.bot_award("tilt"))


def test_the_mock_policies_show_in_the_awards() -> None:
    """Policies of the pinned mock night: seats 2 and 4 collude (they pledge to let each other
    win and keep it), seat 5 backstabs (it pledges not to bid and then bids)."""
    state, cards = mock_night("d8-awards-1")
    result = compute_awards(cards)
    assert award(result, "backstabber").winners == [5]
    assert award(result, "keeper").winners == [2, 4]
    assert award(result, "best_actor").vacant  # no judge spoke
    assert all(entry.award not in ("backstabber", "keeper", "best_actor") for entry in result.bots)


def test_the_awards_of_a_night_hold_whole_numbers_and_nothing_else(night: Any) -> None:
    _, cards, _ = night
    result = compute_awards(cards)

    def refuse(text: str) -> float:
        raise AssertionError(f"a decimal number in the awards: {text}")

    json.loads(canonical_json([*result.models, *result.bots]), parse_float=refuse)
    assert all(
        isinstance(value, int)
        for m in result.metrics.values()
        for value in m.stats_update().values()
        if value is not None
    )


# ------------------------------------------------------------------------- determinism


def test_the_same_night_played_again_gives_the_same_bytes() -> None:
    state, cards = mock_night("d8-awards-1")
    again_state, again = play_mock_night("d8-awards-1")
    assert again_state is not state
    first, second = compute_awards(cards), compute_awards(again)
    assert canonical_json([*first.models, *first.bots]) == canonical_json(
        [*second.models, *second.bots]
    )
    assert first.metrics == second.metrics
    assert canonical_json(compute_awards(cards).models) == canonical_json(first.models)


def test_the_input_order_of_pledges_lots_and_verdicts_does_not_matter() -> None:
    state, cards = mock_night("d8-awards-1")
    data = cards.night.data
    verdicts = [false_claim(2, 1), false_claim(2, 4), false_claim(3, 6)]
    base = build_awards(
        cards.lot_data,
        pledges=data.pledges,
        missions=data.missions,
        verdicts=verdicts,
        budget_span=data.budget_hi - data.budget_lo,
    )
    shuffled = build_awards(
        reversed(cards.lot_data),
        pledges=list(reversed(data.pledges)),
        missions=list(reversed(data.missions)),
        verdicts=list(reversed(verdicts)),
        budget_span=data.budget_hi - data.budget_lo,
    )
    assert canonical_json([*base.models, *base.bots]) == canonical_json(
        [*shuffled.models, *shuffled.bots]
    )
    assert base.metrics == shuffled.metrics


DIGEST_SCRIPT = """
import sys
sys.path[:0] = sys.argv[1:4]
from prize_support import mock_night
from auction_night.contracts.events import canonical_json
from auction_night.show.awards import compute_awards
from auction_night.show.highlights import compute_highlights

state, cards = mock_night("d8-awards-1")
awards = compute_awards(cards)
print(canonical_json([*awards.models, *awards.bots]))
print(canonical_json(compute_highlights(cards, state.events)))
"""


def test_the_bytes_do_not_depend_on_the_hash_seed_of_the_process() -> None:
    tests = Path(__file__).resolve().parents[1]
    outputs = set()
    for seed in ("1", "2", "31337"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "CUDA_VISIBLE_DEVICES": ""}
        done = subprocess.run(
            [
                sys.executable,
                "-c",
                DIGEST_SCRIPT,
                str(tests / "engine"),
                str(tests / "night"),
                str(tests / "show"),
            ],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        outputs.add(done.stdout)
    assert len(outputs) == 1
    state, cards = mock_night("d8-awards-1")
    awards = compute_awards(cards)
    first_line = canonical_json([*awards.models, *awards.bots])
    assert next(iter(outputs)).splitlines()[0] == first_line


# ===================================================================== scripted nights


def test_scripted_tilt_is_the_coins_paid_for_nothing_added_up_over_the_lots() -> None:
    night = awards_night()
    result = compute_awards(night.cards)
    assert as_pair(result.award("tilt")) == ([1], 70)  # 40 + 30; seat 3 lost only 15


def test_scripted_curse_is_the_price_above_the_true_value_of_the_box() -> None:
    night = awards_night()
    first, second = night.lots["blind_box"]
    gaps = {
        5: 5 * night.tier(first) // 2 - night.deal.lot(first).blind_value,
        3: 7 * night.tier(second) // 4 - night.deal.lot(second).blind_value,
    }
    assert gaps[5] * 20 >= night.tier(first) and gaps[3] * 20 >= night.tier(second)
    best = max(gaps.values())
    expected = (sorted(seat for seat, gap in gaps.items() if gap == best), best)
    assert as_pair(compute_awards(night.cards).award("winners_curse")) == expected


def test_scripted_bargain_is_the_value_less_the_price_of_the_best_lot() -> None:
    night = awards_night()
    lot = night.lots["sealed_first"][0]
    value = night.deal.lot(lot).values[1]
    assert (value - 1) * 5 >= night.tier(lot)
    assert as_pair(compute_awards(night.cards).award("bargain")) == ([1], value - 1)


def test_scripted_pledges_decide_the_backstabber_and_the_keeper() -> None:
    night = awards_night()
    result = compute_awards(night.cards)
    outcomes = {(p.seat, p.pledge.lot): p.outcome for p in night.cards.night.data.pledges}
    ap, other = night.lots["all_pay"][0], night.notes["other"]
    assert outcomes == {
        (2, ap): "kept",  # a cap of 60 and a bid of 60
        (3, ap): "violated",  # no bid, and a bid of 15
        (2, other): "kept",  # a cap of 70 and a bid of 70
        (4, other): "violated",  # a cap of 50 and a bid of 55
    }
    assert as_pair(result.award("backstabber")) == ([3, 4], 1)
    assert as_pair(result.award("keeper")) == ([2], 2)


def test_scripted_mission_is_won_by_the_one_that_finished_it() -> None:
    night = awards_night()
    done = [m.seat for m in night.state.missions if m.completed]
    assert done == [night.notes["shill_seat"]]
    result = compute_awards(night.cards)
    assert as_pair(result.award("mission_master")) == (done, None)


def test_scripted_night_agrees_with_the_oracle() -> None:
    night = awards_night()
    result = compute_awards(night.cards)
    expected = LogOracle(night.state).awards("player")
    for entry in result.models:
        assert as_pair(entry) == expected[entry.award], entry.award
    assert result.bots == ()  # nobody sits as a bot at this table


def test_scripted_columns_are_the_oracles_too() -> None:
    night = awards_night()
    oracle = LogOracle(night.state)
    seats = set(oracle.role)
    metrics = compute_awards(night.cards).metrics
    for seat, m in metrics.items():
        assert (m.textbook_bids, m.textbook_deviation_milli) == oracle.textbook(seats)[seat]
        assert (m.valid_guesses, m.mind_error_milli) == oracle.mind(seats)[seat]
        assert (m.judgable_lots, m.consistency_milli) == oracle.consistency(seats)[seat]


# ------------------------------------------------------------------------ the log route


def test_the_awards_compose_with_the_log_through_append_events() -> None:
    state, cards = mock_night("d8-awards-1")
    result = compute_awards(cards)
    longer, added = append_events(state, result.events())
    assert [e.seq for e in added] == list(range(len(state.events) + 1, len(state.events) + 11))
    assert len(longer.events) == len(state.events) + 10
    assert [e.data for e in added] == list(result.models)


def test_a_replay_takes_the_awards_as_they_are() -> None:
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "replays" / "sealed_four_lots.json"
    raw = json.loads(fixture.read_text(encoding="utf-8"))
    _, cards = mock_night("d8-awards-1")
    result = compute_awards(cards)
    raw["awards"] = [entry.model_dump(mode="json") for entry in result.models]
    raw["bots_reference"] = [entry.model_dump(mode="json") for entry in result.bots]
    replay = Replay.model_validate(raw)
    assert [entry.award for entry in replay.awards] == list(AWARD_KEYS)
    assert replay.awards == list(result.models) and replay.bots_reference == list(result.bots)
