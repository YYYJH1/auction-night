"""Highlights: the moments of a night worth a jump, each with a fun score from 0 to 10.

The fact cards list *candidates* (:mod:`~auction_night.show.candidates`) and the judges' verdicts
add one more kind (a false claim). This module scores them, chooses the best, places each one in
the log and writes its sentence. The result is the ``highlights`` list of the replay. It is a pure
function of the cards, the log and the verdicts: nothing reads a clock or a random generator, so
the same night always gives the same bytes.

The score
---------
The score only says how good the moment is to watch. The rules are plain arithmetic on the
numbers of the moment: ``score = min(10, base + rungs + twists)``. A *rung ladder* counts how many
of its thresholds an amount reaches, where the amount is measured in thousandths of the tier of the
lot (of the budget span, the width of the public budget range, for a mind-reading error).

* ``shill_stuck``, base 8. +1 if the price reached the threshold (the right price and the wrong
  buyer). +1 if the shill paid more than its own value for the lot.
* ``winners_curse``, base 4. Rungs 5%, 10% and 20% of the tier for the gap between price and true
  value. +2 if the price is above the top of the public prior of the box.
* ``backstab``, base 4. +2 if the seat that broke the pledge won the lot. +2 if the intent is
  ``deliberate`` (from the pledge record or a verdict; ``unknown`` and ``unintentional`` add
  nothing). +1 if the money that went against the pledge is at least a quarter of the tier (for
  a pledge not to bid, the bid; for a cap, the part above it; for ``let_win``, the price paid).
* ``claim_vs_hand``, base 5. +2 if the intent is ``deliberate``. +1 if the seat won the lot in
  which it spoke. Rungs 25%, 50% and 100% for the gap of the claim (``gap_milli`` of the verdict):
  how far a claimed amount was from the truth, in thousandths of the tier of the lot for a value
  and of the budget for a budget. "Far from the hole card" is what makes the moment, so a claim
  that missed by a quarter of a tier scores one more than a claim that only just left the
  tolerance, and one that missed by a whole tier or budget scores three more. A claim about the
  kind or the tag of the mission has no gap, no rung, and keeps the base score.
* ``plan_contradiction``, base 3. A lot planned at 0 or 1 star that the seat won: +2 for 0 stars
  or +1 for 1 star, rungs 25% and 50% of the tier for the price, +1 if the price is above the
  budget it planned for the lot. A lot planned at 3 stars that the seat bid nothing for: +1 if
  another seat won it for no more than the planned budget and the seat had answered.
* ``all_pay_tilt``, base 3. Rungs 25%, 50% and 100% of the tier for the coins lost. +1 if the seat
  lost by no more than 5% of the tier to the winning bid.
* ``bargain``, base 3. Rungs 35%, 50% and 75% of the tier for the gain.
* ``mindread_miss``, base 3. Rungs 75%, 100% and 150% of the span for the error. +1 if one of
  guess and truth is at least three times the other.

The choice
----------
Every moment is scored. For each category the ``MAX_PER_CATEGORY`` best are kept (higher score
first, then the earlier lot, the earlier event, the lower seat). The kept moments are numbered
``1..N`` in the order of the night: by lot, then by the first event of the moment, then by the
last, then by the order of the categories, then by seat. The viewer ranks them again by score.

Where a moment lies
-------------------
``first_seq`` and ``last_seq`` are event numbers of the log, and always inside the lot of the
moment: from its ``lot_open`` to its ``hammer``, the first and last event of the lot in the
timeline of the replay. The viewer's ``momentStart`` (``viewer/lib/highlights.js``) reads
``first_seq`` against three zones of the lot, and every category opens in the zone it needs.

* The start of the lot (``first_seq`` is the ``lot_open``): ``shill_stuck``, and a ``backstab``
  whose pledge was made in an earlier lot.
* The line of a seat (``first_seq`` falls between the ``lot_open`` and the first bid): ``backstab``
  opens at the line that made the pledge, ``claim_vs_hand`` at the first evidence number of the
  verdict (the line that holds the claim), ``plan_contradiction`` at the seat's line and
  ``mindread_miss`` at the guess event. ``seats`` holds the one seat whose line it is (the
  guesser, not the target, for a mind-reading miss).
* The start of the bid animation (``first_seq`` is at or after the first bid): ``winners_curse``,
  ``bargain`` and ``all_pay_tilt`` open at the first bid action of the seat they are about.

A moment ends at the ``hammer`` of its lot, except ``claim_vs_hand``, which ends at the last
evidence number of the verdict, and ``mindread_miss``, which is the guess event alone.

The sentence
------------
The sentence is the candidate's own sentence, built from the same numbers as the fact card. The
exceptions are ``backstab`` and ``claim_vs_hand``. A pledge is broken on a number that is often
private (an English winner's exit price, a Dutch grab price), and ``backstab`` is a category that
the viewer shows without the hole-card lens, so its sentence names a number only when the whole
room could read it after the hammer: a sealed bid, an English exit price of a seat that did not
win, the price a winner paid. Otherwise it says that the pledge was not kept and names no number.
A pledge made in an earlier lot than the one it was broken in says where it was made.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal, get_args

from auction_night.contracts.events import (
    ActionEvent,
    AnyEvent,
    GuessEvent,
    HammerEvent,
    Intent,
    JudgeVerdictData,
    LotOpenEvent,
    TalkEvent,
)
from auction_night.contracts.mechanism import BID_PHASES, MAX_LOTS
from auction_night.contracts.replay import Highlight, HighlightCategory, PledgeRecord
from auction_night.show.cards import Candidate, LotData, SeatLine
from auction_night.show.factcard import FactCards, NightData
from auction_night.show.pledges import bid_word, pledge_text

__all__ = [
    "BASE_SCORE",
    "CLAIM_RUNGS",
    "HIGHLIGHT_BASIS",
    "HIGHLIGHT_ORDER",
    "MAX_PER_CATEGORY",
    "Moment",
    "choose_moments",
    "compute_highlights",
    "find_moments",
    "score_candidate",
    "score_false_claim",
]

HIGHLIGHT_ORDER: Final[tuple[HighlightCategory, ...]] = get_args(HighlightCategory)

# What each category rests on. A "private" moment needs a secret mission, a plan, a private value,
# a guess or a claim about one's own cards; the viewer holds its sentence back without the lens.
# Every highlight of the replay carries the basis of its category (``basis``).
HIGHLIGHT_BASIS: Final[Mapping[HighlightCategory, Literal["public", "private"]]] = {
    "winners_curse": "public",
    "shill_stuck": "private",
    "backstab": "public",
    "claim_vs_hand": "private",
    "plan_contradiction": "private",
    "all_pay_tilt": "public",
    "bargain": "private",
    "mindread_miss": "private",
}

BASE_SCORE: Final[Mapping[HighlightCategory, int]] = {
    "shill_stuck": 8,
    "winners_curse": 4,
    "backstab": 4,
    "claim_vs_hand": 5,
    "plan_contradiction": 3,
    "all_pay_tilt": 3,
    "bargain": 3,
    "mindread_miss": 3,
}

MAX_PER_CATEGORY: Final = 3
MAX_SCORE: Final = 10

# Rung ladders, in thousandths of the tier (of the budget span for a mind-reading error).
CURSE_RUNGS: Final = (50, 100, 200)
TILT_RUNGS: Final = (250, 500, 1000)
BARGAIN_RUNGS: Final = (350, 500, 750)
CLAIM_RUNGS: Final = (250, 500, 1000)  # the gap of a false claim, thousandths of tier or budget
PLAN_RUNGS: Final = (250, 500)
MIND_RUNGS: Final = (750, 1000, 1500)
BIG_BREAK: Final = 250  # a pledge broken by at least this much (per mille of the tier)
NARROW_LOSS: Final = 50  # an all-pay loss by no more than this (per mille of the tier)
WILD_GUESS_FACTOR: Final = 3  # guess and truth differ by at least this factor

_SEALED: Final = frozenset({"sealed_first", "sealed_second", "all_pay", "blind_box"})
_COUNTED: Final = frozenset({"ok", "clamped", "kept_previous"})


def _rungs(amount: int, unit: int, ladder: Sequence[int]) -> int:
    """How many thresholds of ``ladder`` (thousandths of ``unit``) the amount reaches."""
    unit = max(1, unit)
    return sum(1 for rung in ladder if amount * 1000 >= rung * unit)


# ------------------------------------------------------------------------------- scoring


def _positive_bids(line: SeatLine) -> list[int]:
    return [
        a.value
        for a in line.actions
        if a.status in _COUNTED and a.value is not None and a.value > 0
    ]


def _record_of(data: LotData, candidate: Candidate) -> PledgeRecord | None:
    """The broken pledge that a ``backstab`` candidate is about."""
    seat, made = candidate.seats[0], candidate.facts["made_in_lot"]
    return next(
        (
            r
            for r in data.pledges_due
            if r.outcome == "violated" and r.seat == seat and r.lot == made
        ),
        None,
    )


def _break_size(data: LotData, record: PledgeRecord) -> int:
    """How much of the money in the lot went against the pledge (a bid, or the price paid)."""
    pledge = record.pledge
    if pledge.kind == "let_win":
        return data.price
    top = max(_positive_bids(data.seat_line(record.seat)), default=0)
    return top - (pledge.amount or 0) if pledge.kind == "max_bid" else top


def _score_backstab(data: LotData, candidate: Candidate, intent: Intent) -> int:
    score = BASE_SCORE["backstab"]
    seat = candidate.seats[0]
    if data.winner == seat:
        score += 2
    if intent == "deliberate":
        score += 2
    record = _record_of(data, candidate)
    if record is not None and _break_size(data, record) * 1000 >= BIG_BREAK * data.tier:
        score += 1
    return score


def _score_plan(data: LotData, candidate: Candidate) -> int:
    score = BASE_SCORE["plan_contradiction"]
    line = data.seat_line(candidate.seats[0])
    if "price" in candidate.facts:  # a lot planned at 0 or 1 star, and won
        price = candidate.facts["price"]
        score += 2 if candidate.facts["stars"] == 0 else 1
        score += _rungs(price, data.tier, PLAN_RUNGS)
        if line.plan_budget is not None and price > line.plan_budget:
            score += 1
    elif (  # a lot planned at 3 stars, and nothing bid
        not line.abstained
        and data.winner not in (None, line.seat)
        and line.plan_budget is not None
        and data.price <= line.plan_budget
    ):
        score += 1
    return score


def _score_tilt(data: LotData, candidate: Candidate) -> int:
    score = BASE_SCORE["all_pay_tilt"]
    paid = candidate.facts["paid"]
    score += _rungs(paid, data.tier, TILT_RUNGS)
    behind = data.price - paid  # the winning bid minus what the seat paid for nothing
    if behind >= 0 and behind * 1000 <= NARROW_LOSS * data.tier:
        score += 1
    return score


def score_candidate(
    data: LotData, candidate: Candidate, *, budget_span: int, intent: Intent = "unknown"
) -> int:
    """The fun score (0 to 10) of a candidate of ``data`` (see the module text for the rules).

    ``budget_span`` is the width of the public budget range, the unit of a mind-reading error.
    ``intent`` is what is known of a broken pledge's intent; it only matters for ``backstab``.
    """
    facts = candidate.facts
    category = candidate.category
    score = BASE_SCORE[category]
    if category == "winners_curse":
        score += _rungs(facts["gap"], data.tier, CURSE_RUNGS)
        if data.prior is not None and data.price > data.prior.high:
            score += 2
    elif category == "shill_stuck":
        own = data.seat_line(candidate.seats[0]).value
        score += int(facts["price"] >= facts["threshold"])
        score += int(own is not None and facts["price"] > own)
    elif category == "backstab":
        score = _score_backstab(data, candidate, intent)
    elif category == "plan_contradiction":
        score = _score_plan(data, candidate)
    elif category == "all_pay_tilt":
        score = _score_tilt(data, candidate)
    elif category == "bargain":
        score += _rungs(facts["gain"], data.tier, BARGAIN_RUNGS)
    elif category == "mindread_miss":
        guess, truth = facts["guess"], facts["truth"]
        score += _rungs(facts["error"], budget_span, MIND_RUNGS)
        if guess * WILD_GUESS_FACTOR <= truth or truth * WILD_GUESS_FACTOR <= guess:
            score += 1
    else:  # pragma: no cover - claim_vs_hand has no candidate, see score_false_claim
        raise ValueError(f"{category} is not scored from a candidate")
    return max(0, min(MAX_SCORE, score))


def score_false_claim(data: LotData | None, verdict: JudgeVerdictData) -> int:
    """The fun score (0 to 10) of a ``false_claim`` verdict.

    ``data`` is the lot the claim was spoken in, if it was played; the claimant "won the lot"
    when it is the winner there. The gap of the claim (``verdict.gap_milli``, already in
    thousandths of the tier or the budget) climbs the ladder :data:`CLAIM_RUNGS`.
    """
    score = BASE_SCORE["claim_vs_hand"]
    if verdict.intent == "deliberate":
        score += 2
    if data is not None and data.winner is not None and data.winner in verdict.seats:
        score += 1
    if verdict.gap_milli is not None:
        score += _rungs(verdict.gap_milli, 1000, CLAIM_RUNGS)
    return max(0, min(MAX_SCORE, score))


# --------------------------------------------------------------------------- the log index


@dataclass(frozen=True, slots=True)
class _LogIndex:
    """Where things are in the log: the event numbers that a moment is placed by."""

    opened: Mapping[int, int]
    hammered: Mapping[int, int]
    talked: Mapping[tuple[int, int], int]
    guessed: Mapping[tuple[int, int], int]
    bid: Mapping[tuple[int, int], int]

    @classmethod
    def of(cls, events: Iterable[AnyEvent]) -> "_LogIndex":
        opened: dict[int, int] = {}
        hammered: dict[int, int] = {}
        talked: dict[tuple[int, int], int] = {}
        guessed: dict[tuple[int, int], int] = {}
        bid: dict[tuple[int, int], int] = {}
        for event in events:
            lot = event.lot
            if lot is None:
                continue
            if isinstance(event, LotOpenEvent):
                opened.setdefault(lot, event.seq)
            elif isinstance(event, HammerEvent):
                hammered.setdefault(lot, event.seq)
            elif isinstance(event, TalkEvent):
                talked.setdefault((lot, event.data.seat), event.seq)
            elif isinstance(event, GuessEvent):
                guessed.setdefault((lot, event.data.seat), event.seq)
            elif isinstance(event, ActionEvent) and event.data.phase in BID_PHASES:
                bid.setdefault((lot, event.data.seat), event.seq)
        return cls(opened, hammered, talked, guessed, bid)

    def span(self, lot: int) -> tuple[int, int]:
        """The first and last event of a lot: its ``lot_open`` and its ``hammer``."""
        if lot not in self.opened or lot not in self.hammered:
            raise ValueError(f"the log has no complete lot {lot}")
        return self.opened[lot], self.hammered[lot]

    def place(self, lot: int, first: int | None, last: int | None) -> tuple[int, int]:
        """A range of events inside the lot. A missing end is the matching end of the lot."""
        lo, hi = self.span(lot)
        begin = lo if first is None else min(max(first, lo), hi)
        end = hi if last is None else min(max(last, begin), hi)
        return begin, end


# ----------------------------------------------------------------------------- the moments


@dataclass(frozen=True, slots=True)
class Moment:
    """A scored moment of the night, before the choice and the numbering."""

    category: HighlightCategory
    lot: int | None
    seats: tuple[int, ...]
    first_seq: int
    last_seq: int
    score: int
    text: str

    @property
    def order(self) -> tuple[int, int, int, int, tuple[int, ...]]:
        """Where the moment stands in the night: lot, first event, last event, category, seats."""
        lot = self.lot if self.lot is not None else MAX_LOTS + 1
        return lot, self.first_seq, self.last_seq, HIGHLIGHT_ORDER.index(self.category), self.seats


def _public_figure(data: LotData, line: SeatLine) -> tuple[str, int] | None:
    """A number of the seat in this lot that the whole room could read after the hammer.

    Sealed formats reveal every bid. In an English lot every seat but the winner announces its
    exit price when it leaves. Whoever won, the price paid is known. Nothing else is public.
    """
    if data.format in _SEALED and line.played is not None:
        return bid_word(data.format), line.played
    if data.winner == line.seat:
        return "付", data.price
    if data.format == "english" and line.played is not None:
        return bid_word(data.format), line.played
    return None


def _backstab_text(data: LotData, record: PledgeRecord) -> str:
    line = data.seat_line(record.seat)
    pledge = record.pledge
    made = "" if record.lot == data.lot else f"在第 {record.lot} 件"
    head = f"{line.seat} 号{line.codename}{made}答应{pledge_text(pledge)}，"
    if pledge.kind == "let_win":
        return f"{head}自己却拍下了，付 {data.price}。"
    limit = pledge.amount if pledge.kind == "max_bid" and pledge.amount is not None else 0
    figure = _public_figure(data, line)
    if figure is None or figure[1] <= limit:
        return f"{head}却没有做到。"
    return f"{head}实际{figure[0]} {figure[1]}。"


def _intent_of(record: PledgeRecord, verdicts: Sequence[JudgeVerdictData]) -> Intent:
    """What is known of the intent behind a broken pledge: its record, else a verdict about it."""
    if record.intent != "unknown":
        return record.intent
    for verdict in verdicts:
        if verdict.verdict != "pledge_violation" or record.seat not in verdict.seats:
            continue
        about = (
            record.seq in verdict.evidence_seqs
            if verdict.evidence_seqs
            else record.pledge.lot in verdict.lots
        )
        if about and verdict.intent is not None:
            return verdict.intent
    return "unknown"


def _from_candidate(
    data: LotData,
    candidate: Candidate,
    index: _LogIndex,
    *,
    budget_span: int,
    verdicts: Sequence[JudgeVerdictData],
) -> Moment:
    lot, seat = data.lot, candidate.seats[0]
    category = candidate.category
    text = candidate.reason
    intent: Intent = "unknown"
    first: int | None = None
    last: int | None = None
    if category in ("winners_curse", "bargain", "all_pay_tilt"):
        first = index.bid.get((lot, seat))
    elif category == "plan_contradiction":
        first = index.talked.get((lot, seat))
    elif category == "mindread_miss":
        first = index.guessed.get((lot, seat), index.talked.get((lot, seat)))
        last = first
    elif category == "backstab":
        record = _record_of(data, candidate)
        if record is not None:
            first = record.seq
            text = _backstab_text(data, record)
            intent = _intent_of(record, verdicts)
    begin, end = index.place(lot, first, last)
    return Moment(
        category=category,
        lot=lot,
        seats=(seat,),
        first_seq=begin,
        last_seq=end,
        score=score_candidate(data, candidate, budget_span=budget_span, intent=intent),
        text=text,
    )


def _sentence(text: str) -> str:
    return text if text.endswith(("。", "！", "？")) else text + "。"


def _from_claim(
    verdict: JudgeVerdictData,
    data: LotData | None,
    index: _LogIndex,
    names: Mapping[int, str],
) -> Moment | None:
    lot = min(verdict.lots) if verdict.lots else None
    seats = tuple(sorted(set(verdict.seats)))
    evidence = sorted(verdict.evidence_seqs)
    if lot is not None and lot in index.opened and lot in index.hammered:
        begin, end = index.place(
            lot, evidence[0] if evidence else None, evidence[-1] if evidence else None
        )
    elif evidence:
        begin, end = evidence[0], evidence[-1]
    else:
        return None  # nothing to place it by
    who = "、".join(f"{seat} 号{names.get(seat, '')}" for seat in seats)
    quote = verdict.quotes[0] if verdict.quotes else ""
    if quote and verdict.detail:
        text = f"{who}说「{quote}」，与底牌对不上：{_sentence(verdict.detail)}"
    elif quote:
        text = f"{who}说「{quote}」，与底牌对不上。"
    elif verdict.detail:
        text = f"{who}的说法与底牌对不上：{_sentence(verdict.detail)}"
    else:
        text = f"{who}的说法与底牌对不上。"
    return Moment(
        category="claim_vs_hand",
        lot=lot,
        seats=seats,
        first_seq=begin,
        last_seq=end,
        score=score_false_claim(data, verdict),
        text=text,
    )


# ------------------------------------------------------------------------- the interface


def choose_moments(moments: Sequence[Moment]) -> list[Moment]:
    """The best ``MAX_PER_CATEGORY`` moments of each category, in the order of the night."""
    kept: list[Moment] = []
    for category in HIGHLIGHT_ORDER:
        mine = sorted(
            (m for m in moments if m.category == category),
            key=lambda m: (-m.score, *m.order),
        )
        kept.extend(mine[:MAX_PER_CATEGORY])
    return sorted(kept, key=lambda m: m.order)


def find_moments(
    cards: FactCards,
    events: Iterable[AnyEvent],
    verdicts: Sequence[JudgeVerdictData] = (),
) -> list[Moment]:
    """Every moment of a finished night, scored and placed, in the order of the night.

    One for each candidate of the fact cards and one for each ``false_claim`` verdict that can be
    placed (one with neither a lot nor evidence numbers cannot). ``events`` is the log of the night
    the cards were built from (audience and system events may be in it; only the engine's own
    events are read). A ``pledge_violation`` verdict can tell the intent behind a broken pledge.
    Raises ``ValueError`` if the log lacks the ``lot_open`` or the ``hammer`` of a lot that has a
    candidate.
    """
    night = cards.night.data
    if not isinstance(night, NightData):  # pragma: no cover - the night card is always NightData
        raise TypeError("the night card holds the data of the night")
    index = _LogIndex.of(events)
    span = night.budget_hi - night.budget_lo
    names = {row.seat: row.codename for row in night.seats}
    lots = {data.lot: data for data in cards.lot_data}
    moments = [
        _from_candidate(data, candidate, index, budget_span=span, verdicts=verdicts)
        for data in cards.lot_data
        for candidate in data.candidates
    ]
    for verdict in verdicts:
        if verdict.verdict != "false_claim":
            continue
        lot = min(verdict.lots) if verdict.lots else None
        found = _from_claim(verdict, lots.get(lot) if lot is not None else None, index, names)
        if found is not None:
            moments.append(found)
    return sorted(moments, key=lambda m: m.order)


def compute_highlights(
    cards: FactCards,
    events: Iterable[AnyEvent],
    verdicts: Sequence[JudgeVerdictData] = (),
) -> list[Highlight]:
    """The highlights of a finished night, for the ``highlights`` part of the replay.

    The best moments of :func:`find_moments`, chosen by :func:`choose_moments` and numbered from 1
    in the order of the night.
    """
    chosen = choose_moments(find_moments(cards, events, verdicts))
    return [
        Highlight(
            id=number,
            category=moment.category,
            score=moment.score,
            lot=moment.lot,
            seats=list(moment.seats),
            first_seq=moment.first_seq,
            last_seq=moment.last_seq,
            text=moment.text,
            basis=HIGHLIGHT_BASIS[moment.category],
        )
        for number, moment in enumerate(chosen, start=1)
    ]
