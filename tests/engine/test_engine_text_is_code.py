"""The rule text is the specification, and the settlement has to meet it.

The rules a bidder reads are generated from the mechanism definitions
(:mod:`auction_night.engine.rules_text`), and the settlement runs from the same definitions.
``test_engine_rules_text`` pins single sentences to scripted lots. This module goes the other
way round and lets the text drive the expectations:

* **Declarations.** What a text says about the price (which highest price is paid, who pays,
  whether the reserve is a floor), about ties, about a bid of 0, about clamping and about an
  unsold lot is *read out of the Chinese sentences*. An oracle that knows nothing else computes
  the winner and the payments, and the settlement must agree for random reserves, budgets and
  scripted bids, for every format. A sentence that stops being said, or is said differently,
  makes the reading fail loudly instead of passing quietly.
* **Parameters.** The English ``theta`` and the number of Dutch levels are read from the text,
  and the lot is played with those numbers.
* **Examples.** The numbers of a worked example are parsed back out of the text and compared
  with a fresh computation, for random example inputs.
* **Power.** A text with one sentence swapped for the sentence of another format is caught.
* **Versions.** A text that bidders read is pinned to its version label, because the wording
  changes what models bid.
"""

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pytest
from engine_support import active, edge_answers, oracle_english, tables
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from auction_night.contracts import FORMAT_KEYS, MECHANISMS, make_mechanism
from auction_night.contracts.mechanism import FormatKey, MechanismDef
from auction_night.engine.formats import (
    open_dutch,
    open_english,
    settle_dutch,
    settle_sealed,
    step_english,
)
from auction_night.engine.rules_text import (
    common_rule_text,
    example_text,
    rule_text,
    rule_with_example,
)

PROFILE = settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
SEALED: list[FormatKey] = ["sealed_first", "sealed_second", "all_pay", "blind_box"]
HIDDEN = 77  # the true value of a blind box in these lots


def numbers(text: str) -> list[int]:
    return [int(token) for token in re.findall(r"\d+", text)]


# --------------------------------------------------------------------- reading the text


def needs(text: str, phrase: str) -> None:
    """The text must say ``phrase``. Wording that drifted is a failure, not a skipped case."""
    assert phrase in text, f"the text no longer says {phrase!r}: {text}"


@dataclass(frozen=True)
class SealedRule:
    """What the text of a sealed format declares, and what the common rule adds."""

    pays: str  # "own": the winner's own bid; "second": the second-highest bid
    everyone_pays: bool
    price_floor: bool  # the price is not below the reserve
    has_reserve: bool
    reveals_value: bool  # the hidden value is published with the hammer


def read_sealed(text: str, common: str) -> SealedRule:
    """Read the price rule of a sealed format out of its text."""
    needs(common, "出价 0 表示不参加")
    needs(common, "平局按种子随机")
    needs(common, "出价超过剩余预算时按剩余预算计")
    needs(common, "没有人出正价，或最高出价低于保留价，拍品流拍，谁都不付款")
    needs(common, "密封类拍品落槌后公布所有人的出价")
    everyone = "所有人都付自己写的价" in text
    second = "付第二高的出价" in text
    own = "付自己写的价" in text
    assert second != own, f"the text must name exactly one price rule: {text}"
    return SealedRule(
        pays="second" if second else "own",
        everyone_pays=everyone,
        price_floor="不低于保留价" in text,
        has_reserve="没有保留价" not in text,
        reveals_value="真值在落槌后公布" in text,
    )


def text_expectation(
    rule: SealedRule,
    raw: Mapping[int, int | None],
    budgets: Mapping[int, int],
    reserve: int,
    keys: Mapping[int, int],
) -> tuple[Any, dict[int, int], dict[int, int]]:
    """Winner, payments and revealed bids that the text promises for scripted bids."""
    asked = active(budgets)
    # "出价 0 表示不参加", "出价超过剩余预算时按剩余预算计": nothing and 0 are out, more is clamped
    bids = {seat: min(raw.get(seat) or 0, budgets[seat]) for seat in asked}
    floor = max(reserve if rule.has_reserve else 0, 1)
    live = {seat: bid for seat, bid in bids.items() if bid >= floor}
    if not live:  # "没有人出正价，或最高出价低于保留价，拍品流拍，谁都不付款"
        return None, {}, bids
    top = max(live.values())
    # "平局按种子随机": the seeded key decides, the lowest rank first
    winner = min((seat for seat, bid in live.items() if bid == top), key=lambda s: (keys[s], s))
    if rule.everyone_pays:  # "所有人都付自己写的价，赢没赢都要付"
        return winner, {seat: bid for seat, bid in bids.items() if bid >= 1}, bids
    if rule.pays == "own":
        return winner, {winner: top}, bids
    rivals = [bid for seat, bid in bids.items() if seat != winner]
    return winner, {winner: max(max(rivals, default=0), reserve if rule.price_floor else 0)}, bids


