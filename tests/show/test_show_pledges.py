"""Pledges are judged by program from the event log: kept, violated, or void."""

from typing import Any

import pytest
from show_support import Script, lots_of, play_script, scripted_config

from auction_night.contracts.events import (
    HammerEvent,
    LotOpenEvent,
    Pledge,
    TalkEvent,
)
from auction_night.contracts.replay import PledgeRecord
from auction_night.engine.deal import deal
from auction_night.show.pledges import bid_word, judge_pledges, pledge_text


def by_key(records: list[PledgeRecord]) -> dict[tuple[int, int, str], PledgeRecord]:
    """Records keyed by (seat, lot the pledge is about, kind). The test nights never repeat one."""
    keyed = {(r.seat, r.pledge.lot, r.pledge.kind): r for r in records}
    assert len(keyed) == len(records)
    return keyed


@pytest.fixture(scope="module")
def night() -> Any:
    """One night with a pledge of every kind, kept and broken, in the formats that matter."""
    the_deal = deal(scripted_config(), seed="show-pledges-1")
    s1 = lots_of(the_deal, "sealed_first")[0]
    e1 = lots_of(the_deal, "english")[0]
    d1 = lots_of(the_deal, "dutch")[0]
    l2 = lots_of(the_deal, "sealed_second")[0]
    l3 = lots_of(the_deal, "all_pay")[1]
    script = Script(
        bids={
            (s1, 1): 30, (s1, 3): 40, (s1, 4): 41,
            (e1, 5): 25,
            (d1, 1): 21,
            (l2, 2): 1000,
        },
        pledges={
            (s1, 1): Pledge(kind="no_bid", lot=s1),
            (s1, 2): Pledge(kind="no_bid", lot=s1),
            (s1, 3): Pledge(kind="max_bid", lot=s1, amount=40),
            (s1, 4): Pledge(kind="max_bid", lot=s1, amount=40),
            (e1, 5): Pledge(kind="no_bid", lot=e1),
            (e1, 6): Pledge(kind="no_bid", lot=e1),
            (d1, 1): Pledge(kind="max_bid", lot=d1, amount=20),
            (l2, 2): Pledge(kind="let_win", lot=l2, to=3),
            (l2, 4): Pledge(kind="let_win", lot=l2, to=5),
            (l3, 6): Pledge(kind="let_win", lot=l3, to=1),
        },
    )  # fmt: skip
    _, _, transcript = play_script(script, seed="show-pledges-1")
    lots = {"s1": s1, "e1": e1, "d1": d1, "l2": l2, "l3": l3}
    return lots, transcript, judge_pledges(transcript.events)


def test_a_sealed_bid_above_zero_breaks_no_bid(night: Any) -> None:
    lots, _, records = night
    broken = by_key(records)[(1, lots["s1"], "no_bid")]
    assert broken.outcome == "violated"
    assert broken.detail == f"承诺第 {lots['s1']} 件不出价，实际出价 30"


def test_no_bid_is_kept_when_the_seat_bids_nothing(night: Any) -> None:
    lots, _, records = night
    kept = by_key(records)[(2, lots["s1"], "no_bid")]
    assert kept.outcome == "kept"
    assert "没有出过正价" in kept.detail


def test_max_bid_is_kept_at_exactly_the_amount_and_broken_one_above(night: Any) -> None:
    lots, _, records = night
    keyed = by_key(records)
    assert keyed[(3, lots["s1"], "max_bid")].outcome == "kept"
    assert "实际最高出价 40" in keyed[(3, lots["s1"], "max_bid")].detail
    broken = keyed[(4, lots["s1"], "max_bid")]
    assert broken.outcome == "violated"
    assert broken.detail == f"承诺第 {lots['s1']} 件最多出 40，实际出价 41"


def test_an_english_exit_price_counts_as_a_bid(night: Any) -> None:
    lots, _, records = night
    keyed = by_key(records)
    assert keyed[(5, lots["e1"], "no_bid")].outcome == "violated"
    assert "实际退出价 25" in keyed[(5, lots["e1"], "no_bid")].detail
    assert keyed[(6, lots["e1"], "no_bid")].outcome == "kept"


def test_a_dutch_grab_price_counts_as_a_bid(night: Any) -> None:
    lots, _, records = night
    broken = by_key(records)[(1, lots["d1"], "max_bid")]
    assert broken.outcome == "violated"
    assert "实际出手价 21" in broken.detail


