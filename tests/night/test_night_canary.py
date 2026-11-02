"""The canary test: private numbers and words never reach anyone who may not see them.

The deal of the night is rewritten so that every private thing is a unique marker: each seat gets
a budget, values and appraisals that no other number of the night equals, and a random word as
the canary of its mission. The English exit prices, the Dutch grab prices and the sealed bids
are unique markers too. Then a whole night is played, and every event and every request is
searched:

* no event that a seat may not see (``visible_to``) holds a marker of that seat;
* no public event holds a canary or a budget at all (nor what is left of a budget after any
  hammer), and none holds a value or an appraisal before the night ends;
* the true value of a blind box is in no event before its hammer, and in no event but the hammer,
  and in no request at all;
* a sealed bid is in no event before the hammer of its lot except the action of its owner, and in
  no request;
* a Dutch grab price and the exit price of the English winner are in no public event;
* the public part of a request holds no marker, and the facts of a seat hold no marker of
  another seat, nor the exit price of a seat that is still in.

A check that finds nothing proves nothing, so the same search is run on tampered copies to see
that it does find a leak.

The search runs on the mock night and again on other tables (fewer seats, bots, a short night, a
reserve price, a question after every English exit, other numbers of Dutch levels, no plans). The
keys that decide ties and the missions of the seats are not numbers that can be marked, so they
are checked by their shape: the field of the keys, and the fields of a mission, appear in no
public event before the missions are decided.
"""

import dataclasses
import json
import random
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

import pytest
from night_support import StandIn, make_config, play

from auction_night.bots import TruthfulBot
from auction_night.contracts import AnyEvent, NightConfig, canonical_json
from auction_night.contracts.events import (
    CollectorMission,
    ShillMission,
    TalkData,
    TalkEvent,
    visible_to,
)
from auction_night.engine.deal import Deal, deal
from auction_night.engine.night import Answer, Request

CONFIG = make_config()
SEATS = tuple(range(1, 9))


def pattern_of(tokens: Iterable[str]) -> re.Pattern[str]:
    """A pattern for any of ``tokens`` as a whole word or whole number."""
    words = "|".join(re.escape(t) for t in sorted(tokens, key=len, reverse=True)) or "(?!)"
    return re.compile(rf"(?<![0-9A-Za-z])(?:{words})(?![0-9A-Za-z])")


def token_in(text: str, token: str) -> bool:
    """True when ``token`` occurs as a whole word or whole number in ``text``."""
    return pattern_of([token]).search(text) is not None


@dataclass
class Marked:
    """A deal in which every private thing is a unique marker, and the markers themselves."""

    deal: Deal
    pool: random.Random
    secrets: dict[int, set[str]] = field(default_factory=dict)  # per seat: budget, cards, canary
    budgets: dict[int, str] = field(default_factory=dict)
    canaries: dict[int, str] = field(default_factory=dict)
    true_values: dict[int, str] = field(default_factory=dict)  # lot -> hidden value
    exits: dict[tuple[int, int], int] = field(default_factory=dict)  # (lot, seat) -> exit price
    grabs: dict[tuple[int, int], int] = field(default_factory=dict)  # (lot, seat) -> grab price
    sealed: dict[tuple[int, int], int] = field(default_factory=dict)  # (lot, seat) -> sealed bid
    taken: set[int] = field(default_factory=set)
    seats: tuple[int, ...] = SEATS

    def bid_marker(self) -> int:
        """A fresh six-digit number for a bid that must stay hidden."""
        while True:
            marker = self.pool.randrange(100_000, 1_000_000)
            if marker not in self.taken:
                self.taken.add(marker)
                return marker