def check_sealed_against_text(
    mech: MechanismDef,
    text: str,
    raw: Mapping[int, int | None],
    budgets: Mapping[int, int],
    keys: Mapping[int, int],
) -> None:
    rule = read_sealed(text, common_rule_text())
    winner, payments, bids = text_expectation(rule, raw, budgets, mech.reserve, keys)
    hidden = HIDDEN if mech.value_model == "common" else None
    hammer = settle_sealed(mech, raw, budgets, keys, true_value=hidden).hammer
    assert (hammer.winner, hammer.payments) == (winner, payments)
    assert hammer.unsold == (winner is None)
    assert hammer.bids == bids, "the bids of everybody who was asked are published"
    assert (hammer.true_value is not None) == rule.reveals_value


# -------------------------------------------------------------------------- sealed formats

reserves = st.integers(0, 130)


@PROFILE
@given(st.sampled_from(SEALED), st.data(), reserves)
def test_a_sealed_lot_does_what_its_rule_text_declares(
    key: FormatKey, data: st.DataObject, reserve: int
) -> None:
    budgets, keys = data.draw(tables(anchors=[reserve]))
    mech = make_mechanism(key, reserve=0 if key == "all_pay" else reserve)
    raw = data.draw(edge_answers(active(budgets), budgets, [mech.reserve]))
    check_sealed_against_text(mech, rule_text(mech), raw, budgets, keys)


@pytest.mark.parametrize("key", SEALED)
def test_the_four_sealed_texts_declare_what_the_definitions_say(key: FormatKey) -> None:
    mech = MECHANISMS[key]
    rule = read_sealed(rule_text(mech), common_rule_text())
    assert rule.everyone_pays == (mech.pricing == "all_pay")
    assert rule.pays == ("second" if mech.pricing == "second" else "own")
    assert rule.has_reserve == mech.reserve_allowed
    assert rule.reveals_value == (mech.value_model == "common")
    assert rule.price_floor == (mech.pricing == "second")


def test_a_text_that_names_the_wrong_price_rule_is_caught() -> None:
    budgets = {1: 500, 2: 500, 3: 500}
    keys = {1: 0, 2: 1, 3: 2}
    raw = {1: 120, 2: 90, 3: 40}
    second = MECHANISMS["sealed_second"]
    check_sealed_against_text(second, rule_text(second), raw, budgets, keys)
    for other in ("sealed_first", "all_pay"):  # the engine is second price; the text is not
        with pytest.raises(AssertionError):
            check_sealed_against_text(second, rule_text(MECHANISMS[other]), raw, budgets, keys)
    first = MECHANISMS["sealed_first"]
    with pytest.raises(AssertionError):
        check_sealed_against_text(first, rule_text(second), raw, budgets, keys)
    broken = rule_text(second).replace("付第二高的出价", "付第三高的出价")
    with pytest.raises(AssertionError, match="exactly one price rule"):
        check_sealed_against_text(second, broken, raw, budgets, keys)


def test_a_missing_sentence_of_the_common_rule_is_noticed() -> None:
    mech = MECHANISMS["sealed_first"]
    with pytest.raises(AssertionError, match="no longer says"):
        read_sealed(rule_text(mech), common_rule_text().replace("平局按种子随机。", ""))


# --------------------------------------------------------------------------------- English


def read_theta(text: str) -> int:
    """The ``theta`` the text declares, from both places that name it."""
    ignored = re.search(r"在场的人超过 (\d+) 个时，你的退出价会被自动执行", text)
    asked = re.search(r"剩下 2 到 (\d+) 人时，每有人退出，系统会问剩下的人要不要改退出价", text)
    assert ignored and asked, f"the text no longer states the theta rule: {text}"
    assert ignored.group(1) == asked.group(1), "the two sentences name different thetas"
    return int(ignored.group(1))


def check_english_against_text(
    mech: MechanismDef,
    table: Any,
    init: Mapping[int, int | None],
    scripts: list[Mapping[int, int | None]],
) -> None:
    text = rule_text(mech)
    for phrase in (
        "退出价低于 1 或低于保留价，算不参加",
        "新的退出价不能低于当前价，低于当前价就是当场退出",
        "没有回答则保留原来的退出价",
        "最后留下的人赢，付最后一位退出者的退出价，不低于保留价",
        "剩下的人退出价相同时，按种子随机选一人，按这个价买下",
    ):
        needs(text, phrase)
    budgets, keys = table
    # an independent English auction, tick by tick, played with the theta of the text
    winner, price, log = oracle_english(
        init, budgets, keys, mech.reserve, read_theta(text), scripts
    )
    step = open_english(mech, budgets, keys)
    rounds = iter(scripts)
    if not step.done:
        step = step_english(step.state, init)
    while not step.done:
        step = step_english(
            step.state, {s: v for s, v in next(rounds, {}).items() if s in step.ask}
        )
    assert step.outcome is not None
    hammer = step.outcome.hammer
    assert (hammer.winner, hammer.price) == (winner, price)
    assert [(d.seat, d.price) for d in step.outcome.dropouts if d.participated] == log