def test_let_win_is_broken_only_when_the_pledger_wins(night: Any) -> None:
    lots, transcript, records = night
    keyed = by_key(records)
    winner = next(
        e.data.winner
        for e in transcript.events
        if isinstance(e, HammerEvent) and e.lot == lots["l2"]
    )
    assert winner == 2
    assert keyed[(2, lots["l2"], "let_win")].outcome == "violated"
    assert "自己却拍下了" in keyed[(2, lots["l2"], "let_win")].detail
    assert keyed[(4, lots["l2"], "let_win")].outcome == "kept"


def test_let_win_is_kept_when_the_lot_goes_unsold(night: Any) -> None:
    lots, transcript, records = night
    hammer = next(
        e.data for e in transcript.events if isinstance(e, HammerEvent) and e.lot == lots["l3"]
    )
    assert hammer.unsold
    assert by_key(records)[(6, lots["l3"], "let_win")].outcome == "kept"


def test_records_follow_the_talk_events(night: Any) -> None:
    _, transcript, records = night
    talks = [e for e in transcript.events if isinstance(e, TalkEvent) and e.data.pledge]
    assert [(r.seq, r.lot, r.seat, r.pledge) for r in records] == [
        (e.seq, e.lot, e.data.seat, e.data.pledge) for e in talks
    ]
    assert all(r.intent == "unknown" for r in records)


def test_every_pledge_gets_exactly_one_verdict(night: Any) -> None:
    _, _, records = night
    assert len(records) == 10
    assert sorted(r.outcome for r in records).count("violated") == 5
    assert sorted(r.outcome for r in records).count("void") == 0


# ------------------------------------------------------------------------------- void


def exhausted_night() -> tuple[Any, int, int]:
    """A night in which seat 1 spends its whole budget early, then pledges about a later lot."""
    config = scripted_config(budget_c=60)
    for number in range(1, 40):
        seed = f"show-void-{number}"
        the_deal = deal(config, seed=seed)
        burn = next(
            (
                lot.lot
                for lot in the_deal.lots
                if lot.format in ("sealed_first", "all_pay") and lot.lot < len(the_deal.lots) - 1
            ),
            None,
        )
        if burn is None:
            continue
        later = burn + 1
        script = Script(
            bids={(burn, 1): 100_000},
            pledges={(burn, 1): Pledge(kind="no_bid", lot=later)},
        )
        _, _, transcript = play_script(script, seed=seed, config=config)
        opened = next(
            e for e in transcript.events if isinstance(e, LotOpenEvent) and e.lot == later
        )
        if 1 in opened.data.sitting_out:
            return transcript, burn, later
    raise AssertionError("no seed in the range exhausted seat 1's budget before the next lot")


def test_a_pledge_about_a_lot_the_seat_cannot_bid_in_is_void() -> None:
    transcript, burn, later = exhausted_night()
    (record,) = judge_pledges(transcript.events)
    assert (record.lot, record.pledge.lot, record.seat) == (burn, later, 1)
    assert record.outcome == "void"
    assert "预算已用尽" in record.detail


def test_a_pledge_about_a_lot_that_was_not_played_is_void() -> None:
    transcript, burn, later = exhausted_night()
    cut = next(
        index
        for index, e in enumerate(transcript.events)
        if isinstance(e, LotOpenEvent) and e.lot == later
    )
    (record,) = judge_pledges(transcript.events[:cut])
    assert record.outcome == "void"
    assert "没有开拍" in record.detail


# ----------------------------------------------------------------------------- wording


@pytest.mark.parametrize(
    ("pledge", "text"),
    [
        (Pledge(kind="no_bid", lot=7), "第 7 件不出价"),
        (Pledge(kind="max_bid", lot=3, amount=40), "第 3 件最多出 40"),
        (Pledge(kind="let_win", lot=9, to=2), "让 2 号拿下第 9 件"),
    ],
)
def test_pledge_text(pledge: Pledge, text: str) -> None:
    assert pledge_text(pledge) == text


def test_the_word_for_a_number_follows_the_format() -> None:
    assert bid_word("english") == "退出价"
    assert bid_word("dutch") == "出手价"
    assert {bid_word(f) for f in ("sealed_first", "sealed_second", "all_pay", "blind_box")} == {
        "出价"
    }
