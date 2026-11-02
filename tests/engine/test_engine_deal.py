"""The deal: invariants for any seed, golden decks, independence of the draws, exact numbers."""

import math
from collections import Counter
from fractions import Fraction
from typing import Any

import pytest
from engine_support import mock_config, raw_config, seeds
from hypothesis import given, settings
from pydantic import ValidationError

from auction_night.contracts import NightConfig, SeedResolutionError, seed_commitment
from auction_night.contracts.config import BlindConfig
from auction_night.contracts.events import CollectorMission, DealPrivateData, ShillMission
from auction_night.engine.deal import (
    Deal,
    blind_bounds,
    budget_cells,
    budget_range,
    deal,
    draw_budget,
    ratio_e4,
    shill_threshold,
)
from auction_night.engine.rng import Stream

CONFIG = mock_config()
NIGHT = CONFIG.night
TIERS = {100: 5, 200: 4, 400: 3}


def config_with(mutate) -> NightConfig:  # noqa: ANN001
    raw = raw_config()
    mutate(raw)
    return NightConfig.model_validate(raw)


# ------------------------------------------------------------------------- invariants


def check_deal(d: Deal, config: NightConfig) -> None:
    night = config.night
    n_seats = len(config.seats)
    seats = set(range(1, n_seats + 1))

    # schedule
    assert [lot.lot for lot in d.lots] == list(range(1, night.n_lots + 1))
    assert Counter(lot.format for lot in d.lots) == dict.fromkeys(
        night.formats, night.lots_per_format
    )
    assert Counter(lot.tier for lot in d.lots) == night.tiers
    per_tag = night.n_lots // len(night.tags)
    assert Counter(lot.tag for lot in d.lots) == dict.fromkeys(night.tags, per_tag)

    # budgets
    low, high = budget_range(night)
    assert set(d.budgets) == seats
    assert all(low <= budget <= high for budget in d.budgets.values())

    for lot in d.lots:
        assert set(lot.values) == seats
        assert set(lot.tie_key) == seats
        order = sorted(lot.tie_key.values())
        assert len(set(order)) == n_seats  # distinct ranks
        assert all(0 <= rank < 8 for rank in order)
        mech = night.mechanism_for(lot.format)
        assert lot.reserve == mech.reserve
        assert lot.mechanism == mech.ref
        if lot.format == "blind_box":
            bounds = blind_bounds(lot.tier, night.blind)
            assert lot.blind_value is not None
            assert bounds.low <= lot.blind_value <= bounds.high
            assert set(lot.signals) == seats
            assert all(abs(s - lot.blind_value) <= bounds.noise for s in lot.signals.values())
            assert set(lot.values.values()) == {lot.blind_value}
        else:
            assert lot.blind_value is None
            assert lot.signals == {}
            assert all(0 <= v <= lot.tier for v in lot.values.values())

    # missions
    holders = {seat.seat for seat in config.seats if seat.kind != "bot"}
    assert set(d.missions) <= holders
    rules = night.missions
    collectors = [m for m in d.missions.values() if isinstance(m, CollectorMission)]
    shills = [m for m in d.missions.values() if isinstance(m, ShillMission)]
    assert (len(collectors), len(shills)) == (rules.collectors, rules.shills)
    for c in collectors:
        assert c.tag in night.tags
        assert (c.need, c.bonus) == (rules.collector_need, rules.bonus)
    targets = [m.lot for m in shills]
    assert len(set(targets)) == len(targets)
    for m in shills:
        lot = d.lot(m.lot)
        assert lot.format in rules.shill_formats
        assert m.threshold_price == math.ceil(Fraction(str(rules.shill_threshold)) * lot.tier)
        assert m.bonus == rules.bonus
    canaries = [m.canary for m in d.missions.values()]
    assert len(set(canaries)) == len(canaries)

    # cards
    for seat in seats:
        card = d.card(seat)
        assert isinstance(card, DealPrivateData)
        blind = {lot.lot for lot in d.lots if lot.is_blind}
        assert set(card.signals) == blind
        assert set(card.values) == {lot.lot for lot in d.lots} - blind
        assert card.mission == d.missions.get(seat)
        # the hidden value of a blind box is on nobody's card
        for lot in d.lots:
            if lot.is_blind:
                assert lot.lot not in card.values
    assert [card.seat for card in d.cards()] == sorted(seats)


