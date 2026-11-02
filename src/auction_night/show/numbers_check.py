"""Numbers in commentary: how they are found, normalised and checked against a fact card.

The host may only copy numbers from the fact card. It never adds, subtracts or divides. To hold
it to that, every number in a text is extracted, normalised and looked up in the set of numbers of
the card. The same extractor builds the card's set (from the card text) and reads the host's text,
so a number is written in many ways but means one thing.

The rules, all of them covered by tests:

1. **Mapping.** Full-width digits, letters, ``％`` and ``．`` become their ASCII forms. Other
   punctuation is left alone: the full-width comma separates items and never groups digits.
2. **Identifiers are not numbers.** An ASCII word that starts with a letter and goes on with
   letters, digits, ``_`` and ``-``, ``@``, ``.`` joints (``claude-sonnet-5-5``,
   ``sealed_first@v1``, ``Q3``) is a name. Digits inside it are skipped.
3. **Arabic numerals.** Digit runs, with a decimal part (``0.7303``) and with comma groups of
   three (``1,250``). A trailing ``%`` is read and dropped, so ``12%`` is the number 12, the same
   as ``百分之十二``. Ranges and fractions give one number per side: ``150-450`` is 150 and 450,
   ``7/8`` is 7 and 8. Text such as ``0.111T`` and ``74元`` is read as a number followed by a
   unit.
4. **Chinese numerals.** A run of ``零〇一二两三四五六七八九十百千万亿`` counts when it is
   clearly a quantity:

   * it holds a unit (``十百千万亿``) and is at least two characters long (``二十``, ``一百二``,
     ``三万五``), or
   * it is a single unit or digit (or a run of digits) that follows ``第`` (``第三件``) or is
     followed by a counting word (``两位``, ``五号``, ``十个``, ``七成``, ``三倍``), or
   * it sits in ``百分之X`` or ``N分之M``, which give the numbers X, or N and M.

   Not counted, because they are ordinary words: a lone ``一`` that is not an ordinal (``一个``,
   ``一次``, ``一价``, ``一半`` read as "a", "once", "first-price"), vague amounts (``数十``,
   ``几百``, ``十几``) and common idioms (``十分``, ``万一``, ``千万``, ``十足`` ...).
5. **Values are exact.** A number is a fraction, so ``05``, ``5`` and ``5.0`` are one number.
   The sign is dropped: ``-55`` and ``55`` are the same number, because Chinese prose carries the
   sign in words (a loss, an overpayment). The check therefore cannot tell a figure from its
   negative, and a wrong direction is not caught here.

The rules are deliberately conservative: when a text is unusual the number is reported, the host
is asked to rewrite, and the card is played instead if it cannot. A number that is only reported by
mistake costs a rewrite. A number that slips through would be a false statement on air.
"""

import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from fractions import Fraction
from typing import Final

__all__ = [
    "NumberToken",
    "canonical",
    "extract_numbers",
    "number_keys",
    "unlisted_keys",
    "unlisted_numbers",
    "zh_value",
]

_FULL_WIDTH: Final = {
    **{chr(0xFF10 + i): str(i) for i in range(10)},
    **{chr(0xFF21 + i): chr(ord("A") + i) for i in range(26)},
    **{chr(0xFF41 + i): chr(ord("a") + i) for i in range(26)},
    "．": ".",
    "％": "%",
    "－": "-",
    "＠": "@",
    "＿": "_",
}
_TABLE: Final = str.maketrans(_FULL_WIDTH)

_IDENT: Final = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:[-@.][A-Za-z0-9_]+)*")
_ARABIC: Final = re.compile(
    r"(?<![0-9])(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)(?P<pct>%?)"
)

