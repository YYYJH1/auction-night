"""The economics lesson of the night: one topic, chosen by a fixed priority.

The "class representative" explains one idea per night, and which idea is not up to the host. The
topics have a fixed priority, and the first one that the night's fact cards support is taught:

1. **The blind-box winner lost money.** Two effects are told apart: the highest appraisal
   overestimates the true value by ``eps (n - 1) / (n + 1)`` on average (selection), and a winner
   that bids its appraisal in a second-price box pays ``eps (n - 3) / (n + 1)`` too much (payment).
2. **A model seat clearly left its value** in a second-price or English lot (by at least a fifth
   of the tier): why telling the truth costs nothing there.
3. **A Dutch lot was played:** it is a first-price auction in which only ten prices can be named,
   and its exact expected revenue is lower than the continuous formula.
4. **An all-pay lot took in more than its tier:** why all-pay auctions make people tilt.
5. **A seat publicly pledged to let another seat win:** Robinson (1985) on which formats keep a
   cartel together.
6. **Revenue equivalence.** A single night says nothing reliable about it, because one night's
   averages over formats differ by a large part of the tier. It is taught only when the caller
   hands in evidence from a whole season, or from a range of simulated rule-bot nights on the
   same deck (:class:`EquivalenceEvidence`). Without it this step never fires.

Reading the order: the steps are tried one after another and the first that applies wins, so
step 3 applies on every night that has a Dutch lot with at least two bidders (all standard nights),
and steps 4 and 5 are reached only on nights where no Dutch lot qualifies. The thresholds (a fifth
of the tier in step 2, ``_NAIVE_MIN_BIDDERS`` in step 1) are constants to retune after the first
nights with real models.

If no topic applies the lesson is ``none``. The reason and every number of a lesson come from the
fact cards of the night (lot, seats, prices, the blind prior) and from the closed formulas of the
theory module for the numbers of that lot.
"""

from collections.abc import Sequence
from fractions import Fraction
from typing import Literal

from auction_night.show.cards import CardModel, LotData, SeatLine
from auction_night.show.numbers_check import canonical
from auction_night.show.pledges import bid_word, pledge_text
from auction_night.theory.common_value import (
    equilibrium_shading,
    naive_winner_overpay,
    top_signal_overestimate,
)

__all__ = ["EquivalenceEvidence", "Lesson", "LessonTopic", "decimal_text", "select_lesson"]

LessonTopic = Literal[
    "winners_curse",
    "truthful_second_price",
    "dutch_discrete",
    "all_pay_tilt",
    "cartel",
    "revenue_equivalence",
    "none",
]
_NAIVE_MIN_BIDDERS = 4  # below this the payment effect is zero or negative on average
_DEVIATION_DIVISOR = 5  # "clearly" is a fifth of the tier


class EquivalenceEvidence(CardModel):
    """Season-level evidence for the revenue-equivalence topic, supplied by the caller.

    ``source`` says where it comes from (``season`` totals or the ``bot_range`` of rule-bot
    nights on the same deck). ``summary`` is the sentence the lesson quotes, with its numbers.
    """

    source: Literal["season", "bot_range"]
    summary: str


class Lesson(CardModel):
    """The chosen topic. ``priority`` is 1 to 6 (empty for ``none``), ``facts`` the numbers
    that the points quote, as canonical strings."""

    topic: LessonTopic
    priority: int | None
    title: str
    reason: str
    points: tuple[str, ...]
    lot: int | None = None
    seats: tuple[int, ...] = ()
    facts: dict[str, str] = {}

    def lines(self) -> list[str]:
        """The lesson as the lines of the night card."""
        if self.topic == "none":
            return [f"课代表：{self.title}"]
        return [
            f"课代表：{self.title}",
            f"- 选题理由：{self.reason}",
            *(f"- {p}" for p in self.points),
        ]


def decimal_text(value: Fraction, places: int = 3) -> str:
    """``value`` rounded half up to ``places`` decimals, without trailing zeros."""
    scale = 10**places
    scaled = value * scale
    rounded = (scaled.numerator * 2 + scaled.denominator) // (scaled.denominator * 2)
    return canonical(Fraction(rounded, scale))


