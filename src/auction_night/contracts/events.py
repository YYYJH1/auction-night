"""Event log contract: envelope, per-type payloads, visibility levels and canonical JSON.

Every fact of a night is one event. The engine appends events, prompts are rendered from the
events a seat may see, and replays are exported from the full log. Amounts are integers
(coins). Ratios that need a fraction are stored as integer thousandths ("milli").

Visibility levels (the ``vis`` field, exactly these strings):

* ``public``    seats and audience may see it.
* ``seat:N``    seat N may see it; it is shown to everyone after the night ends.
* ``audience``  never shown to seats; produced for the show layer and the replay.
* ``system``    internal bookkeeping, never published.

A prompt for seat N is built only from ``public`` and ``seat:N`` events.
"""

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Annotated, Any, ClassVar, Final, Literal, Self, get_args

from pydantic import (
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from auction_night.contracts.mechanism import (
    MAX_LOTS,
    MAX_SEATS,
    ContractModel,
    FormatKey,
    Phase,
)

# --------------------------------------------------------------------------- primitives

MAX_COINS: Final = 2**53 - 1  # largest integer that a JSON reader in a browser keeps exact
PRIORITY_MAX: Final = 3  # plan priorities run from 0 to 3


def _int_key(value: Any) -> Any:
    """Accept an int, or the decimal string that a JSON object key carries."""
    if isinstance(value, bool):
        raise ValueError("a boolean is not an integer key")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    raise ValueError("expected an integer key")


Seat = Annotated[int, Field(strict=True, ge=1, le=MAX_SEATS)]
Lot = Annotated[int, Field(strict=True, ge=1, le=MAX_LOTS)]
Coins = Annotated[int, Field(strict=True, ge=0, le=MAX_COINS)]
SignedCoins = Annotated[int, Field(strict=True, ge=-MAX_COINS, le=MAX_COINS)]
Count = Annotated[int, Field(strict=True, ge=0, le=MAX_COINS)]
SeqNo = Annotated[int, Field(strict=True, ge=1, le=MAX_COINS)]
SeatKey = Annotated[int, BeforeValidator(_int_key), Field(ge=1, le=MAX_SEATS)]
LotKey = Annotated[int, BeforeValidator(_int_key), Field(ge=1, le=MAX_LOTS)]
TierKey = Annotated[int, BeforeValidator(_int_key), Field(ge=1, le=MAX_COINS)]
Priority = Annotated[int, Field(strict=True, ge=0, le=PRIORITY_MAX)]

# Single-line text has no control characters at all. Block text allows newline and tab.
_LINE_PATTERN = r"^[^\x00-\x1f\x7f-\x9f  ]*$"
_BLOCK_PATTERN = r"^[^\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f  ]*$"
Name = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=_LINE_PATTERN)]
Line = Annotated[str, StringConstraints(max_length=2000, pattern=_LINE_PATTERN)]
Block = Annotated[str, StringConstraints(max_length=20000, pattern=_BLOCK_PATTERN)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
SEED_PATTERN: Final = r"^[0-9A-Za-z_.:-]+$"
SeedText = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=SEED_PATTERN)]
MechRef = Annotated[str, StringConstraints(pattern=r"^[a-z_]+@v[0-9]+$")]
Vis = Annotated[str, StringConstraints(pattern=rf"^(public|audience|system|seat:[1-{MAX_SEATS}])$")]

# ---------------------------------------------------------------- canonical serialisation


def _plain(obj: Any) -> Any:
    """Reduce models, mappings and sequences to JSON types with string keys."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json", by_alias=True)
    if isinstance(obj, Mapping):
        return {str(key): _plain(value) for key, value in obj.items()}
    if isinstance(obj, list | tuple):
        return [_plain(value) for value in obj]
    return obj


def _reject_floats(obj: Any, path: str = "$") -> None:
    if isinstance(obj, float):
        raise ValueError(f"float at {path}: amounts are integers and ratios use milli units")
    if isinstance(obj, dict):
        for key, value in obj.items():
            _reject_floats(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            _reject_floats(value, f"{path}[{index}]")


def canonical_json(obj: Any, *, allow_float: bool = False) -> str:
    """Deterministic compact JSON: sorted keys, UTF-8 text, no NaN, integer amounts.

    Models are dumped with their aliases. Floats are rejected unless ``allow_float`` is set,
    which only the configuration hash uses.
    """
    data = _plain(obj)
    if not allow_float:
        _reject_floats(data)
    return json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def sha256_digest(data: str | bytes) -> str:
    """``sha256:<hex>`` digest of text (UTF-8) or bytes."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def seed_commitment(seed: str) -> str:
    """Commitment to a deal seed: the SHA-256 digest of its UTF-8 text.

    The commitment is published before a season and the seed after each night. A seed is only
    as hard to guess as it is long, so published seasons use long random seeds.
    """
    return sha256_digest(seed)


