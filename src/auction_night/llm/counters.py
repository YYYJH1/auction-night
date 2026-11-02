"""Counters of the parsing ladder: what was repaired, what was legal, what was invalid.

Three things must not be mixed up when the health of a model or an endpoint is judged:

* **Legal adjustments** are not mistakes. The rules say that a bid above the remaining budget is
  counted as the budget, and that an English revision below the current price means leaving.
  They are counted as ``legal_clamps`` and never lower the rule compliance.
* **Invalid input** is a reply that broke the protocol: a missing field, a value out of range,
  the wrong phase, an inconsistent pledge. It is the only thing that lowers the compliance:
  ``rule_compliance = 1 - invalid_inputs / actions``.
* **Syntax repairs** are measured against the native mode of the endpoint. An endpoint whose
  native mode is the tool call is expected to answer with a tool call, so JSON found in plain
  text costs a repair there; an endpoint that is expected to write JSON as text pays nothing for
  fences or surrounding prose. Fixing the JSON itself (full-width punctuation, trailing commas)
  is a repair in every mode.

An *action* is one thing a seat was asked to do. It ends in exactly one outcome: valid (``ok``)
or an abstention with a reason. A reply that only leads to a retry is an attempt, not an action.
Abstentions that are refusals are counted apart from the other abstentions, so that the two
rates do not overlap.
"""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from auction_night.contracts.config import StructuredOutput
from auction_night.contracts.events import AbstainReason, ParseRepairData, RepairCode
from auction_night.llm.parse import ParseResult, ParseTrace

_CODE_ORDER: tuple[RepairCode, ...] = (
    "syntax",
    "coerce",
    "clamp",
    "invalid_input",
    "retry",
    "fallback",
    "refusal",
    "sanitize",
)


def syntax_kinds(trace: ParseTrace, native: StructuredOutput) -> tuple[str, ...]:
    """The syntax repairs of an attempt, measured against the native mode of the endpoint."""
    kinds = list(trace.syntax_fixes)
    if trace.unwrapped:
        kinds.append("wrapper")
    if trace.source == "text":
        if native == "tools":
            kinds.append("not_a_tool_call")
        if native in ("tools", "json_schema"):
            if trace.fenced:
                kinds.append("fenced")
            if trace.prose:
                kinds.append("prose")
    return tuple(kinds)


def coerce_kinds(trace: ParseTrace) -> tuple[str, ...]:
    """Kinds of coercion in an attempt, including fields the ladder filled in itself."""
    return tuple(c.kind for c in trace.coercions) + tuple(f"filled_{name}" for name in trace.filled)


def repair_codes(result: ParseResult, native: StructuredOutput) -> tuple[RepairCode, ...]:
    """The repair codes of one attempt, in the fixed order of the event contract."""
    present: set[str] = set()
    if syntax_kinds(result.trace, native):
        present.add("syntax")
    if coerce_kinds(result.trace):
        present.add("coerce")
    if result.adjustments:
        present.add("clamp")
    if result.errors:
        present.add("invalid_input")
    if result.status == "retry":
        present.add("retry")
    if result.status == "abstain":
        present.add("refusal" if result.reason == "refusal" else "fallback")
    if result.sanitized_chars:
        present.add("sanitize")
    return tuple(code for code in _CODE_ORDER if code in present)


def repair_fields(result: ParseResult) -> tuple[str, ...]:
    """Names of the fields that were coerced, filled, clamped, cleaned or found invalid."""
    names = [c.field for c in result.trace.coercions]
    names += result.trace.filled
    names += [a.field for a in result.adjustments]
    names += [e.field for e in result.errors]
    names += result.sanitized_fields
    return tuple(dict.fromkeys(names))


def repair_detail(result: ParseResult, native: StructuredOutput) -> str:
    """One line for the repair event: what was done, in a fixed order."""
    parts = []
    if kinds := syntax_kinds(result.trace, native):
        parts.append("syntax=" + ",".join(kinds))
    if result.trace.coercions or result.trace.filled:
        items = [f"{c.field}:{c.kind}" for c in result.trace.coercions]
        items += [f"{name}:filled" for name in result.trace.filled]
        parts.append("coerce=" + ",".join(items))
    if result.adjustments:
        items = [f"{a.field}:{a.kind}:{a.raw}->{a.effective}" for a in result.adjustments]
        parts.append("legal=" + ",".join(items))
    if result.errors:
        parts.append("invalid=" + ",".join(f"{e.field}:{e.code}" for e in result.errors))
    if result.sanitized_chars:
        parts.append(f"sanitized={result.sanitized_chars}")
    if result.reason:
        parts.append(f"reason={result.reason}")
    return "; ".join(parts)


def repair_data(
    result: ParseResult,
    native: StructuredOutput,
    *,
    role: str,
    seat: int | None = None,
    call_id: str | None = None,
) -> ParseRepairData | None:
    """The payload of the ``parse_repair`` event of one attempt, or None when nothing happened.

    ``status`` is how the attempt ended: ``ok``, ``retry`` or ``abstain``.
    """
    codes = repair_codes(result, native)
    if not codes:
        return None
    return ParseRepairData(
        role=role,
        seat=seat,
        phase=result.phase,
        attempt=result.attempt,
        codes=list(codes),
        status=result.status,
        fields=list(repair_fields(result)),
        sanitized_chars=result.sanitized_chars,
        detail=repair_detail(result, native),
        call_id=call_id,
    )


