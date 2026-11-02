"""Laws that hold in every night, for random rules, random budgets and random answers.

``test_night_properties`` plays the one mock night against wild answers. Here the *night itself*
is random: the table (two to eight seats, models and bots), the lots, the reserve price, the
English ``theta``, the number of Dutch levels and the size of the budgets. Seats answer with
nothing at all, with an abstention for any reason, or with numbers that sit on the edges (zero,
the budget, one coin above it, the price of the clock). Some seats never give a usable answer in
any bid phase.

A played night is then audited from its requests, its answers and its events:

* **One winner, no negative budgets.** A lot has one hammer and at most one winner. An unsold
  lot costs nobody anything. Nobody pays more than the budget left, and every budget stays
  non-negative from the first lot to the last.
* **The price of each format** follows the rule of the format, recomputed from the published
  bids, the exit prices or the ladder.
* **Dutch budget rule.** The price is a level of the ladder and no more than the buyer's
  budget. A seat that cannot afford the lowest level is not in the request and has no action.
* **English rules.** A revision is never below the price of the clock, a seat that did not take
  part is not counted towards ``theta`` and is not asked again, and a revision that fails keeps
  the exit price in force.
* **Abstention.** A missing, failed, timed-out or refused answer is recorded as such. It is a
  bid of nothing: no seat that stayed silent ever wins or pays, and no price shows up in its
  place.
* **Status.** The night ends as ``budget_aborted`` exactly when the engine was handed an
  abstention with that reason, in any phase, also when the log cannot show it (an English
  revision that does not come is ``kept_previous``, which names no reason).
* **Ties** go to the lowest key dealt for the lot.
* **The books** balance, per seat and for the table, and the mission bonus is kept apart.
* **Replay.** The same answers give the same events, byte for byte.
"""

from collections import defaultdict
from typing import Any

from chaos_support import Case, cases, players
from hypothesis import HealthCheck, example, given, settings
from night_support import Transcript, play, replay

from auction_night.contracts import NightConfig, dumps_events, validate_log
from auction_night.contracts.events import CollectorMission, ShillMission
from auction_night.engine.night import Answer, Request
from auction_night.engine.scoring import conservation_gap

BID_PHASES = ("english_init", "english_revise", "dutch", "sealed")


# ------------------------------------------------------------------------------- the audit


class Audit:
    """Facts read off a played night, indexed for the checks."""

    def __init__(self, case: Case, config: NightConfig, transcript: Transcript) -> None:
        self.case = case
        self.config = config
        self.night = config.night
        self.state = transcript.state
        self.transcript = transcript
        self.events = transcript.events
        self.deal = transcript.state.deal
        self.models = {seat.seat for seat in config.seats if seat.kind != "bot"}
        self.lots = {e.lot: e.data for e in self.events if e.type == "hammer"}
        self.opened = {e.lot: e.data for e in self.events if e.type == "lot_open"}
        self.records = {record.lot: record for record in self.state.records}
        self.actions: dict[tuple[int, int, str], list[Any]] = defaultdict(list)
        for event in self.events:
            if event.type == "action" and event.lot is not None:
                self.actions[(event.lot, event.data.seat, event.data.phase)].append(event.data)
        self.dropouts: dict[int, list[Any]] = defaultdict(list)
        for event in self.events:
            if event.type == "english_dropout":
                self.dropouts[event.lot].append((event.seq, event.data))

    def mech(self, lot: int):
        return self.night.mechanism_for(self.deal.lot(lot).format)

    def bid_requests(self, lot: int) -> list[tuple[Request, dict[int, Answer]]]:
        return [
            (request, answers)
            for request, answers in zip(
                self.transcript.requests, self.transcript.answers, strict=True
            )
            if request.lot == lot and request.phase in BID_PHASES
        ]


