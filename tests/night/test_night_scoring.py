"""Scores: surplus, mission bonus and total, and the conservation of the books."""

from dataclasses import dataclass, field

import pytest
from engine_support import seeds
from hypothesis import given, settings
from night_support import make_config, play_seed

from auction_night.contracts.events import AnyEvent, HammerData, SeatScore
from auction_night.engine.deal import LotDeal, deal
from auction_night.engine.scoring import (
    SeatTally,
    conservation_gap,
    rank_totals,
    seat_scores,
    seller_revenue,
    tally_night,
    value_sold,
)

CONFIG = make_config()
SEATS = tuple(range(1, 9))
MODEL_SEATS = tuple(range(1, 7))


# ------------------------------------------------------------------- hand-built hammers


def sold(fmt: str, winner: int, price: int, payments: dict[int, int], **extra: int) -> HammerData:
    return HammerData(format=fmt, winner=winner, price=price, payments=payments, **extra)  # type: ignore[arg-type]


def unsold(fmt: str) -> HammerData:
    return HammerData(format=fmt, unsold=True)  # type: ignore[arg-type]


def hammers_for(lots: tuple[LotDeal, ...], decided: dict[int, HammerData]) -> list[HammerData]:
    """One hammer per lot up to the last decided one; lots in between are unsold."""
    return [decided.get(lot.lot, unsold(lot.format)) for lot in lots[: max(decided)]]


def test_surplus_is_value_won_minus_everything_paid_all_pay_losses_included() -> None:
    lots = deal(CONFIG, seed="score-hand").lots
    ordinary = [lot for lot in lots if not lot.is_blind]
    blind = next(lot for lot in lots if lot.is_blind)
    first, second, other = ordinary[0], ordinary[1], ordinary[2]
    hammers = hammers_for(
        lots,
        {
            first.lot: sold("sealed_first", 2, 70, {2: 70}),
            second.lot: sold("all_pay", 3, 50, {1: 30, 2: 20, 3: 50}),  # the losers pay too
            blind.lot: sold("blind_box", 4, 80, {4: 80}, true_value=blind.blind_value),
            other.lot: unsold("english"),  # nobody pays
        },
    )
    assert blind.blind_value is not None
    tally = tally_night(lots, hammers, SEATS)
    assert tally[1] == SeatTally(1, 0, 30, ())
    assert tally[2] == SeatTally(2, first.values[2], 90, (first.lot,))
    assert tally[3] == SeatTally(3, second.values[3], 50, (second.lot,))
    assert tally[4] == SeatTally(4, blind.blind_value, 80, (blind.lot,))  # the hidden value
    assert all(tally[s] == SeatTally(s, 0, 0, ()) for s in (5, 6, 7, 8))
    assert (tally[1].surplus, tally[4].surplus) == (-30, blind.blind_value - 80)
    assert seller_revenue(hammers) == 70 + 100 + 80
    assert value_sold(lots, hammers) == first.values[2] + second.values[3] + blind.blind_value


def test_unplayed_lots_are_simply_not_counted() -> None:
    lots = deal(CONFIG, seed="score-hand").lots
    assert tally_night(lots, [], SEATS)[3] == SeatTally(3, 0, 0, ())
    with pytest.raises(ValueError, match="more hammers"):
        tally_night(lots[:1], [sold("sealed_first", 1, 1, {1: 1})] * 2, SEATS)


def test_ranks_share_a_place_on_equal_totals_and_skip_the_next() -> None:
    assert rank_totals({1: 50, 2: 50, 3: 20, 4: -5, 5: 20, 6: 90}) == {
        6: 1,
        1: 2,
        2: 2,
        3: 4,
        5: 4,
        4: 6,
    }


def tallies(surplus: dict[int, int]) -> dict[int, SeatTally]:
    return {seat: SeatTally(seat, max(v, 0), max(-v, 0), ()) for seat, v in surplus.items()}


def test_the_bonus_stays_apart_and_bots_are_scored_but_not_ranked() -> None:
    surplus = {1: 40, 2: 100, 3: -20, 4: 40, 5: 0, 6: 10, 7: 500, 8: -50}
    scores = seat_scores(tallies(surplus), {1: 150, 3: 150}, ranked=MODEL_SEATS)
    by_seat = {s.seat: s for s in scores}
    assert [s.seat for s in scores] == list(SEATS)
    assert [by_seat[s].surplus for s in SEATS] == [surplus[s] for s in SEATS]
    assert [by_seat[s].mission_bonus for s in SEATS] == [150, 0, 150, 0, 0, 0, 0, 0]
    assert all(by_seat[s].total == by_seat[s].surplus + by_seat[s].mission_bonus for s in SEATS)
    # totals 190, 100, 130, 40, 0, 10: the bots' 500 and -50 do not move anybody
    assert [by_seat[s].rank_tonight for s in MODEL_SEATS] == [1, 3, 2, 4, 6, 5]
    assert by_seat[7].rank_tonight is None and by_seat[8].rank_tonight is None
    assert all(isinstance(s, SeatScore) for s in scores)


