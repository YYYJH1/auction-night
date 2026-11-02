"""The shape of a night: opening events, the order inside a lot, requests, budgets and outs."""

from collections import Counter
from dataclasses import replace

import pytest
from night_support import Transcript, make_config, play, play_seed, players_for

from auction_night.contracts import AnyEvent, dumps_events, loads_events, validate_log
from auction_night.contracts.events import (
    EVENT_TYPES,
    ActionEvent,
    HammerEvent,
    LotOpenEvent,
    TalkEvent,
    seat_vis,
    seed_commitment,
    vis_kind,
)
from auction_night.contracts.mechanism import MECHANISMS
from auction_night.engine.deal import deal
from auction_night.engine.formats import dutch_ladder
from auction_night.engine.night import guess_target, start, talk_order

CONFIG = make_config()
SEATS = tuple(range(1, 9))
MODEL_SEATS = tuple(range(1, 7))
BOTS = (7, 8)

# The visibility level of every event type the night engine writes. The show layer writes
# the others (host lines, judges, awards) and the model layer writes the system events.
ENGINE_EVENTS = {
    "night_start": "public",
    "seed_commit": "public",
    "deal_private": "seat",
    "plan": "seat",
    "lot_open": "public",
    "talk": "public",
    "guess": "seat",
    "action": "seat",
    "english_dropout": "public",
    "dutch_result": "public",
    "hammer": "public",
    "mission_result": "public",
    "night_end": "public",
}


def night(seed: str = "flow-1") -> Transcript:
    return play_seed(CONFIG, seed)


def lot_events(events: tuple[AnyEvent, ...], lot: int) -> list[AnyEvent]:
    return [event for event in events if event.lot == lot]


# --------------------------------------------------------------------------------- opening


def test_start_opens_the_night_and_asks_for_the_plans() -> None:
    the_deal = deal(CONFIG, seed="flow-open")
    state, request = start(CONFIG, the_deal)

    assert [event.type for event in state.events] == ["night_start"] + ["deal_private"] * 8
    assert [event.vis for event in state.events[1:]] == [seat_vis(seat) for seat in SEATS]
    assert [event.seq for event in state.events] == list(range(1, 10))

    assert request.phase == "plan" and request.lot is None and request.index == 0
    assert request.seats == MODEL_SEATS
    assert [facts.seat for facts in request.facts] == list(MODEL_SEATS)
    assert [facts.budget_left for facts in request.facts] == [
        the_deal.budgets[s] for s in MODEL_SEATS
    ]
    assert all(f.value is None and f.signal is None and f.exit_price is None for f in request.facts)
    assert [slot.lot for slot in request.upcoming] == list(range(1, 13))
    assert (request.n_lots, request.n_seats, request.mode) == (12, 8, "plain")
    assert state.pending == request
    assert state.stage == "plan" and not state.done and state.lot == 0
    assert state.n_requests == 1


def test_night_start_describes_the_table_and_the_lots() -> None:
    the_deal = deal(CONFIG, seed="flow-open")
    state, _ = start(CONFIG, the_deal)
    data = state.events[0].data
    assert (data.night_id, data.mode, data.n_lots) == ("mock-0001", "plain", 12)
    assert [(s.seat, s.codename, s.role) for s in data.seats] == [
        (seat.seat, seat.codename, "bot" if seat.seat in BOTS else "player")
        for seat in CONFIG.seats
    ]
    for plan, lot in zip(data.lots, the_deal.lots, strict=True):
        assert (plan.lot, plan.tag, plan.tier, plan.format) == (
            lot.lot,
            lot.tag,
            lot.tier,
            lot.format,
        )
        assert (plan.mechanism, plan.reserve) == (lot.mechanism, lot.reserve)
    assert (data.budget_lo, data.budget_hi) == (150, 450)
    assert (data.collectors, data.shills, data.mission_bonus) == (3, 3, 150)


def test_the_commitment_to_the_seed_is_published_when_the_configuration_has_one() -> None:
    commitment = seed_commitment("mock-seed-0001")
    config = make_config(deal_seed_commitment=commitment, id="mock-0002")
    state, _ = start(config, deal(config))
    kinds = [event.type for event in state.events]
    assert kinds == ["night_start", "seed_commit"] + ["deal_private"] * 8
    data = state.events[1].data
    assert (data.phase, data.commitment, data.seed) == ("commit", commitment, None)
    assert state.events[1].vis == "public"

    # A placeholder is not a commitment: nothing is published.
    pending = make_config(deal_seed_commitment="<to be published>")
    state, _ = start(pending, deal(pending, seed="flow-open"))
    assert "seed_commit" not in [event.type for event in state.events]