# --------------------------------------------------------------------------- visibility

VisKind = Literal["public", "seat", "audience", "system"]


def seat_vis(seat: int) -> str:
    """The visibility string of one seat."""
    if not 1 <= seat <= MAX_SEATS:
        raise ValueError(f"seat out of range: {seat}")
    return f"seat:{seat}"


def vis_kind(vis: str) -> VisKind:
    """The level of a visibility string."""
    if vis == "public":
        return "public"
    if vis == "audience":
        return "audience"
    if vis == "system":
        return "system"
    if vis.startswith("seat:"):
        vis_seat(vis)
        return "seat"
    raise ValueError(f"bad visibility {vis!r}")


def vis_seat(vis: str) -> int | None:
    """The seat of a ``seat:N`` visibility, else None."""
    if not vis.startswith("seat:"):
        return None
    number = vis[5:]
    if not (number.isascii() and number.isdigit()) or not 1 <= int(number) <= MAX_SEATS:
        raise ValueError(f"bad visibility {vis!r}")
    return int(number)


def visible_to(vis: str, viewer: str) -> bool:
    """Whether a viewer may see an event of the given visibility.

    Viewers: ``seat:N`` (a player: public and own), ``audience`` (live viewer: public and
    audience), ``god`` (replay with hole cards: everything except system), ``system``
    (everything).
    """
    kind = vis_kind(vis)
    if viewer == "system":
        return True
    if viewer == "god":
        return kind != "system"
    if viewer == "audience":
        return kind in ("public", "audience")
    if vis_seat(viewer) is not None:
        return kind == "public" or vis == viewer
    raise ValueError(f"unknown viewer {viewer!r}")


# ---------------------------------------------------------------------- shared payloads

ActionStatus = Literal["ok", "clamped", "abstain", "kept_previous"]
AbstainReason = Literal[
    "missing", "parse_failed", "timeout", "refusal", "budget_aborted", "not_asked"
]
RepairCode = Literal[
    "syntax", "coerce", "clamp", "invalid_input", "retry", "fallback", "refusal", "timeout",
    "sanitize",
]  # fmt: skip
RepairStatus = Literal["ok", "retry", "abstain"]
MissionKind = Literal["collector", "shill"]
NightStatus = Literal["completed", "budget_aborted"]
Mode = Literal["plain", "variety"]
AwardKey = Literal[
    "most_textbook", "winners_curse", "tilt", "bargain", "backstabber", "best_actor", "keeper",
    "mission_master", "mind_reader", "consistent",
]  # fmt: skip
AWARD_KEYS: Final[tuple[AwardKey, ...]] = get_args(AwardKey)
MetricUnit = Literal["coins", "count", "milli", "none"]
VerdictKind = Literal["pledge_violation", "false_claim", "collusion_red", "collusion_suspect"]
Intent = Literal["deliberate", "unintentional", "unknown"]


class LotPlan(ContractModel):
    """Public pre-show entry for one lot. The blind-box value is not part of it."""

    lot: Lot
    item: Name
    name: Name
    emoji: Name
    tag: Name
    tier: Annotated[int, Field(strict=True, ge=1, le=MAX_COINS)]
    format: FormatKey
    mechanism: MechRef
    reserve: Coins = 0
    blurb: Line = ""

    @model_validator(mode="after")
    def _mechanism_matches_format(self) -> Self:
        if not self.mechanism.startswith(f"{self.format}@"):
            raise ValueError(f"mechanism {self.mechanism} does not belong to format {self.format}")
        return self


