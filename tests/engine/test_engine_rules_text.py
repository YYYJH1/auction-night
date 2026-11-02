"""The rule texts say what the code does.

Every number in a worked example is parsed back out of the generated text and compared with a
fresh run of the settlement. Every rule that a text states is paired with a scripted lot that
the engine plays, and the parameters in the text follow the mechanism definition.
"""

import re
from collections.abc import Callable
from typing import Any

import pytest

import scan_text
from auction_night.contracts import FORMAT_KEYS, MECHANISMS, make_mechanism
from auction_night.contracts.mechanism import COMMON_RULE_KEY, FormatKey, MechanismDef
from auction_night.engine.formats import (
    LotOutcome,
    dutch_asked,
    dutch_ladder,
    open_english,
    play_lot,
    settle_dutch,
    settle_sealed,
    step_english,
)
from auction_night.engine.rules_text import (
    all_texts,
    card_text,
    common_rule_text,
    example_text,
    glossary_text,
    payment_example,
    render,
    rule_text,
    rule_with_example,
    rules_section,
)

KEYS = {s: s - 1 for s in range(1, 9)}
RICH = dict.fromkeys(range(1, 9), 1000)
SCAN_RULES = scan_text.build_rules()


def numbers(text: str) -> list[int]:
    return [int(token) for token in re.findall(r"\d+", text)]


def with_example(key: FormatKey, **example: Any) -> MechanismDef:
    """The definition of a format with different fixed example inputs."""
    data = MECHANISMS[key].model_dump()
    data["example"] = {**data["example"], **example}
    return MechanismDef.model_validate(data)


# ------------------------------------------------------------------------ the keys


def test_every_declared_text_key_renders() -> None:
    texts = all_texts()
    declared = {COMMON_RULE_KEY}
    for mech in MECHANISMS.values():
        keys = mech.text_keys
        declared |= {keys.rule, keys.example, keys.card, keys.glossary}
    assert set(texts) == declared
    assert len(texts) == 6 * 4 + 1
    for key, text in texts.items():
        assert text.strip() == text and text
        assert render(key) == text
    with pytest.raises(KeyError):
        render("mechanism.english.nothing")
    with pytest.raises(KeyError):
        render("mechanism.nothing.rule")


def test_texts_pass_the_public_text_scanner() -> None:
    for key, text in all_texts().items():
        found = scan_text.find_violations(
            "src/auction_night/engine/rules_text.py", text, SCAN_RULES
        )
        assert not found, (key, found)


@pytest.mark.parametrize("key", FORMAT_KEYS)
def test_a_text_starts_with_the_title_of_its_format(key: FormatKey) -> None:
    mech = MECHANISMS[key]
    for text in (rule_text(mech), card_text(mech), glossary_text(mech)):
        assert text.startswith(f"{mech.title_zh}：")
    assert example_text(mech).startswith("例：")
    assert rule_text(mech).endswith("。")


# ------------------------------------------------- examples equal the settlement output

EXAMPLE_NUMBERS = {
    "english": [120, 90, 40, 120, 90],
    "sealed_first": [120, 90, 40, 120, 120],
    "sealed_second": [120, 90, 40, 120, 90],
    "blind_box": [120, 90, 40, 120, 90],
    "all_pay": [120, 90, 40, 120, 120, 90, 40],
    "dutch": [200, 130, 200, 180, 160, 140, 120, 120, 120],
}