def test_lots_get_plain_names_without_a_catalogue_and_the_given_names_with_one() -> None:
    from auction_night.engine.night import ItemInfo

    the_deal = deal(CONFIG, seed="flow-open")
    state, _ = start(CONFIG, the_deal)
    lots = state.events[0].data.lots
    assert [lot.item for lot in lots][:2] == ["lot-01", "lot-02"]

    items = [ItemInfo(f"thing-{k}", f"物件{k}", "🎁", f"说明{k}") for k in range(1, 13)]
    state, _ = start(CONFIG, the_deal, items=items)
    lots = state.events[0].data.lots
    assert [(lot.item, lot.name, lot.emoji, lot.blurb) for lot in lots] == [
        (i.item, i.name, i.emoji, i.blurb) for i in items
    ]
    with pytest.raises(ValueError, match="one item per lot"):
        start(CONFIG, the_deal, items=items[:3])


def test_a_deal_for_another_table_is_refused() -> None:
    other = make_config(
        formats=["english", "dutch", "sealed_first", "sealed_second"],
        lots_per_format=1,
        tiers={100: 2, 200: 1, 400: 1},
        missions={"shills": 2, "collector_need": 1},
    )
    with pytest.raises(ValueError, match="number of lots"):
        start(CONFIG, deal(other, seed="x"))
    smaller = make_config(
        seats=[
            {"seat": 1, "codename": "甲", "kind": "mock", "mock_policy": "honest"},
            {"seat": 2, "codename": "乙", "kind": "mock", "mock_policy": "honest"},
            {"seat": 3, "codename": "丙", "kind": "bot", "policy": "truthful"},
        ],
        missions={"collectors": 1, "shills": 1},
    )
    with pytest.raises(ValueError, match="different table"):
        start(CONFIG, deal(smaller, seed="x"))


# ------------------------------------------------------------------------- the log of a night


def test_every_event_is_a_valid_contract_event_with_the_visibility_of_its_type() -> None:
    events = night().events
    assert validate_log(events) == []
    assert set(event.type for event in events) <= set(ENGINE_EVENTS) <= set(EVENT_TYPES)
    for event in events:
        assert vis_kind(event.vis) == ENGINE_EVENTS[event.type], event.type
        if ENGINE_EVENTS[event.type] == "seat":
            assert event.vis == seat_vis(event.data.seat)
    # One of each opening and closing event, and the log round-trips through its text form.
    counts = Counter(event.type for event in events)
    assert counts["night_start"] == counts["night_end"] == 1
    assert counts["deal_private"] == 8 and counts["plan"] == 6
    assert counts["mission_result"] == 6 and counts["lot_open"] == counts["hammer"] == 12
    assert loads_events(dumps_events(events)) == list(events)


def test_the_opening_plans_come_before_the_first_lot_and_the_closing_events_after_the_last() -> (
    None
):
    events = night().events
    types = [event.type for event in events]
    first_lot = types.index("lot_open")
    assert types[:9] == ["night_start"] + ["deal_private"] * 8
    plans = [e for e in events[9:first_lot] if e.type == "plan"]
    assert [p.data.seat for p in plans] == list(MODEL_SEATS)
    plan_actions = [e for e in events[9:first_lot] if e.type == "action"]
    assert [(a.data.seat, a.data.phase, a.lot) for a in plan_actions] == [
        (seat, "plan", None) for seat in MODEL_SEATS
    ]
    # plan, then its action, seat by seat
    assert [e.type for e in events[9:first_lot]] == ["plan", "action"] * 6
    assert types[-7:] == ["mission_result"] * 6 + ["night_end"]
    assert [e.data.seat for e in events[-7:-1]] == sorted(e.data.seat for e in events[-7:-1])


