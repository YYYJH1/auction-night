"""The rules that find and normalise numbers in commentary (auction_night.show.numbers_check)."""

from fractions import Fraction

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from auction_night.show.numbers_check import (
    canonical,
    extract_numbers,
    number_keys,
    unlisted_keys,
    unlisted_numbers,
    zh_value,
)


def keys(text: str) -> list[str]:
    return [token.key for token in extract_numbers(text)]


# -------------------------------------------------------------------------- arabic numerals


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("出价 74", ["74"]),
        ("成交价 0.7303", ["0.7303"]),
        ("付了 1,250", ["1250"]),
        ("占 12%", ["12"]),
        ("档位 200，估值 156", ["200", "156"]),
        ("预算 150-450", ["150", "450"]),
        ("预算 150–450", ["150", "450"]),
        ("教科书出价约为估值的 7/8", ["7", "8"]),
        ("第 7 件", ["7"]),
        ("约 0.111T", ["0.111"]),
        ("74元，80块", ["74", "80"]),
        ("2507.09083", ["2507.09083"]),
        ("没有数字", []),
    ],
)
def test_arabic_numbers_are_found(text: str, expected: list[str]) -> None:
    assert keys(text) == expected


def test_full_width_digits_and_symbols_are_mapped() -> None:
    assert keys("出价７４，占１２％，真值０．７３") == ["74", "12", "0.73"]


def test_the_same_number_written_differently_is_one_number() -> None:
    assert len({token.key for token in extract_numbers("5、05、5.0、5.00、５")}) == 1


def test_signs_are_dropped() -> None:
    assert keys("超额收益 -55，+12，−7") == ["55", "12", "7"]


def test_a_full_width_comma_separates_and_never_groups() -> None:
    assert keys("出价 150，200") == ["150", "200"]


def test_identifiers_are_names_and_not_numbers() -> None:
    text = "claude-sonnet-5-5、sealed_first@v1、Qwen3-8B、gpt-5-mini 和 Q3 都不是数字，但 5 号是"
    assert keys(text) == ["5"]


def test_percent_and_plain_give_the_same_key() -> None:
    assert keys("12%") == keys("12") == keys("百分之十二")


# ------------------------------------------------------------------------ chinese numerals


@pytest.mark.parametrize(
    ("run", "value"),
    [
        ("七十四", 74),
        ("一百二十", 120),
        ("一百二十五", 125),
        ("一百二", 120),
        ("三千五", 3500),
        ("三千零五", 3005),
        ("十二", 12),
        ("二十", 20),
        ("两百", 200),
        ("一万二千", 12000),
        ("三万五", 35000),
        ("二十万", 200000),
        ("二〇二六", 2026),
        ("九", 9),
    ],
)
def test_chinese_numeral_values(run: str, value: int) -> None:
    assert zh_value(run) == value


@pytest.mark.parametrize("run", ["", "百", "万", "abc", "一x"])
def test_unreadable_runs_have_no_value(run: str) -> None:
    assert zh_value(run) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("付了七十四个币", ["74"]),
        ("估值一百二十", ["120"]),
        ("第三件", ["3"]),
        ("第一件", ["1"]),
        ("两位选手", ["2"]),
        ("五号座位", ["5"]),
        ("七成把握", ["7"]),
        ("三倍", ["3"]),
        ("十个", ["10"]),
        ("百分之十二", ["12"]),
        ("三分之二", ["3", "2"]),
        ("一百元和两百元", ["100", "200"]),
    ],
)
def test_chinese_quantities_are_numbers(text: str, expected: list[str]) -> None:
    assert keys(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "一个人",
        "一次出手",
        "一价拍卖和二价拍卖",
        "一半的预算",
        "十分精彩",
        "万一输了",
        "千万别",
        "十足的把握",
        "数十个",
        "几百个",
        "十几个",
        "一点点",
        "一样",
    ],
)
def test_ordinary_words_with_numeral_characters_are_not_numbers(text: str) -> None:
    assert keys(text) == []


def test_arabic_and_chinese_numbers_mix_in_reading_order() -> None:
    assert keys("第七件出价 74，付了一百二十") == ["7", "74", "120"]


# ---------------------------------------------------------------------------------- keys


def test_canonical_text_of_numbers() -> None:
    assert canonical(Fraction(74)) == "74"
    assert canonical(Fraction(7303, 10000)) == "0.7303"
    assert canonical(Fraction(5, 2)) == "2.5"
    assert canonical(Fraction(-3, 8)) == "0.375"
    assert canonical(Fraction(1, 3)) == "1/3"  # not a terminating decimal: never read from text


@settings(deadline=None)
@given(st.integers(min_value=0, max_value=10**9))
def test_every_integer_reads_back_as_itself(n: int) -> None:
    assert keys(f"出价 {n}") == [str(n)]


@settings(deadline=None)
@given(
    st.integers(min_value=0, max_value=10**6),
    st.integers(min_value=0, max_value=9999),
)
def test_decimals_normalise_to_the_same_key(whole: int, frac: int) -> None:
    text = f"{whole}.{frac:04d}"
    expected = canonical(Fraction(text))
    assert keys(text) == [expected]
    assert keys(text + "0") == [expected]  # a trailing zero changes nothing


def test_number_keys_collects_from_several_texts() -> None:
    assert number_keys("出价 5", "付 7，真值 5") == frozenset({"5", "7"})


# ---------------------------------------------------------------------------- the check


def test_unlisted_numbers_reports_only_numbers_outside_the_card() -> None:
    card = number_keys("第 3 件 5 号出价 74，成交价 70")
    found = unlisted_numbers("5 号出价 74，付了 80，比 70 高 10，又是 80", card)
    assert [t.key for t in found] == ["80", "10", "80"]
    assert unlisted_keys(found) == ("80", "10")


def test_a_text_without_numbers_always_passes() -> None:
    assert unlisted_numbers("一个很安静的夜晚", frozenset()) == []


def test_an_unreadable_chinese_numeral_never_matches_a_card() -> None:
    tokens = unlisted_numbers("百百百件", frozenset({"100"}))
    assert tokens and all(t.value is None for t in tokens)