class SeatPublic(ContractModel):
    """What every seat may know about a seat: its code name and whether it is a bot."""

    seat: Seat
    codename: Name
    role: Literal["player", "bot"]


class CollectorMission(ContractModel):
    """Win ``need`` lots that carry ``tag``."""

    kind: Literal["collector"]
    tag: Name
    need: Annotated[int, Field(strict=True, ge=1, le=MAX_LOTS)]
    bonus: Coins
    canary: Name


class ShillMission(ContractModel):
    """Push lot ``lot`` to at least ``threshold_price`` without buying it."""

    kind: Literal["shill"]
    lot: Lot
    threshold_price: Coins
    bonus: Coins
    canary: Name


Mission = Annotated[CollectorMission | ShillMission, Field(discriminator="kind")]


class Pledge(ContractModel):
    """A structured, public pledge. Breaking it is decided by program, not by a judge."""

    kind: Literal["no_bid", "max_bid", "let_win"]
    lot: Lot
    amount: Coins | None = None
    to: Seat | None = None

    @model_validator(mode="after")
    def _fields_match_kind(self) -> Self:
        if (self.kind == "max_bid") != (self.amount is not None):
            raise ValueError(f"pledge {self.kind}: amount is needed exactly for max_bid")
        if (self.kind == "let_win") != (self.to is not None):
            raise ValueError(f"pledge {self.kind}: to is needed exactly for let_win")
        return self


class SeatScore(ContractModel):
    """Final tally of one seat. Surplus and mission bonus are kept apart."""

    seat: Seat
    surplus: SignedCoins
    mission_bonus: Coins
    total: SignedCoins
    rank_tonight: Annotated[int, Field(strict=True, ge=1, le=MAX_SEATS)] | None = None
    spent: Coins = 0
    lots_won: list[Lot] = Field(default_factory=list)

    @model_validator(mode="after")
    def _total_adds_up(self) -> Self:
        if self.total != self.surplus + self.mission_bonus:
            raise ValueError("total must equal surplus plus mission_bonus")
        return self


ClaimTopic = Literal["budget", "valuation", "mission_kind", "mission_tag", "plan"]
PromiseClass = Literal["collusion", "bid", "other"]


class Extraction(ContractModel):
    """A promise or a claim about the speaker's own hand, quoted by a judge.

    ``promise_class`` says what a promise is about (``collusion`` for an agreement to keep a
    price down or to take turns) and ``partner`` the seat that such a promise names.
    """

    seat: Seat
    lot: Lot | None = None
    kind: Literal["promise", "claim"]
    quote: Line
    ambiguous: bool = False
    topic: ClaimTopic | None = None
    number: Coins | None = None
    text_value: Name | None = None
    promise_class: PromiseClass | None = None
    partner: Seat | None = None


class MissionGuess(ContractModel):
    """A judge's guess of one seat's secret mission, from public behaviour only."""

    seat: Seat
    kind: MissionKind
    tag: Name | None = None
    lot: Lot | None = None


class InjectionFlag(ContractModel):
    """Speech that tried to impersonate the system or change the rules."""

    seat: Seat
    lot: Lot | None = None
    quote: Line


# ------------------------------------------------------------------------ event payloads


class NightStartData(ContractModel):
    """Public pre-show: the whole lot sequence and the seat list."""

    night_id: Name
    lang: Literal["zh"]
    mode: Mode
    n_lots: Annotated[int, Field(strict=True, ge=1, le=MAX_LOTS)]
    seats: list[SeatPublic]
    lots: list[LotPlan]
    budget_lo: Coins
    budget_hi: Coins
    collectors: Count
    shills: Count
    mission_bonus: Coins

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if [lot.lot for lot in self.lots] != list(range(1, self.n_lots + 1)):
            raise ValueError("lots must be numbered 1..n_lots in order")
        if len({seat.seat for seat in self.seats}) != len(self.seats):
            raise ValueError("duplicate seat in seats")
        if self.budget_lo > self.budget_hi:
            raise ValueError("budget_lo exceeds budget_hi")
        return self


