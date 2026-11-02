"""The three documents are read strictly, and no error message holds a seed."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from seeds_support import STAMP, TEMPLATE, Season, edit_json, read_json, seeds_cli, write_json

from auction_night import seeds
from auction_night.contracts import NightConfig

COMMITMENT = "sha256:" + "ab" * 32


def copy_of(source: Path, workdir: Path) -> Path:
    target = workdir / source.name
    target.write_bytes(source.read_bytes())
    target.chmod(0o600)
    return target


# ------------------------------------------------------------------ the public documents


def break_extra_field(data: Any) -> None:
    data["comment"] = "added by hand"


def break_nested_extra_field(data: Any) -> None:
    data["decks"][0]["note"] = "x"


def break_schema(data: Any) -> None:
    data["schema"] = "auction-night/season-commitments@2"


def break_algorithm(data: Any) -> None:
    data["algorithm"] = "md5"


def break_commitment_format(data: Any) -> None:
    data["decks"][0]["commitment"] = "sha256:XYZ"


def break_shared_commitment(data: Any) -> None:
    data["decks"][1]["commitment"] = data["decks"][0]["commitment"]


def break_repeated_deck(data: Any) -> None:
    data["decks"][1]["deck"] = data["decks"][0]["deck"]


def break_no_decks(data: Any) -> None:
    data["decks"] = []


def break_season_id(data: Any) -> None:
    data["season"] = "has space"


def break_time(data: Any) -> None:
    data["created_utc"] = "yesterday"


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        (break_extra_field, "comment: Extra inputs are not permitted"),
        (break_nested_extra_field, "decks.0.note: Extra inputs are not permitted"),
        (break_schema, "is not an auction-night/season-commitments@1 document"),
        (break_algorithm, "algorithm: Input should be 'sha256'"),
        (break_commitment_format, "decks.0.commitment: String should match pattern"),
        (break_shared_commitment, "two decks share a commitment"),
        (break_repeated_deck, "a deck appears twice"),
        (break_no_decks, "decks: List should have at least 1 item"),
        (break_season_id, "season: String should match pattern"),
        (break_time, "created_utc: String should match pattern"),
    ],
)
def test_a_commitments_file_that_is_not_exactly_the_format_is_not_used(
    mid_season: Season, workdir: Path, damage: Callable[[Any], None], message: str
) -> None:
    path = copy_of(mid_season.commitments, workdir)
    edit_json(path, damage)
    result = seeds_cli("verify", "--commitments", path)
    assert result.exit_code == 2 and message in result.output, result.output
    assert "commitments file" in result.output and "cannot be used" in result.output


def test_a_file_that_is_not_json_or_repeats_a_key_is_not_used(workdir: Path) -> None:
    path = workdir / "commitments.json"
    for text, message in (
        ("", "is not valid JSON"),
        ("{", "is not valid JSON"),
        ('{"schema": 1, "schema": 2}', "duplicate key 'schema'"),
        ('{"decks": [NaN]}', "NaN is not allowed in JSON here"),
        ("[]", "document: Input should be a valid dictionary"),
    ):
        path.write_text(text, encoding="utf-8")
        result = seeds_cli("verify", "--commitments", path)
        assert result.exit_code == 2 and message in result.output, (text, result.output)
    path.write_bytes(b"\xff\xfe")
    assert seeds_cli("verify", "--commitments", path).exit_code == 2


def test_a_seeds_file_is_read_as_strictly(end_of_season: Season, workdir: Path) -> None:
    path = copy_of(end_of_season.revealed, workdir)
    base = ["verify", "--commitments", end_of_season.commitments, "--seeds", path]
    assert seeds_cli(*base).exit_code == 0
    edit_json(path, lambda data: data["decks"][0].update(seed="not a seed"))
    bad = seeds_cli(*base)
    assert bad.exit_code == 2 and "decks.0.seed: String should match pattern" in bad.output
    edit_json(path, lambda data: data["decks"][0].update(seed="x" * 129))
    assert seeds_cli(*base).exit_code == 2
    write_json(path, read_json(end_of_season.commitments))  # the other document
    wrong = seeds_cli(*base)
    assert (
        wrong.exit_code == 2 and "is not an auction-night/season-seeds@1 document" in wrong.output
    )


# ------------------------------------------------------------------- the private document


def test_a_private_file_that_contradicts_itself_is_not_used(
    mid_season: Season, workdir: Path
) -> None:
    path = copy_of(mid_season.private, workdir)
    other = mid_season.seeds["d02"]
    edit_json(path, lambda data: data["decks"][0].update(seed=other))
    for args in (["env", "--deck", "d01"], ["reveal", "-c", mid_season.commitments]):
        result = seeds_cli(args[0], "--private", path, *args[1:])
        assert result.exit_code == 2, result.output
        assert "two decks share a seed" in result.output or "does not match its commitment" in (
            result.output
        )
    assert not any(seed in result.output for seed in mid_season.seeds.values())


def test_a_private_file_must_be_the_private_document(mid_season: Season, workdir: Path) -> None:
    path = workdir / "pub.json"
    path.write_bytes(mid_season.commitments.read_bytes())
    path.chmod(0o600)
    result = seeds_cli("env", "--private", path, "--deck", "d01")
    assert result.exit_code == 2 and "season-seeds-private@1 document" in result.output


def test_no_message_holds_a_seed(mid_season: Season, end_of_season: Season, workdir: Path) -> None:
    """Whatever goes wrong, the text for the person never shows one of the seeds."""
    secrets_ = list(mid_season.seeds.values())
    shown = []
    broken = copy_of(mid_season.private, workdir)
    broken.write_text(broken.read_text(encoding="utf-8").replace('"seed"', '"sead"', 1), "utf-8")
    shown.append(seeds_cli("env", "--private", broken, "--deck", "d01").output)
    broken.write_text("{" + broken.read_text(encoding="utf-8"), encoding="utf-8")
    shown.append(seeds_cli("env", "--private", broken, "--deck", "d01").output)
    shown.append(seeds_cli("env", "--private", mid_season.private, "--deck", "d99").output)
    other = copy_of(end_of_season.revealed, workdir)
    edit_json(other, lambda data: data["decks"][0].update(seed=data["decks"][0]["seed"] + "!"))
    shown.append(
        seeds_cli("verify", "--commitments", end_of_season.commitments, "-s", other).output
    )
    edit_json(other, lambda data: data["decks"][0].update(seed=data["decks"][1]["seed"]))
    args = ("verify", "--commitments", end_of_season.commitments, "-s", other)
    shown.append(seeds_cli(*args, end_of_season.season_dir).output)
    assert all(shown) and not any(seed in text for text in shown for seed in secrets_)
    assert not any(seed in text for text in shown for seed in end_of_season.seeds.values())


def test_a_template_that_holds_a_seed_does_not_repeat_it_in_its_error(
    end_of_season: Season, workdir: Path
) -> None:
    seed = end_of_season.seeds["d01"]
    bad = workdir / "template.yaml"
    bad.write_text(f"night: {{seed: '{seed}', seed_env: SOME_VARIABLE}}\n", encoding="utf-8")
    result = seeds_cli("plan", "-t", bad, "-c", end_of_season.commitments)
    assert result.exit_code == 2 and "cannot read the template" in result.output
    assert seed not in result.output


# --------------------------------------------------------------------------- small pieces


def test_a_planned_configuration_may_carry_the_revealed_seed_but_not_before() -> None:
    private, public = seeds.new_season("s1", ["d01", "d02"], created_utc=STAMP)
    planned, _ = seeds.plan_season(TEMPLATE, public, rotations=1)
    raw = yaml.safe_load(planned[0].text)
    del raw["night"]["seed_env"]
    raw["night"]["seed"] = private.by_deck["d01"]
    inline = NightConfig.model_validate(raw)
    revealed = seeds.reveal_season(private, public, revealed_utc=STAMP)
    assert [c.status for c in seeds.check_config(inline, public, None)] == ["ok", "fail"]
    assert [c.status for c in seeds.check_config(inline, public, revealed)] == ["ok"]
    assert [c.status for c in seeds.check_reveal(public, revealed)] == ["ok", "ok"]


def test_time_stamps_are_utc_and_real() -> None:
    assert seeds.parse_stamp("2026-11-01T12:00:00Z") == "2026-11-01T12:00:00Z"
    assert seeds.parse_stamp("2026-11-01T12:00:00.123Z").endswith(".123Z")
    for bad in (
        "2026-11-01",
        "2026-11-01T12:00:00",
        "2026-11-01T12:00:00+00:00",
        "2026-02-30T00:00:00Z",
    ):
        with pytest.raises(seeds.SeedsError):
            seeds.parse_stamp(bad)
    assert seeds.parse_stamp(seeds.now_stamp())


def test_documents_are_written_with_a_final_newline_and_stable_text(workdir: Path) -> None:
    private, public = seeds.new_season("s1", ["d01"], created_utc=STAMP)
    seeds.write_public(workdir / "c.json", public)
    text = (workdir / "c.json").read_text(encoding="utf-8")
    assert text.endswith("}\n") and not text.endswith("\n\n")
    assert json.loads(text)["decks"][0]["commitment"] == public.by_deck["d01"]
    again = seeds.read_commitments(workdir / "c.json")
    seeds.write_public(workdir / "d.json", again)
    assert (workdir / "d.json").read_text(encoding="utf-8") == text, "read and write agree"
    with pytest.raises(seeds.SeedsError, match="already exists"):
        seeds.write_public(workdir / "d.json", again)
    assert not list(workdir.glob("*.part"))