def fresh_example_numbers(key: FormatKey) -> list[int]:
    """The numbers a worked example must contain, from a run that does not use rules_text."""
    mech = make_mechanism(key, theta=MECHANISMS[key].theta, levels=MECHANISMS[key].levels)
    spec = MECHANISMS[key].example
    if key == "dutch":
        assert spec.tier is not None and spec.grab_price is not None
        outcome = settle_dutch(mech, spec.tier, {1: spec.grab_price}, {1: 5000}, {1: 0})
        assert outcome.dutch is not None and outcome.dutch.level is not None
        head = outcome.dutch.ladder[: outcome.dutch.level]
        return [spec.tier, spec.grab_price, *head, outcome.hammer.price, outcome.hammer.price]
    assert spec.bids is not None
    seats = range(1, len(spec.bids) + 1)
    offers = dict(zip(seats, spec.bids, strict=True))
    outcome = play_lot(
        mech,
        tier=200,
        budgets=dict.fromkeys(seats, 5000),
        tie_key={s: s - 1 for s in seats},
        answer=lambda step: offers if step.phase in ("sealed", "english_init") else {},
        true_value=100 if key == "blind_box" else None,
    )
    hammer = outcome.hammer
    assert hammer.winner is not None
    top = max(spec.bids)
    if key == "all_pay":
        return [*spec.bids, top, *[hammer.payments[s] for s in seats]]
    return [*spec.bids, top, hammer.price]


@pytest.mark.parametrize("key", FORMAT_KEYS)
def test_example_numbers_equal_the_settlement_output(key: FormatKey) -> None:
    parsed = numbers(example_text(MECHANISMS[key]))
    assert parsed == fresh_example_numbers(key), "the text drifted from the code"
    assert parsed == EXAMPLE_NUMBERS[key], "and both agree with the worked examples of the rules"


@pytest.mark.parametrize("key", FORMAT_KEYS)
def test_the_card_carries_the_same_example(key: FormatKey) -> None:
    mech = MECHANISMS[key]
    assert card_text(mech).endswith(example_text(mech))
    assert numbers(card_text(mech)) == EXAMPLE_NUMBERS[key]


@pytest.mark.parametrize("key", FORMAT_KEYS)
def test_the_declared_slots_are_filled_in_order(key: FormatKey) -> None:
    mech = MECHANISMS[key]
    example = payment_example(mech)
    assert example.key == key
    assert tuple(example.slots) == mech.example.slots
    if mech.example.bids is not None:
        assert example.slots["bids"] == tuple(mech.example.bids)
    if mech.example.tier is not None:
        assert example.slots["tier"] == mech.example.tier
        assert example.slots["grab_price"] == mech.example.grab_price


def test_slot_values_are_results_of_the_run() -> None:
    assert payment_example(MECHANISMS["english"]).slots == {
        "bids": (120, 90, 40),
        "winner_bid": 120,
        "price": 90,
    }
    assert payment_example(MECHANISMS["all_pay"]).slots["payments"] == (120, 90, 40)
    dutch = payment_example(MECHANISMS["dutch"]).slots
    assert dutch["ladder_head"] == (200, 180, 160, 140, 120)
    assert dutch["price"] == 120


@pytest.mark.parametrize("key", FORMAT_KEYS)
def test_other_example_inputs_change_the_text_through_the_settlement(key: FormatKey) -> None:
    if key == "dutch":
        mech = with_example("dutch", tier=400, grab_price=250)
        text = example_text(mech)
        assert numbers(text) == [400, 250, 400, 360, 320, 280, 240, 240, 240]
        return
    mech = with_example(key, bids=[300, 250, 70, 5])
    text = example_text(mech)
    price = {"sealed_first": 300, "sealed_second": 250, "blind_box": 250, "english": 250}
    if key == "all_pay":
        assert numbers(text) == [300, 250, 70, 5, 300, 300, 250, 70, 5]
        assert "四人分别付" in text
    else:
        assert numbers(text) == [300, 250, 70, 5, 300, price[key]]


def test_a_two_bidder_example() -> None:
    text = example_text(with_example("all_pay", bids=[80, 30]))
    assert numbers(text) == [80, 30, 80, 80, 30]
    assert "两人分别付" in text


def test_an_example_that_cannot_be_filled_is_refused() -> None:
    from auction_night.contracts.mechanism import ExampleSpec

    broken = MECHANISMS["sealed_first"].model_copy(
        update={"example": ExampleSpec(slots=("bids", "tier"), bids=(120, 90))}
    )
    with pytest.raises(ValueError, match="slots"):
        payment_example(broken)


