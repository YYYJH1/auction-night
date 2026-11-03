"""Findings of the content gate that a rule releases, and the record of them.

The classifier is a model, and it can object to a text that the game itself makes. Two kinds of
objection are false by what the replay holds. A rule releases them after the classification, so
nobody has to write a clearance file for them:

* **A word of the game's own lists.** The ``text_value`` of a judge's extraction is, for a claim
  about a mission, one of a few words that the game defines: the kind of mission (``collector``,
  ``shill`` or ``none``) or a tag of the night. A model may take ``shill`` for a code word and
  ``collector`` for a brand. A ``text_value`` that belongs to the list of its topic is checked
  against that list and is not held back by an opinion of the classifier, whatever its category.
* **A code name.** The code names of the seats are invented, and the classifier is told so, but it
  can still take one for the name of a real person. A ``real_person`` finding whose reason names
  only code names of the night is released. The names are the ones in quotation marks in the
  reason (``「阿铁」``). Every one of them must be a code name of this night and must stand in the
  text that was flagged. A reason that names no one, or that names anyone besides the code names,
  is not released.

What is not touched
-------------------
The texts that the classifier reads, and the request it gets, are what they were, so a recording of
the classifier (``scan_calls.jsonl``) still fits. The rules work on the report of the scan, as
:func:`auction_night.show.clearance.release` does, and only on the findings of the classifier. A
finding of a word list, and a batch that could not be classified, stand.

What is recorded
----------------
A released finding is not dropped silently. It goes to ``manifest.auto_cleared_findings`` of the
replay with its place, its category, what the classifier said and the reason of the rule, apart
from ``manifest.cleared_findings``, which holds what a person released. A person's clearance file
is applied first, so a finding that the file lists is recorded as released by hand.
"""

import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Final

from auction_night.contracts.events import Extraction
from auction_night.contracts.replay import ClearedFinding, Replay
from auction_night.show.claims import MISSION_KIND_WORDS
from auction_night.show.content_scan import Finding, ScanReport, normalise, public_texts

__all__ = ["auto_release", "game_word", "names_quoted"]

# A name in quotation marks: corner brackets, curly or straight double quotes.
_QUOTED: Final = re.compile(r"「([^「」]+)」|『([^『』]+)』|“([^“”]+)”|\"([^\"]+)\"")


def game_word(extraction: Extraction, tags: frozenset[str]) -> str | None:
    """The list of the game that the ``text_value`` of a claim belongs to, or None.

    ``mission_kind`` for a claim about the kind of a mission whose value is ``collector``,
    ``shill`` or ``none`` (case and surrounding space aside, as the checking of claims reads it),
    and ``mission_tag`` for a claim about a tag whose value is a tag of the night.
    """
    value = (extraction.text_value or "").strip()
    if extraction.kind != "claim" or not value:
        return None
    if extraction.topic == "mission_kind" and value.casefold() in MISSION_KIND_WORDS:
        return "mission_kind"
    if extraction.topic == "mission_tag" and value in tags:
        return "mission_tag"
    return None


def names_quoted(reason: str) -> list[str]:
    """The names that a reason puts in quotation marks, once each, in the order they come."""
    found = (next(group for group in match.groups() if group) for match in _QUOTED.finditer(reason))
    return list(dict.fromkeys(name.strip() for name in found if name.strip()))


def _word_reason(
    finding: Finding, extractions: Mapping[str, Extraction], tags: frozenset[str]
) -> str | None:
    extraction = extractions.get(finding.path)
    if finding.rule != "classifier" or extraction is None:
        return None
    kind = game_word(extraction, tags)
    value = (extraction.text_value or "").strip()
    if kind == "mission_kind":
        return f"text_value 是任务类型的取值「{value}」，属于游戏枚举，按枚举校验通过。"
    if kind == "mission_tag":
        return f"text_value 是本夜拍品的标签「{value}」，属于游戏枚举，按枚举校验通过。"
    return None


def _name_reason(finding: Finding, text: str | None, codenames: frozenset[str]) -> str | None:
    if finding.rule != "classifier" or finding.category != "real_person" or text is None:
        return None
    names = names_quoted(finding.note)
    if not names:
        return None
    flagged = normalise(text)
    for name in names:
        if normalise(name) not in codenames or normalise(name) not in flagged:
            return None
    listed = "".join(f"「{name}」" for name in names)
    return f"分类器指出的名字{listed}只是本夜的座位代号。"


def auto_release(
    replay: Replay, report: ScanReport
) -> tuple[ScanReport, tuple[ClearedFinding, ...]]:
    """Apply the rules of the module text to the report of a scan.

    Returns the report with the released findings moved from ``findings`` to ``auto_cleared``,
    and one :class:`~auction_night.contracts.replay.ClearedFinding` for each of them, in the order
    of the report. ``replay`` is the replay that the scan read: the rules look the flagged texts
    and extractions up in it by the path of the finding.
    """
    codenames = frozenset(normalise(seat.codename) for seat in replay.seats)
    tags = frozenset(lot.tag for lot in replay.catalog)
    extractions = {
        f"judges[{i}].extractions[{j}].text_value": extraction
        for i, judge in enumerate(replay.judges)
        for j, extraction in enumerate(judge.extractions)
    }
    texts = {item.path: item.text for item in public_texts(replay)}
    standing, released, records = [], [], []
    for finding in report.findings:
        reason = _word_reason(finding, extractions, tags) or _name_reason(
            finding, texts.get(finding.path), codenames
        )
        if reason is None:
            standing.append(finding)
            continue
        released.append(finding)
        records.append(
            ClearedFinding(
                path=finding.path, category=finding.category, detail=finding.note, reason=reason
            )
        )
    updated = replace(
        report, findings=tuple(standing), auto_cleared=(*report.auto_cleared, *released)
    )
    return updated, tuple(records)