def marked_deal(seed: str, config: NightConfig = CONFIG) -> Marked:
    base = deal(config, seed=seed)
    seats = tuple(range(1, base.n_seats + 1))
    pool = random.Random(f"canary:{seed}")
    private = iter(pool.sample(range(1_000_000, 8_000_000), 4000))
    budgets = {seat: 8_000_000 + 1_000 * seat + pool.randrange(1_000) for seat in seats}
    marked = Marked(deal=base, pool=pool, seats=seats)

    lots = []
    for lot in base.lots:
        if lot.is_blind:
            hidden = next(private)
            marked.true_values[lot.lot] = str(hidden)
            lot = dataclasses.replace(
                lot,
                values=dict.fromkeys(seats, hidden),
                signals={seat: next(private) for seat in seats},
                blind_value=hidden,
            )
        else:
            lot = dataclasses.replace(lot, values={seat: next(private) for seat in seats})
        lots.append(lot)

    missions = {}
    for seat, mission in base.missions.items():
        word = f"CANARY-{pool.getrandbits(96):024x}"
        marked.canaries[seat] = word
        missions[seat] = mission.model_copy(update={"canary": word})
        assert isinstance(missions[seat], CollectorMission | ShillMission)

    marked.deal = dataclasses.replace(base, lots=tuple(lots), budgets=budgets, missions=missions)
    for seat in seats:
        mine = {str(budgets[seat])}
        mine |= {str(lot.signals[seat]) for lot in lots if lot.is_blind}
        mine |= {str(lot.values[seat]) for lot in lots if not lot.is_blind}
        mine |= {marked.canaries[seat]} if seat in marked.canaries else set()
        marked.secrets[seat] = mine
        marked.budgets[seat] = str(budgets[seat])
    return marked


def players(marked: Marked) -> dict[int, object]:
    """Players that bid a unique marker in every lot, so that every number that must stay
    hidden is recognisable."""

    def player(seat: int):
        stand_in = StandIn(seat, TruthfulBot(seat), pledges=False).answer

        def act(request: Request) -> Answer:
            if request.phase in ("plan", "talk"):
                return stand_in(request)
            assert request.lot is not None
            if request.phase == "english_revise":
                return Answer(seat=seat, value=request.facts_of(seat).exit_price)
            if request.phase == "english_init":
                marked.exits[(request.lot, seat)] = marker = marked.bid_marker()
                return Answer(seat=seat, value=marker)
            if request.phase == "dutch":
                marked.grabs[(request.lot, seat)] = marker = marked.bid_marker()
                return Answer(seat=seat, value=marker)
            marked.sealed[(request.lot, seat)] = marker = marked.bid_marker()
            return Answer(seat=seat, value=marker)

        return act

    return {seat: player(seat) for seat in marked.seats}


def request_texts(request: Request) -> tuple[str, dict[int, str]]:
    """The public part of a request as text, and the facts of each asked seat as text."""
    whole = dataclasses.asdict(request)
    facts = {f["seat"]: json.dumps(f) for f in whole.pop("facts")}
    return json.dumps(whole), facts


