"""Mechanism definitions: the single source of truth for the six auction formats.

This module is data only. Settlement, rule-text rendering and payment examples are
generated elsewhere from these definitions and are never written by hand next to them.
A mechanism object answers four questions: how the price is set, what clock drives the
lot, whether values are private or common, and which parameters (reserve, levels, theta)
the format takes.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Structural limits shared by every contract module.
MAX_SEATS: Final = 8
MAX_LOTS: Final = 36

FormatKey = Literal["english", "dutch", "sealed_first", "sealed_second", "all_pay", "blind_box"]
FORMAT_KEYS: Final[tuple[FormatKey, ...]] = get_args(FormatKey)

Pricing = Literal["first", "second", "all_pay"]
Clock = Literal["none", "ascending_button", "descending_levels"]
ValueModel = Literal["private", "common"]

# Protocol phases of the single `act` tool. A lot uses `talk` plus the bid phases of its
# format. `plan` happens once per night, before the first lot.
BidPhase = Literal["english_init", "english_revise", "dutch", "sealed"]
Phase = Literal["plan", "talk", "english_init", "english_revise", "dutch", "sealed"]
PHASES: Final[tuple[Phase, ...]] = get_args(Phase)
BID_PHASES: Final[tuple[BidPhase, ...]] = get_args(BidPhase)

BID_PHASES_BY_CLOCK: Final[Mapping[Clock, tuple[BidPhase, ...]]] = MappingProxyType(
    {
        "none": ("sealed",),
        "ascending_button": ("english_init", "english_revise"),
        "descending_levels": ("dutch",),
    }
)

# Meaning of every slot a payment example may use. The settlement function fills the slots
# from the fixed example inputs of the format, so no number in a rule text is typed by hand.
EXAMPLE_SLOTS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "bids": "The fixed example bids, highest first.",
        "winner_bid": "The highest example bid.",
        "price": "What the winner pays in the example.",
        "payments": "What every bidder pays in the example, in bid order (all-pay only).",
        "tier": "The example tier (Dutch only).",
        "grab_price": "The example grab price (Dutch only).",
        "ladder_head": "The first price levels down to the level where the grab fires (Dutch).",
    }
)

COMMON_RULE_KEY: Final = "mechanism.common.rule"


class ContractModel(BaseModel):
    """Base of every contract model: immutable, unknown fields are errors, and
    validation errors never echo the offending input (it may be a pasted secret)."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, protected_namespaces=(), hide_input_in_errors=True
    )


class ExampleSpec(ContractModel):
    """Fixed inputs and output slots of the payment example in a rule text."""

    slots: tuple[str, ...]
    bids: tuple[int, ...] | None = None
    tier: int | None = None
    grab_price: int | None = None

    @model_validator(mode="after")
    def _known_slots(self) -> "ExampleSpec":
        unknown = [s for s in self.slots if s not in EXAMPLE_SLOTS]
        if unknown:
            raise ValueError(f"unknown example slots: {unknown}")
        return self


class TextKeys(ContractModel):
    """Keys of the zh text templates that the text layer fills for one format."""

    rule: str
    example: str
    card: str
    glossary: str


class MechanismDef(ContractModel):
    """One auction format, fully described by data."""

    key: FormatKey
    version: str = Field(default="v1", pattern=r"^v[0-9]+$")
    title_zh: str
    pricing: Pricing
    clock: Clock
    value_model: ValueModel
    reserve: int = Field(default=0, ge=0)
    reserve_allowed: bool = True
    levels: int | None = Field(default=None, ge=2)
    theta: int | None = Field(default=None, ge=2, le=MAX_SEATS)
    text_keys: TextKeys
    example: ExampleSpec

    @property
    def ref(self) -> str:
        """Versioned reference used in events, for example ``english@v1``."""
        return f"{self.key}@{self.version}"

    @property
    def bid_phases(self) -> tuple[BidPhase, ...]:
        """Bid phases a model seat is asked to act in for one lot of this format."""
        return BID_PHASES_BY_CLOCK[self.clock]

    @model_validator(mode="after")
    def _coherent(self) -> "MechanismDef":
        if self.clock == "ascending_button":
            if self.theta is None or self.levels is not None:
                raise ValueError("an ascending button clock takes theta and no levels")
        elif self.clock == "descending_levels":
            if self.levels is None or self.theta is not None:
                raise ValueError("a descending levels clock takes levels and no theta")
        elif self.theta is not None or self.levels is not None:
            raise ValueError("a format without a clock takes neither theta nor levels")
        if not self.reserve_allowed and self.reserve != 0:
            raise ValueError(f"format {self.key} does not allow a reserve price")
        if self.value_model == "common" and self.pricing != "second":
            raise ValueError("the common value format prices like a second price auction")
        return self


def _text_keys(key: str) -> TextKeys:
    return TextKeys(
        rule=f"mechanism.{key}.rule",
        example=f"mechanism.{key}.example",
        card=f"mechanism.{key}.card",
        glossary=f"mechanism.{key}.glossary",
    )


_BIDS_EXAMPLE = ExampleSpec(slots=("bids", "winner_bid", "price"), bids=(120, 90, 40))

MECHANISMS: Final[Mapping[FormatKey, MechanismDef]] = MappingProxyType(
    {
        "english": MechanismDef(
            key="english",
            title_zh="英式加价",
            pricing="second",
            clock="ascending_button",
            value_model="private",
            theta=4,
            text_keys=_text_keys("english"),
            example=_BIDS_EXAMPLE,
        ),
        "dutch": MechanismDef(
            key="dutch",
            title_zh="荷式降价",
            pricing="first",
            clock="descending_levels",
            value_model="private",
            levels=10,
            text_keys=_text_keys("dutch"),
            example=ExampleSpec(
                slots=("tier", "grab_price", "ladder_head", "price"), tier=200, grab_price=130
            ),
        ),
        "sealed_first": MechanismDef(
            key="sealed_first",
            title_zh="密封一价",
            pricing="first",
            clock="none",
            value_model="private",
            text_keys=_text_keys("sealed_first"),
            example=_BIDS_EXAMPLE,
        ),
        "sealed_second": MechanismDef(
            key="sealed_second",
            title_zh="密封二价",
            pricing="second",
            clock="none",
            value_model="private",
            text_keys=_text_keys("sealed_second"),
            example=_BIDS_EXAMPLE,
        ),
        "all_pay": MechanismDef(
            key="all_pay",
            title_zh="全员付费",
            pricing="all_pay",
            clock="none",
            value_model="private",
            reserve_allowed=False,
            text_keys=_text_keys("all_pay"),
            example=ExampleSpec(slots=("bids", "winner_bid", "payments"), bids=(120, 90, 40)),
        ),
        "blind_box": MechanismDef(
            key="blind_box",
            title_zh="盲盒",
            pricing="second",
            clock="none",
            value_model="common",
            text_keys=_text_keys("blind_box"),
            example=_BIDS_EXAMPLE,
        ),
    }
)


def get_mechanism(key: FormatKey) -> MechanismDef:
    """The default definition of one format."""
    return MECHANISMS[key]


def make_mechanism(
    key: FormatKey, *, reserve: int = 0, theta: int | None = None, levels: int | None = None
) -> MechanismDef:
    """The definition of one format with night-level parameters applied and re-validated."""
    update: dict[str, int] = {"reserve": reserve}
    if theta is not None:
        update["theta"] = theta
    if levels is not None:
        update["levels"] = levels
    return MechanismDef.model_validate({**MECHANISMS[key].model_dump(), **update})