def ladder_of(tier: int, levels: int, reserve: int) -> list[int]:
    """The price levels of a Dutch lot, highest first, written out again: the tier falls by
    ``tier // levels`` at a time and levels below the reserve are skipped."""
    prices = [tier - k * (tier // levels) for k in range(levels)]
    return [price for price in prices if price >= reserve]


def number_of(answer: Answer | None) -> int | None:
    """The number an answer carries; None for no answer, an abstention or an answer without one."""
    return None if answer is None or answer.abstain is not None else answer.value


def check_money(audit: Audit) -> None:
    """One winner at most, nothing paid for an unsold lot, budgets never negative."""
    left = dict(audit.deal.budgets)
    assert len(audit.lots) == audit.night.n_lots
    for number in range(1, audit.night.n_lots + 1):
        hammer = audit.lots[number]
        record = audit.records[number]
        assert record.budgets_before == left
        assert len([e for e in audit.events if e.type == "hammer" and e.lot == number]) == 1
        if hammer.unsold:
            assert hammer.winner is None and hammer.price == 0 and not hammer.payments
        else:
            assert hammer.winner in record.outcome.asked
            assert hammer.payments[hammer.winner] == hammer.price
            if hammer.format != "all_pay":
                assert hammer.payments == {hammer.winner: hammer.price}
        for seat, paid in hammer.payments.items():
            assert seat in record.outcome.asked and 0 <= paid <= left[seat], (number, seat, paid)
            left[seat] -= paid
        assert min(left.values()) >= 0
        assert hammer.newly_out == sorted(
            s for s in left if record.budgets_before[s] >= 1 and left[s] < 1
        )
    assert audit.state.budgets == left


def check_prices(audit: Audit) -> None:
    """The price of each lot follows the rule of its format."""
    for number, hammer in audit.lots.items():
        mech = audit.mech(number)
        before = audit.records[number].budgets_before
        if mech.clock == "descending_levels":
            lot = audit.deal.lot(number)
            ladder = ladder_of(lot.tier, mech.levels or 1, mech.reserve)
            if not hammer.unsold:
                assert hammer.price in ladder and hammer.price <= before[hammer.winner]
            continue
        if mech.clock == "ascending_button":
            drops = [d for _, d in audit.dropouts[number] if d.participated]
            prices = [d.price for d in drops]
            assert prices == sorted(prices), "the clock only goes up"
            if not hammer.unsold:
                assert hammer.price == max(drops[-1].price if drops else 0, mech.reserve)
            continue
        assert hammer.bids is not None
        ranked = sorted(hammer.bids.values(), reverse=True)
        top = ranked[0] if ranked else 0
        if top < max(mech.reserve, 1):
            assert hammer.unsold, "the best bid is below the reserve, or nobody bid"
            continue
        assert not hammer.unsold and hammer.bids[hammer.winner] == top
        if mech.pricing == "first":
            assert hammer.price == top
        elif mech.pricing == "second":
            assert hammer.price == max(ranked[1] if len(ranked) > 1 else 0, mech.reserve)
        else:
            assert hammer.payments == {s: b for s, b in hammer.bids.items() if b >= 1}


def check_dutch_budget_rule(audit: Audit) -> None:
    """Seats below the lowest level are never asked, and everybody else is."""
    for number, record in audit.records.items():
        mech = audit.mech(number)
        if mech.clock != "descending_levels":
            continue
        ladder = ladder_of(audit.deal.lot(number).tier, mech.levels or 1, mech.reserve)
        lowest = ladder[-1] if ladder else None
        poor = [s for s, b in record.budgets_before.items() if lowest is None or b < lowest]
        rich = [s for s, b in record.budgets_before.items() if lowest is not None and b >= lowest]
        opened = audit.opened[number]
        assert list(opened.active) == rich and set(poor) <= set(opened.sitting_out)
        asked = [r for r, _ in audit.bid_requests(number)]
        assert len(asked) == (1 if rich else 0)
        if asked:
            assert list(asked[0].seats) == rich
        for seat in poor:
            assert (number, seat, "dutch") not in audit.actions
        assert record.outcome.asked == tuple(rich)


def check_english_rules(audit: Audit) -> None:
    theta = audit.night.english.revise_when_remaining_le
    for number in audit.records:
        mech = audit.mech(number)
        rounds = audit.bid_requests(number)
        if mech.clock != "ascending_button" or not rounds:
            continue
        floor = max(mech.reserve, 1)
        first, first_answers = rounds[0]
        assert first.phase == "english_init"
        participants = set()
        for seat in first.seats:
            number_given = number_of(first_answers.get(seat))
            if (
                number_given is not None
                and min(number_given, first.facts_of(seat).budget_left) >= floor
            ):
                participants.add(seat)
        drops = [d for _, d in audit.dropouts[number]]
        outsiders = [d for d in drops if not d.participated]
        insiders = [d for d in drops if d.participated]
        # an exit price below max(reserve, 1), or no usable answer, is no part in the lot
        assert {d.seat for d in outsiders} == set(first.seats) - participants
        assert all(d.price == 0 and d.remaining == sorted(participants) for d in outsiders)
        # the clock only goes up
        assert [d.price for d in insiders] == sorted(d.price for d in insiders)
        for request, _ in rounds[1:]:
            assert request.phase == "english_revise"
            # only participants are counted towards theta, and only they are asked
            assert 2 <= len(request.seats) <= theta
            assert set(request.seats) <= participants and request.seats == request.remaining
            # nobody in force has an exit price below the clock, and nobody leaves below it
            assert all(request.facts_of(s).exit_price >= request.price for s in request.seats)
            left_since = insiders[len(participants) - len(request.remaining) :]
            assert all(d.price >= request.price for d in left_since)


def check_answers_are_recorded_as_given(audit: Audit) -> None:
    """Abstentions stay abstentions, and no default price appears in their place."""
    seen: dict[tuple[int, int, str], int] = defaultdict(int)
    for request, answers in zip(audit.transcript.requests, audit.transcript.answers, strict=True):
        if request.phase not in BID_PHASES:
            continue
        lot = request.lot
        assert lot is not None
        hammer = audit.lots[lot]
        for seat in request.seats:
            index = seen[(lot, seat, request.phase)]
            seen[(lot, seat, request.phase)] += 1
            action = audit.actions[(lot, seat, request.phase)][index]
            answer = answers.get(seat)
            budget = request.facts_of(seat).budget_left
            given = number_of(answer)
            if request.phase == "english_revise":
                if given is not None:
                    assert action.value == min(given, budget)
                else:  # "没有回答则保留原来的退出价": the one place where a value is kept
                    assert action.status == "kept_previous" and action.reason is None
                    assert action.value == request.facts_of(seat).exit_price
                continue
            if given is None:
                reason = answer.abstain if answer is not None else "missing"
                assert (action.status, action.reason, action.value) == ("abstain", reason, None)
                assert action.raw_value is None
                if hammer.bids is not None:
                    assert hammer.bids[seat] == 0
                assert seat != hammer.winner and seat not in hammer.payments
                continue
            clamped = given > budget
            assert action.status == ("clamped" if clamped else "ok")
            assert action.value == min(given, budget)
            assert action.raw_value == (given if clamped else None)
            if hammer.bids is not None:
                assert hammer.bids[seat] == action.value
    # a seat that never gave a usable answer in a bid phase never wins and never pays
    for seat in audit.case.silent:
        for number, hammer in audit.lots.items():
            assert hammer.winner != seat and seat not in hammer.payments, (number, seat)


def check_status_follows_the_answers(audit: Audit) -> None:
    """The status in ``night_end`` is read off the answers, not off the log."""
    skipped = any(
        answer.abstain == "budget_aborted"
        for answers in audit.transcript.answers
        for answer in answers.values()
    )
    if audit.case.gate == "revisions":
        # the cap is named by the revisions only, and a revision that does not come names nothing
        assert not [
            e for e in audit.events if e.type == "action" and e.data.reason == "budget_aborted"
        ]
        asked = [
            r
            for r in audit.transcript.requests
            if r.phase == "english_revise" and any(s in audit.models for s in r.seats)
        ]
        assert skipped == bool(asked)
    end = audit.events[-1]
    assert end.type == "night_end"
    assert end.data.status == ("budget_aborted" if skipped else "completed")
    assert audit.state.budget_aborted == skipped


def check_ties(audit: Audit) -> None:
    for number, hammer in audit.lots.items():
        keys = audit.deal.lot(number).tie_key
        if hammer.tie_broken:
            assert hammer.winner == min(hammer.tied, key=lambda s: (keys[s], s))
        else:
            assert not hammer.tied


def check_books(audit: Audit) -> None:
    """Per-seat and table-wide balances, with the mission bonus kept apart."""
    scores = {score.seat: score for score in audit.state.scores}
    worth = dict.fromkeys(scores, 0)
    paid = dict.fromkeys(scores, 0)
    for number, hammer in audit.lots.items():
        lot = audit.deal.lot(number)
        for seat, amount in hammer.payments.items():
            paid[seat] += amount
        if hammer.winner is not None:
            hidden = lot.blind_value  # the value of a blind box is the same for every seat
            worth[hammer.winner] += hidden if hidden is not None else lot.values[hammer.winner]
    bonus = {m.seat: m.bonus for m in audit.state.missions}
    for seat, score in scores.items():
        assert score.surplus == worth[seat] - paid[seat]
        assert score.spent == paid[seat]
        assert score.mission_bonus == bonus.get(seat, 0)
        assert score.total == score.surplus + score.mission_bonus
    revenue = sum(paid.values())
    assert sum(score.surplus for score in scores.values()) == sum(worth.values()) - revenue
    assert sum(score.mission_bonus for score in scores.values()) == sum(bonus.values())
    assert conservation_gap(audit.state.scores, audit.deal.lots, audit.state.hammers) == 0
    # missions are decided from the hammers alone
    for result in audit.state.missions:
        mission = audit.deal.missions[result.seat]
        if isinstance(mission, CollectorMission):
            mine = [
                n
                for n, h in audit.lots.items()
                if h.winner == result.seat and audit.deal.lot(n).tag == mission.tag
            ]
            assert result.completed == (len(mine) >= mission.need)
        else:
            assert isinstance(mission, ShillMission)
            target = audit.lots[mission.lot]
            expected = (
                not target.unsold
                and target.winner != result.seat
                and target.price >= mission.threshold_price
            )
            assert result.completed == expected
        assert result.bonus == (mission.bonus if result.completed else 0)


def audit_night(case: Case) -> None:
    config = case.config()
    the_deal = case.dealt(config)
    transcript = play(config, the_deal, players(case, config, the_deal))
    audit = Audit(case, config, transcript)
    assert audit.state.done and validate_log(audit.events) == []
    check_money(audit)
    check_prices(audit)
    check_dutch_budget_rule(audit)
    check_english_rules(audit)
    check_answers_are_recorded_as_given(audit)
    check_status_follows_the_answers(audit)
    check_ties(audit)
    check_books(audit)
    # the same answers, the same night, byte for byte
    again = replay(config, the_deal, transcript)
    assert dumps_events(again.events) == dumps_events(audit.events)


PROFILE = settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)


@PROFILE
@given(cases())
@example(Case())  # the table of the mock night
@example(Case(budget_c=20, seed=1))  # poor seats: most of them cannot afford a Dutch level
@example(Case(reserve=100, seed=2))  # a reserve as high as the smallest tier
@example(Case(theta=8, levels=20, seed=3))  # a question after every English exit
@example(Case(theta=2, levels=2, reserve=37, seed=4))
@example(Case(silent=(1, 2, 3), models=6, seed=5))  # three seats that never answer a bid phase
@example(Case(shape="pay", collectors=2, shills=0, seed=6))
@example(Case(shape="short", models=2, bots=0, collectors=1, shills=1, seed=7))
@example(Case(models=0, bots=8, collectors=0, shills=0, seed=8))
@example(Case(gate="revisions", seed=11))  # a cap that only cuts off the English revisions
@example(Case(gate="revisions", theta=8, seed=12))  # ... with a question after every English exit
@example(Case(budgets=(9, 10, 19, 20, 39, 40, 41, 1), seed=9))  # on and next to the Dutch levels
@example(Case(reserve=25, budgets=(24, 25, 26, 1, 24, 25, 26, 2), levels=4, seed=10))
def test_every_night_obeys_the_laws(case: Case) -> None:
    audit_night(case)
