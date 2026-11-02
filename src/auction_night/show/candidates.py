"""Highlight candidates: moments of a lot that may deserve a replay highlight.

The fact card lists *candidates* and nothing more. Giving them a score from 0 to 10, choosing
the best ones and writing the replay's highlight texts is the job of a later block. A candidate
is found by a plain rule on the facts of one lot, so the same card always has the same list:

================== =============================================================================
category           rule
================== =============================================================================
winners_curse      a blind box was sold for more than its true value
shill_stuck        the holder of a shill mission bought its own target lot
backstab           a pledge that concerns this lot was broken
plan_contradiction a seat that planned 0 or 1 stars for the lot won it, or a seat that planned
                   3 stars was asked and bid nothing (at most two per lot)
all_pay_tilt       an all-pay loser paid at least a tenth of the tier for nothing
bargain            a winner of a private-value lot paid at least a fifth of the tier less than
                   its value
mindread_miss      a mind-reading guess was off by at least ``miss_error`` (one per lot)
================== =============================================================================

``claim_vs_hand`` (a claim about one's own cards that the truth contradicts) needs the judges and
is not produced here.

A seat that the cost gate muted did not bid nothing, so it cannot contradict its plan. A lot in
which the gate muted a seat has no bargain either: the price was not set by a field of bidders.
"""

from collections.abc import Sequence

from auction_night.contracts.replay import HighlightCategory
from auction_night.show.cards import Candidate, LotData, SeatLine

__all__ = ["CATEGORY_ORDER", "find_candidates"]

CATEGORY_ORDER: tuple[HighlightCategory, ...] = (
    "winners_curse",
    "shill_stuck",
    "backstab",
    "plan_contradiction",
    "all_pay_tilt",
    "bargain",
    "mindread_miss",
)
_MAX_PLAN_CANDIDATES = 2


def _who(line: SeatLine) -> str:
    return f"{line.seat} 号{line.codename}"


def _winners_curse(data: LotData) -> list[Candidate]:
    if data.winner is None or data.true_value is None or data.price <= data.true_value:
        return []
    gap = data.price - data.true_value
    who = _who(data.seat_line(data.winner))
    return [
        Candidate(
            category="winners_curse",
            lot=data.lot,
            seats=(data.winner,),
            facts={"price": data.price, "true_value": data.true_value, "gap": gap},
            reason=f"{who}付 {data.price} 拍下盲盒，真值只有 {data.true_value}，多付 {gap}。",
        )
    ]


def _shill_stuck(data: LotData) -> list[Candidate]:
    found = []
    for mission in data.missions:
        if mission.kind != "shill" or mission.outcome != "stuck":
            continue
        who = _who(data.seat_line(mission.seat))
        assert mission.price is not None and mission.threshold is not None
        found.append(
            Candidate(
                category="shill_stuck",
                lot=data.lot,
                seats=(mission.seat,),
                facts={"price": mission.price, "threshold": mission.threshold},
                reason=(
                    f"托儿 {who}把第 {data.lot} 件抬到 {mission.price}"
                    f"（门槛 {mission.threshold}），结果被自己买下。"
                ),
            )
        )
    return found


def _backstab(data: LotData) -> list[Candidate]:
    return [
        Candidate(
            category="backstab",
            lot=data.lot,
            seats=(record.seat,),
            facts={"made_in_lot": record.lot, "pledged_lot": record.pledge.lot},
            reason=f"{_who(data.seat_line(record.seat))}违背誓约：{record.detail}。",
        )
        for record in data.pledges_due
        if record.outcome == "violated"
    ]


def _bid_nothing(line: SeatLine) -> bool:
    return line.abstained or line.played == 0


def _plan_contradiction(data: LotData) -> list[Candidate]:
    found = []
    for line in data.seats:
        stars = line.plan_stars
        if stars is None:
            continue
        if stars <= 1 and line.won:
            reason = f"{_who(line)}计划里这件只有 {stars} 星，却以 {data.price} 拍下了。"
            facts = {"stars": stars, "price": data.price}
        elif stars == 3 and line.asked and not line.muted and _bid_nothing(line):
            reason = f"{_who(line)}计划里这件是 {stars} 星，真到了现场却一分没出。"
            facts = {"stars": stars}
        else:
            continue
        found.append(
            Candidate(
                category="plan_contradiction",
                lot=data.lot,
                seats=(line.seat,),
                facts=facts,
                reason=reason,
            )
        )
    return found[:_MAX_PLAN_CANDIDATES]


def _all_pay_tilt(data: LotData) -> list[Candidate]:
    if data.format != "all_pay":
        return []
    losers = [
        (paid, seat) for seat, paid in data.payments.items() if seat != data.winner and paid > 0
    ]
    if not losers:
        return []
    paid, seat = max(losers, key=lambda pair: (pair[0], -pair[1]))
    if paid * 10 < data.tier:
        return []
    return [
        Candidate(
            category="all_pay_tilt",
            lot=data.lot,
            seats=(seat,),
            facts={"paid": paid, "tier": data.tier},
            reason=f"{_who(data.seat_line(seat))}没拍到全员付费的第 {data.lot} 件，白付了 {paid}。",
        )
    ]


def _bargain(data: LotData) -> list[Candidate]:
    if data.winner is None or data.prior is not None or data.bids_muted:
        return []
    line = data.seat_line(data.winner)
    if line.value is None:
        return []
    gain = line.value - data.price
    if gain * 5 < data.tier:
        return []
    return [
        Candidate(
            category="bargain",
            lot=data.lot,
            seats=(data.winner,),
            facts={"value": line.value, "price": data.price, "gain": gain},
            reason=f"{_who(line)}估值 {line.value}，只付 {data.price}，捡漏 {gain}。",
        )
    ]


def _mindread_miss(data: LotData, miss_error: int) -> list[Candidate]:
    worst: SeatLine | None = None
    for line in data.seats:
        guess = line.guess
        if guess is None or guess.abs_error < miss_error:
            continue
        if worst is None or worst.guess is None or guess.abs_error > worst.guess.abs_error:
            worst = line
    if worst is None or worst.guess is None:
        return []
    guess = worst.guess
    return [
        Candidate(
            category="mindread_miss",
            lot=data.lot,
            seats=(worst.seat, guess.target),
            facts={"guess": guess.guess, "truth": guess.truth, "error": guess.abs_error},
            reason=(
                f"{_who(worst)}猜 {guess.target} 号预算余 {guess.guess}，"
                f"实际 {guess.truth}，差了 {guess.abs_error}。"
            ),
        )
    ]


def find_candidates(data: LotData, *, miss_error: int) -> tuple[Candidate, ...]:
    """The highlight candidates of one lot, in the order of :data:`CATEGORY_ORDER`.

    ``miss_error`` is how far a mind-reading guess must be off to count as a miss.
    """
    found: Sequence[Candidate] = [
        *_winners_curse(data),
        *_shill_stuck(data),
        *_backstab(data),
        *_plan_contradiction(data),
        *_all_pay_tilt(data),
        *_bargain(data),
        *_mindread_miss(data, miss_error),
    ]
    return tuple(found)
