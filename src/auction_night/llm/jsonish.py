"""A forgiving reader for the JSON-like objects that models write.

Valid JSON is read by :mod:`json`. Everything else goes through :class:`LenientReader`, a small
hand-written reader that builds plain Python values and notes every repair it had to make:
full-width brackets, commas and colons, smart and single quotes, trailing and missing commas,
unquoted keys, Python literals, comments, unclosed brackets and strings, quotes inside a string.
For valid JSON it returns exactly what :func:`json.loads` returns and notes no repair.

Nothing is evaluated or executed: the reader only follows characters and builds ``dict``,
``list``, ``str``, ``int``, ``float``, ``bool`` and ``None`` values.
"""

import json
import re
from typing import Any, Final

_MAX_DEPTH: Final = 32


class Unparseable(ValueError):
    """The text cannot be read as a JSON-like object even with repairs."""


_OPEN_QUOTES: Final = {
    '"': '"',
    "“": '”"',
    "”": '”"',
    "„": '“”"',
    "＂": '＂"',
    "'": "'",
    "‘": "’'",
    "’": "’'",
}
_FULLWIDTH: Final = {"｛": "{", "｝": "}", "［": "[", "］": "]", "，": ",", "：": ":", "、": ","}
_ESCAPES: Final = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}
_NUMBER_TOKEN: Final = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?\Z")
_TOKEN_END: Final = frozenset(",:}]，：｝］、=\"“”„＂'‘’ \t\r\n")
_LITERALS: Final = {"true": True, "false": False, "null": None, "none": None}


