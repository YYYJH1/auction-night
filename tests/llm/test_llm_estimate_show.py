"""The estimate of the show layer: the host, the judges and the content classifier.

The seats of the example API configuration are priced as before (tests/llm/test_llm_accounting.py).
Here the roles that run after the night are priced the way ``auction-night show`` calls them: the
host once for each lot and once for the recap, each judge once (twice in the high estimate, for
the second try), and the classifier once for each batch of up to forty texts. The sizes are those
of the prompts of the mock night (tests/show/test_show_estimate_sizes.py keeps them honest).
"""

import copy
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
import yaml
from sdk_fakes import REPO, api_config, example_prices
from typer.testing import CliRunner

from auction_night.cli import app
from auction_night.contracts import NightConfig, load_config
from auction_night.contracts.config import ModelPrice, RoleConfig
from auction_night.llm.accounting import (
    EstimateAssumptions,
    PriceTable,
    calls_breakdown,
    estimate_night,
    format_estimate,
    scan_texts_expected,
    show_roles,
)

cli = CliRunner()
M = 1_000_000


def priced_show(c1: tuple[float, float] = (1.0, 5.0), d1: tuple[float, float] = (2.0, 10.0)) -> Any:
    """The test prices of the example line-up, with the show models given a price each."""
    prices = dict(example_prices().prices)

    def price(inp: float, out: float) -> ModelPrice:
        return ModelPrice(
            input_per_mtok=inp,
            output_per_mtok=out,
            as_of="2026-11-01",  # type: ignore[arg-type]
            source="https://example.com/prices",
        )

    prices["<model-c1>"] = price(*c1)
    prices["<model-d1>"] = price(*d1)
    return PriceTable(prices, source="test prices")


def role(estimate: Any, label: str) -> Any:
    return next(r for r in estimate.show if r.role == label)


def capped(config: NightConfig, microusd: int) -> NightConfig:
    limits = config.limits.model_copy(update={"max_cost_per_night_usd": microusd / M})
    return config.model_copy(update={"limits": limits})


def mock_seats_with_model_show() -> NightConfig:
    """The mock night (no seat is a model) with a host, two judges and a classifier that are."""
    raw = yaml.safe_load((REPO / "configs" / "night_mock.yaml").read_text(encoding="utf-8"))
    raw["endpoints"] = {"show": {"kind": "openai_compatible", "base_url": "http://localhost:9/v1"}}
    llm = {"kind": "llm", "endpoint": "show", "model": "show-model"}
    raw["show"]["host_offline"] = {**llm, "vendor": "vendor-h"}
    raw["show"]["judges"] = [{**llm, "vendor": f"vendor-j{i}"} for i in (0, 1)]
    raw["show"]["content_scan"] = {"wordlist": None, "classifier": {**llm, "vendor": "vendor-s"}}
    return NightConfig.model_validate(raw)


# ================================================================================ the calls


def test_the_roles_that_are_models_are_listed_in_the_order_they_are_called() -> None:
    estimate = estimate_night(api_config(), example_prices())
    assert [r.role for r in estimate.show] == ["host", "judge:0", "judge:1", "classifier"]
    assert [r.model for r in estimate.show] == [
        "<model-c1>",
        "<model-c1>",
        "<model-d1>",
        "<model-d1>",
    ]
    assert estimate.show_mocks == 0 and estimate.scan_texts == 497


def test_the_host_is_asked_once_for_each_lot_and_once_for_the_recap() -> None:
    host = role(estimate_night(api_config(), example_prices()), "host")
    assert host.calls_low == host.calls_high == 12 + 1


def test_each_judge_is_asked_once_and_in_the_high_estimate_twice() -> None:
    estimate = estimate_night(api_config(), example_prices())
    for label in ("judge:0", "judge:1"):
        judge = role(estimate, label)
        assert (judge.calls_low, judge.calls_high) == (1, 2)


