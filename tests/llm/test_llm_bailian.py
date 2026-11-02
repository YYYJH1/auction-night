"""The first night on Alibaba Cloud Model Studio (Bailian): prices in yuan, the estimate, the
template configuration, the fields of a request, and replies as Bailian writes them.

Every model is a fake here. Nothing calls an API, reads a key or touches the network.
"""

import asyncio
import copy
import inspect
import json
import random
import re
from collections import Counter
from decimal import Decimal
from fractions import Fraction
from math import ceil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import openai
import pytest
import yaml
from llm_replies import ctx as parse_ctx
from openai.resources.chat.completions import Completions
from pydantic import ValidationError
from sdk_fakes import (
    FIXED_TIME,
    REPO,
    CallContext,
    FakeNight,
    ScriptedSdk,
    Step,
    clients_for,
    valid_args,
)
from typer.testing import CliRunner

from auction_night.cli import app
from auction_night.contracts import NightConfig, find_placeholders, load_config
from auction_night.contracts.config import ExchangeRate, ModelPrice
from auction_night.llm.accounting import (
    Charge,
    ExchangeRateError,
    Ledger,
    PriceTable,
    call_cost_microusd,
    estimate_night,
    exact_cost_microusd,
    format_estimate,
)
from auction_night.llm.cassette import CallKey, Cassette
from auction_night.llm.client import Usage, normalise_openai, openai_usage
from auction_night.llm.nightrun import replay_directory, run_directory
from auction_night.llm.orchestrate import ModelRunner, NightSession, RunnerSettings, models_in_use
from auction_night.llm.parse import parse_reply
from auction_night.llm.request import build_request, user_message
from auction_night.show.content_scan import ModelClassifier, Probe
from auction_night.show.host import HostJob, ModelHost
from auction_night.show.judges import JudgeJob, ModelJudge

TEMPLATE = REPO / "configs" / "night_bailian.yaml"
PRICES = REPO / "configs" / "prices.bailian.yaml"
RATE = Decimal("6.6529")
M = 1_000_000
SEAT_MODELS = [
    "qwen3.7-plus",
    "qwen-flash",
    "deepseek-v4.1-flash",
    "deepseek-v4-flash-0731",
    "glm-5",
    "glm-5.1",
]
ALL_MODELS = sorted([*SEAT_MODELS, "qwen-plus", "kimi-k2.5"])  # the seats and the show roles
FAST = RunnerSettings(timeout_s=5.0, backoff_base_s=0.0, backoff_cap_s=0.0, wall_clock_slack_s=0.5)
cli = CliRunner()


