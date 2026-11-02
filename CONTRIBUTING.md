<p align="center"><b>English</b> · <a href="CONTRIBUTING_zh.md">简体中文</a></p>

# Contributing

The project has a single maintainer. Please open an issue before a pull request.

## Checks

Python 3.12 or newer and [uv](https://docs.astral.sh/uv/) are required, and the tests of the replay page need Node 22.

```
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
python3 tools/scan_text.py
node --test tests/viewer/
```

CI runs the ruff and pytest checks on Python 3.12 and 3.13, the replay page tests on Node 22, the text scanner, `tools/check_leaderboard.py` and `tools/check_seeds.py`. No secrets are used anywhere in CI. Before the first commit of new files, add `--include-untracked` to the scanner so that it also reads files git does not track yet.

When you change an English document, change its Chinese version in the same commit, because `tests/test_doc_mirrors.py` checks that the two carry the language switch on the first line, have the same heading levels in the same order and have the same number of code blocks.

## The text scanner

`tools/scan_text.py` reads every tracked text file and fails on two things:

- absolute paths that point into a local home or data directory
- strings shaped like API keys, or a literal assigned to `api_key`

A justified exception goes in `tools/scan_allow.txt` as `path:line-substring`, with the reason in the commit message.
