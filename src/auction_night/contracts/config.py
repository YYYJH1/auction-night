"""Night configuration contract: one YAML file describes one night.

The file has six sections: ``night`` (rules and economy), ``season`` (rotation context),
``seats``, ``endpoints`` (API access and per-model capabilities), ``show`` (offline host,
judges, content scan) and ``limits`` (timeouts, retries, cost cap). Secrets never appear in
a configuration: endpoints name the environment variables that hold them. The deal seed
follows the same rule. A committed season configuration names the variable that holds the
seed (``seed_env``) and carries its commitment, and an inline ``seed`` only appears in mock
and test configurations. ``NightSettings.resolve_seed`` returns the seed in either case.

The validators reject combinations the target APIs refuse, for example sampling parameters
on an endpoint whose capability record says they are unsupported.
"""

import os
import re
from collections import Counter
from collections.abc import Iterator, Mapping
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Final, Literal, Self, get_args

from pydantic import (
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from auction_night.contracts.events import (
    SEED_PATTERN,
    Coins,
    Count,
    Mode,
    Name,
    Seat,
    Sha256,
    TierKey,
    canonical_json,
    seed_commitment,
    sha256_digest,
)
from auction_night.contracts.mechanism import (
    FORMAT_KEYS,
    MAX_SEATS,
    ContractModel,
    FormatKey,
    MechanismDef,
    make_mechanism,
)

# ----------------------------------------------------------------------------- scalars


def _seed_to_text(value: Any) -> Any:
    if isinstance(value, bool):
        raise ValueError("a seed is text or an integer")
    return str(value) if isinstance(value, int) else value


Seed = Annotated[
    str,
    BeforeValidator(_seed_to_text),
    StringConstraints(min_length=1, max_length=128, pattern=SEED_PATTERN),
]
# A value written as <like-this> is a placeholder: it validates, and a real run must find none.
Placeholder = Annotated[str, StringConstraints(pattern=r"^<[^<>\x00-\x1f]{1,80}>$")]
Ident = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
EnvName = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")]
Url = Annotated[str, StringConstraints(pattern=r"^https?://[^\s@?#]+$")]
# A label is typed into prompts, so it carries no whitespace, control or structure symbols.
Label = Annotated[
    str,
    StringConstraints(
        min_length=1, max_length=16, pattern=r"^[^\s\x00-\x1f\x7f-\x9f【】「」\[\]〔〕\"“”<>{}]+$"
    ),
]

Effort = Literal["low", "medium", "high", "xhigh", "max"]
EFFORTS: Final[tuple[Effort, ...]] = get_args(Effort)
ThinkingType = Literal["adaptive", "disabled", "between_tools"]
EndpointKind = Literal["anthropic", "openai_compatible"]
ToolChoice = Literal["auto", "any", "tool"]
StructuredOutput = Literal["tools", "json_schema", "text"]
SeatKind = Literal["llm", "mock", "bot", "human"]
BotPolicy = Literal["truthful", "economist"]
MockPolicy = Literal["honest", "collude", "backstab", "random", "abstain"]
ShillFormat = Literal["english", "sealed_second"]
# What a model is shown of its own past besides the chat window: nothing, or its opening plan
# and the pledges that are still in force.
MemoryMode = Literal["off", "plan_pledges"]
MEMORY_MODES: Final[tuple[MemoryMode, ...]] = get_args(MemoryMode)


def _is_placeholder(text: str) -> bool:
    return text.startswith("<") and text.endswith(">")


class SeedResolutionError(ValueError):
    """The deal seed cannot be resolved. The message names the field, never the seed."""


def _few_decimals(value: float, places: int = 4) -> float:
    if round(value, places) != value:
        raise ValueError(f"use at most {places} decimal places")
    return value


# --------------------------------------------------------------------------- night rules


class BudgetConfig(ContractModel):
    """Budget of a seat: ``floor(c * u)`` with ``u`` uniform on ``spread``."""

    c: Annotated[int, Field(strict=True, gt=0, le=100000)] = 300
    spread: tuple[float, float] = (0.5, 1.5)
    rounding: Literal["floor"] = "floor"

    @model_validator(mode="after")
    def _spread_order(self) -> Self:
        low, high = self.spread
        if not 0 < low <= high:
            raise ValueError("spread must satisfy 0 < low <= high")
        _few_decimals(low)
        _few_decimals(high)
        return self


class DutchConfig(ContractModel):
    """Descending clock: ``levels`` price levels, one grab price per seat."""

    levels: Annotated[int, Field(strict=True, ge=2, le=100)] = 10
    mode: Literal["one_shot"] = "one_shot"


class EnglishConfig(ContractModel):
    """Ascending button clock. Revisions open when at most ``theta`` seats remain."""

    revise_when_remaining_le: Annotated[int, Field(strict=True, ge=2, le=MAX_SEATS)] = 4
    revise_timeout: Literal["keep_previous"] = "keep_previous"


class BlindConfig(ContractModel):
    """Common value prior and signal noise of the blind-box lots, as fractions of the tier."""

    value_range: tuple[float, float] = (0.5, 1.5)
    noise: float = Field(default=0.2, ge=0, le=1)

    @model_validator(mode="after")
    def _prior(self) -> Self:
        low, high = self.value_range
        if not 0 < low < high:
            raise ValueError("value_range must satisfy 0 < low < high")
        for number in (low, high, self.noise):
            _few_decimals(number)
        return self


class MissionConfig(ContractModel):
    """Secret missions. The shill threshold is a fraction of the tier, rounded up."""

    bonus: Coins = 150
    collectors: Count = 3
    shills: Count = 3
    collector_need: Annotated[int, Field(strict=True, ge=1, le=12)] = 2
    shill_threshold: float = Field(default=0.95, gt=0, le=1)
    shill_formats: tuple[ShillFormat, ...] = ("english", "sealed_second")

    @model_validator(mode="after")
    def _threshold_decimals(self) -> Self:
        _few_decimals(self.shill_threshold)
        if len(set(self.shill_formats)) != len(self.shill_formats) or not self.shill_formats:
            raise ValueError("shill_formats must be a non-empty list without repeats")
        return self


class ChatConfig(ContractModel):
    """Public channel limits."""

    max_chars: Annotated[int, Field(strict=True, ge=1, le=500)] = 60
    window_lots: Annotated[int, Field(strict=True, ge=1, le=12)] = 2
    order: Literal["rotate"] = "rotate"
    persona_max_chars: Annotated[int, Field(strict=True, ge=1, le=500)] = 60


class ThoughtConfig(ContractModel):
    """Inner monologue limits."""

    max_chars: Annotated[int, Field(strict=True, ge=1, le=1000)] = 100
    keep_last: Annotated[int, Field(strict=True, ge=0, le=12)] = 2


def _default_tiers() -> dict[int, int]:
    return {100: 5, 200: 4, 400: 3}


class NightSettings(ContractModel):
    """The ``night`` section: rules and economy of one night.

    The deal seed is the root of every random draw of the environment (lots, private cards,
    budgets, missions, tie-break keys). Give it in exactly one of two ways:

    * ``seed_env`` names the environment variable that holds the seed. This is the form of
      every committed season configuration, because the seed must stay private until the
      night is published.
    * ``seed`` holds the seed inline. Only mock and test configurations do this.

    ``deal_seed_commitment`` is the commitment published before the season
    (``seed_commitment(seed)``). Code that needs the seed calls ``resolve_seed``, which also
    checks the commitment. A value written as ``<like-this>`` stands in for a commitment that
    has not been published yet.

    ``memory`` is what the observation of a model seat holds besides the chat window and the
    seat's last monologues. ``off`` adds nothing. ``plan_pledges`` adds a section with the seat's
    own opening plan for the lot in progress, the part of that plan which is still to be spent,
    and every public pledge that is still in force.
    """

    model_config = ConfigDict(
        json_schema_extra={"oneOf": [{"required": ["seed"]}, {"required": ["seed_env"]}]}
    )

    id: Ident
    seed: Seed | None = None
    seed_env: EnvName | None = None
    deal_seed_commitment: Sha256 | Placeholder | None = None
    lang: Literal["zh"] = "zh"
    mode: Mode = "plain"
    formats: tuple[FormatKey, ...] = FORMAT_KEYS
    lots_per_format: Annotated[int, Field(strict=True, ge=1, le=6)] = 2
    reserve: Coins = 0
    tags: tuple[Label, ...] = ("海洋", "机械", "甜品", "怪谈")
    tiers: dict[TierKey, Count] = Field(default_factory=_default_tiers)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    dutch: DutchConfig = Field(default_factory=DutchConfig)
    english: EnglishConfig = Field(default_factory=EnglishConfig)
    blind: BlindConfig = Field(default_factory=BlindConfig)
    missions: MissionConfig = Field(default_factory=MissionConfig)
    plan_and_mindreading: bool = True
    memory: MemoryMode = "off"
    chat: ChatConfig = Field(default_factory=ChatConfig)
    thought: ThoughtConfig = Field(default_factory=ThoughtConfig)

    @property
    def n_lots(self) -> int:
        """Lots in the night: ``lots_per_format`` of every format in play."""
        return len(self.formats) * self.lots_per_format

    def mechanism_for(self, key: FormatKey) -> MechanismDef:
        """The mechanism of one format with this night's reserve, theta and levels applied."""
        return make_mechanism(
            key,
            reserve=0 if key == "all_pay" else self.reserve,
            theta=self.english.revise_when_remaining_le if key == "english" else None,
            levels=self.dutch.levels if key == "dutch" else None,
        )

    def resolve_seed(self, environ: Mapping[str, str] | None = None) -> str:
        """The deal seed, from the inline value or from the variable named by ``seed_env``.

        ``environ`` defaults to the process environment. Surrounding whitespace is removed
        from a value read from the environment. When ``deal_seed_commitment`` is present, the
        seed must open it, and a placeholder commitment is refused. Errors name the field and
        never contain the seed.
        """
        if self.seed is not None:
            seed, origin = self.seed, "night.seed"
        else:
            name = self.seed_env
            assert name is not None  # guaranteed by validation: exactly one of the two is set
            raw = (os.environ if environ is None else environ).get(name)
            if raw is None:
                raise SeedResolutionError(f"night.seed_env: environment variable {name} is not set")
            seed, origin = raw.strip(), f"the variable {name}"
            if not seed:
                raise SeedResolutionError(f"night.seed_env: environment variable {name} is empty")
            if len(seed) > 128 or not re.fullmatch(SEED_PATTERN, seed):
                raise SeedResolutionError(
                    f"night.seed_env: the value of {name} is not a valid seed "
                    "(1 to 128 characters from A-Z a-z 0-9 _ . : -)"
                )
        commitment = self.deal_seed_commitment
        if commitment is not None:
            if _is_placeholder(commitment):
                raise SeedResolutionError(
                    "night.deal_seed_commitment: still a placeholder, "
                    "replace it with the commitment published for the season"
                )
            if seed_commitment(seed) != commitment:
                raise SeedResolutionError(
                    f"night.deal_seed_commitment: does not match the seed in {origin}"
                )
        return seed

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        problems: list[str] = []
        n_lots = self.n_lots
        if (self.seed is None) == (self.seed_env is None):
            problems.append("give exactly one of seed and seed_env")
        if not self.formats or len(set(self.formats)) != len(self.formats):
            problems.append("formats must be a non-empty list without repeats")
        if sum(self.tiers.values()) != n_lots:
            problems.append(f"tier counts must add up to the {n_lots} lots of the night")
        if not self.tags or len(set(self.tags)) != len(self.tags):
            problems.append("tags must be a non-empty list without repeats")
        elif n_lots % len(self.tags):
            problems.append(f"{n_lots} lots cannot be spread evenly over {len(self.tags)} tags")
        elif self.missions.collectors and n_lots // len(self.tags) < self.missions.collector_need:
            problems.append("a tag has fewer lots than a collector must win")
        shill_formats = set(self.formats) & set(self.missions.shill_formats)
        shill_lots = self.lots_per_format * len(shill_formats)
        if self.missions.shills > shill_lots:
            problems.append(f"{self.missions.shills} shills but only {shill_lots} eligible lots")
        if "dutch" in self.formats and any(tier % self.dutch.levels for tier in self.tiers):
            problems.append("every tier must be divisible by dutch.levels so that prices are whole")
        if self.tiers and self.reserve > min(self.tiers):
            problems.append("reserve exceeds the smallest tier")
        commitment = self.deal_seed_commitment
        if (
            self.seed is not None
            and commitment is not None
            and not _is_placeholder(commitment)
            and seed_commitment(self.seed) != commitment
        ):
            problems.append("deal_seed_commitment is not the commitment of seed")
        if problems:
            raise ValueError("; ".join(problems))
        return self


# ------------------------------------------------------------------------------ endpoints


class SamplingParams(ContractModel):
    """Sampling parameters. Unsupported endpoints must carry none of them."""

    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, gt=0, le=1)
    top_k: int | None = Field(default=None, ge=1)
    seed: int | None = Field(default=None, ge=0)

    @property
    def is_empty(self) -> bool:
        """True when no parameter is set."""
        return all(value is None for value in self.model_dump().values())


