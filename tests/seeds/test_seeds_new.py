"""``seeds new``: the seeds, the commitments, and the protection of the private file."""

import hashlib
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from seeds_support import REPO, STAMP, plain, read_json, seeds_cli

from auction_night import __version__, seeds
from auction_night.contracts.events import SEED_PATTERN, seed_commitment
from auction_night.engine.rng import RNG_VERSION

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
posix_only = pytest.mark.skipif(os.name != "posix", reason="file modes are a POSIX matter")


def new_args(workdir: Path, *extra: str | Path, decks: str = "2") -> list[str | Path]:
    """Arguments of a ``seeds new`` that writes into ``workdir``."""
    return [
        "new", "--season", "s1", "--decks", decks,
        "--private-out", workdir / "secret" / "s1.seeds-private.json",
        "--out", workdir / "season" / "commitments.json",
        "--created-utc", STAMP, *extra,
    ]  # fmt: skip


def fake_repo(path: Path) -> Path:
    """A folder that looks like a git work tree to git: HEAD, objects and refs in ``.git``."""
    git = path / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "refs").mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------- the two files


def test_new_writes_the_public_and_the_private_file(workdir: Path) -> None:
    result = seeds_cli(*new_args(workdir))
    assert result.exit_code == 0, result.output
    public = read_json(workdir / "season" / "commitments.json")
    secret = read_json(workdir / "secret" / "s1.seeds-private.json")

    assert list(public) == ["schema", "season", "algorithm", "created_utc", "generator", "decks"]
    assert public["schema"] == "auction-night/season-commitments@1"
    assert (public["season"], public["algorithm"], public["created_utc"]) == ("s1", "sha256", STAMP)
    assert public["generator"] == {
        "name": "auction-night",
        "version": __version__,
        "rng": RNG_VERSION,
    }
    assert [entry["deck"] for entry in public["decks"]] == ["d01", "d02"]
    assert all(list(entry) == ["deck", "commitment"] for entry in public["decks"])

    assert list(secret) == ["schema", "season", "created_utc", "generator", "notice", "decks"]
    assert secret["schema"] == seeds.PRIVATE_SCHEMA
    assert [entry["deck"] for entry in secret["decks"]] == ["d01", "d02"]
    assert "SECRET" in secret["notice"]


def test_every_commitment_is_the_sha256_of_a_long_random_seed(workdir: Path) -> None:
    assert seeds_cli(*new_args(workdir, decks="12")).exit_code == 0
    public = read_json(workdir / "season" / "commitments.json")
    secret = read_json(workdir / "secret" / "s1.seeds-private.json")
    values = [entry["seed"] for entry in secret["decks"]]
    assert len(set(values)) == 12
    for entry, published in zip(secret["decks"], public["decks"], strict=True):
        seed = entry["seed"]
        assert re.fullmatch(r"[0-9a-f]{64}", seed)  # 256 bits
        assert re.fullmatch(SEED_PATTERN, seed)  # a seed that the contract accepts
        digest = "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()
        assert entry["commitment"] == published["commitment"] == digest == seed_commitment(seed)
        assert published["deck"] == entry["deck"]
    text = (workdir / "season" / "commitments.json").read_text(encoding="utf-8")
    assert not any(seed in text for seed in values), "the public file never holds a seed"


def test_new_does_not_print_a_seed(workdir: Path) -> None:
    result = seeds_cli(*new_args(workdir))
    secret = read_json(workdir / "secret" / "s1.seeds-private.json")
    assert not any(entry["seed"] in result.output for entry in secret["decks"])
    assert "season s1: 2 decks (d01, d02)" in result.output


def test_new_names_decks_by_count_or_by_id(workdir: Path) -> None:
    by_id = [
        "new", "--season", "s2", "--deck", "spring", "--deck", "summer",
        "--private-out", workdir / "a.seeds-private.json", "--out", workdir / "a" / "c.json",
    ]  # fmt: skip
    assert seeds_cli(*by_id).exit_code == 0
    decks = read_json(workdir / "a" / "c.json")["decks"]
    assert [entry["deck"] for entry in decks] == ["spring", "summer"]
    assert seeds.deck_ids(2) == ["d01", "d02"]
    assert seeds.deck_ids(100)[0] == "d001" and seeds.deck_ids(100)[-1] == "d100"


def test_new_draws_nothing_by_itself_when_the_draw_is_given() -> None:
    calls = iter(f"{n:064x}" for n in range(1, 10))
    first, public = seeds.new_season("s1", ["a", "b"], created_utc=STAMP, draw=lambda: next(calls))
    again, public_again = seeds.new_season(
        "s1", ["a", "b"], created_utc=STAMP, draw=iter([f"{1:064x}", f"{2:064x}"]).__next__
    )
    assert first == again and public == public_again
    with pytest.raises(seeds.SeedsError, match="same seed"):
        seeds.new_season("s1", ["a", "b"], created_utc=STAMP, draw=lambda: "a" * 64)


