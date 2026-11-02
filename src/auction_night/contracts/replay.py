"""replay.json v1: the single static file a viewer needs to show one night.

A replay is built from the full event log after the night and the show layer. It is
self-contained: it holds the public timeline, the hole cards that the viewer reveals,
the verdicts, the awards, the offline commentary and the run manifest. Like events, it has
integer amounts only. Serialise it with ``canonical_json``, which writes the key ``schema``.
"""

from typing import Annotated, Any, Final, Literal, Self

from pydantic import ConfigDict, Field, model_validator

from auction_night.contracts.events import (
    AbstainReason,
    ActionStatus,
    AwardData,
    Basis,
    Block,
    Coins,
    Count,
    DutchResultData,
    EnglishDropoutData,
    HammerData,
    Intent,
    JudgeExtractData,
    JudgeVerdictData,
    Line,
    Lot,
    LotKey,
    LotPlan,
    MissionResultData,
    Mode,
    Name,
    NightStatus,
    PlanData,
    Pledge,
    Seat,
    SeatKey,
    SeatScore,
    SeedText,
    SeqNo,
    Sha256,
    SignedCoins,
    TalkData,
)
from auction_night.contracts.events import seed_commitment as commitment_of
from auction_night.contracts.mechanism import (
    MAX_LOTS,
    MAX_SEATS,
    ContractModel,
    FormatKey,
    Phase,
)

REPLAY_SCHEMA_ID: Final = "auction-night/replay@1"

Version = Annotated[str, Field(pattern=r"^v[0-9]+$")]
HighlightCategory = Literal[
    "winners_curse", "shill_stuck", "backstab", "claim_vs_hand", "plan_contradiction",
    "all_pay_tilt", "bargain", "mindread_miss",
]  # fmt: skip


# ----------------------------------------------------------------------- night and seats


class ReplayNight(ContractModel):
    """Public facts about the night."""

    id: Name
    lang: Literal["zh"]
    mode: Mode
    status: NightStatus
    ranked: bool
    n_lots: Annotated[int, Field(strict=True, ge=1, le=MAX_LOTS)]
    mechanisms: dict[FormatKey, Version]
    budget_lo: Coins
    budget_hi: Coins
    collectors: Count
    shills: Count
    mission_bonus: Coins
    season_id: Name | None = None
    deck_id: Name | None = None
    rotation: Annotated[int, Field(strict=True, ge=0, le=MAX_SEATS - 1)] | None = None


class ReplaySeat(ContractModel):
    """One seat as the audience sees it. ``model_returned`` is the model id the API reported."""

    seat: Seat
    codename: Name
    kind: Literal["llm", "mock", "bot", "human"]
    vendor: Name | None = None
    endpoint: Name | None = None
    model: Name | None = None
    model_returned: Name | None = None
    persona: Name | None = None
    policy: Name | None = None
    thinking: Name | None = None
    effort: Name | None = None


# --------------------------------------------------------------------------- timeline


class TimelineAction(ContractModel):
    """A bid-phase decision. The inner monologue is in ``reveal.thoughts``."""

    seq: SeqNo
    seat: Seat
    phase: Phase
    status: ActionStatus
    reason: AbstainReason | None = None
    value: Coins | None = None
    raw_value: Coins | None = None


class LotOverlay(ContractModel):
    """Per-lot reference numbers for the overlay. Descriptive only, never aggregated.

    ``excess`` is what a seat earned minus what its textbook bid would have earned, so it is
    negative when leaving the textbook bid cost the seat. ``expected_revenue_milli`` is the exact
    expected revenue of the lot for the number of bidders it had, in thousandths of a coin.
    ``curse_gap`` is the price of a blind box minus its true value.
    """

    ref_bids: dict[SeatKey, Coins] = Field(default_factory=dict)
    alpha_milli: dict[SeatKey, Count] = Field(default_factory=dict)
    regret: dict[SeatKey, Count] = Field(default_factory=dict)
    to_highest_value: bool | None = None
    excess: dict[SeatKey, SignedCoins] = Field(default_factory=dict)
    expected_revenue_milli: Count | None = None
    curse_gap: SignedCoins | None = None