def test_the_classifier_is_asked_once_for_each_batch_of_forty_texts() -> None:
    estimate = estimate_night(api_config(), example_prices())
    assert role(estimate, "classifier").calls_low == -(-497 // 40) == 13
    few = EstimateAssumptions(scan_texts_per_night=400)
    assert (
        role(
            estimate_night(api_config(), example_prices(), assumptions=few), "classifier"
        ).calls_low
        == 10
    )
    big = EstimateAssumptions(scan_texts_per_night=401)
    assert (
        role(
            estimate_night(api_config(), example_prices(), assumptions=big), "classifier"
        ).calls_low
        == 11
    )
    wide = EstimateAssumptions(scan_batch_items=50)
    assert (
        role(
            estimate_night(api_config(), example_prices(), assumptions=wide), "classifier"
        ).calls_low
        == 10
    )


def test_the_texts_follow_the_calls_of_the_model_seats() -> None:
    config, a = api_config(), EstimateAssumptions()
    assert scan_texts_expected(config, calls_breakdown(config), a) == 497
    four = config.model_copy(update={"seats": config.seats[:4]})  # four model seats, no bots
    seat_calls = 4 * sum(calls_breakdown(four).values(), Fraction(0))
    expected = Decimal(497) * Decimal(seat_calls.numerator) / Decimal(seat_calls.denominator)
    expected = expected / (6 * Decimal("27.25"))
    assert scan_texts_expected(four, calls_breakdown(four), a) == round(expected)
    tiny = EstimateAssumptions(scan_texts_per_night=1)
    one_batch = estimate_night(config, example_prices(), assumptions=tiny)
    assert role(one_batch, "classifier").calls_low == 1, "there is always one batch to read"


# =============================================================================== the tokens


def test_the_host_costs_what_its_prompts_and_its_answers_come_to() -> None:
    estimate = estimate_night(api_config(), priced_show())
    host = role(estimate, "host")
    prompt = 12 * 1042 + 2595
    answer = 12 * 40 + 250
    assert host.low.input == prompt and host.low.output == answer
    assert host.low.cache_read == host.low.cache_write == 0, "no cache discount is assumed"
    assert host.high.input == prompt and host.high.output == answer + 13 * 1000
    assert host.cost_low_microusd == prompt * 1 + answer * 5
    assert host.cost_high_microusd == prompt * 1 + (answer + 13_000) * 5


def test_the_second_try_of_a_judge_repeats_the_prompt_and_the_answer_of_the_first() -> None:
    judge = role(estimate_night(api_config(), priced_show()), "judge:1")
    assert (judge.low.input, judge.low.output) == (5628, 3321)
    assert judge.high.input == 2 * 5628 + 3100  # the prompt twice, and the first answer repeated
    assert judge.high.output == 2 * 3321 + 2 * 1000
    assert judge.cost_low_microusd == 5628 * 2 + 3321 * 10
    assert judge.cost_high_microusd == (2 * 5628 + 3100) * 2 + (2 * 3321 + 2000) * 10


def test_the_classifier_reads_a_batch_with_its_system_prompt_each_time() -> None:
    scan = role(estimate_night(api_config(), priced_show()), "classifier")
    assert scan.low.input == 13 * 1348 and scan.low.output == 13 * 20
    assert scan.high.output == 13 * 20 + 13 * 1000
    assert scan.cost_low_microusd == 13 * 1348 * 2 + 13 * 20 * 10


def test_an_anthropic_role_counts_more_tokens_in_the_high_estimate_only() -> None:
    config = api_config()
    show = config.show.model_copy(
        update={
            "host_offline": RoleConfig(
                kind="llm", endpoint="anthropic", model="claude-haiku-5-5", vendor="vendor-c"
            )
        }
    )
    estimate = estimate_night(config.model_copy(update={"show": show}), example_prices())
    host = role(estimate, "host")
    prompt, answer = 12 * 1042 + 2595, 12 * 40 + 250
    assert host.low.input == prompt and host.low.output == answer
    assert host.high.input == prompt * Decimal("1.3")
    assert host.high.output == answer * Decimal("1.3") + 13 * 1000, "thinking is not scaled"
    other = role(estimate, "judge:0")  # an OpenAI-compatible endpoint: the factor is one
    assert other.high.input == 2 * 5628 + 3100


def test_the_thinking_allowance_and_the_factor_of_the_command_line_reach_the_show_layer() -> None:
    flat = EstimateAssumptions(thinking_tokens_high=0, anthropic_token_factor_high=Decimal(1))
    estimate = estimate_night(api_config(), priced_show(), assumptions=flat)
    host = role(estimate, "host")
    assert host.low.output == host.high.output and host.cost_low_microusd == host.cost_high_microusd
    judge = role(estimate, "judge:0")
    assert judge.cost_high_microusd > judge.cost_low_microusd, "the second try still costs"


# ============================================================================ prices and totals


def test_a_model_without_a_price_is_listed_and_no_price_is_made_up() -> None:
    table = PriceTable.from_file(REPO / "configs" / "prices.example.yaml")
    estimate = estimate_night(api_config(), table)
    assert [r.priced for r in estimate.show] == [False] * 4
    assert estimate.unpriced_show_models == ["<model-c1>", "<model-d1>"]
    assert estimate.show_total_microusd() == (0, 0)
    text = format_estimate(estimate)
    show_rows = [line for line in text.splitlines() if line.startswith(("host", "judge", "class"))]
    assert len(show_rows) == 4 and all(row.endswith("no price") for row in show_rows)
    assert "No price for the show layer: <model-c1>, <model-d1>. Add them" in text
    assert "No model role of the show layer has a price, so it has no cost estimate." in text
    assert "Seats and show layer together" not in text


def test_without_any_price_the_show_layer_costs_nothing_in_the_report() -> None:
    text = format_estimate(estimate_night(api_config(), PriceTable()))
    assert "$" not in text and text.count("no price") >= 10  # six seats, four roles, and notes


def test_the_show_layer_has_its_own_total_and_the_night_has_one() -> None:
    estimate = estimate_night(api_config(), priced_show())
    seats_low, seats_high = estimate.total_microusd()
    show_low, show_high = estimate.show_total_microusd()
    parts = [role(estimate, label) for label in ("host", "judge:0", "judge:1", "classifier")]
    assert show_low == sum(r.cost_low_microusd for r in parts)
    assert show_high == sum(r.cost_high_microusd for r in parts)
    assert estimate.night_total_microusd() == (seats_low + show_low, seats_high + show_high)
    text = format_estimate(estimate)
    assert "Show layer for 1 night, all model roles priced: $" in text
    low, high = estimate.night_total_microusd()
    assert f"Seats and show layer together, 1 night: ${low / M:.4f} - ${high / M:.4f}" in text
    assert "Total for 1 night, all model seats priced: $" in text, "the seats' own line is kept"


def test_several_nights_scale_the_show_totals() -> None:
    one = estimate_night(api_config(), priced_show())
    twelve = estimate_night(api_config(), priced_show(), nights=12)
    assert twelve.show_total_microusd()[1] == one.show_total_microusd()[1] * 12
    assert [r.cost_low_microusd for r in twelve.show] == [r.cost_low_microusd for r in one.show]
    assert "Show layer for 12 nights, all model roles priced" in format_estimate(twelve)


def test_the_cap_is_compared_with_the_seats_and_the_show_layer_together() -> None:
    estimate = estimate_night(api_config(), priced_show())
    seats_high, show_high = estimate.total_microusd()[1], estimate.show_total_microusd()[1]
    assert show_high > 1000
    between = int(seats_high + show_high / 2)  # the seats fit, the seats and the show layer do not
    above = format_estimate(estimate_night(capped(api_config(), between), priced_show()))
    cap = f"Cost cap per night: ${between / M:.4f}"
    assert f"{cap} (the high estimate for one night is above the cap)" in above
    assert "holds for the seats (run) and the show layer (show) on their own" in above
    roomy = int(seats_high + show_high * 2)
    within = format_estimate(estimate_night(capped(api_config(), roomy), priced_show()))
    assert "(the high estimate for one night is within the cap)" in within
    no_cap = format_estimate(estimate)
    assert "Cost cap per night: none" in no_cap and "on their own" not in no_cap


def test_the_report_names_the_calls_and_what_is_not_counted() -> None:
    text = format_estimate(estimate_night(api_config(), priced_show()))
    assert "Show layer (host, judges and content scan, the roles that are models):" in text
    assert "Calls per night: host 13 (one for each of the 12 lots, one for the recap); " in text
    assert "each judge 1, or 2 if its first answer cannot be read (high estimate); " in text
    assert "content scan 13 (batches of up to 40 texts, about 497 texts)." in text
    assert "A rewrite of a host text, an unreadable reply of the classifier and retries" in text
    assert "counted at one token per two characters" in text
    assert "Show-layer calls (host, judges, content scan) are not included." not in text


# ========================================================================== roles that are mocks


def test_a_mock_role_has_no_row_and_is_counted_as_a_mock() -> None:
    config = api_config()
    show = config.show.model_copy(update={"host_offline": RoleConfig(kind="mock")})
    estimate = estimate_night(config.model_copy(update={"show": show}), example_prices())
    assert [r.role for r in estimate.show] == ["judge:0", "judge:1", "classifier"]
    assert estimate.show_mocks == 1
    assert "1 show role is a mock and makes no model calls." in format_estimate(estimate)


def test_a_night_whose_show_layer_is_all_mocks_says_so_without_a_section() -> None:
    config = api_config()
    mocks = {"host_offline": RoleConfig(kind="mock"), "judges": [RoleConfig(kind="mock")] * 2}
    show = config.show.model_copy(update=mocks)
    content = show.content_scan.model_copy(update={"classifier": None})
    show = show.model_copy(update={"content_scan": content})
    estimate = estimate_night(config.model_copy(update={"show": show}), example_prices())
    assert estimate.show == () and estimate.show_mocks == 3
    text = format_estimate(estimate)
    assert "Show layer (host" not in text
    assert "The show layer (host, judges, content scan) is played by mocks, with no model." in text


def test_the_mock_night_has_no_show_layer_to_estimate() -> None:
    config = load_config(REPO / "configs" / "night_mock.yaml")
    assert [label for label, _ in show_roles(config)] == ["host", "judge:0", "judge:1"]
    estimate = estimate_night(config, PriceTable())
    assert estimate.show == () and estimate.show_mocks == 3
    assert format_estimate(estimate).endswith("so it makes no model calls.")


def test_a_night_without_model_seats_still_gets_the_show_layer() -> None:
    config = mock_seats_with_model_show()
    text = format_estimate(estimate_night(config, PriceTable()))
    assert text.startswith("Estimate for night mock-0001: 0 model seats, 1 night")
    assert "No seat of this night is played by a model." in text
    assert "so it makes no model calls" not in text and "cache read" not in text
    assert "Show layer (host, judges and content scan, the roles that are models):" in text
    rows = [line for line in text.splitlines() if line.startswith(("host", "judge", "class"))]
    assert len(rows) == 4 and all("show-model" in row for row in rows)
    assert "Per call: prefix" not in text


# ================================================================================ the command


def run_estimate(*args: str) -> Any:
    return cli.invoke(app, ["estimate", *args])


def test_the_command_prints_the_show_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPO)
    result = run_estimate("--config", "configs/night_api.example.yaml")
    assert result.exit_code == 0, result.output
    text = result.output
    assert "Show layer (host, judges and content scan, the roles that are models):" in text
    assert "judge:1" in text and "classifier" in text and "1 - 2" in text
    assert "No price for the show layer: <model-c1>, <model-d1>." in text
    assert "$0.0051 - $0.0202" in text, "the seats are priced as before"


