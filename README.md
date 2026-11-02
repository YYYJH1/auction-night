<p align="center"><b>English</b> · <a href="README_zh.md">简体中文</a></p>

<p align="center"><picture>
<source media="(prefers-color-scheme: dark)" srcset="docs/assets/banner-en-dark.png">
<img alt="AI Auction Night: six language models and two rule bots, six auction rules, twelve lots" src="docs/assets/banner-en-light.png" width="920"></picture></p>

<p align="center"><b>Six language models and two rule-based bots bid for twelve lots under six textbook auction rules. Afterwards a host and two judges review the night.</b></p>

<p align="center">
<a href="https://github.com/YYYJH1/auction-night/actions/workflows/ci.yml"><img alt="CI status" src="https://github.com/YYYJH1/auction-night/actions/workflows/ci.yml/badge.svg"></a>
<img alt="Python 3.12 or newer" src="https://img.shields.io/badge/python-3.12%2B-3776AB">
<a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-2a3770"></a>
</p>

<p align="center"><picture>
<source media="(prefers-color-scheme: dark)" srcset="docs/assets/lot-dark.gif">
<img alt="The replay page playing one lot: the seats talk, each with a private thought under the line, the sealed bids open one by one, the hammer falls and the blind box shows its true value" src="docs/assets/lot-light.gif" width="880"></picture><br>
<sub>Lot 5 of night <code>trial-d01-r1</code>, played on 2026-11-02 (UTC) by Qwen, DeepSeek and Zhipu models: the blind box winner paid 95 for an item worth 84, the winner's curse.</sub></p>

The night of the GIF plays at <https://yyyjh1.github.io/auction-night/>. The replay page is in Chinese.

## What it is

- Six language models and two rule-based bots bid for twelve lots in one night.
- Each lot is sold under one of six textbook auctions.
- A night has table talk, public pledges, secret missions and a review by a host and two judges.
- Every night is saved as a recording that replays byte for byte without a network, and it is published as a static replay page.

<p align="center"><picture>
<source media="(prefers-color-scheme: dark)" srcset="docs/assets/formats-en-dark.png">
<img alt="Six cards: English button, Dutch one shot, first-price sealed bid, second-price sealed bid, all-pay and blind box. Each gives the rule and the textbook result: exiting at your value is dominant in the English auction, bidding your value is dominant in the second-price auction, first-price bids are shaded to (n-1)/n of the value, and the blind box has a winner's curse" src="docs/assets/formats-en-light.png" width="920"></picture></p>

## Try it

Python 3.12 or newer and [uv](https://docs.astral.sh/uv/) are required. A mock night needs no network and no key.

```
uv sync
uv run auction-night run -c configs/night_mock.yaml -o runs/mock
uv run auction-night replay --from runs/mock
uv run auction-night export runs/mock
python3 -m http.server --bind 127.0.0.1 8000
```

The server serves the folder it is started in, so start it in the root of the repository. Then open <http://127.0.0.1:8000/viewer/index.html?replay=../runs/mock/replay.json>.

## Real models

A night with real models goes through the API of Alibaba Cloud Model Studio (Bailian). The night in the GIF cost about \$1 (¥6.5). An open model on your own machine, served by vLLM or Ollama, plays a night at no cost. The commands and the details are in [docs/models.md](docs/models.md).

## Learn more

- [docs/how-it-works.md](docs/how-it-works.md): what a night looks like, how it goes, the review after it and the architecture
- [docs/models.md](docs/models.md): real models, open models on your own machine and what a night costs
- [docs/commands.md](docs/commands.md): every command and its exit status
- [TRANSPARENCY.md](TRANSPARENCY.md): how a night is run, how collusion is looked for, how seeds are committed and what is published
- [docs/contracts.md](docs/contracts.md): the data shapes and the six mechanism definitions
- [CHANGELOG.md](CHANGELOG.md): what changed in each version
- [CONTRIBUTING.md](CONTRIBUTING.md): the checks and the text scanner

## Acknowledgements

This project is inspired by the projects below.

- [AucArena](https://github.com/jiangjiechen/auction-arena): multi-model arena and pre-auction planning
- [llm-auction](https://github.com/KeHang-Zhu/llm-auction): parameterised rules and offline recomputation
- [Magentic Marketplace](https://github.com/microsoft/multi-agent-marketplace): structured actions and event streams
- [Monitored-Collusion-in-LLM-Auctions](https://github.com/EYITAY/Monitored-Collusion-in-LLM-Auctions): detecting collusion from both chat and bids
- [bid-pricing-collusion](https://github.com/linusc9516/bid-pricing-collusion): a round-by-round static viewer
- [AI_Diplomacy](https://github.com/GoodStartLabs/AI_Diplomacy): two-layer commentary and highlight moments
- [llm-colosseum](https://github.com/OpenGenerativeAI/llm-colosseum): presenting model battles and a leaderboard as a show
- [BidBrawl](https://github.com/merc-hecl/BidBrawl), [qiju](https://github.com/TiantianFlow/qiju) and [blind-auction](https://github.com/20010208/blind-auction): blind-box play, deterministic state machines and seed commitments
- [collusion-harness](https://github.com/ArnolFokam/collusion-harness): a missing action never defaults to a price
- [bazaar](https://github.com/lechmazur/bazaar): excess return relative to a baseline

## License

MIT, see [LICENSE](LICENSE).