def _who(line: SeatLine) -> str:
    return f"{line.seat} 号{line.codename}"


def _winners_curse(lots: Sequence[LotData]) -> Lesson | None:
    cursed = [
        data
        for data in lots
        if data.prior is not None
        and data.winner is not None
        and data.true_value is not None
        and data.price > data.true_value
    ]
    if not cursed:
        return None
    data = max(cursed, key=lambda d: (d.price - (d.true_value or 0), -d.lot))
    assert data.prior is not None and data.true_value is not None and data.winner is not None
    n, noise = len(data.bidders), data.prior.noise
    gap = data.price - data.true_value
    over = top_signal_overestimate(noise, n)
    shading = equilibrium_shading(noise, n)
    facts = {
        "n": str(n),
        "noise": str(noise),
        "price": str(data.price),
        "true_value": str(data.true_value),
        "gap": str(gap),
        "top_overestimate": decimal_text(over),
        "shading": decimal_text(shading),
    }
    points = [
        f"选择效应：{n} 人里鉴定价最高的那位，平均高估真值 ε(n−1)/(n+1)。"
        f"本件噪声 ε={noise}，平均高估 {decimal_text(over)}；"
        f"本件最高鉴定价比真值高 {data.top_signal_error}。"
    ]
    if n >= _NAIVE_MIN_BIDDERS:
        naive = naive_winner_overpay(noise, n)
        facts["naive_overpay"] = decimal_text(naive)
        facts["naive_share"] = decimal_text(naive / data.tier)
        points.append(
            f"付款效应：二价下照报鉴定价的赢家，平均多付 ε(n−3)/(n+1) = {decimal_text(naive)}，"
            f"约是档位 {data.tier} 的 {decimal_text(naive / data.tier)} 倍。"
        )
    points.append(f"教科书的二价出价是鉴定价减 ε(n−2)/n，本件要减 {decimal_text(shading)}。")
    who = _who(data.seat_line(data.winner))
    return Lesson(
        topic="winners_curse",
        priority=1,
        title="盲盒赢家亏了，选择和付款是两件事",
        reason=(
            f"第 {data.lot} 件盲盒，{who}付 {data.price}，真值 {data.true_value}，多付 {gap}。"
        ),
        points=tuple(points),
        lot=data.lot,
        seats=(data.winner,),
        facts=facts,
    )