class ThinkingConfig(ContractModel):
    """Thinking control sent with a request. ``adaptive`` means the field is left out."""

    type: ThinkingType


class Capabilities(ContractModel):
    """What an endpoint (or one model on it) accepts. Unknown means unsupported."""

    supports_sampling_params: bool = False
    supports_forced_tool_choice: bool = False
    supports_strict_tools: bool = False
    thinking_modes: tuple[ThinkingType, ...] = ()
    efforts: tuple[Effort, ...] = ()
    explicit_thinking_max_effort: Effort | None = None


def default_capabilities(kind: EndpointKind) -> Capabilities:
    """Conservative defaults when a configuration gives no capability record."""
    if kind == "anthropic":
        return Capabilities(
            supports_strict_tools=True,
            thinking_modes=("adaptive",),
            efforts=EFFORTS,
            explicit_thinking_max_effort="high",
        )
    return Capabilities(supports_sampling_params=True)


class EndpointConfig(ContractModel):
    """API access for one provider, plus what its models accept.

    ``model_capabilities`` maps a model id to a complete capability record that replaces
    ``capabilities`` for that model.
    """

    kind: EndpointKind
    base_url: Url | None = None
    base_url_env: EnvName | None = None
    api_key_env: EnvName | None = None
    api_version: Name | None = None
    tool_choice: ToolChoice = "auto"
    strict_tools: bool = False
    structured_output: StructuredOutput = "tools"
    sampling: SamplingParams | None = None
    capabilities: Capabilities | None = None
    model_capabilities: dict[Name, Capabilities] = Field(default_factory=dict)
    max_concurrency: Annotated[int, Field(strict=True, ge=1, le=64)] = 4
    requests_per_minute: Annotated[int, Field(strict=True, ge=1)] | None = None

    def capabilities_for(self, model: str | None) -> Capabilities:
        """The capability record that applies to one model of this endpoint."""
        if model is not None and model in self.model_capabilities:
            return self.model_capabilities[model]
        return self.capabilities or default_capabilities(self.kind)

    @model_validator(mode="after")
    def _access(self) -> Self:
        if self.base_url is not None and self.base_url_env is not None:
            raise ValueError("give base_url or base_url_env, not both")
        if self.kind == "openai_compatible" and self.base_url is None and self.base_url_env is None:
            raise ValueError("an openai_compatible endpoint needs base_url or base_url_env")
        if self.kind == "anthropic" and self.api_key_env is None:
            raise ValueError("an anthropic endpoint needs api_key_env")
        return self