def test_a_lot_runs_open_talk_bids_hammer_in_that_order() -> None:
    transcript = night()
    the_deal = transcript.state.deal
    events = transcript.events
    for lot in range(1, 13):
        evs = lot_events(events, lot)
        deal_lot = the_deal.lot(lot)
        mech = CONFIG.night.mechanism_for(deal_lot.format)
        opened = evs[0]
        assert isinstance(opened, LotOpenEvent) and opened.vis == "public"
        order = opened.data.talk_order
        pos = 1
        for speaker in order:  # talk, then the guess of a model seat, then the action
            talk = evs[pos]
            assert isinstance(talk, TalkEvent) and talk.data.seat == speaker
            pos += 1
            if speaker in MODEL_SEATS and evs[pos].type == "guess":
                assert evs[pos].data.seat == speaker
                pos += 1
            action = evs[pos]
            assert isinstance(action, ActionEvent)
            assert (action.data.seat, action.data.phase) == (speaker, "talk")
            pos += 1
        rest = evs[pos:]
        assert isinstance(rest[-1], HammerEvent) and rest[-1].data.format == deal_lot.format
        body = [e for e in rest[:-1]]
        if mech.clock == "descending_levels":
            assert body[-1].type == "dutch_result"
            body = body[:-1]
            assert all(e.type == "action" and e.data.phase == "dutch" for e in body)
        elif mech.clock == "none":
            assert all(e.type == "action" and e.data.phase == "sealed" for e in body)
            assert [e.data.seat for e in body] == sorted(e.data.seat for e in body)
        else:
            assert body[0].type == "action" and body[0].data.phase == "english_init"
            assert {e.type for e in body} <= {"action", "english_dropout"}
        assert [e.lot for e in evs] == [lot] * len(evs)


def test_lot_open_announces_who_will_be_asked_and_who_sits_out() -> None:
    transcript = play_seed(make_config(budget_c=60), "flow-outs")
    the_deal = transcript.state.deal
    saw_outsider = False
    for record in transcript.state.records:
        deal_lot = the_deal.lot(record.lot)
        mech = CONFIG.night.mechanism_for(deal_lot.format)
        opened = next(e for e in lot_events(transcript.events, record.lot) if e.type == "lot_open")
        data = opened.data
        assert (data.format, data.tier, data.reserve) == (
            deal_lot.format,
            deal_lot.tier,
            deal_lot.reserve,
        )
        assert data.mechanism == deal_lot.mechanism
        if mech.clock == "descending_levels":
            lowest = dutch_ladder(deal_lot.tier, mech.levels, mech.reserve)[-1]
            expected = [s for s in SEATS if record.budgets_before[s] >= lowest]
        else:
            expected = [s for s in SEATS if record.budgets_before[s] >= 1]
        assert data.active == expected
        assert sorted(data.active + data.sitting_out) == list(SEATS)
        assert sorted(data.talk_order) == list(SEATS)  # a seat that is out still talks
        assert tuple(record.outcome.asked) == tuple(expected)
        saw_outsider |= bool(data.sitting_out)
    assert saw_outsider


# ------------------------------------------------------------------------------ talk order


def test_the_speaking_order_moves_by_one_seat_with_every_lot() -> None:
    assert talk_order(SEATS, 1) == (1, 2, 3, 4, 5, 6, 7, 8)
    assert talk_order(SEATS, 2) == (2, 3, 4, 5, 6, 7, 8, 1)
    assert talk_order(SEATS, 9) == talk_order(SEATS, 1)
    assert talk_order(SEATS, 1, offset=3) == (4, 5, 6, 7, 8, 1, 2, 3)
    firsts = Counter(talk_order(SEATS, lot)[0] for lot in range(1, 13))
    assert max(firsts.values()) <= 2 and set(firsts) == set(SEATS)

    events = night().events
    orders = [e.data.talk_order for e in events if e.type == "lot_open"]
    assert orders == [list(talk_order(SEATS, lot)) for lot in range(1, 13)]


def test_a_seeded_start_of_the_speaking_order() -> None:
    the_deal = deal(CONFIG, seed="flow-1")
    transcript = play(CONFIG, the_deal, talk_offset=5)
    first = next(e for e in transcript.events if e.type == "lot_open")
    assert first.data.talk_order == [6, 7, 8, 1, 2, 3, 4, 5]
    assert transcript.state.talk_offset == 5


