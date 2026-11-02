"""Shared helpers of the engine tests: hypothesis strategies and independent oracles.

The oracles are deliberately written in a different style from the engine (sorting and
level-by-level scanning, no state machine), so that a test comparing the two checks more
than the engine against itself.
"""

import copy
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from hypothesis import strategies as st

from auction_night.contracts import NightConfig

REPO = Path(__file__).resolve().parents[2]
SEATS = tuple(range(1, 9))
SEED_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.:-"


# The deal-relevant fields of the night used by the engine tests. They are pinned here so that
# the golden decks do not move when the shared example configuration is edited for other
# reasons. Everything else (show roles, limits, code names) comes from that example file.
PINNED_NIGHT: dict[str, Any] = {
    "seed": "mock-seed-0001",
    "formats": [
        "english",
        "dutch",
        "sealed_first",
        "sealed_second",
        "all_pay",
        "blind_box",
    ],
    "lots_per_format": 2,
    "reserve": 0,
    "tags": ["海洋", "机械", "甜品", "怪谈"],
    "tiers": {100: 5, 200: 4, 400: 3},
    "budget": {"c": 300, "spread": [0.5, 1.5], "rounding": "floor"},
    "dutch": {"levels": 10, "mode": "one_shot"},
    "english": {"revise_when_remaining_le": 4, "revise_timeout": "keep_previous"},
    "blind": {"value_range": [0.5, 1.5], "noise": 0.2},
    "missions": {
        "bonus": 150,
        "collectors": 3,
        "shills": 3,
        "collector_need": 2,
        "shill_threshold": 0.95,
        "shill_formats": ["english", "sealed_second"],
    },
}
PINNED_SEATS: list[dict[str, Any]] = [
    {"seat": 1, "codename": "鲸落", "kind": "mock", "mock_policy": "honest"},
    {"seat": 2, "codename": "阿铁", "kind": "mock", "mock_policy": "collude"},
    {"seat": 3, "codename": "小满", "kind": "mock", "mock_policy": "honest"},
    {"seat": 4, "codename": "拾光", "kind": "mock", "mock_policy": "collude"},
    {"seat": 5, "codename": "青柠", "kind": "mock", "mock_policy": "backstab"},
    {"seat": 6, "codename": "麦芽", "kind": "mock", "mock_policy": "random"},
    {"seat": 7, "codename": "老实人", "kind": "bot", "policy": "truthful"},
    {"seat": 8, "codename": "经济学家", "kind": "bot", "policy": "economist", "pacing": True},
]


def raw_config() -> dict[str, Any]:
    """The mock night as a plain dict with the deal-relevant fields pinned (a fresh copy)."""
    import yaml

    with (REPO / "configs" / "night_mock.yaml").open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    raw["night"].update(copy.deepcopy(PINNED_NIGHT))
    raw["seats"] = copy.deepcopy(PINNED_SEATS)
    return raw


def mock_config() -> NightConfig:
    return NightConfig.model_validate(raw_config())


# ----------------------------------------------------------------------------- strategies

seeds = st.text(alphabet=SEED_ALPHABET, min_size=1, max_size=40)

# Bids pile up on a few values so that ties and exact budget hits are common.
bid_values = st.one_of(
    st.sampled_from([0, 1, 10, 20, 30, 40, 50, 60, 100, 120, 200, 450, 500]),
    st.integers(0, 600),
)


@st.composite
def tables(
    draw: st.DrawFn, *, min_seats: int = 1, anchors: Sequence[int] = ()
) -> tuple[dict[int, int], dict[int, int]]:
    """Budgets (some of them 0) and tie keys (sometimes equal) for a few seats.

    ``anchors`` are numbers at which a rule changes (a reserve price, the lowest level of a
    Dutch ladder). Budgets are then often one below, on or one above them.
    """
    seats = draw(st.lists(st.sampled_from(SEATS), min_size=min_seats, max_size=8, unique=True))
    near = [max(0, anchor + shift) for anchor in anchors for shift in (-1, 0, 1)]
    ordinary = st.one_of(st.integers(0, 500), st.sampled_from([0, 1, 20, 100, 450]))
    budget = st.one_of(ordinary, st.sampled_from(near)) if near else ordinary
    budgets = {seat: draw(budget) for seat in seats}
    if draw(st.booleans()):
        order = draw(st.permutations(sorted(seats)))
        keys = {seat: rank for rank, seat in enumerate(order)}
    else:
        keys = {seat: draw(st.integers(0, 2)) for seat in seats}
    return budgets, keys


def active(budgets: Mapping[int, int]) -> list[int]:
    return sorted(seat for seat, budget in budgets.items() if budget >= 1)