def plain(text: str) -> str:
    """Output without colour codes: typer styles its help and its errors on CI."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def bailian_config() -> NightConfig:
    return load_config(TEMPLATE)


def bailian_prices() -> PriceTable:
    return PriceTable.from_file(PRICES)


def yuan_price(inp: float, out: float, **more: float | None) -> ModelPrice:
    return ModelPrice(
        input_per_mtok=inp,
        output_per_mtok=out,
        currency="CNY",
        as_of="2026-11-01",  # type: ignore[arg-type]
        source="https://example.com/prices",
        **more,  # type: ignore[arg-type]
    )


def rate(units: float = 6.6529) -> ExchangeRate:
    return ExchangeRate(
        units_per_usd=units,
        as_of="2026-10-30",  # type: ignore[arg-type]
        source="https://example.com/rate",
    )


def exact_microusd(yuan_per_mtok: Fraction | str | int, tokens: int) -> Fraction:
    """The cost in micro-dollars of ``tokens`` at a price in yuan, as an exact fraction."""
    return Fraction(tokens) * Fraction(str(yuan_per_mtok)) / Fraction(str(RATE))


# ============================================================ the conversion of a price


def test_6_6529_yuan_are_exactly_one_dollar_at_the_rate_of_the_list() -> None:
    price = yuan_price(6.6529, 13.3058)
    assert call_cost_microusd(price, Usage(input_tokens=M), RATE) == 1_000_000  # no float noise
    assert call_cost_microusd(price, Usage(output_tokens=M), RATE) == 2_000_000
    assert exact_cost_microusd(price, Usage(input_tokens=M), RATE) == Decimal(1_000_000)


def test_a_price_in_yuan_costs_tokens_times_price_over_the_rate_rounded_up_once() -> None:
    price = yuan_price(2, 8, cache_read_per_mtok=0.5)
    usage = Usage(input_tokens=1234, output_tokens=5678, cache_read_input_tokens=9012)
    exact = exact_microusd(2, 1234) + exact_microusd(8, 5678) + exact_microusd("0.5", 9012)
    assert call_cost_microusd(price, usage, RATE) == ceil(exact)
    unrounded = exact_cost_microusd(price, usage, RATE)
    assert abs(unrounded - Decimal(exact.numerator) / Decimal(exact.denominator)) < Decimal("1e-20")


@pytest.mark.parametrize("seed", range(5))
def test_the_conversion_never_lowers_a_cost_and_is_never_off_by_a_whole_unit(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(200):
        inp, out = rng.choice([0.15, 0.8, 2, 3, 4, 6]), rng.choice([1.5, 8, 9, 18, 21, 24])
        usage = Usage(
            input_tokens=rng.randrange(0, 200_000),
            output_tokens=rng.randrange(0, 40_000),
            cache_read_input_tokens=rng.randrange(0, 100_000),
        )
        price = yuan_price(inp, out)
        exact = (
            exact_microusd(str(inp), usage.input_tokens + usage.cache_read_input_tokens)
            + exact_microusd(str(out), usage.output_tokens)
        )  # fmt: skip
        assert call_cost_microusd(price, usage, RATE) == ceil(exact), (inp, out, usage)


def test_reasoning_tokens_in_yuan_are_part_of_the_output_and_billed_once() -> None:
    price = yuan_price(2, 8)
    plain_usage = Usage(input_tokens=100, output_tokens=1000)
    with_reasoning = Usage(input_tokens=100, output_tokens=1000, reasoning_tokens=800)
    assert call_cost_microusd(price, plain_usage, RATE) == call_cost_microusd(
        price, with_reasoning, RATE
    )
    assert call_cost_microusd(price, with_reasoning, RATE) == ceil(
        exact_microusd(2, 100) + exact_microusd(8, 1000)
    )


def test_a_price_in_yuan_is_never_taken_for_dollars() -> None:
    price = yuan_price(2, 8)
    with pytest.raises(ExchangeRateError, match="CNY"):
        call_cost_microusd(price, Usage(input_tokens=1))
    with pytest.raises(ExchangeRateError):
        exact_cost_microusd(price, Usage(input_tokens=1))


def test_a_table_with_a_price_in_yuan_needs_the_rate() -> None:
    with pytest.raises(ExchangeRateError, match="no exchange rate.*CNY"):
        PriceTable({"m": yuan_price(2, 8)}, source="list.yaml")
    table = PriceTable({"m": yuan_price(2, 8)}, source="list.yaml", rates={"CNY": rate()})
    found = table.lookup("m")
    assert found is not None and found[1].currency == "CNY", "the price stays as it was written"
    assert table.units_per_usd(found[1]) == RATE


def test_prices_in_dollars_need_no_rate_and_give_none() -> None:
    dollars = ModelPrice(
        input_per_mtok=1,
        output_per_mtok=2,
        as_of="2026-11-01",  # type: ignore[arg-type]
        source="https://example.com/p",
    )
    table = PriceTable({"m": dollars})
    assert table.units_per_usd(dollars) is None and table.rates == {}
    with_rate = PriceTable({"m": dollars}, rates={"CNY": rate()})
    assert with_rate.units_per_usd(dollars) is None, "the rate is only for showing yuan"
    assert call_cost_microusd(dollars, Usage(input_tokens=M)) == 1_000_000


def test_the_list_for_bailian_loads_as_a_table_with_its_rate() -> None:
    table = bailian_prices()
    assert sorted(table.prices) == ALL_MODELS
    assert table.rates["CNY"].units_per_usd == 6.6529
    assert table.source is not None and table.source.endswith("prices.bailian.yaml")


# ======================================================== the ledger and the cap in dollars


def test_the_ledger_counts_micro_dollars_for_a_price_in_yuan() -> None:
    ledger = Ledger(bailian_prices())
    usage = Usage(input_tokens=3000, output_tokens=1500, reasoning_tokens=1300)
    cost = ledger.cost_of(usage, "qwen3.7-plus")
    assert cost == ceil(exact_microusd(2, 3000) + exact_microusd(8, 1500)) == 2706
    assert ledger.cost_of(usage, "no-such-model") is None, "an unknown model has no price"
    assert ledger.cost_of(usage, "gpt-nothing", "qwen3.7-plus") == cost, "the second id is tried"


def test_the_cost_cap_is_in_dollars_whatever_the_list_is_written_in() -> None:
    ledger = Ledger(bailian_prices(), cap_microusd=750_000)  # 0.75 USD, about 4.99 yuan
    ledger.require_prices(models_in_use(bailian_config()))
    key = CallKey("n-1", 1, "sealed", 1, 1)
    # 500,000 output tokens of qwen3.7-plus are 4 yuan, 0.601242 dollars: under the cap
    four_yuan = Usage(output_tokens=500_000)
    cost = ledger.cost_of(four_yuan, "qwen3.7-plus")
    assert cost == ceil(exact_microusd(8, 500_000)) == 601_242
    ledger.fold([Charge(key, "seat:1", four_yuan, cost)])
    assert ledger.gate() is None and not ledger.cap_reached
    # 200,000 more are 1.6 yuan: 5.6 yuan is above the cap of 4.989675 yuan, so the gate fires
    more = Usage(output_tokens=200_000)
    cost_more = ledger.cost_of(more, "qwen3.7-plus")
    assert cost_more == 240_497
    ledger.fold([Charge(CallKey("n-1", 2, "sealed", 2, 1), "seat:1", more, cost_more)])
    event = ledger.gate()
    assert event is not None and event.kind == "cap_reached"
    assert (event.spent_microusd, event.cap_microusd) == (601_242 + 240_497, 750_000)


def test_the_cap_needs_a_price_for_every_model_of_the_night() -> None:
    config = bailian_config()
    assert config.limits.max_cost_microusd == 1_500_000
    ledger = Ledger(bailian_prices(), cap_microusd=config.limits.max_cost_microusd)
    ledger.require_prices(models_in_use(config))
    assert sorted(set(models_in_use(config))) == ALL_MODELS


# ===================================================================== the estimate


def test_the_estimate_prices_every_seat_and_every_role_of_the_bailian_night() -> None:
    estimate = estimate_night(bailian_config(), bailian_prices())
    assert [s.model for s in estimate.seats] == SEAT_MODELS
    assert all(s.priced for s in estimate.seats) and estimate.unpriced_models == []
    assert [(r.role, r.model) for r in estimate.show] == [
        ("host", "kimi-k2.5"),
        ("judge:0", "kimi-k2.5"),
        ("judge:1", "qwen-plus"),
        ("classifier", "kimi-k2.5"),
    ]
    assert all(r.priced for r in estimate.show) and estimate.unpriced_show_models == []
    assert estimate.cap_microusd == 1_500_000 and set(estimate.rates) == {"CNY"}
    low, high = estimate.night_total_microusd()
    assert Decimal(0) < low < high < Decimal(1_500_000), "a night fits the cap of 1.50 dollars"


def test_a_seat_costs_its_tokens_at_the_yuan_price_over_the_rate() -> None:
    estimate = estimate_night(bailian_config(), bailian_prices())
    seat = estimate.seats[0]  # qwen3.7-plus: 2 yuan in, 8 yuan out
    for bill, cost in ((seat.low, seat.cost_low_microusd), (seat.high, seat.cost_high_microusd)):
        yuan_micro = (bill.input + bill.cache_write + bill.cache_read) * 2 + bill.output * 8
        assert cost is not None
        assert abs(cost - yuan_micro / RATE) < Decimal("1e-12"), "converted once, and only once"
    # the yuan it prints are the yuan it was priced in
    text = format_estimate(estimate)
    low_yuan = (
        seat.low.input + seat.low.cache_write + seat.low.cache_read
    ) * 2 + seat.low.output * 8
    assert f"¥{low_yuan / M:.4f}" in text


def test_the_report_shows_yuan_beside_dollars_and_names_the_rate_and_its_date() -> None:
    text = format_estimate(estimate_night(bailian_config(), bailian_prices()))
    lines = text.splitlines()
    assert lines[1].startswith("Prices: ") and lines[1].endswith("prices.bailian.yaml")
    assert (
        "Exchange rate: 1 USD = 6.6529 CNY (as of 2026-10-30, "
        "https://api.frankfurter.dev/v1/latest?base=USD&symbols=CNY)"
    ) in lines
    header = next(line for line in lines if line.startswith("seat"))
    assert "cost per night" in header and header.endswith("in CNY")
    seat_rows = [line for line in lines if re.match(r"\d\s{2,}\S", line)]
    assert len(seat_rows) == 6 and all("$" in row and "¥" in row for row in seat_rows)
    total = next(line for line in lines if line.startswith("Total for 1 night"))
    dollars = [float(x) for x in re.findall(r"\$(\d+\.\d+)", total)]
    yuan = [float(x) for x in re.findall(r"¥(\d+\.\d+)", total)]
    assert len(dollars) == len(yuan) == 2
    for d, y in zip(dollars, yuan, strict=True):
        assert abs(y - d * 6.6529) < 0.0002 * 6.6529 + 0.0001, (d, y)
    assert "Seats and show layer together, 1 night: $" in text and "¥" in text
    assert (
        "Cost cap per night: $1.5000 = ¥9.9794 (the high estimate for one night is within" in text
    )
    assert "The ledger and the cost cap count US dollars." in text


def test_the_show_layer_rows_have_yuan_too() -> None:
    lines = format_estimate(estimate_night(bailian_config(), bailian_prices())).splitlines()
    rows = [line for line in lines if line.startswith(("host ", "judge:", "classifier"))]
    assert len(rows) == 4 and all("$" in row and "¥" in row for row in rows)
    show = next(line for line in lines if line.startswith("Show layer for 1 night"))
    assert "$" in show and "¥" in show


def test_an_unpriced_model_has_no_yuan_amount_either() -> None:
    table = bailian_prices()
    fewer = PriceTable(
        {m: p for m, p in table.prices.items() if m != "glm-5"}, table.source, table.rates
    )
    text = format_estimate(estimate_night(bailian_config(), fewer))
    row = next(line for line in text.splitlines() if " glm-5 " in line)
    assert row.endswith("no price") and "$" not in row and "¥" not in row
    assert "No price for: glm-5. Add them to the price list; no price is assumed." in text
    assert "5 of 6 model seats priced" in text


def test_a_list_in_dollars_prints_no_yuan_and_no_rate() -> None:
    text = format_estimate(
        estimate_night(
            load_config(REPO / "configs" / "night_api.example.yaml"),
            PriceTable.from_file(REPO / "configs" / "prices.example.yaml"),
        )
    )
    assert "¥" not in text and "Exchange rate" not in text and "in CNY" not in text
    assert "$0.0051 - $0.0202" in text


def test_a_rate_on_a_list_in_dollars_adds_the_yuan_column() -> None:
    config = load_config(REPO / "configs" / "night_api.example.yaml")
    table = PriceTable.from_file(REPO / "configs" / "prices.example.yaml")
    shown = PriceTable(table.prices, table.source, {"CNY": rate(7.0)})
    estimate = estimate_night(config, shown)
    text = format_estimate(estimate)
    assert "Exchange rate: 1 USD = 7 CNY (as of 2026-10-30, https://example.com/rate)" in text
    haiku = estimate.seats[1]
    assert haiku.cost_low_microusd is not None and haiku.cost_high_microusd is not None
    row = next(line for line in text.splitlines() if " claude-haiku-5-5 " in line)
    low, high = haiku.cost_low_microusd / M * 7, haiku.cost_high_microusd / M * 7
    assert row.endswith(f"$0.0051 - $0.0202  ¥{low:.4f} - ¥{high:.4f}")
    assert "Cost cap per night: none" in text, "no cap, so no yuan amount for it"


def test_the_command_prints_the_night_in_both_currencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPO)
    result = cli.invoke(app, ["estimate", "-c", "configs/night_bailian.yaml"])
    text = plain(result.output)
    assert result.exit_code == 0, text
    assert text.startswith("Estimate for night bailian-trial: 6 model seats, 1 night")
    assert "Prices: configs/prices.bailian.yaml" in text
    assert "Exchange rate: 1 USD = 6.6529 CNY (as of 2026-10-30" in text
    assert "qwen3.7-plus" in text and "glm-5.1" in text and "¥" in text and "$" in text
    assert "Cost cap per night: $1.5000 = ¥9.9794" in text
    assert "Placeholders still in the configuration: night.deal_seed_commitment" in text
    assert "no price" not in text, "every model of the night has a price"


def test_the_command_finds_the_list_from_any_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    result = cli.invoke(app, ["estimate", "-c", str(TEMPLATE)])
    assert result.exit_code == 0 and "¥" in plain(result.output), plain(result.output)


def test_several_nights_scale_the_yuan_with_the_dollars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPO)
    one = plain(cli.invoke(app, ["estimate", "-c", "configs/night_bailian.yaml"]).output)
    ten = plain(
        cli.invoke(app, ["estimate", "-c", "configs/night_bailian.yaml", "--nights", "10"]).output
    )
    assert "Total for 10 nights, all model seats priced: $" in ten
    first = re.findall(
        r"¥(\d+\.\d+)", next(x for x in one.splitlines() if x.startswith("Total for"))
    )
    tenth = re.findall(
        r"¥(\d+\.\d+)", next(x for x in ten.splitlines() if x.startswith("Total for"))
    )
    for a, b in zip(first, tenth, strict=True):
        assert abs(float(b) - 10 * float(a)) < 0.001


# ------------------------------------------------------- a rate that is missing: status 2


def write_list(path: Path, *, rate_block: str = "") -> Path:
    path.write_text(
        "version: 1\n"
        f"{rate_block}"
        "prices:\n"
        '  "qwen3.7-plus": {input_per_mtok: 2, output_per_mtok: 8, currency: CNY,'
        " as_of: 2026-11-01, source: https://example.com/p}\n",
        encoding="utf-8",
    )
    return path


def test_the_estimate_refuses_a_list_in_yuan_without_a_rate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(REPO)
    bad = write_list(tmp_path / "no_rate.yaml")
    result = cli.invoke(app, ["estimate", "-c", "configs/night_bailian.yaml", "--prices", str(bad)])
    text = plain(result.output)
    assert result.exit_code == 2, text
    assert "cannot read" in text and "CNY" in text and "exchange_rates" in text
    assert "no rate is assumed" in text


def test_the_same_list_with_a_rate_is_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(REPO)
    block = (
        "exchange_rates:\n"
        "  CNY: {units_per_usd: 7, as_of: 2026-10-30, source: https://example.com/r}\n"
    )
    good = write_list(tmp_path / "with_rate.yaml", rate_block=block)
    result = cli.invoke(
        app, ["estimate", "-c", "configs/night_bailian.yaml", "--prices", str(good)]
    )
    text = plain(result.output)
    assert result.exit_code == 0, text
    assert "Exchange rate: 1 USD = 7 CNY" in text and "1 of 6 model seats priced" in text


def test_run_refuses_a_list_in_yuan_without_a_rate_before_playing(tmp_path: Path) -> None:
    bad = write_list(tmp_path / "no_rate.yaml")
    out = tmp_path / "night"
    result = cli.invoke(
        app,
        [
            "run",
            "-c",
            str(REPO / "configs" / "night_mock.yaml"),
            "-o",
            str(out),
            "--prices",
            str(bad),
        ],
    )
    text = plain(result.output)
    assert result.exit_code == 2, text
    assert "cannot read" in text and "CNY" in text
    assert not out.exists(), "nothing was played or written"


def test_play_refuses_a_list_in_yuan_without_a_rate_before_asking_anything(tmp_path: Path) -> None:
    bad = write_list(tmp_path / "no_rate.yaml")
    out = tmp_path / "night"
    mock = str(REPO / "configs" / "night_mock.yaml")
    args = ["play", "-c", mock, "-o", str(out), "--human-seat", "3", "--prices", str(bad)]
    result = cli.invoke(app, args)
    text = plain(result.output)
    assert result.exit_code == 2, text
    assert "cannot read" in text and "CNY" in text and not out.exists()


def test_show_refuses_a_list_in_yuan_without_a_rate_and_writes_nothing(tmp_path: Path) -> None:
    out = tmp_path / "night"
    mock = str(REPO / "configs" / "night_mock.yaml")
    played = cli.invoke(app, ["run", "-c", mock, "-o", str(out)])
    assert played.exit_code == 0, plain(played.output)
    before = sorted(p.name for p in out.iterdir())
    bad = write_list(tmp_path / "no_rate.yaml")
    result = cli.invoke(app, ["show", str(out), "--prices", str(bad)])
    text = plain(result.output)
    assert result.exit_code == 2, text
    assert "cannot read" in text and "CNY" in text
    assert sorted(p.name for p in out.iterdir()) == before, "no recording was written"


# ============================================================ the template configuration


def test_the_template_is_a_valid_night_with_six_models_and_two_bots() -> None:
    config = bailian_config()
    kinds = [(s.seat, s.kind) for s in config.seats]
    assert kinds == [(n, "llm") for n in range(1, 7)] + [(7, "bot"), (8, "bot")]
    assert [s.policy for s in config.seats[6:]] == ["truthful", "economist"]
    assert [s.model for s in config.seats[:6]] == SEAT_MODELS
    assert all(s.endpoint == "bailian" and s.max_tokens == 8192 for s in config.seats[:6])
    assert config.night.mode == "plain" and config.night.n_lots == 12
    assert not config.is_offline


def test_each_vendor_has_two_seats() -> None:
    config = bailian_config()
    vendors = Counter(s.vendor for s in config.seats if s.kind == "llm")
    assert vendors == {"qwen": 2, "deepseek": 2, "zhipu": 2}
    assert config.season.max_seats_per_vendor == 2


def test_the_endpoint_is_bailian_with_the_key_in_the_environment() -> None:
    endpoint = bailian_config().endpoints["bailian"]
    assert endpoint.kind == "openai_compatible"
    assert endpoint.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert endpoint.api_key_env == "DASHSCOPE_API_KEY" and endpoint.base_url_env is None
    assert endpoint.tool_choice == "auto" and endpoint.strict_tools is False
    assert endpoint.structured_output == "tools" and endpoint.sampling is None
    assert "sk-" not in TEMPLATE.read_text(encoding="utf-8")


def test_the_host_the_judges_and_the_classifier_follow_the_vendor_rules() -> None:
    config = bailian_config()
    seat_vendors = {s.vendor for s in config.seats if s.kind == "llm"}
    host = config.show.host_offline
    assert (host.vendor, host.model) == ("moonshot", "kimi-k2.5")
    assert host.vendor not in seat_vendors
    classifier = config.show.content_scan.classifier
    assert classifier is not None and classifier.vendor not in seat_vendors
    assert (classifier.vendor, classifier.model) == ("moonshot", "kimi-k2.5")
    judges = [(j.vendor, j.model) for j in config.show.judges]
    assert judges == [("moonshot", "kimi-k2.5"), ("qwen", "qwen-plus")]
    assert judges[1][0] in seat_vendors and config.show.allow_judge_vendor_overlap is True
    assert config.show.content_scan.wordlist is None


def raw_template() -> dict[str, Any]:
    return copy.deepcopy(yaml.safe_load(TEMPLATE.read_text(encoding="utf-8")))


def rejects(raw: dict[str, Any], *needles: str) -> str:
    with pytest.raises(ValidationError) as info:
        NightConfig.model_validate(raw)
    text = str(info.value)
    for needle in needles:
        assert needle in text, text
    return text


def test_the_judge_that_shares_a_vendor_with_a_seat_needs_the_switch() -> None:
    raw = raw_template()
    raw["show"]["allow_judge_vendor_overlap"] = False
    rejects(raw, "a judge shares a vendor with a seat")


def test_two_judges_from_one_vendor_are_refused() -> None:
    raw = raw_template()
    raw["show"]["judges"][1] = {**raw["show"]["judges"][0]}
    rejects(raw, "the two judges must come from different vendors")


def test_a_host_from_a_vendor_with_seats_is_refused_so_qwen_cannot_be_the_host() -> None:
    raw = raw_template()
    raw["show"]["host_offline"] = {
        "kind": "llm",
        "vendor": "qwen",
        "endpoint": "bailian",
        "model": "qwen-flash",
    }
    rejects(raw, "the offline host must not share a vendor with a seat")


def test_a_classifier_from_a_vendor_with_seats_is_refused() -> None:
    raw = raw_template()
    raw["show"]["content_scan"]["classifier"]["vendor"] = "zhipu"
    rejects(raw, "the content classifier must not share a vendor with a seat")


def test_a_third_seat_of_one_vendor_is_refused() -> None:
    raw = raw_template()
    raw["seats"][2]["vendor"] = "qwen"
    rejects(raw, "vendor 'qwen' has 3 seats, limit 2")


def test_the_template_names_a_cap_and_a_price_list_that_exists() -> None:
    config = bailian_config()
    assert config.limits.max_cost_per_night_usd == 1.5
    assert config.limits.prices_file == "configs/prices.bailian.yaml"
    assert (REPO / config.limits.prices_file).is_file()
    assert (config.limits.call_timeout_s, config.limits.transient_retries) == (300, 2)


def test_the_template_holds_no_seed_and_one_placeholder() -> None:
    config = bailian_config()
    assert config.night.seed is None and config.night.seed_env == "AUCTION_NIGHT_SEED_BAILIAN"
    assert find_placeholders(config) == ["night.deal_seed_commitment"]


# ------------------------------------------------------------------ capability records


def test_forcing_the_tool_is_declared_for_qwen3_7_plus_alone() -> None:
    config = bailian_config()
    endpoint = config.endpoints["bailian"]
    models = sorted(set(models_in_use(config)))
    forced = [m for m in models if endpoint.capabilities_for(m).supports_forced_tool_choice]
    assert forced == ["qwen3.7-plus"]
    for model in models:
        caps = endpoint.capabilities_for(model)
        assert caps.supports_strict_tools is False, model
        assert caps.supports_sampling_params is False, model
        assert caps.thinking_modes == () and caps.efforts == (), model


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda raw: raw["seats"][0].update(sampling={"temperature": 0.7}), "sampling parameters"),
        (lambda raw: raw["seats"][1].update(effort="medium"), "effort cannot be set"),
        (
            lambda raw: raw["seats"][2].update(thinking={"type": "disabled"}),
            "thinking cannot be controlled",
        ),
        (
            lambda raw: raw["endpoints"]["bailian"].update(sampling={"top_k": 20}),
            "sampling parameters",
        ),
        (lambda raw: raw["endpoints"]["bailian"].update(strict_tools=True), "strict tools"),
        (lambda raw: raw["endpoints"]["bailian"].update(tool_choice="any"), "forced tool_choice"),
        (lambda raw: raw["endpoints"]["bailian"].update(tool_choice="tool"), "forced tool_choice"),
    ],
    ids=["sampling", "effort", "thinking", "endpoint-sampling", "strict", "any", "tool"],
)
def test_the_records_keep_everything_unmeasured_out_of_a_request(change: Any, message: str) -> None:
    raw = raw_template()
    change(raw)
    rejects(raw, message)


# ===================================================== the fields of a request


MEASURED = {"model", "messages", "tools", "tool_choice", "max_tokens"}


def test_a_seat_request_has_only_the_fields_that_were_measured() -> None:
    config = bailian_config()
    for seat in config.seats:
        if seat.kind != "llm":
            continue
        endpoint = config.endpoints[seat.endpoint]  # type: ignore[index]
        built = build_request(endpoint, seat, system="rules", messages=[user_message("your move")])
        payload = built.payload
        assert set(payload) == MEASURED, seat.model
        assert payload["model"] == seat.model and payload["max_tokens"] == 8192
        assert payload["tool_choice"] == "auto"
        assert [m["role"] for m in payload["messages"]] == ["system", "user"]
        (tool,) = payload["tools"]
        assert tool["type"] == "function" and tool["function"]["name"] == "act"
        assert "strict" not in tool["function"], "strict is accepted but not enforced: off"
        assert built.kind == "openai_compatible" and built.thinking is None and built.effort is None


def test_none_of_the_fields_that_bailian_might_refuse_is_sent() -> None:
    config = bailian_config()
    risky = {
        "parallel_tool_calls",
        "max_completion_tokens",
        "reasoning_effort",
        "seed",
        "temperature",
        "top_p",
        "top_k",
        "extra_body",
        "stream",
        "response_format",
        "enable_thinking",
        "chat_template_kwargs",
    }
    for seat in (s for s in config.seats if s.kind == "llm"):
        endpoint = config.endpoints[seat.endpoint]  # type: ignore[index]
        built = build_request(endpoint, seat, system="rules", messages=[user_message("go")])
        assert not risky & set(built.payload), seat.model
        assert "enable_thinking" not in str(built.payload)


def test_every_field_is_a_parameter_of_the_openai_sdk_call() -> None:
    config = bailian_config()
    seat = config.seats[0]
    built = build_request(
        config.endpoints["bailian"], seat, system="s", messages=[user_message("u")]
    )
    signature = inspect.signature(Completions.create)
    signature.bind(None, **built.payload)  # the SDK itself accepts every one of them
    with pytest.raises(TypeError):
        signature.bind(None, **built.payload, not_a_parameter=1)


def host_request() -> dict[str, Any]:
    config = bailian_config()
    host = ModelHost(
        config.show.host_offline, config.endpoints["bailian"], cassette=Cassette.record(), night="n"
    )
    job = HostJob("lot", 1, None, "system text", "card text", 80, 200)  # type: ignore[arg-type]
    return host.build(job).payload


def test_the_show_roles_send_a_prompt_and_nothing_else() -> None:
    config = bailian_config()
    endpoint = config.endpoints["bailian"]
    cassette = Cassette.record()
    judge = ModelJudge(1, config.show.judges[1], endpoint, cassette=cassette, night="n")
    job = JudgeJob(index=1, night="n", system="s", user="u", input=None)  # type: ignore[arg-type]
    classifier_role = config.show.content_scan.classifier
    assert classifier_role is not None
    classifier = ModelClassifier(classifier_role, endpoint, cassette=cassette, night="n")
    payloads = {
        "host": host_request(),
        "judge": judge.build(job).payload,
        "classifier": classifier.build([Probe(1, "talk", "hello")]).payload,
    }
    for name, payload in payloads.items():
        assert set(payload) == {"model", "messages"}, f"{name}: no tools, no sampling, no limit"
    assert payloads["judge"]["model"] == "qwen-plus"
    assert payloads["host"]["model"] == payloads["classifier"]["model"] == "kimi-k2.5"


# ===================================================== replies as Bailian writes them


def bailian_completion(
    arguments: dict[str, Any] | None,
    *,
    model: str,
    reasoning: str | None = "先看预算，再看对手的发言。很多字很多字很多字。",
    content: str | None = "",
    finish: str | None = None,
    prompt: int = 3000,
    completion: int = 1500,
    reasoning_tokens: int | None = 1300,
) -> Any:
    """A chat completion shaped as DashScope returns it for a thinking model: the reasoning in
    ``message.reasoning_content``, no text, one ``act`` call, and a usage block in which
    ``completion_tokens`` includes the reasoning tokens."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    if arguments is not None:
        message["tool_calls"] = [
            {
                "id": "call_bailian",
                "type": "function",
                "function": {"name": "act", "arguments": json.dumps(arguments, ensure_ascii=False)},
            }
        ]
    usage: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }
    if reasoning_tokens is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning_tokens}
    return openai.types.chat.ChatCompletion.model_validate(
        {
            "id": "chatcmpl-bailian",
            "object": "chat.completion",
            "created": 1,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish or ("tool_calls" if arguments is not None else "stop"),
                    "message": message,
                }
            ],
            "usage": usage,
        }
    )


