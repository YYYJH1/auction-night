"""Prompt templates.

The Chinese texts live in ``prompts/zh/*.toml`` as package data, one literal TOML string per
template. This module loads them into an immutable :class:`TemplateSet`. A template is
addressed as ``<file>.<key>``, for example ``system.intro``. The keys of ``phase.toml`` match
the instruction keys of the phase table in the act contract (``phase.plan``, ``phase.talk``,
and so on).

Placeholders are written ``{name}`` and filled with :meth:`TemplateSet.format`, which fails on a
missing or surplus value instead of leaving a hole in a prompt. A value is inserted as it is: it
is never parsed as a template itself, so braces inside a value cannot reach the formatter.

:func:`templates_digest` is the SHA-256 of the texts in force. The run manifest records it, so a
changed template always shows up there. Some texts exist for an optional setting of the night
(:data:`OPTIONAL_GROUPS`). They are in the digest only when the night uses that setting, so the
digest of a night without it is the one it had before the texts were written.
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

# Optional settings of the night that bring their own texts: the name of the group, and the prefix
# of the template keys that belong to it (``night.memory`` and ``observe.memory_*``).
OPTIONAL_GROUPS: Final[Mapping[str, str]] = MappingProxyType({"memory": "observe.memory_"})


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

    def digest_with(self, *groups: str) -> str:
        """``sha256:<hex>`` over the language, the version label and the texts in force.

        The texts of an optional group (:data:`OPTIONAL_GROUPS`) count only when the group is
        named here. Without any group the digest covers the texts that every night uses.
        """
        unknown = [name for name in groups if name not in OPTIONAL_GROUPS]
        if unknown:
            raise TemplateError(f"no optional group {unknown[0]!r}")
        unused = tuple(prefix for name, prefix in OPTIONAL_GROUPS.items() if name not in groups)
        texts = {key: text for key, text in self.texts.items() if not key.startswith(unused)}
        body = {"lang": self.lang, "version": self.version, "texts": texts}
        return sha256_digest(canonical_json(body))

    @property
    def digest(self) -> str:
        """The digest of the texts that every night uses: :meth:`digest_with` without a group."""
        return self.digest_with()


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


def templates_digest(lang: str = DEFAULT_LANG, *groups: str) -> str:
    """Digest of the templates of one language that a night uses (recorded in the run manifest).

    ``groups`` name the optional settings that the night has turned on. See
    :meth:`TemplateSet.digest_with`.
    """
    return load_templates(lang).digest_with(*groups)