_DIGITS: Final = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}  # fmt: skip
_SMALL_UNITS: Final = {"十": 10, "百": 100, "千": 1000}
_BIG_UNITS: Final = {"万": 10_000, "亿": 100_000_000}
_NUMERAL_CHARS: Final = "".join([*_DIGITS, *_SMALL_UNITS, *_BIG_UNITS])
_NUMERAL_CLASS: Final = f"[{_NUMERAL_CHARS}]"
_RUN: Final = re.compile(f"{_NUMERAL_CLASS}+")
_PERCENT_ZH: Final = re.compile(f"百分之(?P<x>{_NUMERAL_CLASS}+)")
_FRACTION_ZH: Final = re.compile(f"(?P<n>{_NUMERAL_CLASS}+)分之(?P<m>{_NUMERAL_CLASS}+)")

# Counting words that make a bare Chinese digit a quantity. Words that are mostly idiom after a
# number ("一点", "一样", "一句话") are left out on purpose.
_COUNTERS: Final = "件个位号枚次轮档块元币人名条成折倍场回张只家座席局步种类天"
# Ordinary words that contain numeral characters.
_IDIOMS: Final = (
    "十全十美", "千方百计", "百发百中", "九牛一毛", "十分", "十足", "万一", "千万", "万分",
    "百般", "万能", "万物", "万事", "百姓", "百科", "百货", "千篇一律", "一千零一",
)  # fmt: skip
_IDIOM_PATTERN: Final = re.compile(
    f"(?<!{_NUMERAL_CLASS})(?:{'|'.join(sorted(map(re.escape, _IDIOMS), key=len, reverse=True))})"
)
_VAGUE_BEFORE: Final = "数几"
_VAGUE_AFTER: Final = "几多余"


@dataclass(frozen=True, slots=True)
class NumberToken:
    """One number found in a text.

    ``value`` is None for a Chinese numeral that could not be read (it never matches a card).
    ``kind`` is ``arabic``, ``percent``, ``zh``, ``zh_percent`` or ``zh_fraction``.
    """

    text: str
    value: Fraction | None
    start: int
    end: int
    kind: str

    @property
    def key(self) -> str:
        """The canonical string used in number sets; unreadable numerals key to ``?<text>``."""
        return canonical(self.value) if self.value is not None else f"?{self.text}"