def find_leaks(
    marked: Marked, events: tuple[AnyEvent, ...], requests: list[Request], states: list[str]
) -> list[str]:
    """Every place where something private shows to someone who may not see it."""
    leaks: list[str] = []
    reveal = next(e.seq for e in events if e.type in ("mission_result", "night_end"))
    blobs = {e.seq: canonical_json(e) for e in events}
    # what is left of each budget after every hammer is as private as the budget itself
    left = {seat: [marked.deal.budgets[seat]] for seat in marked.seats}
    for event in events:
        if event.type == "hammer":
            for seat, paid in event.data.payments.items():
                left[seat].append(left[seat][-1] - paid)
    secrets = {seat: marked.secrets[seat] | {str(x) for x in left[seat]} for seat in marked.seats}
    secret_of = {seat: pattern_of(secrets[seat]) for seat in marked.seats}
    grabs = pattern_of(str(x) for x in marked.grabs.values())
    every_secret = pattern_of(t for items in secrets.values() for t in items)
    hidden_values = pattern_of(marked.true_values.values())
    sealed_bids = pattern_of(str(m) for m in marked.sealed.values())
    budgets_ever = pattern_of(
        [str(x) for xs in left.values() for x in xs] + list(marked.canaries.values())
    )

    for event in events:
        blob = blobs[event.seq]
        for seat in marked.seats:
            if not visible_to(event.vis, f"seat:{seat}") and secret_of[seat].search(blob):
                leaks.append(f"seq {event.seq} ({event.type}) shows a secret of seat {seat}")
        if visible_to(event.vis, "audience"):  # public or audience-only
            if budgets_ever.search(blob):
                leaks.append(f"public seq {event.seq} ({event.type}) holds a budget or a canary")
            if event.seq < reveal and every_secret.search(blob):
                leaks.append(f"public seq {event.seq} ({event.type}) holds a private number")

    public = [e for e in events if e.vis == "public"]
    for event in public:
        if grabs.search(blobs[event.seq]):
            leaks.append(f"a Dutch grab price is in the public seq {event.seq}")
    winners = {
        e.lot: e.data.winner for e in events if e.type == "hammer" and e.data.winner is not None
    }
    for lot, winner in winners.items():
        exit_price = marked.exits.get((lot, winner))
        if exit_price is None:
            continue
        for event in public:
            if token_in(blobs[event.seq], str(exit_price)):
                leaks.append(f"the English winner's exit price of lot {lot} is public")
    for (lot, owner), marker in marked.sealed.items():
        hammer_seq = next(e.seq for e in events if e.type == "hammer" and e.lot == lot)
        for event in events:
            if event.seq >= hammer_seq:
                break
            owned = event.type == "action" and event.data.seat == owner
            if token_in(blobs[event.seq], str(marker)) and not owned:
                leaks.append(f"the sealed bid of seat {owner} in lot {lot} is in seq {event.seq}")
    for lot, hidden in marked.true_values.items():
        hammer = next(e for e in events if e.type == "hammer" and e.lot == lot)
        homes = [e.seq for e in events if token_in(blobs[e.seq], hidden)]
        if homes != [hammer.seq]:
            leaks.append(f"the true value of lot {lot} is in events {homes}, not only the hammer")

    for request in requests:
        public_text, facts = request_texts(request)
        if request.phase == "english_revise":
            assert request.lot is not None
            still_in = {seat: str(marked.exits[(request.lot, seat)]) for seat in request.remaining}
        else:
            still_in = {}
        if (
            every_secret.search(public_text)
            or grabs.search(public_text)
            or pattern_of(still_in.values()).search(public_text)
        ):
            leaks.append(f"request {request.index} holds a private number in its public part")
        if hidden_values.search(public_text) or any(
            hidden_values.search(t) for t in facts.values()
        ):
            leaks.append(f"request {request.index} holds the true value of a blind box")
        if sealed_bids.search(public_text) or any(sealed_bids.search(t) for t in facts.values()):
            leaks.append(f"request {request.index} holds a sealed bid")
        for seat, text in facts.items():
            for other in marked.seats:
                if other != seat and secret_of[other].search(text):
                    leaks.append(f"request {request.index}: seat {seat} sees a secret of {other}")
            others_exits = [v for who, v in still_in.items() if who != seat]
            if pattern_of(others_exits).search(text):
                leaks.append(f"request {request.index}: seat {seat} sees an exit price")
            if grabs.search(text):
                leaks.append(f"request {request.index}: seat {seat} sees a grab price")

    for text in states:
        if every_secret.search(text) or hidden_values.search(text):
            leaks.append("the printed state shows a secret")
    return leaks


def played(
    seed: str, config: NightConfig = CONFIG
) -> tuple[Marked, list[Request], tuple[AnyEvent, ...], list[str]]:
    marked = marked_deal(seed, config)
    transcript = play(config, marked.deal, players(marked))  # type: ignore[arg-type]
    states = [repr(transcript.state)]
    return marked, transcript.requests, transcript.events, states