def test_the_default_place_of_the_commitments_is_the_season_directory(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(workdir)
    result = seeds_cli(
        "new", "--season", "s7", "--decks", "1", "--private-out", "k.seeds-private.json"
    )
    assert result.exit_code == 0, result.output
    assert read_json(workdir / "seasons" / "s7" / "commitments.json")["season"] == "s7"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--decks", "2", "--deck", "x"], "give exactly one of --decks and --deck"),
        ([], "give exactly one of --decks and --deck"),
        (["--deck", "x", "--deck", "x"], "a deck id appears twice"),
        (["--deck", "bad id"], "deck: String should match pattern"),
        (["--decks", "2", "--created-utc", "yesterday"], "a time stamp looks like"),
        (["--decks", "2", "--created-utc", "2026-13-45T00:00:00Z"], "not a real date"),
    ],
)
def test_new_refuses_bad_arguments_and_writes_nothing(
    workdir: Path, args: list[str], message: str
) -> None:
    base = ["new", "--season", "s1", "--private-out", workdir / "p.seeds-private.json"]
    result = seeds_cli(*base, "--out", workdir / "c.json", *args)
    assert result.exit_code == 2
    assert "auction-night seeds new: " in result.output and message in result.output
    assert not (workdir / "p.seeds-private.json").exists() and not (workdir / "c.json").exists()


def test_new_refuses_a_bad_season_id_and_a_bad_number_of_decks(workdir: Path) -> None:
    bad = seeds_cli("new", "--season", "no good", "--decks", "1", "--private-out", workdir / "p")
    assert bad.exit_code == 2 and "season: String should match pattern" in bad.output
    for count in ("0", "1000", "many"):
        result = seeds_cli(
            "new", "--season", "s1", "--decks", count, "--private-out", workdir / "p"
        )
        assert result.exit_code == 2 and "Invalid value" in plain(result.output)
    assert not (workdir / "p").exists()


# ----------------------------------------------------------------- the private file is private


@posix_only
def test_the_private_file_is_readable_by_its_owner_only(workdir: Path) -> None:
    assert seeds_cli(*new_args(workdir)).exit_code == 0
    private = workdir / "secret" / "s1.seeds-private.json"
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
    assert stat.S_IMODE(private.parent.stat().st_mode) == 0o700  # a directory that new made


@posix_only
def test_the_mode_does_not_depend_on_the_umask(workdir: Path) -> None:
    (workdir / "secret").mkdir()
    (workdir / "season").mkdir()
    before = os.umask(0o277)  # a umask that would leave a new file read-only
    try:
        assert seeds_cli(*new_args(workdir)).exit_code == 0
    finally:
        os.umask(before)
    assert stat.S_IMODE((workdir / "secret" / "s1.seeds-private.json").stat().st_mode) == 0o600


@posix_only
def test_a_private_file_that_others_can_read_is_not_used(workdir: Path) -> None:
    assert seeds_cli(*new_args(workdir)).exit_code == 0
    private = workdir / "secret" / "s1.seeds-private.json"
    private.chmod(0o640)
    refused = seeds_cli("env", "--private", private, "--deck", "d01")
    assert refused.exit_code == 2 and "chmod 600" in refused.output
    assert "export" not in refused.output
    commitments = workdir / "season" / "commitments.json"
    reveal = seeds_cli("reveal", "--private", private, "--commitments", commitments)
    assert reveal.exit_code == 2 and "chmod 600" in reveal.output
    assert not (workdir / "season" / "seeds.json").exists()
    private.chmod(0o600)
    assert seeds_cli("env", "--private", private, "--deck", "d01").exit_code == 0


def test_the_private_file_is_never_replaced(workdir: Path) -> None:
    assert seeds_cli(*new_args(workdir)).exit_code == 0
    private = workdir / "secret" / "s1.seeds-private.json"
    before = private.read_bytes()
    public = (workdir / "season" / "commitments.json").read_bytes()
    again = seeds_cli(*new_args(workdir, "--overwrite"))  # --overwrite is for the public file only
    assert again.exit_code == 2
    assert "already exists" in again.output and "only copy" in again.output
    assert private.read_bytes() == before
    assert (workdir / "season" / "commitments.json").read_bytes() == public


def test_a_second_run_cannot_replace_published_commitments_by_accident(workdir: Path) -> None:
    assert seeds_cli(*new_args(workdir)).exit_code == 0
    public = (workdir / "season" / "commitments.json").read_bytes()
    other = [
        "new", "--season", "s1", "--decks", "2",
        "--private-out", workdir / "other.seeds-private.json",
        "--out", workdir / "season" / "commitments.json",
    ]  # fmt: skip
    refused = seeds_cli(*other)
    assert refused.exit_code == 2 and "already exists (use --overwrite" in refused.output
    assert not (workdir / "other.seeds-private.json").exists(), "no seeds for commitments not kept"
    assert (workdir / "season" / "commitments.json").read_bytes() == public
    replaced = seeds_cli(*other, "--overwrite")
    assert replaced.exit_code == 0, replaced.output
    assert (workdir / "season" / "commitments.json").read_bytes() != public


