"""Awards: ten prizes for the model seats, and the same four figures for the bots as a reference.

The awards are a pure function of the fact cards of a finished night, of the pledge records that
the program decided and of the verdicts of the judges. Nothing reads a clock, an environment or a
random generator, and every collection is walked in lot and seat order, so the same night always
gives the same bytes. All numbers are integers (coins, counts, thousandths); there are no floats.

The table
---------
``worth`` is the value of the lot to its winner: the seat's own value, or the hidden true value
of a blind box. ``span`` is the width of the public budget range (``budget_hi - budget_lo``).

================== ============================================ ========= =========================
key                the seat that wins                           unit      lower limit
================== ============================================ ========= =========================
``most_textbook``  lowest mean of ``|bid - reference| / tier``  milli     at least 6 valid bids
                   over its bids in ordinary lots
``winners_curse``  greatest ``price - true value`` over the     coins     the gap is at least
                   blind boxes it won                                     1/20 of that box's tier
``tilt``           most coins paid in all-pay lots that it      coins     at least 10 coins
                   did not win, summed over those lots
``bargain``        greatest ``worth - price`` over the lots     coins     the gain is at least
                   it won                                                 1/5 of that lot's tier
``backstabber``    most structured pledges broken (decided      count     at least 1
                   by program, see :mod:`.pledges`)
``best_actor``     most claims about its own cards that both    count     at least 1
                   judges found and the deal contradicts
                   (verdict ``false_claim``)
``keeper``         most pledges kept among the seats that       count     at least 2 kept, none
                   kept all of theirs                                     broken
``mission_master`` completed its secret mission earliest        none      a completed mission
``mind_reader``    lowest mean of ``|guess - truth| / span``    milli     at least 8 valid guesses
``consistent``     highest share of judgable lots in which      milli     at least 10 judgable lots
                   what it did followed its opening plan
================== ============================================ ========= =========================

``coins`` and ``count`` figures are exactly what the table says. A ``milli`` figure is the ratio
in thousandths, rounded down (like every ``_milli`` number of the show layer and of the theory
module). ``most_textbook`` is "average distance to the textbook, in tiers", ``mind_reader`` is
"average error, in budget ranges" and ``consistent`` is "share of judgable lots", each times 1000.
The viewer shows exactly these units (``viewer/lib/strings.js``), and a test compares the two.
``mission_master`` has no figure: its ``metric`` is empty and its unit ``none``.

Ties and vacancies
------------------
Seats with the same published figure win together, in seat order. The figure compared is the
integer in the replay, so anybody can check an award from the published numbers. An award that no
seat reaches the lower limit for is **vacant**: no winners, no figure.

Only model seats compete (every seat whose role is ``player``). Bots never win: the four awards
that need nothing but bids and payments (``most_textbook``, ``winners_curse``, ``tilt``,
``bargain``) are computed again among the bots alone, with the same limits, and returned as the
reference list. The other six need a plan, a guess, a pledge, a claim or a mission, which a rule
bot does not have.

What counts
-----------
* A **valid bid** (``most_textbook``) is the number of a seat that was asked, gave a usable action
  and has a textbook reference, in a lot that is not a blind box: the bid, the final English exit
  price or the Dutch grab price. A bid of 0 is a bid. An abstention is not.
* A **valid guess** (``mind_reader``) is a mind-reading guess that the engine recorded, with the
  truth it was asked against (the target's remaining budget before that lot).
* A lot is **judgable** (``consistent``) for a seat when it has a plan, was asked, gave a usable
  action, has a positive number of its own (value, or appraisal in a blind box) and could afford a
  serious bid (twice its remaining budget is at least that number). The seat was **consistent**
  there when it planned 2 or 3 stars and bid at least half of its own number, or planned 0 or 1
  star and bid less than that. "Did not bid much" is the complement of "bid seriously", and a bid
  of 0 is "not much".
* ``keeper`` counts the pledges that could be tested. A pledge that was void (the seat was no
  longer asked to bid, or the cost gate skipped its bid) counts neither for nor against.
* A seat that the cost gate muted in a lot (``SeatLine.muted``) made no bid and no choice there:
  it has no valid bid and no judgable lot in it. A lot with a muted seat gives no ``bargain``,
  to a seat or to a bot, because its price was not set by a field of bidders.
* ``mission_master`` is decided by the lot at which a mission was completed
  (:func:`~auction_night.engine.missions.completion_lot`); missions completed at the same lot tie.

What the awards rest on (``AWARD_BASIS``) is what the viewer holds back while the hole-card lens
is off: ``most_textbook``, ``bargain``, ``best_actor``, ``mind_reader`` and ``consistent`` need
private numbers, plans, guesses or claims, the other five follow from the public record. Every
award, a vacant one included, carries its basis in the replay (``basis``), so the table the viewer
keeps and the file always agree.

Each award is judged by its own rule: ``winners_curse`` and ``bargain`` look at one lot at a time,
``tilt`` adds coins that were really paid, and ``most_textbook`` and ``mind_reader`` average a
distance, as their rules say.

Per-seat reference columns (:class:`SeatMetrics`) come from the same pass, so the leaderboard
columns and the awards can never disagree.
"""

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Final, Literal