def sealed_with_text_number(bid: str = "80") -> dict[str, Any]:
    """The arguments of a sealed bid in which the whole number is written as text."""
    return valid_args("sealed", bid=bid)


def test_the_reply_is_read_from_the_tool_call_and_the_reasoning_is_kept_apart() -> None:
    completion = bailian_completion(sealed_with_text_number(), model="qwen3.7-plus")
    reply = normalise_openai(completion, latency_ms=0)
    assert reply.reply.text == "", "reasoning_content is not the text of the answer"
    (call,) = reply.reply.tool_calls
    assert call.name == "act" and '"bid": "80"' in str(call.arguments)
    assert reply.model == "qwen3.7-plus" and reply.stop_reason == "tool_calls" and not reply.refusal
    stored = reply.response["choices"][0]["message"]  # type: ignore[index]
    assert stored["reasoning_content"].startswith("先看预算"), "the recording keeps it"
    assert reply.reply.text.find("先看预算") == -1


def test_completion_tokens_include_the_reasoning_and_are_the_billed_output() -> None:
    reply = normalise_openai(
        bailian_completion(sealed_with_text_number(), model="qwen3.7-plus"), latency_ms=0
    )
    assert reply.usage == Usage(input_tokens=3000, output_tokens=1500, reasoning_tokens=1300), (
        "output is completion_tokens as it stands; the reasoning is a part of it"
    )
    assert reply.usage is not None and reply.usage.prompt_tokens == 3000
    # the reasoning is not added a second time when the call is priced
    cost = Ledger(bailian_prices()).cost_of(reply.usage, reply.model, "qwen3.7-plus")
    assert cost == ceil(exact_microusd(2, 3000) + exact_microusd(8, 1500))
    assert cost != ceil(exact_microusd(2, 3000) + exact_microusd(8, 1500 + 1300))


