"""Repository hygiene: the text scanner and the CI workflow.

Strings that the scanner rejects are assembled from pieces, so this file stays clean.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import scan_text

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
SCANNER = REPO / "tools" / "scan_text.py"

needs_git = pytest.mark.skipif(
    shutil.which("git") is None or not (REPO / ".git").exists(), reason="needs a git checkout"
)

RULES = scan_text.build_rules()
ABS_DATA = "/" + "data/someone/project/file.txt"
ABS_HOME = "/" + "home/someone/.cache"
SECRETS = {
    "anthropic": ("sk-" + "ant-api03-" + "A" * 24, "A" * 24),
    "openai": ("sk-" + "A1" * 12, "A1" * 12),
    "project": ("sk-" + "proj-" + "B" * 24, "B" * 24),
    "github": ("ghp_" + "C" * 36, "C" * 36),
    "assigned": ("api_" + "key=" + '"hunter2"', "hunter2"),
}


def rules_hit(path: str, text: str) -> set[str]:
    """Names of the scanner rules that fire on ``text`` when it sits in ``path``."""
    return {v.rule for v in scan_text.find_violations(path, text, RULES)}


def make_repo(root: Path, files: dict[str, str], *, stage: bool = True) -> Path:
    """A throwaway git repository holding ``files`` (staged, not committed)."""
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    if stage:
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    return root


# ---------------------------------------------------------------- scanner rules


@pytest.mark.parametrize(
    "text",
    [ABS_DATA, f"cd {ABS_DATA}", f"p = '{ABS_HOME}'", f"`{ABS_HOME}`", f"open file://{ABS_HOME}"],
)
def test_internal_paths_are_flagged(text: str) -> None:
    assert "internal-path" in rules_hit("docs/x.md", text)


@pytest.mark.parametrize(
    "text",
    ["https://example.com/data/set", "src/data/load.py", "./data/x", "~/home/x", "a data/ folder"],
)
def test_relative_paths_and_urls_are_not_flagged(text: str) -> None:
    assert rules_hit("docs/x.md", text) == set()


@pytest.mark.parametrize("secret", SECRETS.values(), ids=SECRETS)
def test_key_shaped_strings_are_flagged_and_never_echoed(secret: tuple[str, str]) -> None:
    text, sensitive = secret
    found = scan_text.find_violations("src/x.py", f"x = {text}", RULES)
    assert {v.rule for v in found} == {"api-key"}
    assert all(sensitive not in v.excerpt for v in found)


@pytest.mark.parametrize(
    "text",
    [
        "api_key_env: ANTHROPIC_API_KEY",
        "api_key = os.environ['SOME_VAR']",
        'api_key=""',
        "risk-" + "x" * 30,
        "task-" + "y" * 30,
    ],
)
def test_lookalikes_are_not_flagged(text: str) -> None:
    assert rules_hit("src/x.py", text) == set()


def test_every_finding_is_reported_with_its_column() -> None:
    found = scan_text.find_violations("docs/a.md", f"{ABS_DATA} and {ABS_HOME}", RULES)
    assert [(v.line, v.column) for v in found] == [(1, 1), (1, len(ABS_DATA) + 6)]


# ----------------------------------------------------------------- allow-list


def test_allow_list_waives_only_matching_lines() -> None:
    found = scan_text.find_violations("docs/a.md", f"{ABS_DATA}\n{ABS_HOME}", RULES)
    entries = [scan_text.AllowEntry("docs/*.md", "someone/project", 1)]
    kept, used = scan_text.split_allowed(found, entries)
    assert [v.line for v in kept] == [2]
    assert used == {1}


def test_allow_list_does_not_cross_files() -> None:
    found = scan_text.find_violations("docs/a.md", ABS_DATA, RULES)
    kept, used = scan_text.split_allowed(found, [scan_text.AllowEntry("src/*", "someone", 1)])
    assert len(kept) == 1
    assert used == set()


def test_allow_list_file_parsing(tmp_path: Path) -> None:
    good = tmp_path / "good.txt"
    good.write_text("# comment\n\ndocs/*.md: a: b\n", encoding="utf-8")
    assert scan_text.load_allow(good) == [scan_text.AllowEntry("docs/*.md", "a: b", 3)]
    bad = tmp_path / "bad.txt"
    bad.write_text("no separator here\n", encoding="utf-8")
    with pytest.raises(ValueError, match="bad.txt:1"):
        scan_text.load_allow(bad)


# --------------------------------------------------------- scanner as a program


@needs_git
def test_scan_text_passes_on_the_current_tree() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCANNER), "--include-untracked"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


@needs_git
def test_main_exit_codes_and_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    clean = make_repo(tmp_path / "clean", {"README.md": "hello\n"})
    assert scan_text.main(["--root", str(clean)]) == 0

    dirty = make_repo(tmp_path / "dirty", {"docs/a.md": f"see {ABS_DATA}\n"})
    capsys.readouterr()
    assert scan_text.main(["--root", str(dirty)]) == 1
    assert "docs/a.md:1:5: internal-path" in capsys.readouterr().out

    broken = tmp_path / "not-a-repo"
    broken.mkdir()
    assert scan_text.main(["--root", str(broken)]) == 2


@needs_git
def test_main_uses_the_allow_list_of_the_scanned_root(tmp_path: Path) -> None:
    files = {
        "docs/a.md": f"see {ABS_DATA}\n",
        "tools/scan_allow.txt": "docs/a.md: someone/project\n",
    }
    repo = make_repo(tmp_path / "repo", files)
    assert scan_text.main(["--root", str(repo)]) == 0


@needs_git
def test_untracked_files_are_scanned_only_on_request(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo", {"docs/a.md": f"see {ABS_DATA}\n"}, stage=False)
    assert scan_text.main(["--root", str(repo)]) == 0
    assert scan_text.main(["--root", str(repo), "--include-untracked"]) == 1


@needs_git
def test_binary_files_and_symlinks_are_skipped(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo", {"blob.bin": f"\0{ABS_DATA}\n"})
    (repo / "link.md").symlink_to(repo / "blob.bin")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    assert scan_text.main(["--root", str(repo)]) == 0


# ----------------------------------------------------------------- CI workflow


def test_ci_workflow_has_the_required_jobs_and_no_secrets() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    for needle in (
        "push:",
        "pull_request:",
        '"3.12"',
        '"3.13"',
        "astral-sh/setup-uv",
        "uv sync",
        "uv run ruff check .",
        "uv run pytest -q",
        "python3 tools/scan_text.py",
    ):
        assert needle in text, needle
    assert not re.search(r"\$\{\{\s*secrets[.\[]", text)


def test_ci_workflow_actions_are_pinned_to_a_version() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    refs = re.findall(r"^\s*(?:-\s*)?uses:\s*(\S+)", text, flags=re.MULTILINE)
    assert refs
    for ref in refs:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@v\d+(?:\.\d+){0,2}", ref), ref
