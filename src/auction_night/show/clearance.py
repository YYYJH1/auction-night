"""Releasing findings of the content gate by hand.

The gate refuses a night when it finds anything, and its classifier can be wrong in both
directions. A person who has read a finding and decided that the text may be published says so in
a clearance file, and the export goes on. This module reads that file and applies it to the report
of a scan.

The file
--------
A TOML file with one ``[[clear]]`` table for each finding::

    [[clear]]
    path = "host.per_lot[0].text"
    category = "real_person"
    reason = "「鲸落」是本节目的座位代号，句中没有指向真实人物的其他线索。"

``path`` is the place as the report of the gate writes it. ``category`` is the category of the
classifier (:data:`~auction_night.show.content_scan.CATEGORIES`), or ``banned_word`` or
``wordlist`` for a finding of a word list. ``reason`` is the reason of the person, one line. A
table clears every finding of that category at that place.

What is checked
---------------
* The file must be readable TOML. Each table has exactly the keys ``path``, ``category`` and
  ``reason``, the category is a known one, the reason is one line of at most 300 characters, and
  a place is listed once for each category. Otherwise the file is a :class:`ClearanceError`.
* Every table must match a finding that the scan of this night reported. A table that matches
  none is a :class:`ClearanceError` too, so a file written for other texts or for another
  recording of the classifier is refused whole and cannot release anything.
* A finding of a batch that the classifier could not read has no category and cannot be released,
  because nobody has read its texts. The scan has to be recorded again.
* A night is exported only when no finding is left. The findings that were released are written
  to ``manifest.cleared_findings`` of the replay, with what the gate said and the reason of the
  person.
"""

import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from auction_night.contracts.replay import ClearedFinding
from auction_night.show.content_scan import CLEARABLE, ScanReport

__all__ = [
    "MAX_REASON_CHARS",
    "Clearance",
    "ClearanceError",
    "load_clearances",
    "release",
]

MAX_REASON_CHARS: Final = 300
_KEYS: Final = ("path", "category", "reason")
_SHOWN: Final = 10


class ClearanceError(ValueError):
    """The clearance cannot be used: the file is wrong, or it lists a finding that this night's
    scan did not report. Nothing is released."""


@dataclass(frozen=True, slots=True)
class Clearance:
    """One table of the file: a place, a category and the reason of the person."""

    path: str
    category: str
    reason: str


def _table(number: int, name: str, table: object) -> Clearance:
    where = f"{name}, [[clear]] table {number}"
    if not isinstance(table, dict):
        raise ClearanceError(f"{where}: it is not a table")
    for key in _KEYS:
        if key not in table:
            raise ClearanceError(f"{where}: {key} is missing")
    for key in table:
        if key not in _KEYS:
            raise ClearanceError(f"{where}: unknown key {key!r}")
    for key in _KEYS:
        if not isinstance(table[key], str):
            raise ClearanceError(f"{where}: {key} is not a string")
    path, category, reason = (table[key].strip() for key in _KEYS)
    try:
        ClearedFinding(path=path, category=category, detail="", reason=reason)
    except ValidationError as exc:
        key = exc.errors()[0]["loc"][0]
        raise ClearanceError(
            f"{where}: {key} must be one line of text, not empty and not too long"
        ) from None
    if len(reason) > MAX_REASON_CHARS:
        raise ClearanceError(f"{where}: reason has more than {MAX_REASON_CHARS} characters")
    if category == "classifier_failed":
        raise ClearanceError(
            f"{where}: a batch that could not be classified cannot be cleared, "
            "because nobody has read its texts. Record the scan again"
        )
    if category not in CLEARABLE:
        raise ClearanceError(f"{where}: category {category!r} is none of {', '.join(CLEARABLE)}")
    return Clearance(path, category, reason)


def load_clearances(path: Path) -> tuple[Clearance, ...]:
    """The tables of a clearance file, in the order of the file.

    Raises :class:`ClearanceError` for a file that cannot be read or is not what the module text
    describes.
    """
    name = path.name
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ClearanceError(
            f"cannot read the clearance file {name}: {exc.strerror or exc}"
        ) from None
    except UnicodeError:
        raise ClearanceError(f"the clearance file {name} is not UTF-8 text") from None
    except tomllib.TOMLDecodeError as exc:
        raise ClearanceError(f"the clearance file {name} is not valid TOML: {exc}") from None
    for key in data:
        if key != "clear":
            raise ClearanceError(f"{name}: unknown key {key!r}, the file holds [[clear]] tables")
    tables = data.get("clear")
    if not isinstance(tables, list) or not tables:
        raise ClearanceError(f"{name}: expected one [[clear]] table for each finding, found none")
    found: list[Clearance] = []
    seen: dict[tuple[str, str], int] = {}
    for number, table in enumerate(tables, start=1):
        entry = _table(number, name, table)
        key = (entry.path, entry.category)
        if key in seen:
            raise ClearanceError(
                f"{name}, [[clear]] table {number}: {entry.path} ({entry.category}) "
                f"is already in table {seen[key]}"
            )
        seen[key] = number
        found.append(entry)
    return tuple(found)


def release(
    report: ScanReport, clearances: Sequence[Clearance]
) -> tuple[ScanReport, tuple[ClearedFinding, ...]]:
    """Apply a clearance file to the report of a scan.

    Returns the report with the released findings moved from ``findings`` to ``cleared``, and one
    :class:`~auction_night.contracts.replay.ClearedFinding` for each of them, in the order of the
    report. Raises :class:`ClearanceError` when a table matches no finding of the report.
    """
    reported = {(finding.path, finding.category) for finding in report.findings if finding.category}
    stale = [entry for entry in clearances if (entry.path, entry.category) not in reported]
    if stale:
        listed = ", ".join(f"{entry.path} ({entry.category})" for entry in stale[:_SHOWN])
        more = f" and {len(stale) - _SHOWN} more" if len(stale) > _SHOWN else ""
        raise ClearanceError(
            f"the clearance file lists {len(stale)} finding(s) that the scan of this night "
            f"did not report: {listed}{more}"
        )
    reasons = {(entry.path, entry.category): entry.reason for entry in clearances}
    standing, released, records = [], [], []
    for finding in report.findings:
        reason = reasons.get((finding.path, finding.category)) if finding.category else None
        if reason is None:
            standing.append(finding)
            continue
        released.append(finding)
        records.append(
            ClearedFinding(
                path=finding.path, category=finding.category, detail=finding.note, reason=reason
            )
        )
    updated = replace(report, findings=tuple(standing), cleared=(*report.cleared, *released))
    return updated, tuple(records)
