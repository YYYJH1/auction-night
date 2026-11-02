"""Fact cards: byte-stable, complete, and made of nothing but the log and the theory numbers.

The cards of real nights (rule bots at the table next to stand-in model seats) are compared with
an oracle that reads the event log and nothing else: every fact on a card must be in the log,
and every fact of the log that a card is about must be on the card.
"""

from pathlib import Path
from typing import Any

import pytest
from night_support import make_config, play, play_seed, wild_players

from auction_night.contracts.events import (
    ActionEvent,
    DealPrivateEvent,
    DutchResultEvent,
    EnglishDropoutEvent,
    GuessEvent,
    HammerEvent,
    LotOpenEvent,
    MissionResultEvent,
    NightEndEvent,
    NightStartEvent,
    PlanEvent,
    TalkEvent,
)
from auction_night.contracts.mechanism import BID_PHASES
from auction_night.engine.deal import deal
from auction_night.show.cards import LotData
from auction_night.show.factcard import FactCards, NightData, build_fact_cards
from auction_night.show.numbers_check import number_keys, unlisted_numbers
from auction_night.show.pledges import judge_pledges
from auction_night.show.predict import prediction_text
from auction_night.show.render import milli_text
from auction_night.theory import analyse_night

SEEDS = ["show-cards-1", "show-cards-2", "show-cards-3"]


@pytest.fixture(scope="module", params=SEEDS)
def night(request: pytest.FixtureRequest) -> Any:
    transcript = play_seed(make_config(), request.param)
    return transcript.state, build_fact_cards(transcript.state)


# --------------------------------------------------------------------- determinism


def test_the_same_night_gives_byte_identical_cards() -> None:
    first = play_seed(make_config(), "show-cards-1")
    second = play_seed(make_config(), "show-cards-1")
    a, b = build_fact_cards(first.state), build_fact_cards(second.state)
    assert a.digest == b.digest
    for left, right in zip((*a.lots, a.night), (*b.lots, b.night), strict=True):
        assert left.text == right.text
        assert left.data_json() == right.data_json()
        assert left.numbers == right.numbers
        assert left.headline == right.headline


DIGEST_SCRIPT = """
import sys
sys.path[:0] = sys.argv[1:3]
from night_support import make_config, play_seed
from auction_night.show.factcard import build_fact_cards

cards = build_fact_cards(play_seed(make_config(), "show-cards-1").state)
print(cards.digest)
"""