from auction_night.contracts.events import (
    AWARD_KEYS,
    AwardData,
    AwardEvent,
    AwardKey,
    JudgeVerdictData,
    MetricUnit,
    MissionResultData,
)
from auction_night.contracts.mechanism import MECHANISMS
from auction_night.contracts.replay import PledgeRecord
from auction_night.engine.missions import completion_lot
from auction_night.show.cards import LotData
from auction_night.show.factcard import FactCards, NightData

__all__ = [
    "AWARD_BASIS",
    "AWARD_UNITS",
    "BOT_AWARDS",
    "MIN_JUDGABLE_LOTS",
    "MIN_KEPT_PLEDGES",
    "MIN_TEXTBOOK_BIDS",
    "MIN_TILT_COINS",
    "MIN_VALID_GUESSES",
    "Awards",
    "SeatMetrics",
    "build_awards",
    "compute_awards",
    "seat_metrics",
]

# The unit of the figure of each award, as the viewer shows it.
AWARD_UNITS: Final[Mapping[AwardKey, MetricUnit]] = {
    "most_textbook": "milli",
    "winners_curse": "coins",
    "tilt": "coins",
    "bargain": "coins",
    "backstabber": "count",
    "best_actor": "count",
    "keeper": "count",
    "mission_master": "none",
    "mind_reader": "milli",
    "consistent": "milli",
}

# What each award rests on. A "private" award needs numbers the players kept to themselves.
AWARD_BASIS: Final[Mapping[AwardKey, Literal["public", "private"]]] = {
    "most_textbook": "private",
    "winners_curse": "public",
    "tilt": "public",
    "bargain": "private",
    "backstabber": "public",
    "best_actor": "private",
    "keeper": "public",
    "mission_master": "public",
    "mind_reader": "private",
    "consistent": "private",
}

# The awards that a rule bot can be measured on, in table order.
BOT_AWARDS: Final[tuple[AwardKey, ...]] = ("most_textbook", "winners_curse", "tilt", "bargain")

MIN_TEXTBOOK_BIDS: Final = 6  # valid bids for ``most_textbook``
MIN_VALID_GUESSES: Final = 8  # valid guesses for ``mind_reader``
MIN_JUDGABLE_LOTS: Final = 10  # judgable lots for ``consistent``
MIN_TILT_COINS: Final = 10  # coins paid without winning for ``tilt``
MIN_KEPT_PLEDGES: Final = 2  # pledges kept (and none broken) for ``keeper``
CURSE_TIER_DIVISOR: Final = 20  # ``winners_curse``: the gap is at least tier / 20
BARGAIN_TIER_DIVISOR: Final = 5  # ``bargain``: the gain is at least tier / 5
HIGH_PLAN_STARS: Final = 2  # a plan of this many stars or more means "bid seriously"
SERIOUS_DIVISOR: Final = 2  # a serious bid is at least half of the seat's own number