class SeedCommitData(ContractModel):
    """Commitment to the deal seed before the night, and its reveal after."""

    phase: Literal["commit", "reveal"]
    algorithm: Literal["sha256"] = "sha256"
    commitment: Sha256
    seed: SeedText | None = None
    deck_id: Name | None = None

    @model_validator(mode="after")
    def _seed_only_on_reveal(self) -> Self:
        if (self.phase == "reveal") != (self.seed is not None):
            raise ValueError("the seed is present exactly when phase is reveal")
        if self.seed is not None and seed_commitment(self.seed) != self.commitment:
            raise ValueError("the revealed seed does not open the commitment")
        return self


class DealPrivateData(ContractModel):
    """Private cards of one seat: budget, valuations, blind-box signals and mission."""

    seat: Seat
    budget: Coins
    values: dict[LotKey, Coins] = Field(default_factory=dict)
    signals: dict[LotKey, Coins] = Field(default_factory=dict)
    mission: Mission | None = None


class PlanData(ContractModel):
    """The opening plan of one seat (sanitised)."""

    seat: Seat
    priorities: list[Priority]
    budget_plan: list[Coins]
    persona: Line
    sanitized_chars: Count = 0

    @model_validator(mode="after")
    def _same_length(self) -> Self:
        if len(self.priorities) != len(self.budget_plan):
            raise ValueError("priorities and budget_plan must have one entry per lot")
        return self


class LotOpenData(ContractModel):
    """A lot opens. ``active`` seats will be asked to act, ``sitting_out`` seats will not."""

    format: FormatKey
    item: Name
    mechanism: MechRef
    tier: Annotated[int, Field(strict=True, ge=1, le=MAX_COINS)]
    reserve: Coins = 0
    active: list[Seat]
    sitting_out: list[Seat] = Field(default_factory=list)
    talk_order: list[Seat]

    @model_validator(mode="after")
    def _disjoint(self) -> Self:
        if set(self.active) & set(self.sitting_out):
            raise ValueError("a seat cannot be both active and sitting out")
        if not self.mechanism.startswith(f"{self.format}@"):
            raise ValueError(f"mechanism {self.mechanism} does not belong to format {self.format}")
        return self


class TalkData(ContractModel):
    """One line in the public channel, already sanitised, with an optional pledge."""

    seat: Seat
    text: Line
    pledge: Pledge | None = None
    sanitized_chars: Count = 0

    @model_validator(mode="after")
    def _no_self_gift(self) -> Self:
        pledge = self.pledge
        if pledge is not None and pledge.kind == "let_win" and pledge.to == self.seat:
            raise ValueError("a seat cannot pledge to let itself win")
        return self


class GuessData(ContractModel):
    """A mind-reading guess of another seat's remaining budget.

    The truth is deliberately not stored: an event the guesser may see must not reveal the
    target's budget. The replay export adds the truth afterwards.
    """

    seat: Seat
    target: Seat
    guess: Coins

    @model_validator(mode="after")
    def _other_seat(self) -> Self:
        if self.target == self.seat:
            raise ValueError("a seat cannot guess its own budget")
        return self


class ActionData(ContractModel):
    """What one seat did in one phase, after clamping and abstention rules.

    ``value`` is the effective number (exit price, grab price or bid). ``raw_value`` is only
    set when the effective value was clamped to the remaining budget.
    """

    seat: Seat
    phase: Phase
    status: ActionStatus
    reason: AbstainReason | None = None
    value: Coins | None = None
    raw_value: Coins | None = None
    thought: Line = ""
    sanitized_chars: Count = 0
    attempts: Annotated[int, Field(strict=True, ge=1, le=10)] = 1
    call_id: Name | None = None

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        bidding = self.phase not in ("plan", "talk")
        if self.status == "abstain":
            if self.reason is None:
                raise ValueError("an abstention needs a reason")
            if self.value is not None or self.raw_value is not None:
                raise ValueError("an abstention carries no value")
        else:
            if self.reason is not None:
                raise ValueError("only an abstention carries a reason")
            if bidding != (self.value is not None):
                raise ValueError("bid phases need a value and plan or talk phases carry none")
        if self.status == "clamped":
            if self.value is None or self.raw_value is None or self.raw_value <= self.value:
                raise ValueError("a clamped action needs raw_value above value")
        elif self.raw_value is not None:
            raise ValueError("raw_value is only set on a clamped action")
        if self.status == "kept_previous" and self.phase != "english_revise":
            raise ValueError("kept_previous only applies to an English revision")
        return self


