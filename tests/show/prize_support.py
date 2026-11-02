"""Helpers of the award and highlight tests: played nights, a log oracle and the viewer's tables.

* ``mock_night`` plays the mock night with the scripted policies (honest, collude, backstab,
  random) and the two rule bots through the engine, and builds its fact cards.
* ``LogOracle`` computes the ten awards from the raw event log and the deal. It does not use the
  fact cards, the pledge judge's output formats or the award module, so comparing it with
  ``compute_awards`` checks the card route against the log route.
* ``viewer_awards`` and ``viewer_categories`` read the tables that the replay page keeps in
  ``viewer/lib``, so that a test can hold the Python constants against them.
"""

import re
from dataclasses import dataclass
from fractions import Fraction
from functools import cache
from pathlib import Path
from typing import Any

from night_support import make_config, play
from show_support import Script, lots_of, play_script, scripted_config

from auction_night.bots import make_bot
from auction_night.contracts.events import (
    ActionData,
    ActionEvent,
    DealPrivateEvent,
    DutchResultEvent,
    EnglishDropoutEvent,
    GuessEvent,
    HammerData,
    HammerEvent,
    JudgeVerdictData,
    LotOpenData,
    LotOpenEvent,
    NightStartEvent,
    PlanEvent,
    Pledge,
    TalkEvent,
)
from auction_night.contracts.mechanism import BID_PHASES, FORMAT_KEYS
from auction_night.engine.deal import Deal, deal
from auction_night.engine.night import NightState
from auction_night.llm.mock import MockModels
from auction_night.show.factcard import FactCards, build_fact_cards
from auction_night.show.pledges import judge_pledges
from auction_night.theory import analyse_night

REPO = Path(__file__).resolve().parents[2]
VIEWER = REPO / "viewer"

Result = tuple[list[int], int | None]


def play_mock_night(seed: str) -> tuple[NightState, FactCards]:
    """The mock night of ``configs/night_mock.yaml`` (pinned deal fields), played to the end."""
    config = make_config()
    the_deal = deal(config, seed=seed)
    models = MockModels(config, the_deal).players
    players = {
        seat.seat: make_bot(seat).answer if seat.kind == "bot" else models[seat.seat].answer
        for seat in config.seats
    }
    state = play(config, the_deal, players).state
    return state, build_fact_cards(state)


mock_night = cache(play_mock_night)  # nights are immutable, so the tests share them


# ------------------------------------------------------------------------ scripted nights


@dataclass(frozen=True)
class Played:
    """A night of scripted seats, with its deal and the lots of each format in lot order."""

    state: NightState
    cards: FactCards
    deal: Deal
    lots: dict[str, list[int]]
    notes: dict[str, Any]

    def tier(self, lot: int) -> int:
        return self.deal.lot(lot).tier


SCRIPT_SEED = "d8-script-1"


def _scripted(script: Script, the_deal: Deal, notes: dict[str, Any]) -> Played:
    _, _, transcript = play_script(script, seed=SCRIPT_SEED, config=scripted_config())
    state = transcript.state
    lots = {fmt: lots_of(the_deal, fmt) for fmt in FORMAT_KEYS}
    return Played(state, build_fact_cards(state), the_deal, lots, notes)