def test_the_digest_does_not_depend_on_the_hash_seed_of_the_process() -> None:
    """Sets and dicts are ordered by the hash seed of a process. The cards must not be."""
    import os
    import subprocess
    import sys

    tests = Path(__file__).resolve().parents[1]
    digests = set()
    for seed in ("1", "2", "31337"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "CUDA_VISIBLE_DEVICES": ""}
        done = subprocess.run(
            [sys.executable, "-c", DIGEST_SCRIPT, str(tests / "engine"), str(tests / "night")],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        digests.add(done.stdout.strip())
    assert len(digests) == 1 and next(iter(digests)).startswith("sha256:")
    state = play_seed(make_config(), "show-cards-1").state
    assert digests == {build_fact_cards(state).digest}


def test_building_the_cards_twice_from_one_state_changes_nothing() -> None:
    state = play_seed(make_config(), "show-cards-1").state
    assert build_fact_cards(state).digest == build_fact_cards(state).digest


def test_another_night_gives_other_cards() -> None:
    a = build_fact_cards(play_seed(make_config(), "show-cards-1").state)
    b = build_fact_cards(play_seed(make_config(), "show-cards-2").state)
    assert a.digest != b.digest


def test_cards_use_theory_numbers_given_or_computed() -> None:
    state = play_seed(make_config(), "show-cards-1").state
    assert (
        build_fact_cards(state, theory=analyse_night(state)).digest
        == build_fact_cards(state).digest
    )


def test_cards_are_built_only_for_a_finished_night() -> None:
    from auction_night.bots import make_bot
    from auction_night.engine.deal import deal
    from auction_night.engine.night import start

    config = make_config()
    state, _ = start(config, deal(config, seed="show-cards-1"))
    with pytest.raises(ValueError, match="over"):
        build_fact_cards(state)
    assert make_bot  # the import documents that bots are not needed to see this error


def test_a_wrong_number_of_theory_entries_is_refused() -> None:
    state = play_seed(make_config(), "show-cards-1").state
    with pytest.raises(ValueError, match="reference numbers"):
        build_fact_cards(state, theory=analyse_night(state)[:-1])


# ------------------------------------------------------------------- the log, the oracle


class Oracle:
    """What the log says, read without using the engine's state or the card builder."""

    def __init__(self, events: Any) -> None:
        self.events = list(events)
        self.start = next(e.data for e in self.events if isinstance(e, NightStartEvent))
        self.deal = {e.data.seat: e.data for e in self.events if isinstance(e, DealPrivateEvent)}
        self.plan = {e.data.seat: e.data for e in self.events if isinstance(e, PlanEvent)}
        self.opens = {e.lot: e.data for e in self.events if isinstance(e, LotOpenEvent)}
        self.talk = {(e.lot, e.data.seat): e.data for e in self.events if isinstance(e, TalkEvent)}
        self.hammer = {e.lot: e.data for e in self.events if isinstance(e, HammerEvent)}
        self.dropouts: dict[int, list[Any]] = {}
        for e in self.events:
            if isinstance(e, EnglishDropoutEvent):
                self.dropouts.setdefault(e.lot, []).append(e.data)
        self.dutch = {e.lot: e.data for e in self.events if isinstance(e, DutchResultEvent)}
        self.acts: dict[tuple[int, int], list[Any]] = {}
        for e in self.events:
            if isinstance(e, ActionEvent) and e.data.phase in BID_PHASES:
                self.acts.setdefault((e.lot, e.data.seat), []).append(e.data)
        self.guess = {
            (e.lot, e.data.seat): e.data for e in self.events if isinstance(e, GuessEvent)
        }
        self.missions = [e.data for e in self.events if isinstance(e, MissionResultEvent)]
        self.end = next(e.data for e in self.events if isinstance(e, NightEndEvent))
        self.codename = {s.seat: s.codename for s in self.start.seats}
        self.role = {s.seat: s.role for s in self.start.seats}

    def budget_before(self, lot: int, seat: int) -> int:
        spent = sum(h.payments.get(seat, 0) for number, h in self.hammer.items() if number < lot)
        return self.deal[seat].budget - spent


def test_every_seat_line_is_what_the_log_says(night: Any) -> None:
    state, cards = night
    log = Oracle(state.events)
    for card in cards.lots:
        data: LotData = card.data
        lot, hammer = data.lot, log.hammer[data.lot]
        assert data.bidders == tuple(sorted(log.opens[lot].active))
        assert data.sitting_out == tuple(sorted(log.opens[lot].sitting_out))
        assert [line.seat for line in data.seats] == sorted(log.codename)
        for line in data.seats:
            seat = line.seat
            asked = seat in log.opens[lot].active
            assert (line.codename, line.role) == (log.codename[seat], log.role[seat])
            assert line.asked == asked
            assert line.budget_before == log.budget_before(lot, seat)
            assert line.paid == hammer.payments.get(seat, 0)
            assert line.budget_after == line.budget_before - line.paid
            assert line.won == (hammer.winner == seat)
            talk = log.talk[(lot, seat)]
            assert (line.said, line.pledge) == (talk.text, talk.pledge)
            card_private = log.deal[seat]
            if asked and lot in card_private.values:
                assert (line.value, line.signal) == (card_private.values[lot], None)
            elif asked:
                assert (line.value, line.signal) == (None, card_private.signals[lot])
            else:
                assert (line.value, line.signal) == (None, None)
            if seat in log.plan:
                assert line.plan_stars == log.plan[seat].priorities[lot - 1]
                assert line.plan_budget == log.plan[seat].budget_plan[lot - 1]
            else:
                assert (line.plan_stars, line.plan_budget) == (None, None)
            logged = log.acts.get((lot, seat), [])
            assert [(a.phase, a.status, a.reason, a.value, a.raw_value) for a in line.actions] == [
                (a.phase, a.status, a.reason, a.value, a.raw_value) for a in logged
            ]


def test_what_each_seat_played_is_what_the_log_says(night: Any) -> None:
    state, cards = night
    log = Oracle(state.events)
    for card in cards.lots:
        data: LotData = card.data
        hammer = log.hammer[data.lot]
        for line in data.seats:
            if not line.asked or line.abstained:
                assert line.played is None
                continue
            if data.format in ("sealed_first", "sealed_second", "all_pay", "blind_box"):
                assert hammer.bids is not None
                assert line.played == hammer.bids[line.seat]
            elif data.format == "dutch":
                assert line.played == log.acts[(data.lot, line.seat)][-1].value
            else:
                left = {d.seat: d for d in log.dropouts.get(data.lot, [])}
                if line.seat in left and not left[line.seat].participated:
                    assert line.played == 0
                elif line.won:
                    last = [a for a in log.acts[(data.lot, line.seat)] if a.value is not None]
                    assert line.played == last[-1].value == data.winner_exit
                else:
                    assert line.played == left[line.seat].price


def test_the_result_of_each_lot_is_the_hammer(night: Any) -> None:
    state, cards = night
    log = Oracle(state.events)
    for card in cards.lots:
        data: LotData = card.data
        hammer = log.hammer[data.lot]
        assert (data.winner, data.price, data.unsold) == (
            hammer.winner,
            hammer.price,
            hammer.unsold,
        )
        assert data.payments == hammer.payments
        assert (data.tied, data.newly_out) == (tuple(hammer.tied), tuple(hammer.newly_out))
        assert data.true_value == hammer.true_value
        assert list(data.dropouts) == log.dropouts.get(data.lot, [])
        assert data.dutch == log.dutch.get(data.lot)
        assert (data.prior is not None) == (data.format == "blind_box")
        assert data.reserve == log.opens[data.lot].reserve


def test_mind_reading_guesses_carry_the_truth(night: Any) -> None:
    state, cards = night
    log = Oracle(state.events)
    for card in cards.lots:
        data: LotData = card.data
        for line in data.seats:
            logged = log.guess.get((data.lot, line.seat))
            if logged is None:
                assert line.guess is None
                continue
            truth = log.budget_before(data.lot, logged.target)
            assert line.guess is not None
            assert (line.guess.target, line.guess.guess, line.guess.truth) == (
                logged.target,
                logged.guess,
                truth,
            )
            assert line.guess.abs_error == abs(logged.guess - truth)


def test_theory_numbers_are_the_theory_modules(night: Any) -> None:
    state, cards = night
    for card, theory in zip(cards.lots, analyse_night(state), strict=True):
        data: LotData = card.data
        assert data.to_highest_value == theory.to_highest_value
        assert data.curse_gap == theory.curse_gap
        assert data.expected_revenue_milli == theory.expected_revenue_milli
        for line in data.seats:
            assert line.ref_bid == theory.ref_bids.get(line.seat)
            assert line.excess == theory.excess.get(line.seat)
            assert line.regret == theory.regret.get(line.seat)
            assert line.alpha_milli == theory.alpha_milli.get(line.seat)
    economist = next(
        line for card in cards.lots for line in card.data.seats if line.alpha_milli is not None
    )
    assert economist.seat == 8 and economist.role == "bot"


def test_the_night_card_holds_scores_missions_and_pledges(night: Any) -> None:
    state, cards = night
    log = Oracle(state.events)
    data: NightData = cards.night.data
    assert [
        (r.seat, r.surplus, r.mission_bonus, r.total, r.rank_tonight, r.spent) for r in data.seats
    ] == [
        (s.seat, s.surplus, s.mission_bonus, s.total, s.rank_tonight, s.spent)
        for s in log.end.scores
    ]
    assert [r.lots_won for r in data.seats] == [tuple(s.lots_won) for s in log.end.scores]
    assert list(data.missions) == log.missions
    assert list(data.pledges) == judge_pledges(state.events)
    assert [(r.lot, r.winner, r.price) for r in data.lots] == [
        (lot, log.hammer[lot].winner, log.hammer[lot].price) for lot in sorted(log.hammer)
    ]
    assert data.n_lots == len(cards.lots) == log.start.n_lots
    for row in data.seats:
        mine = [p for p in data.pledges if p.seat == row.seat]
        assert row.pledges_kept == sum(p.outcome == "kept" for p in mine)
        assert row.pledges_violated == sum(p.outcome == "violated" for p in mine)
        assert row.pledges_void == sum(p.outcome == "void" for p in mine)
        assert row.guesses == sum(1 for (_, seat) in log.guess if seat == row.seat)


def test_missions_appear_in_the_lots_they_can_move(night: Any) -> None:
    state, cards = night
    tags = {lot.lot: lot.tag for lot in state.deal.lots}
    for card in cards.lots:
        data: LotData = card.data
        for line in data.missions:
            mission = state.deal.missions[line.seat]
            if line.kind == "collector":
                assert tags[data.lot] == mission.tag
            else:
                assert mission.lot == data.lot
    shills = {s: m for s, m in state.deal.missions.items() if m.kind == "shill"}
    for seat, mission in shills.items():
        lines = [m for m in cards.lot(mission.lot).data.missions if m.seat == seat]
        assert len(lines) == 1 and lines[0].threshold == mission.threshold_price


# ---------------------------------------------------------------- numbers, headlines


def int_leaves(node: Any, skip: frozenset[str], key: str = "") -> set[int]:
    """Every integer of a dumped card (absolute values), except the fields named in ``skip``."""
    if key in skip or isinstance(node, bool):
        return set()
    if isinstance(node, int):
        return {abs(node)}
    if isinstance(node, dict):
        return {n for k, v in node.items() for n in int_leaves(v, skip, str(k))}
    if isinstance(node, list):
        return {n for v in node for n in int_leaves(v, skip, key)}
    return set()


def string_leaves(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for v in node.values() for s in string_leaves(v)]
    if isinstance(node, list):
        return [s for v in node for s in string_leaves(v)]
    return []


SHOWN_AS_DECIMALS = frozenset({"alpha_milli", "expected_revenue_milli"})
NOT_SHOWN = frozenset({"seq"})


def data_keys(data: Any) -> set[str]:
    """The number keys that the structured data of a card can account for."""
    dumped = data.model_dump(mode="json")
    # The outcome of a pledge belongs to the card of the lot it is about, so that the card of
    # the lot where it was made does not give the end away.
    for record in dumped.get("pledges_made", []):
        record["detail"] = ""
    keys = {str(n) for n in int_leaves(dumped, SHOWN_AS_DECIMALS | NOT_SHOWN)}
    for text in string_leaves(dumped):
        keys |= number_keys(text)

    def decimals(node: Any) -> None:
        if isinstance(node, dict):
            for name, value in node.items():
                if name in SHOWN_AS_DECIMALS and isinstance(value, int):
                    keys.update(number_keys(milli_text(value)))
                else:
                    decimals(value)
        elif isinstance(node, list):
            for value in node:
                decimals(value)

    decimals(dumped)
    return keys


def test_card_numbers_are_the_numbers_of_its_text(night: Any) -> None:
    _, cards = night
    for card in (*cards.lots, cards.night):
        assert card.numbers == number_keys(card.text)


def night_extras(data: NightData) -> set[str]:
    """Numbers of the night card that are counts or fixed text, not fields of its data."""
    outcomes = [p.outcome for p in data.pledges]
    counts = {len(outcomes), *(outcomes.count(o) for o in ("kept", "violated", "void"))}
    return {str(n) for n in counts} | number_keys(prediction_text())


def test_no_number_is_made_up(night: Any) -> None:
    """Every number of a card's text is a number of its data, a count of its pledges, a number
    of the fixed prediction text, or the 1 of a fixed phrase."""
    _, cards = night
    for card in cards.lots:
        assert card.numbers <= data_keys(card.data) | {"1"}, card.lot
    night_card = cards.night
    allowed = data_keys(night_card.data) | night_extras(night_card.data) | {"1"}
    assert night_card.numbers <= allowed


def test_no_fact_is_left_out(night: Any) -> None:
    """Every number of a lot's data is on the card's text."""
    _, cards = night
    for card in cards.lots:
        assert data_keys(card.data) - {"1"} <= card.numbers | set(), card.lot


def test_the_headline_uses_only_numbers_of_its_card(night: Any) -> None:
    _, cards = night
    for card in (*cards.lots, cards.night):
        assert unlisted_numbers(card.headline, card.numbers) == [], (card.kind, card.lot)
        assert card.headline


def test_a_lot_headline_is_one_line(night: Any) -> None:
    _, cards = night
    assert all("\n" not in card.headline for card in cards.lots)


def test_cards_hold_no_canary_and_no_monologue(night: Any) -> None:
    state, cards = night
    secrets = [m.canary for m in state.deal.missions.values()]
    thoughts = [
        e.data.thought for e in state.events if isinstance(e, ActionEvent) and e.data.thought
    ]
    assert secrets and thoughts
    blob = "".join(card.text + card.data_json() for card in (*cards.lots, cards.night))
    assert not any(secret in blob for secret in secrets)
    assert not any(thought in blob for thought in thoughts)


def test_cards_are_god_view(night: Any) -> None:
    state, cards = night
    text = cards.lot(1).text
    blind = next(c for c in cards.lots if c.data.format == "blind_box")
    assert "真值" in blind.text and "鉴定价" in blind.text
    assert "估值" in cards.lot(1).text or "鉴定价" in text
    assert "预算余" in text and "读心" in text and "计划" in text


# --------------------------------------------------------------------------- shape


def test_there_is_one_card_per_lot_and_one_for_the_night(night: Any) -> None:
    state, cards = night
    assert isinstance(cards, FactCards)
    assert [c.lot for c in cards.lots] == list(range(1, len(state.records) + 1))
    assert cards.night.kind == "night" and cards.night.lot is None
    assert cards.lot(3) is cards.lots[2]
    with pytest.raises(KeyError):
        cards.lot(99)
    assert all(isinstance(d, LotData) for d in cards.lot_data)


def test_the_reference_columns_are_never_added_up(night: Any) -> None:
    """The night card has per-lot rows and per-seat rows, and no field that sums per-lot
    reference numbers (excess, regret, expected revenue, curse gaps) over the night."""
    _, cards = night
    fields = set(NightData.model_fields)
    assert not {"excess", "regret", "expected_revenue_milli", "curse_gap", "alpha_milli"} & fields
    seat_fields = set(type(cards.night.data.seats[0]).model_fields)
    assert not {"excess", "regret", "expected_revenue_milli", "curse_gap"} & seat_fields


# ------------------------------------------------------------------- hostile nights


@pytest.mark.parametrize("number", range(8))
def test_cards_of_a_wild_night_hold_together(number: int) -> None:
    """Random numbers, silence, timeouts and huge bids: the cards still build, agree with the
    log, and every headline stays within the numbers of its card."""
    from auction_night.contracts.events import validate_log
    from auction_night.engine.night import append_events
    from auction_night.show.host import MockHost, run_host

    config = make_config()
    transcript = play(
        config, deal(config, seed=f"show-wild-{number}"), wild_players(config, number)
    )
    state = transcript.state
    cards = build_fact_cards(state)
    log = Oracle(state.events)
    for card in cards.lots:
        data: LotData = card.data
        hammer = log.hammer[data.lot]
        assert (data.winner, data.price) == (hammer.winner, hammer.price)
        assert set(state.records[data.lot - 1].outcome.asked) == set(data.bidders)
        assert unlisted_numbers(card.headline, card.numbers) == []
        for line in data.seats:
            if line.abstained:
                assert line.played is None and "弃权" in card.text
    out = run_host(cards, MockHost(bad_number="first", only=frozenset({1})))
    full, _ = append_events(state, out.events)
    assert validate_log(full.events) == []


# ----------------------------------------------------------------- unusual tables


def test_a_table_of_bots_has_cards_but_nothing_to_predict() -> None:
    from night_support import all_bots_config

    from auction_night.show.host import MockHost, run_host

    state = play_seed(all_bots_config(), "show-bots-1").state
    cards = build_fact_cards(state)
    data: NightData = cards.night.data
    assert all(row.role == "bot" and row.rank_tonight is None for row in data.seats)
    assert data.missions == () and data.predictions.hits is None
    assert all(o.hit is None for o in data.predictions.outcomes)
    assert "没有一条预测可以对账" in cards.night.text
    assert all(line.plan_stars is None for card in cards.lots for line in card.data.seats)
    out = run_host(cards, MockHost())
    assert not out.commentary.fallbacks and len(out.events) == len(cards.lots) + 3


def test_a_two_seat_night_builds_its_cards() -> None:
    from night_support import make_config

    seats = [
        {"seat": 1, "codename": "甲", "kind": "mock", "mock_policy": "honest"},
        {"seat": 2, "codename": "乙", "kind": "mock", "mock_policy": "honest"},
    ]
    config = make_config(seats, missions={"collectors": 0, "shills": 0})
    state = play_seed(config, "show-two-1").state
    cards = build_fact_cards(state)
    assert all(len(card.data.seats) == 2 for card in cards.lots)
    assert all(unlisted_numbers(card.headline, card.numbers) == [] for card in cards.lots)