class LenientReader:
    """A reader for the JSON-like objects that models write.

    It builds Python values directly and notes every repair in ``fixes``. Valid JSON reads
    exactly as ``json.loads`` reads it, with no repairs noted.
    """

    def __init__(self, text: str, start: int) -> None:
        self.text = text
        self.pos = start
        self.fixes: list[str] = []

    def fix(self, name: str) -> None:
        if name not in self.fixes:
            self.fixes.append(name)

    def peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def skip(self) -> None:
        text = self.text
        while self.pos < len(text):
            char = text[self.pos]
            if char.isspace() or char == "\ufeff":
                self.pos += 1
            elif text.startswith("//", self.pos):
                end = text.find("\n", self.pos)
                self.pos = len(text) if end < 0 else end
                self.fix("comment")
            elif text.startswith("/*", self.pos):
                end = text.find("*/", self.pos + 2)
                self.pos = len(text) if end < 0 else end + 2
                self.fix("comment")
            else:
                break

    def structural(self, expected: str) -> bool:
        """Consume ``expected`` (or its full-width twin) if it is next."""
        char = self.peek()
        if char == expected:
            self.pos += 1
            return True
        if _FULLWIDTH.get(char) == expected:
            self.pos += 1
            self.fix("fullwidth")
            return True
        return False

    def value(self, depth: int, role: str = "value") -> Any:
        if depth > _MAX_DEPTH:
            raise Unparseable("nested too deep")
        self.skip()
        char = self.peek()
        if char == "":
            raise Unparseable("value expected")
        if char in "{｛":
            return self.mapping(depth)
        if char in "[［":
            return self.array(depth)
        if char in _OPEN_QUOTES:
            return self.string(role)
        return self.bare()

    def mapping(self, depth: int) -> dict[str, Any]:
        self.structural("{")
        result: dict[str, Any] = {}
        while True:
            self.skip()
            if self.pos >= len(self.text):
                self.fix("unclosed")
                return result
            if self.structural("}") or self._close_with("]"):
                return result
            if self.structural(","):
                continue  # a stray or doubled comma
            key = self.key()
            self.skip()
            if not (self.structural(":") or self._equals()):
                raise Unparseable("colon expected")
            result[key] = self.value(depth + 1, "value")
            self.skip()
            if self.structural(","):
                self.skip()
                if self.peek() in ("}", "｝", ""):
                    self.fix("trailing_comma")
            elif self.peek() not in ("}", "｝", "]", ""):
                self.fix("missing_comma")

    def _close_with(self, other: str) -> bool:
        if self.peek() == other:
            self.pos += 1
            self.fix("mismatched_bracket")
            return True
        return False

    def _equals(self) -> bool:
        if self.peek() == "=":
            self.pos += 1
            self.fix("equals_colon")
            return True
        return False

    def array(self, depth: int) -> list[Any]:
        self.structural("[")
        items: list[Any] = []
        while True:
            self.skip()
            if self.pos >= len(self.text):
                self.fix("unclosed")
                return items
            if self.structural("]") or self._close_with("}"):
                return items
            if self.structural(","):
                continue
            items.append(self.value(depth + 1, "item"))
            self.skip()
            if self.structural(","):
                self.skip()
                if self.peek() in ("]", "］", ""):
                    self.fix("trailing_comma")
            elif self.peek() not in ("]", "］", "}", ""):
                self.fix("missing_comma")

    def key(self) -> str:
        self.skip()
        if self.peek() in _OPEN_QUOTES:
            return self.string("key")
        token = self.token()
        if not token:
            raise Unparseable("key expected")
        self.fix("unquoted")
        return token

    def token(self) -> str:
        start = self.pos
        text = self.text
        while self.pos < len(text) and text[self.pos] not in _TOKEN_END and text[self.pos] != "=":
            if text[self.pos] in "{}[]｛｝［］":
                break
            self.pos += 1
        return text[start : self.pos]

    def bare(self) -> Any:
        start = self.pos
        token = self.token()
        if not token:
            self.pos = start + 1  # make progress on a stray character
            raise Unparseable("unexpected character")
        lowered = token.lower()
        if lowered in _LITERALS:
            if token not in ("true", "false", "null"):
                self.fix("python_literal")
            return _LITERALS[lowered]
        if _NUMBER_TOKEN.match(token):
            return float(token) if re.search(r"[.eE]", token) else int(token)
        self.fix("bare_value")
        return token

    def string(self, role: str) -> str:
        opener = self.text[self.pos]
        closers = _OPEN_QUOTES[opener]
        if opener != '"':
            self.fix("single_quotes" if opener in "'‘’" else "smart_quotes")
        self.pos += 1
        text = self.text
        out: list[str] = []
        while self.pos < len(text):
            char = text[self.pos]
            if char == "\\":
                out.append(self.escape())
            elif char in closers and self.closes_here(role):
                self.pos += 1
                return "".join(out)
            else:
                if char in "\n\r\t":
                    self.fix("raw_control")
                elif char in closers:
                    self.fix("stray_quote")
                out.append(char)
                self.pos += 1
        self.fix("unclosed")
        return "".join(out)

    def _next_visible(self, index: int) -> int:
        """The index of the next character that is neither white space nor inside a comment."""
        text = self.text
        while index < len(text):
            if text[index].isspace():
                index += 1
            elif text.startswith("//", index):
                newline = text.find("\n", index)
                index = len(text) if newline < 0 else newline
            elif text.startswith("/*", index):
                close = text.find("*/", index + 2)
                index = len(text) if close < 0 else close + 2
            else:
                break
        return index

    def closes_here(self, role: str) -> bool:
        """Whether the quote at the current position ends the string.

        A quote closes a string when what follows it is JSON again. That is the end of the text or
        a closing bracket in every case, a colon after a key, and a comma that is followed by the
        next key (after an object value) or the next value (after an array item). Anything else
        is a quote inside the text, such as ``"他说"好"，然后走了"``, and belongs to the string.
        """
        text = self.text
        index = self._next_visible(self.pos + 1)
        following = text[index] if index < len(text) else ""
        if following in ("", "}", "]", "｝", "］"):
            return True
        if following in (":", "：", "="):
            return role == "key"
        if following not in (",", "，", "、") or role == "key":
            return False
        index = self._next_visible(index + 1)
        after = text[index] if index < len(text) else ""
        if after in ("", "}", "]", "｝", "］"):
            return True
        if role == "value":
            return after in _OPEN_QUOTES or (
                re.match(r"[A-Za-z_][\w-]*\s*[:：=]", text[index : index + 40]) is not None
            )
        return (
            after in _OPEN_QUOTES
            or after in "{[｛［+-."
            or after.isdigit()
            or (
                re.match(r"(?:true|false|null|none)\b", text[index : index + 6], re.IGNORECASE)
                is not None
            )
        )

    def escape(self) -> str:
        text = self.text
        self.pos += 1
        if self.pos >= len(text):
            return ""
        char = text[self.pos]
        self.pos += 1
        if char in _ESCAPES:
            return _ESCAPES[char]
        if char == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", text[self.pos : self.pos + 4]):
            code = int(text[self.pos : self.pos + 4], 16)
            self.pos += 4
            if 0xD800 <= code < 0xDC00 and text.startswith("\\u", self.pos):
                low = text[self.pos + 2 : self.pos + 6]
                if re.fullmatch(r"[dD][c-fC-F][0-9a-fA-F]{2}", low):
                    self.pos += 6
                    return chr(0x10000 + ((code - 0xD800) << 10) + (int(low, 16) - 0xDC00))
            return chr(code) if not 0xD800 <= code < 0xE000 else "\ufffd"
        self.fix("bad_escape")
        return char


def read_object(text: str, start: int) -> tuple[dict[str, Any], int, tuple[str, ...]]:
    """Read the JSON object that begins at ``text[start]``.

    Returns the object, the index just after it and the repairs that were needed (empty for
    valid JSON). Raises ``ValueError`` when the text cannot be read as an object.
    """
    if text[start : start + 1] == "{":
        try:
            value, end = json.JSONDecoder().raw_decode(text, start)
        except (ValueError, RecursionError):
            pass
        else:
            if isinstance(value, dict):
                return value, end, ()
    reader = LenientReader(text, start)
    try:
        value = reader.value(0)
    except Unparseable as exc:
        raise ValueError(str(exc)) from exc
    if not isinstance(value, dict):
        raise ValueError("not an object")
    return value, reader.pos, tuple(reader.fixes)
