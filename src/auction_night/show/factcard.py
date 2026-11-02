"""Fact cards: everything that happened, computed by the program and read by the host.

After a night there is one fact card for every lot and one for the night. A card has three faces:

* ``data``, the structured facts (:class:`~auction_night.show.cards.LotData` or
  :class:`NightData`), from which prediction, lesson and highlight candidates are computed;
* ``text``, the Chinese rendering that the host reads (god's view: budgets, values, appraisals,
  true values, plans and mind-reading guesses are all on it);
* ``numbers``, the canonical keys of every number in ``text``. The host may only use these.

A card is a pure function of the finished :class:`~auction_night.engine.night.NightState` and of
the reference numbers of :func:`~auction_night.theory.analyse_night`. The same night always gives
byte-identical cards: nothing reads a clock, an environment or a random generator, and every
collection is walked in lot and seat order.

Where the facts come from. What seats did (talk, pledges, bids, drop-outs, the hammer, missions
and scores) is read from the event log. Private cards (budgets, values, appraisals) come from the
deal, plans from the ``plan`` events, mind-reading guesses with their truth from the state, and
every reference number from the theory module. Pledges are judged by
:func:`~auction_night.show.pledges.judge_pledges`. The night card adds the lesson of the night
(:mod:`~auction_night.show.lesson`) and the settled predictions
(:mod:`~auction_night.show.predict`), so the host may use their numbers.

A night that reached its cost cap has lots in which the cost gate skipped some seats
(:mod:`~auction_night.engine.muted`). The card of such a lot carries one line that says so, with the
first lot from which it happened, and the seats it skipped are shown as not having acted. The host
reads that line, so a silenced table is not told as a table of abstentions.

Not on a card: the inner monologues (the host would be tempted to speak for the seat), the model
ids of the seats, and anything that adds the per-lot reference numbers across lots.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from auction_night.contracts.config import NightSettings
from auction_night.contracts.events import (
    ActionData,
    ActionEvent,
    CollectorMission,
    HammerData,
    LotOpenData,
    LotOpenEvent,
    MissionResultData,
    NightEndData,
    NightEndEvent,
    NightStartData,
    NightStartEvent,
    PlanData,
    PlanEvent,
    SeatPublic,
    TalkData,
    TalkEvent,
    canonical_json,
    sha256_digest,
)
from auction_night.contracts.mechanism import BID_PHASES, FormatKey
from auction_night.contracts.replay import PledgeRecord
from auction_night.engine.deal import Deal, blind_bounds
from auction_night.engine.muted import Muted, muted_actions
from auction_night.engine.night import GuessTruth, NightState
from auction_night.engine.rules_text import rule_text
from auction_night.show.candidates import find_candidates
from auction_night.show.cards import (
    ActionLine,
    Candidate,
    CardModel,
    GuessLine,
    LotData,
    MissionLine,
    PriorLine,
    SeatLine,
)
from auction_night.show.lesson import EquivalenceEvidence, Lesson, select_lesson
from auction_night.show.numbers_check import number_keys
from auction_night.show.pledges import judge_pledges, pledge_text
from auction_night.show.predict import (
    PredictionReport,
    prediction_text,
    reconcile,
    result_text,
)
from auction_night.show.render import (
    CATEGORY_ZH,
    OUTCOME_ZH,
    lot_headline,
    render_lot,
)
from auction_night.theory import LotTheory, analyse_night
from auction_night.theory.counterfactual import effective_bids

__all__ = [
    "FactCard",
    "FactCards",
    "LotRow",
    "NightData",
    "SeatRow",
    "WorstGuess",
    "build_fact_cards",
]

_MODE_ZH: Final = {"plain": "原味模式", "variety": "综艺模式"}


# ------------------------------------------------------------------------ night-level data


class WorstGuess(CardModel):
    """The mind-reading guess of a seat that was furthest from the truth."""

    lot: int
    target: int
    guess: int
    truth: int
    abs_error: int


class SeatRow(CardModel):
    """A seat on the night card: who it is, how the night ended for it, how it read minds."""

    seat: int
    codename: str
    role: Literal["player", "bot"]
    budget: int
    persona: str = ""
    surplus: int
    mission_bonus: int
    total: int
    rank_tonight: int | None = None
    spent: int
    lots_won: tuple[int, ...] = ()
    guesses: int = 0
    worst_guess: WorstGuess | None = None
    pledges_kept: int = 0
    pledges_violated: int = 0
    pledges_void: int = 0


class LotRow(CardModel):
    """One line of the night's ledger of lots."""

    lot: int
    name: str
    emoji: str
    tag: str
    tier: int
    format: FormatKey
    title: str
    winner: int | None = None
    price: int = 0
    unsold: bool = False
    true_value: int | None = None