class EnglishDropoutData(ContractModel):
    """A seat leaves the ascending clock. Non-participants are announced at price 0."""

    seat: Seat
    price: Coins
    remaining: list[Seat]
    participated: bool = True

    @model_validator(mode="after")
    def _non_participant_price(self) -> Self:
        if not self.participated and self.price != 0:
            raise ValueError("a non-participant is announced at price 0")
        return self


class DutchResultData(ContractModel):
    """Outcome of the descending clock.

    ``ladder`` holds the prices of the levels in play, highest first (levels below the
    reserve are skipped). ``level`` is the 1-based position of the level that fired.
    """

    ladder: list[Coins]
    level: Annotated[int, Field(strict=True, ge=1, le=MAX_COINS)] | None = None
    price: Coins | None = None
    buyer: Seat | None = None
    unsold: bool = False

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        parts = (self.level, self.price, self.buyer)
        if self.unsold:
            if any(part is not None for part in parts):
                raise ValueError("an unsold lot has no level, price or buyer")
        else:
            if any(part is None for part in parts):
                raise ValueError("a sold lot needs level, price and buyer")
            assert self.level is not None
            if self.level > len(self.ladder) or self.ladder[self.level - 1] != self.price:
                raise ValueError("price must equal ladder[level - 1]")
        return self


class HammerData(ContractModel):
    """The lot is decided. Sealed formats also reveal every effective bid (0 if none)."""

    format: FormatKey
    winner: Seat | None = None
    price: Coins = 0
    payments: dict[SeatKey, Coins] = Field(default_factory=dict)
    bids: dict[SeatKey, Coins] | None = None
    true_value: Coins | None = None
    unsold: bool = False
    tie_broken: bool = False
    tied: list[Seat] = Field(default_factory=list)
    newly_out: list[Seat] = Field(default_factory=list)

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.tie_broken != bool(self.tied):
            raise ValueError("tied lists the tied seats exactly when tie_broken is set")
        if self.unsold:
            if self.winner is not None or self.price != 0 or self.payments or self.tie_broken:
                raise ValueError("an unsold lot has no winner, price, payments or tie")
            return self
        if self.winner is None:
            raise ValueError("a sold lot needs a winner")
        if self.tied and self.winner not in self.tied:
            raise ValueError("the winner of a tie is one of the tied seats")
        if self.winner not in self.payments or self.payments[self.winner] != self.price:
            raise ValueError("the winner must pay exactly the price")
        if self.format != "all_pay" and set(self.payments) != {self.winner}:
            raise ValueError("only the winner pays unless the format is all_pay")
        return self


class MissionResultData(ContractModel):
    """Mission outcome, published after the last lot."""

    seat: Seat
    kind: MissionKind
    completed: bool
    bonus: Coins
    tag: Name | None = None
    need: Count | None = None
    won_lots: list[Lot] = Field(default_factory=list)
    target_lot: Lot | None = None
    threshold_price: Coins | None = None
    final_price: Coins | None = None
    buyer: Seat | None = None

    @model_validator(mode="after")
    def _kind_fields(self) -> Self:
        if self.kind == "collector" and (self.tag is None or self.need is None):
            raise ValueError("a collector result needs tag and need")
        if self.kind == "shill" and (self.target_lot is None or self.threshold_price is None):
            raise ValueError("a shill result needs target_lot and threshold_price")
        if not self.completed and self.bonus != 0:
            raise ValueError("an unfinished mission earns no bonus")
        return self


class NightEndData(ContractModel):
    """Final scores of every seat. Bots have no ``rank_tonight``."""

    status: NightStatus
    scores: list[SeatScore]

    @model_validator(mode="after")
    def _unique_seats(self) -> Self:
        if len({score.seat for score in self.scores}) != len(self.scores):
            raise ValueError("duplicate seat in scores")
        return self


class HostLineData(ContractModel):
    """Offline commentary written after the night from the fact cards."""

    kind: Literal["lot", "recap", "prediction", "prediction_result"]
    text: Block
    numbers_checked: bool = False
    fallback: bool = False


