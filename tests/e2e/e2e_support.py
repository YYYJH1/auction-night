"""Helpers of the end-to-end tests: the mock night in its variants, and small readers of its log.

The night is ``configs/night_mock.yaml``: eight seats, twelve lots, all six formats, six scripted
mock seats and two rule bots. It is played through the whole model path (prompt, request, mock
reply, parsing ladder, cassette) with no network. A night takes under a second.
"""

import copy
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from auction_night.contracts import AnyEvent, NightConfig
from auction_night.contracts.config import ModelPrice
from auction_night.engine.deal import deal
from auction_night.llm.accounting import PriceTable
from auction_night.llm.cassette import Cassette, CassetteEntry
from auction_night.llm.driver import NightResult
from auction_night.llm.mock import Fault, MockOptions
from auction_night.llm.nightrun import play_night

REPO = Path(__file__).resolve().parents[2]
MOCK_CONFIG = REPO / "configs" / "night_mock.yaml"
MODEL_SEATS = (1, 2, 3, 4, 5, 6)
BOT_SEATS = (7, 8)
POLICIES = ("honest", "collude", "backstab", "random", "abstain")


def raw_config() -> dict[str, Any]:
    """The mock configuration as plain data, ready to be changed and validated."""
    with MOCK_CONFIG.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def make_config(
    *,
    policies: dict[int, str] | None = None,
    night: dict[str, Any] | None = None,
    limits: dict[str, Any] | None = None,
) -> NightConfig:
    """The mock night with some seats given another policy, and some settings replaced."""
    raw = raw_config()
    for seat in raw["seats"]:
        if policies and seat["seat"] in policies:
            seat["mock_policy"] = policies[seat["seat"]]
    raw["night"].update(copy.deepcopy(night or {}))
    raw["limits"].update(copy.deepcopy(limits or {}))
    return NightConfig.model_validate(raw)


def mock_prices(per_mtok: float = 1.0) -> PriceTable:
    """Every token of every mock model costs ``per_mtok`` micro-dollars."""
    price = ModelPrice(
        input_per_mtok=per_mtok,
        output_per_mtok=per_mtok,
        cache_read_per_mtok=per_mtok,
        cache_write_per_mtok=per_mtok,
        as_of=date(2026, 11, 1),
        source="https://example.com/prices",
    )
    return PriceTable({f"mock-{policy}": price for policy in POLICIES}, source="test prices")


@dataclass
class Played:
    """A night that was played, with the cassette it recorded."""

    result: NightResult
    cassette: Cassette

    @property
    def events(self) -> tuple[AnyEvent, ...]:
        return self.result.events

    def of_type(self, kind: str) -> list[Any]:
        return [e for e in self.result.events if e.type == kind]

    def actions(
        self, seat: int | None = None, phase: str | None = None, lot: int | None = None
    ) -> list[Any]:
        """The action events that match, in log order."""
        return [
            e
            for e in self.of_type("action")
            if (seat is None or e.data.seat == seat)
            and (phase is None or e.data.phase == phase)
            and (lot is None or e.lot == lot)
        ]

    def repairs(self, seat: int | None = None, phase: str | None = None) -> list[Any]:
        return [
            e
            for e in self.of_type("parse_repair")
            if (seat is None or e.data.seat == seat) and (phase is None or e.data.phase == phase)
        ]

    def entries(
        self, seat: int | None = None, phase: str | None = None, lot: int | None = None
    ) -> list[CassetteEntry]:
        """The recorded attempts that match, in key order."""
        return [
            e
            for e in self.cassette.entries
            if (seat is None or e.key.seat == seat)
            and (phase is None or e.key.phase == phase)
            and (lot is None or e.key.lot == lot)
        ]


def play(
    config: NightConfig | None = None,
    *,
    faults: tuple[Fault, ...] = (),
    kind: str = "anthropic",
    cassette: Cassette | None = None,
    **rig: Any,
) -> Played:
    """Play a mock night and keep its cassette in memory (or in the one that is passed in)."""
    config = config or make_config()
    cassette = cassette or Cassette.record()
    options = MockOptions(faults=faults, kind=kind)  # type: ignore[arg-type]
    result = play_night(config, cassette, mock=options, **rig)
    return Played(result, cassette)