# ---------------------------------------------------------------------------- seats, roles


class ModelFields(ContractModel):
    """Model access and request parameters shared by seats and show roles."""

    endpoint: Ident | None = None
    model: Name | None = None
    vendor: Name | None = None
    thinking: ThinkingConfig | None = None
    effort: Effort | None = None
    max_tokens: Annotated[int, Field(strict=True, ge=64, le=131072)] | None = None
    sampling: SamplingParams | None = None

    def _has_model(self) -> bool:
        return any(
            value is not None
            for value in (
                self.endpoint,
                self.model,
                self.vendor,
                self.thinking,
                self.effort,
                self.max_tokens,
                self.sampling,
            )
        )

    def _check_model_fields(self, runs_llm: bool, label: str) -> None:
        if runs_llm:
            if self.endpoint is None or self.model is None or self.vendor is None:
                raise ValueError(f"{label}: endpoint, model and vendor are required")
        elif self._has_model():
            raise ValueError(f"{label}: model fields are only allowed on an llm role")


class SeatConfig(ModelFields):
    """One seat. ``llm`` seats call a model, ``mock`` seats run a scripted policy."""

    seat: Seat
    codename: Label
    kind: SeatKind
    policy: BotPolicy | None = None
    pacing: bool = False
    mock_policy: MockPolicy | None = None
    persona: Name | None = None

    @model_validator(mode="after")
    def _fields_match_kind(self) -> Self:
        label = f"seat {self.seat}"
        self._check_model_fields(self.kind == "llm", label)
        if (self.kind == "bot") != (self.policy is not None):
            raise ValueError(f"{label}: policy is required for bots and only for bots")
        if (self.kind == "mock") != (self.mock_policy is not None):
            raise ValueError(f"{label}: mock_policy is required for mock seats and only for them")
        if self.pacing and self.policy != "economist":
            raise ValueError(f"{label}: pacing only applies to the economist bot")
        if self.persona is not None and self.kind not in ("llm", "mock"):
            raise ValueError(f"{label}: only model seats carry a persona")
        return self