@cache
def awards_night() -> Played:
    """Six scripted seats on sealed, all-pay and blind-box lots, with numbers chosen so that
    every pledge award, the tilt, the curse, the bargain and a mission have a known winner.

    * Tilt: seat 1 pays 40 and 30 in the two all-pay lots and wins neither; seat 3 pays 15.
    * Curse: seat 5 and seat 3 each win a blind box far above its value.
    * Bargain: seat 1 takes the first first-price lot for one coin.
    * Pledges: seat 3 pledges not to bid and bids; seat 4 breaks a cap of 50 with 55; seat 2 keeps
      a cap of 60 and a cap of 70 by bidding exactly 60 and 70.
    * Mission: the first shill whose lot is a second-price lot is pushed to its threshold by a
      second seat that outbids it, so the shill completes it at that lot.
    """
    the_deal = deal(scripted_config(), seed=SCRIPT_SEED)
    ap, bx, sf, ss = (
        lots_of(the_deal, fmt) for fmt in ("all_pay", "blind_box", "sealed_first", "sealed_second")
    )
    tier = lambda lot: the_deal.lot(lot).tier  # noqa: E731
    shill_seat, mission = next(
        (seat, m)
        for seat, m in the_deal.missions.items()
        if m.kind == "shill" and the_deal.lot(m.lot).format == "sealed_second"
    )
    other = next(lot for lot in ss if lot != mission.lot)
    buyer = 1 if shill_seat != 1 else 2
    bids = {
        (ap[0], 1): 40, (ap[0], 2): 60, (ap[0], 3): 15,
        (ap[1], 1): 30, (ap[1], 4): 50,
        (bx[0], 5): 3 * tier(bx[0]), (bx[0], 2): 5 * tier(bx[0]) // 2,
        (bx[1], 3): 3 * tier(bx[1]), (bx[1], 1): 7 * tier(bx[1]) // 4,
        (sf[0], 1): 1,
        (mission.lot, shill_seat): mission.threshold_price,
        (mission.lot, buyer): mission.threshold_price + 10,
        (other, 4): 55, (other, 2): 70,
    }  # fmt: skip
    pledges = {
        (ap[0], 2): Pledge(kind="max_bid", lot=ap[0], amount=60),
        (ap[0], 3): Pledge(kind="no_bid", lot=ap[0]),
        (other, 2): Pledge(kind="max_bid", lot=other, amount=70),
        (other, 4): Pledge(kind="max_bid", lot=other, amount=50),
    }
    notes = {"shill_seat": shill_seat, "shill_lot": mission.lot, "other": other}
    return _scripted(Script(bids=bids, pledges=pledges), the_deal, notes)


@cache
def clocks_night() -> Played:
    """English and Dutch lots with pledges that are broken, some where the room can see it and
    some on a number that only the pledger knows (the English winner's exit price, a Dutch
    grab price), and a shill that buys its own English lot.

    * English lot ``en[1]``: seat 2 (exit 150) wins at 120 and pledged a cap of 100, which the
      price shows; seat 5 left at 120 with a cap of 100; seat 6 (exit 90) kept a cap of 95.
    * English lot ``en[0]``: seat 1 (exit 60) wins at 40 and pledged a cap of 50: only its exit
      price breaks it. Seat 1 holds the shill mission of that lot, so it also bought its own lot.
    * Dutch lot ``du[0]``: seat 2 (grab 80) buys at 80 against a cap of 50; seat 6 (grab 70) had
      pledged not to bid; seat 5 (grab 60) kept a cap of 100.
    * Sealed lot ``sf[1]``: seat 3 pledges to let seat 4 win and wins itself with 30.
    """
    the_deal = deal(scripted_config(), seed=SCRIPT_SEED)
    en, du, sf = (lots_of(the_deal, fmt) for fmt in ("english", "dutch", "sealed_first"))
    bids = {
        (en[1], 2): 150, (en[1], 5): 120, (en[1], 6): 90,
        (en[0], 1): 60, (en[0], 3): 40,
        (du[0], 2): 80, (du[0], 6): 70, (du[0], 5): 60,
        (sf[1], 3): 30,
    }  # fmt: skip
    pledges = {
        (en[1], 2): Pledge(kind="max_bid", lot=en[1], amount=100),
        (en[1], 5): Pledge(kind="max_bid", lot=en[1], amount=100),
        (en[1], 6): Pledge(kind="max_bid", lot=en[1], amount=95),
        (en[0], 1): Pledge(kind="max_bid", lot=en[0], amount=50),
        (du[0], 2): Pledge(kind="max_bid", lot=du[0], amount=50),
        (du[0], 6): Pledge(kind="no_bid", lot=du[0]),
        (du[0], 5): Pledge(kind="max_bid", lot=du[0], amount=100),
        (sf[1], 3): Pledge(kind="let_win", lot=sf[1], to=4),
    }
    return _scripted(Script(bids=bids, pledges=pledges), the_deal, {})


