"""A seat that the cost gate muted did not choose what the log shows for it.

When a night reaches its cost cap, every action of a model seat that is still to come reaches the
engine as an abstention with the reason ``budget_aborted``. The show layer must not read those as
the seat's own doing. These tests play scripted nights in which the gate skips seats on purpose
(``Script.mute``) and follow the muted seats through every reader of the log: the reader itself,
the pledges, the collusion screen, the fact cards, the highlight candidates, the awards, the
predictions and the reference numbers. A seat that abstained for any other reason is still a seat
that abstained.

The scripted night has twelve lots. The gate skips the bids of the six model seats in lot 6, which
still has its talk (it fired between the two), and their talk and bids in lots 7 to 12. The two
rule bots play on.
"""

import json
from dataclasses import dataclass
from functools import cache
from typing import get_args

import pytest
from export_support import NO_GIT, SAMPLE, mock_night
from show_support import SEATS_SIX, Script, make_lot, play_script, scripted_config, seat_line

from auction_night.contracts import NightConfig
from auction_night.contracts.events import AbstainReason, ActionData, ActionEvent, Pledge
from auction_night.engine.muted import muted_actions
from auction_night.engine.night import NightState
from auction_night.prompts import templates_digest
from auction_night.show.awards import build_awards, compute_awards
from auction_night.show.cards import LotData
from auction_night.show.content_scan import scan_prompt_digest
from auction_night.show.factcard import FactCards, NightData, build_fact_cards
from auction_night.show.host import (
    host_templates_digest,
    lot_job,
    make_host,
    mock_lot_text,
    mock_recap_text,
    run_host,
)
from auction_night.show.judges import judge_templates_digest, make_judges
from auction_night.show.lesson import select_lesson
from auction_night.show.pledges import judge_pledges
from auction_night.show.predict import reconcile
from auction_night.show.replay_export import build_replay
from auction_night.show.screening import muted_lots, screen_state
from auction_night.show.verdicts import run_judging
from auction_night.theory import LotTheory, analyse_night

MODELS = (1, 2, 3, 4, 5, 6)
BOTS = [
    {"seat": 7, "codename": "老实人", "kind": "bot", "policy": "truthful"},
    {"seat": 8, "codename": "经济学家", "kind": "bot", "policy": "economist", "pacing": True},
]
BID_CUT = 6  # the bids of this lot are skipped, its talk is not
TALK_CUT = 7  # from this lot on the talk is skipped too
LAST = 12
CAP_LINE = (
    "费用上限：这一夜从第 6 件起，模型座位因为费用上限不再行动。"
    "本件被跳过的座位：1、2、3、4、5、6 号，这不是它们自己的选择。"
)


@dataclass(frozen=True)
class Cut:
    """A night that the gate cut, with everything the show layer builds from it."""

    config: NightConfig
    state: NightState
    cards: FactCards
    theory: list[LotTheory]


@cache
def cut_night() -> Cut:
    bids = {(3, 4): 50, (4, 3): 80, (5, 3): 60, **{(lot, 5): 30 for lot in range(1, 6)}}
    pledges = {
        (3, 4): Pledge(kind="no_bid", lot=3),  # broken: seat 4 bids 50
        (4, 3): Pledge(kind="max_bid", lot=4, amount=100),  # kept: seat 3 bids 80
        (5, 3): Pledge(kind="max_bid", lot=5, amount=100),  # kept: seat 3 bids 60
        (4, 1): Pledge(kind="no_bid", lot=10),  # about a lot in which the gate muted seat 1
        (5, 1): Pledge(kind="no_bid", lot=9),  # the same
        (6, 2): Pledge(kind="no_bid", lot=6),  # made in lot 6, whose bids the gate skipped
        (6, 4): Pledge(kind="let_win", lot=6, to=3),  # the same
    }
    script = Script(bids=bids, pledges=pledges, plans={5: ((3,) * LAST, (100,) * LAST)})
    script.mute(range(TALK_CUT, LAST + 1), MODELS)
    script.mute([BID_CUT], MODELS, "bid")
    config = scripted_config(SEATS_SIX + BOTS)
    _, _, played = play_script(script, seed="show-muted-1", config=config)
    state = played.state
    return Cut(config, state, build_fact_cards(state), analyse_night(state))


# ================================================================================ the reader