class RoleConfig(ModelFields):
    """A model used outside the seats: host, judge or classifier."""

    kind: Literal["llm", "mock"] = "llm"

    @model_validator(mode="after")
    def _fields_match_kind(self) -> Self:
        self._check_model_fields(self.kind == "llm", "show role")
        return self


class ContentScanConfig(ContractModel):
    """Scan of every public text before a replay is published."""

    wordlist: Name | None = None
    classifier: RoleConfig | None = None


class ShowConfig(ContractModel):
    """Offline host, two judges and the content scan."""

    host_offline: RoleConfig
    judges: list[RoleConfig]
    content_scan: ContentScanConfig = Field(default_factory=ContentScanConfig)
    allow_judge_vendor_overlap: bool = False

    @model_validator(mode="after")
    def _two_judges(self) -> Self:
        if len(self.judges) != 2:
            raise ValueError("exactly two judges are required")
        return self


class LimitsConfig(ContractModel):
    """Timeouts, retries and the cost cap."""

    call_timeout_s: Annotated[int, Field(strict=True, ge=1, le=3600)] = 90
    human_timeout_s: Annotated[int, Field(strict=True, ge=1)] | None = None
    syntax_retries: Annotated[int, Field(strict=True, ge=0, le=3)] = 1
    transient_retries: Annotated[int, Field(strict=True, ge=0, le=5)] = 2
    max_cost_per_night_usd: float | None = Field(default=None, ge=0)
    prices_file: Name | None = None

    @property
    def max_cost_microusd(self) -> int | None:
        """The cost cap in integer micro-dollars, or None when only accounting is wanted."""
        cap = self.max_cost_per_night_usd
        return None if cap is None else round(cap * 1_000_000)

    @model_validator(mode="after")
    def _cap_precision(self) -> Self:
        if self.max_cost_per_night_usd is not None:
            _few_decimals(self.max_cost_per_night_usd, 6)
        return self


