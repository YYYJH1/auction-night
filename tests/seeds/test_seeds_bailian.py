"""The Bailian template through ``seeds new`` and ``seeds plan``.

The template ``configs/night_bailian.yaml`` holds no deal seed. ``plan`` writes the configuration of
every night of a season from it, and each of those must be a valid night of its own. Nothing here
calls a model or touches the network.
"""

import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml
from seeds_support import REPO, STAMP, plain, read_json, seeds_cli
from typer.testing import CliRunner

from auction_night.cli import app
from auction_night.contracts import NightConfig, find_placeholders, load_config
from auction_night.llm.nightrun import NightRunError, load_night_config

TEMPLATE = REPO / "configs" / "night_bailian.yaml"
MODELS = [
    "qwen3.7-plus",
    "qwen-flash",
    "deepseek-v4.1-flash",
    "deepseek-v4-flash-0731",
    "glm-5",
    "glm-5.1",
]


def season(workdir: Path, *extra: str, decks: int = 2) -> tuple[Path, Path, Any]:
    """``seeds new`` and ``seeds plan`` in a scratch directory. Returns the season directory, the
    private seed file and the result of ``plan``."""
    season_dir = workdir / "seasons" / "s1"
    private = workdir / "private" / "s1.seeds-private.json"
    made = seeds_cli(
        "new", "--season", "s1", "--decks", str(decks), "--private-out", private,
        "--out", season_dir / "commitments.json", "--created-utc", STAMP,
    )  # fmt: skip
    assert made.exit_code == 0, made.output
    planned = seeds_cli(
        "plan", "--template", TEMPLATE, "--commitments", season_dir / "commitments.json", *extra
    )
    assert planned.exit_code == 0, planned.output
    return season_dir, private, planned


def planned(season_dir: Path) -> dict[str, dict[str, Any]]:
    """The configurations ``plan`` wrote, as plain data, by night id."""
    files = sorted((season_dir / "configs").glob("*.yaml"))
    return {p.stem: yaml.safe_load(p.read_text(encoding="utf-8")) for p in files}


# ------------------------------------------------------------------------ the template


def test_the_template_holds_no_seed_and_leaves_the_commitment_to_plan() -> None:
    config = load_config(TEMPLATE)
    assert config.night.seed is None and config.night.seed_env
    assert config.night.deal_seed_commitment == "<commitment-of-the-deal-seed>"
    assert find_placeholders(config) == ["night.deal_seed_commitment"]
    assert config.season.format == "single" and config.season.max_seats_per_vendor == 2


def test_the_template_cannot_be_run_as_it_is() -> None:
    """The commitment is a placeholder until plan fills it in, so a run refuses the template."""
    with pytest.raises(NightRunError, match="night.deal_seed_commitment"):
        load_night_config(TEMPLATE)


# ------------------------------------------------------------------------ what plan writes


def test_plan_writes_a_valid_night_for_every_deck_and_rotation(workdir: Path) -> None:
    season_dir, _, _ = season(workdir)
    commitments = read_json(season_dir / "commitments.json")
    published = {d["deck"]: d["commitment"] for d in commitments["decks"]}
    files = sorted((season_dir / "configs").glob("*.yaml"))
    assert [p.stem for p in files] == [f"s1-d0{d}-r{r}" for d in (1, 2) for r in range(6)]
    for path in files:
        config = load_config(path)
        deck = config.season.deck
        assert deck in published and config.night.id == path.stem
        assert config.night.seed is None and config.night.seed_env == (
            f"AUCTION_NIGHT_SEED_S1_{deck.upper()}"  # type: ignore[union-attr]
        )
        assert config.night.deal_seed_commitment == published[deck]  # type: ignore[index]
        assert (config.season.id, config.season.format) == ("s1", "rotation")
        assert find_placeholders(config) == [], "nothing is left to fill in before a run"
        assert path.read_text(encoding="utf-8").startswith(f"# Night {path.stem}: deck")


def test_plan_prints_one_line_per_night_and_no_note(workdir: Path) -> None:
    season_dir, _, result = season(workdir, decks=1)
    text = plain(result.output)
    assert f"season s1: 6 nights in {season_dir / 'configs'}" in text
    assert "wrote 6 files, 0 were already there" in text
    assert "note:" not in text, "the template has neither an inline seed nor a placeholder to flag"


