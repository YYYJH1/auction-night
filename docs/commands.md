<p align="center"><b>English</b> · <a href="commands_zh.md">简体中文</a></p>

# Commands

`uv run auction-night <command> --help` lists every option of a command. A command exits with status 0 when it succeeds and 2 on a usage error or a file it cannot use. `replay` and `export` exit with 1 when they refuse a night, `show` when a model of the show layer gets no answer, the cost cap stops it or the night does not replay, and `seeds reveal` and `seeds verify` when a seed does not open its commitment or a check fails.

| Command | What it does |
| --- | --- |
| `run -c configs/night_mock.yaml -o runs/mock` | Plays a night and writes the log (`events.jsonl`), the cassette of every model call (`calls.jsonl`) and a copy of the configuration (`config.yaml`) to a directory. With `configs/night_mock.yaml` every model seat is a mock model, so it needs no network and no key. |
| `replay --from runs/mock` | Plays a recorded night again from its cassette alone and checks the log byte for byte. Without `-c` it reads the `config.yaml` that `run` saved. |
| `play -c configs/night_mock.yaml -o runs/me --human-seat 3` | Seats a person at the terminal in place of one model seat, while the other seats play as configured. It also writes `human.jsonl`, the record of what was typed. A night with a person is not ranked. |
| `simulate --preset s2 --nights 200` | Plays many nights with rule-based bots only, writes nothing, and prints the average surplus of each bidding style with 95% intervals. |
| `estimate -c configs/night_bailian.yaml` | Prices a night in tokens and dollars before it runs. See Cost in [models.md](models.md). |
| `season runs/mock` | Builds the leaderboard (mean surplus per night, with bootstrap rank intervals) from finished night directories, reading the `events.jsonl` and `config.yaml` of each. |
| `export runs/mock` | Turns a recorded night into `replay.json`, the file the replay page plays. The night is played again from its cassette and every public text goes through the content gate. If the night is refused (the gate stops it, or the replay does not match the log), nothing is written and the exit status is 1. |
| `show runs/night-01` | Calls the models of the show layer on a recorded night and records what they say. The host, the two judges and the content classifier work after the night, and `export` never calls them: when one of them is a model, `export` reads its answers from `host_calls.jsonl`, `judge_calls.jsonl` or `scan_calls.jsonl` in the run directory, and `show` writes those files. The night is played again from its cassette, so the seats call nothing, and a role that is a mock runs on the spot and leaves no file. The calls are priced from the price list, and `limits.max_cost_per_night_usd` also caps what the show layer may spend, on its own. If a model gets no answer or the cap stops the calls, nothing is written and the exit status is 1. A recording that already exists is refused unless `--overwrite` is given. |
| `seeds` | Holds the seed commitments of a season. `seeds new` draws a secret seed for every deck and writes the commitments to publish before the season, with the seeds in a private file outside the repository. `seeds plan` writes the configuration of every night, `seeds env` prints the line that gives a night its seed, `seeds reveal` opens the commitments after the season, and `seeds verify --commitments seasons/s1/commitments.json --seeds seasons/s1/seeds.json seasons/s1` checks them against the nights and draws their cards again. The steps are described under Seed commitments in [TRANSPARENCY.md](../TRANSPARENCY.md). |

## Development

The checks are listed under Checks in [CONTRIBUTING.md](../CONTRIBUTING.md). The pictures of the READMEs and of [how-it-works.md](how-it-works.md) are built from `tools/readme_assets/`, whose [README](../tools/readme_assets/README.md) says how to make them again.