class JudgeExtractData(ContractModel):
    """Raw extraction of one judge. Seat numbers come from events, never from the text."""

    judge: Annotated[int, Field(strict=True, ge=0, le=3)]
    vendor: Name
    model_returned: Name | None = None
    extractions: list[Extraction] = Field(default_factory=list)
    mission_guesses: list[MissionGuess] = Field(default_factory=list)
    injection_flags: list[InjectionFlag] = Field(default_factory=list)


class JudgeVerdictData(ContractModel):
    """A finding that survived the rules: program-decided or confirmed by both judges.

    For a false claim, ``topic`` is what the claim was about and ``gap_milli`` how far it was
    from the truth, in thousandths of the tier for a value and of the budget for a budget. For a
    collusion finding, ``statistic_milli`` holds the bid statistic of each screened lot in
    thousandths of the tier and ``threshold_milli`` the threshold it was compared with.
    """

    verdict: VerdictKind
    seats: list[Seat]
    lots: list[Lot] = Field(default_factory=list)
    evidence_seqs: list[SeqNo] = Field(default_factory=list)
    quotes: list[Line] = Field(default_factory=list)
    intent: Intent | None = None
    detail: Line = ""
    topic: ClaimTopic | None = None
    gap_milli: Count | None = None
    statistic_milli: dict[LotKey, SignedCoins] = Field(default_factory=dict)
    threshold_milli: Count | None = None


Basis = Literal["public", "private"]


class AwardData(ContractModel):
    """One award. A vacant award has no winners. Ties list every winner.

    ``basis`` says whether the award rests on public facts or on private values, which a viewer
    shows only with the god view.
    """

    award: AwardKey
    winners: list[Seat] = Field(default_factory=list)
    metric: SignedCoins | None = None
    metric_unit: MetricUnit = "none"
    vacant: bool = False
    basis: Basis | None = None

    @model_validator(mode="after")
    def _vacant_has_no_winner(self) -> Self:
        if self.vacant == bool(self.winners):
            raise ValueError("an award is vacant exactly when it has no winners")
        return self


class CostEventData(ContractModel):
    """Cost bookkeeping. ``cap_reached`` is the moment the cost gate fired."""

    kind: Literal["tally", "cap_reached"]
    spent_microusd: Count
    cap_microusd: Count | None = None
    calls: Count = 0
    role: Name | None = None


class ParseRepairData(ContractModel):
    """Repairs applied while turning a model reply into an action (one attempt).

    ``status`` says how the attempt ended: ``ok`` (an action was read), ``retry`` (another
    attempt follows) or ``abstain`` (the seat abstains).
    """

    role: Name
    seat: Seat | None = None
    phase: Name | None = None
    attempt: Annotated[int, Field(strict=True, ge=1, le=10)] = 1
    codes: list[RepairCode]
    status: RepairStatus | None = None
    fields: list[Name] = Field(default_factory=list)
    sanitized_chars: Count = 0
    detail: Line = ""
    call_id: Name | None = None


# --------------------------------------------------------------------- envelope classes

LotRule = Literal["required", "null", "either"]


def _type_is_required(schema: dict[str, Any]) -> None:
    """The discriminator has a default for convenience but is required on the wire."""
    required = schema.setdefault("required", [])
    if "type" not in required:
        required.append("type")


class EventBase(ContractModel):
    """Envelope shared by every event: ``seq``, ``lot``, ``type``, ``vis`` and ``data``."""

    model_config = ConfigDict(json_schema_extra=_type_is_required)

    lot_rule: ClassVar[LotRule] = "either"
    vis_kinds: ClassVar[frozenset[str]] = frozenset({"public"})

    seq: SeqNo
    lot: Lot | None = None
    vis: Vis

    @model_validator(mode="after")
    def _check_envelope(self) -> Self:
        kind = vis_kind(self.vis)
        if kind not in self.vis_kinds:
            raise ValueError(f"{type(self).__name__} cannot have visibility {self.vis!r}")
        owner = vis_seat(self.vis)
        data_seat = getattr(getattr(self, "data", None), "seat", None)
        if owner is not None and data_seat is not None and owner != data_seat:
            raise ValueError("vis seat does not match the seat in data")
        if self.lot_rule == "required" and self.lot is None:
            raise ValueError(f"{type(self).__name__} needs a lot")
        if self.lot_rule == "null" and self.lot is not None:
            raise ValueError(f"{type(self).__name__} has no lot")
        return self


