<p align="center"><b>English</b> · <a href="seasons_zh.md">简体中文</a></p>

# Seasons

A season is a set of nights whose deal seeds are committed before the first night and revealed after the last. In a rotation season the model seats of a deck are played by the same models in every night, and the models move one seat on from night to night, so that over a deck every model sits at every seat once with the same cards. The steps are described under Seed commitments in [TRANSPARENCY.md](../TRANSPARENCY.md).

## Season s1

| | |
|---|---|
| Played | 2026-11-02 (UTC), on Alibaba Cloud Model Studio (Bailian) |
| Line-up | the template [configs/night_bailian.yaml](../configs/night_bailian.yaml): qwen-flash, qwen3.7-plus, deepseek-v4.1-flash, deepseek-v4-flash-0731, glm-5 and glm-5.1, with the economist and the truthful bot |
| Nights | one deck (d01) and six rotations, `s1-d01-r0` to `s1-d01-r5` |
| Seeds | the commitments were pushed before the first night (commit `b4ee7e5`) and the seeds revealed after the last |
| Cost | \$5.91 for the seats and the show layer of the six nights, ¥39.33 at the rate in the price list |

### Leaderboard

Mean surplus per night, which is the value of the lots won minus the price paid. The mission bonus is not included.

| # | Entry | Nights | Mean surplus | Abstentions |
|---|---|---|---|---|
| 1 | deepseek-v4-flash-0731 | 6 | 32.5 | 5.5% |
| 2 | glm-5.1 | 6 | 29.5 | 0.0% |
| 3 | deepseek-v4.1-flash | 6 | 23.7 | 0.0% |
| 4 | glm-5 | 6 | 23.2 | 0.0% |
| 5 | qwen3.7-plus | 6 | 22.0 | 1.2% |
| 6 | qwen-flash | 6 | -19.7 | 0.0% |
| 7 | truthful bot | 6 | -70.3 | 0.0% |
| 8 | economist | 6 | -158.0 | 0.0% |

Rank intervals are drawn over decks, and s1 has one deck, so the board does not say which differences can be told apart. The board is `seasons/s1/leaderboard.json`, as `auction-night season` wrote it. Of the missions, deepseek-v4-flash-0731 completed two of its six and deepseek-v4.1-flash one of its six, and the other models completed none.

### Content gate

The classifier raised three findings, one each in `s1-d01-r1`, `s1-d01-r3` and `s1-d01-r4`, and all three were read and cleared by hand. Two are mission kinds (`shill` and `collector`) in the extractions of a judge, and one is a code name in a line of the host, taken for the name of a real person. The manifest of each replay lists its finding under `cleared_findings`.

### Replays

- [s1-d01-r0](https://yyyjh1.github.io/auction-night/viewer/index.html?replay=../seasons/s1/s1-d01-r0/replay.json)
- [s1-d01-r1](https://yyyjh1.github.io/auction-night/viewer/index.html?replay=../seasons/s1/s1-d01-r1/replay.json)
- [s1-d01-r2](https://yyyjh1.github.io/auction-night/viewer/index.html?replay=../seasons/s1/s1-d01-r2/replay.json)
- [s1-d01-r3](https://yyyjh1.github.io/auction-night/viewer/index.html?replay=../seasons/s1/s1-d01-r3/replay.json)
- [s1-d01-r4](https://yyyjh1.github.io/auction-night/viewer/index.html?replay=../seasons/s1/s1-d01-r4/replay.json)
- [s1-d01-r5](https://yyyjh1.github.io/auction-night/viewer/index.html?replay=../seasons/s1/s1-d01-r5/replay.json)

### Files

`seasons/s1/` holds `commitments.json`, `seeds.json` and `leaderboard.json`, and for every night the configuration it was played with (`config.yaml`), its log (`events.jsonl`) and its replay (`replay.json`). The recordings of the model calls are not in the repository. CI rebuilds the leaderboard from the logs with `tools/check_leaderboard.py` and checks the seeds with `tools/check_seeds.py`. The same check of the seeds runs anywhere:

```
uv run auction-night seeds verify --commitments seasons/s1/commitments.json --seeds seasons/s1/seeds.json seasons/s1 --strict
```