@PROFILE
@given(st.data(), reserves, st.integers(2, 8))
def test_an_english_lot_does_what_its_rule_text_declares(
    data: st.DataObject, reserve: int, theta: int
) -> None:
    table = data.draw(tables(anchors=[reserve]))
    budgets, _ = table
    asked = active(budgets)
    init = data.draw(edge_answers(asked, budgets, [reserve]))
    clocks = [reserve, *(value for value in init.values() if value is not None)]
    scripts = [data.draw(edge_answers(asked, budgets, clocks)) for _ in range(8)]
    mech = make_mechanism("english", reserve=reserve, theta=theta)
    check_english_against_text(mech, table, init, scripts)


@pytest.mark.parametrize("theta", [2, 3, 4, 5, 8])
def test_the_theta_of_the_text_is_the_theta_of_the_lot(theta: int) -> None:
    assert read_theta(rule_text(make_mechanism("english", theta=theta))) == theta


def test_a_text_whose_two_thetas_disagree_is_refused() -> None:
    text = rule_text(make_mechanism("english", theta=4)).replace("剩下 2 到 4 人", "剩下 2 到 5 人")
    with pytest.raises(AssertionError, match="different thetas"):
        read_theta(text)


# ----------------------------------------------------------------------------------- Dutch


def read_levels(text: str) -> int:
    found = re.search(r"共 (\d+) 档", text)
    assert found, f"the text no longer states the number of levels: {text}"
    return int(found.group(1))