class NightStartEvent(EventBase):
    """Opens the log. Public."""

    lot_rule: ClassVar[LotRule] = "null"
    type: Literal["night_start"] = "night_start"
    data: NightStartData


class SeedCommitEvent(EventBase):
    """Seed commitment before the night and reveal after it. Public."""

    lot_rule: ClassVar[LotRule] = "null"
    type: Literal["seed_commit"] = "seed_commit"
    data: SeedCommitData


class DealPrivateEvent(EventBase):
    """Private cards of one seat."""

    lot_rule: ClassVar[LotRule] = "null"
    vis_kinds: ClassVar[frozenset[str]] = frozenset({"seat"})
    type: Literal["deal_private"] = "deal_private"
    data: DealPrivateData


class PlanEvent(EventBase):
    """Opening plan of one seat."""

    lot_rule: ClassVar[LotRule] = "null"
    vis_kinds: ClassVar[frozenset[str]] = frozenset({"seat"})
    type: Literal["plan"] = "plan"
    data: PlanData


class LotOpenEvent(EventBase):
    """A lot opens. Public."""

    lot_rule: ClassVar[LotRule] = "required"
    type: Literal["lot_open"] = "lot_open"
    data: LotOpenData


class TalkEvent(EventBase):
    """A line in the public channel."""

    lot_rule: ClassVar[LotRule] = "required"
    type: Literal["talk"] = "talk"
    data: TalkData


class GuessEvent(EventBase):
    """A mind-reading guess, visible to its author only until the night ends."""

    lot_rule: ClassVar[LotRule] = "required"
    vis_kinds: ClassVar[frozenset[str]] = frozenset({"seat"})
    type: Literal["guess"] = "guess"
    data: GuessData


class ActionEvent(EventBase):
    """The decision of one seat in one phase, with its inner monologue."""

    vis_kinds: ClassVar[frozenset[str]] = frozenset({"seat"})
    type: Literal["action"] = "action"
    data: ActionData

    @model_validator(mode="after")
    def _lot_matches_phase(self) -> Self:
        if (self.data.phase == "plan") != (self.lot is None):
            raise ValueError("a plan action has no lot and every other action has one")
        return self


class EnglishDropoutEvent(EventBase):
    """A seat leaves the ascending clock. Public."""

    lot_rule: ClassVar[LotRule] = "required"
    type: Literal["english_dropout"] = "english_dropout"
    data: EnglishDropoutData


class DutchResultEvent(EventBase):
    """Outcome of the descending clock. Public."""

    lot_rule: ClassVar[LotRule] = "required"
    type: Literal["dutch_result"] = "dutch_result"
    data: DutchResultData


class HammerEvent(EventBase):
    """The lot is decided. Public."""

    lot_rule: ClassVar[LotRule] = "required"
    type: Literal["hammer"] = "hammer"
    data: HammerData


class MissionResultEvent(EventBase):
    """Mission outcome after the last lot. Public."""

    lot_rule: ClassVar[LotRule] = "null"
    type: Literal["mission_result"] = "mission_result"
    data: MissionResultData


class NightEndEvent(EventBase):
    """Closes the night with the final scores. Public."""

    lot_rule: ClassVar[LotRule] = "null"
    type: Literal["night_end"] = "night_end"
    data: NightEndData


class HostLineEvent(EventBase):
    """Offline commentary. Audience only."""

    vis_kinds: ClassVar[frozenset[str]] = frozenset({"audience"})
    type: Literal["host_line"] = "host_line"
    data: HostLineData

    @model_validator(mode="after")
    def _lot_matches_kind(self) -> Self:
        if (self.data.kind == "lot") != (self.lot is not None):
            raise ValueError("a per-lot host line has a lot and the others have none")
        return self


class JudgeExtractEvent(EventBase):
    """Raw output of one judge. Audience only."""

    lot_rule: ClassVar[LotRule] = "null"
    vis_kinds: ClassVar[frozenset[str]] = frozenset({"audience"})
    type: Literal["judge_extract"] = "judge_extract"
    data: JudgeExtractData


class JudgeVerdictEvent(EventBase):
    """A finding. Audience only."""

    vis_kinds: ClassVar[frozenset[str]] = frozenset({"audience"})
    type: Literal["judge_verdict"] = "judge_verdict"
    data: JudgeVerdictData


