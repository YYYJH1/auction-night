"""Claims about one's own cards: when two judges found the same one, and when it is not true.

A seat may say things about its hole cards in the public channel: how much money it has, what a
lot is worth to it, what its secret mission is. A **claim** is a finding only if both judges
extracted it and neither was unsure, and it is **false** only if what the deal gave the seat at
that moment lies outside a tolerance. Promises made in words are not checked here: structured
pledges are judged by program (:mod:`auction_night.show.pledges`), and free-text promises matter
only for collusion (:mod:`auction_night.show.verdicts`).

The same claim
--------------
Two extractions, one from each judge, are *the same claim* when all of these hold:

1. both are claims (``kind == "claim"``) and neither is ``ambiguous``;
2. same seat, same lot, same ``topic`` (the topic must be set);
3. the values do not conflict: if both give a number the numbers are equal, and if both give a
   ``text_value`` the texts are equal (case and surrounding space aside);
4. and either the values agree (equal numbers, or equal texts) **or** the quotes overlap.

Two quotes *overlap* when, with spaces, punctuation and symbols removed, one contains the other,
or they share a run of at least :data:`MIN_OVERLAP_CHARS` characters that is at least half of the
shorter quote. The agreed value of a pair is the number (or text) that either judge gave.

Two promises are *the same promise* under the same seat, lot and class rule (both neither
ambiguous, both of the same ``promise_class``: ``collusion``, ``bid`` or ``other``, where a promise
without a class is ``other``) and overlapping quotes.

False
-----
Each topic is checked against the truth of the lot in which the claim was spoken, with integer
arithmetic only:

================ =============================================================================
topic            false when
================ =============================================================================
``budget``       the claimed amount is more than 10% away from the seat's remaining budget
                 before that lot **and** more than 10% away from its opening budget (a seat may
                 mean either, so only a number that fits neither is false)
``valuation``    the claimed amount is more than 15% of the lot's tier away from the seat's
                 value for that lot, or from its appraisal if the lot is a blind box
``mission_kind`` the claimed kind (``collector``, ``shill`` or ``none``) is not the seat's
``mission_tag``  the seat is not a collector, or its tag is another one
``plan``, none   never checked
================ =============================================================================

A claim that cannot be checked (no number or text, a topic that is not checked, a kind of
mission other than the three words, a speaker that is a bot) is *unverifiable* and never makes a
finding. The tolerance is inclusive: a claim exactly 10% (or 15% of the tier) away is still true.

The gap
-------
A false claim about an amount also says how far off it was, as ``gap_milli``: the distance between
the claimed number and the truth in whole thousandths (rounded down) of

* the tier of the lot, for a ``valuation`` (against the seat's value, or its appraisal in a blind
  box);
* the budget, for a ``budget``. The claim is held against both readings the seat may have meant
  (what it had left, and what it started with); the gap is the smaller of the two, each in
  thousandths of the reading it is measured against, so the number is what the claim missed even
  on the reading that suits it best.

A false claim about the kind or the tag of the mission has no gap (``None``). A false amount is
at least 100 (budget) or 150 (valuation) thousandths off, since that is where the tolerance ends.
"""

import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Final, Literal

from auction_night.contracts.events import Extraction, JudgeExtractData
from auction_night.show.judge_input import JudgeInput
from auction_night.show.truth import Truth, mission_kind_of

__all__ = [
    "BUDGET_TOLERANCE_PERCENT",
    "MIN_OVERLAP_CHARS",
    "VALUATION_TOLERANCE_PERCENT",
    "ClaimCheck",
    "FalseClaim",
    "agreed_pairs",
    "check_claim",
    "false_claims",
    "promise_class",
    "quotes_overlap",
    "same_claim",
    "same_promise",
]

BUDGET_TOLERANCE_PERCENT: Final = 10  # of the budget
VALUATION_TOLERANCE_PERCENT: Final = 15  # of the tier of the lot
MIN_OVERLAP_CHARS: Final = 6

_KIND_ZH: Final = {"collector": "收藏家", "shill": "托儿", "none": "没有任务"}


# ---------------------------------------------------------------------------- matching


def _compact(text: str) -> str:
    """Text without white space, punctuation and symbols, in one case."""
    return "".join(
        char
        for char in text.casefold()
        if not char.isspace() and unicodedata.category(char)[0] not in "PSZ"
    )