def public_numbers(state: NightState) -> set[int]:
    """Every whole number that the public events of a night state: the lots and seats, the
    catalogue tiers, pledge amounts, drop-out prices, the Dutch ladder, and the price, payments,
    bids and true value of each hammer (with the gap of a box between price and true value)."""
    found: set[int] = set(range(0, 9)) | set(range(1, 37))
    for event in state.events:
        if event.vis != "public":
            continue
        if isinstance(event, NightStartEvent):
            found |= {lot.tier for lot in event.data.lots} | {event.data.budget_lo}
            found |= {event.data.budget_hi, event.data.mission_bonus}
        elif isinstance(event, LotOpenEvent):
            found |= {event.data.tier, event.data.reserve}
        elif isinstance(event, TalkEvent) and event.data.pledge is not None:
            pledge: Pledge = event.data.pledge
            found |= {n for n in (pledge.lot, pledge.amount, pledge.to) if n is not None}
        elif isinstance(event, EnglishDropoutEvent):
            found.add(event.data.price)
        elif isinstance(event, DutchResultEvent):
            found |= set(event.data.ladder) | {n for n in (event.data.level, event.data.price) if n}
        elif isinstance(event, HammerEvent):
            hammer = event.data
            found |= {hammer.price, *hammer.payments.values(), *(hammer.bids or {}).values()}
            if hammer.true_value is not None:
                found |= {hammer.true_value, hammer.price - hammer.true_value}
    return found


# ----------------------------------------------------------------------------- the oracle


def _top(figures: dict[int, int], *, lowest: bool = False) -> Result:
    if not figures:
        return [], None
    best = min(figures.values()) if lowest else max(figures.values())
    return sorted(seat for seat, figure in figures.items() if figure == best), best