def test_the_options_of_the_command_reach_the_show_layer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(REPO)
    prices = tmp_path / "prices.yaml"
    entry = "{input_per_mtok: 1, output_per_mtok: 5, as_of: 2026-11-01, source: https://e.org/p}"
    prices.write_text(
        f'version: 1\nprices:\n  "<model-c1>": {entry}\n  "<model-d1>": {entry}\n', encoding="utf-8"
    )
    base = ("-c", "configs/night_api.example.yaml", "--prices", str(prices))
    plain = run_estimate(*base, "--thinking-high", "0", "--token-factor-high", "1")
    assert plain.exit_code == 0, plain.output
    host = next(line for line in plain.output.splitlines() if line.startswith("host"))
    cost = (12 * 1042 + 2595) + (12 * 40 + 250) * 5
    assert host.endswith(f"${cost / M:.4f}"), host  # low and high agree when nothing is added
    twelve = run_estimate(*base, "--nights", "12")
    assert "Show layer for 12 nights, all model roles priced:" in twelve.output


def test_the_example_configuration_has_a_model_for_every_show_role() -> None:
    """The shape the estimate assumes: a host, two judges, a classifier, all models."""
    config = copy.deepcopy(api_config())
    labels = [label for label, role_ in show_roles(config) if role_.kind == "llm"]
    assert labels == ["host", "judge:0", "judge:1", "classifier"]
