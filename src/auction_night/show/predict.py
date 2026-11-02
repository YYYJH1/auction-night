"""The pre-show prediction card, and its settlement after the night.

Before the show the host makes one prediction per kind of rule, based on published measurements
of how language models bid. After the night the program settles each one from the fact cards of
the lots. The predictions are fixed sentences and the settlement rules are fixed too, so the
card says nothing that the night could bend after the fact.

Only the *model seats* are measured (seats that are not rule bots), and only bids that were
really made (a seat that was asked and did not abstain). The yardstick is the single-lot textbook
reference bid on the card (``ref_bid``), which already accounts for the remaining budget.

The rules, in the order of the card:

1. **First-price sealed bid, "models give too much".** Hit when, over the model seats' bids in
   first-price sealed lots, more bids lie above the textbook reference than below it. Bids equal
   to the reference count for neither side.
2. **Second-price sealed bid, "models do not dare tell the truth".** Hit when fewer than half of
   the model seats' bids in second-price sealed lots are truthful. A truthful bid equals the
   seat's value, or its remaining budget if that is lower (which is the reference bid).
3. **Blind box, "there is always a winner's curse".** Hit when at least one blind-box lot won by
   a model seat was sold for more than its true value.
4. **The English button, "the most rule-abiding".** The deviation of a bid is
   ``|bid - reference|`` as thousandths of the lot's tier, rounded down. Hit when the *median*
   deviation over the model seats' exit prices in English lots, the only running clock of the
   game, is smaller than the median over their bids in all other lots. The Dutch lots count
   with the others: a seat names its grab price once, which makes a Dutch lot a first-price
   auction with ten prices and not a clock. The study behind the card found smaller deviations
   under ascending clocks than under sealed bids.

A rule that has nothing to compare (the night had no such lot, or no model seat bid in it) leaves
``hit`` empty. The rules use counts and medians only.

Where the predictions come from: the host cites arXiv 2507.09083, which measured how models of
2024 and 2025 bid in several auction formats. The sentences are the show's bets in the spirit of
that study.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Final, NamedTuple

from auction_night.contracts.mechanism import FormatKey
from auction_night.contracts.replay import PredictionCard, PredictionItem
from auction_night.show.cards import CardModel, LotData
from auction_night.show.numbers_check import canonical

__all__ = [
    "PREDICTIONS",
    "SOURCE_NOTE",
    "PredictionOutcome",
    "PredictionReport",
    "PredictionSpec",
    "prediction_text",
    "reconcile",
    "result_text",
]

SOURCE_NOTE: Final = "依据 arXiv 2507.09083 对 2024 到 2025 年模型的测量"


@dataclass(frozen=True, slots=True)
class PredictionSpec:
    """One fixed prediction: the claim key, the format it is attached to and its sentence."""

    claim: str
    format: FormatKey
    title: str
    text: str


PREDICTIONS: Final[tuple[PredictionSpec, ...]] = (
    PredictionSpec(
        claim="first_price_overbid",
        format="sealed_first",
        title="一价密封",
        text="模型多半报得比教科书更高，也就是一价给多了。",
    ),
    PredictionSpec(
        claim="second_price_not_truthful",
        format="sealed_second",
        title="二价密封",
        text="说真话本是最优，模型却不敢报自己的真价。",
    ),
    PredictionSpec(
        claim="blind_box_curse",
        format="blind_box",
        title="盲盒",
        text="盲盒必有赢家诅咒，总有赢家付得比真值还多。",
    ),
    PredictionSpec(
        claim="clock_closest_to_textbook",
        format="english",
        title="英式按钮",
        text="英式按钮最守规矩，报价最贴近教科书。",
    ),
)


class PredictionOutcome(CardModel):
    """One prediction settled: ``hit`` is empty when the night gave nothing to compare.

    ``basis`` is the settlement in words, with the counts it rests on.
    """

    claim: str
    format: FormatKey
    title: str
    text: str
    hit: bool | None
    basis: str
    counts: dict[str, int]


class PredictionReport(CardModel):
    """All predictions of the night, settled. ``hits`` is empty when none could be settled."""

    outcomes: tuple[PredictionOutcome, ...]
    judged: int
    hits: int | None

    def card(self) -> PredictionCard:
        """The contract form, for the replay."""
        return PredictionCard(
            items=[
                PredictionItem(format=o.format, claim=o.claim, text=o.text, hit=o.hit)
                for o in self.outcomes
            ],
            hits=self.hits,
        )


# ------------------------------------------------------------------------------ measuring


class _Bid(NamedTuple):
    """A bid that was really made, with the reference it is compared to."""

    lot: LotData
    played: int
    ref: int

    @property
    def deviation(self) -> int:
        """``|bid - reference|`` in thousandths of the tier, rounded down."""
        return abs(self.played - self.ref) * 1000 // self.lot.tier


def _bids(lots: Sequence[LotData], formats: frozenset[FormatKey]) -> list[_Bid]:
    """The model seats' real bids in lots of the given formats, with a reference to compare."""
    return [
        _Bid(data, line.played, line.ref_bid)
        for data in lots
        if data.format in formats
        for line in data.seats
        if line.role == "player"
        and line.asked
        and not line.abstained
        and line.played is not None
        and line.ref_bid is not None
    ]


