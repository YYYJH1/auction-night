<p align="center"><a href="seasons.md">English</a> · <b>简体中文</b></p>

# 赛季

赛季是一组夜晚，发牌种子在第一夜之前承诺，在最后一夜之后揭晓。轮座赛季里，一副牌的每一夜都由同一批模型坐模型座位，模型每夜往后挪一个座位，所以一副牌打完，每个模型都在同样的牌下把每个座位坐过一次。步骤见 [TRANSPARENCY_zh.md](../TRANSPARENCY_zh.md) 的“种子承诺”一节。

## 赛季 s1

| | |
|---|---|
| 时间 | 2026-11-02（UTC），在阿里云百炼上打 |
| 阵容 | 模板 [configs/night_bailian.yaml](../configs/night_bailian.yaml)：qwen-flash、qwen3.7-plus、deepseek-v4.1-flash、deepseek-v4-flash-0731、glm-5 和 glm-5.1，另有经济学家和老实人两个规则机器人 |
| 夜数 | 一副牌（d01），六次轮座，`s1-d01-r0` 到 `s1-d01-r5` |
| 种子 | 承诺在第一夜之前推送（提交 `b4ee7e5`），种子在最后一夜之后揭晓 |
| 花费 | 六夜的座位和节目层共 \$5.91，按价目表里的汇率合 ¥39.33 |

### 积分榜

按每夜平均盈余排名，盈余是拍下的东西的价值减去付出的价钱，不含任务奖金。

| # | 参赛者 | 夜数 | 平均盈余 | 弃权率 |
|---|---|---|---|---|
| 1 | deepseek-v4-flash-0731 | 6 | 32.5 | 5.5% |
| 2 | glm-5.1 | 6 | 29.5 | 0.0% |
| 3 | deepseek-v4.1-flash | 6 | 23.7 | 0.0% |
| 4 | glm-5 | 6 | 23.2 | 0.0% |
| 5 | qwen3.7-plus | 6 | 22.0 | 1.2% |
| 6 | qwen-flash | 6 | -19.7 | 0.0% |
| 7 | 老实人（机器人） | 6 | -70.3 | 0.0% |
| 8 | 经济学家（机器人） | 6 | -158.0 | 0.0% |

名次区间是在不同的牌之间重抽算出来的，s1 只有一副牌，所以这张榜分不出哪些差距是真的。榜单文件是 `seasons/s1/leaderboard.json`，由 `auction-night season` 写出。任务方面，deepseek-v4-flash-0731 的六次任务完成了两次，deepseek-v4.1-flash 完成了一次，其他模型都没有完成。

### 内容闸门

分类器一共标出三处，`s1-d01-r1`、`s1-d01-r3` 和 `s1-d01-r4` 各一处，三处都经人读过后手动放行。两处是评委摘录里的任务类型取值（`shill` 和 `collector`），一处是主持人点评里的座位代号，被当成了真人姓名。每份回放的清单在 `cleared_findings` 里列出了它的那一处。

### 回放

- [s1-d01-r0](https://yyyjh1.github.io/auction-night/viewer/index.html?replay=../seasons/s1/s1-d01-r0/replay.json)
- [s1-d01-r1](https://yyyjh1.github.io/auction-night/viewer/index.html?replay=../seasons/s1/s1-d01-r1/replay.json)
- [s1-d01-r2](https://yyyjh1.github.io/auction-night/viewer/index.html?replay=../seasons/s1/s1-d01-r2/replay.json)
- [s1-d01-r3](https://yyyjh1.github.io/auction-night/viewer/index.html?replay=../seasons/s1/s1-d01-r3/replay.json)
- [s1-d01-r4](https://yyyjh1.github.io/auction-night/viewer/index.html?replay=../seasons/s1/s1-d01-r4/replay.json)
- [s1-d01-r5](https://yyyjh1.github.io/auction-night/viewer/index.html?replay=../seasons/s1/s1-d01-r5/replay.json)

### 文件

`seasons/s1/` 里有 `commitments.json`、`seeds.json` 和 `leaderboard.json`，每一夜还有它所用的配置（`config.yaml`）、日志（`events.jsonl`）和回放（`replay.json`）。模型调用的录像不在仓库里。CI 用 `tools/check_leaderboard.py` 从日志重建积分榜，用 `tools/check_seeds.py` 核对种子。同样的种子核对在任何地方都能跑：

```
uv run auction-night seeds verify --commitments seasons/s1/commitments.json --seeds seasons/s1/seeds.json seasons/s1 --strict
```