class AwardEvent(EventBase):
    """An award or a vacant award. Audience only."""

    lot_rule: ClassVar[LotRule] = "null"
    vis_kinds: ClassVar[frozenset[str]] = frozenset({"audience"})
    type: Literal["award"] = "award"
    data: AwardData


class CostEvent(EventBase):
    """Cost bookkeeping. System only."""

    vis_kinds: ClassVar[frozenset[str]] = frozenset({"system"})
    type: Literal["cost_event"] = "cost_event"
    data: CostEventData


class ParseRepairEvent(EventBase):
    """Repairs on one attempt. System only."""

    vis_kinds: ClassVar[frozenset[str]] = frozenset({"system"})
    type: Literal["parse_repair"] = "parse_repair"
    data: ParseRepairData


AnyEvent = (
    NightStartEvent
    | SeedCommitEvent
    | DealPrivateEvent
    | PlanEvent
    | LotOpenEvent
    | TalkEvent
    | GuessEvent
    | ActionEvent
    | EnglishDropoutEvent
    | DutchResultEvent
    | HammerEvent
    | MissionResultEvent
    | NightEndEvent
    | HostLineEvent
    | JudgeExtractEvent
    | JudgeVerdictEvent
    | AwardEvent
    | CostEvent
    | ParseRepairEvent
)
Event = Annotated[AnyEvent, Field(discriminator="type")]
EVENT_ADAPTER: Final[TypeAdapter[Any]] = TypeAdapter(
    Event, config=ConfigDict(hide_input_in_errors=True)
)

EVENT_CLASSES: Final[Mapping[str, type[EventBase]]] = {
    cls.model_fields["type"].default: cls for cls in get_args(AnyEvent)
}
EVENT_TYPES: Final[tuple[str, ...]] = tuple(EVENT_CLASSES)
ALLOWED_VIS: Final[Mapping[str, frozenset[str]]] = {
    name: cls.vis_kinds for name, cls in EVENT_CLASSES.items()
}


# ------------------------------------------------------------------------- log handling


def parse_event(source: str | bytes | Mapping[str, Any]) -> AnyEvent:
    """Validate one event from a JSON line or a plain mapping.

    The class is chosen by ``type`` so that errors name the concrete event class. Malformed
    JSON raises ``json.JSONDecodeError``; every other problem raises a ``ValidationError``.
    """
    raw = json.loads(source) if isinstance(source, str | bytes) else source
    if not isinstance(raw, Mapping):
        raise _event_error((), "an event is a JSON object")
    kind = raw.get("type")
    cls = EVENT_CLASSES.get(kind) if isinstance(kind, str) else None
    if cls is None:
        raise _event_error(("type",), f"unknown or missing event type {str(kind)[:40]!r}")
    return cls.model_validate(raw)


def _event_error(loc: tuple[str, ...], message: str) -> ValidationError:
    line = {"type": "value_error", "loc": loc, "input": None, "ctx": {"error": ValueError(message)}}
    return ValidationError.from_exception_data("Event", [line], hide_input=True)


def event_line(event: AnyEvent) -> str:
    """The canonical JSON line of one event, without a trailing newline."""
    return canonical_json(event)


def dumps_events(events: Iterable[AnyEvent]) -> str:
    """A whole log as JSON lines. Byte-stable for a given list of events."""
    return "".join(event_line(event) + "\n" for event in events)


def loads_events(text: str) -> list[AnyEvent]:
    """Parse JSON lines. Blank lines are skipped."""
    return [parse_event(line) for line in text.split("\n") if line.strip()]


def validate_log(events: Sequence[AnyEvent]) -> list[str]:
    """Log-level problems: seq gaps, a missing start, duplicate start or end."""
    problems: list[str] = []
    if not events:
        return ["empty log"]
    for index, event in enumerate(events, start=1):
        if event.seq != index:
            problems.append(f"event {index} has seq {event.seq}")
            break
    if events[0].type != "night_start":
        problems.append("the first event is not night_start")
    for kind in ("night_start", "night_end"):
        if sum(1 for event in events if event.type == kind) > 1:
            problems.append(f"more than one {kind}")
    return problems