class NightData(CardModel):
    """The facts of the whole night, and the lesson and the predictions built from the lots.

    ``muted_from`` is the first lot in which the cost gate skipped a seat, None for a night it
    never cut.
    """

    night_id: str
    mode: Literal["plain", "variety"]
    status: Literal["completed", "budget_aborted"]
    n_lots: int
    n_seats: int
    budget_lo: int
    budget_hi: int
    mission_bonus: int
    seats: tuple[SeatRow, ...]
    lots: tuple[LotRow, ...]
    missions: tuple[MissionResultData, ...]
    pledges: tuple[PledgeRecord, ...]
    candidates: tuple[Candidate, ...]
    lesson: Lesson
    predictions: PredictionReport
    muted_from: int | None = None


# ------------------------------------------------------------------------------- the cards


@dataclass(frozen=True)
class FactCard:
    """One fact card: data, the text the host reads, the numbers on it and a headline.

    ``headline`` is the short plain account that is played instead of the host when the host's
    text cannot be trusted. Its numbers are all among ``numbers``.
    """

    kind: Literal["lot", "night"]
    lot: int | None
    data: LotData | NightData
    text: str
    headline: str
    numbers: frozenset[str]

    def data_json(self) -> str:
        """The structured data as canonical JSON."""
        return canonical_json(self.data)

    @property
    def digest(self) -> str:
        """``sha256:<hex>`` over the canonical data and the text."""
        return sha256_digest(canonical_json({"data": self.data, "text": self.text}))


@dataclass(frozen=True)
class FactCards:
    """The cards of a night: one per lot, in lot order, and the night card."""

    lots: tuple[FactCard, ...]
    night: FactCard

    def lot(self, number: int) -> FactCard:
        """The card of one lot."""
        for card in self.lots:
            if card.lot == number:
                return card
        raise KeyError(number)

    @property
    def lot_data(self) -> tuple[LotData, ...]:
        """The structured data of every lot, in lot order."""
        return tuple(card.data for card in self.lots if isinstance(card.data, LotData))

    @property
    def digest(self) -> str:
        """``sha256:<hex>`` over every card of the night."""
        return sha256_digest("".join(card.digest for card in (*self.lots, self.night)))


# ------------------------------------------------------------------------------ the context


@dataclass(frozen=True)
class _Context:
    """The parts of the log that the builders look up often."""

    state: NightState
    start: NightStartData
    end: NightEndData
    roles: dict[int, SeatPublic]
    plans: dict[int, PlanData]
    talks: dict[tuple[int, int], TalkData]
    acts: dict[tuple[int, int], list[ActionData]]
    opens: dict[int, LotOpenData]
    guesses: dict[tuple[int, int], GuessTruth]
    hammers: list[HammerData]
    muted: Muted

    @classmethod
    def of(cls, state: NightState) -> "_Context":
        start: NightStartData | None = None
        end: NightEndData | None = None
        plans: dict[int, PlanData] = {}
        talks: dict[tuple[int, int], TalkData] = {}
        acts: dict[tuple[int, int], list[ActionData]] = {}
        opens: dict[int, LotOpenData] = {}
        for event in state.events:
            if isinstance(event, NightStartEvent):
                start = event.data
            elif isinstance(event, NightEndEvent):
                end = event.data
            elif isinstance(event, PlanEvent):
                plans[event.data.seat] = event.data
            elif isinstance(event, LotOpenEvent) and event.lot is not None:
                opens[event.lot] = event.data
            elif isinstance(event, TalkEvent) and event.lot is not None:
                talks[(event.lot, event.data.seat)] = event.data
            elif (
                isinstance(event, ActionEvent)
                and event.lot is not None
                and event.data.phase in BID_PHASES
            ):
                acts.setdefault((event.lot, event.data.seat), []).append(event.data)
        if start is None or end is None:
            raise ValueError("the log has no night_start or night_end event")
        return cls(
            state=state,
            start=start,
            end=end,
            roles={seat.seat: seat for seat in start.seats},
            plans=plans,
            talks=talks,
            acts=acts,
            opens=opens,
            guesses={(g.lot, g.seat): g for g in state.guesses},
            hammers=[record.outcome.hammer for record in state.records],
            muted=muted_actions(state.events),
        )