Role = Literal["player", "bot"]


# ----------------------------------------------------------------------- per-seat columns


@dataclass(frozen=True, slots=True)
class SeatMetrics:
    """The reference columns of one seat, from the lots of the night.

    The ``*_milli`` figures are ``None`` when the seat has nothing to average. They are the
    figures the awards rank, so a leaderboard column and an award always agree. Their names
    are those of :class:`~auction_night.contracts.replay.SeatStats`; :meth:`stats_update` gives
    them in that form.
    """

    seat: int
    role: Role
    textbook_bids: int = 0
    textbook_deviation_milli: int | None = None
    valid_guesses: int = 0
    mind_error_milli: int | None = None
    judgable_lots: int = 0
    consistent_lots: int = 0
    consistency_milli: int | None = None

    def stats_update(self) -> dict[str, int | None]:
        """The fields of ``SeatStats`` that these metrics fill, for ``model_copy(update=...)``."""
        return {
            "textbook_bids": self.textbook_bids,
            "textbook_deviation_milli": self.textbook_deviation_milli,
            "valid_guesses": self.valid_guesses,
            "mind_error_milli": self.mind_error_milli,
            "judgable_lots": self.judgable_lots,
            "consistency_milli": self.consistency_milli,
        }


def _is_blind(data: LotData) -> bool:
    return MECHANISMS[data.format].value_model == "common"


def _milli(numerator: int, denominator: int) -> int:
    """``numerator / denominator`` in whole thousandths, rounded down."""
    return numerator * 1000 // denominator


def seat_metrics(lots: Iterable[LotData], *, budget_span: int) -> dict[int, SeatMetrics]:
    """The reference columns of every seat that appears in ``lots``, in seat order.

    ``budget_span`` is the width of the public budget range, the unit of the mind-reading error
    (a span below 1 is taken as 1, so that a degenerate range cannot divide by zero).
    """
    span = max(1, budget_span)
    roles: dict[int, Role] = {}
    bids: Counter[int] = Counter()
    distance: defaultdict[int, Fraction] = defaultdict(Fraction)
    guesses: Counter[int] = Counter()
    error: Counter[int] = Counter()
    judged: Counter[int] = Counter()
    followed: Counter[int] = Counter()
    for data in sorted(lots, key=lambda entry: entry.lot):
        ordinary = not _is_blind(data)
        for line in data.seats:
            roles.setdefault(line.seat, line.role)
            if line.guess is not None:
                guesses[line.seat] += 1
                error[line.seat] += line.guess.abs_error
            if not line.asked or line.abstained or line.played is None:
                continue
            if ordinary and line.ref_bid is not None:
                bids[line.seat] += 1
                distance[line.seat] += Fraction(abs(line.played - line.ref_bid), data.tier)
            own = line.value if line.value is not None else line.signal
            if (
                line.plan_stars is not None
                and own is not None
                and own > 0
                and SERIOUS_DIVISOR * line.budget_before >= own
            ):
                judged[line.seat] += 1
                serious = SERIOUS_DIVISOR * line.played >= own
                if (line.plan_stars >= HIGH_PLAN_STARS) == serious:
                    followed[line.seat] += 1
    found = {}
    for seat in sorted(roles):
        total = distance[seat]
        found[seat] = SeatMetrics(
            seat=seat,
            role=roles[seat],
            textbook_bids=bids[seat],
            textbook_deviation_milli=(
                _milli(total.numerator, total.denominator * bids[seat]) if bids[seat] else None
            ),
            valid_guesses=guesses[seat],
            mind_error_milli=(_milli(error[seat], guesses[seat] * span) if guesses[seat] else None),
            judgable_lots=judged[seat],
            consistent_lots=followed[seat],
            consistency_milli=_milli(followed[seat], judged[seat]) if judged[seat] else None,
        )
    return found


# ------------------------------------------------------------------------- the awards