def test_a_reply_without_the_reasoning_detail_still_prices_the_whole_output() -> None:
    reply = normalise_openai(
        bailian_completion(sealed_with_text_number(), model="qwen-flash", reasoning_tokens=None),
        latency_ms=0,
    )
    assert reply.usage == Usage(input_tokens=3000, output_tokens=1500, reasoning_tokens=None)
    cost = Ledger(bailian_prices()).cost_of(reply.usage, reply.model)
    assert cost == ceil(exact_microusd("0.15", 3000) + exact_microusd("1.5", 1500))


def test_cached_prompt_tokens_are_taken_out_of_the_plain_input() -> None:
    usage = openai_usage(
        {
            "prompt_tokens": 3000,
            "completion_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 2000},
            "completion_tokens_details": {"reasoning_tokens": 40},
        }
    )
    assert usage == Usage(
        input_tokens=1000, output_tokens=100, cache_read_input_tokens=2000, reasoning_tokens=40
    )
    cost = Ledger(bailian_prices()).cost_of(usage, "qwen3.7-plus")
    # the list gives no cache price, so cached tokens are billed at the input price (overstated)
    assert cost == ceil(exact_microusd(2, 3000) + exact_microusd(8, 100))


def test_a_whole_number_written_as_text_is_read_as_the_number() -> None:
    reply = normalise_openai(
        bailian_completion(sealed_with_text_number("80"), model="qwen3.7-plus"), latency_ms=0
    )
    result = parse_reply(reply.reply, parse_ctx("sealed"))
    assert result.ok and result.value == 80 and result.args is not None
    assert result.args["bid"] == 80 and isinstance(result.args["bid"], int)
    assert [(c.field, c.kind) for c in result.trace.coercions] == [("bid", "text_number")]