def lots_of(config: NightConfig, format_key: str) -> list[int]:
    """The lots of one format in the deal of a configuration."""
    return [lot.lot for lot in deal(config).lots if lot.format == format_key]


# ------------------------------------------------------------------------- unique markers


def canary_config() -> NightConfig:
    """A mock night of ten lots without an all-pay lot, in which nobody runs out of money.

    The numbers of the marked deal are far above the tiers, and an all-pay lot would make the
    textbook bidders pay their whole budget for it. The random seat is made honest, so that
    every bid and exit price of a seat is one of its markers. Everything else is the mock night.
    """
    return make_config(
        policies={6: "honest"},
        night={
            "formats": ["english", "dutch", "sealed_first", "sealed_second", "blind_box"],
            "tags": ["海洋", "机械", "甜品", "怪谈", "星空"],
            "tiers": {100: 4, 200: 3, 400: 3},
        },
    )


@dataclass(frozen=True)
class Marks:
    """The unique markers of a marked deal, by owner: budgets, values, appraisals, canaries and
    the hidden values of the blind boxes."""

    budgets: dict[int, int]
    private: dict[int, set[int]]  # every private number of a seat, budget included
    canaries: dict[int, str]
    blind_values: dict[int, int]  # lot -> hidden value

    def numbers(self) -> set[int]:
        found = set(self.blind_values.values())
        for numbers in self.private.values():
            found |= numbers
        return found


def marked_deal(config: NightConfig) -> tuple[Any, Marks]:
    """The deal of ``config`` with every private number replaced by a unique, searchable one.

    Budgets are eight-digit numbers, values and appraisals six-digit numbers that differ for
    every seat and lot, the hidden value of a blind box is another six-digit number, and the
    canary of a mission is a word that names its seat. Nothing else of the deal changes.
    """
    import dataclasses

    base = deal(config)
    seats = range(1, base.n_seats + 1)
    budgets = {seat: 40_000_000 + 100_000 * seat + 11 for seat in seats}
    private = {seat: {budgets[seat]} for seat in seats}
    blind_values: dict[int, int] = {}
    lots = []
    for lot in base.lots:
        values, signals = {}, {}
        if lot.is_blind:
            hidden = 500_000 + 100 * lot.lot + 7
            blind_values[lot.lot] = hidden
            values = dict.fromkeys(seats, hidden)
            for seat in seats:
                signals[seat] = 100_000 + 10_000 * seat + 100 * lot.lot + 3
                private[seat].add(signals[seat])
        else:
            for seat in seats:
                values[seat] = 100_000 + 10_000 * seat + 100 * lot.lot + 1
                private[seat].add(values[seat])
        lots.append(
            dataclasses.replace(
                lot, values=values, signals=signals, blind_value=blind_values.get(lot.lot)
            )
        )
    canaries = {seat: f"CANARY-S{seat}-ZXQ" for seat in base.missions}
    missions = {
        seat: mission.model_copy(update={"canary": canaries[seat]})
        for seat, mission in base.missions.items()
    }
    marked = dataclasses.replace(base, budgets=budgets, lots=tuple(lots), missions=missions)
    return marked, Marks(budgets, private, canaries, blind_values)


# -------------------------------------------------------------------- real endpoints, offline


def concrete_api_config() -> NightConfig:
    """The example API configuration with every ``<placeholder>`` filled and an inline seed.

    It has six model seats on four endpoints (two Claude models, two OpenAI-compatible vendors),
    so a night on it needs the real client classes and nothing else. The price file is dropped.
    """

    def fill(node: Any) -> Any:
        if isinstance(node, dict):
            return {key: fill(value) for key, value in node.items()}
        if isinstance(node, list):
            return [fill(value) for value in node]
        if isinstance(node, str) and node.startswith("<") and node.endswith(">"):
            return node.strip("<>")
        return node

    with (REPO / "configs" / "night_api.example.yaml").open(encoding="utf-8") as handle:
        raw = fill(yaml.safe_load(handle))
    raw["night"].pop("deal_seed_commitment")
    raw["night"]["seed"] = raw["night"].pop("seed_env").lower()
    raw["limits"]["prices_file"] = None
    return NightConfig.model_validate(raw)