class TimelineLot(ContractModel):
    """Everything the viewer needs to play one lot."""

    lot: Lot
    format: FormatKey
    mechanism: Name
    first_seq: SeqNo
    last_seq: SeqNo
    active: list[Seat]
    sitting_out: list[Seat] = Field(default_factory=list)
    talk_order: list[Seat]
    talk: list[TalkData] = Field(default_factory=list)
    actions: list[TimelineAction] = Field(default_factory=list)
    dropouts: list[EnglishDropoutData] = Field(default_factory=list)
    dutch: DutchResultData | None = None
    hammer: HammerData
    overlay: LotOverlay = Field(default_factory=LotOverlay)
    host: Line | None = None
    talk_seqs: dict[SeatKey, SeqNo] = Field(default_factory=dict)
    hammer_seq: SeqNo | None = None

    @model_validator(mode="after")
    def _seq_order(self) -> Self:
        if self.first_seq > self.last_seq:
            raise ValueError("first_seq exceeds last_seq")
        return self


# ----------------------------------------------------------------------------- reveal


class GuessRecord(ContractModel):
    """A mind-reading guess with the truth, known after the fact."""

    lot: Lot
    seat: Seat
    target: Seat
    guess: Coins
    truth: Coins
    abs_error: Count


class ThoughtRecord(ContractModel):
    """One inner monologue line."""

    seq: SeqNo
    lot: Lot | None = None
    seat: Seat
    phase: Phase
    text: Line


class Reveal(ContractModel):
    """Hole cards and private records, shown by the hole-card camera."""

    budgets: dict[SeatKey, Coins]
    values: dict[SeatKey, dict[LotKey, Coins]]
    signals: dict[SeatKey, dict[LotKey, Coins]] = Field(default_factory=dict)
    true_values: dict[LotKey, Coins] = Field(default_factory=dict)
    missions: list[MissionResultData] = Field(default_factory=list)
    plans: list[PlanData] = Field(default_factory=list)
    guesses: list[GuessRecord] = Field(default_factory=list)
    thoughts: list[ThoughtRecord] = Field(default_factory=list)


class PledgeRecord(ContractModel):
    """A pledge and how it ended. The outcome is decided by program."""

    seq: SeqNo
    lot: Lot
    seat: Seat
    pledge: Pledge
    outcome: Literal["kept", "violated", "void"]
    intent: Intent = "unknown"
    detail: Line = ""


class JudgeAccuracy(ContractModel):
    """How well one judge guessed the secret missions of the night."""

    judge: Annotated[int, Field(strict=True, ge=0, le=3)]
    vendor: Name
    guesses: Count
    correct: Count
    targets: Count


class Highlight(ContractModel):
    """A moment worth jumping to. ``score`` runs from 0 to 10.

    ``basis`` says whether the moment rests on public facts or on private values.
    """

    id: Annotated[int, Field(strict=True, ge=1)]
    category: HighlightCategory
    score: Annotated[int, Field(strict=True, ge=0, le=10)]
    lot: Lot | None = None
    seats: list[Seat] = Field(default_factory=list)
    first_seq: SeqNo
    last_seq: SeqNo
    text: Line
    basis: Basis | None = None


class SeatStats(ContractModel):
    """Reference columns of the leaderboard. Ratios are integer thousandths."""

    actions: Count = 0
    invalid_inputs: Count = 0
    abstentions: Count = 0
    refusals: Count = 0
    clamps: Count = 0
    syntax_repairs: Count = 0
    valid_guesses: Count = 0
    mind_error_milli: Count | None = None
    judgable_lots: Count = 0
    consistency_milli: Count | None = None
    textbook_bids: Count = 0
    textbook_deviation_milli: Count | None = None


class ReplayScore(SeatScore):
    """A seat score plus the reference statistics."""

    stats: SeatStats = Field(default_factory=SeatStats)


# -------------------------------------------------------------------------- commentary


class HostLine(ContractModel):
    """One offline commentary line for a lot. ``fallback`` means fact card only."""

    lot: Lot
    text: Line
    fallback: bool = False


class PredictionItem(ContractModel):
    """One pre-show prediction and whether the night confirmed it."""

    format: FormatKey
    claim: Name
    text: Line
    hit: bool | None = None


class PredictionCard(ContractModel):
    """Pre-show predictions per format, reconciled after the night."""

    items: list[PredictionItem] = Field(default_factory=list)
    hits: Count | None = None


class HostBlock(ContractModel):
    """Offline commentary written from the fact cards."""

    per_lot: list[HostLine] = Field(default_factory=list)
    recap: Block | None = None
    prediction_card: PredictionCard | None = None


# ------------------------------------------------------------------------ usage, manifest


class RoleUsage(ContractModel):
    """Calls and tokens of one role. Providers count tokens differently."""

    calls: Count = 0
    input_tokens: Count = 0
    output_tokens: Count = 0
    cache_read_tokens: Count = 0
    cache_write_tokens: Count = 0
    reasoning_tokens: Count = 0
    retries: Count = 0
    timeouts: Count = 0
    refusals: Count = 0
    latency_ms: Count = 0