def test_a_failed_commitments_file_takes_the_private_file_with_it(workdir: Path) -> None:
    (workdir / "afile").write_text("not a directory", encoding="utf-8")
    result = seeds_cli(
        "new", "--season", "s1", "--decks", "1", "--private-out", workdir / "p.seeds-private.json",
        "--out", workdir / "afile" / "commitments.json",
    )  # fmt: skip
    assert result.exit_code == 2 and "cannot write" in result.output
    assert not (workdir / "p.seeds-private.json").exists()


def test_a_symbolic_link_cannot_take_the_private_file_anywhere(workdir: Path) -> None:
    elsewhere = workdir / "elsewhere.json"
    (workdir / "out").mkdir()
    (workdir / "out" / "t.json").symlink_to(elsewhere)  # a link at the place of the file
    args = new_args(workdir)
    args[args.index("--private-out") + 1] = workdir / "out" / "t.json"
    result = seeds_cli(*args)
    assert result.exit_code == 2 and "already exists" in result.output
    assert not elsewhere.exists()


# -------------------------------------------------------------- never inside a git work tree


def test_new_refuses_to_write_the_private_file_into_a_work_tree(workdir: Path) -> None:
    repo = fake_repo(workdir / "repo")
    target = repo / "seasons" / "s1" / "s1.seeds-private.json"
    args = new_args(workdir)
    args[args.index("--private-out") + 1] = target
    result = seeds_cli(*args)
    assert result.exit_code == 2
    assert "inside the git work tree 'repo'" in result.output and "outside every" in result.output
    assert not (repo / "seasons").exists(), "not even the folders are made"
    assert not (workdir / "season" / "commitments.json").exists(), "and nothing is published"


def test_a_relative_path_and_a_link_into_a_work_tree_are_refused_too(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = fake_repo(workdir / "repo")
    (repo / "docs").mkdir()
    monkeypatch.chdir(repo / "docs")
    args = new_args(workdir)
    args[args.index("--private-out") + 1] = Path("..") / "mine.json"
    relative = seeds_cli(*args)
    assert relative.exit_code == 2 and "inside the git work tree" in relative.output
    link = workdir / "link"
    link.symlink_to(repo / "docs", target_is_directory=True)
    args[args.index("--private-out") + 1] = link / "mine.json"
    through_link = seeds_cli(*args)
    assert through_link.exit_code == 2 and "inside the git work tree" in through_link.output
    assert not (repo / "mine.json").exists() and not (repo / "docs" / "mine.json").exists()


@needs_git
def test_a_real_repository_counts_as_a_work_tree(workdir: Path) -> None:
    subprocess.run(["git", "init", "-q", str(workdir / "repo")], check=True)
    assert seeds.work_tree_of(workdir / "repo" / "a" / "b.json") == workdir / "repo"
    args = new_args(workdir)
    args[args.index("--private-out") + 1] = workdir / "repo" / "s1.seeds-private.json"
    assert seeds_cli(*args).exit_code == 2
    assert not (workdir / "repo" / "s1.seeds-private.json").exists()


def test_what_counts_as_a_work_tree(workdir: Path) -> None:
    assert seeds.work_tree_of(workdir / "x" / "y.json") is None
    (workdir / "empty" / ".git").mkdir(parents=True)  # a folder named .git is not a repository
    assert seeds.work_tree_of(workdir / "empty" / "y.json") is None
    (workdir / "tree").mkdir()
    (workdir / "tree" / ".git").write_text("gitdir: ../elsewhere/.git/worktrees/tree\n", "utf-8")
    assert seeds.work_tree_of(workdir / "tree" / "y.json") == workdir / "tree"  # a worktree
    assert seeds.work_tree_of(fake_repo(workdir / "repo") / "deep" / "er" / "y.json") == (
        workdir / "repo"
    )


# --------------------------------------------------------------------------------- .gitignore


@needs_git
@pytest.mark.parametrize(
    "name",
    [
        "s1.seeds-private.json",
        "seeds-private.json",
        "seasons/s1/s1.seeds-private.json",
        "x/y-seeds-private-old",
    ],
)
def test_gitignore_covers_the_private_file(name: str) -> None:
    ignored = subprocess.run(
        ["git", "-C", str(REPO), "check-ignore", "-q", "--no-index", name], check=False
    )
    assert ignored.returncode == 0, name


@needs_git
@pytest.mark.parametrize("name", ["seasons/s1/commitments.json", "seasons/s1/seeds.json"])
def test_gitignore_leaves_the_public_files_alone(name: str) -> None:
    ignored = subprocess.run(
        ["git", "-C", str(REPO), "check-ignore", "-q", "--no-index", name], check=False
    )
    assert ignored.returncode == 1, name
