"""``seeds plan``: the configuration of every night of a rotation season."""

from pathlib import Path
from typing import Any

import pytest
import yaml
from seeds_support import REPO, STAMP, TEMPLATE, plain, seeds_cli

from auction_night import seeds
from auction_night.contracts import NightConfig, load_config
from auction_night.engine.deal import deal

API_EXAMPLE = REPO / "configs" / "night_api.example.yaml"
CHANGED = {"id", "seed", "seed_env", "deal_seed_commitment"}  # what plan rewrites in `night`


def commit(workdir: Path, decks: int = 2) -> tuple[Path, seeds.Commitments, dict[str, str]]:
    """Published commitments in a file, and the seeds behind them."""
    private, public = seeds.new_season("s1", seeds.deck_ids(decks), created_utc=STAMP)
    path = workdir / "season" / "commitments.json"
    seeds.write_public(path, public)
    return path, public, private.by_deck


def planned_configs(planned: list[seeds.PlannedNight]) -> dict[str, dict[str, Any]]:
    return {night.night: yaml.safe_load(night.text) for night in planned}


def players(seats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The model seats without their number and code name: who sits there."""
    return [
        {k: v for k, v in seat.items() if k not in ("seat", "codename")}
        for seat in seats
        if seat["kind"] in ("llm", "mock")
    ]


def distinct_template(workdir: Path) -> Path:
    """The mock night with six different models at its model seats."""
    raw = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    raw["endpoints"] = {"api": {"kind": "openai_compatible", "base_url": "http://localhost:9/v1"}}
    for number, seat in enumerate(raw["seats"][:6]):
        raw["seats"][number] = {
            "seat": seat["seat"],
            "codename": seat["codename"],
            "kind": "llm",
            "vendor": f"vendor-{number // 2}",
            "endpoint": "api",
            "model": f"model-{number}",
        }
    raw["show"] = {"host_offline": {"kind": "mock"}, "judges": [{"kind": "mock"}, {"kind": "mock"}]}
    path = workdir / "distinct.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------- what is written


def test_plan_writes_a_configuration_for_every_deck_and_rotation(workdir: Path) -> None:
    _, public, _ = commit(workdir)
    planned, notes = seeds.plan_season(TEMPLATE, public)
    assert [n.night for n in planned] == [f"s1-d0{d}-r{r}" for d in (1, 2) for r in range(6)]
    assert [n.filename for n in planned][0] == "s1-d01-r0.yaml"
    assert notes == ["the inline night.seed of the template is not copied into the plan"]
    for night in planned:
        config = NightConfig.model_validate(yaml.safe_load(night.text))
        assert (config.night.id, config.night.seed_env) == (night.night, night.seed_env)
        assert night.seed_env == f"AUCTION_NIGHT_SEED_S1_{night.deck.upper()}"
        assert config.night.deal_seed_commitment == public.by_deck[night.deck]
        season = config.season
        assert (season.id, season.format) == ("s1", "rotation")
        assert (season.deck, season.rotation) == (night.deck, night.rotation)
        assert [(d.id, d.seed_commitment) for d in season.decks] == list(public.by_deck.items())
        assert night.text.startswith(f"# Night {night.night}: deck {night.deck}, rotation")


def test_no_seed_is_in_any_configuration(workdir: Path) -> None:
    _, public, secrets_ = commit(workdir)
    planned, _ = seeds.plan_season(TEMPLATE, public)
    for night in planned:
        assert yaml.safe_load(night.text)["night"].get("seed") is None
        assert "mock-seed-0001" not in night.text, "not even the seed of the template"
        assert not any(seed in night.text for seed in secrets_.values())
        assert NightConfig.model_validate(yaml.safe_load(night.text)).night.seed is None


def test_a_plan_is_the_same_every_time(workdir: Path) -> None:
    _, public, _ = commit(workdir)  # plan_season is given the public commitments and nothing else
    first, _ = seeds.plan_season(TEMPLATE, public)
    again, _ = seeds.plan_season(TEMPLATE, public)
    assert first == again, "the same inputs give the same files, byte for byte"


def test_the_rest_of_the_template_is_kept(workdir: Path) -> None:
    _, public, _ = commit(workdir)
    template = yaml.safe_load(API_EXAMPLE.read_text(encoding="utf-8"))
    planned, _ = seeds.plan_season(API_EXAMPLE, public, rotations=1)
    config = yaml.safe_load(planned[0].text)
    for section in ("endpoints", "show", "limits"):
        assert config[section] == template[section]
    kept = {k: v for k, v in template["night"].items() if k not in CHANGED}
    assert {k: v for k, v in config["night"].items() if k not in CHANGED} == kept
    assert config["season"]["max_seats_per_vendor"] == template["season"]["max_seats_per_vendor"]
    assert list(config["night"])[:3] == ["id", "seed_env", "deal_seed_commitment"]


# ----------------------------------------------------------------------------- the rotation


def test_the_players_move_up_one_seat_in_every_rotation(workdir: Path) -> None:
    _, public, _ = commit(workdir, decks=1)
    template = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    sitting = players(template["seats"])
    planned, _ = seeds.plan_season(TEMPLATE, public)
    configs = planned_configs(planned)
    for rotation in range(6):
        seats = configs[f"s1-d01-r{rotation}"]["seats"]
        for index, seat in enumerate(seats[:6]):
            expected = sitting[(index + rotation) % 6]
            assert players([seat]) == [expected]
            assert (seat["seat"], seat["codename"]) == (
                index + 1,
                template["seats"][index]["codename"],
            )
        assert seats[6:] == template["seats"][6:], "the bots do not move"


def test_every_model_sits_at_every_seat_once(workdir: Path) -> None:
    _, public, _ = commit(workdir, decks=1)
    planned, _ = seeds.plan_season(distinct_template(workdir), public)
    seen: dict[str, list[int]] = {}
    for night in planned:
        for seat in yaml.safe_load(night.text)["seats"]:
            if seat["kind"] == "llm":
                seen.setdefault(seat["model"], []).append(seat["seat"])
    assert sorted(seen) == [f"model-{n}" for n in range(6)]
    assert all(sorted(seats) == [1, 2, 3, 4, 5, 6] for seats in seen.values())


def test_the_model_settings_travel_with_the_model(workdir: Path) -> None:
    _, public, _ = commit(workdir, decks=1)
    planned, notes = seeds.plan_season(API_EXAMPLE, public)
    template = yaml.safe_load(API_EXAMPLE.read_text(encoding="utf-8"))
    haiku = template["seats"][1]  # seat 2 of the template
    second = yaml.safe_load(planned[1].text)  # rotation 1: the player of seat 2 sits at seat 1
    assert second["seats"][0] == {"seat": 1, "codename": template["seats"][0]["codename"]} | {
        k: v for k, v in haiku.items() if k not in ("seat", "codename")
    }
    assert second["seats"][0]["max_tokens"] == 2048 and second["seats"][0]["effort"] == "medium"
    assert second["seats"][5]["model"] == "<model-a1>"  # and the player of seat 1 went to seat 6
    assert any("placeholders to replace before a run" in note for note in notes)


def test_a_deck_is_the_same_cards_in_every_rotation(workdir: Path) -> None:
    _, public, secrets_ = commit(workdir)
    planned, _ = seeds.plan_season(TEMPLATE, public)
    digests: dict[str, set[str]] = {"d01": set(), "d02": set()}
    for night in planned:
        config = NightConfig.model_validate(yaml.safe_load(night.text))
        digests[night.deck].add(deal(config, seed=secrets_[night.deck]).digest())
    assert [len(d) for d in digests.values()] == [1, 1], (
        "the cards belong to the deck, not to the models"
    )
    assert digests["d01"] != digests["d02"]


# ------------------------------------------------------------------------ writing the files


def test_a_file_that_is_already_right_is_left_alone(workdir: Path) -> None:
    _, public, _ = commit(workdir)
    planned, _ = seeds.plan_season(TEMPLATE, public, rotations=2)
    target = workdir / "configs"
    assert len(seeds.write_planned(target, planned, overwrite=False)) == 4
    assert seeds.write_planned(target, planned, overwrite=False) == []
    changed = target / "s1-d01-r1.yaml"
    changed.write_text(changed.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
    gone = target / "s1-d02-r0.yaml"
    gone.unlink()
    with pytest.raises(seeds.SeedsError, match="already exists and differs"):
        seeds.write_planned(target, planned, overwrite=False)
    assert not gone.exists(), "nothing is written when one file is refused"
    assert len(seeds.write_planned(target, planned, overwrite=True)) == 2
    assert changed.read_text(encoding="utf-8") == planned[1].text and gone.is_file()
    assert not list(target.glob("*.part"))


def test_plan_prints_the_nights_and_the_variables(workdir: Path) -> None:
    path, _, _ = commit(workdir)
    args = ["plan", "--template", TEMPLATE, "--commitments", path, "--rotations", "2"]
    result = seeds_cli(*args)
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == f"season s1: 4 nights in {path.parent / 'configs'}"
    assert lines[1] == "  s1-d01-r0  deck d01  rotation 0  seed from AUCTION_NIGHT_SEED_S1_D01"
    assert lines[4] == "  s1-d02-r1  deck d02  rotation 1  seed from AUCTION_NIGHT_SEED_S1_D02"
    assert "wrote 4 files, 0 were already there" in lines
    assert "note: the inline night.seed of the template is not copied into the plan" in lines
    assert sorted(p.name for p in (path.parent / "configs").iterdir()) == [
        f"s1-d0{d}-r{r}.yaml" for d in (1, 2) for r in (0, 1)
    ]
    assert "wrote 0 files, 4 were already there" in seeds_cli(*args).output
    assert load_config(path.parent / "configs" / "s1-d02-r1.yaml").season.rotation == 1


def test_plan_can_be_limited_to_some_decks_and_rotations(workdir: Path) -> None:
    path, _, _ = commit(workdir, decks=3)
    out = workdir / "mine"
    args = ["plan", "-t", TEMPLATE, "-c", path, "-o", out, "--deck", "d03", "--deck", "d01"]
    result = seeds_cli(*args, "--rotations", "1")
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in out.iterdir()) == ["s1-d01-r0.yaml", "s1-d03-r0.yaml"]
    season = load_config(out / "s1-d03-r0.yaml").season
    assert [d.id for d in season.decks] == ["d01", "d02", "d03"], "the whole season is listed"


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--deck", "d09"], "not in the commitments of season s1: d09"),
        (["--rotations", "7"], "rotations is a whole number from 1 to 6"),
    ],
)
def test_plan_refuses_what_the_season_does_not_have(
    workdir: Path, extra: list[str], message: str
) -> None:
    path, _, _ = commit(workdir)
    result = seeds_cli("plan", "-t", TEMPLATE, "-c", path, "-o", workdir / "o", *extra)
    assert result.exit_code == 2 and f"auction-night seeds plan: {message}" in result.output
    assert not (workdir / "o").exists()


def test_plan_checks_its_numbers_and_its_files(workdir: Path) -> None:
    path, _, _ = commit(workdir)
    for value in ("0", "9", "x"):
        bad = seeds_cli("plan", "-t", TEMPLATE, "-c", path, "--rotations", value)
        assert bad.exit_code == 2 and "Invalid value" in plain(bad.output)
    missing = seeds_cli("plan", "-t", workdir / "none.yaml", "-c", path)
    assert missing.exit_code == 2 and "cannot read the template" in missing.output
    nothing = seeds_cli("plan", "-t", TEMPLATE, "-c", workdir / "none.json")
    assert nothing.exit_code == 2 and "commitments file not found" in nothing.output
    assert seeds_cli("plan").exit_code == 2


# --------------------------------------------------------------------- templates that fail


def test_a_template_with_a_person_in_it_is_refused(workdir: Path) -> None:
    _, public, _ = commit(workdir)
    raw = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    raw["seats"][2] = {"seat": 3, "codename": raw["seats"][2]["codename"], "kind": "human"}
    template = workdir / "human.yaml"
    template.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    with pytest.raises(seeds.SeedsError, match="human seat"):
        seeds.plan_season(template, public)


def test_a_template_without_model_seats_is_refused(workdir: Path) -> None:
    _, public, _ = commit(workdir)
    raw = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    raw["seats"] = raw["seats"][6:]
    raw["seats"][0]["seat"], raw["seats"][1]["seat"] = 1, 2
    raw["night"]["missions"].update(collectors=0, shills=0)
    template = workdir / "bots.yaml"
    template.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    with pytest.raises(seeds.SeedsError, match="no model seat"):
        seeds.plan_season(template, public)


def test_a_template_that_is_not_a_valid_configuration_is_refused(workdir: Path) -> None:
    _, public, _ = commit(workdir)
    text = TEMPLATE.read_text(encoding="utf-8")
    broken = workdir / "broken.yaml"
    broken.write_text(text.replace("lots_per_format: 2", "lots_per_format: 99"), encoding="utf-8")
    with pytest.raises(seeds.SeedsError, match="cannot read the template"):
        seeds.plan_season(broken, public)
    twice = workdir / "twice.yaml"
    twice.write_text(text.replace("season:", "season:\n  id: a\n  id: b", 1), encoding="utf-8")
    with pytest.raises(seeds.SeedsError, match="duplicate key"):
        seeds.plan_season(twice, public)


# ----------------------------------------------------------------------------- the variable


def test_the_variable_of_a_deck_is_made_from_the_season_and_the_deck() -> None:
    assert seeds.seed_env_name("s1", "d03") == "AUCTION_NIGHT_SEED_S1_D03"
    example = yaml.safe_load(API_EXAMPLE.read_text(encoding="utf-8"))
    assert example["night"]["seed_env"] == seeds.seed_env_name("s1", "d03")
    assert seeds.seed_env_name("s1.beta", "d-1") == "AUCTION_NIGHT_SEED_S1_BETA_D_1"
    with pytest.raises(seeds.SeedsError, match="too long"):
        seeds.seed_env_name("s" * 30, "d" * 30)


def test_the_example_names_its_night_the_way_plan_does() -> None:
    example = yaml.safe_load(API_EXAMPLE.read_text(encoding="utf-8"))
    season = example["season"]
    assert example["night"]["id"] == f"{season['id']}-{season['deck']}-r{season['rotation']}"


def test_two_decks_may_not_share_a_variable(workdir: Path) -> None:
    private, public = seeds.new_season("s1", ["a-1", "a_1"], created_utc=STAMP)
    assert private.by_deck.keys() == {"a-1", "a_1"}
    with pytest.raises(seeds.SeedsError, match="same variable name"):
        seeds.plan_season(TEMPLATE, public)