@pytest.mark.parametrize("seed", ["canary-1", "canary-2", "canary-3"])
def test_nothing_private_reaches_a_public_event_or_the_request_of_another_seat(seed: str) -> None:
    marked, requests, events, states = played(seed)
    assert find_leaks(marked, events, requests, states) == []


def test_the_markers_are_where_they_belong() -> None:
    """The negative of the search above: each marker is in the one place it should be."""
    marked, requests, events, _ = played("canary-1")
    cards = {e.data.seat: canonical_json(e) for e in events if e.type == "deal_private"}
    for seat in SEATS:
        assert token_in(cards[seat], marked.budgets[seat])
        if seat in marked.canaries:
            assert token_in(cards[seat], marked.canaries[seat])
        for secret in marked.secrets[seat]:
            assert token_in(cards[seat], secret)
    assert all(
        sum(token_in(text, w) for text in cards.values()) == 1 for w in marked.canaries.values()
    )
    for lot, hidden in marked.true_values.items():
        hammer = next(e for e in events if e.type == "hammer" and e.lot == lot)
        assert hammer.data.true_value == int(hidden)
    english = {lot for (lot, _) in marked.exits}
    assert english and {lot for (lot, _) in marked.grabs}
    # the facts of a seat hold its own numbers, and a revision round shows only its own exit price
    own = [r for r in requests if r.phase == "english_revise"]
    assert own
    for request in own:
        for facts in request.facts:
            assert facts.exit_price == marked.exits[(request.lot, facts.seat)]
    drops = [e for e in events if e.type == "english_dropout" and e.data.participated]
    assert drops and all(e.data.price == marked.exits[(e.lot, e.data.seat)] for e in drops), (
        "a drop-out announces the exit price of the seat that left"
    )
    first = requests[0]
    assert [f.budget_left for f in first.facts] == [marked.deal.budgets[s] for s in first.seats]


def test_the_search_does_find_a_secret_in_a_public_event() -> None:
    marked, requests, events, states = played("canary-1")
    word = marked.canaries[1]
    talk = next(e for e in events if e.type == "talk")
    leaky = TalkEvent(
        seq=talk.seq,
        lot=talk.lot,
        vis="public",
        data=TalkData(seat=talk.data.seat, text=f"我是{word}"),
    )
    tampered = tuple(leaky if e.seq == talk.seq else e for e in events)
    assert any(
        "holds" in leak or "shows" in leak
        for leak in find_leaks(marked, tampered, requests, states)
    )


def test_the_search_does_find_a_secret_in_the_request_of_another_seat() -> None:
    marked, requests, events, states = played("canary-1")
    sealed = next(
        r for r in requests if r.phase == "sealed" and r.format != "blind_box" and len(r.seats) >= 2
    )
    victim = sealed.seats[1]
    stolen = marked.deal.lot(sealed.lot).values[victim]
    thief = dataclasses.replace(sealed.facts[0], value=stolen)
    bad = dataclasses.replace(sealed, facts=(thief, *sealed.facts[1:]))
    swapped = [bad if r.index == sealed.index else r for r in requests]
    assert any("sees a secret" in leak for leak in find_leaks(marked, events, swapped, states))


def test_the_search_does_find_a_hidden_bid_in_a_public_request() -> None:
    marked, requests, events, states = played("canary-1")
    revise = next(r for r in requests if r.phase == "english_revise")
    hidden = marked.exits[(revise.lot, revise.remaining[0])]
    bad = dataclasses.replace(revise, price=hidden)
    swapped = [bad if r.index == revise.index else r for r in requests]
    assert any("public part" in leak for leak in find_leaks(marked, events, swapped, states))


