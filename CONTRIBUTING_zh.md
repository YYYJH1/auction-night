<p align="center"><a href="CONTRIBUTING.md">English</a> · <b>简体中文</b></p>

# 贡献指南

项目只有一位维护者。请先开一个 issue，再提 pull request。

## 检查

需要 Python 3.12 及以上和 [uv](https://docs.astral.sh/uv/)。回放页的测试需要 Node 22。

```
uv sync --locked
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
python3 tools/scan_text.py
node --test tests/viewer/
```

CI 在 Python 3.12 和 3.13 上运行 ruff 和 pytest 检查，在 Node 22 上运行回放页的测试，并运行文本扫描器、`tools/check_leaderboard.py`、`tools/check_seeds.py` 和上文的历史检查。CI 里任何地方都不使用密钥。第一次提交新文件之前，给扫描器加上 `--include-untracked`，让它也读取 git 还没有跟踪的文件。

改英文文档时，请在同一次提交里同步改它的中文版，因为 `tests/test_doc_mirrors.py` 会核对两份文档的第一行语言切换、标题级别的顺序和代码块的数目是否一致。

## 文本扫描器

`tools/scan_text.py` 读取每个已跟踪的文本文件，遇到以下两类内容就判为失败：

- 指向本地 home 目录或 data 目录的绝对路径
- 形似 API 密钥的字符串，或者赋给 `api_key` 的字面量

有正当理由的例外，以 `path:line-substring` 的格式写进 `tools/scan_allow.txt`，理由写在提交信息里。