class DeckRef(ContractModel):
    """A planned deck of a season, with the seed commitment published before the season."""

    id: Ident
    seed_commitment: Sha256 | None = None


class SeasonConfig(ContractModel):
    """Season context of this night."""

    id: Ident | None = None
    format: Literal["single", "rotation"] = "single"
    decks: list[DeckRef] = Field(default_factory=list)
    deck: Ident | None = None
    rotation: Annotated[int, Field(strict=True, ge=0, le=MAX_SEATS - 1)] | None = None
    max_seats_per_vendor: Annotated[int, Field(strict=True, ge=1, le=MAX_SEATS)] = 2

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        if self.format == "single" and self.rotation is not None:
            raise ValueError("rotation only applies to a rotation season")
        if self.deck is not None and self.decks and self.deck not in {d.id for d in self.decks}:
            raise ValueError(f"deck {self.deck!r} is not listed in decks")
        return self


# ------------------------------------------------------------------------------ the root


def _llm_roles(config: "NightConfig") -> Iterator[tuple[str, RoleConfig]]:
    show = config.show
    yield "show.host_offline", show.host_offline
    for index, judge in enumerate(show.judges):
        yield f"show.judges[{index}]", judge
    if show.content_scan.classifier is not None:
        yield "show.content_scan.classifier", show.content_scan.classifier


