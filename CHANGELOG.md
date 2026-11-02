<p align="center"><b>English</b> · <a href="CHANGELOG_zh.md">简体中文</a></p>

# Changelog

All notable changes to this project are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows [Semantic Versioning](https://semver.org/).

## [0.1.1] - 2026-11-02

### Added

- **Season s1.** Six nights of the Bailian line-up on one deck, every model at every seat once. The commitments were pushed before the first night and the seeds revealed after the last. `seasons/s1/` holds the configurations, logs and replays of the nights with the leaderboard and the seeds, and `docs/seasons.md` describes the season.

### Changed

- The review picture shows all four predictions, and the architecture figure says that only `show` calls the models of the show layer.
- The vLLM example serves on port 8001, so that it leaves port 8000 to the server of the replay page.

### Fixed

- `configs/night_local.yaml` carried a comment of the mock night. It is gone.
- The documents give the show-layer cost of the demo night as it was recorded and put the budget guesses in the chat instead of the opening plan. The predictions of the host are described as the show's own bets, written with reference to a study.
- The help of `season` lists every reason a night is left out, the help of `simulate` defines its styles, and `--preset` says what its setups are.
- The replay page names all three arrangements of the chat side of collusion, and the awards page says once that it shows recorded results.

## [0.1.0] - 2026-11-02

The first release. From this version on the contracts are frozen: fields and values may be added, and nothing is changed or removed (see [docs/contracts.md](docs/contracts.md)).

### Added

- **Six auctions.** English button, Dutch, first-price sealed bid, second-price sealed bid, all-pay and blind box. The rule texts, the worked payment examples and the settlement come from the same definitions.
- **The night.** Two to eight seats, each a language model, a scripted mock, a rule bot (the economist and the truthful bot) or a person at the terminal. Private budgets, values and secret missions, a public chat with structured pledges and guesses of other seats' budgets, and an opening plan. A variety mode adds personas that change the tone of a seat.
- **The model layer.** Anthropic and OpenAI-compatible endpoints behind one `act` tool, a cassette of every call, and a replay from the cassette that gives the same log byte for byte without a network. Dated price lists with an exchange rate, a cost estimate before a run and a cost cap during it.
- **The show layer.** Fact cards, an offline host, two judges from different vendors, findings for broken pledges, false claims and collusion, awards, highlights and pre-show predictions. A content gate (word list, classifier model and a file for the findings a person releases) stands between a night and its replay file.
- **Seeds and seasons.** Seed commitments for a season (`seeds new`, `plan`, `env`, `reveal` and `verify`), rotations of the players over a deck, and a leaderboard with bootstrap rank intervals that anyone can recompute from the logs.
- **The replay page.** A static page without dependencies that plays a replay lot by lot, with a three-minute version of the night, highlights, awards and the host's review.
- **Open models on your own machine.** `configs/night_local.yaml` seats one model behind a local OpenAI-compatible server (vLLM, Ollama and the like) in all six model seats, with the scripted host and judges, so a night costs nothing.
- **The command line.** `run`, `replay`, `play`, `simulate`, `estimate`, `season`, `export`, `show` and `seeds`.
- **Schemas.** JSON Schemas of the configuration, the events, the replay and the price list, and of the two `act` tool definitions, in `schemas/`.

[0.1.1]: https://github.com/YYYJH1/auction-night/releases/tag/v0.1.1
[0.1.0]: https://github.com/YYYJH1/auction-night/releases/tag/v0.1.0