def test_the_reader_finds_the_actions_that_name_the_cap() -> None:
    muted = muted_actions(cut_night().state.events)
    assert muted and muted.first_lot == BID_CUT
    assert set(muted.talks) == set(range(TALK_CUT, LAST + 1))
    assert set(muted.bids) == set(range(BID_CUT, LAST + 1))
    assert all(
        seats == frozenset(MODELS) for seats in (*muted.talks.values(), *muted.bids.values())
    )
    assert muted.seats_in(BID_CUT) == MODELS  # lot 6 was cut between its talk and its bids
    assert not muted.talks.get(BID_CUT)
    assert muted.bid_muted(6, 1) and not muted.bid_muted(5, 1) and not muted.bid_muted(6, 7)
    assert muted.lot_muted(LAST) and not muted.lot_muted(5)


def test_a_night_the_gate_never_cut_has_nothing_muted() -> None:
    plain = mock_night()
    muted = muted_actions(plain.state.events)
    assert not muted and muted.first_lot is None and muted.seats_in(3) == ()
    assert all(not d.muted and d.muted_from is None for d in plain.cards.lot_data)
    assert plain.cards.night.data.muted_from is None
    assert all("费用上限" not in card.text for card in plain.cards.lots)


def action(
    reason: AbstainReason | None, *, phase: str = "sealed", status: str = "abstain", lot: int = 3
) -> ActionEvent:
    data = ActionData(
        seat=2,
        phase=phase,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        reason=reason,
        value=None if status == "abstain" else 40,
    )
    return ActionEvent(seq=1, lot=None if phase == "plan" else lot, vis="seat:2", data=data)


@pytest.mark.parametrize("reason", [r for r in get_args(AbstainReason) if r != "budget_aborted"])
def test_an_abstention_for_another_reason_is_the_seats_own(reason: AbstainReason) -> None:
    assert not muted_actions([action(reason)])
    assert not muted_actions([action(reason, phase="talk")])


def test_the_cap_names_a_skipped_bid_and_a_skipped_line() -> None:
    assert muted_actions([action("budget_aborted")]).bids == {3: frozenset({2})}
    assert muted_actions([action("budget_aborted", phase="talk")]).talks == {3: frozenset({2})}
    assert muted_actions([action("budget_aborted", phase="english_init")]).lot_muted(3)


def test_a_kept_english_price_and_a_skipped_plan_are_not_muted() -> None:
    # the log gives no reason for an English revision that the gate skipped (see the engine text)
    kept = action(None, phase="english_revise", status="kept_previous")
    assert not muted_actions([kept])
    assert not muted_actions([action("budget_aborted", phase="plan")])


# =============================================================================== the pledges


def test_a_pledge_that_the_gate_made_untestable_is_void() -> None:
    night = cut_night()
    records = {(r.seat, r.lot, r.pledge.lot): r for r in judge_pledges(night.state.events)}
    for key in [(1, 4, 10), (1, 5, 9), (2, 6, 6), (4, 6, 6)]:
        assert records[key].outcome == "void", key
        assert "费用上限" in records[key].detail
    assert records[(4, 3, 3)].outcome == "violated"
    assert records[(3, 4, 4)].outcome == records[(3, 5, 5)].outcome == "kept"
    night_data = night.cards.night.data
    rows = {row.seat: row for row in night_data.seats}
    assert (rows[1].pledges_void, rows[1].pledges_kept, rows[1].pledges_violated) == (2, 0, 0)
    assert (rows[3].pledges_void, rows[3].pledges_kept) == (0, 2)


def test_a_muted_seat_neither_keeps_nor_breaks_its_word() -> None:
    night = cut_night()
    muted = muted_actions(night.state.events)
    for record in judge_pledges(night.state.events):
        if muted.bid_muted(record.pledge.lot, record.seat):
            assert record.outcome == "void", record


def test_a_pledge_that_could_not_be_tested_earns_no_award() -> None:
    awards = compute_awards(cut_night().cards)
    # seat 1 would have two kept pledges (no bid in the lots where the gate muted it)
    assert (awards.award("keeper").winners, awards.award("keeper").metric) == ([3], 2)
    assert awards.award("backstabber").winners == [4]


# ============================================================================== the screening


def test_a_lot_the_models_did_not_play_is_not_screened() -> None:
    state = cut_night().state
    assert [screen.lot for screen in screen_state(state)] == [1, 2, 3, 4, 5]
    assert muted_lots(state) == tuple(range(BID_CUT, LAST + 1))


