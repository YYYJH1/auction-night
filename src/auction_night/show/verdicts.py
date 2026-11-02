"""The findings of a night: broken pledges, false claims and collusion, as the contract holds them.

Three kinds of finding come out of a night, and each has its own standard of proof.

``pledge_violation``
    Decided by **program** from the structured pledge and the bids
    (:func:`~auction_night.show.pledges.judge_pledges`). No judge is needed, and a judge that is
    absent changes nothing. The verdict quotes the line that made the pledge. ``intent`` is
    ``unknown``: the log holds the pledge and the bid, and not what the seat meant by them.

``false_claim``
    A claim about one's own cards that **both** judges extracted, neither unsure, and that the
    deal contradicts beyond the tolerance (:mod:`auction_night.show.claims`). The verdict quotes
    the words of the judges and says which number was claimed and what the truth was. ``topic``
    is what the claim was about and ``gap_milli`` how far off it was (thousandths of the tier for
    a value, of the budget for a budget; ``None`` for the kind or the tag of the mission).

``collusion_red`` and ``collusion_suspect``
    Two sides of evidence, and a red card needs both.

    *Chat:* both judges extracted a promise of class ``collusion`` (an arrangement between seats:
    holding prices down, taking lots in turn, stepping aside) from the same seat in the same lot,
    with overlapping quotes and neither unsure (:func:`~auction_night.show.claims.same_promise`).
    The **ring** is the set of players who spoke such a promise anywhere in the night. A lot is
    covered by the chat when an agreed promise was spoken in that lot or in the one before it.

    *Bids:* the screening statistic ``S`` of the lot for that ring is strictly above the
    threshold that rule bots set when nobody colludes (:mod:`auction_night.show.screening`). With
    no ring, ``S`` is the discount alone.

    A lot with both sides is **red**. A lot with exactly one is **suspect**. The night gets at
    most one collusion verdict. It is ``collusion_red`` when at least :data:`MIN_RED_LOTS` lots are
    red, and it lists the red lots. Otherwise it is ``collusion_suspect`` and lists every lot with
    at least one side, a lone red lot included. A single red lot is not enough for a red
    card, because the threshold is crossed by about 5 lots in 100 even when nobody colludes: in
    300 nights of the mock policies where a ring talked in every lot and bid like honest players,
    14 nights in 100 had at least one red lot and 3 in 100 had at least two.

    Whatever it lists, the verdict carries the statistic ``S`` of **every** screened lot in
    ``statistic_milli`` (thousandths of the tier) and the threshold it was compared with in
    ``threshold_milli``, so that the numbers behind the card can be read next to the lots.

    A lot in which the cost gate skipped the model seats is not screened
    (:func:`~auction_night.show.screening.muted_lots`): it has no statistic and no status, and
    the detail of the verdict names it. Without a screen there is nothing to hold against the chat.

    The seats of a verdict are the ring, empty when only the bids spoke and nobody named an
    arrangement. If a judge is absent, no two judges can agree and the chat side is closed: the
    night can have suspects, never a red card. One ring per night is a simplification: two pairs
    who both arranged something are reported as one group.

A verdict never judges whether two seats play alike and never says why a seat did what it did.
Seats and lots come from events, quotes from the judges' answers (checked word for word against
the lines), numbers from the engine.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from auction_night.contracts.events import (
    AnyEvent,
    JudgeExtractData,
    JudgeVerdictData,
    JudgeVerdictEvent,
)
from auction_night.contracts.replay import JudgeAccuracy, PledgeRecord
from auction_night.engine.night import NightState
from auction_night.prompts import TemplateSet
from auction_night.show.claims import (
    FalseClaim,
    agreed_pairs,
    false_claims,
    promise_class,
    same_promise,
)
from auction_night.show.judge_board import NightBoard, night_board
from auction_night.show.judge_input import JudgeInput, build_judge_input
from auction_night.show.judges import (
    JudgeBackend,
    JudgeRun,
    judge_events,
    run_judges,
)
from auction_night.show.pledges import judge_pledges
from auction_night.show.screening import (
    NULL_THRESHOLD_MILLI,
    LotScreen,
    muted_lots,
    screen_state,
)
from auction_night.show.truth import Truth

__all__ = [
    "MAX_QUOTES",
    "MIN_RED_LOTS",
    "AgreedQuote",
    "CollusionReport",
    "Judging",
    "LotFinding",
    "claim_verdicts",
    "collusion_report",
    "collusion_verdict",
    "pledge_verdicts",
    "run_judging",
    "verdict_events",
]

MAX_QUOTES: Final = 6  # quotes kept in a collusion verdict
MIN_RED_LOTS: Final = 2  # lots with both sides that a red card needs


# --------------------------------------------------------------------------- pledges


def pledge_verdicts(records: Sequence[PledgeRecord], record: JudgeInput) -> list[JudgeVerdictData]:
    """One ``pledge_violation`` per violated pledge, in the order the pledges were made.

    Evidence is the line that made the pledge and the hammer of the lot it was about (the lot
    where the pledge was broken); ``lots`` holds both lots when they differ. The quote is the
    line itself (nothing, if the seat said no words and only pledged).
    """
    found = []
    for pledge in sorted(records, key=lambda r: r.seq):
        if pledge.outcome != "violated":
            continue
        target = record.lot(pledge.pledge.lot)
        seqs = [pledge.seq]
        if target is not None and target.hammer_seq is not None:
            seqs.append(target.hammer_seq)
        line = next(
            (t for t in record.talk_of(pledge.seat, pledge.lot) if t.seq == pledge.seq), None
        )
        quotes = [line.text] if line is not None and line.text else []
        found.append(
            JudgeVerdictData(
                verdict="pledge_violation",
                seats=[pledge.seat],
                lots=sorted({pledge.lot, pledge.pledge.lot}),
                evidence_seqs=sorted(set(seqs)),
                quotes=quotes,
                intent="unknown",
                detail=pledge.detail,
            )
        )
    return found


# ---------------------------------------------------------------------------- claims


def claim_verdicts(claims: Sequence[FalseClaim], record: JudgeInput) -> list[JudgeVerdictData]:
    """One ``false_claim`` per claim that both judges found and the truth contradicts."""
    found = []
    for claim in claims:
        lot = record.lot(claim.lot)
        seqs = [seq for seq in (claim.seq, None if lot is None else lot.hammer_seq) if seq]
        found.append(
            JudgeVerdictData(
                verdict="false_claim",
                seats=[claim.seat],
                lots=[claim.lot],
                evidence_seqs=sorted(set(seqs)),
                quotes=list(claim.quotes),
                intent="unknown",
                detail=claim.detail,
                topic=claim.topic,  # type: ignore[arg-type]
                gap_milli=claim.gap_milli,
            )
        )
    return found


# -------------------------------------------------------------------------- collusion


@dataclass(frozen=True, slots=True)
class AgreedQuote:
    """A promise of class ``collusion`` that both judges found: who, where, in whose words."""

    seat: int
    lot: int
    quote: str
    seq: int | None


@dataclass(frozen=True, slots=True)
class LotFinding:
    """One lot seen from both sides.

    ``chat`` says an agreed promise covers the lot, ``over`` that the statistic is above the
    threshold. ``discount_milli`` and ``cover_milli`` are the two parts of ``statistic_milli``.
    """

    lot: int
    chat: bool
    over: bool
    statistic_milli: int
    discount_milli: int
    cover_milli: int

    @property
    def status(self) -> Literal["red", "suspect"] | None:
        """``red`` with both sides, ``suspect`` with one, None with neither."""
        if self.chat and self.over:
            return "red"
        return "suspect" if self.chat or self.over else None


@dataclass(frozen=True, slots=True)
class CollusionReport:
    """The collusion screen of a night: the ring, the agreed promises and every lot's finding.

    ``judges_agree`` is False when a judge is absent (the chat side is then closed).
    ``skipped`` lists the lots that were not screened because the cost gate muted the models.
    """

    ring: tuple[int, ...]
    quotes: tuple[AgreedQuote, ...]
    lots: tuple[LotFinding, ...]
    threshold_milli: int
    judges_agree: bool
    skipped: tuple[int, ...] = ()

    def of_status(self, status: str) -> tuple[LotFinding, ...]:
        """The findings of one status, in lot order."""
        return tuple(f for f in self.lots if f.status == status)


def collusion_report(
    run: JudgeRun,
    screens: Sequence[LotScreen],
    *,
    players: Sequence[int],
    threshold_milli: int = NULL_THRESHOLD_MILLI,
    skipped: Sequence[int] = (),
) -> CollusionReport:
    """Put the judges' agreed promises and the bid statistic side by side, lot by lot.

    ``screens`` are the lots that could be screened; ``skipped`` the lots that could not, because
    the cost gate muted the models there.
    """
    quotes: list[AgreedQuote] = []
    if run.both:
        first, second = run.extracts[0], run.extracts[1]
        for a, _ in agreed_pairs(first, second, same_promise):
            if promise_class(a) == "collusion" and a.seat in players and a.lot is not None:
                quotes.append(
                    AgreedQuote(a.seat, a.lot, a.quote, run.record.seq_of.get((a.seat, a.lot)))
                )
    quotes.sort(key=lambda q: (q.lot, q.seat, q.quote))
    ring = tuple(sorted({q.seat for q in quotes}))
    spoken = {q.lot for q in quotes}
    findings = []
    for screen in screens:
        value = screen.statistic_milli(ring)
        findings.append(
            LotFinding(
                lot=screen.lot,
                chat=screen.lot in spoken or screen.lot - 1 in spoken,
                over=value > threshold_milli,
                statistic_milli=value,
                discount_milli=screen.discount_milli,
                cover_milli=screen.cover_milli(ring),
            )
        )
    return CollusionReport(
        ring=ring,
        quotes=tuple(quotes),
        lots=tuple(findings),
        threshold_milli=threshold_milli,
        judges_agree=run.both,
        skipped=tuple(skipped),
    )


def _lots_text_of(lots: Sequence[int]) -> str:
    return "、".join(str(lot) for lot in lots)


def _lots_text(findings: Sequence[LotFinding]) -> str:
    return _lots_text_of([f.lot for f in findings])


def _statistics_text(findings: Sequence[LotFinding]) -> str:
    return "，".join(f"第 {f.lot} 件 {f.statistic_milli}" for f in findings)


def collusion_verdict(
    report: CollusionReport, record: JudgeInput, *, min_red_lots: int = MIN_RED_LOTS
) -> JudgeVerdictData | None:
    """The one collusion verdict of the night, or None when no lot has either side.

    ``collusion_red`` needs ``min_red_lots`` lots with both sides (see the module text);
    otherwise the verdict is ``collusion_suspect`` and lists every lot with at least one. Its
    ``lots`` are those lots; its ``statistic_milli`` covers all the lots of the report.
    """
    both = report.of_status("red")
    one = report.of_status("suspect")
    is_red = len(both) >= min_red_lots
    chosen = both if is_red else tuple(sorted((*both, *one), key=lambda f: f.lot))
    if not chosen:
        return None
    threshold = report.threshold_milli
    chat_only = [f for f in one if not f.over]
    bid_only = [f for f in one if not f.chat]
    parts: list[str] = []
    if both:
        parts.append(
            f"第 {_lots_text(both)} 件两侧都满足：聊天里两位评委都抽到了约定压价或轮流拿的原话，"
            f"出价侧统计量（{_statistics_text(both)}）超过阈值 {threshold}。"
        )
        if is_red and one:
            parts.append(f"另有 {len(one)} 件只满足一侧。")
        elif not is_red:
            parts.append(
                f"红牌要求至少 {min_red_lots} 件两侧都满足，这里只有 {len(both)} 件，记为疑似。"
            )
    if chat_only and not is_red:
        parts.append(
            f"第 {_lots_text(chat_only)} 件只有聊天一侧成立（两位评委都抽到了约定的原话），"
            f"出价侧统计量没有超过阈值 {threshold}。"
        )
    if bid_only and not is_red:
        parts.append(
            f"第 {_lots_text(bid_only)} 件只有出价一侧成立，统计量（{_statistics_text(bid_only)}）"
            f"超过阈值 {threshold}，聊天里没有两位评委都认定的约定。"
        )
    if not report.judges_agree:
        parts.append("有评委缺席，聊天一侧无法成立。")
    if report.skipped:
        parts.append(f"第 {_lots_text_of(report.skipped)} 件的模型座位被费用上限静音，没有筛查。")
    parts.append("统计量和阈值的单位是千分之档位。")

    wanted = {f.lot for f in chosen}
    shown = [q for q in report.quotes if q.lot in wanted or q.lot + 1 in wanted]
    seqs = {q.seq for q in shown if q.seq is not None}
    for finding in chosen:
        entry = record.lot(finding.lot)
        if entry is not None and entry.hammer_seq is not None:
            seqs.add(entry.hammer_seq)
    return JudgeVerdictData(
        verdict="collusion_red" if is_red else "collusion_suspect",
        seats=list(report.ring),
        lots=[f.lot for f in chosen],
        evidence_seqs=sorted(seqs),
        quotes=list(dict.fromkeys(q.quote for q in shown))[:MAX_QUOTES],
        intent=None,
        detail="".join(parts),
        statistic_milli={f.lot: f.statistic_milli for f in report.lots},
        threshold_milli=threshold,
    )


# ------------------------------------------------------------------------- the stage


@dataclass(frozen=True)
class Judging:
    """The judging stage of a night: what the judges said and every finding.

    ``events`` are the ``judge_extract`` events of the judges that delivered, then the
    ``judge_verdict`` events, all for the audience, numbered from 1 (append them with
    :func:`~auction_night.engine.night.append_events`). ``pledges``, ``judges`` (the raw answers),
    ``verdicts`` and ``accuracy`` are the replay parts ``pledges``, ``judges``, ``verdicts`` and
    ``judge_accuracy``. ``board`` is the judges' score of the night with the agreement of the two,
    which a season adds up (:func:`~auction_night.show.judge_board.season_board`).
    """

    run: JudgeRun
    pledges: tuple[PledgeRecord, ...]
    false_claims: tuple[FalseClaim, ...]
    collusion: CollusionReport
    verdicts: tuple[JudgeVerdictData, ...]
    events: tuple[AnyEvent, ...]
    accuracy: tuple[JudgeAccuracy, ...]
    board: NightBoard

    @property
    def judges(self) -> tuple[JudgeExtractData, ...]:
        """The raw answers of the judges that delivered (the replay's ``judges``)."""
        return self.run.extracts


def verdict_events(
    verdicts: Sequence[JudgeVerdictData], *, first_seq: int = 1
) -> tuple[JudgeVerdictEvent, ...]:
    """The ``judge_verdict`` events (audience only) of some verdicts, numbered from ``first_seq``.

    A verdict about one lot carries that lot.
    """
    return tuple(
        JudgeVerdictEvent(
            seq=number,
            lot=data.lots[0] if len(data.lots) == 1 else None,
            vis="audience",
            data=data,
        )
        for number, data in enumerate(verdicts, start=first_seq)
    )


def run_judging(
    state: NightState,
    backends: Sequence[JudgeBackend],
    *,
    threshold_milli: int = NULL_THRESHOLD_MILLI,
    templates: TemplateSet | None = None,
) -> Judging:
    """Ask the judges about a finished night and gather every finding.

    Order of the verdicts: broken pledges (in the order they were made), false claims (by lot,
    seat, topic), then the collusion verdict if there is one.
    """
    if not state.done:
        raise ValueError("the judges read a night that is over")
    record = build_judge_input(state.events)
    run = run_judges(record, backends, templates=templates)
    truth = Truth.of_events(state.events)
    pledges = tuple(judge_pledges(state.events))
    claims: tuple[FalseClaim, ...] = ()
    if run.both:
        claims = tuple(false_claims(run.extracts[0], run.extracts[1], truth, record))
    report = collusion_report(
        run,
        screen_state(state),
        players=truth.players,
        threshold_milli=threshold_milli,
        skipped=muted_lots(state),
    )
    verdicts = [*pledge_verdicts(pledges, record), *claim_verdicts(claims, record)]
    collusion = collusion_verdict(report, record)
    if collusion is not None:
        verdicts.append(collusion)
    extract_events = judge_events(run)
    board = night_board(run, truth)
    return Judging(
        run=run,
        pledges=pledges,
        false_claims=claims,
        collusion=report,
        verdicts=tuple(verdicts),
        events=(
            *extract_events,
            *verdict_events(verdicts, first_seq=len(extract_events) + 1),
        ),
        accuracy=tuple(score.accuracy() for score in board.scores),
        board=board,
    )
