<p align="center"><a href="CHANGELOG.md">English</a> · <b>简体中文</b></p>

# 更新日志

本项目所有值得注意的改动都列在这里。格式遵循 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)。项目遵循 [Semantic Versioning](https://semver.org/)。

## [0.1.0] - 2026-11-02

第一个版本。从这个版本起契约冻结：可以新增字段和取值，已有的内容不改动也不删除（见 [docs/contracts_zh.md](docs/contracts_zh.md)）。

### 新增

- **六种拍卖。** 英式加价、荷式降价、密封一价、密封二价、全员付费和盲盒。规则文字、付款例子和结算出自同一份定义。
- **一夜。** 两到八个座位，每个座位可以是大模型、脚本模拟模型、规则机器人（经济学家和老实人）或终端前的真人。私密的预算、估值和秘密任务，带结构化誓约的公开聊天，以及带读心猜测的开场计划。综艺模式（variety）加入会改变座位语气的人设。
- **模型层。** 统一接在一个 `act` 工具后面的 Anthropic 端点和兼容 OpenAI 的端点、每次调用的录像带，以及从录像带出发的重放（不联网也能得到逐字节相同的日志）。带汇率、注明日期的价目表，运行前的费用估算和运行中的费用上限。
- **节目层。** 事实卡、离线主持人、来自不同厂商的两位评委、针对违约誓约、不实说法和合谋的判定、奖项、高光和赛前预测。内容闸门（词表、分类器模型，以及记录由人放行的命中项的文件）挡在一夜和它的回放文件之间。
- **种子与赛季。** 赛季的种子承诺（`seeds new`、`plan`、`env`、`reveal` 和 `verify`），选手在一副牌上的轮座，以及带 bootstrap 名次区间、任何人都能从日志重新算出的积分榜。
- **回放页。** 一个没有依赖的静态页面，逐件播放回放，附有三分钟版的一夜、高光、颁奖和主持人复盘。
- **在自己的机器上跑开源模型。** `configs/night_local.yaml` 让本地兼容 OpenAI 的服务（vLLM、Ollama 之类）上的同一个模型坐满六个模型座位，并让主持人和评委用脚本角色，所以一夜不花钱。
- **命令行。** `run`、`replay`、`play`、`simulate`、`estimate`、`season`、`export`、`show` 和 `seeds`。
- **Schema。** 配置、事件、回放和价目表的 JSON Schema，以及两份 `act` 工具定义的 JSON Schema，放在 `schemas/` 里。

[0.1.0]: https://github.com/YYYJH1/auction-night/releases/tag/v0.1.0