# --------------------------------------------------------------------------------- lots


def _missions(ctx: _Context, lot: int) -> tuple[MissionLine, ...]:
    deal: Deal = ctx.state.deal
    tag_of = {entry.lot: entry.tag for entry in deal.lots}
    n_lots = len(deal.lots)
    hammer = ctx.hammers[lot - 1]
    lines = []
    for seat in sorted(deal.missions):
        mission = deal.missions[seat]
        if isinstance(mission, CollectorMission):
            if tag_of[lot] != mission.tag:
                continue
            before = sum(
                1
                for k in range(1, lot)
                if tag_of[k] == mission.tag and ctx.hammers[k - 1].winner == seat
            )
            if before >= mission.need:
                continue
            won_now = hammer.winner == seat
            so_far = before + (1 if won_now else 0)
            outcome = "missed"
            if won_now:
                outcome = "done" if so_far >= mission.need else "advanced"
            lines.append(
                MissionLine(
                    seat=seat,
                    kind="collector",
                    outcome=outcome,  # type: ignore[arg-type]
                    tag=mission.tag,
                    need=mission.need,
                    won_so_far=so_far,
                    tag_lots_left=sum(
                        1 for k in range(lot + 1, n_lots + 1) if tag_of[k] == mission.tag
                    ),
                )
            )
        elif mission.lot == lot:
            if hammer.winner is None:
                outcome = "unsold"
            elif hammer.winner == seat:
                outcome = "stuck"
            elif hammer.price >= mission.threshold_price:
                outcome = "reached"
            else:
                outcome = "short"
            lines.append(
                MissionLine(
                    seat=seat,
                    kind="shill",
                    outcome=outcome,
                    target_lot=lot,
                    threshold=mission.threshold_price,
                    price=None if hammer.winner is None else hammer.price,
                    buyer=hammer.winner,
                )
            )
    return tuple(lines)


def _seat_lines(
    ctx: _Context,
    lot: int,
    bidders: tuple[int, ...],
    numbers: dict[int, int],
    theory: LotTheory,
) -> tuple[SeatLine, ...]:
    state = ctx.state
    record = state.records[lot - 1]
    deal_lot = state.deal.lot(lot)
    hammer = record.outcome.hammer
    lines = []
    for seat in sorted(ctx.roles):
        public = ctx.roles[seat]
        asked = seat in bidders
        actions = tuple(
            ActionLine(
                phase=a.phase,
                status=a.status,
                reason=a.reason,
                value=a.value,
                raw_value=a.raw_value,
            )
            for a in ctx.acts.get((lot, seat), ())
        )
        abstained = asked and all(a.status == "abstain" for a in actions)
        muted = asked and ctx.muted.bid_muted(lot, seat)
        talk = ctx.talks.get((lot, seat))
        plan = ctx.plans.get(seat)
        truth = ctx.guesses.get((lot, seat))
        paid = hammer.payments.get(seat, 0)
        lines.append(
            SeatLine(
                seat=seat,
                codename=public.codename,
                role=public.role,
                asked=asked,
                budget_before=record.budgets_before[seat],
                budget_after=record.budgets_before[seat] - paid,
                value=deal_lot.values[seat] if asked and not deal_lot.is_blind else None,
                signal=deal_lot.signals[seat] if asked and deal_lot.is_blind else None,
                plan_stars=plan.priorities[lot - 1] if plan is not None else None,
                plan_budget=plan.budget_plan[lot - 1] if plan is not None else None,
                said=talk.text if talk is not None else "",
                pledge=talk.pledge if talk is not None else None,
                actions=actions,
                abstained=abstained,
                muted=muted,
                played=numbers.get(seat) if asked and not abstained else None,
                paid=paid,
                won=hammer.winner == seat,
                ref_bid=theory.ref_bids.get(seat),
                excess=theory.excess.get(seat),
                regret=theory.regret.get(seat),
                alpha_milli=theory.alpha_milli.get(seat),
                guess=None
                if truth is None
                else GuessLine(
                    target=truth.target,
                    guess=truth.guess,
                    truth=truth.truth,
                    abs_error=truth.abs_error,
                ),
            )
        )
    return tuple(lines)