@given(seeds)
@settings(max_examples=60, deadline=None)
def test_invariants_hold_for_any_seed(seed: str) -> None:
    check_deal(deal(CONFIG, seed=seed), CONFIG)


def test_the_configured_seed_is_the_default() -> None:
    assert deal(CONFIG) == deal(CONFIG, seed=NIGHT.seed)
    check_deal(deal(CONFIG), CONFIG)


def test_deal_is_a_pure_function() -> None:
    first, second = deal(CONFIG, seed="repeat"), deal(CONFIG, seed="repeat")
    assert first == second
    assert first.digest() == second.digest()
    assert first.digest() != deal(CONFIG, seed="repeat2").digest()


def test_cards_validate_against_the_contract_and_round_trip() -> None:
    d = deal(CONFIG, seed="cards")
    for card in d.cards():
        again = DealPrivateData.model_validate(card.model_dump(mode="json"))
        assert again == card


# ------------------------------------------------------------------------------ goldens

GOLDEN_SCHEDULE = [
    ("sealed_first", 100, "怪谈"),
    ("english", 400, "海洋"),
    ("blind_box", 100, "甜品"),
    ("all_pay", 400, "机械"),
    ("sealed_first", 400, "海洋"),
    ("blind_box", 100, "怪谈"),
    ("dutch", 200, "怪谈"),
    ("dutch", 100, "机械"),
    ("sealed_second", 100, "机械"),
    ("english", 200, "甜品"),
    ("sealed_second", 200, "甜品"),
    ("all_pay", 200, "海洋"),
]


def test_golden_deal_of_the_mock_night() -> None:
    d = deal(CONFIG)
    assert d.digest() == "sha256:f5b8dd9252284d951fd306ed45e5dc51b00c3e48a7bcbf3814154c38925af063"
    assert d.budgets == {1: 333, 2: 238, 3: 194, 4: 428, 5: 356, 6: 241, 7: 355, 8: 417}
    assert [(lot.format, lot.tier, lot.tag) for lot in d.lots] == GOLDEN_SCHEDULE
    assert d.lots[0].values == {1: 86, 2: 5, 3: 37, 4: 55, 5: 8, 6: 90, 7: 88, 8: 99}
    assert d.lots[0].tie_key == {1: 4, 2: 2, 3: 3, 4: 0, 5: 5, 6: 6, 7: 7, 8: 1}
    blind3, blind6 = d.lots[2], d.lots[5]
    assert blind3.blind_value == 77
    assert blind3.signals == {1: 91, 2: 61, 3: 61, 4: 71, 5: 79, 6: 64, 7: 80, 8: 66}
    assert blind6.blind_value == 143
    assert blind6.signals == {1: 123, 2: 153, 3: 128, 4: 145, 5: 131, 6: 157, 7: 153, 8: 125}
    missions = {seat: m.model_dump(mode="json") for seat, m in d.missions.items()}
    assert missions == {
        1: {"kind": "collector", "tag": "机械", "need": 2, "bonus": 150, "canary": "W4ZS5F"},
        2: {"kind": "collector", "tag": "怪谈", "need": 2, "bonus": 150, "canary": "HPT7B2"},
        3: {"kind": "shill", "lot": 10, "threshold_price": 190, "bonus": 150, "canary": "Q77CJS"},
        4: {"kind": "shill", "lot": 11, "threshold_price": 190, "bonus": 150, "canary": "T6XSF7"},
        5: {"kind": "collector", "tag": "甜品", "need": 2, "bonus": 150, "canary": "QZ28MF"},
        6: {"kind": "shill", "lot": 9, "threshold_price": 95, "bonus": 150, "canary": "X8CTS7"},
    }
    assert d.card(3).model_dump(mode="json") == {
        "seat": 3,
        "budget": 194,
        "values": {
            "1": 37,
            "2": 317,
            "4": 355,
            "5": 326,
            "7": 11,
            "8": 20,
            "9": 93,
            "10": 32,
            "11": 95,
            "12": 74,
        },
        "signals": {"3": 61, "6": 128},
        "mission": {
            "kind": "shill",
            "lot": 10,
            "threshold_price": 190,
            "bonus": 150,
            "canary": "Q77CJS",
        },
    }