def quotes_overlap(first: str, second: str) -> bool:
    """Whether two quotes cover the same words (see the module text)."""
    a, b = _compact(first), _compact(second)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    match = SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b))
    return match.size >= MIN_OVERLAP_CHARS and 2 * match.size >= min(len(a), len(b))


def _text(extraction: Extraction) -> str:
    return (extraction.text_value or "").strip().casefold()


def same_claim(first: Extraction, second: Extraction) -> bool:
    """Whether two extractions, one from each judge, are the same claim."""
    if first.kind != "claim" or second.kind != "claim":
        return False
    if first.ambiguous or second.ambiguous or first.lot is None:
        return False
    if (first.seat, first.lot) != (second.seat, second.lot):
        return False
    if first.topic is None or first.topic != second.topic:
        return False
    a_text, b_text = _text(first), _text(second)
    if first.number is not None and second.number is not None and first.number != second.number:
        return False
    if a_text and b_text and a_text != b_text:
        return False
    if first.number is not None and first.number == second.number:
        return True
    if a_text and a_text == b_text:
        return True
    return quotes_overlap(first.quote, second.quote)


def promise_class(extraction: Extraction) -> Literal["collusion", "bid", "other"]:
    """The class a judge gave a promise: ``collusion`` (an arrangement between seats), ``bid`` (a
    promise about its own bidding) or ``other``. A promise without a class is ``other``."""
    return extraction.promise_class or "other"


def same_promise(first: Extraction, second: Extraction) -> bool:
    """Whether two extractions, one from each judge, are the same promise."""
    if first.kind != "promise" or second.kind != "promise":
        return False
    if first.ambiguous or second.ambiguous or first.lot is None:
        return False
    if (first.seat, first.lot) != (second.seat, second.lot):
        return False
    if promise_class(first) != promise_class(second):
        return False
    return quotes_overlap(first.quote, second.quote)


def agreed_pairs(
    first: JudgeExtractData,
    second: JudgeExtractData,
    same: Callable[[Extraction, Extraction], bool],
) -> list[tuple[Extraction, Extraction]]:
    """The extractions of the two judges that ``same`` pairs up, in the first judge's order.

    Each extraction of the second judge is used at most once.
    """
    pairs: list[tuple[Extraction, Extraction]] = []
    used: set[int] = set()
    for a in first.extractions:
        for index, b in enumerate(second.extractions):
            if index not in used and same(a, b):
                used.add(index)
                pairs.append((a, b))
                break
    return pairs


# ------------------------------------------------------------------------------- truth


@dataclass(frozen=True, slots=True)
class ClaimCheck:
    """The result of holding a claim against the truth.

    ``status`` is ``false`` (outside the tolerance), ``consistent`` or ``unverifiable``.
    ``detail`` is one Chinese sentence for the verdict. ``gap_milli`` is the gap of a false claim
    about an amount (see the module text), else None.
    """

    status: Literal["false", "consistent", "unverifiable"]
    detail: str = ""
    gap_milli: int | None = None


def _near(claimed: int, actual: int, allowed_times_100: int) -> bool:
    """Whether ``|claimed - actual| <= allowed`` where ``allowed = allowed_times_100 / 100``."""
    return abs(claimed - actual) * 100 <= allowed_times_100


def _gap_milli(claimed: int, actual: int, unit: int) -> int:
    """``|claimed - actual|`` in whole thousandths of ``unit`` (a unit below 1 counts as 1)."""
    return abs(claimed - actual) * 1000 // max(1, unit)


def _check_budget(claim: Extraction, truth: Truth) -> ClaimCheck:
    if claim.number is None or claim.lot is None or claim.seat not in truth.budgets:
        return ClaimCheck("unverifiable")
    left = truth.remaining(claim.seat, claim.lot)
    opening = truth.budgets[claim.seat]
    for actual in {left, opening}:
        if _near(claim.number, actual, BUDGET_TOLERANCE_PERCENT * actual):
            return ClaimCheck("consistent")
    return ClaimCheck(
        "false",
        f"自称预算 {claim.number}，当时实际还剩 {left}（开场 {opening}），"
        f"差距超出 {BUDGET_TOLERANCE_PERCENT}%。",
        gap_milli=min(_gap_milli(claim.number, actual, actual) for actual in (left, opening)),
    )