def _lot_data(
    ctx: _Context, record_index: int, theory: LotTheory, pledges: Sequence[PledgeRecord]
) -> LotData:
    state = ctx.state
    record = state.records[record_index]
    lot = record.lot
    deal_lot = state.deal.lot(lot)
    entry = ctx.start.lots[lot - 1]
    night = state.config.night
    mech = night.mechanism_for(deal_lot.format)
    outcome = record.outcome
    hammer = outcome.hammer
    bidders = tuple(sorted(ctx.opens[lot].active))
    muted = ctx.muted.seats_in(lot)
    top_value, top_seats = None, ()
    if theory.to_highest_value is not None:
        values = {seat: deal_lot.values[seat] for seat in bidders}
        top_value = max(values.values())
        top_seats = tuple(sorted(seat for seat, value in values.items() if value == top_value))
    data = LotData(
        lot=lot,
        item=entry.item,
        name=entry.name,
        emoji=entry.emoji,
        tag=entry.tag,
        tier=entry.tier,
        format=deal_lot.format,
        title=mech.title_zh,
        mechanism=deal_lot.mechanism,
        reserve=deal_lot.reserve,
        rule=rule_text(mech),
        bidders=bidders,
        sitting_out=tuple(sorted(ctx.opens[lot].sitting_out)),
        seats=_seat_lines(ctx, lot, bidders, effective_bids(mech, outcome), theory),
        muted=muted,
        muted_from=ctx.muted.first_lot if muted else None,
        winner=hammer.winner,
        price=hammer.price,
        unsold=hammer.unsold,
        payments=dict(sorted(hammer.payments.items())),
        tied=tuple(hammer.tied),
        newly_out=tuple(hammer.newly_out),
        true_value=hammer.true_value,
        prior=_prior(night, deal_lot.tier) if deal_lot.is_blind else None,
        curse_gap=theory.curse_gap,
        top_signal_error=theory.top_signal_error,
        to_highest_value=theory.to_highest_value,
        top_value=top_value,
        top_value_seats=top_seats,
        winner_exit=outcome.winner_exit,
        dropouts=tuple(outcome.dropouts),
        dutch=outcome.dutch,
        expected_revenue_milli=theory.expected_revenue_milli,
        pledges_made=tuple(p for p in pledges if p.lot == lot),
        pledges_due=tuple(p for p in pledges if p.pledge.lot == lot),
        missions=_missions(ctx, lot),
    )
    miss_error = max(1, night.budget.c // 2)
    return data.model_copy(update={"candidates": find_candidates(data, miss_error=miss_error)})


def _prior(night: NightSettings, tier: int) -> PriorLine:
    bounds = blind_bounds(tier, night.blind)
    return PriorLine(low=bounds.low, high=bounds.high, noise=bounds.noise)


# ---------------------------------------------------------------------------------- night


def _worst_guess(guesses: Sequence[GuessTruth]) -> WorstGuess | None:
    if not guesses:
        return None
    worst = max(guesses, key=lambda g: (g.abs_error, -g.lot))
    return WorstGuess(
        lot=worst.lot,
        target=worst.target,
        guess=worst.guess,
        truth=worst.truth,
        abs_error=worst.abs_error,
    )


def _night_data(
    ctx: _Context,
    lots: Sequence[LotData],
    pledges: Sequence[PledgeRecord],
    lesson: Lesson,
    report: PredictionReport,
) -> NightData:
    state = ctx.state
    start = ctx.start
    scores = {score.seat: score for score in ctx.end.scores}
    rows = []
    for seat in sorted(ctx.roles):
        public = ctx.roles[seat]
        score = scores[seat]
        plan = ctx.plans.get(seat)
        own = [g for g in state.guesses if g.seat == seat]
        mine = [p for p in pledges if p.seat == seat]
        rows.append(
            SeatRow(
                seat=seat,
                codename=public.codename,
                role=public.role,
                budget=state.deal.budgets[seat],
                persona=plan.persona if plan is not None else "",
                surplus=score.surplus,
                mission_bonus=score.mission_bonus,
                total=score.total,
                rank_tonight=score.rank_tonight,
                spent=score.spent,
                lots_won=tuple(score.lots_won),
                guesses=len(own),
                worst_guess=_worst_guess(own),
                pledges_kept=sum(1 for p in mine if p.outcome == "kept"),
                pledges_violated=sum(1 for p in mine if p.outcome == "violated"),
                pledges_void=sum(1 for p in mine if p.outcome == "void"),
            )
        )
    return NightData(
        night_id=start.night_id,
        mode=start.mode,
        status=ctx.end.status,
        n_lots=start.n_lots,
        n_seats=len(start.seats),
        budget_lo=start.budget_lo,
        budget_hi=start.budget_hi,
        mission_bonus=start.mission_bonus,
        seats=tuple(rows),
        lots=tuple(
            LotRow(
                lot=d.lot,
                name=d.name,
                emoji=d.emoji,
                tag=d.tag,
                tier=d.tier,
                format=d.format,
                title=d.title,
                winner=d.winner,
                price=d.price,
                unsold=d.unsold,
                true_value=d.true_value,
            )
            for d in lots
        ),
        missions=tuple(state.missions),
        pledges=tuple(pledges),
        candidates=tuple(c for d in lots for c in d.candidates),
        lesson=lesson,
        predictions=report,
        muted_from=ctx.muted.first_lot,
    )


def _mission_result_line(night: NightData, result: MissionResultData) -> str:
    names = {row.seat: row.codename for row in night.seats}
    who = f"{result.seat} 号「{names[result.seat]}」"
    verdict = f"完成，奖金 {result.bonus}" if result.completed else "没完成"
    if result.kind == "collector":
        if result.won_lots:
            won = "、".join(str(lot) for lot in result.won_lots)
            mine = f"拍到的该标签拍品是第 {won} 件"
        else:
            mine = "一件该标签的拍品也没拍到"
        return f"- 收藏家 {who}：要集齐「{result.tag}」{result.need} 件，{mine}；{verdict}。"
    if result.final_price is None:
        outcome = "这件流拍"
    else:
        outcome = f"成交价 {result.final_price}，买家 {result.buyer} 号"
    return (
        f"- 托儿 {who}：目标第 {result.target_lot} 件，门槛 {result.threshold_price}，{outcome}；"
        f"{verdict}。"
    )


def _seat_row_lines(row: SeatRow) -> list[str]:
    role = "机器人" if row.role == "bot" else "选手"
    rank = "不参与排名" if row.rank_tonight is None else f"今晚名次 {row.rank_tonight}"
    won = "、".join(str(lot) for lot in row.lots_won)
    parts = [
        f"- {row.seat} 号「{row.codename}」（{role}）",
        f"开场预算 {row.budget}",
        f"盈余 {row.surplus}",
        f"任务奖金 {row.mission_bonus}",
        f"总分 {row.total}",
        rank,
        f"花了 {row.spent}",
        f"拍得第 {won} 件" if row.lots_won else "一件也没拍到",
    ]
    lines = ["｜".join(parts)]
    if row.persona:
        lines.append(f"  自述「{row.persona}」")
    if row.guesses:
        worst = row.worst_guess
        assert worst is not None
        lines.append(
            f"  读心 {row.guesses} 次，最远的一次在第 {worst.lot} 件：猜 {worst.target} 号预算余 "
            f"{worst.guess}，实际 {worst.truth}，差 {worst.abs_error}"
        )
    return lines


def _render_night(night: NightData) -> str:
    lines = [
        f"【整夜】{night.night_id}｜{_MODE_ZH[night.mode]}｜{night.n_lots} 件｜{night.n_seats} 座"
        f"｜预算范围 {night.budget_lo} 到 {night.budget_hi}｜任务奖金 {night.mission_bonus}"
    ]
    if night.status != "completed":
        status = "状态：因费用上限中止，这一夜不进积分榜。"
        if night.muted_from is not None:
            status += f"从第 {night.muted_from} 件起，模型座位因为费用上限不再行动。"
        lines.append(status)
    lines.append("座位与终场成绩：")
    for row in night.seats:
        lines += _seat_row_lines(row)
    if night.missions:
        lines.append("任务结果：")
        lines += [_mission_result_line(night, result) for result in night.missions]
    names = {row.seat: row.codename for row in night.seats}
    if night.pledges:
        kept = sum(1 for p in night.pledges if p.outcome == "kept")
        broken = [p for p in night.pledges if p.outcome == "violated"]
        void = sum(1 for p in night.pledges if p.outcome == "void")
        lines.append(
            f"誓约共 {len(night.pledges)} 条：遵守 {kept} 条，违约 {len(broken)} 条，"
            f"无法检验 {void} 条。"
        )
        for p in broken:
            lines.append(
                f"- {p.seat} 号「{names[p.seat]}」在第 {p.lot} 件承诺{pledge_text(p.pledge)}："
                f"{OUTCOME_ZH[p.outcome]}（{p.detail}）"
            )
    lines.append("各件一览：")
    for row in night.lots:
        if row.unsold or row.winner is None:
            result = "流拍"
        else:
            result = f"{row.winner} 号「{names[row.winner]}」拍得，成交价 {row.price}"
            if row.true_value is not None:
                result += f"，真值 {row.true_value}"
        lines.append(
            f"- 第 {row.lot} 件 {row.emoji}{row.name}｜{row.title}｜标签 {row.tag}"
            f"｜档位 {row.tier}｜{result}"
        )
    if night.candidates:
        lines.append("高光候选：")
        lines += [
            f"- 第 {c.lot} 件（{CATEGORY_ZH[c.category]}）：{c.reason}" for c in night.candidates
        ]
    lines += night.lesson.lines()
    lines.append(prediction_text())
    lines.append(result_text(night.predictions))
    return "\n".join(lines)


def _night_headline(night: NightData) -> str:
    """The plain account of the night that is played if the host's recap cannot be used."""
    lines = [f"整夜回顾（只播事实）：{night.n_lots} 件拍品，{night.n_seats} 座。"]
    ranked = sorted(
        (row for row in night.seats if row.rank_tonight is not None),
        key=lambda row: (row.rank_tonight or 0, row.seat),
    )
    if ranked:
        lines.append(
            "今晚名次："
            + "；".join(
                f"第 {row.rank_tonight} 名 {row.seat} 号{row.codename}（总分 {row.total}）"
                for row in ranked
            )
            + "。"
        )
    done = [r for r in night.missions if r.completed]
    if done:
        names = {row.seat: row.codename for row in night.seats}
        lines.append(
            "完成任务的有：" + "、".join(f"{r.seat} 号{names[r.seat]}" for r in done) + "。"
        )
    lines.append(f"课代表题目：{night.lesson.title}。")
    if night.predictions.hits is not None:
        lines.append(
            f"赛前预测可对账 {night.predictions.judged} 条，命中 {night.predictions.hits} 条。"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- the interface


def build_fact_cards(
    state: NightState,
    *,
    theory: Sequence[LotTheory] | None = None,
    equivalence: EquivalenceEvidence | None = None,
) -> FactCards:
    """The fact cards of a finished night.

    ``theory`` is the reference numbers of every lot, as returned by
    :func:`~auction_night.theory.analyse_night`; it is computed when not given.
    ``equivalence`` is season evidence for the revenue-equivalence topic of the lesson (see
    :mod:`~auction_night.show.lesson`).
    """
    if not state.done:
        raise ValueError("fact cards are built after the night is over")
    numbers = list(analyse_night(state) if theory is None else theory)
    if len(numbers) != len(state.records):
        raise ValueError("there must be one set of reference numbers for every decided lot")
    ctx = _Context.of(state)
    pledges = judge_pledges(state.events)
    lots = [_lot_data(ctx, index, numbers[index], pledges) for index in range(len(state.records))]
    report = reconcile(lots)
    lesson = select_lesson(lots, equivalence=equivalence)
    night = _night_data(ctx, lots, pledges, lesson, report)

    def lot_card(data: LotData) -> FactCard:
        text = render_lot(data)
        return FactCard("lot", data.lot, data, text, lot_headline(data), number_keys(text))

    text = _render_night(night)
    return FactCards(
        lots=tuple(lot_card(data) for data in lots),
        night=FactCard("night", None, night, text, _night_headline(night), number_keys(text)),
    )