def test_talk_is_asked_one_seat_at_a_time_and_bids_all_at_once() -> None:
    transcript = night()
    talk = [r for r in transcript.requests if r.phase == "talk"]
    assert len(talk) == 12 * 8 and all(len(r.seats) == 1 for r in talk)
    for lot in range(1, 13):
        speakers = [r.seats[0] for r in talk if r.lot == lot]
        assert speakers == list(talk_order(SEATS, lot))
    bids = [r for r in transcript.requests if r.phase not in ("plan", "talk")]
    assert all(len(r.seats) >= 1 for r in bids)
    assert [r.index for r in transcript.requests] == list(range(len(transcript.requests)))
    first_bid = {r.lot: r for r in reversed(bids) if r.phase in ("english_init", "sealed", "dutch")}
    for lot, request in first_bid.items():
        assert request.bidders == len(request.seats), lot


def test_every_request_carries_the_public_facts_of_the_lot() -> None:
    transcript = night()
    the_deal = transcript.state.deal
    for request in transcript.requests:
        if request.phase == "plan":
            continue
        deal_lot = the_deal.lot(request.lot)
        assert (request.format, request.tier, request.reserve) == (
            deal_lot.format,
            deal_lot.tier,
            deal_lot.reserve,
        )
        assert [slot.lot for slot in request.upcoming] == list(range(request.lot, 13))
        assert request.upcoming[0].format == deal_lot.format
        assert (request.blind is not None) == (deal_lot.format == "blind_box")
        for facts in request.facts:
            if deal_lot.is_blind:
                assert facts.signal == deal_lot.signals[facts.seat] and facts.value is None
            else:
                assert facts.value == deal_lot.values[facts.seat] and facts.signal is None


# -------------------------------------------------------------------------- mind reading


def test_the_guess_target_rotates_over_seat_plus_lot_and_skips_seats_that_are_out() -> None:
    budgets = dict.fromkeys(SEATS, 100)
    for seat in SEATS:
        seen = [guess_target(seat, lot, budgets) for lot in range(1, 8)]
        assert all(t is not None and t != seat for t in seen)
        assert sorted(seen) == sorted(s for s in SEATS if s != seat)  # all rivals, once each
        again = [guess_target(seat, lot, budgets) for lot in range(8, 15)]
        assert again == seen  # a cycle of seven
    budgets[3] = 0
    budgets[5] = 0
    assert {guess_target(1, lot, budgets) for lot in range(1, 40)} == {2, 4, 6, 7, 8}
    assert guess_target(1, 4, {1: 10, 2: 0, 3: 0}) is None


def test_model_seats_are_asked_to_read_a_rival_and_bots_are_not() -> None:
    transcript = night()
    guessed = 0
    for request in transcript.requests:
        if request.phase != "talk":
            continue
        facts = request.facts[0]
        records = {r.lot: r for r in transcript.state.records}
        left = records[request.lot].budgets_before
        if facts.seat in BOTS:
            assert facts.guess_target is None
        else:
            assert facts.guess_target == guess_target(facts.seat, request.lot, left)
            assert left[facts.guess_target] >= 1
            guessed += 1
    assert guessed == 6 * 12
    # a guess is recorded with the truth at the time: the target's budget before the lot
    truths = transcript.state.guesses
    assert len(truths) == guessed
    for item in truths:
        before = transcript.state.records[item.lot - 1].budgets_before
        assert item.truth == before[item.target] and item.abs_error == abs(item.guess - item.truth)
    events = [e for e in transcript.events if e.type == "guess"]
    assert len(events) == guessed
    for event, item in zip(events, truths, strict=True):
        assert event.vis == seat_vis(item.seat) and event.lot == item.lot
        assert (event.data.seat, event.data.target, event.data.guess) == (
            item.seat,
            item.target,
            item.guess,
        )
        assert "truth" not in event.data.model_dump()


def test_without_plans_and_mind_reading_the_night_starts_at_the_first_speaker() -> None:
    config = make_config(plan_and_mindreading=False)
    the_deal = deal(config, seed="flow-plain")
    state, request = start(config, the_deal)
    assert request.phase == "talk" and request.seats == (1,) and request.lot == 1
    assert [e.type for e in state.events][-1] == "lot_open"
    transcript = play(config, the_deal)
    kinds = Counter(e.type for e in transcript.events)
    assert kinds["plan"] == 0 and kinds["guess"] == 0
    assert all(r.phase != "plan" for r in transcript.requests)
    assert all(r.facts[0].guess_target is None for r in transcript.requests if r.phase == "talk")


# --------------------------------------------------------------------------- budgets, outs


