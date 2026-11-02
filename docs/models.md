<p align="center"><b>English</b> · <a href="models_zh.md">简体中文</a></p>

# Models and cost

Run the commands in the root of the repository, after `uv sync` as in [Try it](../README.md).

## Alibaba Cloud Model Studio

A night with real models goes through Alibaba Cloud Model Studio (Bailian), whose OpenAI-compatible endpoint serves Qwen, DeepSeek, Zhipu and Kimi models. `configs/night_bailian.yaml` is the line-up of a trial night and the template of a season: it holds no deal seed, so `seeds new` draws one, `seeds plan` writes the configuration of each night, and `seeds env` prints the line that hands a night its seed.

```
uv run auction-night estimate -c configs/night_bailian.yaml
uv run auction-night seeds new --season s1 --decks 1 --private-out ~/private/s1.seeds-private.json
uv run auction-night seeds plan -t configs/night_bailian.yaml -c seasons/s1/commitments.json
eval "$(uv run auction-night seeds env --private ~/private/s1.seeds-private.json --deck d01)"
export DASHSCOPE_API_KEY=<your key>
uv run auction-night run -c seasons/s1/configs/s1-d01-r0.yaml -o runs/night-01
uv run auction-night show runs/night-01
uv run auction-night export runs/night-01
```

## Open models on your own machine

A night can also run on an open model on your own machine, with nothing paid. `configs/night_local.yaml` puts one model in all six model seats and keeps the host and the judges scripted, so one model server is enough. Any server that speaks the OpenAI-compatible API works: vLLM on a GPU, or Ollama on a laptop.

```
vllm serve Qwen/Qwen3-4B --served-model-name qwen3-4b \
  --enable-auto-tool-choice --tool-call-parser hermes --reasoning-parser qwen3
export LOCAL_LLM_BASE_URL=http://127.0.0.1:8000/v1
export AUCTION_NIGHT_SEED_LOCAL=$(python3 -c "import secrets; print(secrets.token_hex(16))")
uv run auction-night run -c configs/night_local.yaml -o runs/local
uv run auction-night export runs/local
```

With Ollama, run `ollama pull qwen3:4b`, set `LOCAL_LLM_BASE_URL` to `http://127.0.0.1:11434/v1`, and write `model: qwen3:4b` in the seats of the configuration. On 2026-11-02 (UTC) Qwen3-4B on vLLM played a night in 142 s on one GPU, and all 139 model calls gave a valid action at the first attempt. A model of 0.8B parameters filled the fields of the wrong phase in most requests, so use 4B parameters or more.

## Cost

`estimate` prices a night before it runs, from the configuration and a dated price list. It counts the model seats, and also the host, the judges and the content classifier when they are models. A price list may be in yuan: it then carries a dated exchange rate, costs are still counted in dollars, and `estimate` prints the yuan beside them.

For `configs/night_bailian.yaml` with `configs/prices.bailian.yaml` (prices of 2026-11-01 and the rate of 2026-10-30, 1 USD = 6.6529 CNY) it prints:

| One night | Low | High |
| --- | --- | --- |
| Six model seats | \$0.25 (¥1.68) | \$0.53 (¥3.54) |
| Host, judges and content scan | \$0.04 (¥0.27) | \$0.15 (¥1.02) |
| Together | \$0.29 (¥1.95) | \$0.69 (¥4.56) |

Low assumes no thinking tokens, and High assumes 1,000 thinking tokens per call. The models of the two trial nights of 2026-11-02 (UTC) thought for longer, and the opening plan alone took some of them several thousand tokens. Night `trial-d01-r1` played all twelve lots for \$0.89 (¥5.92) in the seats and \$0.06 (¥0.42) in the show layer. Night `trial-d01-r0` had a cap of \$0.60 and reached it at lot 10. The file sets `limits.max_cost_per_night_usd` to 1.50 (about ¥10), and a night that reaches the cap is played on with the models silent and flagged. The template still carries a placeholder for the commitment of the deal seed, which `estimate` points out and `seeds plan` fills in.