def test_a_tie_for_first_gives_two_first_places() -> None:
    scores = seat_scores(tallies({1: 70, 2: 70, 3: 10}), {}, ranked={1, 2, 3})
    assert [s.rank_tonight for s in scores] == [1, 1, 3]


# -------------------------------------------------------------- whole nights, from the log


@dataclass
class Book:
    """One seat's books, kept from the events alone."""

    worth: int = 0
    paid: int = 0
    bonus: int = 0
    won: list[int] = field(default_factory=list)


def books_from_the_log(events: tuple[AnyEvent, ...]) -> dict[int, Book]:
    """Recompute every seat's score from the events alone, with a different bookkeeping."""
    own_values = {e.data.seat: e.data.values for e in events if e.type == "deal_private"}
    books = {seat: Book() for seat in own_values}
    for event in events:
        if event.type == "hammer":
            data = event.data
            for seat, amount in data.payments.items():
                books[seat].paid += amount
            if data.winner is not None:
                blind = data.format == "blind_box"
                worth = data.true_value if blind else own_values[data.winner][event.lot]
                assert worth is not None
                books[data.winner].worth += worth
                books[data.winner].won.append(event.lot)
        elif event.type == "mission_result":
            books[event.data.seat].bonus = event.data.bonus
    return books


@given(seeds)
@settings(max_examples=30, deadline=None)
def test_the_night_end_scores_equal_an_independent_recount(seed: str) -> None:
    transcript = play_seed(CONFIG, seed)
    books = books_from_the_log(transcript.events)
    end = transcript.events[-1]
    assert end.type == "night_end" and end.data.status == "completed"
    assert [s.seat for s in end.data.scores] == list(SEATS)
    for score in end.data.scores:
        mine = books[score.seat]
        assert score.surplus == mine.worth - mine.paid
        assert score.mission_bonus == mine.bonus
        assert score.total == score.surplus + mine.bonus
        assert score.spent == mine.paid
        assert score.lots_won == sorted(mine.won)
    assert list(transcript.state.scores) == list(end.data.scores)


@given(seeds)
@settings(max_examples=30, deadline=None)
def test_the_books_balance_what_seats_win_in_value_they_pay_the_seller(seed: str) -> None:
    transcript = play_seed(CONFIG, seed)
    scores = transcript.events[-1].data.scores
    books = books_from_the_log(transcript.events)
    value_won = sum(book.worth for book in books.values())
    revenue = sum(sum(e.data.payments.values()) for e in transcript.events if e.type == "hammer")
    bonuses = sum(s.mission_bonus for s in scores)
    assert sum(s.total for s in scores) == value_won - revenue + bonuses
    assert conservation_gap(scores, transcript.state.deal.lots, transcript.state.hammers) == 0
    assert revenue == sum(s.spent for s in scores)
    assert bonuses == sum(m.bonus for m in transcript.state.missions)


def test_the_conservation_check_notices_money_that_appears_or_vanishes() -> None:
    transcript = play_seed(CONFIG, "score-gap")
    lots, hammers = transcript.state.deal.lots, transcript.state.hammers
    scores = list(transcript.state.scores)
    assert conservation_gap(scores, lots, hammers) == 0

    def moved(score: SeatScore, by: int) -> SeatScore:
        return score.model_copy(update={"surplus": score.surplus + by, "total": score.total + by})

    assert conservation_gap([moved(scores[0], 7), *scores[1:]], lots, hammers) == 7
    assert conservation_gap([scores[0], moved(scores[1], -3), *scores[2:]], lots, hammers) == -3


def test_model_seats_are_ranked_by_total_and_bots_are_not() -> None:
    transcript = play_seed(CONFIG, "score-rank")
    scores = {s.seat: s for s in transcript.state.scores}
    assert scores[7].rank_tonight is None and scores[8].rank_tonight is None
    totals = {seat: scores[seat].total for seat in MODEL_SEATS}
    ranks = [scores[seat].rank_tonight for seat in MODEL_SEATS]
    for seat, rank in zip(MODEL_SEATS, ranks, strict=True):
        assert rank == 1 + sum(1 for other in totals.values() if other > totals[seat])
    assert min(r for r in ranks if r is not None) == 1