def _sides(bids: Sequence[_Bid]) -> dict[str, int]:
    above = sum(1 for bid in bids if bid.played > bid.ref)
    below = sum(1 for bid in bids if bid.played < bid.ref)
    return {"above": above, "below": below, "equal": len(bids) - above - below}


def _median(values: Sequence[int]) -> Fraction:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return Fraction(ordered[middle])
    return Fraction(ordered[middle - 1] + ordered[middle], 2)


def _first_price(lots: Sequence[LotData]) -> tuple[bool | None, str, dict[str, int]]:
    counts = _sides(_bids(lots, frozenset({"sealed_first"})))
    if counts["above"] + counts["below"] == 0:
        return None, "今晚没有可以和教科书比较的一价密封出价。", counts
    basis = (
        f"模型座位在一价密封拍品上，出价高于教科书参考 {counts['above']} 次，"
        f"低于 {counts['below']} 次，相同 {counts['equal']} 次。"
    )
    return counts["above"] > counts["below"], basis, counts


def _second_price(lots: Sequence[LotData]) -> tuple[bool | None, str, dict[str, int]]:
    bids = _bids(lots, frozenset({"sealed_second"}))
    counts = _sides(bids)
    if not bids:
        return None, "今晚没有可以比较的二价密封出价。", counts
    basis = (
        f"模型座位在二价密封拍品上，报出真价的有 {counts['equal']} 次，"
        f"报低了 {counts['below']} 次，报高了 {counts['above']} 次。"
    )
    return counts["above"] + counts["below"] > counts["equal"], basis, counts


def _blind_box(lots: Sequence[LotData]) -> tuple[bool | None, str, dict[str, int]]:
    boxes = [
        data
        for data in lots
        if data.format == "blind_box"
        and data.winner is not None
        and data.true_value is not None
        and data.seat_line(data.winner).role == "player"
    ]
    cursed = sum(1 for data in boxes if data.price > (data.true_value or 0))
    counts = {"sold_to_models": len(boxes), "paid_over_true_value": cursed}
    if not boxes:
        return None, "今晚没有被模型座位拍下的盲盒。", counts
    basis = f"模型座位拍下的盲盒有 {len(boxes)} 件，其中付得比真值多的有 {cursed} 件。"
    return cursed > 0, basis, counts


_CLOCK: Final = frozenset({"english"})
_OTHERS: Final = frozenset({"sealed_first", "sealed_second", "all_pay", "blind_box", "dutch"})


def _clock(lots: Sequence[LotData]) -> tuple[bool | None, str, dict[str, int]]:
    clock = [bid.deviation for bid in _bids(lots, _CLOCK)]
    rest = [bid.deviation for bid in _bids(lots, _OTHERS)]
    counts = {"clock_bids": len(clock), "other_bids": len(rest)}
    if not clock or not rest:
        return None, "今晚英式拍品和其他规则没有同时出现可以比较的出价。", counts
    near, far = _median(clock), _median(rest)
    basis = (
        "模型座位的出价与教科书参考的偏差（占档位的千分之几）中，"
        f"英式拍品的中位数是 {canonical(near)}，其他规则的中位数是 {canonical(far)}。"
    )
    return near < far, basis, counts


_RULES: Final = {
    "first_price_overbid": _first_price,
    "second_price_not_truthful": _second_price,
    "blind_box_curse": _blind_box,
    "clock_closest_to_textbook": _clock,
}


def reconcile(lots: Sequence[LotData]) -> PredictionReport:
    """Settle the four predictions from the fact cards of the night's lots."""
    outcomes = []
    for spec in PREDICTIONS:
        hit, basis, counts = _RULES[spec.claim](lots)
        outcomes.append(
            PredictionOutcome(
                claim=spec.claim,
                format=spec.format,
                title=spec.title,
                text=spec.text,
                hit=hit,
                basis=basis,
                counts=counts,
            )
        )
    judged = sum(1 for outcome in outcomes if outcome.hit is not None)
    hits = sum(1 for outcome in outcomes if outcome.hit)
    return PredictionReport(outcomes=tuple(outcomes), judged=judged, hits=hits if judged else None)


# --------------------------------------------------------------------------------- texts


def prediction_text() -> str:
    """The pre-show card as the host announces it. It is the same on every night."""
    lines = [f"赛前预测，{SOURCE_NOTE}。"]
    lines += [f"- {spec.title}：{spec.text}" for spec in PREDICTIONS]
    return "\n".join(lines)


_VERDICT: Final = {True: "命中", False: "没中", None: "无法对账"}


def result_text(report: PredictionReport) -> str:
    """The settlement as the host announces it after the night."""
    if report.hits is None:
        head = "赛后对账：今晚没有一条预测可以对账。"
    else:
        head = f"赛后对账：可对账 {report.judged} 条，命中 {report.hits} 条。"
    lines = [head]
    lines += [f"- {o.title}：{_VERDICT[o.hit]}。{o.basis}" for o in report.outcomes]
    return "\n".join(lines)