def _check_valuation(claim: Extraction, truth: Truth) -> ClaimCheck:
    if claim.number is None or claim.lot is None or claim.lot not in truth.tiers:
        return ClaimCheck("unverifiable")
    actual = truth.own_number(claim.seat, claim.lot)
    if actual is None:
        return ClaimCheck("unverifiable")
    tier = truth.tiers[claim.lot]
    if _near(claim.number, actual, VALUATION_TOLERANCE_PERCENT * tier):
        return ClaimCheck("consistent")
    word = "鉴定价" if truth.formats.get(claim.lot) == "blind_box" else "估值"
    return ClaimCheck(
        "false",
        f"自称{word} {claim.number}，实际是 {actual}，差距超出档位 {tier} 的 "
        f"{VALUATION_TOLERANCE_PERCENT}%。",
        gap_milli=_gap_milli(claim.number, actual, tier),
    )


def _check_mission_kind(claim: Extraction, truth: Truth) -> ClaimCheck:
    said = _text(claim)
    if said not in _KIND_ZH:
        return ClaimCheck("unverifiable")
    actual = mission_kind_of(truth.mission_of(claim.seat))
    if said == actual:
        return ClaimCheck("consistent")
    return ClaimCheck("false", f"自称{_KIND_ZH[said]}，实际是{_KIND_ZH[actual]}。")


def _check_mission_tag(claim: Extraction, truth: Truth) -> ClaimCheck:
    said = (claim.text_value or "").strip()
    if not said:
        return ClaimCheck("unverifiable")
    collector = truth.collector_of(claim.seat)
    if collector is not None and collector.tag == said:
        return ClaimCheck("consistent")
    if collector is None:
        actual = _KIND_ZH[mission_kind_of(truth.mission_of(claim.seat))]
        return ClaimCheck("false", f"自称在收集「{said}」，实际是{actual}。")
    return ClaimCheck("false", f"自称在收集「{said}」，实际收集的是「{collector.tag}」。")


def check_claim(claim: Extraction, truth: Truth) -> ClaimCheck:
    """Hold one claim against the truth (the rules are in the module text)."""
    if claim.kind != "claim" or claim.seat not in truth.players:
        return ClaimCheck("unverifiable")
    if claim.topic == "budget":
        return _check_budget(claim, truth)
    if claim.topic == "valuation":
        return _check_valuation(claim, truth)
    if claim.topic == "mission_kind":
        return _check_mission_kind(claim, truth)
    if claim.topic == "mission_tag":
        return _check_mission_tag(claim, truth)
    return ClaimCheck("unverifiable")


# --------------------------------------------------------------------------- the findings


@dataclass(frozen=True, slots=True)
class FalseClaim:
    """A claim that both judges found and that the truth contradicts.

    ``gap_milli`` is how far off a claim about an amount was (see the module text), None for a
    claim about the kind or the tag of the mission.
    """

    seat: int
    lot: int
    topic: str
    quotes: tuple[str, ...]
    seq: int | None
    detail: str
    gap_milli: int | None = None


def _merged(first: Extraction, second: Extraction) -> Extraction:
    """The pair as one claim: the number and the text that either judge gave."""
    return first.model_copy(
        update={
            "number": first.number if first.number is not None else second.number,
            "text_value": first.text_value or second.text_value,
        }
    )


def false_claims(
    first: JudgeExtractData, second: JudgeExtractData, truth: Truth, record: JudgeInput
) -> list[FalseClaim]:
    """The claims that both judges found and that are false, in lot, seat and topic order.

    ``record`` gives the sequence number of the line that holds each claim. The same claim found
    twice (a seat that says it in two sentences) is reported once.
    """
    found: dict[tuple[int, int, str, int | str], FalseClaim] = {}
    for a, b in agreed_pairs(first, second, same_claim):
        claim = _merged(a, b)
        assert claim.lot is not None and claim.topic is not None
        check = check_claim(claim, truth)
        if check.status != "false":
            continue
        value: int | str = claim.number if claim.number is not None else _text(claim)
        key = (claim.lot, claim.seat, claim.topic, value)
        if key in found:
            continue
        quotes = tuple(dict.fromkeys((a.quote, b.quote)))
        found[key] = FalseClaim(
            seat=claim.seat,
            lot=claim.lot,
            topic=claim.topic,
            quotes=quotes,
            seq=record.seq_of.get((claim.seat, claim.lot)),
            detail=check.detail,
            gap_milli=check.gap_milli,
        )
    return [found[key] for key in sorted(found, key=lambda k: (k[0], k[1], k[2], str(k[3])))]