def test_the_collusion_verdict_names_the_lots_it_did_not_screen() -> None:
    night = cut_night()
    judging = run_judging(night.state, make_judges(night.config))
    (verdict,) = [v for v in judging.verdicts if v.verdict.startswith("collusion")]
    # lot 2 is over the threshold, and no lot of the cut is on the list or in the numbers
    assert (verdict.verdict, verdict.lots) == ("collusion_suspect", [2])
    assert set(verdict.statistic_milli) == {1, 2, 3, 4, 5}
    assert "第 6、7、8、9、10、11、12 件的模型座位被费用上限静音，没有筛查。" in verdict.detail
    assert judging.collusion.skipped == tuple(range(BID_CUT, LAST + 1))
    assert {f.lot for f in judging.collusion.lots} == {1, 2, 3, 4, 5}


# ============================================================================= the fact cards


def test_a_lot_with_muted_seats_carries_the_line_of_the_cap() -> None:
    cards = cut_night().cards
    for lot in range(BID_CUT, LAST + 1):
        data = cards.lot(lot).data
        assert data.muted_from == BID_CUT and data.muted == MODELS
        assert CAP_LINE in cards.lot(lot).text.splitlines(), lot
    for lot in range(1, BID_CUT):
        assert not cards.lot(lot).data.muted and "费用上限" not in cards.lot(lot).text


def test_a_muted_seat_is_shown_as_not_having_acted_and_never_as_an_abstention() -> None:
    card = cut_night().cards.lot(9)
    models = [line for line in card.data.seats if line.role == "player"]
    assert models and all(line.muted and line.abstained and line.played is None for line in models)
    assert not any(line.muted for line in card.data.seats if line.role == "bot")
    assert card.text.count("费用上限，没有行动") == len(models)
    assert "弃权" not in card.text


def test_the_night_card_and_the_headline_say_it_too() -> None:
    cards = cut_night().cards
    status = "状态：因费用上限中止，这一夜不进积分榜。从第 6 件起，模型座位因为费用上限不再行动。"
    assert status in cards.night.text.splitlines()
    assert cards.night.data.muted_from == BID_CUT
    assert "费用上限已满，模型座位没有行动。" in cards.lot(9).headline
    assert "费用上限" not in cards.lot(2).headline
    # every number of a line is on the card, so the host may repeat it
    assert {"6", "9"} <= cards.lot(9).numbers


def test_the_host_reads_the_line_and_the_templates_are_the_same() -> None:
    cards = cut_night().cards
    assert CAP_LINE in lot_job(cards.lot(9)).user.splitlines()
    # the hashes of the templates are those of the committed sample, so no prompt was touched
    manifest = json.loads(SAMPLE.read_text(encoding="utf-8"))["manifest"]
    assert templates_digest("zh") == manifest["prompt_hash"]
    assert host_templates_digest("zh") == manifest["host_prompt_hash"]
    assert judge_templates_digest("zh") == manifest["judge_prompt_hash"]
    assert scan_prompt_digest() == manifest["scan_prompt_hash"]


def test_the_mock_host_says_the_cap_on_a_muted_lot_and_in_the_recap() -> None:
    night = cut_night()
    muted, open_lot, whole = (
        night.cards.lot(9).data,
        night.cards.lot(2).data,
        night.cards.night.data,
    )
    assert isinstance(muted, LotData) and isinstance(open_lot, LotData)
    assert isinstance(whole, NightData)
    assert "费用上限已满，模型座位没有行动。" in mock_lot_text(muted)
    assert "费用上限" not in mock_lot_text(open_lot)
    assert "从第 6 件起，模型座位因为费用上限不再行动。" in mock_recap_text(whole)
    block = run_host(night.cards, make_host(night.config)).block
    assert not any(line.fallback for line in block.per_lot)  # the numbers are on the cards
    assert "费用上限已满" in next(line.text for line in block.per_lot if line.lot == 9)


# ====================================================================== the reference numbers


def test_a_muted_seat_has_no_regret_and_no_excess_return() -> None:
    night = cut_night()
    muted = muted_actions(night.state.events)
    for theory in night.theory:
        silent = set(muted.bids.get(theory.lot, ()))
        assert silent.isdisjoint(theory.regret) and silent.isdisjoint(theory.excess)
        assert silent <= set(theory.ref_bids)  # the textbook bid is a fact about its cards
        assert set(theory.regret) == set(theory.ref_bids) - silent
        if silent:
            assert theory.to_highest_value is None
    sold = [t for t in night.theory[:5] if t.to_highest_value is not None]
    assert sold, "the lots before the cut keep their flag"
    line = night.cards.lot(9).data.seat_line(1)
    assert (line.regret, line.excess, line.ref_bid is not None) == (None, None, True)