def _top(key: AwardKey, scores: Mapping[int, int], *, lowest: bool = False) -> AwardData:
    """The award for the seats with the best figure (all of them on a tie), or a vacant one."""
    if not scores:
        return AwardData(award=key, vacant=True, basis=AWARD_BASIS[key])
    best = min(scores.values()) if lowest else max(scores.values())
    winners = sorted(seat for seat, figure in scores.items() if figure == best)
    return AwardData(
        award=key,
        winners=winners,
        metric=best,
        metric_unit=AWARD_UNITS[key],
        basis=AWARD_BASIS[key],
    )


def _textbook(metrics: Mapping[int, SeatMetrics], group: frozenset[int]) -> dict[int, int]:
    return {
        seat: m.textbook_deviation_milli
        for seat, m in metrics.items()
        if seat in group
        and m.textbook_bids >= MIN_TEXTBOOK_BIDS
        and m.textbook_deviation_milli is not None
    }


def _curse(lots: Sequence[LotData], group: frozenset[int]) -> dict[int, int]:
    found: dict[int, int] = {}
    for data in lots:
        winner = data.winner
        if winner is None or winner not in group or data.true_value is None or not _is_blind(data):
            continue
        gap = data.price - data.true_value
        if gap * CURSE_TIER_DIVISOR >= data.tier:
            found[winner] = max(found.get(winner, gap), gap)
    return found


def _tilt(lots: Sequence[LotData], group: frozenset[int]) -> dict[int, int]:
    lost: Counter[int] = Counter()
    for data in lots:
        if data.format != "all_pay":
            continue
        for seat, amount in data.payments.items():
            if seat in group and seat != data.winner:
                lost[seat] += amount
    return {seat: total for seat, total in lost.items() if total >= MIN_TILT_COINS}


def _bargain(lots: Sequence[LotData], group: frozenset[int]) -> dict[int, int]:
    found: dict[int, int] = {}
    for data in lots:
        winner = data.winner
        if winner is None or winner not in group or data.bids_muted:
            continue
        worth = data.true_value if _is_blind(data) else data.seat_line(winner).value
        if worth is None:
            continue
        gain = worth - data.price
        if gain * BARGAIN_TIER_DIVISOR >= data.tier:
            found[winner] = max(found.get(winner, gain), gain)
    return found


def _broken(pledges: Sequence[PledgeRecord], group: frozenset[int]) -> dict[int, int]:
    count = Counter(p.seat for p in pledges if p.outcome == "violated" and p.seat in group)
    return dict(count)


def _false_claims(verdicts: Sequence[JudgeVerdictData], group: frozenset[int]) -> dict[int, int]:
    count: Counter[int] = Counter()
    for verdict in verdicts:
        if verdict.verdict == "false_claim":
            count.update(seat for seat in set(verdict.seats) if seat in group)
    return dict(count)


def _kept(pledges: Sequence[PledgeRecord], group: frozenset[int]) -> dict[int, int]:
    kept = Counter(p.seat for p in pledges if p.outcome == "kept" and p.seat in group)
    broken = {p.seat for p in pledges if p.outcome == "violated"}
    return {seat: n for seat, n in kept.items() if n >= MIN_KEPT_PLEDGES and seat not in broken}


def _first_to_finish(missions: Sequence[MissionResultData], group: frozenset[int]) -> AwardData:
    done = {
        m.seat: lot for m in missions if m.seat in group and (lot := completion_lot(m)) is not None
    }
    if not done:
        return AwardData(award="mission_master", vacant=True, basis=AWARD_BASIS["mission_master"])
    first = min(done.values())
    return AwardData(
        award="mission_master",
        winners=sorted(s for s, lot in done.items() if lot == first),
        basis=AWARD_BASIS["mission_master"],
    )


def _mind(metrics: Mapping[int, SeatMetrics], group: frozenset[int]) -> dict[int, int]:
    return {
        seat: m.mind_error_milli
        for seat, m in metrics.items()
        if seat in group and m.valid_guesses >= MIN_VALID_GUESSES and m.mind_error_milli is not None
    }


