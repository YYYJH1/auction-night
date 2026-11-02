"""Laws of the night that hold whatever the seats answer."""

import zlib
from collections import Counter

from engine_support import seeds
from hypothesis import given, settings
from hypothesis import strategies as st
from night_support import make_config, play, replay, wild_players

from auction_night.contracts import dumps_events, loads_events, validate_log, visible_to
from auction_night.contracts.events import CollectorMission, ShillMission
from auction_night.engine.deal import deal
from auction_night.engine.scoring import conservation_gap

CONFIG = make_config()
SEATS = tuple(range(1, 9))


@given(st.integers(0, 10**9))
@settings(max_examples=40, deadline=None)
def test_whatever_the_seats_answer_the_night_stays_lawful(seed: int) -> None:
    the_deal = deal(CONFIG, seed=f"law-{seed}")
    transcript = play(CONFIG, the_deal, wild_players(CONFIG, seed))
    state = transcript.state
    events = state.events

    # the log is a valid log and survives its own text form
    assert validate_log(events) == []
    assert loads_events(dumps_events(events)) == list(events)
    assert state.done and events[-1].type == "night_end"

    # one lot after the other, one hammer each
    hammers = [e for e in events if e.type == "hammer"]
    assert [e.lot for e in hammers] == list(range(1, 13))
    assert Counter(e.type for e in events)["lot_open"] == 12

    # money: nobody pays more than they have, nothing is paid for an unsold lot, budgets stay >= 0
    left = dict(the_deal.budgets)
    for record in state.records:
        assert record.budgets_before == left
        hammer = record.outcome.hammer
        if hammer.unsold:
            assert hammer.winner is None and hammer.payments == {} and hammer.price == 0
        else:
            assert hammer.winner in record.outcome.asked
            assert hammer.payments[hammer.winner] == hammer.price
        for seat, paid in hammer.payments.items():
            assert seat in record.outcome.asked and 0 <= paid <= left[seat]
            left[seat] -= paid
        assert all(budget >= 0 for budget in left.values())
        assert hammer.newly_out == sorted(
            s for s in SEATS if record.budgets_before[s] >= 1 and left[s] < 1
        )
    assert state.budgets == left

    # the books balance
    assert conservation_gap(state.scores, the_deal.lots, state.hammers) == 0

    # nothing private is public, and every seat's own events are its own
    for event in events:
        if event.type in ("deal_private", "plan", "guess", "action"):
            assert event.vis == f"seat:{event.data.seat}"
            assert not visible_to(event.vis, "audience")
        else:
            assert event.vis == "public"

    # the same answers replay to the same night
    assert dumps_events(replay(CONFIG, the_deal, transcript).events) == dumps_events(events)


@given(seeds)
@settings(max_examples=40, deadline=None)
def test_missions_are_decided_the_same_way_when_recounted_from_the_log(seed: str) -> None:
    transcript = play(
        CONFIG, deal(CONFIG, seed=seed), wild_players(CONFIG, zlib.crc32(seed.encode()))
    )
    events = transcript.events
    tags = {
        lot.lot: lot.tag for lot in next(e for e in events if e.type == "night_start").data.lots
    }
    won = {e.lot: e.data.winner for e in events if e.type == "hammer"}
    price = {e.lot: e.data.price for e in events if e.type == "hammer"}
    cards = {e.data.seat: e.data.mission for e in events if e.type == "deal_private"}
    results = {e.data.seat: e.data for e in events if e.type == "mission_result"}
    assert set(results) == {seat for seat, mission in cards.items() if mission is not None}
    for seat, mission in cards.items():
        if mission is None:
            continue
        done = results[seat]
        if isinstance(mission, CollectorMission):
            mine = sorted(
                lot for lot, tag in tags.items() if tag == mission.tag and won[lot] == seat
            )
            assert done.completed == (len(mine) >= mission.need) and done.won_lots == mine
        else:
            assert isinstance(mission, ShillMission)
            sold = won[mission.lot] is not None
            ok = sold and won[mission.lot] != seat and price[mission.lot] >= mission.threshold_price
            assert done.completed == ok
        assert done.bonus == (mission.bonus if done.completed else 0)