def text_dutch(
    text: str,
    tier: int,
    reserve: int,
    raw: Mapping[int, int | None],
    budgets: Mapping[int, int],
    keys: Mapping[int, int],
) -> tuple[Any, Any, list[int], list[int]]:
    """Buyer, price, ladder and the seats to be asked, as the text promises them."""
    for phrase in (
        "价格降到出手价或更低的第一档时，你自动出手",
        "价格高于你剩余预算的档位，你不会出手",
        "剩余预算买不起最低一档的人不会被询问",
        "最先出手的人按那一档的价格买下",
        "同一档几人同时出手就按种子随机选一人",
        "到最低一档仍没人出手就流拍",
        "保留价以下的档位跳过",
    ):
        needs(text, phrase)
    levels = read_levels(text)
    # "价格从档位开始，每档降档位的一成": the step is the tier over the number of levels
    ladder = [tier - k * (tier // levels) for k in range(levels)]
    ladder = [price for price in ladder if price >= reserve]
    lowest = ladder[-1] if ladder else None
    asked = [seat for seat in active(budgets) if lowest is not None and budgets[seat] >= lowest]
    for price in ladder:
        # a seat steps in when the price has fallen to its grab price or below and it can pay
        fires = [s for s in asked if (raw.get(s) or 0) >= price and budgets[s] >= price]
        if fires:
            return min(fires, key=lambda s: (keys[s], s)), price, ladder, asked
    return None, None, ladder, asked


@PROFILE
@given(
    st.data(),
    st.sampled_from([(100, 10), (200, 10), (400, 10), (100, 5), (200, 4), (400, 20), (200, 2)]),
)
def test_a_dutch_lot_does_what_its_rule_text_declares(
    data: st.DataObject, shape: tuple[int, int]
) -> None:
    tier, levels = shape
    prices = [tier - k * (tier // levels) for k in range(levels)]  # the ladder of an open lot
    near_a_level = [max(0, price + shift) for price in prices for shift in (-1, 0, 1)]
    reserve = data.draw(st.one_of(st.integers(0, tier + 30), st.sampled_from(near_a_level)))
    mech = make_mechanism("dutch", reserve=reserve, levels=levels)
    budgets, keys = data.draw(tables(anchors=[reserve, *prices]))
    raw = data.draw(edge_answers(active(budgets), budgets, [reserve, *prices]))
    buyer, price, ladder, expected_asked = text_dutch(
        rule_text(mech), tier, reserve, raw, budgets, keys
    )
    step = open_dutch(mech, tier, budgets, keys)
    assert list(step.ask) == expected_asked, "only seats that can afford the lowest level are asked"
    answers = {seat: value for seat, value in raw.items() if seat in expected_asked}
    outcome = settle_dutch(mech, tier, answers, budgets, keys)
    assert outcome.dutch is not None and outcome.dutch.ladder == ladder
    assert (outcome.hammer.winner, outcome.hammer.price if buyer else None) == (buyer, price)
    assert outcome.hammer.unsold == (buyer is None)
    assert outcome.hammer.payments == ({} if buyer is None else {buyer: price})


@pytest.mark.parametrize("levels", [2, 4, 5, 10, 20])
def test_the_levels_of_the_text_are_the_levels_of_the_ladder(levels: int) -> None:
    mech = make_mechanism("dutch", levels=levels)
    assert read_levels(rule_text(mech)) == levels
    outcome = settle_dutch(mech, 400, {1: 400}, {1: 500}, {1: 0})
    assert outcome.dutch is not None and len(outcome.dutch.ladder) == levels


# ------------------------------------------------------------------------ worked examples


def with_example(key: FormatKey, **example: Any) -> MechanismDef:
    """The definition of a format with other fixed inputs for its worked example."""
    data = MECHANISMS[key].model_dump()
    data["example"] = {**data["example"], **example}
    return MechanismDef.model_validate(data)


def example_numbers(key: FormatKey, bids: list[int]) -> list[int]:
    """What the example text must contain, computed by sorting and not by the settlement."""
    ranked = sorted(bids, reverse=True)
    if key == "all_pay":
        return [*bids, ranked[0], *bids]
    price = ranked[0] if key == "sealed_first" else ranked[1]
    return [*bids, ranked[0], price]


@PROFILE
@given(
    st.sampled_from(["english", "sealed_first", "sealed_second", "all_pay", "blind_box"]),
    st.lists(st.integers(0, 450), min_size=2, max_size=8).filter(lambda bids: max(bids) > 0),
)
def test_example_numbers_parsed_from_the_text_equal_a_fresh_computation(
    key: FormatKey, bids: list[int]
) -> None:
    text = example_text(with_example(key, bids=bids))
    assert numbers(text) == example_numbers(key, bids)
    assert text.startswith("例：") and text.endswith("。")


@PROFILE
@given(st.integers(1, 60), st.data())
def test_dutch_example_numbers_parsed_from_the_text_equal_a_fresh_computation(
    step: int, data: st.DataObject
) -> None:
    tier = 10 * step  # ten levels: the price of every level is a whole number
    grab = data.draw(st.integers(step, tier + 50))
    text = example_text(with_example("dutch", tier=tier, grab_price=grab))
    ladder = [tier - k * step for k in range(10)]
    reached = next(i for i, price in enumerate(ladder) if price <= grab)
    head = ladder[: reached + 1]
    assert numbers(text) == [tier, grab, *head, ladder[reached], ladder[reached]]


@pytest.mark.parametrize("key", ["english", "sealed_first", "sealed_second", "all_pay"])
def test_an_example_in_which_nothing_sells_has_no_text(key: FormatKey) -> None:
    with pytest.raises(ValueError, match="does not sell"):
        example_text(with_example(key, bids=[0, 0, 0]))


def test_a_dutch_example_that_never_fires_has_no_text() -> None:
    with pytest.raises(ValueError, match="does not sell"):
        example_text(with_example("dutch", tier=200, grab_price=5))


# ---------------------------------------------------------------------- versions of texts

# The wording of a text that bidders read changes what they bid. Every digest is bound to the
# version label of the format (``english@v1`` and so on). The common rule belongs to every
# format and has no label of its own: a change there needs a new version for all six formats.
# When a sentence is changed on purpose, raise the version and replace the digest here, so that
# nights played under two wordings can never be mistaken for one another.
PINNED = {
    "english@v1": "59764e23995bb0dd",
    "dutch@v1": "63de9d5446da54a3",
    "sealed_first@v1": "b4dd5b59a173a782",
    "sealed_second@v1": "0386ccb897f914d3",
    "all_pay@v1": "ae7dd6ca681047a4",
    "blind_box@v1": "88a09a2f64c2f1b9",
    "common": "070c0c2facc3a198",
}


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def current_digests() -> dict[str, str]:
    """Digest of what a bidder reads for each format (rule and worked example) and of the
    common rule."""
    out = {mech.ref: digest(rule_with_example(mech)) for mech in MECHANISMS.values()}
    out["common"] = digest(common_rule_text())
    return out


def test_the_rule_texts_are_pinned_to_their_versions() -> None:
    got = current_digests()
    assert set(got) == set(PINNED), "every format needs a pinned text"
    changed = sorted(ref for ref in PINNED if got[ref] != PINNED[ref])
    assert not changed, (
        f"the wording of {changed} changed: raise the version of the format (all six for the "
        f"common rule) and update PINNED with { {ref: got[ref] for ref in changed} }"
    )
    assert len(FORMAT_KEYS) == len(MECHANISMS) == 6