def paid_before(events: tuple[AnyEvent, ...], lot: int) -> dict[int, int]:
    paid = dict.fromkeys(SEATS, 0)
    for event in events:
        if event.type == "hammer" and event.lot is not None and event.lot < lot:
            for seat, amount in event.data.payments.items():
                paid[seat] += amount
    return paid


def test_budgets_are_tracked_from_the_payments_and_seats_run_out() -> None:
    transcript = play_seed(make_config(budget_c=45), "flow-outs-2")
    state = transcript.state
    the_deal = state.deal
    ran_out = set()
    for record in state.records:
        paid = paid_before(state.events, record.lot)
        assert record.budgets_before == {s: the_deal.budgets[s] - paid[s] for s in SEATS}
        assert all(left >= 0 for left in record.budgets_before.values())
        hammer = record.outcome.hammer
        assert set(hammer.payments) <= set(record.outcome.asked)
        assert all(p <= record.budgets_before[s] for s, p in hammer.payments.items())
        after = {s: record.budgets_before[s] - hammer.payments.get(s, 0) for s in SEATS}
        assert hammer.newly_out == sorted(
            s for s in SEATS if record.budgets_before[s] >= 1 and after[s] < 1
        )
        ran_out |= set(hammer.newly_out)
    assert ran_out, "small budgets must produce outs"
    total_paid = paid_before(state.events, 13)
    assert state.budgets == {s: the_deal.budgets[s] - total_paid[s] for s in SEATS}
    assert state.out == tuple(s for s in SEATS if state.budgets[s] < 1)
    assert set(state.out) >= ran_out
    # a seat that is out is never asked to bid again, but keeps speaking
    for later in state.records:
        for seat in ran_out:
            if later.budgets_before[seat] < 1:
                assert seat not in later.outcome.asked
    assert all(len(r.seats) == 1 for r in transcript.requests if r.phase == "talk")
    assert sum(1 for r in transcript.requests if r.phase == "talk") == 12 * 8


def test_seats_that_cannot_afford_the_lowest_dutch_level_are_not_asked() -> None:
    the_deal = deal(CONFIG, seed="flow-dutch")
    poor = {**the_deal.budgets, 1: 5, 2: 9}
    the_deal = replace(the_deal, budgets=poor)
    transcript = play(CONFIG, the_deal)
    dutch_lots = [r.lot for r in transcript.state.records if r.outcome.dutch is not None]
    assert dutch_lots
    for number in dutch_lots:
        opened = next(e for e in lot_events(transcript.events, number) if e.type == "lot_open")
        assert {1, 2} <= set(opened.data.sitting_out) and not {1, 2} & set(opened.data.active)
        asked = [r for r in transcript.requests if r.lot == number and r.phase == "dutch"]
        assert len(asked) == 1 and not {1, 2} & set(asked[0].seats)
        assert not {1, 2} & set(transcript.state.records[number - 1].outcome.asked)
    # In the other formats a budget of 5 still takes part.
    sealed = next(r for r in transcript.requests if r.phase == "sealed")
    assert {1, 2} <= set(sealed.seats)


def test_a_night_nobody_can_bid_in_still_runs_to_the_end() -> None:
    the_deal = deal(CONFIG, seed="flow-broke")
    broke = replace(the_deal, budgets=dict.fromkeys(SEATS, 0))
    transcript = play(CONFIG, broke)
    state = transcript.state
    assert state.done and state.pending is None and state.out == SEATS
    assert [r.phase for r in transcript.requests].count("talk") == 12 * 8
    assert all(r.phase in ("plan", "talk") for r in transcript.requests)
    for record in state.records:
        assert record.outcome.hammer.unsold and record.outcome.asked == ()
    assert transcript.state.guesses == ()
    assert all(
        (s.surplus, s.mission_bonus, s.total, s.spent, s.lots_won) == (0, 0, 0, 0, [])
        for s in state.scores
    )
    assert all(not m.completed for m in state.missions)


# ---------------------------------------------------------------------------- English, Dutch