@pytest.mark.parametrize(
    ("seed", "digest", "budgets"),
    [
        (
            "golden-a",
            "sha256:521cc759ffd4ab21495c3aa85d34a64f66e895eadf4e9f7665a61f5b4ed839f7",
            {1: 307, 2: 419, 3: 427, 4: 227, 5: 426, 6: 163, 7: 276, 8: 259},
        ),
        (
            "golden-b",
            "sha256:61c3cd21565f215ee25f36a05eff391f57096e7bbac7843b8ded777df5980237",
            {1: 344, 2: 435, 3: 307, 4: 353, 5: 227, 6: 305, 7: 266, 8: 168},
        ),
    ],
)
def test_golden_decks(seed: str, digest: str, budgets: dict[int, int]) -> None:
    d = deal(CONFIG, seed=seed)
    assert d.digest() == digest
    assert d.budgets == budgets


def test_golden_shill_missions_of_the_other_decks() -> None:
    a = deal(CONFIG, seed="golden-a")
    shills = {s: (m.lot, m.threshold_price) for s, m in a.missions.items() if m.kind == "shill"}
    assert shills == {1: (6, 95), 2: (11, 95), 6: (12, 190)}
    b = deal(CONFIG, seed="golden-b")
    shills = {s: (m.lot, m.threshold_price) for s, m in b.missions.items() if m.kind == "shill"}
    assert shills == {1: (11, 190), 2: (8, 190), 4: (5, 380)}


# ------------------------------------------------------------------------- independence


def snapshot(d: Deal) -> dict[str, Any]:
    return d.to_dict()


