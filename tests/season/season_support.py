"""Helpers of the season tests: real mock nights played into directories, and logs written by hand.

A real night is the mock night of ``configs/night_mock.yaml`` cut down to four model seats and two
bots, played through the whole model path (a second or less each). A hand-written night has only
the events the season reads (``night_start``, some actions, the missions and ``night_end``), so its
scores are whatever a test needs them to be. Both come with the ``config.yaml`` snapshot that
``auction-night season`` reads.
"""

import json
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from auction_night.contracts import NightConfig, dumps_events, seed_commitment
from auction_night.contracts.config import ModelPrice
from auction_night.contracts.events import (
    ActionData,
    ActionEvent,
    LotPlan,
    MissionResultData,
    MissionResultEvent,
    NightEndData,
    NightEndEvent,
    NightStartData,
    NightStartEvent,
    SeatPublic,
    SeatScore,
    SeedCommitData,
    SeedCommitEvent,
)
from auction_night.llm.accounting import PriceTable
from auction_night.llm.nightrun import run_directory

REPO = Path(__file__).resolve().parents[2]
MOCK_CONFIG = REPO / "configs" / "night_mock.yaml"
POLICIES = ("honest", "collude", "backstab", "random")
NAMES = ("鲸落", "阿铁", "小满", "拾光")
BOTS = (("老实人", "truthful"), ("经济学家", "economist"))


