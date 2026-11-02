"""tools/check_seeds.py: what CI checks in the seasons and in the files git would publish."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from seeds_support import REPO, Season, card_of, edit_json

import check_seeds
from auction_night import seeds

SCRIPT = REPO / "tools" / "check_seeds.py"
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

pytestmark = needs_git


def make_repo(root: Path, files: dict[str, str] | None = None) -> Path:
    """A throwaway git repository (nothing committed) with some files in it."""
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for name, text in (files or {"README.md": "hello\n"}).items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    return root


def publish(repo: Path, season: Season) -> Path:
    """Copy the season directory into ``seasons/s1`` of a repository."""
    target = repo / "seasons" / "s1"
    shutil.copytree(season.season_dir, target)
    return target


def run_tool(
    repo: Path, capsys: pytest.CaptureFixture[str], *extra: str | Path
) -> tuple[int, str, str]:
    status = check_seeds.main(["--root", str(repo), *map(str, extra)])
    shown = capsys.readouterr()
    return status, shown.out, shown.err


# ------------------------------------------------------------------------ no season at all


def test_without_a_season_only_the_private_files_are_looked_for(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo")
    status, out, err = run_tool(repo, capsys)
    assert (status, err) == (0, "")
    assert out.splitlines() == [
        "check_seeds: no private seed file among 1 files",
        "check_seeds: no season directory under seasons, nothing to check",
    ]


def test_outside_a_repository_the_tool_cannot_list_the_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    plain = tmp_path / "plain"
    plain.mkdir()
    status, _, err = run_tool(plain, capsys)
    assert status == 2 and "git ls-files failed" in err


# ------------------------------------------------------------------------------- seasons


def test_a_season_before_its_reveal_passes_without_a_seeds_file(
    mid_season: Season, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo")
    publish(repo, mid_season)
    status, out, err = run_tool(repo, capsys)
    assert (status, err) == (0, "")
    assert out.splitlines()[-1] == (
        "check_seeds: seasons/s1: 13 checks: 11 ok, 0 failed, 2 skipped (seeds not revealed yet)"
    )


def test_a_revealed_season_passes_with_every_card_drawn_again(
    end_of_season: Season, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo")
    publish(repo, end_of_season)
    status, out, err = run_tool(repo, capsys)
    assert (status, err) == (0, "")
    assert out.splitlines()[-1] == (
        "check_seeds: seasons/s1: 18 checks: 18 ok, 0 failed, 0 skipped (seeds revealed)"
    )


def test_commitments_alone_are_checked_for_their_format_and_nothing_else(
    mid_season: Season, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo")
    target = repo / "seasons" / "s1"
    target.mkdir(parents=True)
    shutil.copy(mid_season.commitments, target / "commitments.json")
    status, out, err = run_tool(repo, capsys)
    assert (status, err) == (0, "")
    assert out.splitlines()[-1].endswith(
        "1 check: 1 ok, 0 failed, 0 skipped (seeds not revealed yet)"
    )
    (target / "commitments.json").write_text('{"schema": "x"}', encoding="utf-8")
    status, _, err = run_tool(repo, capsys)
    assert status == 2 and "commitments file" in err and "cannot be used" in err


def test_a_season_with_nights_and_no_commitments_fails(
    mid_season: Season, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo")
    target = publish(repo, mid_season)
    (target / "commitments.json").unlink()
    status, _, err = run_tool(repo, capsys)
    assert status == 1
    assert "no commitments.json: the seeds of this season were not committed" in err


def test_a_changed_night_fails_the_tool(
    end_of_season: Season, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo")
    target = publish(repo, end_of_season)
    card_of(target / "s1-d01-r0", 1, lambda card: card.update(budget=card["budget"] + 1))
    status, out, err = run_tool(repo, capsys)
    assert status == 1 and "FAIL  events.jsonl differs from the cards of the seed" in err
    assert "seat 1: the budget is" in err and "seasons/s1" in err


def test_once_revealed_every_night_must_bring_its_configuration(
    end_of_season: Season, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo")
    target = publish(repo, end_of_season)
    (target / "s1-d02-r1" / "config.yaml").unlink()
    status, _, err = run_tool(repo, capsys)
    assert status == 1 and "FAIL  cards not recomputed: no config.yaml" in err


def test_a_season_that_is_still_being_played_may_lack_a_configuration(
    mid_season: Season, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo")
    target = publish(repo, mid_season)
    (target / "s1-d02-r1" / "config.yaml").unlink()
    status, out, _ = run_tool(repo, capsys)
    assert status == 0
    assert out.splitlines()[-1].endswith(
        "12 checks: 10 ok, 0 failed, 2 skipped (seeds not revealed yet)"
    )


def test_the_planned_configurations_are_checked_too(
    mid_season: Season, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo")
    target = publish(repo, mid_season)
    planned = target / "configs" / "s1-d01-r1.yaml"
    data = yaml.safe_load(planned.read_text(encoding="utf-8"))
    data["night"]["deal_seed_commitment"] = "sha256:" + "cd" * 32
    planned.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    status, _, err = run_tool(repo, capsys)
    assert status == 1 and "configuration configs/s1-d01-r1.yaml" in err
    assert "night.deal_seed_commitment is not the one published for deck d01" in err


def test_a_planned_configuration_with_the_seed_inside_fails_before_the_reveal(
    mid_season: Season, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo")
    target = publish(repo, mid_season)
    planned = target / "configs" / "s1-d02-r0.yaml"
    data = yaml.safe_load(planned.read_text(encoding="utf-8"))
    del data["night"]["seed_env"]
    data["night"]["seed"] = mid_season.seeds["d02"]
    planned.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    status, _, err = run_tool(repo, capsys)
    assert status == 1
    assert "night.seed holds a deal seed in a file before the reveal" in err
    assert mid_season.seeds["d02"] not in err, "the report never shows a seed"


def test_a_malformed_night_file_is_a_file_that_cannot_be_used(
    mid_season: Season, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo")
    target = publish(repo, mid_season)
    (target / "s1-d01-r0" / "events.jsonl").write_text("{broken\n", encoding="utf-8")
    status, _, err = run_tool(repo, capsys)
    assert status == 2 and "line 1 is not a valid event" in err


def test_a_reveal_that_edits_the_published_seed_fails_the_tool(
    end_of_season: Season, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo")
    target = publish(repo, end_of_season)
    edit_json(target / "seeds.json", lambda data: data["decks"][1].update(seed="x" * 64))
    status, _, err = run_tool(repo, capsys)
    assert status == 1 and "deck d02: the revealed seed does not open" in err


# ------------------------------------------------------------- private files in the repository


def a_private_file(workdir: Path) -> Path:
    private, public = seeds.new_season("s9", ["d01", "d02"], created_utc="2026-11-01T12:00:00Z")
    return seeds.write_private(workdir / "elsewhere" / "s9.seeds-private.json", private)


def test_a_private_file_in_the_work_tree_is_found_by_its_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = a_private_file(tmp_path)
    repo = make_repo(tmp_path / "repo")
    (repo / "seasons").mkdir()
    shutil.copy(source, repo / "seasons" / "s9.seeds-private.json")
    status, out, err = run_tool(repo, capsys)
    assert status == 1 and "no private seed file" not in out
    assert "seasons/s9.seeds-private.json is named like a private seed file" in err
    assert "it must never be committed" in err
    subprocess.run(["git", "-C", str(repo), "add", "-f", "-A"], check=True)  # committed in effect
    assert run_tool(repo, capsys)[0] == 1


def test_a_renamed_private_file_is_found_by_what_it_says(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = a_private_file(tmp_path)
    repo = make_repo(tmp_path / "repo")
    shutil.copy(source, repo / "notes.txt")
    status, _, err = run_tool(repo, capsys)
    assert status == 1 and "notes.txt is a private seed file" in err
    secret = seeds.read_private(source)
    assert not any(seed in err for seed in secret.by_deck.values())


def test_an_ignored_private_file_is_not_among_the_files_git_would_publish(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = a_private_file(tmp_path)
    repo = make_repo(tmp_path / "repo", {".gitignore": (REPO / ".gitignore").read_text("utf-8")})
    shutil.copy(source, repo / "mine.seeds-private.json")
    status, out, err = run_tool(repo, capsys)
    assert (status, err) == (0, "") and "no private seed file among 1 files" in out


def test_other_json_files_are_not_private_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    files = {
        "a.json": '{"schema": "something-else"}',
        "b.json": "[1, 2, 3]",
        "c.json": "{broken",
        "d.txt": "plain text",
    }
    repo = make_repo(tmp_path / "repo", files)
    status, out, err = run_tool(repo, capsys)
    assert (status, err) == (0, "") and "no private seed file among 4 files" in out


def test_the_seeds_of_the_private_file_are_looked_for_in_the_repository(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = a_private_file(tmp_path)
    secret = seeds.read_private(source)
    clean = make_repo(tmp_path / "clean")
    status, _, err = run_tool(clean, capsys, "--private", source)
    assert (status, err) == (0, "")
    dirty = make_repo(tmp_path / "dirty", {"notes.md": f"remember {secret.by_deck['d02']}\n"})
    status, _, err = run_tool(dirty, capsys, "--private", source)
    assert status == 1 and "notes.md holds the seed of deck d02" in err
    assert not any(seed in err for seed in secret.by_deck.values()), "and it never shows one"


@pytest.mark.skipif(sys.platform == "win32", reason="file modes")
def test_the_private_file_is_read_only_if_it_is_private(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = a_private_file(tmp_path)
    source.chmod(0o644)
    repo = make_repo(tmp_path / "repo")
    status, _, err = run_tool(repo, capsys, "--private", source)
    assert status == 2 and "chmod 600" in err


# ---------------------------------------------------------------------------- the repository


def test_the_tool_passes_on_the_current_tree() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=REPO, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.splitlines()[0].startswith("check_seeds: no private seed file among ")


def test_the_constants_of_the_tool_are_those_of_the_package() -> None:
    assert check_seeds.PRIVATE_SCHEMA == seeds.PRIVATE_SCHEMA
    assert check_seeds.PRIVATE_NAME in seeds.PRIVATE_SUFFIX
    assert check_seeds.COMMITMENTS_FILE == seeds.COMMITMENTS_FILE
    assert check_seeds.REVEAL_FILE == seeds.REVEAL_FILE
    ignored = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert f"*{check_seeds.PRIVATE_NAME}*" in ignored


def test_ci_runs_the_check_once_in_the_test_job() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert text.count("tools/check_seeds.py") == 1
    test_job = text[text.index("\n  test:") : text.index("\n  viewer:")]
    assert "run: uv run python tools/check_seeds.py" in test_job
    assert test_job.index("check_leaderboard.py") < test_job.index("check_seeds.py")


def test_the_workflow_is_valid_yaml_and_the_step_is_in_the_test_job() -> None:
    """The step name once held a colon and a space, which YAML reads as a mapping."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["test"]["steps"]
    ours = [step for step in steps if step.get("run") == "uv run python tools/check_seeds.py"]
    assert len(ours) == 1 and ours[0]["name"].startswith("Seed commitments hold")
    assert steps[steps.index(ours[0]) - 1]["run"] == "uv run python tools/check_leaderboard.py"
