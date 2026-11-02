"""The command line: help, and every command with its exit statuses.

Where the commands are tested: estimate in tests/llm/test_llm_accounting.py and
tests/llm/test_llm_estimate_show.py, season in more depth in tests/season, show in
tests/show/test_show_showrun.py, and the rest below. The export itself is tested in tests/show.
"""

import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

import auction_night.cli as cli
from auction_night.cli import app
from auction_night.contracts import Replay

COMMANDS = ["run", "replay", "play", "simulate", "estimate", "season", "export", "show"]

runner = CliRunner()


def plain(text: str) -> str:
    """Output without colour codes. On CI typer styles its help and its usage errors, and the
    escape codes can split an option name, which a local run does not show."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def test_help_lists_every_command() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in COMMANDS:
        assert name in plain(result.output)


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "Usage" in plain(result.output)
    assert "run" in plain(result.output)


def test_every_command_has_help_and_none_is_a_stub() -> None:
    registered = [command.name or command.callback.__name__ for command in app.registered_commands]
    assert registered == COMMANDS  # a command added without a test here is found
    assert not hasattr(cli, "_STUB") and not hasattr(cli, "_not_implemented")
    for name in COMMANDS:
        helped = runner.invoke(app, [name, "--help"])
        text = plain(helped.output)
        assert helped.exit_code == 0 and "Usage" in text, name
        assert "not implemented" not in text, name
        bare = runner.invoke(app, [name])  # no arguments: a usage error, never a notice
        assert bare.exit_code == 2 and "not implemented" not in plain(bare.output), name


SEEDS_COMMANDS = ["new", "plan", "env", "reveal", "verify"]


def test_seeds_is_a_group_whose_commands_are_all_there() -> None:
    """The group is tested in depth in tests/seeds. Here: it is listed, and no command is a stub."""
    assert "seeds" in [group.name for group in app.registered_groups]
    assert "seeds" in plain(runner.invoke(app, ["--help"]).output)
    group = next(g for g in app.registered_groups if g.name == "seeds")
    registered = [c.name or c.callback.__name__ for c in group.typer_instance.registered_commands]
    assert registered == SEEDS_COMMANDS  # a command added without a test in tests/seeds is found
    listing = plain(runner.invoke(app, ["seeds", "--help"]).output)
    for name in SEEDS_COMMANDS:
        helped = runner.invoke(app, ["seeds", name, "--help"])
        assert helped.exit_code == 0 and "Usage" in plain(helped.output), name
        assert name in listing and "not implemented" not in plain(helped.output), name
        bare = runner.invoke(app, ["seeds", name])  # nothing given: a usage error
        assert bare.exit_code == 2 and "not implemented" not in plain(bare.output), name


# ----------------------------------------------------------------------------- simulate

CONFIGS = Path(__file__).resolve().parents[1] / "configs"
MOCK_CONFIG = CONFIGS / "night_mock.yaml"
SAMPLE = Path(__file__).resolve().parent / "fixtures" / "replays" / "mock_night.json"


def test_simulate_prints_the_tables_of_a_preset() -> None:
    result = runner.invoke(app, ["simulate", "--preset", "s2", "--nights", "4"])
    assert result.exit_code == 0
    assert result.output.splitlines()[0] == "s2: 4 nights, seed s2, missions chased"
    for text in ("mean surplus", "P-E", "P-S", "collector", "shill"):
        assert text in result.output


def test_simulate_json_is_the_same_report_and_is_reproducible() -> None:
    args = ["simulate", "--preset", "s1-c300", "--nights", "5", "--seed", "cli", "--json"]
    first = runner.invoke(app, args)
    assert first.exit_code == 0
    data = json.loads(first.output)
    assert (data["source"], data["nights"], data["seed"]) == ("s1-c300", 5, "cli")
    assert list(data["styles"]) == ["E", "O", "S", "P", "T"]
    assert list(data["missions"]) == ["collector", "shill"]
    assert runner.invoke(app, args).output == first.output


def test_simulate_reads_a_configuration_file() -> None:
    args = ["simulate", "--config", str(MOCK_CONFIG), "--nights", "3", "--no-chase"]
    result = runner.invoke(app, args)
    assert result.exit_code == 0
    assert result.output.splitlines()[0] == "mock-0001: 3 nights, seed simulation, missions ignored"


def test_simulate_gives_the_same_report_with_more_processes() -> None:
    args = ["simulate", "--preset", "s2", "--nights", "4", "--seed", "jobs", "--json"]
    assert runner.invoke(app, [*args, "--jobs", "2"]).output == runner.invoke(app, args).output


@pytest.mark.parametrize(
    "args, message",
    [
        ([], "give exactly one of --config and --preset"),
        (["--preset", "s2", "--config", str(MOCK_CONFIG)], "give exactly one of"),
        (["--preset", "s9"], "unknown preset 's9': use one of s1-c300, s2"),
        (["--config", "no-such-file.yaml"], "configuration not found: no-such-file.yaml"),
        (["--config", str(CONFIGS / "prices.example.yaml")], "cannot read"),
        (["--preset", "s2", "--seed", "two words"], "the seed is not empty"),
    ],
)
def test_simulate_errors_exit_2_with_a_message(args: list[str], message: str) -> None:
    result = runner.invoke(app, ["simulate", *args])
    assert result.exit_code == 2
    assert "auction-night simulate: " in result.output and message in result.output


@pytest.mark.parametrize("option", ["--nights", "--jobs"])
def test_simulate_counts_must_be_positive(option: str) -> None:
    result = runner.invoke(app, ["simulate", "--preset", "s2", option, "0"])
    assert result.exit_code == 2
    assert "Invalid value" in plain(result.output)


# --------------------------------------------------------------------------- run and replay

MOCK_MODELS = ("mock-honest", "mock-collude", "mock-backstab", "mock-random", "mock-abstain")


def run_args(out: Path, *extra: str, config: Path = MOCK_CONFIG) -> list[str]:
    return ["run", "--config", str(config), "--out", str(out), *extra]


def replay_args(source: Path, config: Path = MOCK_CONFIG) -> list[str]:
    return ["replay", "--config", str(config), "--from", str(source)]


@pytest.fixture(scope="module")
def ran(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    """The mock night run once through the command line: its directory and what it printed."""
    out = tmp_path_factory.mktemp("cli") / "night"
    result = runner.invoke(app, run_args(out))
    assert result.exit_code == 0, result.output
    return out, result.output


def test_run_plays_the_mock_night_and_writes_the_log_and_the_cassette(
    ran: tuple[Path, str],
) -> None:
    from auction_night.contracts import loads_events, validate_log

    out, output = ran
    lines = output.splitlines()
    assert lines[0] == "night mock-0001: completed"
    assert lines[1].startswith("events ") and "model attempts" in lines[1]
    names = ["鲸落", "阿铁", "小满", "拾光", "青柠", "麦芽", "老实人", "经济学家"]
    assert [line.split(":")[0] for line in lines[2:10]] == [
        f"  seat {n} {name}" for n, name in enumerate(names, 1)
    ]
    assert lines[-2] == f"saved the configuration as {out / 'config.yaml'}"
    assert lines[-1] == f"wrote {out / 'events.jsonl'} and {out / 'calls.jsonl'}"
    assert sorted(path.name for path in out.iterdir()) == [
        "calls.jsonl",
        "config.yaml",
        "events.jsonl",
    ]
    events = loads_events((out / "events.jsonl").read_text(encoding="utf-8"))
    assert validate_log(events) == [] and events[0].type == "night_start"
    assert f"events {len(events)}," in lines[1]
    assert (out / "calls.jsonl").stat().st_size > 100_000


def test_run_refuses_to_replace_an_earlier_run_unless_told_to(
    ran: tuple[Path, str], tmp_path: Path
) -> None:
    import shutil

    out = tmp_path / "night"
    shutil.copytree(ran[0], out)
    again = runner.invoke(app, run_args(out))
    assert again.exit_code == 2
    assert "auction-night run: " in again.output and "already exists" in again.output
    assert "--overwrite" in again.output
    replaced = runner.invoke(app, run_args(out, "--overwrite"))
    assert replaced.exit_code == 0
    assert (out / "events.jsonl").read_bytes() == (ran[0] / "events.jsonl").read_bytes()


def test_replay_of_a_run_matches_byte_for_byte(ran: tuple[Path, str]) -> None:
    out, _ = ran
    result = runner.invoke(app, replay_args(out))
    assert result.exit_code == 0, result.output
    count = len((out / "events.jsonl").read_text(encoding="utf-8").splitlines())
    assert result.output.strip() == f"night mock-0001: replay matches, {count} events"


def test_replay_reports_a_log_that_differs(ran: tuple[Path, str], tmp_path: Path) -> None:
    import shutil

    out = tmp_path / "night"
    shutil.copytree(ran[0], out)
    path = out / "events.jsonl"
    path.write_text(path.read_text(encoding="utf-8").replace('"seq":9,', '"seq":90,', 1))
    result = runner.invoke(app, replay_args(out))
    assert result.exit_code == 1
    assert "auction-night replay: the replay differs: line 9 differs" in result.output


def test_replay_reports_a_cassette_that_no_longer_fits_the_prompts(
    ran: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import dataclasses
    from types import MappingProxyType

    from auction_night.prompts import load_templates

    def edited(lang: str = "zh") -> object:
        templates = load_templates(lang)
        texts = {**templates.texts, "system.donts": templates.texts["system.donts"] + "再加一句。"}
        return dataclasses.replace(templates, texts=MappingProxyType(texts))

    monkeypatch.setattr("auction_night.llm.render.load_templates", edited)
    result = runner.invoke(app, replay_args(ran[0]))
    assert result.exit_code == 1
    assert "the cassette does not fit this night" in result.output
    assert "differs from the recording" in result.output
    assert "changed fields: system" in result.output


def test_replay_needs_the_files_of_a_run(tmp_path: Path) -> None:
    result = runner.invoke(app, replay_args(tmp_path))
    assert result.exit_code == 2
    assert "auction-night replay: not found: " in result.output


def test_run_and_replay_check_their_configuration(tmp_path: Path) -> None:
    missing = runner.invoke(app, run_args(tmp_path / "a", config=tmp_path / "none.yaml"))
    assert missing.exit_code == 2 and "configuration not found" in missing.output
    broken = runner.invoke(app, run_args(tmp_path / "b", config=CONFIGS / "prices.example.yaml"))
    assert broken.exit_code == 2 and "cannot read" in broken.output
    example = runner.invoke(
        app, run_args(tmp_path / "c", config=CONFIGS / "night_api.example.yaml")
    )
    assert example.exit_code == 2 and "still holds placeholders" in example.output
    for name in ("a", "b", "c"):
        assert not (tmp_path / name).exists()  # nothing is written for a run that cannot start
    assert runner.invoke(app, replay_args(tmp_path, config=tmp_path / "none.yaml")).exit_code == 2


def test_run_needs_an_output_directory_and_a_configuration() -> None:
    assert runner.invoke(app, ["run", "--config", str(MOCK_CONFIG)]).exit_code == 2
    assert runner.invoke(app, ["run", "--out", "somewhere"]).exit_code == 2
    assert runner.invoke(app, ["replay", "--config", str(MOCK_CONFIG)]).exit_code == 2


# ------------------------------------------------------ the configuration saved with a night


def test_run_saves_the_configuration_file_as_it_is(tmp_path: Path) -> None:
    odd = tmp_path / "odd.yaml"  # a comment, non-ASCII text and Windows line ends are all kept
    odd.write_bytes(MOCK_CONFIG.read_bytes().replace(b"\n", b"\r\n") + "# 注释 é\r\n".encode())
    out = tmp_path / "night"
    result = runner.invoke(app, run_args(out, config=odd))
    assert result.exit_code == 0, result.output
    assert (out / "config.yaml").read_bytes() == odd.read_bytes()
    assert runner.invoke(app, ["replay", "--from", str(out)]).exit_code == 0


def test_run_treats_an_earlier_config_yaml_like_the_other_files(tmp_path: Path) -> None:
    out = tmp_path / "night"
    out.mkdir()
    (out / "config.yaml").write_text("stale: true\n", encoding="utf-8")
    refused = runner.invoke(app, run_args(out))
    assert refused.exit_code == 2
    assert "config.yaml already exists" in refused.output and "--overwrite" in refused.output
    assert (out / "config.yaml").read_text(encoding="utf-8") == "stale: true\n"
    assert not (out / "events.jsonl").exists()  # refused before anything was played
    replaced = runner.invoke(app, run_args(out, "--overwrite"))
    assert replaced.exit_code == 0, replaced.output
    assert (out / "config.yaml").read_bytes() == MOCK_CONFIG.read_bytes()


def test_a_night_is_run_again_from_its_own_directory(ran: tuple[Path, str], tmp_path: Path) -> None:
    out = tmp_path / "night"
    shutil.copytree(ran[0], out)
    saved = out / "config.yaml"
    before = saved.read_bytes()
    refused = runner.invoke(app, run_args(out, config=saved))
    assert refused.exit_code == 2 and "events.jsonl already exists" in refused.output
    assert "config.yaml already exists" not in refused.output  # it is the configuration itself
    again = runner.invoke(app, run_args(out, "--overwrite", config=saved))
    assert again.exit_code == 0, again.output
    assert saved.read_bytes() == before
    assert (out / "events.jsonl").read_bytes() == (ran[0] / "events.jsonl").read_bytes()


def test_replay_finds_the_configuration_in_the_directory_of_the_run(ran: tuple[Path, str]) -> None:
    result = runner.invoke(app, ["replay", "--from", str(ran[0])])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == runner.invoke(app, replay_args(ran[0])).output.strip()


def test_replay_prefers_the_configuration_it_is_given(
    ran: tuple[Path, str], tmp_path: Path
) -> None:
    out = tmp_path / "night"
    shutil.copytree(ran[0], out)
    (out / "config.yaml").write_text("not: a configuration\n", encoding="utf-8")
    given = runner.invoke(app, replay_args(out))
    assert given.exit_code == 0, given.output
    saved = runner.invoke(app, ["replay", "--from", str(out)])
    assert saved.exit_code == 2 and "cannot read" in saved.output
    none = runner.invoke(app, replay_args(out, config=tmp_path / "none.yaml"))
    assert none.exit_code == 2 and "configuration not found" in none.output  # no fallback


def test_replay_without_a_configuration_says_what_to_do(tmp_path: Path) -> None:
    result = runner.invoke(app, ["replay", "--from", str(tmp_path)])
    assert result.exit_code == 2
    assert "auction-night replay: no configuration: give --config" in result.output
    assert "config.yaml" in result.output
    nowhere = runner.invoke(app, ["replay", "--from", str(tmp_path / "nowhere")])
    assert nowhere.exit_code == 2 and "auction-night replay: not a directory: " in nowhere.output


def test_run_stops_a_night_that_reaches_its_cost_cap(tmp_path: Path) -> None:
    capped = tmp_path / "capped.yaml"
    text = MOCK_CONFIG.read_text(encoding="utf-8")
    assert "max_cost_per_night_usd: null" in text
    capped.write_text(
        text.replace("max_cost_per_night_usd: null", "max_cost_per_night_usd: 0.000001")
    )
    price = (
        "{input_per_mtok: 1, output_per_mtok: 1, as_of: 2026-11-01, source: https://example.com}"
    )
    prices = tmp_path / "prices.yaml"
    prices.write_text(
        "version: 1\nprices:\n" + "".join(f"  {m}: {price}\n" for m in MOCK_MODELS),
        encoding="utf-8",
    )
    # without prices the cap could not be enforced, so the run does not start
    refused = runner.invoke(app, run_args(tmp_path / "a", config=capped))
    assert refused.exit_code == 2 and "no price" in refused.output
    assert not (tmp_path / "a" / "config.yaml").exists()  # saved once the night is played
    out = tmp_path / "b"
    result = runner.invoke(app, run_args(out, "--prices", str(prices), config=capped))
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == "night mock-0001: budget_aborted"
    replayed = runner.invoke(app, replay_args(out, config=capped))
    assert replayed.exit_code == 0, replayed.output
    missing = runner.invoke(app, run_args(tmp_path / "c", "--prices", str(tmp_path / "none.yaml")))
    assert missing.exit_code == 2 and "price list not found" in missing.output


def test_the_module_entry_point_runs_and_replays_a_night(tmp_path: Path) -> None:
    out = tmp_path / "night"
    for args, expected in (
        (run_args(out), "night mock-0001: completed"),
        (replay_args(out), "replay matches"),
    ):
        proc = subprocess.run(
            [sys.executable, "-m", "auction_night.cli", *args],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert expected in proc.stdout and proc.stderr == ""


def test_unknown_command_is_a_usage_error() -> None:
    result = runner.invoke(app, ["no-such-command"])
    assert result.exit_code == 2
    assert "not implemented yet" not in result.output


def test_module_entry_point_reports_on_stderr_only(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "auction_night.cli", "season", str(tmp_path / "no-such-directory")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "auction-night season: not a directory" in proc.stderr


def test_installed_script_runs() -> None:
    script = Path(sys.executable).parent / "auction-night"
    if not script.exists():
        pytest.skip("console script not installed next to this interpreter")
    proc = subprocess.run([str(script), "--help"], capture_output=True, text=True, check=False)
    assert proc.returncode == 0
    assert "season" in proc.stdout


# ------------------------------------------------------------------------------ variety mode


def variety_config(tmp_path: Path, persona: str = "铁算盘") -> Path:
    """The mock night in variety mode, with the same persona at every mock seat."""
    raw = yaml.safe_load(MOCK_CONFIG.read_text(encoding="utf-8"))
    raw["night"]["mode"] = "variety"
    for seat in raw["seats"]:
        if seat["kind"] == "mock":
            seat["persona"] = persona
    path = tmp_path / "variety.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def test_a_variety_night_is_run_replayed_and_exported_from_the_command_line(
    tmp_path: Path,
) -> None:
    config = variety_config(tmp_path)
    out = tmp_path / "night"
    assert runner.invoke(app, run_args(out, config=config)).exit_code == 0
    assert "【你的人设】铁算盘：" in (out / "calls.jsonl").read_text(encoding="utf-8")
    replayed = runner.invoke(app, replay_args(out, config))
    assert replayed.exit_code == 0 and "replay matches" in replayed.output
    exported = runner.invoke(app, export_args(out, "--no-git"))
    assert exported.exit_code == 0, exported.output
    replay = Replay.model_validate_json((out / "replay.json").read_text(encoding="utf-8"))
    assert replay.night.mode == "variety" and not replay.night.ranked
    assert {seat.persona for seat in replay.seats if seat.kind == "mock"} == {"铁算盘"}


def test_a_night_in_variety_mode_does_not_count_for_the_season(tmp_path: Path) -> None:
    out = tmp_path / "night"
    assert runner.invoke(app, run_args(out, config=variety_config(tmp_path))).exit_code == 0
    result = runner.invoke(app, ["season", str(out)])
    assert result.exit_code == 2
    assert "no night counts: mock-0001 (variety)" in result.output


def test_a_persona_that_is_not_known_is_a_usage_error_and_leaves_no_cassette(
    tmp_path: Path,
) -> None:
    known = tmp_path / "known"
    known.mkdir()
    out = tmp_path / "night"
    assert runner.invoke(app, run_args(out, config=variety_config(known))).exit_code == 0
    unknown = variety_config(tmp_path, persona="没有这个人设")
    fresh = tmp_path / "fresh"
    result = runner.invoke(app, run_args(fresh, config=unknown))
    assert result.exit_code == 2 and "Traceback" not in result.output
    message = "persona '没有这个人设' is not known (known: 铁算盘, 浪里个浪, 影帝)"
    assert f"auction-night run: seat 1: {message}" in plain(result.output)
    assert list(fresh.iterdir()) == []  # neither a cassette nor a log
    replayed = runner.invoke(app, replay_args(out, unknown))
    assert replayed.exit_code == 2 and message in plain(replayed.output)
    exported = runner.invoke(app, export_args(out, "-c", unknown, "--no-git"))
    assert exported.exit_code == 1 and message in plain(exported.output)
    assert not (out / "replay.json").exists()


def test_play_reports_a_persona_that_is_not_known_like_run(tmp_path: Path) -> None:
    out = tmp_path / "night"
    unknown = variety_config(tmp_path, persona="没有这个人设")
    result = runner.invoke(app, play_args(out, unknown), input="")
    assert result.exit_code == 2 and "Traceback" not in result.output
    assert "auction-night play: seat 1: persona '没有这个人设' is not known" in plain(result.output)
    assert list(out.iterdir()) == []  # neither a cassette nor the inputs of a person


# ----------------------------------------------------------------------------------- season


def test_season_prints_the_board_of_a_run(ran: tuple[Path, str]) -> None:
    result = runner.invoke(app, ["season", str(ran[0])])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == "season: single, 1 night counted"
    for text in ("mock-collude", "mock-honest", "老实人 [bot]", "mean surplus [95% CI]"):
        assert text in result.output
    assert "missions" in result.output and "rank intervals need at least two nights" in (
        result.output
    )


def test_season_json_is_the_text_of_the_file_and_reproducible(
    ran: tuple[Path, str], tmp_path: Path
) -> None:
    out = tmp_path / "leaderboard.json"
    args = ["season", str(ran[0]), "--json", "--out", str(out), "--resamples", "200", "--seed", "x"]
    first = runner.invoke(app, args)
    assert first.exit_code == 0, first.output
    assert first.output == out.read_text(encoding="utf-8")
    data = json.loads(first.output)
    assert data["schema"] == "auction-night/leaderboard@1"
    assert (data["method"]["seed"], data["method"]["resamples"]) == ("x", 200)
    assert runner.invoke(app, args).output == first.output
    tables = runner.invoke(app, ["season", str(ran[0]), "--out", str(out)])
    assert tables.exit_code == 0 and tables.output.splitlines()[-1] == f"wrote {out}"


def test_season_ranks_the_nights_of_a_directory(ran: tuple[Path, str], tmp_path: Path) -> None:
    root = tmp_path / "season"
    shutil.copytree(ran[0], root / "n1")
    other = tmp_path / "other.yaml"
    other.write_text(MOCK_CONFIG.read_text(encoding="utf-8").replace("mock-0001", "mock-0002"))
    assert runner.invoke(app, run_args(root / "n2", config=other)).exit_code == 0
    result = runner.invoke(app, ["season", str(root), "--resamples", "300"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == "season: single, 2 nights counted"
    assert "rank: 95% interval over 300 resamples of 2 nights" in result.output
    assert "rank intervals need" not in result.output


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["NONE"], "not a directory"),
        (["EMPTY"], "no events.jsonl in or under"),
        (["RUN", "RUN2"], "appears more than once"),
        (["RUN", "--seed", ""], "the seed is text of 1 to 128 characters"),
    ],
)
def test_season_errors_exit_2_with_a_message(
    args: list[str], message: str, ran: tuple[Path, str], tmp_path: Path
) -> None:
    (tmp_path / "empty").mkdir()
    shutil.copytree(ran[0], tmp_path / "copy")  # the same night under another name
    names = {
        "NONE": str(tmp_path / "none"),
        "EMPTY": str(tmp_path / "empty"),
        "RUN": str(ran[0]),
        "RUN2": str(tmp_path / "copy"),
    }
    result = runner.invoke(app, ["season", *(names.get(a, a) for a in args)])
    assert result.exit_code == 2
    assert "auction-night season: " in result.output and message in result.output


def test_season_reports_a_log_that_is_not_valid(ran: tuple[Path, str], tmp_path: Path) -> None:
    shutil.copytree(ran[0], tmp_path / "bad")
    path = tmp_path / "bad" / "events.jsonl"
    path.write_text("not json\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
    result = runner.invoke(app, ["season", str(tmp_path / "bad")])
    assert result.exit_code == 2 and "line 1 is not a valid event" in result.output


def test_season_needs_a_directory_and_checks_its_numbers(ran: tuple[Path, str]) -> None:
    assert runner.invoke(app, ["season"]).exit_code == 2
    for value in ("0", "-1", "many"):
        result = runner.invoke(app, ["season", str(ran[0]), "--resamples", value])
        assert result.exit_code == 2 and "Invalid value" in plain(result.output)


def test_season_cannot_write_where_there_is_no_directory(
    ran: tuple[Path, str], tmp_path: Path
) -> None:
    out = tmp_path / "missing" / "leaderboard.json"
    result = runner.invoke(app, ["season", str(ran[0]), "--out", str(out)])
    assert result.exit_code == 2 and "auction-night season: cannot write" in result.output


def test_season_describes_itself() -> None:
    result = runner.invoke(app, ["season", "--help"])
    assert result.exit_code == 0
    for text in ("--json", "--out", "--resamples", "--seed", "config.yaml", "check_leaderboard"):
        assert text in plain(result.output)


# ------------------------------------------------------------------------------------- play

# A night of sealed lots only and no plan: the person is asked for a line, a pledge and a bid in
# every lot, so the same three lines serve every lot and a stream of text can play the night.
THREE_LINES = "好\n\n5\n"
TWELVE_LOTS = THREE_LINES * 12


@pytest.fixture(scope="module")
def sealed_config(tmp_path_factory: pytest.TempPathFactory) -> Path:
    raw = yaml.safe_load(MOCK_CONFIG.read_text(encoding="utf-8"))
    raw["night"].update(
        formats=["sealed_first", "sealed_second"], lots_per_format=6, plan_and_mindreading=False
    )
    path = tmp_path_factory.mktemp("config") / "sealed.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return path


def play_args(out: Path, config: Path, *extra: str) -> list[str]:
    return ["play", "--config", str(config), "--out", str(out), "--human-seat", "3", *extra]


@pytest.fixture(scope="module")
def played(sealed_config: Path, tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, str]:
    """The sealed night played through the command line by a person at seat 3."""
    out = tmp_path_factory.mktemp("play") / "night"
    result = runner.invoke(app, play_args(out, sealed_config), input=TWELVE_LOTS)
    assert result.exit_code == 0, result.output
    return out, result.output


def test_play_takes_a_seat_and_writes_the_log_the_cassette_and_the_inputs(
    played: tuple[Path, str], sealed_config: Path
) -> None:
    from auction_night.contracts import loads_events, validate_log

    out, output = played
    assert sorted(path.name for path in out.iterdir()) == [
        "calls.jsonl",
        "config.yaml",
        "events.jsonl",
        "human.jsonl",
    ]
    assert (out / "config.yaml").read_bytes() == sealed_config.read_bytes()
    events = loads_events((out / "events.jsonl").read_text(encoding="utf-8"))
    assert validate_log(events) == [] and events[-2].type == "night_end"
    lines = output.splitlines()
    assert "night mock-0001: completed" in lines
    assert not any(line.startswith("  seat ") for line in lines)  # the scores are shown in Chinese
    assert lines[-2] == f"saved the configuration as {out / 'config.yaml'}"
    assert lines[-1] == (
        f"wrote {out / 'events.jsonl'}, {out / 'calls.jsonl'} and {out / 'human.jsonl'}"
    )
    tape = (out / "human.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(tape) == 1 + 24 and '"seat":3' in tape[0]


def test_play_shows_the_person_the_request_and_the_account_of_the_night(
    played: tuple[Path, str],
) -> None:
    _, output = played
    assert "你坐在 3 号位，代号「小满」" in output  # the briefing, once
    assert output.count("你坐在 3 号位") == 1
    assert "【第 1/12 件】" in output and "【现在要做】阶段 sealed：" in output
    assert "第 12 件" in output and "最终成绩" in output
    assert "这是一场有真人的夜晚，不计入模型积分榜。" in output
    assert "\x1b" not in output  # not a terminal: no colour codes


def test_a_night_played_at_the_command_line_replays_without_the_person(
    played: tuple[Path, str], sealed_config: Path
) -> None:
    out, _ = played
    result = runner.invoke(app, replay_args(out, sealed_config))  # no input at all
    assert result.exit_code == 0, result.output
    count = len((out / "events.jsonl").read_text(encoding="utf-8").splitlines())
    assert result.output.strip() == f"night mock-0001: replay matches, {count} events"


def test_a_night_with_a_person_replays_from_its_directory_alone(
    played: tuple[Path, str],
) -> None:
    out, _ = played
    result = runner.invoke(app, ["replay", "--from", str(out)])  # no configuration, no input
    assert result.exit_code == 0, result.output
    assert "replay matches" in result.output


def test_play_stops_when_the_input_ends(sealed_config: Path, tmp_path: Path) -> None:
    out = tmp_path / "night"
    result = runner.invoke(app, play_args(out, sealed_config), input=THREE_LINES * 2)
    assert result.exit_code == 2
    assert "auction-night play: the input ended before the night did" in result.output
    assert not (out / "events.jsonl").exists()
    assert not (out / "config.yaml").exists()  # saved once the night is over
    assert len((out / "human.jsonl").read_text(encoding="utf-8").splitlines()) == 1 + 4


def test_a_wrong_entry_costs_nothing_at_the_command_line(
    sealed_config: Path, tmp_path: Path
) -> None:
    out = tmp_path / "night"
    typed = "好\n\n99999\nabc\n5\n" + THREE_LINES * 11
    result = runner.invoke(app, play_args(out, sealed_config), input=typed)
    assert result.exit_code == 0, result.output
    assert result.output.count("超过你的剩余预算") == 1 and result.output.count("这不是整数") == 1
    replay = runner.invoke(app, replay_args(out, sealed_config))
    assert replay.exit_code == 0, replay.output


@pytest.mark.parametrize(
    ("seat", "message"),
    [
        ("7", "seat 7 is a rule bot"),
        ("9", "the table has no seat 9"),
    ],
)
def test_play_refuses_a_seat_a_person_cannot_take(
    sealed_config: Path, tmp_path: Path, seat: str, message: str
) -> None:
    args = ["play", "-c", str(sealed_config), "-o", str(tmp_path / "n"), "--human-seat", seat]
    result = runner.invoke(app, args, input=TWELVE_LOTS)
    assert result.exit_code == 2 and f"auction-night play: {message}" in result.output
    assert not (tmp_path / "n").exists()


def test_play_needs_to_know_which_seat(sealed_config: Path, tmp_path: Path) -> None:
    args = ["play", "-c", str(sealed_config), "-o", str(tmp_path / "n")]
    result = runner.invoke(app, args, input=TWELVE_LOTS)
    assert result.exit_code == 2 and "give --human-seat" in result.output


def human_config(source: Path, tmp_path: Path, seat: int = 3) -> Path:
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    raw["seats"][seat - 1] = {
        "seat": seat,
        "codename": raw["seats"][seat - 1]["codename"],
        "kind": "human",
    }
    path = tmp_path / "human.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    return path


def test_play_takes_the_seat_the_configuration_names(sealed_config: Path, tmp_path: Path) -> None:
    config = human_config(sealed_config, tmp_path, seat=5)
    out = tmp_path / "night"
    result = runner.invoke(app, ["play", "-c", str(config), "-o", str(out)], input=TWELVE_LOTS)
    assert result.exit_code == 0, result.output
    assert "你坐在 5 号位，代号「青柠」" in result.output
    conflict = runner.invoke(
        app, ["play", "-c", str(config), "-o", str(tmp_path / "b"), "--human-seat", "3"]
    )
    assert conflict.exit_code == 2 and "already seats a person at 5, not at 3" in conflict.output
    replay = runner.invoke(app, replay_args(out, config))
    assert replay.exit_code == 0, replay.output


def test_run_sends_a_person_to_play(sealed_config: Path, tmp_path: Path) -> None:
    config = human_config(sealed_config, tmp_path)
    result = runner.invoke(app, run_args(tmp_path / "n", config=config))
    assert result.exit_code == 2
    assert "auction-night run: seat 3 is a human seat: take it with `auction-night play`" in (
        result.output
    )
    assert not (tmp_path / "n").exists()


def test_play_refuses_to_replace_an_earlier_night_unless_told_to(
    played: tuple[Path, str], sealed_config: Path, tmp_path: Path
) -> None:
    out = tmp_path / "night"
    shutil.copytree(played[0], out)
    again = runner.invoke(app, play_args(out, sealed_config), input=TWELVE_LOTS)
    assert again.exit_code == 2
    assert "auction-night play: " in again.output and "already exists" in again.output
    assert "--overwrite" in again.output
    replaced = runner.invoke(app, play_args(out, sealed_config, "--overwrite"), input=TWELVE_LOTS)
    assert replaced.exit_code == 0, replaced.output
    assert (out / "events.jsonl").read_bytes() == (played[0] / "events.jsonl").read_bytes()
    assert (out / "config.yaml").read_bytes() == sealed_config.read_bytes()


def test_play_treats_an_earlier_config_yaml_like_the_other_files(
    sealed_config: Path, tmp_path: Path
) -> None:
    out = tmp_path / "night"
    out.mkdir()
    (out / "config.yaml").write_text("stale: true\n", encoding="utf-8")
    refused = runner.invoke(app, play_args(out, sealed_config), input=TWELVE_LOTS)
    assert refused.exit_code == 2
    assert "config.yaml already exists" in refused.output and "--overwrite" in refused.output
    assert not (out / "human.jsonl").exists()  # refused before the person was asked anything


def test_play_checks_its_files(sealed_config: Path, tmp_path: Path) -> None:
    missing = runner.invoke(app, play_args(tmp_path / "a", tmp_path / "none.yaml"))
    assert missing.exit_code == 2 and "configuration not found" in missing.output
    prices = runner.invoke(
        app, play_args(tmp_path / "b", sealed_config, "--prices", str(tmp_path / "none.yaml"))
    )
    assert prices.exit_code == 2 and "price list not found" in prices.output
    assert not (tmp_path / "a").exists() and not (tmp_path / "b").exists()


@pytest.mark.parametrize(
    "args", [["--timeout", "0"], ["--timeout", "0.5"], ["--human-seat", "0"], ["--human-seat", "x"]]
)
def test_play_checks_its_numbers(sealed_config: Path, tmp_path: Path, args: list[str]) -> None:
    base = ["play", "-c", str(sealed_config), "-o", str(tmp_path / "n")]
    if "--human-seat" not in args:
        base += ["--human-seat", "3"]
    result = runner.invoke(app, [*base, *args])
    assert result.exit_code == 2 and "Invalid value" in plain(result.output)
    assert not (tmp_path / "n").exists()


def test_play_describes_itself() -> None:
    result = runner.invoke(app, ["play", "--help"])
    assert result.exit_code == 0
    for text in (
        "--human-seat",
        "--timeout",
        "--overwrite",
        "human.jsonl",
        "config.yaml",
        "replay",
    ):
        assert text in plain(result.output)


def test_the_module_entry_point_reads_a_real_pipe(sealed_config: Path, tmp_path: Path) -> None:
    """The night is typed into the standard input of a real process, and replayed by another."""
    out = tmp_path / "night"
    play = subprocess.run(
        [sys.executable, "-m", "auction_night.cli", *play_args(out, sealed_config)],
        input=TWELVE_LOTS,
        capture_output=True,
        text=True,
        check=False,
    )
    assert play.returncode == 0, play.stderr
    assert "night mock-0001: completed" in play.stdout and play.stderr == ""
    replay = subprocess.run(
        [sys.executable, "-m", "auction_night.cli", *replay_args(out, sealed_config)],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    assert replay.returncode == 0, replay.stderr
    assert "replay matches" in replay.stdout


# ----------------------------------------------------------------------------------- export

SAMPLE_WORD = "祝大家好运"  # the first words of the first lot of the mock night
STAMP = "2026-11-01T12:00:00.000Z"


@pytest.fixture
def night_copy(ran: tuple[Path, str], tmp_path: Path) -> Path:
    """A private copy of the run directory that a test may change."""
    out = tmp_path / "night"
    shutil.copytree(ran[0], out)
    return out


def export_args(run_dir: Path, *extra: str | Path) -> list[str]:
    return ["export", str(run_dir), *(str(arg) for arg in extra)]


def test_export_writes_replay_json_next_to_the_log(night_copy: Path) -> None:
    result = runner.invoke(app, export_args(night_copy, "--no-git"))
    assert result.exit_code == 0, result.output
    target = night_copy / "replay.json"
    done = re.fullmatch(
        rf"wrote {re.escape(str(target))} \((\d+) bytes, 12 lots\); "
        r"content scan: (\d+) texts read, nothing found",
        result.output.strip(),
    )
    assert done, result.output
    text = target.read_text(encoding="utf-8")
    assert int(done.group(1)) == len(text.encode("utf-8"))
    replay = Replay.model_validate_json(text)
    assert replay.night.id == "mock-0001" and replay.night.ranked
    assert replay.manifest.git_commit is None and replay.manifest.revealed_seed is None
    assert not (night_copy / "replay.json.part").exists()


def test_export_takes_the_configuration_saved_by_run_or_the_one_it_is_given(
    night_copy: Path, tmp_path: Path
) -> None:
    saved = runner.invoke(app, export_args(night_copy, "-o", tmp_path / "a.json", "--no-git"))
    assert saved.exit_code == 0, saved.output
    (night_copy / "config.yaml").unlink()
    given = runner.invoke(
        app, export_args(night_copy, "-c", MOCK_CONFIG, "-o", tmp_path / "b.json", "--no-git")
    )
    assert given.exit_code == 0, given.output
    assert (tmp_path / "a.json").read_bytes() == (tmp_path / "b.json").read_bytes()
    assert not (night_copy / "replay.json").exists()  # -o says where the file goes


def test_exporting_a_directory_twice_gives_the_same_bytes(night_copy: Path, tmp_path: Path) -> None:
    for name in ("a.json", "b.json"):
        args = export_args(night_copy, "-o", tmp_path / name, "--no-git")
        assert runner.invoke(app, args).exit_code == 0
    first = (tmp_path / "a.json").read_bytes()
    assert first == (tmp_path / "b.json").read_bytes()
    manifest = json.loads(first)["manifest"]
    assert manifest["created_utc"] and manifest["created_utc"].endswith("Z")  # the last call


def test_the_manifest_has_the_commit_of_this_code_unless_told_otherwise(
    night_copy: Path, tmp_path: Path
) -> None:
    assert runner.invoke(app, export_args(night_copy, "-o", tmp_path / "git.json")).exit_code == 0
    manifest = json.loads((tmp_path / "git.json").read_text(encoding="utf-8"))["manifest"]
    # a checkout gives its commit, and a copy that git cannot read gives none
    assert manifest["git_commit"] is None or re.fullmatch(r"[0-9a-f]{40}", manifest["git_commit"])


def test_export_puts_the_stamp_and_the_seed_in_the_manifest_when_asked(
    night_copy: Path, tmp_path: Path
) -> None:
    target = tmp_path / "revealed.json"
    args = export_args(
        night_copy, "-o", target, "--created-utc", STAMP, "--reveal-seed", "--no-git"
    )
    assert runner.invoke(app, args).exit_code == 0
    manifest = json.loads(target.read_text(encoding="utf-8"))["manifest"]
    assert manifest["created_utc"] == STAMP and manifest["revealed_seed"] == "mock-seed-0001"
    assert manifest["git_commit"] is None


def test_the_content_gate_refuses_a_night_and_nothing_is_written(
    night_copy: Path, tmp_path: Path
) -> None:
    words = tmp_path / "words.txt"
    words.write_text(f"# for the test\n{SAMPLE_WORD}\n", encoding="utf-8")
    (night_copy / "replay.json").write_text("earlier\n", encoding="utf-8")
    result = runner.invoke(app, export_args(night_copy, "--wordlist", words, "--no-git"))
    assert result.exit_code == 1
    assert "auction-night export: content scan: " in result.output
    assert f"[wordlist: {SAMPLE_WORD}]" in result.output
    assert "timeline[0].talk[0].text (lot 1, seat 1)" in result.output
    assert (night_copy / "replay.json").read_text(encoding="utf-8") == "earlier\n"
    elsewhere = tmp_path / "elsewhere.json"
    again = runner.invoke(app, export_args(night_copy, "--wordlist", words, "-o", elsewhere))
    assert again.exit_code == 1 and not elsewhere.exists()
    assert not list(night_copy.glob("*.part")) and not list(tmp_path.glob("*.part"))


def test_export_without_a_configuration_exits_2(night_copy: Path, tmp_path: Path) -> None:
    (night_copy / "config.yaml").unlink()
    absent = runner.invoke(app, export_args(night_copy))
    assert absent.exit_code == 2
    assert "auction-night export: no configuration: give --config" in absent.output
    named = runner.invoke(app, export_args(night_copy, "-c", tmp_path / "none.yaml"))
    assert named.exit_code == 2 and "configuration not found: " in named.output
    broken = runner.invoke(app, export_args(night_copy, "-c", CONFIGS / "prices.example.yaml"))
    assert broken.exit_code == 2 and "cannot read" in broken.output
    placeholders = runner.invoke(
        app, export_args(night_copy, "-c", CONFIGS / "night_api.example.yaml")
    )
    assert placeholders.exit_code == 2 and "still holds placeholders" in placeholders.output
    assert not (night_copy / "replay.json").exists()


def test_export_of_a_bad_directory_exits_2_or_names_the_missing_files(tmp_path: Path) -> None:
    nowhere = runner.invoke(app, export_args(tmp_path / "nowhere"))
    assert nowhere.exit_code == 2
    assert f"auction-night export: not a directory: {tmp_path / 'nowhere'}" in nowhere.output
    (tmp_path / "a-file").write_text("x", encoding="utf-8")
    assert runner.invoke(app, export_args(tmp_path / "a-file")).exit_code == 2
    (tmp_path / "empty").mkdir()
    # a night without its log is refused, not a usage error
    empty = runner.invoke(app, export_args(tmp_path / "empty", "-c", MOCK_CONFIG))
    assert empty.exit_code == 1
    assert "not found: " in empty.output and "events.jsonl" in empty.output
    assert not (tmp_path / "empty" / "replay.json").exists()


def test_export_refuses_a_log_that_the_cassette_does_not_give_back(night_copy: Path) -> None:
    path = night_copy / "events.jsonl"
    path.write_text(path.read_text(encoding="utf-8").replace('"seq":9,', '"seq":90,', 1))
    result = runner.invoke(app, export_args(night_copy))
    assert result.exit_code == 1
    assert "auction-night export: the replay of the night differs from events.jsonl" in (
        result.output
    )
    assert not (night_copy / "replay.json").exists()


def test_export_refuses_a_cassette_that_no_longer_fits_the_prompts(
    night_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dataclasses
    from types import MappingProxyType

    from auction_night.prompts import load_templates

    def edited(lang: str = "zh") -> object:
        templates = load_templates(lang)
        texts = {**templates.texts, "system.donts": templates.texts["system.donts"] + "再加一句。"}
        return dataclasses.replace(templates, texts=MappingProxyType(texts))

    monkeypatch.setattr("auction_night.llm.render.load_templates", edited)
    result = runner.invoke(app, export_args(night_copy))
    assert result.exit_code == 1
    assert "auction-night export: a recording does not fit this night" in result.output
    assert not (night_copy / "replay.json").exists()


def test_export_checks_its_options(night_copy: Path, tmp_path: Path) -> None:
    long_stamp = runner.invoke(app, export_args(night_copy, "--created-utc", "x" * 65))
    assert long_stamp.exit_code == 2 and "--created-utc is one line" in long_stamp.output
    no_words = runner.invoke(app, export_args(night_copy, "--wordlist", tmp_path / "none.txt"))
    assert no_words.exit_code == 2 and "cannot read the word list" in no_words.output
    where = tmp_path / "missing" / "replay.json"
    nowhere = runner.invoke(app, export_args(night_copy, "-o", where, "--no-git"))
    assert nowhere.exit_code == 2 and "auction-night export: cannot write " in nowhere.output
    assert not (night_copy / "replay.json").exists()
    assert runner.invoke(app, ["export"]).exit_code == 2  # the directory is required


def test_a_show_role_that_is_a_model_needs_its_recording_and_is_never_called(
    night_copy: Path, tmp_path: Path
) -> None:
    raw = yaml.safe_load((night_copy / "config.yaml").read_text(encoding="utf-8"))
    raw["endpoints"] = {"show": {"kind": "openai_compatible", "base_url": "http://localhost:9/v1"}}
    raw["show"]["judges"] = [
        {"kind": "llm", "endpoint": "show", "model": "judge-model", "vendor": f"vendor-{n}"}
        for n in (1, 2)
    ]
    (night_copy / "config.yaml").write_text(yaml.safe_dump(raw, allow_unicode=True), "utf-8")
    result = runner.invoke(app, export_args(night_copy))
    assert result.exit_code == 2
    assert "the judge is a model" in result.output and "judge_calls.jsonl" in result.output
    elsewhere = tmp_path / "judges.jsonl"
    named = runner.invoke(app, export_args(night_copy, "--judge-calls", elsewhere))
    assert named.exit_code == 2 and str(elsewhere) in named.output  # the option is the place
    assert not (night_copy / "replay.json").exists()


def test_export_makes_the_replay_of_the_committed_sample_but_for_the_time_stamp(
    night_copy: Path, tmp_path: Path
) -> None:
    target = tmp_path / "exported.json"
    args = export_args(night_copy, "-o", target, "--no-git", "--reveal-seed")
    assert runner.invoke(app, args).exit_code == 0
    exported = json.loads(target.read_text(encoding="utf-8"))
    assert exported["manifest"]["created_utc"] is not None
    exported["manifest"]["created_utc"] = None  # the sample has no wall clock in it
    assert exported == json.loads(SAMPLE.read_text(encoding="utf-8"))


def edit_config(night: Path, change: Callable[[dict[str, Any]], None]) -> None:
    """Change the configuration that the run saved in its directory."""
    path = night / "config.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(raw)
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")


def test_the_word_list_of_the_configuration_is_looked_for_beside_the_night(
    night_copy: Path,
) -> None:
    edit_config(night_copy, lambda raw: raw["show"]["content_scan"].update(wordlist="blocked.txt"))
    absent = runner.invoke(app, export_args(night_copy, "--no-git"))
    assert absent.exit_code == 2 and "word list not found: blocked.txt" in absent.output
    (night_copy / "blocked.txt").write_text("好运\n", encoding="utf-8")
    found = runner.invoke(app, export_args(night_copy, "--no-git"))
    assert found.exit_code == 1 and "[wordlist: 好运]" in found.output
    assert not (night_copy / "replay.json").exists()


def test_a_deal_seed_that_lives_in_the_environment_must_be_set_to_export(
    night_copy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def move_seed(raw: dict[str, Any]) -> None:
        raw["night"]["seed_env"] = "AUCTION_NIGHT_TEST_SEED"
        del raw["night"]["seed"]

    edit_config(night_copy, move_seed)
    monkeypatch.delenv("AUCTION_NIGHT_TEST_SEED", raising=False)
    unset = runner.invoke(app, export_args(night_copy, "--no-git"))
    assert unset.exit_code == 2 and "AUCTION_NIGHT_TEST_SEED" in unset.output
    assert not (night_copy / "replay.json").exists()
    monkeypatch.setenv("AUCTION_NIGHT_TEST_SEED", "mock-seed-0001")
    done = runner.invoke(app, export_args(night_copy, "--no-git"))
    assert done.exit_code == 0, done.output


def test_export_refuses_a_night_whose_cassette_is_missing(night_copy: Path) -> None:
    (night_copy / "calls.jsonl").unlink()
    result = runner.invoke(app, export_args(night_copy))
    assert result.exit_code == 1 and "not found: " in result.output
    assert "calls.jsonl" in result.output and not (night_copy / "replay.json").exists()


MODEL_ROLE = {"kind": "llm", "endpoint": "show", "model": "show-model", "vendor": "vendor-s"}


def put_models_in_the_show(night: Path, **show: Any) -> None:
    """Make some roles of the show layer models, so that the export needs their recordings."""

    def change(raw: dict[str, Any]) -> None:
        raw["endpoints"] = {
            "show": {"kind": "openai_compatible", "base_url": "http://localhost:9/v1"}
        }
        raw["show"].update(show)

    edit_config(night, change)


@pytest.mark.parametrize(
    ("show", "option", "role"),
    [
        ({"host_offline": MODEL_ROLE}, "--host-calls", "offline host"),
        ({"content_scan": {"classifier": MODEL_ROLE}}, "--scan-calls", "content classifier"),
    ],
)
def test_the_recording_options_of_the_host_and_the_classifier_name_the_place_looked_at(
    night_copy: Path, tmp_path: Path, show: dict[str, Any], option: str, role: str
) -> None:
    put_models_in_the_show(night_copy, **show)
    default = runner.invoke(app, export_args(night_copy))
    assert default.exit_code == 2 and f"the {role} is a model" in default.output
    elsewhere = tmp_path / "recording.jsonl"
    named = runner.invoke(app, export_args(night_copy, option, elsewhere))
    assert named.exit_code == 2 and str(elsewhere) in named.output
    assert not (night_copy / "replay.json").exists()


def test_a_recording_that_cannot_be_read_is_a_refusal_and_not_a_crash(night_copy: Path) -> None:
    put_models_in_the_show(night_copy, host_offline=MODEL_ROLE)
    (night_copy / "host_calls.jsonl").write_text("this is not json\n", encoding="utf-8")
    result = runner.invoke(app, export_args(night_copy))
    assert result.exit_code == 1 and "a recording does not fit this night" in result.output
    assert not (night_copy / "replay.json").exists()


def test_export_of_a_night_with_a_person_is_not_ranked(played: tuple[Path, str]) -> None:
    out, _ = played
    result = runner.invoke(app, export_args(out, "--no-git"))
    assert result.exit_code == 0, result.output
    replay = Replay.model_validate_json((out / "replay.json").read_text(encoding="utf-8"))
    assert not replay.night.ranked
    assert [seat.kind for seat in replay.seats if seat.seat == 3] == ["human"]


def test_the_manifest_names_the_commit_only_of_a_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    here = cli._checkout()
    assert here is None or (here / "pyproject.toml").is_file()
    fake = tmp_path / "src" / "auction_night"
    fake.mkdir(parents=True)
    monkeypatch.setattr(cli, "__file__", str(fake / "cli.py"))
    assert cli._checkout() is None  # neither a repository nor a project
    (tmp_path / ".git").mkdir()
    assert cli._checkout() is None  # a repository around an installed copy is not its checkout
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    assert cli._checkout() == tmp_path.resolve()


def test_export_describes_itself() -> None:
    result = runner.invoke(app, ["export", "--help"])
    assert result.exit_code == 0
    text = plain(result.output)
    options = ("--config", "--out", "--host-calls", "--judge-calls", "--scan-calls", "--wordlist")
    for option in (*options, "--clear", "--reveal-seed", "--created-utc", "--no-git", "RUN_DIR"):
        assert option in text
    for word in ("replay.json", "config.yaml", "events.jsonl", "TOML", "host.per_lot[0].text"):
        assert word in text


def test_the_module_entry_point_exports_a_night_and_refuses_one(tmp_path: Path) -> None:
    out = tmp_path / "night"
    ran_night = subprocess.run(
        [sys.executable, "-m", "auction_night.cli", *run_args(out)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ran_night.returncode == 0, ran_night.stderr
    command = [sys.executable, "-m", "auction_night.cli", *export_args(out, "--no-git")]
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert done.stderr == "" and "12 lots" in done.stdout
    assert (out / "replay.json").is_file()
    words = tmp_path / "words.txt"
    words.write_text(f"{SAMPLE_WORD}\n", encoding="utf-8")
    refused = subprocess.run(
        [*command, "--wordlist", str(words), "-o", str(tmp_path / "refused.json")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert refused.returncode == 1 and refused.stdout == ""
    assert "auction-night export: content scan: " in refused.stderr
    assert not (tmp_path / "refused.json").exists()


# ------------------------------------------------------------------------------------ clear
#
# A finding of the content gate that a person has read is released with --clear. The word list is
# what makes findings here: SAMPLE_WORD is said by six lines of the mock night.

REASON = "开场祝词，词表是为这个测试加的"


def places_of(output: str) -> list[str]:
    """The places a refusal lists, in order."""
    return re.findall(r"^  (\S+) \(lot \d+", output, flags=re.MULTILINE)


def clear_file(tmp_path: Path, places: list[str], category: str = "wordlist") -> Path:
    tables = "".join(
        f'[[clear]]\npath = "{place}"\ncategory = "{category}"\nreason = "{REASON}"\n\n'
        for place in places
    )
    path = tmp_path / "clear.toml"
    path.write_text(tables, encoding="utf-8")
    return path


@pytest.fixture
def found(night_copy: Path, tmp_path: Path) -> tuple[Path, list[str], list[str | Path]]:
    """A run directory whose export is refused for six lines, the places the refusal lists, and
    the options that make the word list."""
    words = tmp_path / "words.txt"
    words.write_text(f"{SAMPLE_WORD}\n", encoding="utf-8")
    options: list[str | Path] = ["--wordlist", words, "--no-git"]
    refused = runner.invoke(app, export_args(night_copy, *options))
    assert refused.exit_code == 1
    places = places_of(refused.output)
    assert len(places) == 6
    return night_copy, places, options


def test_clear_releases_the_findings_and_the_manifest_lists_them(
    found: tuple[Path, list[str], list[str | Path]], tmp_path: Path
) -> None:
    night, places, options = found
    sheet = clear_file(tmp_path, places)
    result = runner.invoke(app, export_args(night, *options, "--clear", sheet))
    assert result.exit_code == 0, result.output
    assert re.fullmatch(
        rf"wrote {re.escape(str(night / 'replay.json'))} \(\d+ bytes, 12 lots\); "
        r"content scan: \d+ texts read, 6 finding\(s\) cleared by hand",
        result.output.strip(),
    ), result.output
    replay = Replay.model_validate_json((night / "replay.json").read_text(encoding="utf-8"))
    cleared = replay.manifest.cleared_findings
    assert [c.path for c in cleared] == places
    assert {c.category for c in cleared} == {"wordlist"}
    assert {c.detail for c in cleared} == {SAMPLE_WORD} and {c.reason for c in cleared} == {REASON}


def test_clear_with_a_finding_left_refuses_the_night_and_lists_that_finding_alone(
    found: tuple[Path, list[str], list[str | Path]], tmp_path: Path
) -> None:
    night, places, options = found
    (night / "replay.json").write_text("earlier\n", encoding="utf-8")
    result = runner.invoke(
        app, export_args(night, *options, "--clear", clear_file(tmp_path, places[:5]))
    )
    assert result.exit_code == 1
    assert "content scan: 1 finding(s) in" in result.output and "5 cleared by hand" in result.output
    assert places_of(result.output) == places[5:]
    assert (night / "replay.json").read_text(encoding="utf-8") == "earlier\n"
    assert not list(night.glob("*.part"))


def test_clear_with_a_table_the_scan_did_not_report_is_a_usage_error(
    found: tuple[Path, list[str], list[str | Path]], tmp_path: Path
) -> None:
    night, places, options = found
    stale = clear_file(tmp_path, [*places, "host.recap"])
    result = runner.invoke(app, export_args(night, *options, "--clear", stale))
    assert result.exit_code == 2
    assert "auction-night export: the clearance file lists 1 finding(s)" in result.output
    assert "host.recap (wordlist)" in result.output
    assert not (night / "replay.json").exists()
    # the text was fixed and the file was kept: now none of its tables is reported
    kept = runner.invoke(
        app, export_args(night, "--no-git", "--clear", clear_file(tmp_path, places))
    )
    assert kept.exit_code == 2 and "lists 6 finding(s)" in kept.output
    assert not (night / "replay.json").exists()


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("[[clear\n", "is not valid TOML"),
        ("", "found none"),
        ('[[clear]]\npath = "a"\ncategory = "wordlist"\n', "reason is missing"),
        ('[[clear]]\npath = "a"\ncategory = "nope"\nreason = "r"\n', "is none of"),
    ],
)
def test_clear_with_a_file_that_is_wrong_is_a_usage_error_before_anything_is_played(
    night_copy: Path, tmp_path: Path, body: str, message: str
) -> None:
    path = tmp_path / "clear.toml"
    path.write_text(body, encoding="utf-8")
    (night_copy / "events.jsonl").unlink()  # the night itself could not be played
    result = runner.invoke(app, export_args(night_copy, "--clear", path))
    assert result.exit_code == 2
    assert result.output.startswith("auction-night export: ")
    assert "clear.toml" in result.output and message in result.output
    missing = runner.invoke(app, export_args(night_copy, "--clear", tmp_path / "none.toml"))
    assert missing.exit_code == 2 and "cannot read the clearance file none.toml" in missing.output
    assert not (night_copy / "replay.json").exists()


def test_a_night_the_gate_passes_on_its_own_has_no_cleared_findings(
    night_copy: Path, tmp_path: Path
) -> None:
    target = tmp_path / "plain.json"
    assert runner.invoke(app, export_args(night_copy, "-o", target, "--no-git")).exit_code == 0
    assert json.loads(target.read_text(encoding="utf-8"))["manifest"]["cleared_findings"] == []


def test_the_module_entry_point_exports_with_a_clearance(tmp_path: Path) -> None:
    out = tmp_path / "night"
    ran_night = subprocess.run(
        [sys.executable, "-m", "auction_night.cli", *run_args(out)],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert ran_night.returncode == 0, ran_night.stderr
    words = tmp_path / "words.txt"
    words.write_text(f"{SAMPLE_WORD}\n", encoding="utf-8")
    command = [sys.executable, "-m", "auction_night.cli", *export_args(out, "--no-git")]
    command += ["--wordlist", str(words), "-o", str(tmp_path / "replay.json")]
    refused = subprocess.run(command, capture_output=True, text=True, check=False)
    assert refused.returncode == 1 and refused.stdout == ""
    places = places_of(refused.stderr)
    assert len(places) == 6
    done = subprocess.run(
        [*command, "--clear", str(clear_file(tmp_path, places))],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert done.stderr == "" and "6 finding(s) cleared by hand" in done.stdout
    manifest = json.loads((tmp_path / "replay.json").read_text(encoding="utf-8"))["manifest"]
    assert [c["path"] for c in manifest["cleared_findings"]] == places


# ------------------------------------------------------------------------------------- show
#
# The recording itself, with fake models, is tested in tests/show/test_show_showrun.py. Here: what
# the command does for a night that needs no recording, and what it says about itself.


def test_show_has_nothing_to_record_when_no_role_of_the_show_layer_is_a_model(
    night_copy: Path,
) -> None:
    result = runner.invoke(app, ["show", str(night_copy)])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == (
        "night mock-0001: every role of the show layer is a mock, so there is nothing to record"
    )
    assert sorted(path.name for path in night_copy.iterdir()) == [
        "calls.jsonl",
        "config.yaml",
        "events.jsonl",
    ]
    exported = runner.invoke(app, export_args(night_copy, "--no-git"))
    assert exported.exit_code == 0, exported.output  # the mock roles are what export runs


def test_show_replays_a_night_with_a_person_like_any_other(played: tuple[Path, str]) -> None:
    out, _ = played
    result = runner.invoke(app, ["show", str(out)])
    assert result.exit_code == 0, result.output
    assert "nothing to record" in result.output


def test_show_describes_itself() -> None:
    result = runner.invoke(app, ["show", "--help"])
    assert result.exit_code == 0
    text = plain(result.output)
    for option in ("RUN_DIR", "--config", "--overwrite", "--prices"):
        assert option in text
    for word in ("host_calls.jsonl", "judge_calls.jsonl", "scan_calls.jsonl", "config.yaml"):
        assert word in text
    for status in ("Exit status 0", "Exit status 1", "Exit status 2"):
        assert status in text


def test_show_reports_on_stderr_only_through_the_module_entry_point(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "auction_night.cli", "show", str(tmp_path / "no-such-directory")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "auction-night show: not a directory" in proc.stderr