def base_config() -> dict[str, Any]:
    with MOCK_CONFIG.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def write_config(directory: Path, raw: Mapping[str, Any]) -> None:
    (directory / "config.yaml").write_text(
        yaml.safe_dump(dict(raw), allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def night_name(season: str, deck: str, rotation: int) -> str:
    return f"{season}-{deck}-r{rotation}"


def roster(rotation: int, models: Sequence[str] = POLICIES) -> list[tuple[int, str, str]]:
    """``(seat, codename, mock policy)`` of the model seats of one rotation: the model at seat 1
    moves up one place in the list each night, so every model sits at every seat once."""
    count = len(models)
    return [(i + 1, NAMES[i], models[(i + rotation) % count]) for i in range(count)]


# -------------------------------------------------------------------------- real mock nights


def real_config(
    deck: str,
    rotation: int,
    *,
    season: str = "s1",
    deck_seed: str | None = None,
    season_format: str = "rotation",
    night_id: str | None = None,
    cost_cap: float | None = None,
) -> dict[str, Any]:
    """The configuration of one real night: four mock model seats, two bots, six lots."""
    raw = base_config()
    night = raw["night"]
    night["id"] = night_id or night_name(season, deck, rotation)
    night["seed"] = deck_seed or f"seed-{season}-{deck}"
    night["lots_per_format"] = 1
    night["tiers"] = {100: 3, 200: 2, 400: 1}
    night["tags"] = ["海洋", "机械", "甜品"]
    night["missions"].update(collectors=2, shills=2)
    raw["season"] = {"id": season, "format": season_format, "deck": deck}
    if season_format == "rotation":
        raw["season"]["rotation"] = rotation
    raw["seats"] = [
        {"seat": seat, "codename": name, "kind": "mock", "mock_policy": policy}
        for seat, name, policy in roster(rotation)
    ] + [
        {"seat": 5, "codename": BOTS[0][0], "kind": "bot", "policy": "truthful"},
        {"seat": 6, "codename": BOTS[1][0], "kind": "bot", "policy": "economist", "pacing": True},
    ]
    if cost_cap is not None:
        raw["limits"]["max_cost_per_night_usd"] = cost_cap
    return raw


def mock_prices() -> PriceTable:
    price = ModelPrice(
        input_per_mtok=1.0,
        output_per_mtok=1.0,
        cache_read_per_mtok=1.0,
        cache_write_per_mtok=1.0,
        as_of=date(2026, 11, 1),
        source="https://example.com/prices",
    )
    return PriceTable({f"mock-{policy}": price for policy in POLICIES}, source="test prices")


def play_real_night(directory: Path, raw: Mapping[str, Any], *, priced: bool = False) -> Path:
    """Play the night of ``raw`` into ``directory`` and put its configuration next to the log."""
    config = NightConfig.model_validate(dict(raw))
    run_directory(config, directory, prices=mock_prices() if priced else None)
    write_config(directory, raw)
    return directory


def play_real_season(root: Path, decks: Sequence[str] = ("d1", "d2"), rotations: int = 4) -> Path:
    """A rotation season of real mock nights: ``rotations`` nights on each of the decks."""
    for deck in decks:
        for rotation in range(rotations):
            name = night_name("s1", deck, rotation)
            play_real_night(root / name, real_config(deck, rotation))
    return root


def copy_season(source: Path, target: Path) -> Path:
    shutil.copytree(source, target)
    return target


# ------------------------------------------------------------------------- hand-written nights


@dataclass(frozen=True)
class SeatSpec:
    """One seat of a hand-written night."""

    seat: int
    codename: str
    surplus: int
    policy: str  # the mock policy of a model seat, or the policy of a bot
    bot: bool = False
    bonus: int = 0
    mission: str | None = None  # "collector" or "shill"
    done: bool = False
    actions: int = 4  # decisions that were not abstentions
    abstain: Mapping[str, int] = field(default_factory=dict)  # reason -> number of actions
    kept: int = 0  # English revisions that kept the earlier exit price


def _lot() -> LotPlan:
    return LotPlan(
        lot=1,
        item="lot-01",
        name="拍品 1 号",
        emoji="📦",
        tag="海洋",
        tier=100,
        format="sealed_first",
        mechanism="sealed_first@v1",
    )


def _action(seq: int, seat: int, **data: Any) -> ActionEvent:
    phase = data.pop("phase", "talk")
    return ActionEvent(
        seq=seq,
        lot=1,
        vis=f"seat:{seat}",
        data=ActionData(seat=seat, phase=phase, **data),
    )


def night_events(
    night_id: str, seats: Sequence[SeatSpec], *, status: str = "completed", deck: str | None = None
) -> list[Any]:
    """The events of a hand-written night, in order, with ``seq`` from 1."""
    events: list[Any] = [
        NightStartEvent(
            seq=1,
            vis="public",
            data=NightStartData(
                night_id=night_id,
                lang="zh",
                mode="plain",
                n_lots=1,
                seats=[
                    SeatPublic(seat=s.seat, codename=s.codename, role="bot" if s.bot else "player")
                    for s in seats
                ],
                lots=[_lot()],
                budget_lo=150,
                budget_hi=450,
                collectors=1,
                shills=1,
                mission_bonus=150,
            ),
        )
    ]
    if deck is not None:
        commitment = seed_commitment(f"seed-{deck}")
        events.append(
            SeedCommitEvent(
                seq=2,
                vis="public",
                data=SeedCommitData(phase="commit", commitment=commitment, deck_id=deck),
            )
        )

    def add(factory: Callable[[int], Any]) -> None:
        events.append(factory(len(events) + 1))

    for s in seats:
        for _ in range(s.actions):
            add(lambda q, s=s: _action(q, s.seat, status="ok"))
        for reason, count in s.abstain.items():
            for _ in range(count):
                add(
                    lambda q, s=s, r=reason: _action(
                        q, s.seat, phase="sealed", status="abstain", reason=r
                    )
                )
        for _ in range(s.kept):
            add(
                lambda q, s=s: _action(
                    q, s.seat, phase="english_revise", status="kept_previous", value=5
                )
            )
    for s in seats:
        if s.mission is None:
            continue
        shill = s.mission == "shill"
        data = MissionResultData(
            seat=s.seat,
            kind=s.mission,
            completed=s.done,
            bonus=s.bonus,
            tag=None if shill else "海洋",
            need=None if shill else 1,
            target_lot=1 if shill else None,
            threshold_price=50 if shill else None,
        )
        add(lambda q, data=data: MissionResultEvent(seq=q, vis="public", data=data))
    scores = [
        SeatScore(seat=s.seat, surplus=s.surplus, mission_bonus=s.bonus, total=s.surplus + s.bonus)
        for s in seats
    ]
    add(
        lambda q: NightEndEvent(
            seq=q,
            vis="public",
            data=NightEndData(status=status, scores=scores),
        )
    )
    return events


def written_config(
    night_id: str,
    seats: Sequence[SeatSpec],
    *,
    season: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """A valid configuration whose seats are those of ``seats``."""
    raw = base_config()
    raw["night"]["id"] = night_id
    models = sum(1 for s in seats if not s.bot)
    collectors = min(1, models)
    raw["night"]["missions"].update(collectors=collectors, shills=min(1, models - collectors))
    if season is not None:
        raw["season"] = dict(season)
    raw["seats"] = [
        (
            {"seat": s.seat, "codename": s.codename, "kind": "bot", "policy": s.policy}
            if s.bot
            else {"seat": s.seat, "codename": s.codename, "kind": "mock", "mock_policy": s.policy}
        )
        for s in seats
    ]
    return raw


def write_night(
    directory: Path,
    night_id: str,
    seats: Sequence[SeatSpec],
    *,
    status: str = "completed",
    season: Mapping[str, Any] | None = None,
    config: bool = True,
    log_deck: str | None = None,
) -> Path:
    """Write a hand-written night: ``events.jsonl`` and, unless ``config`` is false, its
    ``config.yaml``."""
    directory.mkdir(parents=True, exist_ok=True)
    events = night_events(night_id, seats, status=status, deck=log_deck)
    (directory / "events.jsonl").write_text(dumps_events(events), encoding="utf-8")
    if config:
        write_config(directory, written_config(night_id, seats, season=season))
    return directory


def specs(
    rotation: int,
    surplus: Callable[[str], int],
    *,
    models: Sequence[str] = POLICIES[:3],
    bot_surplus: int = 0,
    **extra: Any,
) -> list[SeatSpec]:
    """The seats of one night of a hand-written rotation season.

    ``surplus`` maps the model id of a seat (``mock-honest``) to its surplus tonight. A bot sits
    after the models.
    """
    out = [
        SeatSpec(seat, name, surplus(f"mock-{policy}"), policy, **extra)
        for seat, name, policy in roster(rotation, models)
    ]
    out.append(SeatSpec(len(out) + 1, BOTS[0][0], bot_surplus, BOTS[0][1], bot=True))
    return out


def write_rotation_season(
    root: Path,
    scores: Callable[[str, str, int], int],
    *,
    decks: Sequence[str] = ("d1", "d2"),
    models: Sequence[str] = POLICIES[:3],
    bot: Callable[[str, int], int] = lambda deck, rotation: 0,
    season_id: str = "syn",
    **extra: Any,
) -> list[Path]:
    """A rotation season written by hand: each deck is played in every rotation.

    ``scores(model id, deck, rotation)`` is the surplus of that model on that night.
    """
    paths = []
    for deck in decks:
        for rotation in range(len(models)):
            night = night_name(season_id, deck, rotation)
            seats = specs(
                rotation,
                lambda model, d=deck, r=rotation: scores(model, d, r),
                models=models,
                bot_surplus=bot(deck, rotation),
                **extra,
            )
            season = {"id": season_id, "format": "rotation", "deck": deck, "rotation": rotation}
            paths.append(write_night(root / night, night, seats, season=season))
    return paths


def write_single_season(
    root: Path,
    scores: Sequence[Mapping[str, int]],
    *,
    models: Sequence[str] = POLICIES[:3],
    season_id: str = "one",
    bot: int = 0,
    **extra: Any,
) -> list[Path]:
    """A single-night season written by hand: ``scores[k]`` maps model ids to surplus on night k."""
    paths = []
    for k, table in enumerate(scores):
        seats = specs(0, lambda model, t=table: t[model], models=models, bot_surplus=bot, **extra)
        night = f"{season_id}-n{k:02d}"
        season = {"id": season_id, "format": "single"}
        paths.append(write_night(root / night, night, seats, season=season))
    return paths


def rewrite_status(directory: Path, status: str) -> None:
    """Change the status in ``night_end`` of a log, leaving the rest as it is."""
    path = directory / "events.jsonl"
    text = path.read_text(encoding="utf-8")
    old = '"status":"completed"'
    assert text.count(old) == 1
    path.write_text(text.replace(old, f'"status":"{status}"'), encoding="utf-8")


def rewrite_mode(directory: Path, mode: str) -> None:
    """Change the mode in ``night_start`` of a log, leaving the rest as it is."""
    path = directory / "events.jsonl"
    text = path.read_text(encoding="utf-8")
    old = '"mode":"plain"'
    assert text.count(old) == 1
    path.write_text(text.replace(old, f'"mode":"{mode}"'), encoding="utf-8")


def write_calls(directory: Path, models: Mapping[int, str]) -> None:
    """A cassette reduced to what the season reads: the seat and the model of each call."""
    rows = [json.dumps({"v": 1, "role": f"seat:{seat}", "model": m}) for seat, m in models.items()]
    (directory / "calls.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