# ------------------------------------------ parameters in the text follow the definition


@pytest.mark.parametrize("theta", [2, 3, 4, 6, 8])
def test_the_english_text_and_the_engine_use_the_same_theta(theta: int) -> None:
    mech = make_mechanism("english", theta=theta)
    assert set(numbers(rule_text(mech))) == {1, 2, theta}
    assert f"超过 {theta} 个" in rule_text(mech)
    assert f"剩下 2 到 {theta} 人" in rule_text(mech)
    # the engine asks the first revision question when theta seats remain (at most the seven
    # that stay after the first exit of eight), and not before
    step = open_english(mech, RICH, KEYS)
    step = step_english(step.state, {s: 10 * s for s in range(1, 9)})
    expected = min(theta, 7)
    assert step.phase == "english_revise"
    assert len(step.ask) == expected
    assert step.state.price == 10 * (8 - expected)


@pytest.mark.parametrize("levels", [2, 4, 5, 10, 20])
def test_the_dutch_text_and_the_engine_use_the_same_levels(levels: int) -> None:
    mech = make_mechanism("dutch", levels=levels)
    text = rule_text(mech)
    assert f"共 {levels} 档" in text
    assert set(numbers(text)) == ({levels} if levels == 10 else {1, levels})
    assert len(dutch_ladder(400, levels)) == levels
    assert ("一成" in text) == (levels == 10)


@pytest.mark.parametrize("key", ["sealed_first", "sealed_second", "all_pay", "blind_box"])
def test_sealed_rules_name_no_numbers(key: FormatKey) -> None:
    assert numbers(rule_text(MECHANISMS[key])) == []


def test_the_common_rule_names_only_zero() -> None:
    assert set(numbers(common_rule_text())) == {0}


def test_texts_can_be_rendered_for_a_nights_own_mechanisms() -> None:
    mechanisms = {
        key: make_mechanism(key, theta=3 if key == "english" else None) for key in FORMAT_KEYS
    }
    assert "超过 3 个" in render("mechanism.english.rule", mechanisms)
    assert "超过 4 个" in render("mechanism.english.rule")
    assert "超过 3 个" in rules_section(mechanisms=mechanisms)
    assert all_texts(mechanisms)["mechanism.english.rule"] == render(
        "mechanism.english.rule", mechanisms
    )


# --------------------------------------- every rule a text states is played by the engine


def sealed(key: FormatKey, bids: dict[int, int | None], reserve: int = 0, **kw: Any) -> LotOutcome:
    mech = make_mechanism(key, reserve=reserve)
    return settle_sealed(
        mech, bids, RICH, KEYS, true_value=77 if key == "blind_box" else None, **kw
    )


def english(
    init: dict[int, int | None], reserve: int = 0, revisions: list[dict] | None = None
) -> LotOutcome:
    mech = make_mechanism("english", reserve=reserve)
    scripts = [init, *(revisions or [])]
    budgets = dict.fromkeys(init, 1000)
    step = open_english(mech, budgets, KEYS)
    i = 0
    while not step.done:
        step = step_english(step.state, scripts[i] if i < len(scripts) else {})
        i += 1
    assert step.outcome is not None
    return step.outcome


def claim_first_price_pays_own_bid() -> None:
    assert sealed("sealed_first", {1: 120, 2: 90, 3: 40}).hammer.payments == {1: 120}


def claim_second_price_pays_second_highest() -> None:
    for key in ("sealed_second", "blind_box"):
        assert sealed(key, {1: 120, 2: 90, 3: 40}).hammer.payments == {1: 90}
        assert sealed(key, {1: 55, 2: 200, 3: 10}).hammer.payments == {2: 55}