def test_an_english_lot_asks_for_exit_prices_then_revisions_while_few_seats_remain() -> None:
    transcript = night("flow-english")
    english = [r for r in transcript.state.records if r.outcome.hammer.format == "english"]
    assert english
    for record in english:
        requests = [
            r for r in transcript.requests if r.lot == record.lot and r.phase.startswith("english")
        ]
        assert requests[0].phase == "english_init"
        assert requests[0].seats == tuple(record.outcome.asked)
        assert requests[0].price is None and requests[0].remaining == ()
        revisions = requests[1:]
        if len(record.outcome.asked) < 3:
            assert not revisions  # one exit leaves a single seat, and nobody is asked
            continue
        assert revisions
        sizes = [len(r.remaining) for r in revisions]
        assert all(2 <= size <= 4 for size in sizes) and sizes == sorted(sizes, reverse=True)
        assert [r.price for r in revisions] == sorted(r.price for r in revisions)
        for request in revisions:
            assert request.phase == "english_revise" and request.seats == request.remaining
            assert request.bidders == len(record.outcome.asked)
            assert request.price is not None
            for facts in request.facts:
                assert facts.exit_price is not None and facts.exit_price >= request.price
            # the drop-outs announced before the question are exactly the others' history
            asked_at = next(
                i
                for i, e in enumerate(transcript.events)
                if e.type == "english_dropout"
                and e.lot == record.lot
                and e.data.remaining
                and list(e.data.remaining) == list(request.remaining)
            )
            assert transcript.events[asked_at].data.price == request.price
        # revision answers are recorded as actions before the next drop-outs
        phases = [
            e.data.phase
            for e in lot_events(transcript.events, record.lot)
            if e.type == "action" and e.data.phase.startswith("english")
        ]
        assert phases[: len(record.outcome.asked)] == ["english_init"] * len(record.outcome.asked)
        assert "english_revise" in phases


def test_only_the_dropouts_and_the_hammer_announce_english_prices() -> None:
    transcript = night("flow-english")
    for record in transcript.state.records:
        if record.outcome.hammer.format != "english":
            continue
        hammer = record.outcome.hammer
        evs = lot_events(transcript.events, record.lot)
        public = [e for e in evs if e.vis == "public"]
        assert {e.type for e in public} == {"lot_open", "talk", "english_dropout", "hammer"}
        assert hammer.bids is None  # an English hammer never lists the exit prices
        assert record.outcome.winner_exit is not None
        assert record.outcome.winner_exit >= hammer.price


def test_only_the_hammer_of_a_blind_box_carries_a_true_value() -> None:
    transcript = night("flow-blind")
    the_deal = transcript.state.deal
    hammers = {e.lot: e.data for e in transcript.events if e.type == "hammer"}
    for lot in the_deal.lots:
        assert hammers[lot.lot].true_value == (lot.blind_value if lot.is_blind else None)
    assert sum(lot.is_blind for lot in the_deal.lots) == 2
    # the private cards hold appraisals, never the true value
    for card in the_deal.cards():
        assert set(card.signals) == {lot.lot for lot in the_deal.lots if lot.is_blind}


@pytest.mark.parametrize("fmt", sorted(MECHANISMS))
def test_every_format_is_played_and_decided(fmt: str) -> None:
    transcript = night("flow-formats")
    records = [r for r in transcript.state.records if r.outcome.hammer.format == fmt]
    assert len(records) == 2
    for record in records:
        hammer = record.outcome.hammer
        assert hammer.unsold or hammer.winner in record.outcome.asked


def test_players_for_gives_every_seat_a_player() -> None:
    assert sorted(players_for(CONFIG)) == list(SEATS)


# ----------------------------------------------------------------------- outside events


def test_events_from_outside_are_numbered_after_the_last_one_and_the_night_goes_on() -> None:
    from auction_night.contracts.events import (
        CostEvent,
        CostEventData,
        ParseRepairData,
        ParseRepairEvent,
    )
    from auction_night.engine.night import append_events, step

    config = CONFIG
    the_deal = deal(config, seed="flow-outside")
    players = players_for(config)
    state, request = start(config, the_deal)
    log = list(state.events)
    repair = ParseRepairEvent(
        seq=1,
        lot=None,
        vis="system",
        data=ParseRepairData(role="seat:1", seat=1, phase="plan", codes=["syntax"]),
    )
    cost = CostEvent(
        seq=1, lot=None, vis="system", data=CostEventData(kind="tally", spent_microusd=5)
    )
    outside = 0
    while request is not None:
        answers = {seat: players[seat](request) for seat in request.seats}
        state, added = append_events(state, [repair, cost])
        assert [e.seq for e in added] == [len(log) + 1, len(log) + 2]
        assert [e.type for e in added] == ["parse_repair", "cost_event"]
        assert (added[0].data, added[0].vis) == (repair.data, "system")
        log += added
        outside += 2
        state, events, request = step(state, answers)
        log += events
    assert list(state.events) == log
    assert validate_log(state.events) == []
    assert [e.seq for e in state.events] == list(range(1, len(log) + 1))
    assert sum(e.vis == "system" for e in state.events) == outside
    # the engine's own events are the same as in a night without them, apart from their numbers
    plain = play(config, the_deal, players).events
    kept = [e for e in state.events if e.vis != "system"]
    assert [(e.type, e.lot, e.vis, e.data) for e in kept] == [
        (e.type, e.lot, e.vis, e.data) for e in plain
    ]


