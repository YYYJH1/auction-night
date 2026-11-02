"""Highlights: the score of each category, the choice, the place in the log and the sentence.

The scores are checked on lots written by hand, rung by rung. The placement, the choice and the
sentences are checked on played nights: the mock policies with the rule bots, and two scripted
nights whose numbers are known. The placement is held against a port of the viewer's
``momentStart`` (``viewer/lib/highlights.js``), and the viewer's tables against the Python ones.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from prize_support import (
    VIEWER,
    Played,
    awards_night,
    clocks_night,
    first_bid_seq,
    mock_night,
    play_mock_night,
    public_numbers,
    viewer_basis,
    viewer_zone,
)
from show_support import make_lot, seat_line

from auction_night.contracts.events import (
    ActionEvent,
    GuessEvent,
    HammerEvent,
    JudgeVerdictData,
    LotOpenEvent,
    Pledge,
    TalkEvent,
    canonical_json,
)
from auction_night.contracts.replay import Highlight, PledgeRecord, Replay
from auction_night.show.candidates import find_candidates
from auction_night.show.cards import (
    ActionLine,
    Candidate,
    GuessLine,
    LotData,
    MissionLine,
    PriorLine,
)
from auction_night.show.highlights import (
    BASE_SCORE,
    CLAIM_RUNGS,
    HIGHLIGHT_BASIS,
    HIGHLIGHT_ORDER,
    MAX_PER_CATEGORY,
    Moment,
    choose_moments,
    compute_highlights,
    find_moments,
    score_candidate,
    score_false_claim,
)

SPAN = 300  # the width of the budget range in the hand-made lots
MISS = 150


def candidates_of(lot: LotData, category: str) -> list[Candidate]:
    return [c for c in find_candidates(lot, miss_error=MISS) if c.category == category]


def score_of(lot: LotData, category: str, **options: Any) -> int:
    (found,) = candidates_of(lot, category)
    return score_candidate(lot, found, budget_span=SPAN, **options)


# ================================================================== the score, category by category


def test_every_category_has_a_base_score_a_basis_and_a_place_in_the_order() -> None:
    assert len(HIGHLIGHT_ORDER) == 8
    assert set(BASE_SCORE) == set(HIGHLIGHT_BASIS) == set(HIGHLIGHT_ORDER)
    assert all(0 <= base <= 10 for base in BASE_SCORE.values())
    assert min(BASE_SCORE.values()) == 3


# ----------------------------------------------------------------------------- winner's curse


def box(price: int, *, true: int = 200, high: int = 300, tier: int = 200) -> LotData:
    return make_lot(
        fmt="blind_box",
        tier=tier,
        seats=[seat_line(1, won=True, paid=price), seat_line(2)],
        winner=1,
        price=price,
        true_value=true,
        prior=PriorLine(low=100, high=high, noise=40),
    )


@pytest.mark.parametrize(
    ("price", "score"),
    [
        (201, 4),  # any overpay is a candidate: the base
        (209, 4),  # 4.5% of the tier
        (210, 5),  # 5%
        (219, 5),
        (220, 6),  # 10%
        (239, 6),
        (240, 7),  # 20%
        (300, 7),  # 50%, at the top of the prior but not above it
    ],
)
def test_the_curse_climbs_a_rung_at_five_ten_and_twenty_percent_of_the_tier(
    price: int, score: int
) -> None:
    assert score_of(box(price), "winners_curse") == score


def test_a_price_above_the_top_of_the_public_prior_adds_two() -> None:
    assert score_of(box(301), "winners_curse") == 7 + 2
    assert score_of(box(300), "winners_curse") == 7
    assert score_of(box(251, true=245, high=250), "winners_curse") == 4 + 2  # a gap of 3%


def test_the_curse_never_goes_above_ten() -> None:
    assert score_of(box(1000, true=100), "winners_curse") == 9


# ------------------------------------------------------------------------------ shill stuck


def stuck(price: int, threshold: int, value: int) -> LotData:
    mission = MissionLine(
        seat=1,
        kind="shill",
        outcome="stuck",
        target_lot=1,
        threshold=threshold,
        price=price,
        buyer=1,
    )
    return make_lot(
        seats=[seat_line(1, value=value, won=True, paid=price), seat_line(2)],
        winner=1,
        price=price,
        missions=(mission,),
    )


@pytest.mark.parametrize(
    ("price", "threshold", "value", "score"),
    [
        (100, 190, 150, 8),  # short of the threshold, below the value
        (190, 190, 250, 9),  # the right price, the wrong buyer
        (100, 190, 60, 9),  # paid more than the lot was worth
        (200, 190, 60, 10),  # both
        (100, 190, 100, 8),  # a price equal to the value is not a loss
    ],
)
def test_a_stuck_shill_scores_eight_plus_one_for_each_twist(
    price: int, threshold: int, value: int, score: int
) -> None:
    assert score_of(stuck(price, threshold, value), "shill_stuck") == score


# --------------------------------------------------------------------------------- backstab


def broken(
    pledge: Pledge,
    *,
    bids: tuple[int, ...] = (),
    winner: int | None = 2,
    price: int = 10,
    tier: int = 200,
) -> LotData:
    """Seat 1 broke ``pledge`` in lot 3 (it made it there). ``bids`` are its bid actions."""
    record = PledgeRecord(seq=20, lot=3, seat=1, pledge=pledge, outcome="violated")
    actions = tuple(ActionLine(phase="sealed", status="ok", value=value) for value in bids)
    return make_lot(
        lot=3,
        tier=tier,
        seats=[
            seat_line(1, won=winner == 1, paid=price if winner == 1 else 0, actions=actions),
            seat_line(2, won=winner == 2, paid=price if winner == 2 else 0),
        ],
        winner=winner,
        price=price,
        pledges_due=(record,),
    )


NO_BID = Pledge(kind="no_bid", lot=3)


def test_a_broken_pledge_scores_four_and_two_more_when_the_breaker_won() -> None:
    assert score_of(broken(NO_BID, bids=(30,), winner=2), "backstab") == 4
    assert score_of(broken(NO_BID, bids=(30,), winner=1), "backstab") == 6


def test_a_deliberate_break_adds_two_and_other_intents_nothing() -> None:
    lot = broken(NO_BID, bids=(30,))
    assert score_of(lot, "backstab", intent="deliberate") == 6
    assert score_of(lot, "backstab", intent="unintentional") == 4
    assert score_of(lot, "backstab", intent="unknown") == 4


@pytest.mark.parametrize(
    ("pledge", "bids", "price", "score"),
    [
        (NO_BID, (49,), 10, 4),  # a bid of a quarter of the tier (50 of 200) is big
        (NO_BID, (50,), 10, 5),
        (Pledge(kind="max_bid", lot=3, amount=40), (89,), 10, 4),  # 49 above the cap
        (Pledge(kind="max_bid", lot=3, amount=40), (90,), 10, 5),  # 50 above the cap
        (Pledge(kind="max_bid", lot=3, amount=40), (30, 90, 41), 10, 5),  # the highest bid counts
    ],
)
def test_a_bid_a_quarter_of_the_tier_against_the_word_adds_one(
    pledge: Pledge, bids: tuple[int, ...], price: int, score: int
) -> None:
    assert score_of(broken(pledge, bids=bids, price=price, winner=2), "backstab") == score


@pytest.mark.parametrize(("price", "score"), [(49, 4 + 2), (50, 4 + 2 + 1)])
def test_for_let_win_the_price_paid_is_the_money_against_the_word(price: int, score: int) -> None:
    lot = broken(Pledge(kind="let_win", lot=3, to=2), winner=1, price=price)
    assert score_of(lot, "backstab") == score


def test_all_the_twists_of_a_backstab_add_up_to_nine() -> None:
    lot = broken(NO_BID, bids=(80,), winner=1, price=80)
    assert score_of(lot, "backstab", intent="deliberate") == 4 + 2 + 2 + 1


# ------------------------------------------------------------------------------ false claim


def claim(**extra: Any) -> JudgeVerdictData:
    return JudgeVerdictData(verdict="false_claim", seats=[2], lots=[1], **extra)


def test_a_false_claim_scores_five_with_two_for_intent_and_one_for_winning_the_lot() -> None:
    won = make_lot(seats=[seat_line(1), seat_line(2, won=True)], winner=2, price=30)
    lost = make_lot(seats=[seat_line(1, won=True), seat_line(2)], winner=1, price=30)
    assert score_false_claim(lost, claim()) == 5
    assert score_false_claim(won, claim()) == 6
    assert score_false_claim(lost, claim(intent="deliberate")) == 7
    assert score_false_claim(won, claim(intent="deliberate")) == 8
    assert score_false_claim(won, claim(intent="unintentional")) == 6
    assert score_false_claim(None, claim(intent="deliberate")) == 7


def test_an_unsold_lot_is_not_won_by_the_claimant() -> None:
    unsold = make_lot(seats=[seat_line(2)], winner=None)
    assert score_false_claim(unsold, claim()) == 5


@pytest.mark.parametrize(
    ("gap", "rungs"),
    [
        (None, 0),  # the kind or the tag of a mission has no gap
        (0, 0),
        (100, 0),  # the least that makes a budget claim false
        (150, 0),  # the least that makes a valuation false
        (249, 0),
        (250, 1),  # a quarter of the tier or of the budget
        (499, 1),
        (500, 2),
        (999, 2),
        (1000, 3),  # a whole tier or a whole budget off
        (5000, 3),  # and no more than three rungs
    ],
)
def test_the_gap_of_a_claim_adds_a_rung_for_a_quarter_a_half_and_a_whole(
    gap: int | None, rungs: int
) -> None:
    assert CLAIM_RUNGS == (250, 500, 1000)
    lost = make_lot(seats=[seat_line(1, won=True), seat_line(2)], winner=1, price=30)
    assert score_false_claim(lost, claim(gap_milli=gap)) == 5 + rungs
    assert score_false_claim(None, claim(gap_milli=gap)) == 5 + rungs


def test_the_rungs_of_the_gap_add_to_intent_and_a_win_and_stop_at_ten() -> None:
    won = make_lot(seats=[seat_line(1), seat_line(2, won=True)], winner=2, price=30)
    far = claim(intent="deliberate", gap_milli=700)
    assert score_false_claim(won, far) == 5 + 2 + 1 + 2
    assert score_false_claim(won, claim(intent="deliberate", gap_milli=1000)) == 10
    assert score_false_claim(won, claim(intent="deliberate", gap_milli=90_000)) == 10
    near = claim(intent="unintentional", gap_milli=260)
    assert score_false_claim(won, near) == 5 + 1 + 1


def test_a_wider_miss_never_scores_less_and_the_order_of_two_claims_follows_the_gap() -> None:
    lost = make_lot(seats=[seat_line(1, won=True), seat_line(2)], winner=1, price=30)
    scores = [score_false_claim(lost, claim(gap_milli=gap)) for gap in range(0, 1500, 7)]
    assert scores == sorted(scores) and scores[0] == 5 and scores[-1] == 8


# ------------------------------------------------------------------------ plan contradiction


def plan_won(stars: int, price: int, budget: int | None, tier: int = 200) -> LotData:
    line = seat_line(1, won=True, paid=price, played=price, plan_stars=stars, plan_budget=budget)
    return make_lot(tier=tier, seats=[line, seat_line(2)], winner=1, price=price)


def plan_unbid(stars: int, *, budget: int, price: int, abstained: bool = False) -> LotData:
    line = seat_line(
        1,
        played=None if abstained else 0,
        abstained=abstained,
        plan_stars=stars,
        plan_budget=budget,
    )
    other = seat_line(2, won=price > 0, paid=price)
    return make_lot(seats=[line, other], winner=2 if price > 0 else None, price=price)


@pytest.mark.parametrize(
    ("stars", "price", "budget", "score"),
    [
        (0, 40, 50, 3 + 2),  # nothing for the size: 20% of the tier
        (1, 40, 50, 3 + 1),
        (0, 50, 60, 3 + 2 + 1),  # 25% of the tier: a rung
        (0, 99, 120, 3 + 2 + 1),
        (0, 100, 120, 3 + 2 + 2),  # 50%: two rungs
        (1, 100, 99, 3 + 1 + 2 + 1),  # and above the planned budget
        (1, 40, None, 3 + 1),  # no planned budget: no twist
    ],
)
def test_a_low_plan_that_won_scores_for_the_stars_the_price_and_the_budget(
    stars: int, price: int, budget: int | None, score: int
) -> None:
    assert score_of(plan_won(stars, price, budget), "plan_contradiction") == score


def test_a_three_star_lot_without_a_bid_scores_three_and_one_when_it_was_affordable() -> None:
    sold = plan_unbid(3, budget=80, price=80)
    assert score_of(sold, "plan_contradiction") == 4  # sold for no more than it planned
    assert score_of(plan_unbid(3, budget=79, price=80), "plan_contradiction") == 3
    assert score_of(plan_unbid(3, budget=80, price=0), "plan_contradiction") == 3  # unsold
    assert score_of(plan_unbid(3, budget=80, price=80, abstained=True), "plan_contradiction") == 3


# -------------------------------------------------------------------------- all-pay tilt


def tilt(paid: int, winning: int, tier: int = 200) -> LotData:
    return make_lot(
        fmt="all_pay",
        tier=tier,
        seats=[seat_line(1, won=True, paid=winning), seat_line(2, paid=paid), seat_line(3)],
        winner=1,
        price=winning,
        payments={1: winning, 2: paid},
    )


@pytest.mark.parametrize(
    ("paid", "winning", "score"),
    [
        (20, 100, 3),  # a tenth of the tier: the base
        (49, 100, 3),
        (50, 100, 4),  # a quarter
        (99, 150, 4),
        (100, 150, 5),  # a half
        (199, 250, 5),
        (200, 250, 6),  # the whole tier
        (60, 70, 4 + 1),  # lost by 10 of 200: exactly 5% of the tier
        (60, 71, 4),  # by 11
        (100, 105, 5 + 1),
    ],
)
def test_a_tilt_climbs_with_the_coins_lost_and_gains_one_for_a_narrow_loss(
    paid: int, winning: int, score: int
) -> None:
    assert score_of(tilt(paid, winning), "all_pay_tilt") == score


# ------------------------------------------------------------------------------- bargain


def bargain(gain: int, tier: int = 200) -> LotData:
    price = 20
    line = seat_line(1, won=True, paid=price, value=price + gain)
    return make_lot(tier=tier, seats=[line, seat_line(2)], winner=1, price=price)


@pytest.mark.parametrize(
    ("gain", "score"),
    [(40, 3), (69, 3), (70, 4), (99, 4), (100, 5), (149, 5), (150, 6), (180, 6)],
)
def test_a_bargain_climbs_a_rung_at_35_50_and_75_percent_of_the_tier(gain: int, score: int) -> None:
    assert score_of(bargain(gain), "bargain") == score


# ------------------------------------------------------------------------ mind-reading miss


def missed(guess: int, truth: int) -> LotData:
    reading = GuessLine(target=2, guess=guess, truth=truth, abs_error=abs(guess - truth))
    return make_lot(seats=[seat_line(1, guess=reading), seat_line(2)])


@pytest.mark.parametrize(
    ("guess", "truth", "score"),
    [
        (300, 150, 3),  # 50% of the span: below the first rung; twice the truth
        (400, 200, 3),  # 67% of the span; twice the truth
        (300, 100, 3 + 1),  # three times the truth
        (100, 300, 3 + 1),  # a third of the truth
        (300, 75, 3 + 1 + 1),  # 75% of the span, and four times the truth
        (500, 200, 3 + 2),  # 100% of the span; 2.5 times the truth is not three
        (200, 600, 3 + 2 + 1),  # 133%, and exactly a third
        (50, 500, 3 + 3 + 1),  # 150%
    ],
)
def test_a_miss_climbs_with_the_error_in_budget_spans(guess: int, truth: int, score: int) -> None:
    lot = missed(guess, truth)
    (found,) = find_candidates(lot, miss_error=1)
    assert score_candidate(lot, found, budget_span=SPAN) == score


def test_a_candidate_that_is_not_scored_from_the_cards_is_refused() -> None:
    odd = Candidate(category="claim_vs_hand", lot=1, seats=(1,), facts={}, reason="x")
    with pytest.raises(ValueError, match="claim_vs_hand"):
        score_candidate(make_lot(), odd, budget_span=SPAN)


# ====================================================================== played nights


SEEDS = ["d8-lights-1", "d8-lights-2", "d8-lights-3", "d8-lights-4"]


def mock_play(seed: str) -> Played:
    state, cards = mock_night(seed)
    return Played(state, cards, state.deal, {}, {})


NIGHTS = {
    **{seed: lambda seed=seed: mock_play(seed) for seed in SEEDS},
    "awards": awards_night,
    "clocks": clocks_night,
}


@pytest.fixture(scope="module", params=list(NIGHTS))
def night(request: pytest.FixtureRequest) -> Played:
    return NIGHTS[request.param]()


def spans(state: Any) -> dict[int, tuple[int, int]]:
    """Lot -> (its lot_open, its hammer)."""
    opens = {e.lot: e.seq for e in state.events if isinstance(e, LotOpenEvent)}
    hammers = {e.lot: e.seq for e in state.events if isinstance(e, HammerEvent)}
    return {lot: (opens[lot], hammers[lot]) for lot in opens}


# ------------------------------------------------------------------------------ the choice


def test_every_candidate_of_the_cards_becomes_a_moment(night: Played) -> None:
    moments = find_moments(night.cards, night.state.events)
    assert len(moments) == len(night.cards.night.data.candidates)
    assert [m.order for m in moments] == sorted(m.order for m in moments)


def test_the_three_best_of_each_category_are_kept_and_numbered_in_the_order_of_the_night(
    night: Played,
) -> None:
    moments = find_moments(night.cards, night.state.events)
    lights = compute_highlights(night.cards, night.state.events)
    expected: list[Moment] = []
    for category in HIGHLIGHT_ORDER:
        mine = [m for m in moments if m.category == category]
        mine.sort(key=lambda m: (-m.score, m.lot, m.first_seq, m.last_seq, m.seats))
        expected += mine[:MAX_PER_CATEGORY]
    expected.sort(key=lambda m: (m.lot, m.first_seq, m.last_seq, HIGHLIGHT_ORDER.index(m.category)))
    assert [(h.category, h.lot, h.first_seq, h.last_seq) for h in lights] == [
        (m.category, m.lot, m.first_seq, m.last_seq) for m in expected
    ]
    assert [h.id for h in lights] == list(range(1, len(lights) + 1))
    for category in HIGHLIGHT_ORDER:
        assert sum(h.category == category for h in lights) <= MAX_PER_CATEGORY


def test_a_night_with_many_misses_keeps_the_three_worst_in_the_order_of_the_night() -> None:
    night = awards_night()
    moments = [
        m for m in find_moments(night.cards, night.state.events) if m.category == "mindread_miss"
    ]
    assert len(moments) == 12  # the scripted seats guess 100, so every lot has a miss
    kept = choose_moments(moments)
    assert len(kept) == MAX_PER_CATEGORY
    floor = sorted((m.score for m in moments), reverse=True)[MAX_PER_CATEGORY - 1]
    assert all(m.score >= floor for m in kept)
    assert [m.lot for m in kept] == sorted(m.lot for m in kept)


def test_scores_are_whole_numbers_from_the_lowest_base_to_ten(night: Played) -> None:
    for light in compute_highlights(night.cards, night.state.events):
        assert min(BASE_SCORE.values()) <= light.score <= 10


def test_the_highlights_validate_in_a_replay(night: Played) -> None:
    fixture = VIEWER.parent / "tests" / "fixtures" / "replays" / "sealed_four_lots.json"
    raw = json.loads(fixture.read_text(encoding="utf-8"))
    lights = compute_highlights(night.cards, night.state.events)
    raw["highlights"] = [light.model_dump(mode="json") for light in lights]
    assert Replay.model_validate(raw).highlights == lights


# --------------------------------------------------------------------- where a moment lies


def talk_seq(state: Any, lot: int, seat: int) -> int:
    return next(
        e.seq
        for e in state.events
        if isinstance(e, TalkEvent) and e.lot == lot and e.data.seat == seat
    )


def test_every_moment_lies_inside_its_lot(night: Played) -> None:
    lots = spans(night.state)
    for light in compute_highlights(night.cards, night.state.events):
        first, last = lots[light.lot]
        assert first <= light.first_seq <= light.last_seq <= last, light


def test_each_category_begins_and_ends_at_the_events_it_is_about(night: Played) -> None:
    state = night.state
    lots = spans(state)
    records = night.cards.night.data.pledges
    for m in find_moments(night.cards, state.events):
        first, last = lots[m.lot]
        (seat,) = m.seats
        begin, end = state.events[m.first_seq - 1], state.events[m.last_seq - 1]
        if m.category == "shill_stuck":
            assert (m.first_seq, m.last_seq) == (first, last)
        elif m.category in ("winners_curse", "bargain", "all_pay_tilt"):
            assert isinstance(begin, ActionEvent) and begin.data.seat == seat
            assert begin.data.phase in ("sealed", "english_init", "dutch")
            assert begin.lot == m.lot and isinstance(end, HammerEvent) and m.last_seq == last
        elif m.category == "plan_contradiction":
            assert isinstance(begin, TalkEvent) and begin.data.seat == seat and begin.lot == m.lot
            assert m.last_seq == last
        elif m.category == "mindread_miss":
            assert isinstance(begin, GuessEvent) and begin.data.seat == seat and begin.lot == m.lot
            assert m.last_seq == m.first_seq
        else:
            assert m.category == "backstab"
            sources = [
                r
                for r in records
                if r.seat == seat and r.pledge.lot == m.lot and r.outcome == "violated"
            ]
            assert m.first_seq in {max(r.seq, first) for r in sources}
            assert m.last_seq == last


def test_a_pledge_made_in_an_earlier_lot_opens_at_the_start_of_the_lot_it_was_broken_in(
    night: Played,
) -> None:
    state = night.state
    lots = spans(state)
    for m in find_moments(night.cards, state.events):
        if m.category != "backstab":
            continue
        (seat,) = m.seats
        made = {
            r.lot
            for r in night.cards.night.data.pledges
            if r.seat == seat and r.pledge.lot == m.lot and r.outcome == "violated"
        }
        if made == {m.lot}:
            assert m.first_seq > lots[m.lot][0]
            assert f"在第 {m.lot} 件答应" not in m.text
        elif m.lot not in made:
            assert m.first_seq == lots[m.lot][0] and "在第" in m.text


ZONES = {
    "shill_stuck": "lot",
    "winners_curse": "bid",
    "claim_vs_hand": "talk",
    "plan_contradiction": "talk",
    "all_pay_tilt": "bid",
    "bargain": "bid",
    "mindread_miss": "talk",
}


def test_the_viewer_sends_every_moment_to_the_zone_it_was_placed_for(night: Played) -> None:
    state = night.state
    lots = spans(state)
    found = {"lot": 0, "talk": 0, "bid": 0}
    for m in find_moments(night.cards, state.events):
        zone = viewer_zone(m.first_seq, lots[m.lot][0], first_bid_seq(state, m.lot))
        if m.category == "backstab":
            (seat,) = m.seats
            made = {
                r.lot
                for r in night.cards.night.data.pledges
                if r.seat == seat and r.pledge.lot == m.lot and r.outcome == "violated"
            }
            allowed = (
                {"talk"} if made == {m.lot} else {"lot"} if m.lot not in made else {"talk", "lot"}
            )
            assert zone in allowed, m
        else:
            assert zone == ZONES[m.category], m
        found[zone] += 1
    assert sum(found.values()) == len(night.cards.night.data.candidates)


def test_in_the_talk_zone_the_viewer_picks_the_line_that_the_moment_opens_at(night: Played) -> None:
    """``momentStart`` jumps to the first line, in speaking order, of a seat of the highlight."""
    state = night.state
    lots = spans(state)
    opens = {e.lot: e.data for e in state.events if isinstance(e, LotOpenEvent)}
    for m in find_moments(night.cards, state.events):
        if viewer_zone(m.first_seq, lots[m.lot][0], first_bid_seq(state, m.lot)) != "talk":
            continue
        picked = next(seat for seat in opens[m.lot].talk_order if seat in m.seats)
        opened_by = state.events[m.first_seq - 1]
        assert isinstance(opened_by, TalkEvent | GuessEvent)
        assert picked == opened_by.data.seat, m


# ------------------------------------------------------------------------------ the sentence


def numbers_of(text: str) -> set[int]:
    return {int(digits) for digits in re.findall(r"\d+", text)}


def test_a_public_category_names_only_numbers_that_the_whole_room_could_read(night: Played) -> None:
    public = public_numbers(night.state)
    for m in find_moments(night.cards, night.state.events):
        if HIGHLIGHT_BASIS[m.category] == "public":
            assert numbers_of(m.text) <= public, (m.category, m.text, numbers_of(m.text) - public)


def sentence_of(night: Played, category: str, lot: int, seat: int) -> str:
    (found,) = [
        m
        for m in find_moments(night.cards, night.state.events)
        if (m.category, m.lot, m.seats) == (category, lot, (seat,))
    ]
    return found.text


def test_a_broken_pledge_says_a_number_only_when_the_room_could_read_it() -> None:
    night = clocks_night()
    en, du, sf = night.lots["english"], night.lots["dutch"], night.lots["sealed_first"]
    # English: the winner's price is public, a loser's exit price is announced
    assert (
        sentence_of(night, "backstab", en[1], 2) == "2 号阿铁答应第 11 件最多出 100，实际付 120。"
    )
    assert (
        sentence_of(night, "backstab", en[1], 5)
        == "5 号青柠答应第 11 件最多出 100，实际退出价 120。"
    )
    # English: the winner's exit price of 60 broke a cap of 50 but only the winner knows it
    private = sentence_of(night, "backstab", en[0], 1)
    assert private == "1 号鲸落答应第 6 件最多出 50，却没有做到。"
    assert "60" not in private
    # Dutch: the buyer's price is public, another seat's grab price is not
    assert sentence_of(night, "backstab", du[0], 2) == "2 号阿铁答应第 4 件最多出 50，实际付 80。"
    hidden = sentence_of(night, "backstab", du[0], 6)
    assert hidden == "6 号麦芽答应第 4 件不出价，却没有做到。" and "70" not in hidden
    # a pledge to let another seat win, broken by winning
    assert (
        sentence_of(night, "backstab", sf[1], 3)
        == "3 号小满答应让 4 号拿下第 7 件，自己却拍下了，付 30。"
    )


def test_a_sealed_bid_is_public_and_a_blind_box_and_a_tilt_use_the_candidates_sentence() -> None:
    night = awards_night()
    ap, other = night.lots["all_pay"][0], night.notes["other"]
    assert sentence_of(night, "backstab", ap, 3) == "3 号小满答应第 2 件不出价，实际出价 15。"
    assert sentence_of(night, "backstab", other, 4) == (
        f"4 号拾光答应第 {other} 件最多出 50，实际出价 55。"
    )
    for m in find_moments(night.cards, night.state.events):
        if m.category in ("winners_curse", "all_pay_tilt"):
            data = night.cards.lot(m.lot).data
            (candidate,) = [c for c in data.candidates if c.category == m.category]
            assert m.text == candidate.reason


def test_a_pledge_from_an_earlier_lot_says_where_it_was_made(night: Played) -> None:
    state = night.state
    texts = [
        m.text
        for m in find_moments(night.cards, state.events)
        if m.category == "backstab" and "在第" in m.text
    ]
    for text in texts:
        assert re.search(r"号\S+在第 \d+ 件答应(第 \d+ 件|让 \d 号拿下第 \d+ 件)", text), text


# ------------------------------------------------------------------------- the stuck shill


def test_a_stuck_shill_is_scored_and_placed_over_the_whole_lot() -> None:
    night = clocks_night()
    lot = night.lots["english"][0]
    (stuck_moment,) = [
        m for m in find_moments(night.cards, night.state.events) if m.category == "shill_stuck"
    ]
    assert (stuck_moment.lot, stuck_moment.seats) == (lot, (1,))
    own = night.deal.lot(lot).values[1]
    assert stuck_moment.score == 8 + int(own < 40)  # 40 is below the threshold of 95
    first, last = spans(night.state)[lot]
    assert (stuck_moment.first_seq, stuck_moment.last_seq) == (first, last)
    assert stuck_moment.text.startswith("托儿 1 号鲸落把第 6 件抬到 40（门槛 95）")


# ------------------------------------------------------------------ the verdicts of the judges


def claim_in(night: Played, lot: int, seat: int, **extra: Any) -> JudgeVerdictData:
    seqs = spans(night.state)[lot][1]
    return JudgeVerdictData(
        verdict="false_claim",
        seats=[seat],
        lots=[lot],
        evidence_seqs=[talk_seq(night.state, lot, seat), seqs],
        **extra,
    )


def only_claims(night: Played, verdicts: list[JudgeVerdictData]) -> list[Moment]:
    found = find_moments(night.cards, night.state.events, verdicts)
    return [m for m in found if m.category == "claim_vs_hand"]


def test_a_false_claim_moment_is_scored_by_how_far_the_claim_was_from_the_hole_card() -> None:
    night = clocks_night()
    lot = night.lots["english"][1]
    near = claim_in(night, lot, 5, gap_milli=160, topic="valuation")
    far = claim_in(night, lot, 5, gap_milli=1200, topic="budget")
    mission = claim_in(night, lot, 5, topic="mission_kind")
    scores = [only_claims(night, [v])[0].score for v in (mission, near, far)]
    assert scores == [5, 5, 5 + 3], "seat 5 did not win the lot, so only the gap counts"


def test_a_false_claim_becomes_a_moment_from_its_words_to_the_hammer() -> None:
    night = clocks_night()
    lot = night.lots["english"][1]
    verdict = claim_in(
        night,
        lot,
        5,
        quotes=["我只剩 50"],
        detail="自称预算 50，当时实际还剩 5000。",
        intent="deliberate",
    )
    (found,) = only_claims(night, [verdict])
    assert (found.lot, found.seats, found.score) == (lot, (5,), 5 + 2)  # seat 2 won the lot
    assert (found.first_seq, found.last_seq) == (
        talk_seq(night.state, lot, 5),
        spans(night.state)[lot][1],
    )
    assert found.text == "5 号青柠说「我只剩 50」，与底牌对不上：自称预算 50，当时实际还剩 5000。"
    first = spans(night.state)[lot][0]
    assert viewer_zone(found.first_seq, first, first_bid_seq(night.state, lot)) == "talk"


def test_a_claim_by_the_winner_of_the_lot_scores_one_more() -> None:
    night = clocks_night()
    lot = night.lots["english"][1]
    (found,) = only_claims(night, [claim_in(night, lot, 2)])
    assert found.score == 5 + 1
    assert found.text == "2 号阿铁的说法与底牌对不上。"


def test_the_sentence_of_a_claim_uses_what_the_verdict_has() -> None:
    night = clocks_night()
    lot = night.lots["english"][1]
    quote_only = claim_in(night, lot, 5, quotes=["我穷"])
    detail_only = claim_in(night, lot, 5, detail="实际还剩 5000")
    (a,) = only_claims(night, [quote_only])
    (b,) = only_claims(night, [detail_only])
    assert a.text == "5 号青柠说「我穷」，与底牌对不上。"
    assert b.text == "5 号青柠的说法与底牌对不上：实际还剩 5000。"


def test_a_claim_without_evidence_covers_its_lot_and_one_without_a_place_is_left_out() -> None:
    night = clocks_night()
    lot = night.lots["english"][1]
    bare = JudgeVerdictData(verdict="false_claim", seats=[5], lots=[lot])
    (found,) = only_claims(night, [bare])
    assert (found.first_seq, found.last_seq) == spans(night.state)[lot]
    nowhere = JudgeVerdictData(verdict="false_claim", seats=[5])
    assert only_claims(night, [nowhere]) == []
    by_number = JudgeVerdictData(verdict="false_claim", seats=[5], evidence_seqs=[40, 90])
    (loose,) = only_claims(night, [by_number])
    assert (loose.lot, loose.first_seq, loose.last_seq) == (None, 40, 90)


def test_a_claim_is_placed_inside_its_lot_even_if_the_evidence_reaches_beyond() -> None:
    night = clocks_night()
    lot = night.lots["english"][1]
    first, last = spans(night.state)[lot]
    wide = JudgeVerdictData(
        verdict="false_claim", seats=[5], lots=[lot], evidence_seqs=[1, len(night.state.events)]
    )
    (found,) = only_claims(night, [wide])
    assert (found.first_seq, found.last_seq) == (first, last)


def test_only_false_claims_make_a_claim_moment() -> None:
    night = clocks_night()
    lot = night.lots["english"][1]
    other = [
        JudgeVerdictData(verdict=kind, seats=[5], lots=[lot])  # type: ignore[arg-type]
        for kind in ("pledge_violation", "collusion_red", "collusion_suspect")
    ]
    assert only_claims(night, other) == []


def test_the_claims_join_the_highlights_and_are_chosen_like_the_others() -> None:
    night = clocks_night()
    lots = [night.lots["english"][1], night.lots["english"][0], night.lots["dutch"][0]]
    verdicts = [claim_in(night, lot, 5, quotes=[f"说法 {lot}"]) for lot in lots]
    verdicts.append(claim_in(night, lots[0], 6, intent="deliberate"))
    lights = compute_highlights(night.cards, night.state.events, verdicts)
    claims = [h for h in lights if h.category == "claim_vs_hand"]
    assert len(claims) == MAX_PER_CATEGORY
    assert max(h.score for h in claims) == 7  # the deliberate one is kept
    assert [h.id for h in lights] == list(range(1, len(lights) + 1))


def test_a_verdict_about_intent_raises_the_score_of_the_broken_pledge() -> None:
    night = clocks_night()
    lot = night.lots["sealed_first"][1]
    record = next(r for r in night.cards.night.data.pledges if r.seat == 3 and r.pledge.lot == lot)
    hammer = spans(night.state)[lot][1]
    verdict = JudgeVerdictData(
        verdict="pledge_violation",
        seats=[3],
        lots=[lot],
        evidence_seqs=[record.seq, hammer],
        intent="deliberate",
    )
    before = find_moments(night.cards, night.state.events)
    after = find_moments(night.cards, night.state.events, [verdict])
    pick = lambda moments: next(m for m in moments if (m.category, m.lot) == ("backstab", lot))  # noqa: E731
    assert pick(after).score == pick(before).score + 2
    other = JudgeVerdictData(
        verdict="pledge_violation",
        seats=[4],
        lots=[lot],
        evidence_seqs=[record.seq],
        intent="deliberate",
    )
    assert pick(find_moments(night.cards, night.state.events, [other])).score == pick(before).score
    unknown = verdict.model_copy(update={"intent": "unknown"})
    assert (
        pick(find_moments(night.cards, night.state.events, [unknown])).score == pick(before).score
    )


def test_a_log_that_lacks_a_lot_is_refused() -> None:
    night = clocks_night()
    events = [e for e in night.state.events if not (isinstance(e, HammerEvent) and e.lot == 4)]
    with pytest.raises(ValueError, match="lot 4"):
        find_moments(night.cards, events)


def test_events_of_the_audience_and_the_system_are_ignored() -> None:
    night = clocks_night()
    plain = compute_highlights(night.cards, night.state.events)
    again = compute_highlights(night.cards, list(night.state.events))
    assert plain == again


# ------------------------------------------------------------------------------ determinism


def test_the_same_night_played_again_gives_the_same_bytes() -> None:
    state, cards = mock_night("d8-lights-1")
    again_state, again = play_mock_night("d8-lights-1")
    assert again_state is not state
    assert canonical_json(compute_highlights(cards, state.events)) == canonical_json(
        compute_highlights(again, again_state.events)
    )


def test_the_order_of_the_verdicts_does_not_change_the_highlights() -> None:
    night = clocks_night()
    lot = night.lots["english"][1]
    verdicts = [claim_in(night, lot, seat, quotes=[f"q{seat}"]) for seat in (1, 2, 3, 4, 5)]
    forward = compute_highlights(night.cards, night.state.events, verdicts)
    backward = compute_highlights(night.cards, night.state.events, list(reversed(verdicts)))
    assert canonical_json(forward) == canonical_json(backward)


def test_highlights_hold_no_decimal_number(night: Played) -> None:
    def refuse(text: str) -> float:
        raise AssertionError(f"a decimal number: {text}")

    lights = compute_highlights(night.cards, night.state.events)
    json.loads(canonical_json(lights), parse_float=refuse)
    assert all(isinstance(h, Highlight) for h in lights)


# ------------------------------------------------------------------- the viewer's tables


def js_list(source: str, name: str) -> list[str]:
    block = re.search(rf"{name}\s*=\s*Object\.freeze\(\[(.*?)\]\)", source, re.S)
    assert block, f"{name} not found"
    return re.findall(r"'([a-z_]+)'", block.group(1))


def test_the_categories_and_what_they_rest_on_follow_the_viewer() -> None:
    source = (VIEWER / "lib" / "highlights.js").read_text(encoding="utf-8")
    assert js_list(source, "CATEGORY_KEYS") == list(HIGHLIGHT_ORDER)
    assert viewer_basis("highlights.js") == dict(HIGHLIGHT_BASIS)


def test_every_highlight_carries_the_basis_of_its_category() -> None:
    viewer = viewer_basis("highlights.js")
    seen = set()
    for night in (clocks_night(), awards_night()):
        verdict = JudgeVerdictData(
            verdict="false_claim", seats=[1], lots=[1], evidence_seqs=[night.state.events[0].seq]
        )
        lights = compute_highlights(night.cards, night.state.events, [verdict])
        assert lights
        for light in lights:
            assert light.basis == HIGHLIGHT_BASIS[light.category] == viewer[light.category]
            seen.add(light.category)
    assert {"claim_vs_hand", "backstab"} <= seen
    assert {HIGHLIGHT_BASIS[c] for c in seen} == {"public", "private"}


# ------------------------------------------------------------- the same bytes in other processes

DIGEST_SCRIPT = """
import sys
sys.path[:0] = sys.argv[1:4]
from prize_support import clocks_night
from auction_night.contracts.events import canonical_json
from auction_night.show.highlights import compute_highlights

night = clocks_night()
print(canonical_json(compute_highlights(night.cards, night.state.events)))
"""


def test_the_bytes_do_not_depend_on_the_hash_seed_of_the_process() -> None:
    tests = Path(__file__).resolve().parents[1]
    outputs = set()
    for seed in ("1", "2", "31337"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "CUDA_VISIBLE_DEVICES": ""}
        done = subprocess.run(
            [sys.executable, "-c", DIGEST_SCRIPT]
            + [str(tests / part) for part in ("engine", "night", "show")],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        outputs.add(done.stdout)
    assert len(outputs) == 1
    night = clocks_night()
    assert outputs == {canonical_json(compute_highlights(night.cards, night.state.events)) + "\n"}