def claim_second_price_is_not_below_the_reserve() -> None:
    for key in ("sealed_second", "blind_box"):
        assert sealed(key, {1: 120, 2: 90, 3: 40}, reserve=100).hammer.price == 100
    assert english({1: 120, 2: 90, 3: 40}, reserve=100).hammer.price == 100


def claim_all_pay_everyone_pays_own_bid() -> None:
    hammer = sealed("all_pay", {1: 120, 2: 90, 3: 40}).hammer
    assert hammer.payments == {1: 120, 2: 90, 3: 40}
    assert hammer.winner == 1


def claim_all_pay_has_no_reserve() -> None:
    with pytest.raises(ValueError, match="reserve"):
        make_mechanism("all_pay", reserve=1)


def claim_blind_box_value_is_revealed() -> None:
    assert sealed("blind_box", {1: 120, 2: 90}).hammer.true_value == 77


def claim_exit_below_one_or_the_reserve_is_not_taking_part() -> None:
    outcome = english({1: 100, 2: 59, 3: 0, 4: None}, reserve=60)
    assert [(d.seat, d.price, d.participated) for d in outcome.dropouts] == [
        (2, 0, False),
        (3, 0, False),
        (4, 0, False),
    ]
    assert not english({1: 1, 2: 0}).dropouts[0].participated


def claim_english_pays_the_last_exit_price() -> None:
    assert english({1: 120, 2: 90, 3: 40}).hammer.payments == {1: 90}
    assert english({1: 120, 2: 90, 3: 40}, revisions=[{1: None, 2: 150}]).hammer.payments == {
        2: 120
    }


def claim_english_asks_only_while_theta_remain_and_below_current_price_leaves() -> None:
    mech = make_mechanism("english")
    first = step_english(open_english(mech, RICH, KEYS).state, {s: 10 * s for s in range(1, 9)})
    assert len(first.ask) == 4
    # "新的退出价不能低于当前价，低于当前价就是当场退出"
    left_at_once = english({1: 120, 2: 90, 3: 40}, revisions=[{1: None, 2: 30}])
    assert (left_at_once.hammer.winner, left_at_once.hammer.price) == (1, 40)
    # "没有回答则保留原来的退出价"
    kept = english({1: 120, 2: 90, 3: 40}, revisions=[{}])
    assert [r.status for r in kept.records if r.phase == "english_revise"] == ["kept_previous"] * 2


def claim_english_equal_exit_prices_go_to_the_tie_key() -> None:
    outcome = english({1: 80, 2: 80, 3: 80})
    assert outcome.hammer.winner == 1 and outcome.hammer.price == 80


def claim_dutch_buys_at_the_first_level_at_or_below_the_grab_price() -> None:
    outcome = settle_dutch(make_mechanism("dutch"), 200, {1: 130}, {1: 1000}, {1: 0})
    assert outcome.hammer.price == 120


def claim_dutch_skips_levels_below_the_reserve() -> None:
    assert dutch_ladder(200, 10, reserve=130) == (200, 180, 160, 140)


def claim_dutch_levels_above_the_budget_do_not_fire() -> None:
    outcome = settle_dutch(make_mechanism("dutch"), 200, {1: 999}, {1: 150}, {1: 0})
    assert outcome.hammer.price == 140


def claim_dutch_seats_below_the_lowest_level_are_not_asked() -> None:
    assert dutch_asked({1: 19, 2: 20}, dutch_ladder(200, 10)) == (2,)


def claim_dutch_ties_and_no_buyer() -> None:
    mech = make_mechanism("dutch")
    tie = settle_dutch(mech, 200, {1: 150, 2: 150}, {1: 500, 2: 500}, {1: 1, 2: 0})
    assert tie.hammer.winner == 2
    assert settle_dutch(mech, 200, {1: 10}, {1: 500}, KEYS).hammer.unsold


def claim_sealed_ties_go_to_the_tie_key() -> None:
    for key in ("sealed_first", "sealed_second", "all_pay", "blind_box"):
        assert sealed(key, {1: 80, 2: 80, 3: 80}).hammer.winner == 1
        assert sealed(key, {1: 80, 2: 80}).hammer.tie_broken