class NightConfig(ContractModel):
    """A complete night configuration."""

    night: NightSettings
    season: SeasonConfig = Field(default_factory=SeasonConfig)
    seats: list[SeatConfig]
    endpoints: dict[Ident, EndpointConfig] = Field(default_factory=dict)
    show: ShowConfig
    limits: LimitsConfig = Field(default_factory=LimitsConfig)

    @property
    def model_seats(self) -> list[SeatConfig]:
        """Seats that are played by a model, a scripted mock or a human."""
        return [seat for seat in self.seats if seat.kind != "bot"]

    @property
    def has_human(self) -> bool:
        """True when a human takes a seat. Such nights are not ranked."""
        return any(seat.kind == "human" for seat in self.seats)

    @property
    def is_offline(self) -> bool:
        """True when no model API is called: only mocks, bots and mock show roles."""
        roles_offline = all(role.kind == "mock" for _, role in _llm_roles(self))
        return roles_offline and all(seat.kind != "llm" for seat in self.seats)

    @model_validator(mode="after")
    def _cross_checks(self) -> Self:
        problems: list[str] = []
        problems += self._seat_problems()
        problems += self._mission_problems()
        problems += self._endpoint_problems()
        problems += self._vendor_problems()
        problems += self._season_problems()
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def _seat_problems(self) -> list[str]:
        problems = []
        numbers = sorted(seat.seat for seat in self.seats)
        if not 2 <= len(numbers) <= MAX_SEATS or numbers != list(range(1, len(numbers) + 1)):
            problems.append(f"seats must be numbered 1..N with 2 <= N <= {MAX_SEATS}")
        names = [seat.codename for seat in self.seats]
        if len(set(names)) != len(names):
            problems.append("code names must be unique")
        if sum(seat.kind == "human" for seat in self.seats) > 1:
            problems.append("at most one human seat")
        for seat in self.seats:
            if seat.kind in ("llm", "mock"):
                if self.night.mode == "plain" and seat.persona is not None:
                    problems.append(f"seat {seat.seat}: plain mode has no personas")
                if self.night.mode == "variety" and seat.kind == "llm" and seat.persona is None:
                    problems.append(f"seat {seat.seat}: variety mode needs a persona")
        return problems

    def _mission_problems(self) -> list[str]:
        holders = len(self.model_seats)
        wanted = self.night.missions.collectors + self.night.missions.shills
        if wanted > holders:
            return [f"{wanted} missions but only {holders} model seats"]
        return []

    def _endpoint_problems(self) -> list[str]:
        problems: list[str] = []
        for seat in self.seats:
            if seat.kind == "llm":
                problems += self._model_problems(f"seat {seat.seat}", seat)
        for label, role in _llm_roles(self):
            if role.kind == "llm":
                problems += self._model_problems(label, role)
        return list(dict.fromkeys(problems))

    def _model_problems(self, label: str, ref: ModelFields) -> list[str]:
        assert ref.endpoint is not None and ref.model is not None
        endpoint = self.endpoints.get(ref.endpoint)
        if endpoint is None:
            return [f"{label}: unknown endpoint {ref.endpoint!r}"]
        caps = endpoint.capabilities_for(ref.model)
        where = f"{label} ({ref.endpoint}/{ref.model})"
        problems = []
        for origin, sampling in (("seat", ref.sampling), ("endpoint", endpoint.sampling)):
            if sampling is not None and not sampling.is_empty and not caps.supports_sampling_params:
                problems.append(f"{where}: sampling parameters are not supported ({origin})")
        if endpoint.kind == "anthropic" and ref.max_tokens is None:
            problems.append(f"{where}: max_tokens is required")
        problems += self._thinking_problems(where, ref, caps)
        if endpoint.tool_choice != "auto" and not caps.supports_forced_tool_choice:
            problems.append(f"{where}: forced tool_choice is not supported")
        if endpoint.strict_tools and not caps.supports_strict_tools:
            problems.append(f"{where}: strict tools are not supported")
        return problems

    @staticmethod
    def _thinking_problems(where: str, ref: ModelFields, caps: Capabilities) -> list[str]:
        problems = []
        if not caps.thinking_modes:
            if ref.thinking is not None:
                problems.append(f"{where}: thinking cannot be controlled on this model")
        elif ref.thinking is None:
            problems.append(f"{where}: thinking must be pinned explicitly")
        elif ref.thinking.type not in caps.thinking_modes:
            problems.append(f"{where}: thinking type {ref.thinking.type!r} is not supported")
        elif ref.thinking.type != "adaptive" and caps.explicit_thinking_max_effort is not None:
            limit = EFFORTS.index(caps.explicit_thinking_max_effort)
            if ref.effort is None or EFFORTS.index(ref.effort) > limit:
                problems.append(
                    f"{where}: thinking {ref.thinking.type!r} needs effort up to "
                    f"{caps.explicit_thinking_max_effort!r}"
                )
        if not caps.efforts:
            if ref.effort is not None:
                problems.append(f"{where}: effort cannot be set on this model")
        elif ref.effort is None:
            problems.append(f"{where}: effort must be pinned explicitly")
        elif ref.effort not in caps.efforts:
            problems.append(f"{where}: effort {ref.effort!r} is not supported")
        return problems

    def _vendor_problems(self) -> list[str]:
        problems = []
        lineup = Counter(seat.vendor for seat in self.seats if seat.kind == "llm")
        limit = self.season.max_seats_per_vendor
        problems += [
            f"vendor {vendor!r} has {count} seats, limit {limit}"
            for vendor, count in lineup.items()
            if count > limit
        ]
        host = self.show.host_offline
        if host.kind == "llm" and host.vendor in lineup:
            problems.append("the offline host must not share a vendor with a seat")
        classifier = self.show.content_scan.classifier
        if classifier is not None and classifier.kind == "llm" and classifier.vendor in lineup:
            problems.append("the content classifier must not share a vendor with a seat")
        judge_vendors = [j.vendor for j in self.show.judges if j.kind == "llm"]
        if len(set(judge_vendors)) != len(judge_vendors):
            problems.append("the two judges must come from different vendors")
        if not self.show.allow_judge_vendor_overlap and set(judge_vendors) & set(lineup):
            problems.append("a judge shares a vendor with a seat (allow_judge_vendor_overlap)")
        return problems

    def _season_problems(self) -> list[str]:
        if self.season.format == "rotation" and self.season.rotation is None:
            return ["a rotation season needs the rotation index of this night"]
        return []