def test_a_number_that_comes_back_as_a_number_needs_no_repair() -> None:
    reply = normalise_openai(
        bailian_completion(valid_args("sealed", bid=80), model="deepseek-v4.1-flash"), latency_ms=0
    )
    result = parse_reply(reply.reply, parse_ctx("sealed"))
    assert result.ok and result.value == 80 and result.trace.coercions == ()


def test_reasoning_that_looks_like_an_answer_is_never_taken_for_one() -> None:
    bait = '{"phase": "sealed", "bid": 999, "thought": "from the reasoning"}'
    with_call = normalise_openai(
        bailian_completion(sealed_with_text_number("80"), model="glm-5", reasoning=bait),
        latency_ms=0,
    )
    assert parse_reply(with_call.reply, parse_ctx("sealed")).value == 80
    alone = normalise_openai(
        bailian_completion(None, model="glm-5", reasoning=bait, content=""), latency_ms=0
    )
    result = parse_reply(alone.reply, parse_ctx("sealed"), max_attempts=1)
    assert result.status == "abstain" and result.reason == "missing"


def test_a_reply_cut_off_while_thinking_is_a_missing_action() -> None:
    cut = normalise_openai(
        bailian_completion(
            None,
            model="kimi-k2.5",
            content=None,
            finish="length",
            completion=8192,
            reasoning_tokens=8192,
        ),
        latency_ms=0,
    )
    assert cut.stop_reason == "length" and cut.reply.tool_calls == () and cut.reply.text == ""
    assert cut.usage is not None and cut.usage.output_tokens == 8192
    result = parse_reply(cut.reply, parse_ctx("sealed"), max_attempts=1)
    assert result.status == "abstain" and result.reason == "missing"
    # the tokens were spent all the same, and they are priced
    assert Ledger(bailian_prices()).cost_of(cut.usage, "kimi-k2.5") == ceil(
        exact_microusd(4, 3000) + exact_microusd(21, 8192)
    )