def claim_zero_bids_are_not_taking_part_and_clamp_is_legal() -> None:
    outcome = settle_sealed(
        make_mechanism("sealed_first"), {1: 0, 2: 900}, {1: 500, 2: 300}, {1: 0, 2: 1}
    )
    assert outcome.hammer.winner == 2 and outcome.hammer.price == 300
    assert [r.status for r in outcome.records] == ["ok", "clamped"]


def claim_unsold_conditions() -> None:
    for key in ("sealed_first", "sealed_second", "blind_box"):
        assert sealed(key, {1: 50, 2: 20}, reserve=51).hammer.unsold
        assert sealed(key, {1: 0, 2: 0}).hammer.unsold
    assert sealed("all_pay", {1: 0, 2: 0}).hammer.unsold
    assert not sealed("all_pay", {1: 1, 2: 0}).hammer.unsold


def claim_sealed_bids_are_revealed_and_others_are_not() -> None:
    assert sealed("sealed_first", {1: 5, 2: 3}).hammer.bids == {
        s: v for s, v in zip(range(1, 9), [5, 3, 0, 0, 0, 0, 0, 0], strict=True)
    }
    assert english({1: 5, 2: 3}).hammer.bids is None
    outcome = settle_dutch(make_mechanism("dutch"), 200, {1: 150}, {1: 500}, KEYS)
    assert outcome.hammer.bids is None


# phrase in the text, the format whose text must contain it, and the claim the engine must meet
CLAIMS: list[tuple[FormatKey | None, str, Callable[[], None]]] = [
    ("sealed_first", "付自己写的价", claim_first_price_pays_own_bid),
    ("sealed_second", "付第二高的出价，不低于保留价", claim_second_price_pays_second_highest),
    ("sealed_second", "不低于保留价", claim_second_price_is_not_below_the_reserve),
    ("blind_box", "付第二高的出价，不低于保留价", claim_second_price_pays_second_highest),
    ("blind_box", "真值在落槌后公布", claim_blind_box_value_is_revealed),
    ("all_pay", "所有人都付自己写的价", claim_all_pay_everyone_pays_own_bid),
    ("all_pay", "没有保留价", claim_all_pay_has_no_reserve),
    (
        "english",
        "退出价低于 1 或低于保留价，算不参加",
        claim_exit_below_one_or_the_reserve_is_not_taking_part,
    ),
    ("english", "付最后一位退出者的退出价，不低于保留价", claim_english_pays_the_last_exit_price),
    ("english", "不低于保留价", claim_second_price_is_not_below_the_reserve),
    (
        "english",
        "低于当前价就是当场退出，没有回答则保留原来的退出价",
        claim_english_asks_only_while_theta_remain_and_below_current_price_leaves,
    ),
    (
        "english",
        "按种子随机选一人，按这个价买下",
        claim_english_equal_exit_prices_go_to_the_tie_key,
    ),
    (
        "dutch",
        "价格降到出手价或更低的第一档时，你自动出手",
        claim_dutch_buys_at_the_first_level_at_or_below_the_grab_price,
    ),
    ("dutch", "保留价以下的档位跳过", claim_dutch_skips_levels_below_the_reserve),
    (
        "dutch",
        "价格高于你剩余预算的档位，你不会出手",
        claim_dutch_levels_above_the_budget_do_not_fire,
    ),
    (
        "dutch",
        "买不起最低一档的人不会被询问",
        claim_dutch_seats_below_the_lowest_level_are_not_asked,
    ),
    ("dutch", "同一档几人同时出手就按种子随机选一人", claim_dutch_ties_and_no_buyer),
    (None, "平局按种子随机", claim_sealed_ties_go_to_the_tie_key),
    (None, "出价 0 表示不参加。", claim_zero_bids_are_not_taking_part_and_clamp_is_legal),
    (
        None,
        "出价超过剩余预算时按剩余预算计，这是合法的",
        claim_zero_bids_are_not_taking_part_and_clamp_is_legal,
    ),
    (None, "没有人出正价，或最高出价低于保留价，拍品流拍，谁都不付款", claim_unsold_conditions),
    (None, "全员付费没有保留价，全员出 0 才流拍", claim_unsold_conditions),
    (None, "密封类拍品落槌后公布所有人的出价", claim_sealed_bids_are_revealed_and_others_are_not),
]


