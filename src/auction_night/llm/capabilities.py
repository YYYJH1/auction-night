"""What an endpoint and a model accept, and the request parameters that follow from it.

A configuration names a *capability record* for every model (or falls back to the defaults of
the endpoint kind). The request builders never send what the record does not allow:

* sampling parameters go out only when the record supports them;
* thinking and effort must be pinned explicitly wherever the record lists modes or efforts;
* a non-adaptive thinking mode is limited to the effort the record permits;
* ``tool_choice`` other than ``auto`` needs forced tool choice, and strict tools need strict
  support.

:func:`request_problems` lists every conflict between a seat or role and its record. The
configuration contract rejects the same combinations when it is validated, so for a validated
configuration the list is empty. The builders check again, because a request can also be built
from objects that were never validated together.

``DOCUMENTED_CLAUDE_RECORDS`` states what the API documentation says about the two Claude 5.5
models. It is not used to build a request: the record in the configuration is. It exists so that
a test can compare the shipped example configuration with the documented facts, which keeps the
records honest when a model changes. Re-check it against the documentation whenever one does.
"""

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, Final

from auction_night.contracts.config import (
    EFFORTS,
    Capabilities,
    EndpointConfig,
    ModelFields,
    ThinkingConfig,
)

_SAMPLING_FIELDS: Final = ("temperature", "top_p", "top_k", "seed")

DOCUMENTED_CLAUDE_RECORDS: Final[Mapping[str, Capabilities]] = MappingProxyType(
    {
        # Sampling parameters answer HTTP 400 (the newest API version rejects them on every
        # model). Forced tool choice is accepted, but the reply then has no thinking block.
        # Thinking can be switched off up to effort "high".
        "claude-haiku-5-5": Capabilities(
            supports_sampling_params=False,
            supports_forced_tool_choice=True,
            supports_strict_tools=True,
            thinking_modes=("adaptive", "disabled"),
            efforts=EFFORTS,
            explicit_thinking_max_effort="high",
        ),
        # Forced tool choice answers HTTP 400. "disabled" is rejected: the lowest thinking
        # setting is "between_tools", accepted up to effort "high".
        "claude-sonnet-5-5": Capabilities(
            supports_sampling_params=False,
            supports_forced_tool_choice=False,
            supports_strict_tools=True,
            thinking_modes=("adaptive", "between_tools"),
            efforts=EFFORTS,
            explicit_thinking_max_effort="high",
        ),
    }
)


class CapabilityError(ValueError):
    """A request cannot be built because the configuration conflicts with the capability record.

    ``problems`` holds one sentence per conflict.
    """

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("; ".join(self.problems))


def record_conflicts(model: str, record: Capabilities) -> list[str]:
    """Differences between a configured record and the documented one for the same model.

    The list is empty for a model without a documented record.
    """
    documented = DOCUMENTED_CLAUDE_RECORDS.get(model)
    if documented is None:
        return []
    differences = []
    for name in type(documented).model_fields:
        configured, expected = getattr(record, name), getattr(documented, name)
        if configured != expected:
            differences.append(
                f"{model}: {name} is {configured!r} in the record, the documentation says "
                f"{expected!r}"
            )
    return differences


def merged_sampling(endpoint: EndpointConfig, ref: ModelFields) -> dict[str, Any]:
    """Sampling parameters that are set: the endpoint default, overridden by the seat."""
    merged: dict[str, Any] = {}
    for source in (endpoint.sampling, ref.sampling):
        if source is None:
            continue
        for name in _SAMPLING_FIELDS:
            value = getattr(source, name)
            if value is not None:
                merged[name] = value
    return merged


def _thinking_problems(
    where: str, endpoint: EndpointConfig, ref: ModelFields, caps: Capabilities
) -> list[str]:
    problems = []
    thinking: ThinkingConfig | None = ref.thinking
    if not caps.thinking_modes:
        if thinking is not None:
            problems.append(f"{where}: thinking cannot be controlled on this model")
    elif thinking is None:
        problems.append(f"{where}: thinking must be pinned explicitly")
    elif thinking.type not in caps.thinking_modes:
        problems.append(f"{where}: thinking type {thinking.type!r} is not supported")
    elif thinking.type != "adaptive" and caps.explicit_thinking_max_effort is not None:
        limit = EFFORTS.index(caps.explicit_thinking_max_effort)
        if ref.effort is None or EFFORTS.index(ref.effort) > limit:
            problems.append(
                f"{where}: thinking {thinking.type!r} needs effort up to "
                f"{caps.explicit_thinking_max_effort!r}"
            )
    if thinking is not None and thinking.type == "between_tools" and endpoint.kind != "anthropic":
        problems.append(f"{where}: thinking 'between_tools' exists only on Anthropic endpoints")
    if not caps.efforts:
        if ref.effort is not None:
            problems.append(f"{where}: effort cannot be set on this model")
    elif ref.effort is None:
        problems.append(f"{where}: effort must be pinned explicitly")
    elif ref.effort not in caps.efforts:
        problems.append(f"{where}: effort {ref.effort!r} is not supported")
    return problems


def request_problems(endpoint: EndpointConfig, ref: ModelFields) -> list[str]:
    """Every conflict between a seat or role and the record of its model on this endpoint."""
    where = f"{ref.endpoint or 'endpoint'}/{ref.model or '?'}"
    if ref.model is None:
        return [f"{where}: a model id is required"]
    caps = endpoint.capabilities_for(ref.model)
    problems = []
    if merged_sampling(endpoint, ref) and not caps.supports_sampling_params:
        problems.append(f"{where}: sampling parameters are not supported")
    if endpoint.kind == "anthropic" and ref.max_tokens is None:
        problems.append(f"{where}: max_tokens is required")
    problems += _thinking_problems(where, endpoint, ref, caps)
    if endpoint.tool_choice != "auto" and not caps.supports_forced_tool_choice:
        problems.append(f"{where}: forced tool_choice is not supported")
    if endpoint.strict_tools and not caps.supports_strict_tools:
        problems.append(f"{where}: strict tools are not supported")
    if endpoint.structured_output != "tools":
        problems.append(
            f"{where}: structured_output {endpoint.structured_output!r} is not built here, "
            "only 'tools' is"
        )
    return problems


def ensure_buildable(endpoint: EndpointConfig, ref: ModelFields) -> Capabilities:
    """The record that applies, or :class:`CapabilityError` when the request would be refused."""
    problems = request_problems(endpoint, ref)
    if problems:
        raise CapabilityError(problems)
    return endpoint.capabilities_for(ref.model)


def anthropic_thinking(thinking: ThinkingConfig | None) -> dict[str, str] | None:
    """The ``thinking`` request field of a pinned mode, or None when the field is left out.

    ``adaptive`` is the default behaviour of the models, so it is pinned in the configuration
    (and in the request hash) but not sent. Only ``disabled`` and ``between_tools`` are.
    """
    if thinking is None or thinking.type == "adaptive":
        return None
    return {"type": thinking.type}