# ---------------------------------------------------------------------------------- prices


Currency = Literal["USD", "CNY"]


class ModelPrice(ContractModel):
    """Price of one model per million tokens, in ``currency`` (USD unless stated), with date and
    source. A price in another currency is counted in dollars with the rate of the price list."""

    input_per_mtok: float = Field(ge=0)
    output_per_mtok: float = Field(ge=0)
    cache_read_per_mtok: float | None = Field(default=None, ge=0)
    cache_write_per_mtok: float | None = Field(default=None, ge=0)
    currency: Currency = "USD"
    as_of: date
    source: str = Field(pattern=r"^https?://\S+$")
    note: str | None = None


class ExchangeRate(ContractModel):
    """What one US dollar buys of a currency on a date: ``units_per_usd`` units of it.

    The rate is written the way a rate source quotes it (``1 USD = 6.6529 CNY``), so it is the
    number of the source and not its inverse.
    """

    units_per_usd: float = Field(gt=0, allow_inf_nan=False)
    as_of: date
    source: str = Field(pattern=r"^https?://\S+$")
    note: str | None = None


class PricesFile(ContractModel):
    """Price list read by the cost gate. A missing cache price means the input price.

    ``exchange_rates`` holds the rate of each currency other than USD. A price in such a currency
    is divided by its rate when the cost of a call is worked out, so ledgers and the cost cap stay
    in dollars. A price list that uses a currency without giving its rate is refused: no rate is
    assumed. A rate may also be listed for a list in dollars, to show amounts in that currency.
    """

    version: Literal[1] = 1
    prices: dict[Name, ModelPrice]
    exchange_rates: dict[Currency, ExchangeRate] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _rates_cover_the_prices(self) -> Self:
        problems = []
        if "USD" in self.exchange_rates:
            problems.append("exchange_rates: a dollar is a dollar, USD takes no rate")
        used = {price.currency for price in self.prices.values()} - {"USD"}
        missing = sorted(used - set(self.exchange_rates))
        if missing:
            problems.append(
                f"prices in {', '.join(missing)} need an entry in exchange_rates "
                "(no rate is assumed)"
            )
        if problems:
            raise ValueError("; ".join(problems))
        return self


