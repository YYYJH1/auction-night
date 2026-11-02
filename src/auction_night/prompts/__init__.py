"""Prompt templates.

The Chinese texts live in ``prompts/zh/*.toml`` as package data, one literal TOML string per
template. This module loads them into an immutable :class:`TemplateSet`. A template is
addressed as ``<file>.<key>``, for example ``system.intro``. The keys of ``phase.toml`` match
the instruction keys of the phase table in the act contract (``phase.plan``, ``phase.talk``,
and so on).

Placeholders are written ``{name}`` and filled with :meth:`TemplateSet.format`, which fails on a
missing or surplus value instead of leaving a hole in a prompt. A value is inserted as it is: it
is never parsed as a template itself, so braces inside a value cannot reach the formatter.

:func:`templates_digest` is the SHA-256 of all texts. The run manifest records it, so a changed
template always shows up there.
"""

import functools
import string
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from types import MappingProxyType
from typing import Any, Final

from auction_night.contracts.events import canonical_json, sha256_digest

DEFAULT_LANG: Final = "zh"
_FILES: Final = ("system", "observe", "phase", "result")


class TemplateError(ValueError):
    """A template is missing, malformed or filled with the wrong values."""


@dataclass(frozen=True)
class TemplateSet:
    """All templates of one language."""

    lang: str
    version: str
    texts: Mapping[str, str]

    def get(self, key: str) -> str:
        """The raw text of a template."""
        try:
            return self.texts[key]
        except KeyError:
            raise TemplateError(f"no template {key!r}") from None

    def format(self, key: str, /, **values: object) -> str:
        """Fill the placeholders of a template. The values must match the placeholders."""
        text = self.get(key)
        wanted = {name for _, name, _, _ in string.Formatter().parse(text) if name}
        if wanted != set(values):
            missing = sorted(wanted - set(values))
            extra = sorted(set(values) - wanted)
            raise TemplateError(f"template {key!r}: missing {missing}, unexpected {extra}")
        return text.format_map({name: str(value) for name, value in values.items()})

    @property
    def digest(self) -> str:
        """``sha256:<hex>`` over the language, the version label and every text."""
        body = {"lang": self.lang, "version": self.version, "texts": dict(self.texts)}
        return sha256_digest(canonical_json(body))


def _read_table(lang: str, stem: str) -> dict[str, Any]:
    path = resources.files(__package__).joinpath(lang, f"{stem}.toml")
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise TemplateError(f"missing template file {lang}/{stem}.toml") from None
    except tomllib.TOMLDecodeError as exc:
        raise TemplateError(f"{lang}/{stem}.toml: {exc}") from exc


@functools.cache
def load_templates(lang: str = DEFAULT_LANG) -> TemplateSet:
    """Read and validate the templates of one language. The result is cached."""
    meta = _read_table(lang, "meta")
    texts: dict[str, str] = {}
    for stem in _FILES:
        for key, value in _read_table(lang, stem).items():
            if not isinstance(value, str):
                raise TemplateError(f"{lang}/{stem}.toml: {key!r} must be a string")
            texts[f"{stem}.{key}"] = value.strip("\n")
    return TemplateSet(
        lang=lang, version=str(meta.get("version", "")), texts=MappingProxyType(texts)
    )


def templates_digest(lang: str = DEFAULT_LANG) -> str:
    """Digest of all templates of one language (recorded in the run manifest)."""
    return load_templates(lang).digest
