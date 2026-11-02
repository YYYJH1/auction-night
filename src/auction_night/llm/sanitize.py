"""Cleaning of text that a model wrote, before it becomes an event or part of a prompt.

Chat lines, inner monologue and the plan persona all come from models. They are shown to other
seats (inside observations), to the audience (replay page, terminal) and to the judges, so a
line must not be able to pretend to be something else:

* line breaks are removed, so a line cannot start a fake channel entry such as
  ``[第 7 件] 5 号「青柠」：...``;
* the structural characters of the prompt templates (``【】「」[]〔〕｜`` and the brackets of
  markup languages) are dropped, so a line cannot forge a section header or a markup tag;
* control characters, ANSI escape sequences and invisible format characters are removed, so a
  line cannot drive a terminal or hide text;
* the length is capped.

The functions are pure and idempotent: cleaning clean text changes nothing. Cleaning returns the
number of characters that were removed, replaced or cut, which the engine stores as
``sanitized_chars`` and the parser reports as the ``sanitize`` repair.

Observations additionally show chat as a JSON string literal (:func:`quote_literal`), which
escapes the quote character, so the text cannot end its own quotation either. Nothing in this
module evaluates or executes text.
"""

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Final

from auction_night.contracts.config import ChatConfig, NightSettings, ThoughtConfig

# The defaults are read from the configuration contract, so they cannot drift apart from it.
SAY_MAX_CHARS: Final[int] = ChatConfig().max_chars
PERSONA_MAX_CHARS: Final[int] = ChatConfig().persona_max_chars
THOUGHT_MAX_CHARS: Final[int] = ThoughtConfig().max_chars
LABEL_MAX_CHARS: Final = 16

# Characters that the prompt templates use as structure, plus the brackets of markup languages
# (HTML tags, rich markup, JSON) and their full-width forms.
STRUCTURAL_CHARS: Final = frozenset("【】「」『』〔〕[]{}<>｜|［］｛｝＜＞")

# Unicode categories that are removed: control (Cc), format (Cf, zero-width and bidirectional
# marks) and surrogates (Cs). White space is handled separately and becomes a single space.
_DROPPED_CATEGORIES: Final = frozenset({"Cc", "Cf", "Cs"})

# Longer input is cut before it is looked at: nothing past this point could survive the cap.
_PRECUT_CHARS: Final = 50_000

_ANSI: Final = re.compile(
    r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"  # CSI: colours, cursor moves, erase
    r"|(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)?"  # OSC: titles, links
    r"|\x1b[PX^_][^\x1b]*(?:\x1b\\)?"  # DCS, SOS, PM, APC
    r"|\x1b[ -/]*[0-~]"  # any other escape sequence
)


@dataclass(frozen=True)
class Cleaned:
    """Clean text and how much of the input did not survive.

    ``changed`` counts input characters that were removed, replaced (a tab or a line break
    becomes a space) or cut by the length cap. Trimmed white space at either end is not counted.
    """

    text: str
    changed: int = 0


@dataclass(frozen=True)
class TextLimits:
    """Length caps of the three model-written fields."""

    say: int = SAY_MAX_CHARS
    thought: int = THOUGHT_MAX_CHARS
    persona: int = PERSONA_MAX_CHARS

    @classmethod
    def from_night(cls, night: NightSettings) -> "TextLimits":
        """The caps of one night's configuration."""
        return cls(
            say=night.chat.max_chars,
            thought=night.thought.max_chars,
            persona=night.chat.persona_max_chars,
        )


def strip_ansi(text: str) -> str:
    """``text`` without ANSI escape sequences (colours, cursor control, titles, links)."""
    return _ANSI.sub("", text)


def _whitespace_cost(run: str) -> int:
    """Characters of a white space run that do not survive as one plain space."""
    return len(run) - 1 + (0 if run[0] == " " else 1)


def clean_text(raw: object, max_chars: int | None) -> Cleaned:
    """Clean one line of model text and cap it at ``max_chars`` characters.

    Escape sequences go first, then control and format characters and the structural characters.
    Every run of white space (line breaks and tabs included) becomes one space and the ends are
    trimmed. ``None`` is empty text and any other non-string is converted with ``str``.
    """
    if max_chars is not None and max_chars < 0:
        raise ValueError("max_chars must not be negative")
    if raw is None:
        return Cleaned("")
    text = raw if isinstance(raw, str) else str(raw)
    changed = max(0, len(text) - _PRECUT_CHARS)
    text = text[:_PRECUT_CHARS]
    plain = strip_ansi(text)
    changed += len(text) - len(plain)

    kept: list[str] = []
    run: list[str] = []  # white space waiting for the next visible character
    for char in plain:
        if char.isspace():
            run.append(char)
        elif char in STRUCTURAL_CHARS or unicodedata.category(char) in _DROPPED_CATEGORIES:
            changed += 1  # dropped characters do not interrupt a white space run
        else:
            if run and kept:  # white space before the first visible character is trimmed
                kept.append(" ")
                changed += _whitespace_cost("".join(run))
            run.clear()
            kept.append(char)
    result = "".join(kept)  # trailing white space was never flushed

    if max_chars is not None and len(result) > max_chars:
        changed += len(result) - max_chars
        result = result[:max_chars].rstrip(" ")
    return Cleaned(result, changed)


def clean_say(raw: object, max_chars: int = SAY_MAX_CHARS) -> Cleaned:
    """A chat line for the public channel."""
    return clean_text(raw, max_chars)


def clean_thought(raw: object, max_chars: int = THOUGHT_MAX_CHARS) -> Cleaned:
    """An inner monologue."""
    return clean_text(raw, max_chars)


def clean_persona(raw: object, max_chars: int = PERSONA_MAX_CHARS) -> Cleaned:
    """The persona line of an opening plan."""
    return clean_text(raw, max_chars)


def clean_label(raw: object, max_chars: int = LABEL_MAX_CHARS) -> Cleaned:
    """A short name that is typed into a template, for example a code name."""
    return clean_text(raw, max_chars)


def quote_literal(raw: object, max_chars: int | None = None) -> str:
    """Clean text as a double-quoted JSON string literal, for use inside an observation.

    The literal escapes the quote character and the backslash, so a quoted line cannot close
    its own quotation. Non-ASCII characters are written as they are.
    """
    return json.dumps(clean_text(raw, max_chars).text, ensure_ascii=False)


def has_structure(text: str) -> bool:
    """True when ``text`` holds a structural character, a control character or an escape."""
    if _ANSI.search(text):
        return True
    return any(
        char in STRUCTURAL_CHARS or unicodedata.category(char) in _DROPPED_CATEGORIES
        for char in text
    )