@pytest.mark.parametrize(
    ("key", "phrase", "claim"), CLAIMS, ids=lambda v: getattr(v, "__name__", str(v))[:40]
)
def test_what_the_text_states_the_engine_does(
    key: FormatKey | None, phrase: str, claim: Callable[[], None]
) -> None:
    text = common_rule_text() if key is None else rule_text(MECHANISMS[key])
    assert phrase in text
    claim()


def test_wrong_claims_are_not_in_the_wrong_texts() -> None:
    assert "第二高" not in rule_text(MECHANISMS["sealed_first"])
    assert "自己写的价" not in rule_text(MECHANISMS["sealed_second"])
    assert "保留价" not in rule_text(MECHANISMS["sealed_first"]), "first price pays its bid anyway"
    assert "赢没赢都要付" not in rule_text(MECHANISMS["sealed_second"])
    assert "没有保留价" not in rule_text(MECHANISMS["sealed_second"])


# ---------------------------------------------------------- what a bidder is never told

STRATEGY_WORDS = ["最优", "占优", "均衡", "如实", "说真话", "最好", "应该", "建议", "策略", "划算"]


@pytest.mark.parametrize("key", FORMAT_KEYS)
def test_rule_and_example_never_say_what_to_bid(key: FormatKey) -> None:
    mech = MECHANISMS[key]
    for text in (rule_text(mech), example_text(mech)):
        assert not [w for w in STRATEGY_WORDS if w in text], (key, text)
    assert not [w for w in STRATEGY_WORDS if w in common_rule_text()]


def test_the_prompt_section_holds_rules_and_examples_only() -> None:
    section = rules_section()
    lines = section.split("\n")
    assert len(lines) == len(FORMAT_KEYS) + 1
    for line, key in zip(lines, FORMAT_KEYS, strict=False):
        mech = MECHANISMS[key]
        assert line == f"- {rule_with_example(mech)}"
        assert rule_with_example(mech) == rule_text(mech) + example_text(mech)
    assert lines[-1] == f"- {common_rule_text()}"
    for key in FORMAT_KEYS:
        assert glossary_text(MECHANISMS[key]) not in section
    assert not [w for w in STRATEGY_WORDS if w in section]


def test_the_prompt_section_can_be_limited_to_some_formats() -> None:
    lines = rules_section(["dutch", "sealed_first"]).split("\n")
    assert [line.split("：")[0] for line in lines[:-1]] == ["- 荷式降价", "- 密封一价"]
    assert lines[-1].startswith("- 通用：")


GLOSSARY_FORMULAS = {
    "english": "(n−1)/(n+1)·T",
    "sealed_first": "(n−1)/n·v",
    "sealed_second": "(n−1)/(n+1)·T",
    "all_pay": "(n−1)/n · vⁿ / Tⁿ⁻¹",
    "dutch": "(n−1)/n·v",
    "blind_box": "b(s) = s − ε(n−2)/n",
}


@pytest.mark.parametrize("key", FORMAT_KEYS)
def test_the_glossary_states_the_textbook_formula(key: FormatKey) -> None:
    text = glossary_text(MECHANISMS[key])
    assert "教科书模型" in text
    assert GLOSSARY_FORMULAS[key] in text
    assert ("共同价值" in text) == (key == "blind_box")
    assert ("赢家诅咒" in text) == (key == "blind_box")