def test_no_seed_is_written_to_any_file(workdir: Path) -> None:
    season_dir, private, _ = season(workdir)
    seeds = [d["seed"] for d in read_json(private)["decks"]]
    for path in (season_dir / "configs").glob("*.yaml"):
        text = path.read_text(encoding="utf-8")
        assert not any(seed in text for seed in seeds), path.name
    assert not (season_dir / "seeds.json").exists(), "nothing was revealed"


# ------------------------------------------------------------------------ the rules hold


def test_the_vendor_and_role_rules_hold_in_every_rotation(workdir: Path) -> None:
    season_dir, _, _ = season(workdir, "--deck", "d01")
    nights = planned(season_dir)
    assert len(nights) == 6
    for name, raw in nights.items():
        config = NightConfig.model_validate(raw)
        vendors = Counter(s.vendor for s in config.seats if s.kind == "llm")
        assert vendors == {"qwen": 2, "deepseek": 2, "zhipu": 2}, name
        host = config.show.host_offline
        assert host.vendor == "moonshot" and host.vendor not in vendors
        classifier = config.show.content_scan.classifier
        assert classifier is not None and classifier.vendor not in vendors
        judges = [j.vendor for j in config.show.judges]
        assert len(set(judges)) == 2 and config.show.allow_judge_vendor_overlap
        assert config.limits.max_cost_per_night_usd == 1.5, name


def test_every_model_sits_at_every_seat_once_over_the_rotations(workdir: Path) -> None:
    season_dir, _, _ = season(workdir, "--deck", "d01")
    nights = planned(season_dir)
    for seat in range(6):
        sitting = [
            next(s for s in nights[f"s1-d01-r{r}"]["seats"] if s["seat"] == seat + 1)
            for r in range(6)
        ]
        assert sorted(s["model"] for s in sitting) == sorted(MODELS), f"seat {seat + 1}"
        assert len({s["codename"] for s in sitting}) == 1, "the code name stays at the seat"
        assert all(s["max_tokens"] == 8192 for s in sitting), "max_tokens moves with the model"
    first = nights["s1-d01-r0"]["seats"]
    assert [s["model"] for s in first[:6]] == MODELS
    for raw in nights.values():
        assert [s["kind"] for s in raw["seats"][6:]] == ["bot", "bot"]


def test_the_rest_of_the_template_is_kept(workdir: Path) -> None:
    season_dir, _, _ = season(workdir, "--deck", "d02", "--rotations", "2")
    template = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    nights = planned(season_dir)
    assert sorted(nights) == ["s1-d02-r0", "s1-d02-r1"]
    for raw in nights.values():
        for section in ("endpoints", "show", "limits"):
            assert raw[section] == template[section], section
        kept = {
            k: v
            for k, v in template["night"].items()
            if k not in {"id", "seed", "seed_env", "deal_seed_commitment"}
        }
        assert {k: raw["night"][k] for k in kept} == kept
        assert raw["season"]["max_seats_per_vendor"] == 2


# ----------------------------------------------------------- a planned night, priced


def test_a_planned_night_costs_the_same_in_every_rotation(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    season_dir, _, _ = season(workdir, "--deck", "d01", "--rotations", "3")
    monkeypatch.chdir(REPO)  # the price list is named relative to the repository
    totals = []
    for rotation in range(3):
        path = season_dir / "configs" / f"s1-d01-r{rotation}.yaml"
        result = CliRunner().invoke(app, ["estimate", "-c", str(path)])
        text = plain(result.output)
        assert result.exit_code == 0, text
        assert "Prices: configs/prices.bailian.yaml" in text
        assert "Exchange rate: 1 USD = 6.6529 CNY (as of 2026-10-30" in text
        total = next(line for line in text.splitlines() if line.startswith("Total for 1 night"))
        assert "$" in total and "¥" in total
        assert "Placeholders still in the configuration" not in text
        totals.append(re.sub(r"\s+", " ", total))
    assert len(set(totals)) == 1, "the same six models cost the same wherever they sit"