# ------------------------------------------------- a night played on such replies


def reply_step() -> Step:
    """Every call answers like a Bailian thinking model: reasoning, a tool call, text numbers."""

    def make(kind: str, call: CallContext) -> Any:
        args = valid_args(call.phase)
        for name in ("bid", "guess_budget", "exit_price", "grab_price"):
            if args.get(name) is not None:
                args[name] = str(args[name])
        return bailian_completion(args, model=call.model)

    return Step(make)


def play_bailian_night(
    path: Path | None = None, lots: int = 1
) -> tuple[FakeNight, Any, ScriptedSdk, Cassette]:
    async def no_sleep(seconds: float) -> None:
        return None

    config = bailian_config()
    sdk = ScriptedSdk({seat: [reply_step()] * 20 for seat in (1, 2, 3, 4)})
    runner = ModelRunner(clients_for(config, sdk), clock=lambda: FIXED_TIME, sleep=no_sleep)
    cassette = Cassette.record(path)
    session = NightSession.open(config, cassette, prices=bailian_prices(), settings=FAST)
    driver = FakeNight(config, lots=lots)
    run = asyncio.run(runner.run_night(driver, session))
    runner.close()
    return driver, run, sdk, cassette


def test_a_night_on_bailian_replies_is_read_and_billed_in_dollars() -> None:
    driver, run, sdk, cassette = play_bailian_night()
    assert run.status == "completed" and sdk.call_count == run.calls == 12  # 3 phases, 4 seats
    for result in driver.results:
        assert all(o.ok for o in result.outcomes), [o.reason for o in result.outcomes]
    sealed = list(driver.results[-1].outcomes)
    assert [o.result.value for o in sealed if o.result] == [60, 60, 60, 60]
    assert all(isinstance(o.args["bid"], int) for o in sealed if o.args)
    prices = {1: (2, 8), 2: ("0.15", "1.5"), 3: (2, 8), 4: (3, 9)}  # yuan per million tokens
    by_role = run.summary["by_role"]
    for seat, (inp, out) in prices.items():
        per_call = ceil(exact_microusd(inp, 3000) + exact_microusd(out, 1500))
        tally = by_role[f"seat:{seat}"]
        assert tally["calls"] == 3 and tally["cost_microusd"] == 3 * per_call, seat
        assert tally["input_tokens"] == 3 * 3000 and tally["output_tokens"] == 3 * 1500
        assert tally["reasoning_tokens"] == 3 * 1300, "counted, and not billed a second time"
    total = run.summary["total"]
    assert (
        total["cost_microusd"]
        == run.spent_microusd
        == sum(by_role[f"seat:{s}"]["cost_microusd"] for s in prices)
    )
    assert total["unpriced_calls"] == 0 and total["unmetered_calls"] == 0
    assert len(cassette.entries) == 12
    for entry in cassette.entries:
        assert entry.cost_microusd and entry.usage is not None
        assert entry.usage["reasoning_tokens"] == 1300 and entry.usage["output_tokens"] == 1500