@dataclass
class ParseCounters:
    """Counts over any number of attempts and actions (one seat, one endpoint, one night)."""

    attempts: int = 0
    actions: int = 0
    ok: int = 0
    kept_previous: int = 0
    not_asked: int = 0
    retries: int = 0
    invalid_inputs: int = 0
    syntax_attempts: int = 0
    coerce_attempts: int = 0
    sanitize_attempts: int = 0
    sanitized_chars: int = 0
    abstentions: Counter[str] = field(default_factory=Counter)
    legal_clamps: Counter[str] = field(default_factory=Counter)
    invalid_by_code: Counter[str] = field(default_factory=Counter)
    syntax_by_kind: Counter[str] = field(default_factory=Counter)
    coerce_by_kind: Counter[str] = field(default_factory=Counter)

    def record(self, result: ParseResult, native: StructuredOutput) -> None:
        """Count one attempt, and the action it ended if it was the last one."""
        self.attempts += 1
        if kinds := syntax_kinds(result.trace, native):
            self.syntax_attempts += 1
            self.syntax_by_kind.update(kinds)
        if kinds := coerce_kinds(result.trace):
            self.coerce_attempts += 1
            self.coerce_by_kind.update(kinds)
        if result.sanitized_chars:
            self.sanitize_attempts += 1
            self.sanitized_chars += result.sanitized_chars
        if result.errors:
            self.invalid_inputs += 1
            self.invalid_by_code.update(error.code for error in result.errors)
        self.legal_clamps.update(adjustment.kind for adjustment in result.adjustments)
        if result.status == "retry":
            self.retries += 1
            return
        self.actions += 1
        if result.status == "ok":
            self.ok += 1
        else:
            assert result.reason is not None
            self.abstentions[result.reason] += 1
            if result.phase == "english_revise":
                self.kept_previous += 1

    def record_abstention(self, reason: AbstainReason) -> None:
        """Count an action that ended without any reply to parse (timeout, cost cap, ...).

        A seat that was never asked is not an action and only increments ``not_asked``.
        """
        if reason == "not_asked":
            self.not_asked += 1
            return
        self.actions += 1
        self.abstentions[reason] += 1

    def merge(self, other: "ParseCounters") -> None:
        """Add the counts of ``other`` to this one."""
        for name in (
            "attempts",
            "actions",
            "ok",
            "kept_previous",
            "not_asked",
            "retries",
            "invalid_inputs",
            "syntax_attempts",
            "coerce_attempts",
            "sanitize_attempts",
            "sanitized_chars",
        ):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        for name in ("abstentions", "legal_clamps", "invalid_by_code", "syntax_by_kind"):
            getattr(self, name).update(getattr(other, name))
        self.coerce_by_kind.update(other.coerce_by_kind)

    # ------------------------------------------------------------------ derived

    @property
    def refusals(self) -> int:
        """Actions that ended in a refusal."""
        return self.abstentions["refusal"]

    @property
    def other_abstentions(self) -> int:
        """Actions that ended in an abstention for any reason but refusal."""
        return sum(self.abstentions.values()) - self.refusals

    def _rate(self, count: int) -> float | None:
        return count / self.actions if self.actions else None

    @property
    def rule_compliance(self) -> float | None:
        """``1 - invalid_inputs / actions``; legal clamps do not count against it."""
        rate = self._rate(self.invalid_inputs)
        return None if rate is None else 1 - rate

    @property
    def abstain_rate(self) -> float | None:
        """Share of actions that ended in an abstention other than a refusal."""
        return self._rate(self.other_abstentions)

    @property
    def refusal_rate(self) -> float | None:
        """Share of actions that ended in a refusal."""
        return self._rate(self.refusals)

    @property
    def syntax_rate(self) -> float | None:
        """Share of attempts that needed a syntax repair (relative to the native mode)."""
        return self.syntax_attempts / self.attempts if self.attempts else None

    def rates(self) -> dict[str, float | None]:
        """The derived rates, by name."""
        return {
            "rule_compliance": self.rule_compliance,
            "abstain_rate": self.abstain_rate,
            "refusal_rate": self.refusal_rate,
            "syntax_rate": self.syntax_rate,
        }

    def to_dict(self) -> dict[str, Any]:
        """All counts as plain integers, with sorted keys (no rates, so no floats)."""
        out: dict[str, Any] = {}
        for name in (
            "attempts",
            "actions",
            "ok",
            "kept_previous",
            "not_asked",
            "retries",
            "invalid_inputs",
            "syntax_attempts",
            "coerce_attempts",
            "sanitize_attempts",
            "sanitized_chars",
        ):
            out[name] = getattr(self, name)
        for name in (
            "abstentions",
            "legal_clamps",
            "invalid_by_code",
            "syntax_by_kind",
            "coerce_by_kind",
        ):
            out[name] = dict(sorted(getattr(self, name).items()))
        return out


@dataclass
class CounterBook:
    """Counters by label, for example ``"seat:3"`` or an endpoint name."""

    by_label: dict[str, ParseCounters] = field(default_factory=dict)

    def get(self, label: str) -> ParseCounters:
        """The counters of one label, created on first use."""
        return self.by_label.setdefault(label, ParseCounters())

    def record(self, label: str, result: ParseResult, native: StructuredOutput) -> None:
        self.get(label).record(result, native)

    def total(self, labels: Iterable[str] | None = None) -> ParseCounters:
        """The sum over some labels, or over all of them."""
        total = ParseCounters()
        for label in self.by_label if labels is None else labels:
            if label in self.by_label:
                total.merge(self.by_label[label])
        return total

    def to_dict(self) -> dict[str, dict[str, Any]]:
        return {label: self.by_label[label].to_dict() for label in sorted(self.by_label)}