def _consistency(metrics: Mapping[int, SeatMetrics], group: frozenset[int]) -> dict[int, int]:
    return {
        seat: m.consistency_milli
        for seat, m in metrics.items()
        if seat in group
        and m.judgable_lots >= MIN_JUDGABLE_LOTS
        and m.consistency_milli is not None
    }


@dataclass(frozen=True, slots=True)
class Awards:
    """The awards of a night.

    ``models`` has one entry for each of the ten awards, in table order, for the model seats.
    ``bots`` is the reference list of the bots (:data:`BOT_AWARDS`, same order), and empty when no
    bot sits at the table. ``metrics`` are the reference columns of every seat.
    """

    models: tuple[AwardData, ...]
    bots: tuple[AwardData, ...]
    metrics: Mapping[int, SeatMetrics]

    def award(self, key: AwardKey) -> AwardData:
        """The award of the model seats with this key."""
        return next(entry for entry in self.models if entry.award == key)

    def bot_award(self, key: AwardKey) -> AwardData:
        """The reference entry of the bots with this key."""
        return next(entry for entry in self.bots if entry.award == key)

    def events(self) -> tuple[AwardEvent, ...]:
        """The ``award`` events of the log (model seats only), numbered from 1.

        :func:`~auction_night.engine.night.append_events` renumbers them after the log.
        """
        return tuple(
            AwardEvent(seq=index, lot=None, vis="audience", data=entry)
            for index, entry in enumerate(self.models, start=1)
        )


def build_awards(
    lots: Iterable[LotData],
    *,
    pledges: Sequence[PledgeRecord] = (),
    missions: Sequence[MissionResultData] = (),
    verdicts: Sequence[JudgeVerdictData] = (),
    budget_span: int,
) -> Awards:
    """The awards from the parts they are made of.

    ``lots`` are the decided lots (the data of the fact cards), ``pledges`` the pledge records
    that :func:`~auction_night.show.pledges.judge_pledges` decided, ``missions`` the mission
    results, ``verdicts`` the verdicts of the judges (only ``false_claim`` is read) and
    ``budget_span`` the width of the public budget range.
    """
    ordered = sorted(lots, key=lambda entry: entry.lot)
    metrics = seat_metrics(ordered, budget_span=budget_span)
    players = frozenset(seat for seat, m in metrics.items() if m.role == "player")
    bots = frozenset(metrics) - players

    def table(group: frozenset[int], keys: Sequence[AwardKey]) -> tuple[AwardData, ...]:
        made: dict[AwardKey, AwardData] = {
            "most_textbook": _top("most_textbook", _textbook(metrics, group), lowest=True),
            "winners_curse": _top("winners_curse", _curse(ordered, group)),
            "tilt": _top("tilt", _tilt(ordered, group)),
            "bargain": _top("bargain", _bargain(ordered, group)),
            "backstabber": _top("backstabber", _broken(pledges, group)),
            "best_actor": _top("best_actor", _false_claims(verdicts, group)),
            "keeper": _top("keeper", _kept(pledges, group)),
            "mission_master": _first_to_finish(missions, group),
            "mind_reader": _top("mind_reader", _mind(metrics, group), lowest=True),
            "consistent": _top("consistent", _consistency(metrics, group)),
        }
        return tuple(made[key] for key in keys)

    return Awards(
        models=table(players, AWARD_KEYS),
        bots=table(bots, BOT_AWARDS) if bots else (),
        metrics=metrics,
    )


def compute_awards(cards: FactCards, verdicts: Sequence[JudgeVerdictData] = ()) -> Awards:
    """The awards of a finished night, from its fact cards and the verdicts of the judges.

    Without verdicts nobody wins ``best_actor``. The pledges, the missions and the budget range
    come from the night card.
    """
    night = cards.night.data
    if not isinstance(night, NightData):  # pragma: no cover - the night card is always NightData
        raise TypeError("the night card holds the data of the night")
    return build_awards(
        cards.lot_data,
        pledges=night.pledges,
        missions=night.missions,
        verdicts=verdicts,
        budget_span=night.budget_hi - night.budget_lo,
    )
