"""Amounts as models write them: digits, units, currency words and Chinese numerals.

``amount_from_text`` turns ``"120"``, ``"1,200"``, ``"120 金币"``, ``"约 一百二十"`` into an
integer. An explicit "no" (``"不出"``, ``"等"``, ``"false"``) is the amount 0, which means not
taking part, and a "yes" (``"抢"``, ``"true"``) has no number and gives None: nothing here ever
invents a price.
"""

import math
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Final

_CN_DIGITS: Final = {
    **dict.fromkeys("零〇○", 0),
    **dict.fromkeys("一壹", 1),
    **dict.fromkeys("二贰貳两兩", 2),
    **dict.fromkeys("三叁參", 3),
    **dict.fromkeys("四肆", 4),
    **dict.fromkeys("五伍", 5),
    **dict.fromkeys("六陆陸", 6),
    **dict.fromkeys("七柒", 7),
    **dict.fromkeys("八捌", 8),
    **dict.fromkeys("九玖", 9),
}
_CN_UNITS: Final = {
    **dict.fromkeys("十拾", 10),
    **dict.fromkeys("百佰", 100),
    **dict.fromkeys("千仟", 1000),
}
_CN_BIG: Final = {**dict.fromkeys("万萬", 10**4), **dict.fromkeys("亿億", 10**8)}
_SCALES: Final = {
    "万": 10**4,
    "萬": 10**4,
    "亿": 10**8,
    "億": 10**8,
    "千": 1000,
    "百": 100,
    "k": 1000,
}
NO_WORDS: Final = frozenset(
    {"否", "不", "不要", "不出", "不出价", "不参加", "不竞拍", "放弃", "弃权", "弃", "等", "等待",
     "再等等", "观望", "pass", "skip", "wait", "no", "false"}
)  # fmt: skip
NULL_WORDS: Final = frozenset(
    {"", "null", "none", "nil", "n/a", "na", "无", "没有", "空", "-", "—"}
)
_PREFIX: Final = re.compile(
    r"^(?:约为|大约|大概|差不多|约|出价|报价|价格为|价格|bid|price|about|around|approximately|是|为|出|报|[~≈$¥€])\s*",
    re.IGNORECASE,
)
_SUFFIX: Final = re.compile(
    r"\s*(?:左右|上下|金币|金幣|币|元|块钱|块|个|枚|分|整|coins?|gold|gp)$", re.IGNORECASE
)


def chinese_to_int(text: str) -> int | None:
    """An integer from Chinese numerals, or None when the text is not one.

    Handles ``一百二十``, ``两千零五``, ``十五``, the colloquial shorthand ``一百二`` (120) and
    ``三万五`` (35000), digit-by-digit forms such as ``二〇二六``, financial characters and a
    leading ``负``.
    """
    body = text.strip()
    sign = 1
    if body[:1] in ("负", "負"):
        sign, body = -1, body[1:]
    if not body or any(
        c not in _CN_DIGITS and c not in _CN_UNITS and c not in _CN_BIG for c in body
    ):
        return None
    if all(c in _CN_DIGITS for c in body):
        return sign * int("".join(str(_CN_DIGITS[c]) for c in body))
    total = section = number = digits = 0
    last_scale = 0
    zero_seen = False
    for char in body:
        if char in _CN_DIGITS:
            number = number * 10 + _CN_DIGITS[char]
            digits += 1
            zero_seen = zero_seen or _CN_DIGITS[char] == 0 and digits == 1
        elif char in _CN_UNITS:
            if digits == 0:
                if char not in "十拾" or section or total:
                    return None
                number = 1
            section += number * _CN_UNITS[char]
            number = digits = 0
            last_scale, zero_seen = _CN_UNITS[char], False
        else:
            big = _CN_BIG[char]
            section += number
            if section == 0:
                return None
            total += section * big
            section = number = digits = 0
            last_scale, zero_seen = big, False
    if number:
        colloquial = digits == 1 and last_scale >= 100 and not zero_seen
        section += number * (last_scale // 10) if colloquial else number
    return sign * (total + section)


def amount_from_text(text: str) -> int | None:
    """An integer amount from text such as ``120``, ``1,200``, ``120 金币`` or ``一百二十``.

    Returns None for anything that is not clearly one number. An explicit "no" (``不出``,
    ``false``, ``等``) is the amount 0, which means not taking part. A "yes" has no number and
    gives None.
    """
    body = (
        unicodedata.normalize("NFKC", text)
        .strip()
        .strip("。.!！?？,，;；\"'“”‘’「」『』【】[]()<> ")
    )
    if body.lower() in NO_WORDS:
        return 0
    for _ in range(3):
        body = _PREFIX.sub("", body).strip()
    for _ in range(3):
        body = _SUFFIX.sub("", body).strip()
    if re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?", body):
        body = body.replace(",", "")
    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", body):
        try:
            return math.floor(Decimal(body))
        except InvalidOperation:
            return None
    scaled = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([万萬亿億千百kK])", body)
    if scaled:
        unit = scaled.group(2).lower() if scaled.group(2) in "kK" else scaled.group(2)
        return math.floor(Decimal(scaled.group(1)) * _SCALES[unit])
    return chinese_to_int(body)