# --------------------------------------------------------------------------------- helpers


# Settings that were added after the contracts were frozen, as (section, name, default). The
# manifest of a replay carries the ``config_hash`` of the configuration it was made with, and
# ``seeds verify`` compares it with the hash of the ``config.yaml`` beside the replay. A setting
# that holds its default is therefore left out of the digest, so that a configuration written
# before the setting existed keeps the digest it was published with.
_ADDED_AFTER_FREEZE: Final[tuple[tuple[str, str, Any], ...]] = (("night", "memory", "off"),)


def config_hash(config: NightConfig) -> str:
    """Digest of the canonical JSON of a configuration. It enters the run manifest.

    A setting added after the freeze is part of the digest only once it differs from its default.
    """
    data = config.model_dump(mode="json", by_alias=True)
    for section, name, default in _ADDED_AFTER_FREEZE:
        if data[section].get(name) == default:
            del data[section][name]
    return sha256_digest(canonical_json(data, allow_float=True))


def find_placeholders(config: NightConfig) -> list[str]:
    """Paths of every string written as ``<placeholder>``. A real run must find none.

    This covers model ids, the commitment of the deal seed and any other placeholder.
    """
    found: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")
        elif isinstance(node, str) and _is_placeholder(node):
            found.append(path)

    walk(config.model_dump(mode="json"), "")
    return found


def _load_yaml(path: Path) -> Any:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError(
            "PyYAML is required to read configuration files: add pyyaml>=6 to the dependencies"
        ) from exc

    class StrictLoader(yaml.SafeLoader):
        """Safe loader that rejects duplicate mapping keys."""

    merge_tag = "tag:yaml.org,2002:merge"

    def construct_mapping(loader: Any, node: Any, deep: bool = False) -> Any:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            if key_node.tag == merge_tag:
                continue  # an explicit key may legitimately override a merged one
            key = loader.construct_object(key_node, deep=True)
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r}", key_node.start_mark
                )
            seen.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep)

    tag = yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG
    StrictLoader.add_constructor(tag, construct_mapping)
    with path.open(encoding="utf-8") as handle:
        return yaml.load(handle, Loader=StrictLoader)  # noqa: S506 - safe subclass


def load_config(path: str | Path) -> NightConfig:
    """Read and validate a night configuration from YAML."""
    return NightConfig.model_validate(_load_yaml(Path(path)))


def load_prices(path: str | Path) -> PricesFile:
    """Read and validate a price list from YAML."""
    return PricesFile.model_validate(_load_yaml(Path(path)))