def test_the_search_does_find_what_is_left_of_a_budget_in_the_request_of_another_seat() -> None:
    marked, requests, events, states = played("canary-1")
    paid = dict.fromkeys(marked.seats, 0)
    spent_by: dict[int, dict[int, int]] = {}  # lot -> money paid before it
    for event in events:
        if event.type == "hammer":
            spent_by[event.lot + 1] = dict(paid)
            for seat, amount in event.data.payments.items():
                paid[seat] += amount
    for request in requests:
        if request.phase != "sealed" or len(request.seats) < 2 or request.lot not in spent_by:
            continue
        victim = request.seats[1]
        if spent_by[request.lot][victim] == 0:
            continue  # nothing paid yet: the initial budget would be found anyway
        left = marked.deal.budgets[victim] - spent_by[request.lot][victim]
        stolen = dataclasses.replace(request.facts[0], budget_left=left)
        bad = dataclasses.replace(request, facts=(stolen, *request.facts[1:]))
        swapped = [bad if r.index == request.index else r for r in requests]
        assert any("sees a secret" in leak for leak in find_leaks(marked, events, swapped, states))
        return
    raise AssertionError("no later lot in which a seat had paid something and two seats bid")


def test_the_search_does_find_the_true_value_of_a_blind_box_in_a_request() -> None:
    marked, requests, events, states = played("canary-1")
    box = next(r for r in requests if r.phase == "sealed" and r.format == "blind_box")
    hidden = int(marked.true_values[box.lot])
    shown = dataclasses.replace(box.facts[0], value=hidden)
    bad = dataclasses.replace(box, facts=(shown, *box.facts[1:]))
    swapped = [bad if r.index == box.index else r for r in requests]
    assert any("true value" in leak for leak in find_leaks(marked, events, swapped, states))
    public = dataclasses.replace(box, price=hidden)  # the same number in the public part
    swapped = [public if r.index == box.index else r for r in requests]
    assert any(
        "true value" in leak or "public part" in leak
        for leak in find_leaks(marked, events, swapped, states)
    )


def test_the_search_does_find_a_sealed_bid_before_the_hammer_and_in_a_request() -> None:
    marked, requests, events, states = played("canary-1")
    (lot, _owner), bid = next(iter(marked.sealed.items()))
    assert find_leaks(marked, events, requests, states) == []
    talk = next(e for e in events if e.type == "talk" and e.lot == lot)
    leaky = TalkEvent(
        seq=talk.seq,
        lot=talk.lot,
        vis="public",
        data=TalkData(seat=talk.data.seat, text=f"听说有人出{bid}"),
    )
    tampered = tuple(leaky if e.seq == talk.seq else e for e in events)
    assert any("sealed bid" in leak for leak in find_leaks(marked, tampered, requests, states))
    sealed = next(r for r in requests if r.phase == "sealed" and r.lot == lot and len(r.seats) > 1)
    shown = dataclasses.replace(sealed.facts[0], value=bid)
    bad = dataclasses.replace(sealed, facts=(shown, *sealed.facts[1:]))
    swapped = [bad if r.index == sealed.index else r for r in requests]
    assert any("sealed bid" in leak for leak in find_leaks(marked, events, swapped, states))


def inject(events: tuple[AnyEvent, ...], text: str) -> tuple[AnyEvent, ...]:
    """The log with the first line of the public channel replaced by ``text``."""
    talk = next(e for e in events if e.type == "talk")
    leaky = TalkEvent(
        seq=talk.seq, lot=talk.lot, vis="public", data=TalkData(seat=talk.data.seat, text=text)
    )
    return tuple(leaky if e.seq == talk.seq else e for e in events)