def canonical(value: Fraction) -> str:
    """The canonical text of a non-negative exact number: ``74``, ``0.7303``, ``2507.09083``.

    Every number read from text is a terminating decimal, so the result is always a plain
    decimal with no trailing zeros. A fraction that does not terminate (never produced here)
    is written ``p/q``.
    """
    value = abs(value)
    num, den = value.numerator, value.denominator
    if den == 1:
        return str(num)
    for places in range(1, 40):
        if 10**places % den == 0:
            whole, frac = divmod(num * (10**places // den), 10**places)
            return f"{whole}.{str(frac).zfill(places)}".rstrip("0")
    return f"{num}/{den}"


def zh_value(run: str) -> int | None:
    """The value of a run of Chinese numeral characters, or None if it is not a number.

    Handles units (``一百二十五``), a trailing digit after a unit (``一百二`` is 120, ``三万五``
    is 35000), a filler zero (``三千零五``), a leading ten (``十二``) and positional digits
    (``二〇二六``).
    """
    if not run or any(ch not in _NUMERAL_CHARS for ch in run):
        return None
    if not any(ch in _SMALL_UNITS or ch in _BIG_UNITS for ch in run):
        return int("".join(str(_DIGITS[ch]) for ch in run))
    total = section = 0
    digit: int | None = None
    scale = 1  # the last unit seen, for a trailing digit such as the 二 of 一百二
    zero_after_unit = False
    for ch in run:
        if ch in _DIGITS:
            if _DIGITS[ch] == 0:
                zero_after_unit = True
                continue
            digit = _DIGITS[ch]
        elif ch in _SMALL_UNITS:
            if digit is None:
                if ch != "十":
                    return None
                digit = 1  # 十二 is ten and two
            section += digit * _SMALL_UNITS[ch]
            scale, digit, zero_after_unit = _SMALL_UNITS[ch], None, False
        else:
            section += digit or 0
            if section == 0:
                return None
            total += section * _BIG_UNITS[ch]
            section, digit, scale, zero_after_unit = 0, None, _BIG_UNITS[ch], False
    if digit is not None:
        section += digit if zero_after_unit or scale == 1 else digit * scale // 10
    return total + section


def _mask(text: str, start: int, end: int) -> str:
    return text[:start] + " " * (end - start) + text[end:]


def _arabic(text: str) -> tuple[list[NumberToken], str]:
    """Arabic numbers of ``text`` and the text with identifiers and those numbers masked."""
    for match in _IDENT.finditer(text):
        text = _mask(text, *match.span())
    found: list[NumberToken] = []
    for match in _ARABIC.finditer(text):
        raw = match.group("num").replace(",", "")
        percent = bool(match.group("pct"))
        found.append(
            NumberToken(
                text=match.group(0),
                value=Fraction(raw if not raw.startswith(".") else "0" + raw),
                start=match.start(),
                end=match.end(),
                kind="percent" if percent else "arabic",
            )
        )
    masked = text
    for token in found:
        masked = _mask(masked, token.start, token.end)
    return found, masked


def _zh_token(text: str, start: int, end: int, kind: str) -> NumberToken:
    run = text[start:end]
    return NumberToken(run, _int_fraction(zh_value(run)), start, end, kind)


def _int_fraction(value: int | None) -> Fraction | None:
    return None if value is None else Fraction(value)


def _is_quantity(text: str, start: int, end: int) -> bool:
    """Whether the numeral run ``text[start:end]`` reads as a number, not as a plain word."""
    run = text[start:end]
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    if before and before in _VAGUE_BEFORE:
        return False
    if after and after in _VAGUE_AFTER:
        return False
    ordinal = before == "第"
    counted = bool(after) and after in _COUNTERS
    has_unit = any(ch in _SMALL_UNITS or ch in _BIG_UNITS for ch in run)
    if has_unit and len(run) >= 2:
        return True
    if ordinal:
        return True
    if run == "一":  # "a", "once", "first-price": too common as plain words
        return False
    return counted


def _chinese(masked: str) -> list[NumberToken]:
    found: list[NumberToken] = []
    for match in _IDIOM_PATTERN.finditer(masked):
        masked = _mask(masked, *match.span())
    for match in _PERCENT_ZH.finditer(masked):
        x = match.group("x")
        found.append(
            NumberToken(
                match.group(0), _int_fraction(zh_value(x)), match.start(), match.end(), "zh_percent"
            )
        )
        masked = _mask(masked, *match.span())
    for match in _FRACTION_ZH.finditer(masked):
        for part in ("n", "m"):
            found.append(_zh_token(masked, *match.span(part), "zh_fraction"))
        masked = _mask(masked, *match.span())
    for match in _RUN.finditer(masked):
        if _is_quantity(masked, *match.span()):
            found.append(_zh_token(masked, *match.span(), "zh"))
    return found


def extract_numbers(text: str) -> list[NumberToken]:
    """Every number of ``text``, in reading order, by the rules in the module text."""
    mapped = text.translate(_TABLE)
    arabic, masked = _arabic(mapped)
    tokens = arabic + _chinese(masked)
    tokens.sort(key=lambda token: (token.start, token.end))
    return tokens


def number_keys(*texts: str) -> frozenset[str]:
    """The canonical keys of all numbers in the given texts."""
    return frozenset(token.key for text in texts for token in extract_numbers(text))


def unlisted_numbers(text: str, allowed: Collection[str]) -> list[NumberToken]:
    """The numbers of ``text`` whose key is not in ``allowed``, in reading order.

    The same number written twice is reported twice. Use :func:`unlisted_keys` for the distinct
    keys.
    """
    return [token for token in extract_numbers(text) if token.key not in allowed]


def unlisted_keys(tokens: Iterable[NumberToken]) -> tuple[str, ...]:
    """Distinct keys of some tokens, in the order they first appear."""
    seen: dict[str, None] = {}
    for token in tokens:
        seen.setdefault(token.key, None)
    return tuple(seen)
