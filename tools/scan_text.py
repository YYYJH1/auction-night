"""Scan tracked text files for content that must not reach the public repository.

Rules:
  internal-path  absolute paths under the private data or home mounts
  api-key        strings shaped like API keys, or a literal assigned to api_key

Exceptions go in tools/scan_allow.txt, one ``path:line-substring`` entry per line. A finding
is waived when its file matches ``path`` (fnmatch syntax) and its line contains the substring.

Usage: python tools/scan_text.py [--root DIR] [--include-untracked]
                                [--allow FILE] [FILE ...]
Exit status: 0 clean, 1 violations found, 2 usage or I/O error. Standard library only.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

TOOLS_DIR = Path(__file__).resolve().parent

# The patterns below are written so that this file does not match them.
_INTERNAL_PATH = re.compile(r"(?:(?<![\w./~-])|(?<=file://))/(?:data|home)/")
_KEY_SHAPES = (
    ("Anthropic-style key", r"sk-an[t]-[A-Za-z0-9_-]*"),
    ("sk- key with 20 or more characters", r"(?<![A-Za-z0-9])sk-[A-Za-z0-9]{20,}"),
    ("sk- project key", r"(?<![A-Za-z0-9])sk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{20,}"),
    ("literal assigned to api_key", r"""(?i)api_key['"]?\s*[:=]\s*['"][^'"]+"""),
    (
        "GitHub token",
        r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{20,})",
    ),
    ("Google API key", r"(?<![\w-])AIza[\w-]{35}"),
    ("private key block", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


@dataclass(frozen=True)
class Rule:
    """A pattern to look for, optionally limited to some paths."""

    name: str
    message: str
    pattern: re.Pattern[str]
    secret: bool = False
    scopes: tuple[str, ...] = ()

    def applies_to(self, path: str) -> bool:
        """Whether the rule is checked in this repo-relative path."""
        if not self.scopes:
            return True
        return any(path == s or (s.endswith("/") and path.startswith(s)) for s in self.scopes)


class Violation(NamedTuple):
    """One finding. ``text`` is the whole line and is only used to match the allow-list."""

    path: str
    line: int
    column: int
    rule: str
    message: str
    excerpt: str
    text: str


class AllowEntry(NamedTuple):
    """One ``path:line-substring`` exception and its line number in the allow-list file."""

    glob: str
    needle: str
    number: int


def build_rules() -> list[Rule]:
    """The rules of the scan."""
    rules = [Rule("internal-path", "absolute path under a private mount", _INTERNAL_PATH)]
    rules += [Rule("api-key", msg, re.compile(rx), secret=True) for msg, rx in _KEY_SHAPES]
    return rules


def load_allow(path: Path) -> list[AllowEntry]:
    """Parse ``path:line-substring`` entries. Blank lines and ``#`` comments are ignored."""
    entries = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        glob, _, needle = line.partition(":")
        if not glob.strip() or not needle.strip():
            raise ValueError(f"{path}:{number}: expected 'path:line-substring', got {raw!r}")
        entries.append(AllowEntry(glob.strip(), needle.strip(), number))
    return entries


def _excerpt(line: str, match: re.Match[str], redact: bool, context: int = 30) -> str:
    """The match with a little context. Secrets are replaced, so reports never echo them."""
    start, end = match.span()
    shown = "[redacted]" if redact else match.group()
    return (line[max(0, start - context) : start] + shown + line[end : end + context]).strip()


def find_violations(path: str, text: str, rules: Iterable[Rule]) -> list[Violation]:
    """Every match of every rule that applies to ``path``, in line order."""
    active = [rule for rule in rules if rule.applies_to(path)]
    found = []
    for number, line in enumerate(text.split("\n"), 1):
        if not line.strip():
            continue
        for rule in active:
            for match in rule.pattern.finditer(line):
                if not match.group():
                    continue
                excerpt = _excerpt(line, match, rule.secret)
                where = (path, number, match.start() + 1)
                found.append(Violation(*where, rule.name, rule.message, excerpt, line))
    return sorted(found, key=lambda v: (v.line, v.column))


def split_allowed(
    found: Iterable[Violation], entries: Sequence[AllowEntry]
) -> tuple[list[Violation], set[int]]:
    """Drop findings covered by the allow-list. Also return the entries that were used."""
    kept, used = [], set()
    for violation in found:
        hit = next(
            (
                e
                for e in entries
                if fnmatch.fnmatchcase(violation.path, e.glob) and e.needle in violation.text
            ),
            None,
        )
        if hit is None:
            kept.append(violation)
        else:
            used.add(hit.number)
    return kept, used


def read_text(path: Path) -> str | None:
    """File contents, or None for symlinks, binary files and anything unreadable."""
    if path.is_symlink():
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def list_files(root: Path, include_untracked: bool) -> list[str]:
    """Repo-relative paths from ``git ls-files``, optionally plus untracked, unignored files."""
    cmd = ["git", "-C", str(root), "ls-files", "-z"]
    if include_untracked:
        cmd += ["--cached", "--others", "--exclude-standard"]
    try:
        proc = subprocess.run(cmd, capture_output=True, check=True)
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"git ls-files failed: {detail}") from exc
    return sorted({p for p in os.fsdecode(proc.stdout).split("\0") if p})


def _relative(root: Path, path: Path) -> str | None:
    """``path`` relative to ``root`` in POSIX form, or None when it lies outside."""
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return None


def format_report(violations: Sequence[Violation], scanned: int) -> str:
    """A readable report: one location line and one excerpt line per finding."""
    files = len({v.path for v in violations})
    lines = [f"scan_text: {len(violations)} violation(s) in {files} file(s), {scanned} scanned", ""]
    for v in violations:
        lines.append(f"{v.path}:{v.line}:{v.column}: {v.rule}: {v.message}")
        lines.append(f"    {v.excerpt}")
    lines += ["", "Fix the text, or justify an exception in tools/scan_allow.txt (path:substring)."]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the scan and return the exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("files", nargs="*", help="scan only these files (default: all tracked)")
    parser.add_argument("--root", type=Path, default=TOOLS_DIR.parent, help="repository root")
    parser.add_argument("--allow", type=Path, help="allow-list (default: tools/scan_allow.txt)")
    parser.add_argument(
        "--include-untracked",
        action="store_true",
        help="also scan untracked files that git does not ignore (local check before committing)",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    allow_path = args.allow or root / "tools" / "scan_allow.txt"

    try:
        rules = build_rules()
        entries = load_allow(allow_path) if args.allow or allow_path.exists() else []
        if args.files:
            names = [_relative(root, Path(f)) or "" for f in args.files]
            if not all(names):
                raise ValueError("every FILE must lie inside --root")
        else:
            names = list_files(root, args.include_untracked)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"scan_text: {exc}", file=sys.stderr)
        return 2

    # The allow-list quotes the strings it is about.
    skip = {p for p in (_relative(root, allow_path),) if p}
    found: list[Violation] = []
    scanned = 0
    for name in names:
        text = None if name in skip else read_text(root / name)
        if text is not None:
            scanned += 1
            found += find_violations(name, text, rules)
    remaining, used = split_allowed(found, entries)

    if not args.files:  # a partial scan cannot tell whether an entry is stale
        for entry in entries:
            if entry.number not in used:
                note = f"unused allow-list entry at line {entry.number}"
                print(f"scan_text: warning: {note}", file=sys.stderr)
    if remaining:
        print(format_report(remaining, scanned))
        return 1
    print(f"scan_text: OK, {scanned} files scanned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
