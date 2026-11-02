"""Changing the commitments, the seeds, a replay, a log or a configuration is caught by verify.

Each case damages one thing in a private copy of a finished season and runs ``seeds verify`` on
it. A change that leaves a file valid is a failed check (exit status 1). A change that makes a file
unreadable is a file that cannot be used (exit status 2). Either way verify does not pass.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from seeds_support import (
    TEMPLATE,
    Season,
    card_of,
    cli,
    edit_events,
    edit_json,
    read_json,
    seeds_cli,
    verify_args,
)

from auction_night.contracts.events import seed_commitment

D1, D2 = "s1-d01-r0", "s1-d02-r1"
OPEN_D1 = "deck d01: the revealed seed does not open"
OPEN_D2 = "deck d02: the revealed seed does not open"
OTHER = "sha256:" + "ab" * 32


def flip(commitment: str) -> str:
    """The same commitment with its last digit changed."""
    return commitment[:-1] + ("0" if commitment[-1] != "0" else "1")


def deck_entry(data: dict[str, Any], deck: str) -> dict[str, Any]:
    return next(entry for entry in data["decks"] if entry["deck"] == deck)


# ----------------------------------------------------------------- damage to the public files


def commitment_flipped(season: Season) -> None:
    def change(data: Any) -> None:
        entry = deck_entry(data, "d01")
        entry["commitment"] = flip(entry["commitment"])

    edit_json(season.commitments, change)


def commitments_swapped(season: Season) -> None:
    def change(data: Any) -> None:
        a, b = deck_entry(data, "d01"), deck_entry(data, "d02")
        a["commitment"], b["commitment"] = b["commitment"], a["commitment"]

    edit_json(season.commitments, change)


def deck_added(season: Season) -> None:
    edit_json(
        season.commitments,
        lambda data: data["decks"].append({"deck": "d03", "commitment": OTHER}),
    )


def deck_renamed(season: Season) -> None:
    edit_json(season.commitments, lambda data: deck_entry(data, "d01").update(deck="x01"))


def seed_changed(season: Season) -> None:
    def change(data: Any) -> None:
        entry = deck_entry(data, "d01")
        entry["seed"] = entry["seed"][:-1] + ("0" if entry["seed"][-1] != "0" else "1")

    edit_json(season.revealed, change)


def seeds_swapped(season: Season) -> None:
    def change(data: Any) -> None:
        a, b = deck_entry(data, "d01"), deck_entry(data, "d02")
        a["seed"], b["seed"] = b["seed"], a["seed"]

    edit_json(season.revealed, change)


def seed_too_short(season: Season) -> None:
    """A seed that opens its commitment, but could have been guessed from it."""
    short = "guessable"
    edit_json(season.revealed, lambda data: deck_entry(data, "d01").update(seed=short))
    edit_json(
        season.commitments,
        lambda data: deck_entry(data, "d01").update(commitment=seed_commitment(short)),
    )


def deck_not_revealed(season: Season) -> None:
    edit_json(season.revealed, lambda data: data.update(decks=data["decks"][:1]))


def seeds_of_another_season(season: Season) -> None:
    edit_json(season.revealed, lambda data: data.update(season="s2"))


# ----------------------------------------------------------------------- damage to a replay


def replay_other_deck(season: Season) -> None:
    other = deck_entry(read_json(season.commitments), "d02")["commitment"]
    edit_json(
        season.night(D1) / "replay.json",
        lambda data: data["manifest"].update(seed_commitment=other),
    )


def replay_pair_replaced(season: Season) -> None:
    """A seed and a commitment that fit each other, and are not the published ones."""
    fresh = "e" * 64
    edit_json(
        season.night(D1) / "replay.json",
        lambda data: data["manifest"].update(
            seed_commitment=seed_commitment(fresh), revealed_seed=fresh
        ),
    )


def replay_budget(season: Season) -> None:
    def change(data: Any) -> None:
        data["reveal"]["budgets"]["1"] += 1

    edit_json(season.night(D1) / "replay.json", change)


def replay_value(season: Season) -> None:
    def change(data: Any) -> None:
        values = data["reveal"]["values"]["2"]
        lot = next(iter(values))
        values[lot] += 1

    edit_json(season.night(D1) / "replay.json", change)


def replay_lot_tier(season: Season) -> None:
    edit_json(
        season.night(D1) / "replay.json",
        lambda data: data["catalog"][0].update(tier=data["catalog"][0]["tier"] + 1),
    )


# ------------------------------------------------------------------------- damage to a log


def log_budget(season: Season) -> None:
    card_of(season.night(D1), 1, lambda card: card.update(budget=card["budget"] + 1))


def log_value(season: Season) -> None:
    def change(card: dict[str, Any]) -> None:
        lot = next(iter(card["values"]))
        card["values"][lot] += 1

    card_of(season.night(D1), 2, change)


def log_signal(season: Season) -> None:
    def change(card: dict[str, Any]) -> None:
        lot = next(iter(card["signals"]))
        card["signals"][lot] -= 1

    card_of(season.night(D1), 3, change)


def log_mission(season: Season) -> None:
    card_of(season.night(D1), 1, lambda card: card["mission"].update(bonus=999))


def log_lot_tier(season: Season) -> None:
    def pick(event: dict[str, Any]) -> bool:
        if event["type"] == "night_start":
            event["data"]["lots"][0]["tier"] += 1
            return True
        return False

    assert edit_events(season.night(D1) / "events.jsonl", pick) == 1


def log_commit_event(season: Season) -> None:
    def pick(event: dict[str, Any]) -> bool:
        if event["type"] == "seed_commit":
            event["data"]["commitment"] = OTHER
            return True
        return False

    assert edit_events(season.night(D1) / "events.jsonl", pick) == 1


def log_true_value(season: Season) -> None:
    def pick(event: dict[str, Any]) -> bool:
        if event["type"] == "hammer" and event["data"]["true_value"] is not None:
            event["data"]["true_value"] += 1
            return True
        return False

    assert edit_events(season.night(D1) / "events.jsonl", pick) >= 1


def log_commit_event_cut_out(season: Season) -> None:
    path = season.night(D1) / "events.jsonl"
    lines = path.read_text(encoding="utf-8").split("\n")
    path.write_text("\n".join(line for line in lines if '"seed_commit"' not in line), "utf-8")


def log_not_json(season: Season) -> None:
    path = season.night(D1) / "events.jsonl"
    path.write_text("not json\n" + path.read_text(encoding="utf-8"), encoding="utf-8")


def log_of_the_other_night(season: Season) -> None:
    (season.night(D1) / "events.jsonl").write_bytes(
        (season.night(D2) / "events.jsonl").read_bytes()
    )


# ------------------------------------------------------------------ damage to a configuration


def edit_config(season: Season, night: str, change: Callable[[dict[str, Any]], None]) -> None:
    path = season.night(night) / "config.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def config_commitment(season: Season) -> None:
    edit_config(season, D1, lambda c: c["night"].update(deal_seed_commitment=OTHER))


def config_economy(season: Season) -> None:
    edit_config(season, D1, lambda c: c["night"]["budget"].update(c=301))


def config_deck(season: Season) -> None:
    edit_config(season, D1, lambda c: c["season"].update(deck="d02"))


def config_season(season: Season) -> None:
    edit_config(season, D1, lambda c: c["season"].update(id="s2"))


def config_inline_seed(season: Season) -> None:
    def change(config: dict[str, Any]) -> None:
        del config["night"]["seed_env"]
        config["night"]["seed"] = season.seeds["d01"]

    edit_config(season, D1, change)


def config_placeholder(season: Season) -> None:
    edit_config(season, D1, lambda c: c["night"].update(deal_seed_commitment="<not-yet-known>"))


def config_removed(season: Season) -> None:
    (season.night(D1) / "config.yaml").unlink()


def replay_removed(season: Season) -> None:
    (season.night(D1) / "replay.json").unlink()


def replay_swapped(season: Season) -> None:
    (season.night(D1) / "replay.json").write_bytes((season.night(D2) / "replay.json").read_bytes())


# --------------------------------------------------------------------------------- the cases

CASES: list[tuple[str, str, Callable[[Season], None], int, str]] = [
    # (name, season, damage, exit status, text that verify must show)
    ("commitment flipped", "end", commitment_flipped, 1, OPEN_D1),
    ("commitment flipped, nights", "end", commitment_flipped, 1, "was published for deck d01"),
    ("commitments swapped", "end", commitments_swapped, 1, OPEN_D2),
    ("deck added", "end", deck_added, 1, "deck d03: committed, but its seed is not revealed"),
    ("deck renamed", "end", deck_renamed, 1, "deck d01 is not in the commitments of season s1"),
    ("seed changed", "end", seed_changed, 1, OPEN_D1),
    ("seed changed, cards", "end", seed_changed, 1, "differs from the cards of the seed"),
    ("seeds swapped", "end", seeds_swapped, 1, OPEN_D1),
    ("seed too short", "end", seed_too_short, 1, "deck d01: the seed has 9 characters"),
    ("deck not revealed", "end", deck_not_revealed, 1, "deck d02: committed, but its seed is not"),
    ("seeds of another season", "end", seeds_of_another_season, 1, "the seeds are of season 's2'"),
    ("replay commitment", "mid", replay_other_deck, 1, "replay.json carries"),
    ("replay seed and commitment", "end", replay_pair_replaced, 1, "replay.json carries"),
    ("replay budget", "end", replay_budget, 1, "reveal.budgets differ from the seed"),
    ("replay value", "end", replay_value, 1, "reveal.values differ from the seed"),
    ("replay lot", "end", replay_lot_tier, 1, "lot 1: the tier is"),
    ("log budget", "end", log_budget, 1, "seat 1: the budget is"),
    ("log value", "end", log_value, 1, "seat 2: the value of lot"),
    ("log signal", "end", log_signal, 1, "seat 3: the signal of lot"),
    ("log mission", "end", log_mission, 1, "seat 1: the mission differs"),
    ("log lot", "end", log_lot_tier, 1, "lot 1: the tier is"),
    ("log commit event", "end", log_commit_event, 1, "events.jsonl carries sha256:"),
    ("log commit event, before the reveal", "mid", log_commit_event, 1, "events.jsonl carries"),
    ("log true value", "end", log_true_value, 1, "the true value at the hammer differs"),
    ("log without its commitment", "end", log_commit_event_cut_out, 2, "has seq"),
    ("log that is not JSON", "end", log_not_json, 2, "line 1 is not a valid event"),
    ("log of another night", "end", log_of_the_other_night, 1, "the files are not of one night"),
    ("config commitment", "end", config_commitment, 1, "config.yaml carries sha256:"),
    ("config economy", "end", config_economy, 1, "is not the configuration that replay.json"),
    ("config deck", "end", config_deck, 1, "the files name different decks"),
    ("config season", "end", config_season, 1, "config.yaml is of season 's2'"),
    ("config with the seed inside", "mid", config_inline_seed, 1, "holds the deal seed while"),
    ("config with a placeholder", "end", config_placeholder, 1, "still holds a placeholder"),
    ("config removed, strict", "end", config_removed, 1, "cards not recomputed: no config.yaml"),
    ("replay of the other night", "end", replay_swapped, 1, "not of one night"),
]  # fmt: skip


@pytest.mark.parametrize(
    ("season_name", "damage", "status", "text"),
    [pytest.param(s, d, e, t, id=name) for name, s, d, e, t in CASES],
)
def test_damage_to_any_file_is_caught(
    season_name: str,
    damage: Callable[[Season], None],
    status: int,
    text: str,
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> None:
    base: Season = request.getfixturevalue(
        "end_of_season" if season_name == "end" else "mid_season"
    )
    season = base.copy_to(tmp_path / "tree")
    revealed = season_name == "end"
    assert seeds_cli(*verify_args(season, revealed=revealed)).exit_code == 0, "undamaged: passes"
    damage(season)
    extra = ["--strict"] if revealed else []
    result = seeds_cli(*verify_args(season, *extra, revealed=revealed))
    assert result.exit_code == status, result.output
    assert text in result.output, result.output
    if status == 1:
        assert "FAIL" in result.output and ", 0 failed," not in result.output.splitlines()[-1]


def test_a_night_played_without_a_commitment_is_refused(mid_season: Season, tmp_path: Path) -> None:
    """The mock night of the repository has an inline seed and no commitment at all."""
    night = tmp_path / "plain"
    assert cli("run", "--config", TEMPLATE, "--out", night).exit_code == 0
    result = seeds_cli("verify", "--commitments", mid_season.commitments, night)
    assert result.exit_code == 1
    assert "it names no deck, and its commitment is not one of the published ones" in result.output


def test_a_commitment_from_another_season_is_not_accepted(
    mid_season: Season, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    made = seeds_cli(
        "new", "--season", "s1", "--decks", "2", "--private-out", tmp_path / "p.seeds-private.json",
        "--out", other / "commitments.json",
    )  # fmt: skip
    assert made.exit_code == 0, made.output
    args = ["verify", "--commitments", other / "commitments.json", mid_season.night(D1)]
    result = seeds_cli(*args)  # the right ids, but commitments that were drawn later
    assert result.exit_code == 1 and "was published for deck d01" in result.output


def test_verification_needs_the_public_files_only(mid_season: Season, tmp_path: Path) -> None:
    """No private file, and no environment variable: anyone can run the commitment checks."""
    copy = mid_season.copy_to(tmp_path / "tree")
    copy.private.unlink()
    result = seeds_cli(*verify_args(copy))
    assert result.exit_code == 0, result.output