@st.composite
def answers_for(draw: st.DrawFn, asked: list[int] | tuple[int, ...]) -> dict[int, int | None]:
    """Answers for some of the asked seats: missing keys, None and numbers."""
    out: dict[int, int | None] = {}
    for seat in asked:
        if draw(st.integers(0, 9)) == 0:
            continue  # no entry at all
        out[seat] = draw(st.one_of(st.none(), bid_values))
    return out


@st.composite
def edge_answers(
    draw: st.DrawFn,
    asked: list[int] | tuple[int, ...],
    budgets: Mapping[int, int],
    anchors: Sequence[int] = (),
) -> dict[int, int | None]:
    """Answers that sit on the edges of the rules.

    Some seats send nothing and some send ``None``. The others answer 0, 1, a number next to
    their budget, a number next to one of the ``anchors`` (a reserve price, a level of a Dutch
    ladder, the price of a clock), or any number at all. Every boundary of a rule (one coin
    below the reserve, exactly the budget, exactly a level) is then met within a few examples.
    """
    out: dict[int, int | None] = {}
    for seat in asked:
        if draw(st.integers(0, 9)) == 0:
            continue  # no entry at all
        edges = [0, 1, budgets[seat] - 1, budgets[seat], budgets[seat] + 1]
        edges += [anchor + shift for anchor in anchors for shift in (-1, 0, 1)]
        out[seat] = draw(
            st.one_of(st.none(), st.sampled_from([max(0, edge) for edge in edges]), bid_values)
        )
    return out


# ------------------------------------------------------------------------------ oracles


def clamp(raw: int | None, budget: int) -> int:
    """Effective number: abstention is 0, anything above the budget is the budget."""
    return 0 if raw is None else min(raw, budget)


def oracle_sealed(
    pricing: str, effective: Mapping[int, int], reserve: int, keys: Mapping[int, int]
) -> tuple[int | None, dict[int, int]]:
    """Winner and payments of a sealed lot from the effective bids of the asked seats."""
    live = {seat: bid for seat, bid in effective.items() if bid >= max(reserve, 1)}
    if not live:
        return None, {}
    order = sorted(live, key=lambda seat: (-live[seat], keys[seat], seat))
    winner = order[0]
    if pricing == "first":
        return winner, {winner: live[winner]}
    if pricing == "second":
        rivals = [live[seat] for seat in order[1:]]
        return winner, {winner: max(rivals[0] if rivals else 0, reserve)}
    return winner, {seat: bid for seat, bid in effective.items() if bid >= 1}


def oracle_dutch(
    tier: int,
    levels: int,
    reserve: int,
    effective: Mapping[int, int],
    keys: Mapping[int, int],
) -> tuple[int | None, int | None]:
    """Buyer and price of a Dutch lot by walking the price down level by level."""
    for k in range(levels):
        price = tier * (levels - k) // levels
        if price < reserve:
            continue
        buyers = [seat for seat, grab in effective.items() if grab >= price]
        if buyers:
            return min(buyers, key=lambda seat: (keys[seat], seat)), price
    return None, None


def oracle_english(
    init: Mapping[int, int | None],
    budgets: Mapping[int, int],
    keys: Mapping[int, int],
    reserve: int,
    theta: int,
    scripts: list[Mapping[int, int | None]],
) -> tuple[int | None, int, list[tuple[int, int]]]:
    """An English auction played tick by tick, one currency unit at a time.

    ``scripts`` holds the answers of the successive revision rounds. Returns the winner, the
    price and the exits of the participants as (seat, price) in announcement order.
    """
    asked = active(budgets)
    exits = {
        seat: clamp(init.get(seat), budgets[seat])
        for seat in asked
        if clamp(init.get(seat), budgets[seat]) >= max(reserve, 1)
    }
    rounds = iter(scripts)
    log: list[tuple[int, int]] = []
    price = reserve
    fresh_exit = False
    while len(exits) > 1:
        if fresh_exit and len(exits) <= theta:
            answers = next(rounds, {})
            for seat in list(exits):
                answer = answers.get(seat)
                if answer is not None:
                    exits[seat] = max(clamp(answer, budgets[seat]), price)
            fresh_exit = False
            continue
        while not any(exit_price <= price for exit_price in exits.values()):
            price += 1
        leaving = sorted((s for s, e in exits.items() if e <= price), key=lambda s: (keys[s], s))
        if len(leaving) == len(exits):
            for seat in reversed(leaving[1:]):
                log.append((seat, price))
            return leaving[0], price, log
        for seat in reversed(leaving):
            del exits[seat]
            log.append((seat, price))
        fresh_exit = True
    if exits:
        return next(iter(exits)), max(price, reserve), log
    return None, 0, log
