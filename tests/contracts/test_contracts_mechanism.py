"""Mechanism definitions: the registry of the six formats."""

from typing import get_args

import pytest
from pydantic import ValidationError

from auction_night.contracts.mechanism import (
    BID_PHASES,
    BID_PHASES_BY_CLOCK,
    EXAMPLE_SLOTS,
    FORMAT_KEYS,
    MECHANISMS,
    PHASES,
    FormatKey,
    MechanismDef,
    get_mechanism,
    make_mechanism,
)

# pricing, clock, value model, reserve allowed, theta, levels
EXPECTED = {
    "english": ("second", "ascending_button", "private", True, 4, None),
    "dutch": ("first", "descending_levels", "private", True, None, 10),
    "sealed_first": ("first", "none", "private", True, None, None),
    "sealed_second": ("second", "none", "private", True, None, None),
    "all_pay": ("all_pay", "none", "private", False, None, None),
    "blind_box": ("second", "none", "common", True, None, None),
}


def test_registry_holds_exactly_the_six_formats() -> None:
    assert set(MECHANISMS) == set(EXPECTED) == set(get_args(FormatKey))
    assert tuple(MECHANISMS) == FORMAT_KEYS
    assert len(FORMAT_KEYS) == 6


@pytest.mark.parametrize("key", FORMAT_KEYS)
def test_definition_matches_the_rules(key: FormatKey) -> None:
    mech = get_mechanism(key)
    pricing, clock, value_model, reserve_allowed, theta, levels = EXPECTED[key]
    assert mech.key == key
    assert (mech.pricing, mech.clock, mech.value_model) == (pricing, clock, value_model)
    assert mech.reserve_allowed is reserve_allowed
    assert (mech.theta, mech.levels) == (theta, levels)
    assert mech.reserve == 0
    assert mech.ref == f"{key}@v1"


@pytest.mark.parametrize("key", FORMAT_KEYS)
def test_bid_phases_follow_the_clock(key: FormatKey) -> None:
    mech = MECHANISMS[key]
    assert mech.bid_phases == BID_PHASES_BY_CLOCK[mech.clock]
    assert set(mech.bid_phases) <= set(BID_PHASES)
    assert set(BID_PHASES) <= set(PHASES)


def test_every_bid_phase_belongs_to_some_format() -> None:
    used = {phase for mech in MECHANISMS.values() for phase in mech.bid_phases}
    assert used == set(BID_PHASES)


def test_text_keys_are_unique_and_namespaced() -> None:
    keys = [
        key
        for mech in MECHANISMS.values()
        for key in (
            mech.text_keys.rule,
            mech.text_keys.example,
            mech.text_keys.card,
            mech.text_keys.glossary,
        )
    ]
    assert len(keys) == len(set(keys)) == 24
    assert all(key.startswith("mechanism.") for key in keys)
    for mech in MECHANISMS.values():
        assert mech.text_keys.rule == f"mechanism.{mech.key}.rule"


@pytest.mark.parametrize("key", FORMAT_KEYS)
def test_example_slots_are_known_and_inputs_are_fixed(key: FormatKey) -> None:
    example = MECHANISMS[key].example
    assert example.slots
    assert set(example.slots) <= set(EXAMPLE_SLOTS)
    if key == "dutch":
        assert example.bids is None
        assert example.tier is not None and example.grab_price is not None
        assert example.grab_price < example.tier
    else:
        assert example.bids == (120, 90, 40)
        assert example.tier is None and example.grab_price is None


def test_all_pay_example_reports_every_payment() -> None:
    assert "payments" in MECHANISMS["all_pay"].example.slots
    assert "price" not in MECHANISMS["all_pay"].example.slots


def test_make_mechanism_applies_night_parameters() -> None:
    english = make_mechanism("english", reserve=10, theta=3)
    assert (english.reserve, english.theta) == (10, 3)
    dutch = make_mechanism("dutch", levels=5)
    assert dutch.levels == 5
    assert MECHANISMS["english"].theta == 4, "the registry entry must not change"


@pytest.mark.parametrize(
    ("key", "kwargs"),
    [
        ("all_pay", {"reserve": 5}),
        ("sealed_first", {"theta": 3}),
        ("sealed_second", {"levels": 10}),
        ("english", {"levels": 10}),
        ("dutch", {"theta": 3}),
        ("english", {"theta": 1}),
        ("dutch", {"levels": 1}),
    ],
)
def test_make_mechanism_rejects_incoherent_parameters(key: FormatKey, kwargs: dict) -> None:
    with pytest.raises(ValidationError):
        make_mechanism(key, **kwargs)


def test_definitions_are_frozen_and_closed() -> None:
    mech = MECHANISMS["english"]
    with pytest.raises(ValidationError):
        mech.reserve = 5  # type: ignore[misc]
    with pytest.raises(ValidationError):
        MechanismDef.model_validate({**mech.model_dump(), "surprise": 1})


def test_common_value_must_price_as_second_price() -> None:
    data = MECHANISMS["blind_box"].model_dump() | {"pricing": "first"}
    with pytest.raises(ValidationError):
        MechanismDef.model_validate(data)