@dataclass
class LogOracle:
    """What the log of a finished night says, read with plain loops.

    The private cards come from the ``deal_private`` events, the plans from the ``plan`` events,
    the guesses from the ``guess`` events, and what a seat did from its ``action`` events. The
    truth of a guess is recomputed from the opening budget and the payments before the lot.
    """

    state: NightState

    def __post_init__(self) -> None:
        events = self.state.events
        self.start = next(e.data for e in events if isinstance(e, NightStartEvent))
        self.role = {seat.seat: seat.role for seat in self.start.seats}
        self.tier = {lot.lot: lot.tier for lot in self.start.lots}
        self.format = {lot.lot: lot.format for lot in self.start.lots}
        self.cards = {e.data.seat: e.data for e in events if isinstance(e, DealPrivateEvent)}
        self.plans = {e.data.seat: e.data for e in events if isinstance(e, PlanEvent)}
        self.opens: dict[int, LotOpenData] = {
            e.lot: e.data for e in events if isinstance(e, LotOpenEvent) and e.lot is not None
        }
        self.hammers: dict[int, HammerData] = {
            e.lot: e.data for e in events if isinstance(e, HammerEvent) and e.lot is not None
        }
        self.acts: dict[tuple[int, int], list[ActionData]] = {}
        for e in events:
            if isinstance(e, ActionEvent) and e.lot is not None and e.data.phase in BID_PHASES:
                self.acts.setdefault((e.lot, e.data.seat), []).append(e.data)
        self.guesses = [
            (e.lot, e.data.seat, e.data.target, e.data.guess)
            for e in events
            if isinstance(e, GuessEvent) and e.lot is not None
        ]
        self.span = self.start.budget_hi - self.start.budget_lo

    # ------------------------------------------------------------------------ small facts

    def group(self, role: str) -> set[int]:
        return {seat for seat, kind in self.role.items() if kind == role}

    def remaining(self, seat: int, lot: int) -> int:
        paid = sum(h.payments.get(seat, 0) for n, h in self.hammers.items() if n < lot)
        return self.cards[seat].budget - paid

    def own(self, seat: int, lot: int) -> int | None:
        card = self.cards[seat]
        return card.values[lot] if lot in card.values else card.signals.get(lot)

    def played(self, lot: int, seat: int) -> int | None:
        """The last usable number of the seat in the lot, or None (not asked, or abstained)."""
        if seat not in self.opens[lot].active:
            return None
        usable = [a for a in self.acts.get((lot, seat), []) if a.status != "abstain"]
        return usable[-1].value if usable else None

    # ------------------------------------------------------------------------- the awards

    def textbook(self, group: set[int]) -> dict[int, tuple[int, int]]:
        """Per seat: the number of valid bids and the mean distance in thousandths of a tier."""
        ref = {t.lot: t.ref_bids for t in analyse_night(self.state)}
        found = {}
        for seat in sorted(group):
            total, count = Fraction(0), 0
            for lot in sorted(self.hammers):
                number = self.played(lot, seat)
                if self.format[lot] == "blind_box" or number is None or seat not in ref[lot]:
                    continue
                total += Fraction(abs(number - ref[lot][seat]), self.tier[lot])
                count += 1
            if count:
                found[seat] = (count, int(total * 1000 / count))
        return found

    def curse(self, group: set[int]) -> Result:
        figures: dict[int, int] = {}
        for lot, hammer in self.hammers.items():
            if self.format[lot] != "blind_box" or hammer.winner not in group:
                continue
            assert hammer.winner is not None and hammer.true_value is not None
            gap = hammer.price - hammer.true_value
            if gap * 20 >= self.tier[lot]:
                figures[hammer.winner] = max(figures.get(hammer.winner, gap), gap)
        return _top(figures)

    def tilt(self, group: set[int]) -> Result:
        lost = dict.fromkeys(group, 0)
        for lot, hammer in self.hammers.items():
            if self.format[lot] == "all_pay":
                for seat, amount in hammer.payments.items():
                    if seat in group and seat != hammer.winner:
                        lost[seat] += amount
        return _top({seat: total for seat, total in lost.items() if total >= 10})

    def bargain(self, group: set[int]) -> Result:
        figures: dict[int, int] = {}
        for lot, hammer in self.hammers.items():
            seat = hammer.winner
            if seat is None or seat not in group:
                continue
            if self.format[lot] == "blind_box":
                assert hammer.true_value is not None
                worth = hammer.true_value
            else:
                worth = self.cards[seat].values[lot]
            gain = worth - hammer.payments[seat]
            if gain * 5 >= self.tier[lot]:
                figures[seat] = max(figures.get(seat, gain), gain)
        return _top(figures)

    def backstabber(self, group: set[int]) -> Result:
        broken = [p.seat for p in judge_pledges(self.state.events) if p.outcome == "violated"]
        return _top({s: broken.count(s) for s in set(broken) if s in group})

    def best_actor(self, group: set[int], verdicts: list[JudgeVerdictData]) -> Result:
        lies = [
            seat
            for v in verdicts
            if v.verdict == "false_claim"
            for seat in set(v.seats)
            if seat in group
        ]
        return _top({s: lies.count(s) for s in set(lies)})

    def keeper(self, group: set[int]) -> Result:
        by_seat: dict[int, list[str]] = {}
        for p in judge_pledges(self.state.events):
            if p.seat in group and p.outcome != "void":
                by_seat.setdefault(p.seat, []).append(p.outcome)
        return _top({s: len(o) for s, o in by_seat.items() if len(o) >= 2 and set(o) == {"kept"}})

    def mission_master(self, group: set[int]) -> Result:
        done = {}
        for m in self.state.missions:
            if m.seat in group and m.completed:
                done[m.seat] = m.target_lot if m.kind == "shill" else sorted(m.won_lots)[m.need - 1]
        if not done:
            return [], None
        first = min(done.values())
        return sorted(s for s, lot in done.items() if lot == first), None

    def mind(self, group: set[int]) -> dict[int, tuple[int, int]]:
        """Per seat: the number of guesses and the mean error in thousandths of the span."""
        errors: dict[int, list[int]] = {}
        for lot, seat, target, guess in self.guesses:
            if seat in group:
                errors.setdefault(seat, []).append(abs(guess - self.remaining(target, lot)))
        return {
            seat: (len(found), sum(found) * 1000 // (len(found) * self.span))
            for seat, found in errors.items()
        }

    def consistency(self, group: set[int]) -> dict[int, tuple[int, int]]:
        """Per seat: the number of judgable lots and the share in which it followed its plan."""
        found = {}
        for seat in sorted(group & set(self.plans)):
            judged = followed = 0
            for lot in sorted(self.hammers):
                number, own = self.played(lot, seat), self.own(seat, lot)
                if number is None or own is None or own <= 0:
                    continue
                if 2 * self.remaining(seat, lot) < own:
                    continue
                judged += 1
                serious = 2 * number >= own
                followed += (self.plans[seat].priorities[lot - 1] >= 2) == serious
            if judged:
                found[seat] = (judged, followed * 1000 // judged)
        return found

    def awards(
        self, role: str, verdicts: list[JudgeVerdictData] | None = None
    ) -> dict[str, Result]:
        """Every award of the seats with this role, as (winners, figure)."""
        group = self.group(role)
        textbook = {s: v[1] for s, v in self.textbook(group).items() if v[0] >= 6}
        mind = {s: v[1] for s, v in self.mind(group).items() if v[0] >= 8}
        consistent = {s: v[1] for s, v in self.consistency(group).items() if v[0] >= 10}
        return {
            "most_textbook": _top(textbook, lowest=True),
            "winners_curse": self.curse(group),
            "tilt": self.tilt(group),
            "bargain": self.bargain(group),
            "backstabber": self.backstabber(group),
            "best_actor": self.best_actor(group, verdicts or []),
            "keeper": self.keeper(group),
            "mission_master": self.mission_master(group),
            "mind_reader": _top(mind, lowest=True),
            "consistent": _top(consistent),
        }


# ------------------------------------------------------------------------ the viewer tables


def viewer_awards() -> dict[str, dict[str, str | None]]:
    """The ``floor``, ``unit`` and ``rule`` of each award in ``viewer/lib/strings.js``."""
    source = (VIEWER / "lib" / "strings.js").read_text(encoding="utf-8")
    found: dict[str, dict[str, str | None]] = {}
    pattern = r"^ {6}(\w+): Object\.freeze\(\{\n(.*?)\n {6}\}\),$"
    for key, body in re.findall(pattern, source, flags=re.S | re.M):
        entry: dict[str, str | None] = {}
        for field in ("rule", "floor", "unit"):
            match = re.search(rf"^ {{8}}{field}: (null|'[^']*'),$", body, flags=re.M)
            if match:
                entry[field] = None if match.group(1) == "null" else match.group(1)[1:-1]
        found[key] = entry
    return found


def viewer_basis(file: str) -> dict[str, str]:
    """The ``BASIS`` table of ``viewer/lib/awards.js`` or ``viewer/lib/highlights.js``."""
    source = (VIEWER / "lib" / file).read_text(encoding="utf-8")
    block = re.search(r"const BASIS = Object\.freeze\(\{(.*?)\}\);", source, flags=re.S)
    assert block, f"BASIS not found in {file}"
    return dict(re.findall(r"(\w+): '(public|private)'", block.group(1)))


def viewer_zone(first_seq: int, lot_first_seq: int, first_bid_seq: int) -> str:
    """Where ``momentStart`` in ``viewer/lib/highlights.js`` sends a moment: ``lot``, ``talk``
    (a line of the lot) or ``bid`` (the start of the bid animation)."""
    if not first_seq > lot_first_seq:
        return "lot"
    if first_seq < first_bid_seq:
        return "talk"
    return "bid"


def first_bid_seq(state: NightState, lot: int) -> int:
    """The first bid-phase action of a lot (what ``momentStart`` calls ``firstBid``)."""
    return min(
        e.seq
        for e in state.events
        if isinstance(e, ActionEvent) and e.lot == lot and e.data.phase in BID_PHASES
    )


def canon(*parts: Any) -> str:
    """Canonical JSON of models and lists of models, for byte comparisons."""
    from auction_night.contracts.events import canonical_json

    return canonical_json(list(parts))