def test_the_requests_of_a_night_pass_the_signature_check_of_the_sdk() -> None:
    _, _, sdk, _ = play_bailian_night()
    assert sdk.requests and all(set(r) - {"timeout"} == MEASURED for r in sdk.requests)
    assert {r["model"] for r in sdk.requests} == set(SEAT_MODELS[:4])  # seats 1 to 4 play


def test_a_night_on_bailian_replies_replays_without_a_price_list_or_a_network(
    tmp_path: Path,
) -> None:
    path = tmp_path / "calls.jsonl"
    driver, run, _, cassette = play_bailian_night(path)
    recorded = Cassette.replay(path)
    config = bailian_config()

    async def no_sleep(seconds: float) -> None:
        return None

    runner = ModelRunner({}, clock=lambda: FIXED_TIME, sleep=no_sleep)
    session = NightSession.open(config, recorded, settings=FAST)  # no prices, no client
    again = FakeNight(config, lots=1)
    replayed = asyncio.run(runner.run_night(again, session))
    runner.close()
    assert again.events_text() == driver.events_text()
    assert replayed.summary == run.summary and replayed.spent_microusd == run.spent_microusd
    assert recorded.unused() == []
    assert path.read_text(encoding="utf-8").count("reasoning_content") == len(cassette.entries)


# ---------------------------------------------------- a whole night, through the real clients

KEY = "sk-" + "test" * 8  # key-shaped, made up; it must never reach a file
REASONING = "想一想预算，再想一想对手。"
REPLY_FIELDS: dict[str, Any] = {
    "say": "大家好",
    "pledge_kind": None,
    "pledge_lot": None,
    "pledge_to": None,
    "pledge_amount": None,
    "guess_budget": "150",
    "exit_price": "50",
    "grab_price": "100",
    "bid": "50",
    "plan_priorities": [1] * 12,
    "plan_budget": [20] * 12,
    "plan_persona": "稳",
    "thought": "想好了。",
}  # every field of the act tool, numbers as text, valid in every phase of the night