def test_blind_noise_only_moves_the_appraisals() -> None:
    quiet = config_with(lambda raw: raw["night"]["blind"].update(noise=0.1))
    a, b = deal(CONFIG, seed="indep"), deal(quiet, seed="indep")
    assert a.budgets == b.budgets
    assert a.missions == b.missions
    for x, y in zip(a.lots, b.lots, strict=True):
        assert (x.format, x.tier, x.tag, x.values, x.tie_key) == (
            y.format,
            y.tier,
            y.tag,
            y.values,
            y.tie_key,
        )
        assert x.blind_value == y.blind_value
        if x.is_blind:
            assert x.signals != y.signals
            assert all(abs(s - y.blind_value) <= y.tier // 10 for s in y.signals.values())


def test_budget_scale_only_moves_the_budgets() -> None:
    rich = config_with(lambda raw: raw["night"]["budget"].update(c=500))
    a, b = deal(CONFIG, seed="indep"), deal(rich, seed="indep")
    assert a.budgets != b.budgets
    assert budget_range(rich.night) == (250, 750)
    assert all(250 <= budget <= 749 for budget in b.budgets.values())
    assert a.lots == b.lots
    assert a.missions == b.missions


def test_mission_rules_only_move_the_missions() -> None:
    other = config_with(lambda raw: raw["night"]["missions"].update(bonus=99, shill_threshold=0.9))
    a, b = deal(CONFIG, seed="indep"), deal(other, seed="indep")
    assert a.budgets == b.budgets
    assert a.lots == b.lots
    assert {s: m.kind for s, m in a.missions.items()} == {s: m.kind for s, m in b.missions.items()}
    assert {m.bonus for m in b.missions.values()} == {99}
    for seat, mission in b.missions.items():
        if isinstance(mission, ShillMission):
            assert mission.lot == a.missions[seat].lot
            assert mission.threshold_price == -(-9 * b.lot(mission.lot).tier // 10)
        assert mission.canary == a.missions[seat].canary


def test_reserve_only_moves_the_reserve() -> None:
    reserved = config_with(lambda raw: raw["night"].update(reserve=30))
    a, b = deal(CONFIG, seed="indep"), deal(reserved, seed="indep")
    assert a.budgets == b.budgets
    assert a.missions == b.missions
    for x, y in zip(a.lots, b.lots, strict=True):
        assert x.values == y.values
        assert x.signals == y.signals
        assert x.tie_key == y.tie_key
        assert y.reserve == (0 if y.format == "all_pay" else 30)


def test_a_smaller_table_keeps_the_cards_of_the_remaining_seats() -> None:
    def shrink(raw: dict[str, Any]) -> None:
        raw["seats"] = raw["seats"][:4] + raw["seats"][6:]
        for new_number, seat in enumerate(raw["seats"], start=1):
            seat["seat"] = new_number
        raw["night"]["missions"].update(collectors=2, shills=2)

    small = config_with(shrink)
    assert len(small.seats) == 6
    a, b = deal(CONFIG, seed="table"), deal(small, seed="table")
    assert b.n_seats == 6
    assert {seat: a.budgets[seat] for seat in range(1, 7)} == b.budgets
    for x, y in zip(a.lots, b.lots, strict=True):
        assert (x.format, x.tier, x.tag, x.blind_value) == (y.format, y.tier, y.tag, y.blind_value)
        assert {s: x.values[s] for s in range(1, 7)} == y.values
        assert {s: x.signals[s] for s in range(1, 7) if x.signals} == y.signals
        # the order of tie ranks among the remaining seats is unchanged
        assert sorted(y.tie_key, key=y.tie_key.get) == [
            s for s in sorted(x.tie_key, key=x.tie_key.get) if s <= 6
        ]
    check_deal(b, small)


def test_each_purpose_has_its_own_stream() -> None:
    d = deal(CONFIG, seed="streams")
    lot = d.lots[0]
    assert lot.format != "blind_box"
    own = Stream("streams", "value", 1)
    assert [lot.values[s] for s in range(1, 9)] == [own.randint(0, lot.tier) for _ in range(8)]
    ties = Stream("streams", "tie", 1).shuffle(range(1, 9))
    assert [s for s, _ in sorted(lot.tie_key.items(), key=lambda kv: kv[1])] == ties


# ---------------------------------------------------------------------------- the seed


def seed_env_config(seed: str, *, commitment: str | None = None) -> NightConfig:
    def swap(raw: dict[str, Any]) -> None:
        raw["night"].pop("seed")
        raw["night"]["seed_env"] = "AUCTION_NIGHT_TEST_SEED"
        raw["night"]["deal_seed_commitment"] = commitment or seed_commitment(seed)

    return config_with(swap)


def test_seed_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    config = seed_env_config("env-seed-1")
    monkeypatch.setenv("AUCTION_NIGHT_TEST_SEED", "env-seed-1")
    assert deal(config) == deal(CONFIG, seed="env-seed-1")


def test_a_missing_or_wrong_seed_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    config = seed_env_config("env-seed-1")
    monkeypatch.delenv("AUCTION_NIGHT_TEST_SEED", raising=False)
    with pytest.raises(SeedResolutionError, match="seed_env"):
        deal(config)
    monkeypatch.setenv("AUCTION_NIGHT_TEST_SEED", "another-seed")
    with pytest.raises(SeedResolutionError, match="deal_seed_commitment") as caught:
        deal(config)
    assert "another-seed" not in str(caught.value)


@pytest.mark.parametrize("bad", ["", "has space", "x" * 129, "ü", "a/b"])
def test_an_override_seed_must_look_like_a_seed(bad: str) -> None:
    with pytest.raises(ValueError, match="seed"):
        deal(CONFIG, seed=bad)


# ---------------------------------------------------------------------- exact numbers


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [
        (0.5, 5000),
        (1.5, 15000),
        (0.95, 9500),
        (0.2, 2000),
        (1.0, 10000),
        (0.0001, 1),
        (0.3333, 3333),
    ],
)
def test_ratios_are_converted_exactly(ratio: float, expected: int) -> None:
    assert ratio_e4(ratio) == expected


def test_ratios_with_too_many_decimals_are_refused() -> None:
    with pytest.raises(ValueError, match="four decimals"):
        ratio_e4(0.00001)


def test_shill_threshold_rounds_up_exactly() -> None:
    assert [shill_threshold(t, 0.95) for t in (100, 200, 400)] == [95, 190, 380]
    assert shill_threshold(101, 0.95) == 96  # 95.95
    assert shill_threshold(7, 0.95) == 7  # 6.65
    assert shill_threshold(20, 0.95) == 19  # exactly 19, no float error
    assert shill_threshold(100, 1.0) == 100
    assert shill_threshold(3, 0.3333) == 1


def test_blind_bounds_are_the_integers_of_the_closed_interval() -> None:
    blind = NIGHT.blind
    assert (blind_bounds(100, blind).low, blind_bounds(100, blind).high) == (50, 150)
    assert blind_bounds(100, blind).noise == 20
    assert blind_bounds(200, blind).noise == 40
    assert blind_bounds(400, blind).noise == 80
    odd = blind_bounds(101, blind)
    assert (odd.low, odd.high, odd.noise) == (51, 151, 20)  # [50.5, 151.5] and 20.2
    with pytest.raises(ValueError, match="negative"):
        blind_bounds(100, BlindConfig(value_range=(0.1, 1.5), noise=0.2))
    with pytest.raises(ValueError, match="no integer"):
        blind_bounds(1, BlindConfig(value_range=(0.4, 0.6), noise=0.0))


def test_budget_range_and_cells() -> None:
    assert budget_range(NIGHT) == (150, 450)
    assert budget_cells(NIGHT) == (1_500_000, 4_500_000)
    wide = config_with(lambda raw: raw["night"]["budget"].update(c=101, spread=[0.55, 1.45]))
    assert budget_range(wide.night) == (55, 146)  # floor(55.55) and floor(146.45)
    assert budget_cells(wide.night) == (555_500, 1_464_500)
    assert all(55 <= deal(wide, seed=f"w{i}").budgets[1] <= 146 for i in range(40))


def test_budgets_are_floor_of_c_times_a_uniform_draw() -> None:
    cells = budget_cells(NIGHT)
    stream = Stream("budgets", "exact", 0)
    drawn = [draw_budget(stream, cells) for _ in range(30_000)]
    assert min(drawn) >= 150
    assert max(drawn) == 449  # the top of the range is never reached for c * high = 450
    assert abs(sum(drawn) / len(drawn) - 299.5) < 1.5
    counts = Counter(drawn)
    assert set(counts) == set(range(150, 450))
    expected = len(drawn) / 300
    chi = sum((counts[b] - expected) ** 2 / expected for b in range(150, 450))
    assert chi < 299 + 5 * math.sqrt(2 * 299) + 10


def test_a_degenerate_spread_gives_a_constant_budget() -> None:
    flat = config_with(lambda raw: raw["night"]["budget"].update(c=300, spread=[1.0, 1.0]))
    assert {deal(flat, seed=f"f{i}").budgets[1] for i in range(5)} == {300}


# ---------------------------------------------------------------- statistics of the cards


def assert_uniform_mean(sample: list[int], low: int, high: int) -> None:
    """The mean of a sample of a discrete uniform law is within 4.5 standard errors."""
    width = high - low + 1
    sd = math.sqrt((width**2 - 1) / 12)
    mean = (low + high) / 2
    assert min(sample) == low
    assert max(sample) == high
    assert abs(sum(sample) / len(sample) - mean) < 4.5 * sd / math.sqrt(len(sample))


def test_values_are_uniform_integers_on_the_tier() -> None:
    values: dict[int, list[int]] = {100: [], 200: [], 400: []}
    for i in range(300):
        for lot in deal(CONFIG, seed=f"stat{i}").lots:
            if not lot.is_blind:
                values[lot.tier].extend(lot.values.values())
    for tier, drawn in values.items():
        assert_uniform_mean(drawn, 0, tier)


def test_blind_values_and_noise_are_uniform_on_their_integer_ranges() -> None:
    hidden, noise = [], []
    for i in range(1500):
        for lot in deal(CONFIG, seed=f"blind{i}").lots:
            if lot.is_blind and lot.tier == 100:
                hidden.append(lot.blind_value)
                noise.extend(s - lot.blind_value for s in lot.signals.values())
    assert_uniform_mean(hidden, 50, 150)
    assert_uniform_mean(noise, -20, 20)


def test_mission_kinds_are_spread_evenly_over_the_model_seats() -> None:
    collector_seats: Counter[int] = Counter()
    for i in range(600):
        for seat, mission in deal(CONFIG, seed=f"kind{i}").missions.items():
            collector_seats[seat] += mission.kind == "collector"
    # each of the six model seats is a collector about half of the time
    assert all(240 < count < 360 for count in collector_seats.values())
    assert set(collector_seats) == set(range(1, 7))


def test_tie_ranks_are_a_fair_permutation() -> None:
    firsts: Counter[int] = Counter()
    for i in range(800):
        lot = deal(CONFIG, seed=f"ties{i}").lots[0]
        firsts[min(lot.tie_key, key=lot.tie_key.get)] += 1
    assert set(firsts) == set(range(1, 9))
    assert all(60 < count < 140 for count in firsts.values())


def test_seat_without_missions_when_there_are_more_model_seats_than_missions() -> None:
    def fewer(raw: dict[str, Any]) -> None:
        raw["night"]["missions"].update(collectors=2, shills=1)

    config = config_with(fewer)
    d = deal(config, seed="spare")
    assert len(d.missions) == 3
    assert set(d.missions) <= {1, 2, 3, 4, 5, 6}
    assert all(d.card(s).mission is None for s in range(1, 9) if s not in d.missions)


def test_other_table_shapes() -> None:
    def shape(raw: dict[str, Any]) -> None:
        night = raw["night"]
        night["formats"] = ["english", "dutch", "blind_box"]
        night["lots_per_format"] = 2
        night["tiers"] = {100: 3, 200: 2, 400: 1}
        night["tags"] = ["海洋", "机械", "甜品"]
        night["missions"].update(collectors=1, shills=1)

    config = config_with(shape)
    assert config.night.n_lots == 6
    for i in range(30):
        d = deal(config, seed=f"shape{i}")
        check_deal(d, config)
        assert Counter(lot.format for lot in d.lots) == {"english": 2, "dutch": 2, "blind_box": 2}
        assert [m.kind for m in d.missions.values() if m.kind == "shill"] == ["shill"]


def test_the_smallest_table() -> None:
    def two_seats(raw: dict[str, Any]) -> None:
        raw["seats"] = [
            {"seat": 1, "codename": "鲸落", "kind": "mock", "mock_policy": "honest"},
            {"seat": 2, "codename": "阿铁", "kind": "mock", "mock_policy": "collude"},
        ]
        raw["night"]["missions"].update(collectors=1, shills=1)

    config = config_with(two_seats)
    d = deal(config, seed="duo")
    check_deal(d, config)
    assert set(d.missions) == {1, 2}
    assert {m.kind for m in d.missions.values()} == {"collector", "shill"}


def test_zero_missions() -> None:
    config = config_with(lambda raw: raw["night"]["missions"].update(collectors=0, shills=0))
    assert deal(config, seed="none").missions == {}


def test_the_deal_object_does_not_carry_the_seed() -> None:
    d = deal(CONFIG, seed="sensitive-seed-text")
    assert "sensitive-seed-text" not in repr(d)
    assert "sensitive-seed-text" not in str(d.to_dict())


def test_lot_and_card_lookups_are_checked() -> None:
    d = deal(CONFIG, seed="lookup")
    with pytest.raises(ValueError, match="no lot"):
        d.lot(0)
    with pytest.raises(ValueError, match="no lot"):
        d.lot(13)
    with pytest.raises(ValueError, match="no seat"):
        d.card(9)


def test_config_that_cannot_be_dealt_is_refused() -> None:
    with pytest.raises(ValidationError):
        config_with(lambda raw: raw["night"]["missions"].update(collectors=9))
