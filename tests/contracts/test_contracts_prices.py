"""The price list in dollars and in yuan, and the exchange rate that turns one into the other."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from auction_night.contracts import PricesFile, load_prices, schema_texts
from auction_night.contracts.config import ExchangeRate, ModelPrice

REPO = Path(__file__).resolve().parents[2]
BAILIAN = REPO / "configs" / "prices.bailian.yaml"

ENTRY: dict[str, Any] = {
    "input_per_mtok": 2,
    "output_per_mtok": 8,
    "as_of": "2026-11-01",
    "source": "https://example.com/prices",
}
RATE: dict[str, Any] = {
    "units_per_usd": 6.6529,
    "as_of": "2026-10-30",
    "source": "https://example.com/rate",
}


def yuan(**changes: Any) -> dict[str, Any]:
    """A price entry in yuan."""
    return {**ENTRY, "currency": "CNY", **changes}


# ------------------------------------------------------------------------ what is accepted


def test_a_price_in_yuan_is_accepted_when_the_list_carries_the_rate() -> None:
    prices = PricesFile.model_validate({"prices": {"m": yuan()}, "exchange_rates": {"CNY": RATE}})
    assert prices.prices["m"].currency == "CNY"
    rate = prices.exchange_rates["CNY"]
    assert isinstance(rate, ExchangeRate)
    assert rate.units_per_usd == 6.6529 and str(rate.as_of) == "2026-10-30"
    assert rate.source == "https://example.com/rate" and rate.note is None


def test_the_currency_of_a_price_is_dollars_unless_stated() -> None:
    assert ModelPrice.model_validate(ENTRY).currency == "USD"
    assert ModelPrice.model_validate(yuan()).currency == "CNY"
    with pytest.raises(ValidationError):
        ModelPrice.model_validate({**ENTRY, "currency": "EUR"})


def test_lists_in_dollars_read_as_before_and_need_no_rate() -> None:
    plain = PricesFile.model_validate({"prices": {"m": ENTRY}})
    assert plain.exchange_rates == {} and plain.prices["m"].currency == "USD"
    old = load_prices(REPO / "configs" / "prices.example.yaml")
    assert old.exchange_rates == {} and {p.currency for p in old.prices.values()} == {"USD"}


def test_a_rate_may_be_listed_for_a_list_that_prices_only_in_dollars() -> None:
    prices = PricesFile.model_validate({"prices": {"m": ENTRY}, "exchange_rates": {"CNY": RATE}})
    assert set(prices.exchange_rates) == {"CNY"}, "the rate is for showing amounts in yuan"


def test_a_rate_may_carry_a_note() -> None:
    rate = ExchangeRate.model_validate({**RATE, "note": "reference rate"})
    assert rate.note == "reference rate"


# ------------------------------------------------------------------------ what is refused


def test_a_price_in_yuan_without_a_rate_is_refused() -> None:
    with pytest.raises(ValidationError) as info:
        PricesFile.model_validate({"prices": {"m": yuan()}})
    text = str(info.value)
    assert "CNY" in text and "exchange_rates" in text and "no rate is assumed" in text


def test_only_the_currencies_of_the_contract_have_a_rate() -> None:
    with pytest.raises(ValidationError, match="CNY"):
        PricesFile.model_validate({"prices": {"m": ENTRY}, "exchange_rates": {"EUR": RATE}})


def test_the_dollar_takes_no_rate() -> None:
    with pytest.raises(ValidationError, match="USD"):
        PricesFile.model_validate({"prices": {"m": ENTRY}, "exchange_rates": {"USD": RATE}})


@pytest.mark.parametrize(
    "change",
    [
        {"units_per_usd": 0},
        {"units_per_usd": -6.6529},
        {"units_per_usd": float("nan")},
        {"units_per_usd": float("inf")},
        {"units_per_usd": "six"},
        {"as_of": None},
        {"as_of": "yesterday"},
        {"source": "not a link"},
        {"source": None},
        {"extra": 1},
    ],
    ids=lambda change: next(iter(change)) + "=" + str(next(iter(change.values()))),
)
def test_a_rate_needs_a_positive_number_a_date_and_a_source(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ExchangeRate.model_validate({**RATE, **change})


@pytest.mark.parametrize("missing", ["units_per_usd", "as_of", "source"])
def test_a_rate_cannot_leave_out_a_field(missing: str) -> None:
    with pytest.raises(ValidationError):
        ExchangeRate.model_validate({k: v for k, v in RATE.items() if k != missing})


def test_the_error_does_not_echo_what_was_written() -> None:
    secret = "sk-" + "A1" * 10
    with pytest.raises(ValidationError) as info:
        PricesFile.model_validate(
            {"prices": {"m": yuan()}, "exchange_rates": {"CNY": {**RATE, "note": 5, "x": secret}}}
        )
    assert secret not in str(info.value)


# ----------------------------------------------------------------------------- the schema


def test_the_schema_lists_the_new_currency_and_the_optional_rates() -> None:
    schema = json.loads(schema_texts()["prices.v1.schema.json"])
    currency = schema["$defs"]["ModelPrice"]["properties"]["currency"]
    assert currency["enum"] == ["USD", "CNY"] and currency["default"] == "USD"
    assert "exchange_rates" in schema["properties"]
    assert "exchange_rates" not in schema["required"], "old lists must keep validating"
    rate = schema["$defs"]["ExchangeRate"]
    assert set(rate["required"]) == {"units_per_usd", "as_of", "source"}
    assert rate["additionalProperties"] is False
    assert rate["properties"]["units_per_usd"]["exclusiveMinimum"] == 0


# ------------------------------------------------------------------- the list for Bailian

NOTE = "中国内地部署、最低档、忙时价、未计限时折扣与缓存折扣"
EXPECTED = {  # model: (input, output) in yuan per million tokens
    "qwen3.7-plus": (2, 8),
    "qwen-flash": (0.15, 1.5),
    "qwen-plus": (0.8, 8),
    "deepseek-v4.1-flash": (2, 8),
    "deepseek-v4-flash-0731": (3, 9),
    "glm-5": (4, 18),
    "glm-5.1": (6, 24),
    "kimi-k2.5": (4, 21),
}


def test_the_bailian_list_has_the_eight_models_in_yuan_with_date_source_and_note() -> None:
    prices = load_prices(BAILIAN)
    assert {m: (p.input_per_mtok, p.output_per_mtok) for m, p in prices.prices.items()} == EXPECTED
    for model, price in prices.prices.items():
        assert price.currency == "CNY", model
        assert str(price.as_of) == "2026-11-01", model
        assert price.source == "https://help.aliyun.com/zh/model-studio/model-pricing", model
        assert price.note == NOTE, model
        assert price.cache_read_per_mtok is None and price.cache_write_per_mtok is None, model


def test_the_bailian_list_carries_the_ecb_rate_of_2026_10_30() -> None:
    rate = load_prices(BAILIAN).exchange_rates["CNY"]
    assert rate.units_per_usd == 6.6529 and str(rate.as_of) == "2026-10-30"
    assert rate.source == "https://api.frankfurter.dev/v1/latest?base=USD&symbols=CNY"
    assert set(load_prices(BAILIAN).exchange_rates) == {"CNY"}