class BailianSdk:
    """The OpenAI SDK as a night sees it, with Bailian's habits and no network behind it.

    Every call answers with one ``act`` call that holds a value for every field, numbers written
    as text, the reasoning in ``message.reasoning_content`` and the reasoning counted in
    ``completion_tokens``. The options the SDK was made with and every request are kept.
    """

    made: list["BailianSdk"] = []

    def __init__(self, **options: Any) -> None:
        self.options = options
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        BailianSdk.made.append(self)

    def _create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        return bailian_completion(
            REPLY_FIELDS,
            model=kwargs["model"],
            reasoning=REASONING,
            prompt=1500,
            completion=600,
            reasoning_tokens=500,
        )


def rehearsal_config(cap_usd: float | None = 0.75) -> NightConfig:
    """The template, made runnable: an inline seed (a test config may have one) and a cap."""
    raw = raw_template()
    raw["night"].pop("seed_env")
    raw["night"].pop("deal_seed_commitment")
    raw["night"]["seed"] = "bailian-rehearsal-0001"
    raw["limits"]["max_cost_per_night_usd"] = cap_usd
    return NightConfig.model_validate(raw)


def rehearse(out: Path, cap_usd: float | None = 0.75) -> Any:
    BailianSdk.made = []
    return run_directory(
        rehearsal_config(cap_usd),
        out,
        prices=bailian_prices(),
        environ={"DASHSCOPE_API_KEY": KEY},
        factories={"openai_compatible": BailianSdk},
    )


@pytest.fixture(scope="module")
def rehearsed(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Any, list[BailianSdk]]:
    out = tmp_path_factory.mktemp("bailian") / "night"
    result = rehearse(out)
    return out, result, list(BailianSdk.made)


def actions_of_models(result: Any) -> list[Any]:
    return [e for e in result.events if e.type == "action" and e.data.seat <= 6]


def test_a_whole_night_is_played_through_the_real_clients(rehearsed: Any) -> None:
    out, result, sdks = rehearsed
    (end,) = [e for e in result.events if e.type == "night_end"]
    assert end.data.status == "completed"
    actions = actions_of_models(result)
    assert {a.data.status for a in actions} <= {"ok", "clamped"}, "no model abstained"
    assert {a.data.seat for a in actions} == {1, 2, 3, 4, 5, 6}
    assert len(sdks) == 1, "one endpoint, one SDK client, built once"
    options = sdks[0].options
    assert options["api_key"] == KEY and options["max_retries"] == 0
    assert options["base_url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1"


def test_every_request_of_the_night_has_the_measured_fields_and_the_timeout(rehearsed: Any) -> None:
    _, _, sdks = rehearsed
    requests = sdks[0].requests
    assert len(requests) > 100
    assert all(set(r) == MEASURED | {"timeout"} for r in requests)
    assert {r["timeout"] for r in requests} == {300.0}
    assert {r["tool_choice"] for r in requests} == {"auto"}
    assert {r["max_tokens"] for r in requests} == {8192}
    assert {r["model"] for r in requests} == set(SEAT_MODELS)


def test_the_dollars_of_the_ledger_are_the_yuan_prices_over_the_rate(rehearsed: Any) -> None:
    out, result, _ = rehearsed
    cassette = Cassette.replay(out / "calls.jsonl")
    yuan = {  # input and output, yuan per million tokens
        "qwen3.7-plus": (2, 8),
        "qwen-flash": ("0.15", "1.5"),
        "deepseek-v4.1-flash": (2, 8),
        "deepseek-v4-flash-0731": (3, 9),
        "glm-5": (4, 18),
        "glm-5.1": (6, 24),
    }
    expected = 0
    for entry in cassette.entries:
        assert entry.usage is not None and entry.usage["reasoning_tokens"] == 500
        inp, outp = yuan[entry.model]
        call = ceil(
            exact_microusd(inp, entry.usage["input_tokens"])
            + exact_microusd(outp, entry.usage["output_tokens"])
        )
        assert entry.cost_microusd == call, entry.key
        expected += call
    final = [e for e in result.events if e.type == "cost_event"][-1].data
    assert (final.kind, final.calls, final.cap_microusd) == ("tally", len(cassette), 750_000)
    assert final.spent_microusd == expected and 0 < expected < 750_000


def test_neither_the_key_nor_the_reasoning_reaches_the_public_log(rehearsed: Any) -> None:
    out, _, _ = rehearsed
    events = (out / "events.jsonl").read_text(encoding="utf-8")
    calls = (out / "calls.jsonl").read_text(encoding="utf-8")
    for text in (events, calls):
        assert KEY not in text
    assert REASONING not in events and "reasoning_content" not in events
    assert REASONING in calls, "the recording keeps the whole reply"


def test_such_a_night_replays_byte_for_byte_with_no_key_and_no_price_list(rehearsed: Any) -> None:
    out, _, _ = rehearsed
    report = replay_directory(rehearsal_config(), out)
    assert report.matches and report.unused_attempts == 0


def test_a_small_cap_in_dollars_stops_a_night_priced_in_yuan(tmp_path: Path) -> None:
    result = rehearse(tmp_path / "night", cap_usd=0.05)  # about 0.33 yuan
    fired = [e for e in result.events if e.type == "cost_event" and e.data.kind == "cap_reached"]
    assert len(fired) == 1 and fired[0].data.cap_microusd == 50_000
    assert fired[0].data.spent_microusd >= 50_000
    (end,) = [e for e in result.events if e.type == "night_end"]
    assert end.data.status == "budget_aborted"
    skipped = [
        a
        for a in actions_of_models(result)
        if (a.data.status, a.data.reason) == ("abstain", "budget_aborted")
    ]
    assert skipped, "later actions were skipped, not priced"
    assert result.run.status == "budget_aborted" and result.run.spent_microusd < 750_000
    assert result.run.calls < 100, "the night was cut short, not played to the end"
