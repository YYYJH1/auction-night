"""A season from ``seeds new`` to ``seeds verify``, played through the command line.

The shared fixtures (``tests/seeds/conftest.py``) do the whole story with the commands and check
the exit status of each step: ``new`` (two decks), ``plan`` (two rotations of each), ``env`` and
``run`` for two mock nights, ``export``, and later ``reveal`` and ``export --reveal-seed``. The
tests below read what that left on disk and run ``seeds verify`` on it.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from seeds_support import (
    LATER,
    NIGHTS,
    STAMP,
    Season,
    cli,
    parse_exports,
    plain,
    read_json,
    seeds_cli,
    seeds_in_environment,
    verify_args,
)

from auction_night import seeds
from auction_night.contracts import event_line, load_config, loads_events
from auction_night.contracts.events import DealPrivateEvent
from auction_night.engine.deal import deal

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def files_under(directory: Path) -> list[Path]:
    return sorted(path for path in directory.rglob("*") if path.is_file())


# ------------------------------------------------------------------------------- the story


def test_the_season_left_the_files_the_documentation_describes(mid_season: Season) -> None:
    names = sorted(
        path.relative_to(mid_season.season_dir).as_posix()
        for path in files_under(mid_season.season_dir)
    )
    expected = ["commitments.json"]
    expected += [f"configs/{n}.yaml" for n in sorted((*NIGHTS, "s1-d01-r1", "s1-d02-r0"))]
    for night in NIGHTS:
        expected += [
            f"{night}/{name}"
            for name in ("calls.jsonl", "config.yaml", "events.jsonl", "replay.json")
        ]
    assert names == sorted(expected)
    assert mid_season.private.relative_to(mid_season.root).parts[0] == "private"
    assert not mid_season.revealed.exists()


def test_before_the_reveal_the_commitments_and_the_nights_are_checked(mid_season: Season) -> None:
    result = seeds_cli(*verify_args(mid_season))
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "season s1: 2 decks committed, not revealed yet"
    assert lines[1] == "  ok    commitments.json is well formed"
    for night, deck in NIGHTS.items():
        commitment = seeds.read_commitments(mid_season.commitments).by_deck[deck][:19]
        heading = f"night {night} ({mid_season.night(night)})"
        assert lines[lines.index(heading) + 1 :][:4] == [
            f"  ok    events.jsonl carries {commitment}…, published for deck {deck}",
            f"  ok    config.yaml carries {commitment}…, published for deck {deck}",
            f"  ok    replay.json carries {commitment}…, published for deck {deck}",
            "  skip  cards not recomputed: the seed of the deck is not revealed",
        ]
    assert lines[-4:-1] == [
        "decks",
        "  note  deck d01: 1 night given (rotations 0)",
        "  note  deck d02: 1 night given (rotations 1)",
    ]
    assert lines[-1] == "9 checks: 7 ok, 0 failed, 2 skipped"


def test_after_the_reveal_every_card_is_drawn_again_and_matches(end_of_season: Season) -> None:
    result = seeds_cli(*verify_args(end_of_season, revealed=True))
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "season s1: 2 decks committed and revealed"
    assert lines[1].startswith("  ok    deck d01: the revealed seed opens sha256:")
    for night in NIGHTS:
        start = lines.index(f"night {night} ({end_of_season.night(night)})")
        shown = lines[start + 1 : start + 7]
        assert all(line.startswith("  ok    ") for line in shown)
        assert shown[3].startswith("  ok    the seed revealed in replay.json opens sha256:")
        size = "the cards recomputed from the seed (8 seats, 12 lots)"
        assert shown[4] == f"  ok    events.jsonl matches {size}"
        assert shown[5] == f"  ok    replay.json matches {size}"
    assert lines[-1] == "14 checks: 14 ok, 0 failed, 0 skipped"


def test_a_directory_of_nights_and_a_replay_file_are_nights_too(end_of_season: Season) -> None:
    common = [
        "verify",
        "--commitments",
        end_of_season.commitments,
        "--seeds",
        end_of_season.revealed,
    ]
    whole = seeds_cli(*common, end_of_season.season_dir)  # also holds configs/, which are no nights
    assert whole.exit_code == 0, whole.output
    assert whole.output.splitlines()[-1] == "14 checks: 14 ok, 0 failed, 0 skipped"
    replay = end_of_season.night("s1-d01-r0") / "replay.json"
    alone = seeds_cli(*common, replay)  # the config.yaml beside it brings the economy
    assert alone.exit_code == 0, alone.output
    assert alone.output.splitlines()[-1] == "6 checks: 6 ok, 0 failed, 0 skipped"
    assert "replay.json matches the cards recomputed" in alone.output
    assert "events.jsonl matches" not in alone.output
    assert "committed, and no night of it was given" in alone.output  # deck d02


def test_strict_verification_wants_every_file(end_of_season: Season, tmp_path: Path) -> None:
    strict = seeds_cli(*verify_args(end_of_season, "--strict", revealed=True))
    assert strict.exit_code == 0, strict.output
    bare = seeds_cli("verify", "--commitments", end_of_season.commitments, "--strict")
    assert bare.exit_code == 2 and "--strict needs --seeds" in bare.output
    copy = end_of_season.copy_to(tmp_path / "tree")
    (copy.night("s1-d01-r0") / "config.yaml").unlink()  # the economy is gone: no cards to draw
    lax = seeds_cli(*verify_args(copy, revealed=True))
    assert lax.exit_code == 0 and "skip  cards not recomputed: no config.yaml" in lax.output
    harsh = seeds_cli(*verify_args(copy, "--strict", revealed=True))
    assert harsh.exit_code == 1 and "FAIL  cards not recomputed: no config.yaml" in harsh.output


def test_verify_lists_the_decks_that_no_night_was_given_for(end_of_season: Season) -> None:
    args = ["verify", "-c", end_of_season.commitments, "-s", end_of_season.revealed]
    result = seeds_cli(*args, end_of_season.night("s1-d02-r1"))
    assert result.exit_code == 0, result.output
    assert "  note  deck d01: committed, and no night of it was given" in result.output
    assert "  note  deck d02: 1 night given (rotations 1)" in result.output


def test_verify_refuses_what_it_cannot_read(end_of_season: Season, tmp_path: Path) -> None:
    base = ["verify", "--commitments", end_of_season.commitments]
    nothing = seeds_cli(*base, tmp_path)
    assert nothing.exit_code == 2 and "no night in or under" in nothing.output
    absent = seeds_cli(*base, tmp_path / "nowhere")
    assert absent.exit_code == 2 and "not a file or directory" in absent.output
    wrong = seeds_cli(
        "verify", "--commitments", end_of_season.revealed
    )  # seeds.json is no such file
    assert (
        wrong.exit_code == 2 and "this is not an auction-night/season-commitments@1" in wrong.output
    )
    gone = seeds_cli("verify", "--commitments", tmp_path / "none.json")
    assert gone.exit_code == 2 and "commitments file not found" in gone.output
    assert seeds_cli("verify").exit_code == 2


# ------------------------------------------------------------------- the seed stays secret


def test_the_seed_is_in_no_file_of_a_night(mid_season: Season) -> None:
    tree = files_under(mid_season.season_dir)
    assert len(tree) > 10
    for seed in mid_season.seeds.values():
        needle = seed.encode("utf-8")
        assert [p.name for p in tree if needle in p.read_bytes()] == []
    config = load_config(mid_season.night("s1-d01-r0") / "config.yaml")
    assert config.night.seed is None and config.night.seed_env == "AUCTION_NIGHT_SEED_S1_D01"
    text = (mid_season.night("s1-d01-r0") / "replay.json").read_text(encoding="utf-8")
    assert (
        read_json(mid_season.night("s1-d01-r0") / "replay.json")["manifest"]["revealed_seed"]
        is None
    )
    assert "seed_commitment" in text


def test_after_the_reveal_only_the_published_files_hold_the_seeds(end_of_season: Season) -> None:
    holders = {}
    for deck, seed in end_of_season.seeds.items():
        holders[deck] = sorted(
            p.relative_to(end_of_season.season_dir).as_posix()
            for p in files_under(end_of_season.season_dir)
            if seed.encode("utf-8") in p.read_bytes()
        )
    reveals = {"d01": "s1-d01-r0", "d02": "s1-d02-r1"}
    for deck, night in reveals.items():
        assert holders[deck] == sorted(["seeds.json", f"{night}/replay.json"])
    replay = read_json(end_of_season.night("s1-d01-r0") / "replay.json")
    assert replay["manifest"]["revealed_seed"] == end_of_season.seeds["d01"]
    assert (
        replay["manifest"]["seed_commitment"]
        == seeds.read_commitments(end_of_season.commitments).by_deck["d01"]
    )


def test_a_night_cannot_be_run_or_replayed_with_another_seed(
    mid_season: Season, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = mid_season.season_dir / "configs" / "s1-d01-r0.yaml"
    monkeypatch.delenv("AUCTION_NIGHT_SEED_S1_D01", raising=False)
    missing = cli("run", "--config", config, "--out", tmp_path / "a")
    assert (
        missing.exit_code == 2
        and "environment variable AUCTION_NIGHT_SEED_S1_D01 is not set" in missing.output
    )
    monkeypatch.setenv("AUCTION_NIGHT_SEED_S1_D01", "f" * 64)
    wrong = cli("run", "--config", config, "--out", tmp_path / "b")
    assert wrong.exit_code == 2
    assert "night.deal_seed_commitment: does not match the seed in the variable" in wrong.output
    assert "f" * 64 not in wrong.output
    assert not (tmp_path / "a").exists() and not (tmp_path / "b").exists()
    replay = cli("replay", "--from", mid_season.night("s1-d01-r0"))
    assert replay.exit_code == 2 and "does not match the seed" in replay.output


def test_a_night_replays_when_the_seed_is_in_the_environment(mid_season: Season) -> None:
    night = mid_season.night("s1-d01-r0")
    with seeds_in_environment(mid_season.private, "d01"):
        result = cli("replay", "--from", night)
    assert result.exit_code == 0 and "replay matches" in result.output, result.output


# ------------------------------------------------------------------------------ env, reveal


def test_env_prints_one_export_line_for_each_deck(mid_season: Season) -> None:
    result = seeds_cli("env", "--private", mid_season.private, "--deck", "d02", "--deck", "d01")
    assert result.exit_code == 0, result.output
    assert parse_exports(result.output) == {
        "AUCTION_NIGHT_SEED_S1_D02": mid_season.seeds["d02"],
        "AUCTION_NIGHT_SEED_S1_D01": mid_season.seeds["d01"],
    }
    assert [line.split("=")[0] for line in result.output.splitlines()] == [
        "export AUCTION_NIGHT_SEED_S1_D02",
        "export AUCTION_NIGHT_SEED_S1_D01",
    ]


@needs_bash
def test_a_shell_can_evaluate_the_line(mid_season: Season) -> None:
    result = seeds_cli("env", "--private", mid_season.private, "--deck", "d01")
    script = result.output + 'printf %s "$AUCTION_NIGHT_SEED_S1_D01"\n'
    shown = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    assert shown.stdout == mid_season.seeds["d01"]


def test_env_names_the_decks_it_exports(mid_season: Season) -> None:
    none = seeds_cli("env", "--private", mid_season.private)
    assert none.exit_code == 2 and "name at least one deck with --deck" in none.output
    unknown = seeds_cli("env", "--private", mid_season.private, "--deck", "d09")
    assert unknown.exit_code == 2 and "the private file has no deck 'd09'" in unknown.output
    assert not any(seed in unknown.output for seed in mid_season.seeds.values())
    absent = seeds_cli("env", "--private", mid_season.root / "none.json", "--deck", "d01")
    assert absent.exit_code == 2 and "private seed file not found" in absent.output


def test_reveal_publishes_the_seeds_that_open_the_commitments(end_of_season: Season) -> None:
    revealed = read_json(end_of_season.revealed)
    assert list(revealed) == ["schema", "season", "revealed_utc", "decks"]
    assert revealed["schema"] == "auction-night/season-seeds@1"
    assert (revealed["season"], revealed["revealed_utc"]) == ("s1", LATER)
    assert revealed["decks"] == [
        {"deck": deck, "seed": seed} for deck, seed in end_of_season.seeds.items()
    ]
    public = seeds.read_commitments(end_of_season.commitments).by_deck
    assert all(seeds.seed_commitment(e["seed"]) == public[e["deck"]] for e in revealed["decks"])


def test_reveal_says_what_it_did_and_does_not_replace_a_file(end_of_season: Season) -> None:
    args = ["reveal", "--private", end_of_season.private, "-c", end_of_season.commitments]
    refused = seeds_cli(*args)
    assert refused.exit_code == 2 and "seeds.json already exists (use --overwrite" in refused.output
    before = end_of_season.revealed.read_bytes()
    again = seeds_cli(*args, "--overwrite", "--revealed-utc", LATER)
    assert again.exit_code == 0, again.output
    assert "season s1: all 2 seeds open their commitments" in again.output
    assert again.output.splitlines()[-1].endswith(
        "publish it only after the last night of the season"
    )
    assert end_of_season.revealed.read_bytes() == before


def new_private(workdir: Path, season: str, decks: str) -> Path:
    path = workdir / f"{season}-{decks}.seeds-private.json"
    made = seeds_cli(
        "new",
        "--season",
        season,
        "--decks",
        decks,
        "--private-out",
        path,
        "--out",
        workdir / "c.json",
    )
    assert made.exit_code == 0, made.output
    return path


def test_reveal_refuses_seeds_that_do_not_open_the_commitments(
    mid_season: Season, workdir: Path
) -> None:
    other = new_private(workdir, "s1", "2")  # the same decks, but other seeds
    out = workdir / "seeds.json"
    result = seeds_cli("reveal", "--private", other, "-c", mid_season.commitments, "-o", out)
    assert result.exit_code == 1
    assert "auction-night seeds reveal: the seed of deck d01 does not open sha256:" in result.output
    assert not out.exists(), "a reveal is complete or it is not made"


@pytest.mark.parametrize(
    ("season", "decks", "message"),
    [
        ("s2", "2", "the private file is of season 's2' and the commitments of 's1'"),
        ("s1", "3", "the decks differ: not committed: d03"),
        ("s1", "1", "the decks differ: no seed for d02"),
    ],
)
def test_reveal_refuses_files_that_are_not_of_one_season(
    mid_season: Season, workdir: Path, season: str, decks: str, message: str
) -> None:
    other = new_private(workdir, season, decks)
    out = workdir / "seeds.json"
    result = seeds_cli("reveal", "--private", other, "-c", mid_season.commitments, "-o", out)
    assert result.exit_code == 1 and message in result.output
    assert not out.exists()


def test_reveal_defaults_to_the_directory_of_the_commitments(workdir: Path) -> None:
    private = new_private(workdir, "s1", "2")
    result = seeds_cli(
        "reveal", "--private", private, "-c", workdir / "c.json", "--revealed-utc", STAMP
    )
    assert result.exit_code == 0, result.output
    assert read_json(workdir / "seeds.json")["revealed_utc"] == STAMP


# --------------------------------------------------------------------- the same cards again


def seat_lines(night: Path) -> dict[int, str]:
    """The raw lines of the private cards in a log, by seat."""
    found = {}
    for line in (night / "events.jsonl").read_text(encoding="utf-8").splitlines():
        if '"type":"deal_private"' in line:
            found[json.loads(line)["data"]["seat"]] = line
    return found


@pytest.mark.parametrize("night", list(NIGHTS))
def test_the_cards_are_drawn_again_byte_for_byte(mid_season: Season, night: str) -> None:
    directory = mid_season.night(night)
    seed = mid_season.seeds[NIGHTS[night]]
    config = load_config(directory / "config.yaml")
    logged = seat_lines(directory)
    assert sorted(logged) == list(range(1, 9))
    events = loads_events("\n".join(logged.values()))
    for event in events:
        assert isinstance(event, DealPrivateEvent)
        card = deal(config, seed=seed).card(event.data.seat)
        again = DealPrivateEvent(seq=event.seq, vis=event.vis, data=card)
        assert event_line(again) == logged[event.data.seat], f"seat {event.data.seat}"


@pytest.mark.parametrize("night", list(NIGHTS))
def test_the_deal_does_not_depend_on_the_process(mid_season: Season, night: str) -> None:
    config_file = mid_season.night(night) / "config.yaml"
    seed = mid_season.seeds[NIGHTS[night]]
    expected = deal(load_config(config_file), seed=seed).digest()
    script = (
        "import sys\n"
        "from auction_night.contracts import load_config\n"
        "from auction_night.engine.deal import deal\n"
        "print(deal(load_config(sys.argv[1]), seed=sys.argv[2]).digest())\n"
    )
    for hash_seed in ("0", "1", "12345"):  # Python's own hash() is salted per process
        env = {**os.environ, "PYTHONHASHSEED": hash_seed}
        shown = subprocess.run(
            [sys.executable, "-c", script, str(config_file), seed],
            capture_output=True, text=True, check=True, env=env,
        )  # fmt: skip
        assert shown.stdout.strip() == expected


def test_the_help_of_every_seeds_command_says_what_it_reads_and_writes() -> None:
    expected = {
        "new": (
            "--season",
            "--decks",
            "--deck",
            "--private-out",
            "--out",
            "mode 600",
            "git work tree",
        ),
        "plan": ("--template", "--commitments", "--out-dir", "--rotations", "night.seed_env"),
        "env": ("--private", "--deck", "eval"),
        "reveal": ("--private", "--commitments", "--out", "--revealed-utc", "last night"),
        "verify": ("--commitments", "--seeds", "--strict", "NIGHT", "dealer", "Exit status"),
    }
    for command, words in expected.items():
        result = seeds_cli(command, "--help")
        text = plain(result.output)
        assert result.exit_code == 0, command
        for word in words:
            assert word in text, (command, word)
        bare = seeds_cli(command)
        assert bare.exit_code == 2, command  # every command has something it must be given
    group = seeds_cli("--help")
    assert group.exit_code == 0
    assert all(name in plain(group.output) for name in expected)
