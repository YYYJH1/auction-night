<p align="center"><a href="models.md">English</a> · <b>简体中文</b></p>

# 模型与成本

命令在仓库根目录运行。先按 [试一试](../README_zh.md#试一试) 做 `uv sync`。

## 阿里云百炼

真实模型的一夜走阿里云百炼（Bailian），它兼容 OpenAI 的端点提供千问、DeepSeek、智谱和 Kimi 的模型。`configs/night_bailian.yaml` 是试跑夜的阵容，也是一个赛季的模板：它不含发牌种子，所以先用 `seeds new` 抽种子，再用 `seeds plan` 写出每一夜的配置，`seeds env` 打印让一夜拿到种子的那一行。

```
uv run auction-night estimate -c configs/night_bailian.yaml
uv run auction-night seeds new --season s1 --decks 1 --private-out ~/private/s1.seeds-private.json
uv run auction-night seeds plan -t configs/night_bailian.yaml -c seasons/s1/commitments.json
eval "$(uv run auction-night seeds env --private ~/private/s1.seeds-private.json --deck d01)"
export DASHSCOPE_API_KEY=<你的密钥>
uv run auction-night run -c seasons/s1/configs/s1-d01-r0.yaml -o runs/night-01
uv run auction-night show runs/night-01
uv run auction-night export runs/night-01
```

## 自己机器上的开源模型

也可以在自己的机器上用开源模型跑一夜，不花一分钱。`configs/night_local.yaml` 让一个模型坐满六个模型座位，主持人和评委用脚本角色，所以一台模型服务就够。任何说 OpenAI 兼容接口的服务都能接：有 GPU 用 vLLM，笔记本上用 Ollama。

```
vllm serve Qwen/Qwen3-4B --served-model-name qwen3-4b --port 8001 \
  --enable-auto-tool-choice --tool-call-parser hermes --reasoning-parser qwen3
export LOCAL_LLM_BASE_URL=http://127.0.0.1:8001/v1
export AUCTION_NIGHT_SEED_LOCAL=$(python3 -c "import secrets; print(secrets.token_hex(16))")
uv run auction-night run -c configs/night_local.yaml -o runs/local
uv run auction-night export runs/local
```

用 Ollama 的话，先 `ollama pull qwen3:4b`，把 `LOCAL_LLM_BASE_URL` 设为 `http://127.0.0.1:11434/v1`，再把配置里座位的模型写成 `model: qwen3:4b`。2026-11-02（UTC）实测：Qwen3-4B 在 vLLM 上用一张 GPU 跑完一夜用了 142 秒，139 次模型调用都一次给出有效动作。0.8B 参数的模型在多数请求里填成了别的阶段的字段，所以请用 4B 及以上的模型。

## 成本

`estimate` 在开跑前按配置和带日期的价目表，估算一夜的花费。除了模型座位，主持人、评委和内容分类器只要是模型，也一并估算。价目表可以用人民币标价：这时它带一个注明日期的汇率，费用仍按美元记，`estimate` 把人民币金额列在美元旁边。

对 `configs/night_bailian.yaml` 和 `configs/prices.bailian.yaml`（2026-11-01 的价格和 2026-10-30 的汇率，1 美元 = 6.6529 元），它给出：

| 一夜 | 低 | 高 |
| --- | --- | --- |
| 六个模型座位 | \$0.25（¥1.68） | \$0.53（¥3.54） |
| 主持人、评委和内容筛查 | \$0.04（¥0.27） | \$0.15（¥1.02） |
| 合计 | \$0.29（¥1.95） | \$0.69（¥4.56） |

低档假设没有思考 token，高档假设每次调用 1,000 个思考 token。2026-11-02（UTC）两个试跑夜里，模型想得更久，有的模型光作战计划就用了几千个思考 token。夜号 `trial-d01-r1` 打完全部 12 件，座位花 \$0.89（¥5.92），节目层花 \$0.09（¥0.59）。夜号 `trial-d01-r0` 的上限是 \$0.60，打到第 10 件时触顶。该文件把 `limits.max_cost_per_night_usd` 设为 1.50（约 ¥10），达到上限的一夜会让模型沉默着打完，并加上标记。模板里带有发牌种子承诺的占位符，`estimate` 会指出来，`seeds plan` 会填上。