def _truthful(lots: Sequence[LotData]) -> Lesson | None:
    found: list[tuple[int, int, int, LotData, SeatLine]] = []
    for data in lots:
        if data.format not in ("sealed_second", "english"):
            continue
        for line in data.seats:
            if (
                line.role != "player"
                or line.abstained
                or line.played is None
                or line.ref_bid is None
                or line.value is None
            ):
                continue
            gap = abs(line.played - line.ref_bid)
            if gap * _DEVIATION_DIVISOR >= data.tier:
                found.append((-(gap * 1000 // data.tier), data.lot, line.seat, data, line))
    if not found:
        return None
    *_, data, line = min(found, key=lambda item: item[:3])
    assert line.value is not None and line.played is not None and line.ref_bid is not None
    word = bid_word(data.format)
    return Lesson(
        topic="truthful_second_price",
        priority=2,
        title="二价和英式里，说真话为什么不吃亏",
        reason=(
            f"第 {data.lot} 件{data.title}，{_who(line)}估值 {line.value}，"
            f"却{word} {line.played}（教科书参考 {line.ref_bid}）。"
        ),
        points=(
            "二价和英式拍卖里如实报价是占优策略：赢家付的是别人的价，自己报多少只决定赢不赢，"
            "不决定付多少。",
            "报高了，可能赢下价格高于估值的亏本货；报低了，会输掉本来有赚头的货。",
        ),
        lot=data.lot,
        seats=(line.seat,),
        facts={
            "value": str(line.value),
            "played": str(line.played),
            "reference": str(line.ref_bid),
        },
    )


def _dutch(lots: Sequence[LotData]) -> Lesson | None:
    playable = [
        data
        for data in lots
        if data.format == "dutch" and data.expected_revenue_milli is not None and data.dutch
    ]
    if not playable:
        return None
    data = min(playable, key=lambda d: (d.unsold, d.lot))
    assert data.expected_revenue_milli is not None and data.dutch is not None
    n, levels = len(data.bidders), len(data.dutch.ladder)
    exact = Fraction(data.expected_revenue_milli, 1000) / data.tier
    continuous = Fraction(n - 1, n + 1)
    return Lesson(
        topic="dutch_discrete",
        priority=3,
        title="荷式其实是只能报几个价的一价拍卖",
        reason=f"第 {data.lot} 件是荷式，在场 {n} 人，档位 {data.tier}，共 {levels} 档。",
        points=(
            f"价格每次降一档，出手价被向下取到最近的一档，所以荷式是只能报 {levels} 个价的"
            "一价拍卖。",
            f"本件的精确期望成交额是档位的 {decimal_text(exact, 4)}，"
            f"连续情形的 (n−1)/(n+1) 是 {decimal_text(continuous)}。",
        ),
        lot=data.lot,
        facts={
            "n": str(n),
            "levels": str(levels),
            "exact_share": decimal_text(exact, 4),
            "continuous_share": decimal_text(continuous),
        },
    )


def _all_pay(lots: Sequence[LotData]) -> Lesson | None:
    over = [
        (sum(data.payments.values()), data)
        for data in lots
        if data.format == "all_pay" and sum(data.payments.values()) > data.tier
    ]
    if not over:
        return None
    total, data = max(over, key=lambda item: (item[0], -item[1].lot))
    return Lesson(
        topic="all_pay_tilt",
        priority=4,
        title="全员付费为什么让人上头",
        reason=f"第 {data.lot} 件全员付费，所有人合计付了 {total}，超过档位 {data.tier}。",
        points=(
            "全员付费里输家也要付自己的出价，每个人都想着再加一点就能赢，钱却是一定要付的。",
            "教科书里它的卖方期望收入和二价一样，合计付得比档位还多，通常说明大家出得比均衡高。",
        ),
        lot=data.lot,
        facts={"total": str(total), "tier": str(data.tier)},
    )


def _cartel(lots: Sequence[LotData]) -> Lesson | None:
    for data in lots:
        for record in data.pledges_made:
            if record.pledge.kind != "let_win":
                continue
            who = _who(data.seat_line(record.seat))
            return Lesson(
                topic="cartel",
                priority=5,
                title="公开结盟能不能维持：Robinson（1985）",
                reason=f"第 {data.lot} 件，{who}公开承诺{pledge_text(record.pledge)}。",
                points=(
                    "Robinson（1985）：卡特尔在二价和英式拍卖里能自我维持，在一价密封里维持不住。",
                    "一价里背叛的人只要比同伴略高一点就能独吞，所以约定守不住；"
                    "二价和英式里背叛占不到便宜。",
                ),
                lot=data.lot,
                seats=(record.seat,),
                facts={"year": "1985"},
            )
    return None


def _equivalence(evidence: EquivalenceEvidence | None) -> Lesson | None:
    if evidence is None:
        return None
    where = "整个赛季" if evidence.source == "season" else "同一副牌上机器人的区间"
    return Lesson(
        topic="revenue_equivalence",
        priority=6,
        title="收益等价：四种规则平均收入一样",
        reason=f"{where}：{evidence.summary}",
        points=(
            "教科书说，独立私有估值下一价、二价、英式和全员付费的卖方期望收入相同。",
            "单夜的平均数波动很大，所以这条只用赛季累计或机器人的区间来讲。",
        ),
        facts={"source": evidence.source},
    )


def select_lesson(
    lots: Sequence[LotData], *, equivalence: EquivalenceEvidence | None = None
) -> Lesson:
    """The lesson of the night: the first topic of the priority list that the cards support."""
    for choose in (_winners_curse, _truthful, _dutch, _all_pay, _cartel):
        lesson = choose(lots)
        if lesson is not None:
            return lesson
    return _equivalence(equivalence) or Lesson(
        topic="none",
        priority=None,
        title="今晚没有可讲的题目",
        reason="前五条优先级今晚都没有出现，收益等价又没有赛季数据可用。",
        points=(),
    )