def test_only_system_and_audience_events_can_be_added_from_outside() -> None:
    from auction_night.contracts.events import TalkData, TalkEvent
    from auction_night.engine.night import append_events

    state, _ = start(CONFIG, deal(CONFIG, seed="flow-outside"))
    forged = TalkEvent(seq=1, lot=1, vis="public", data=TalkData(seat=1, text="我是系统"))
    with pytest.raises(ValueError, match="written by the engine"):
        append_events(state, [forged])
    assert append_events(state, []) == (state, ())


# ------------------------------------------------------------------- other table shapes


def seats_of(*kinds: str) -> list[dict]:
    names = ["甲", "乙", "丙", "丁", "戊", "己", "庚", "辛"]
    out = []
    for number, kind in enumerate(kinds, start=1):
        seat: dict = {"seat": number, "codename": names[number - 1], "kind": kind}
        if kind == "bot":
            seat["policy"] = "economist" if number % 2 else "truthful"
        else:
            seat["mock_policy"] = "honest"
        out.append(seat)
    return out


@pytest.mark.parametrize(
    ("kinds", "lots", "missions"),
    [
        (("mock", "mock"), "short", {"collectors": 1, "shills": 1, "collector_need": 1}),
        (("mock", "mock", "bot"), "short", {"collectors": 1, "shills": 1, "collector_need": 1}),
        (("bot", "bot", "mock"), "short", {"collectors": 1, "shills": 0, "collector_need": 1}),
        (("mock",) * 8, "full", {"collectors": 4, "shills": 4}),
        (("mock",) * 5 + ("bot",) * 3, "full", {"collectors": 3, "shills": 2}),
    ],
)
def test_other_table_shapes_play_to_the_end(kinds, lots, missions) -> None:
    from auction_night.engine.scoring import conservation_gap

    shape = (
        {"formats": ["english", "dutch", "sealed_first", "sealed_second"], "lots_per_format": 1}
        | {"tiers": {100: 2, 200: 1, 400: 1}}
        if lots == "short"
        else {}
    )
    config = make_config(seats_of(*kinds), missions=missions, **shape)
    the_deal = deal(config, seed=f"shape-{len(kinds)}-{lots}")
    transcript = play(config, the_deal)
    state = transcript.state
    assert state.done and validate_log(state.events) == []
    assert len(state.records) == config.night.n_lots
    assert conservation_gap(state.scores, the_deal.lots, state.hammers) == 0
    models = [seat.seat for seat in config.seats if seat.kind != "bot"]
    ranked = [s.seat for s in state.scores if s.rank_tonight is not None]
    assert ranked == models
    assert {m.seat for m in state.missions} <= set(models)
    plans = [e for e in state.events if e.type == "plan"]
    assert [e.data.seat for e in plans] == models
    assert all(len(e.data.priorities) == config.night.n_lots for e in plans)


def test_variety_nights_pass_the_mode_on_and_the_economist_recites_the_textbook() -> None:
    from auction_night.bots.lines import VARIETY_ECONOMIST

    config = make_config(mode="variety")
    transcript = play_seed(config, "flow-variety")
    assert {r.mode for r in transcript.requests} == {"variety"}
    state = transcript.state
    economist = [e for e in state.events if e.type == "talk" and e.data.seat == 8]
    assert [e.data.text for e in economist] == [
        VARIETY_ECONOMIST[state.deal.lot(e.lot).format] for e in economist
    ]
    truthful = {e.data.text for e in state.events if e.type == "talk" and e.data.seat == 7}
    assert truthful <= {"看着办。", "我就是来买东西的。"}