@pytest.mark.parametrize("what", ["budget", "value", "signal", "grab", "winner_exit", "true_value"])
def test_the_search_catches_every_kind_of_private_number_in_a_public_line(what: str) -> None:
    marked, requests, events, states = played("canary-2")
    lots = marked.deal.lots
    english_hammer = next(
        e for e in events if e.type == "hammer" and e.data.format == "english" and e.data.winner
    )
    token = {
        "budget": marked.budgets[3],
        "value": str(next(lot for lot in lots if not lot.is_blind).values[3]),
        "signal": str(next(lot for lot in lots if lot.is_blind).signals[2]),
        "grab": str(next(iter(marked.grabs.values()))),
        "winner_exit": str(marked.exits[(english_hammer.lot, english_hammer.data.winner)]),
        "true_value": next(iter(marked.true_values.values())),
    }[what]
    clean = find_leaks(marked, events, requests, states)
    assert clean == []
    assert find_leaks(marked, inject(events, f"数字{token}"), requests, states)


# ------------------------------------------------- other tables, other rules, other secrets


def table(*kinds: str) -> list[dict]:
    """Seat dictionaries for a table of model seats (``mock``) and rule bots (``bot``)."""
    seats = []
    for number, kind in enumerate(kinds, start=1):
        seat: dict = {"seat": number, "codename": f"座位{number}", "kind": kind}
        if kind == "bot":
            seat["policy"] = "economist" if number % 2 else "truthful"
        else:
            seat["mock_policy"] = "honest"
        seats.append(seat)
    return seats


SHORT_NIGHT = {
    "formats": ["english", "dutch", "sealed_first", "sealed_second"],
    "lots_per_format": 1,
    "tiers": {100: 2, 200: 1, 400: 1},
}

SHAPES = {
    "five seats, two of them bots": make_config(
        table("mock", "mock", "mock", "bot", "bot"), missions={"collectors": 2, "shills": 1}
    ),
    "eight models": make_config(table(*["mock"] * 8), missions={"collectors": 4, "shills": 4}),
    "a reserve, a question after every exit, five Dutch levels": make_config(
        reserve=60, english={"revise_when_remaining_le": 8}, dutch={"levels": 5}
    ),
    "four lots, two seats": make_config(
        table("mock", "mock"),
        missions={"collectors": 1, "shills": 1, "collector_need": 1},
        **SHORT_NIGHT,
    ),
    "variety mode without plans and guesses": make_config(
        mode="variety", plan_and_mindreading=False
    ),
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_nothing_private_leaks_at_other_tables_and_under_other_rules(shape: str) -> None:
    marked, requests, events, states = played(f"canary-{len(shape)}", SHAPES[shape])
    assert marked.exits and marked.grabs, "the markers of both clock formats were played"
    assert find_leaks(marked, events, requests, states) == []


def test_the_tie_keys_are_in_no_event_and_in_no_request() -> None:
    marked, requests, events, states = played("canary-1")
    blobs = [canonical_json(e) for e in events]
    blobs += [json.dumps(dataclasses.asdict(r), default=str) for r in requests] + states
    keys = [canonical_json(lot.tie_key) for lot in marked.deal.lots]
    for blob in blobs:
        assert "tie_key" not in blob, "the field that holds the keys is never published"
        assert not any(key in blob for key in keys), "nor is the order of any lot"
    # ... while the tied seats of a hammer are, as they should be
    assert any('"tied":[' in blob and '"tied":[]' not in blob for blob in blobs)


def test_missions_are_private_until_the_last_lot_is_sold() -> None:
    _, _, events, _ = played("canary-1")
    reveal = next(e.seq for e in events if e.type == "mission_result")
    kinds = re.compile(r'"kind":"(?:collector|shill)"|"threshold_price"|"need":|"canary"')
    public_before = [e for e in events if e.vis == "public" and e.seq < reveal]
    assert public_before and not [e.type for e in public_before if kinds.search(canonical_json(e))]
    after = [e for e in events if e.type == "mission_result"]
    assert after and all(kinds.search(canonical_json(e)) for e in after)
    # ... and every mission is on the private card of its holder alone
    for event in (e for e in events if e.type == "deal_private"):
        others = [m for m in events if m.type == "deal_private" and m.data.seat != event.data.seat]
        if event.data.mission is not None:
            word = event.data.mission.canary
            assert not any(word in canonical_json(m) for m in others)