def test_the_replay_gives_a_muted_seat_no_regret_and_counts_none_of_its_skipped_actions() -> None:
    night = cut_night()
    replay = build_replay(night.config, night.state, git=NO_GIT)
    for lot in replay.timeline:
        if lot.lot >= BID_CUT:
            assert (
                set(MODELS).isdisjoint(lot.overlay.regret) and lot.overlay.to_highest_value is None
            )
            assert {a.seat for a in lot.actions if a.reason == "budget_aborted"} == set(MODELS)
    skipped = ("not_asked", "budget_aborted")
    for score in replay.scores:
        own = [
            e.data
            for e in night.state.events
            if isinstance(e, ActionEvent) and e.data.seat == score.seat
        ]
        counted = [d for d in own if not (d.status == "abstain" and d.reason in skipped)]
        assert score.stats.actions == len(counted), score.seat
        assert score.stats.abstentions == sum(
            d.status == "kept_previous" or (d.status == "abstain" and d.reason != "refusal")
            for d in counted
        )
    assert next(s for s in replay.scores if s.seat == 1).stats.abstentions == 0


# ================================================================================ the show


def test_a_muted_seat_does_not_contradict_its_plan_and_a_lot_without_a_field_has_no_bargain() -> (
    None
):
    cards = cut_night().cards
    # seat 5 planned three stars for lot 9, was asked and did not act: before, a broken plan
    line = cards.lot(9).data.seat_line(5)
    assert (line.plan_stars, line.asked, line.abstained, line.muted) == (3, True, True, True)
    # a bot took lots 7 and 8 for far less than their value: before, two bargains
    for lot in (7, 8):
        data = cards.lot(lot).data
        assert data.winner in (7, 8) and data.prior is None and data.bids_muted
        assert (data.seat_line(data.winner).value or 0) - data.price >= data.tier // 5
    found = {(c.category, c.lot) for d in cards.lot_data for c in d.candidates}
    assert not {x for x in found if x[1] >= BID_CUT and x[0] in ("plan_contradiction", "bargain")}
    assert ("bargain", 2) in found, "a lot before the cut keeps its bargain"


def test_a_lot_without_a_field_gives_no_bargain_to_a_bot() -> None:
    def lot(number: int, price: int, *, muted: bool) -> object:
        bot = seat_line(8, role="bot", played=150, ref=150, value=190, won=True, paid=price)
        rival = seat_line(1, played=None, abstained=True, muted=muted, ref=100, value=180)
        return make_lot(
            number, "sealed_second", 200, [rival, bot], winner=8, price=price,
            payments={8: price},
        )  # fmt: skip

    quiet, real = lot(1, 1, muted=True), lot(2, 100, muted=False)
    awards = build_awards([quiet, real], budget_span=300)  # type: ignore[list-item]
    bargain = awards.bot_award("bargain")
    assert (bargain.winners, bargain.metric) == ([8], 90)  # lot 2: 190 - 100, not lot 1: 190 - 1


def test_a_muted_seat_is_no_example_for_the_lesson() -> None:
    def lot(number: int, *, muted: bool) -> object:
        # the seat is far from its textbook bid because it did not bid, or because it chose to
        far = seat_line(
            1, played=None if muted else 10, abstained=muted, muted=muted, ref=150, value=150
        )
        rival = seat_line(2, played=150, ref=150, value=150, won=True, paid=150)
        return make_lot(number, "sealed_second", 200, [far, rival], winner=2, price=150)

    assert select_lesson([lot(1, muted=True)]).topic != "truthful_second_price"  # type: ignore[list-item]
    assert select_lesson([lot(1, muted=False)]).topic == "truthful_second_price"  # type: ignore[list-item]


def test_a_muted_bid_is_in_no_measure_of_behaviour() -> None:
    night = cut_night()
    lots = night.cards.lot_data
    first = next(o for o in reconcile(lots).outcomes if o.claim == "first_price_overbid")
    # lot 5 is the only first-price lot that was played; the skipped bids of lot 9 are not counted
    assert sum(first.counts.values()) == 6
    metrics = compute_awards(night.cards).metrics
    for seat in MODELS:
        ordinary = [
            d
            for d in lots
            if d.lot < BID_CUT and d.prior is None and d.seat_line(seat).ref_bid is not None
        ]
        assert metrics[seat].textbook_bids == len(ordinary), seat


def test_a_skipped_line_has_no_mind_reading_guess() -> None:
    night = cut_night()
    metrics = compute_awards(night.cards).metrics
    for seat in MODELS:
        guesses = [
            (d.lot, d.seat_line(seat).guess)
            for d in night.cards.lot_data
            if d.seat_line(seat).guess
        ]
        assert guesses and all(lot < TALK_CUT for lot, _ in guesses), seat
        assert metrics[seat].valid_guesses == len(guesses), seat
