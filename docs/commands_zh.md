<p align="center"><a href="commands.md">English</a> · <b>简体中文</b></p>

# 命令

`uv run auction-night <命令> --help` 列出一个命令的全部选项。命令成功时退出状态为 0，用法错误或遇到无法使用的文件时为 2。`replay` 和 `export` 在拒绝一夜时退出状态为 1，`show` 在某个节目层模型没有任何回答、费用上限拦下调用或这一夜重放不一致时也是 1，`seeds reveal` 和 `seeds verify` 在种子打不开承诺或核对不通过时同样是 1。

| 命令 | 作用 |
| --- | --- |
| `run -c configs/night_mock.yaml -o runs/mock` | 跑一夜，把日志（`events.jsonl`）、每次模型调用的录像带（`calls.jsonl`）和配置的副本（`config.yaml`）写进一个目录。用 `configs/night_mock.yaml` 时所有模型座位都是模拟模型，不联网，也不需要密钥。 |
| `replay --from runs/mock` | 只用录像带把已录下的一夜重跑一遍，并逐字节核对日志。没给 `-c` 时读 `run` 存下的 `config.yaml`。 |
| `play -c configs/night_mock.yaml -o runs/me --human-seat 3` | 让一个人在终端里坐上一个模型座位，其余座位照配置上场。它同时写出 `human.jsonl`，记下这个人输入的内容，有真人的夜晚不计入积分榜。 |
| `simulate --preset s2 --nights 200` | 只用规则机器人打很多夜，不写任何文件，打印各出价风格的平均盈余及其 95% 区间。 |
| `estimate -c configs/night_bailian.yaml` | 在开跑前估算一夜的 token 数和美元费用。见 [models_zh.md](models_zh.md) 的“成本”一节。 |
| `season runs/mock` | 从已跑完的夜目录建积分榜（每夜平均盈余，带 bootstrap 名次区间），读每个目录里的 `events.jsonl` 和 `config.yaml`。 |
| `export runs/mock` | 把已录下的一夜导出成回放页播放的 `replay.json`。这一夜用录像带重跑，所有公开文字都过内容闸门。被拒绝时（闸门拦下，或重放和日志对不上）不写文件，退出状态为 1。 |
| `show runs/night-01` | 对一夜已录下的比赛调用节目层的模型，并把它们的回答录下来。主持人、两位评委和内容分类器在赛后工作，`export` 从不调用它们：其中某一个是模型时，`export` 从运行目录里的 `host_calls.jsonl`、`judge_calls.jsonl` 或 `scan_calls.jsonl` 读它的回答，这几个文件由 `show` 写出。这一夜用录像带重跑，所以选手座位不调用任何模型。模拟角色当场照常运行，不留文件。调用按价目表计费，`limits.max_cost_per_night_usd` 同样限制节目层自己能花多少。某个模型没有任何回答，或费用上限拦下调用时，不写任何文件，退出状态为 1。已有的录像带需要 `--overwrite` 才会被替换。 |
| `seeds` | 管一个赛季的种子承诺。`seeds new` 为每副牌抽一个秘密种子，写出赛前要公布的承诺，种子本身存进仓库之外的私密文件。`seeds plan` 写出每一夜的配置，`seeds env` 打印让一夜拿到种子的那一行，`seeds reveal` 在赛后打开承诺，`seeds verify --commitments seasons/s1/commitments.json --seeds seasons/s1/seeds.json seasons/s1` 把承诺和各夜对照核对，并重新算一遍各夜的牌。各步怎样衔接，见 [TRANSPARENCY_zh.md](../TRANSPARENCY_zh.md) 里的“种子承诺”一节。 |

## 开发

检查命令见 [CONTRIBUTING_zh.md](../CONTRIBUTING_zh.md) 的“检查”一节。README 和 [how-it-works_zh.md](how-it-works_zh.md) 的配图由 `tools/readme_assets/` 生成。重新生成的办法见那里的 [README](../tools/readme_assets/README_zh.md)。