class Usage(ContractModel):
    """Usage by role (``seat:3``, ``host``, ``judge:0``) and in total."""

    by_role: dict[Name, RoleUsage] = Field(default_factory=dict)
    total: RoleUsage = Field(default_factory=RoleUsage)
    cost_microusd: Count | None = None


class ClearedFinding(ContractModel):
    """A finding of the content gate that a person read and released for publication.

    ``path`` and ``category`` are the place and the category that the gate reported: a category
    of the classifier, or ``banned_word`` or ``wordlist`` for a word list. ``detail`` is what the
    gate said (the reason of the classifier, or the term of the word list), and ``reason`` is the
    reason that the person gave.
    """

    path: Name
    category: Name
    detail: Line = ""
    reason: Annotated[Line, Field(min_length=1)]


class Manifest(ContractModel):
    """What is needed to reproduce and audit the run.

    ``cleared_findings`` lists the findings of the content gate that a person released, in the
    order of the gate's report. It is empty for a night the gate passed on its own.
    """

    config_hash: Sha256
    prompt_template_version: Name
    prompt_hash: Sha256 | None = None
    tool_schema_hash: Sha256 | None = None
    engine_version: Name
    git_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")] | None = None
    git_dirty: bool = False
    mechanism_versions: dict[FormatKey, Version] = Field(default_factory=dict)
    seed_commitment: Sha256 | None = None
    revealed_seed: SeedText | None = None
    models_returned: dict[Name, Name] = Field(default_factory=dict)
    created_utc: Name | None = None
    host_prompt_hash: Sha256 | None = None
    judge_prompt_hash: Sha256 | None = None
    scan_prompt_hash: Sha256 | None = None
    cleared_findings: list[ClearedFinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def _reveal_opens_commitment(self) -> Self:
        if self.revealed_seed is None:
            return self
        if self.seed_commitment is None:
            raise ValueError("a revealed seed needs the commitment it opens")
        if commitment_of(self.revealed_seed) != self.seed_commitment:
            raise ValueError("the revealed seed does not open the commitment")
        return self


class JudgeAbsence(ContractModel):
    """A judge that gave no usable answer this night, and why."""

    judge: Annotated[int, Field(strict=True, ge=0, le=3)]
    vendor: Name
    reason: Literal["unreadable", "timeout", "refusal", "budget_aborted", "other"]


# --------------------------------------------------------------------------------- root


def _schema_is_required(schema: dict[str, Any]) -> None:
    required = schema.setdefault("required", [])
    if "schema" not in required:
        required.append("schema")


class Replay(ContractModel):
    """replay.json v1. Required parts always exist, the show layer parts default to empty."""

    model_config = ConfigDict(populate_by_name=True, json_schema_extra=_schema_is_required)

    schema_id: Literal["auction-night/replay@1"] = Field(alias="schema")
    night: ReplayNight
    seats: list[ReplaySeat]
    catalog: list[LotPlan]
    timeline: list[TimelineLot]
    reveal: Reveal
    scores: list[ReplayScore]
    manifest: Manifest
    pledges: list[PledgeRecord] = Field(default_factory=list)
    judges: list[JudgeExtractData] = Field(default_factory=list)
    judges_absent: list[JudgeAbsence] = Field(default_factory=list)
    verdicts: list[JudgeVerdictData] = Field(default_factory=list)
    judge_accuracy: list[JudgeAccuracy] = Field(default_factory=list)
    awards: list[AwardData] = Field(default_factory=list)
    bots_reference: list[AwardData] = Field(default_factory=list)
    highlights: list[Highlight] = Field(default_factory=list)
    host: HostBlock = Field(default_factory=HostBlock)
    usage: Usage = Field(default_factory=Usage)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        problems: list[str] = []
        seats = [seat.seat for seat in self.seats]
        if len(set(seats)) != len(seats):
            problems.append("duplicate seat in seats")
        if [lot.lot for lot in self.catalog] != list(range(1, self.night.n_lots + 1)):
            problems.append("catalog must hold lots 1..n_lots in order")
        lots = [entry.lot for entry in self.timeline]
        if lots != sorted(set(lots)) or any(lot > self.night.n_lots for lot in lots):
            problems.append("timeline lots must be unique, ascending and within n_lots")
        if not {score.seat for score in self.scores} <= set(seats):
            problems.append("scores mention a seat that is not in seats")
        versions = self.manifest.mechanism_versions
        if versions and versions != self.night.mechanisms:
            problems.append("manifest and night disagree on the mechanism versions")
        if problems:
            raise ValueError("; ".join(problems))
        return self
