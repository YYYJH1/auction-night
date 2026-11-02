"""Thin wrappers around the vendor SDKs, and the normalised reply they return.

One call is one ``messages.create`` (Anthropic) or ``chat.completions.create`` (OpenAI-compatible
servers) with the keyword arguments that :mod:`auction_night.llm.request` built. The wrapper adds
nothing to the payload but the call timeout, and turns the SDK object into a
:class:`ClientReply`:

* ``reply`` is the :class:`~auction_night.llm.parse.RawReply` that the parsing ladder reads, with
  the refusal flag and the stop reason;
* ``usage`` counts the tokens. Cache reads and cache writes are kept apart from the plain input,
  and the reasoning tokens that the provider reports are a part of ``output_tokens`` (both APIs
  count them in the output total, which is what is billed), so they are never added a second
  time;
* ``latency_ms`` is the wall time of the SDK call;
* ``response`` is the response as plain JSON with secrets scrubbed, and the reply is read from
  this very object. The cassette stores it, so a live call and a replay hand the ladder the same
  bytes.

API keys are read from the environment variable that the endpoint configuration names, when the
client is created. They never leave this module: they are not stored in a reply, not shown by
``repr`` and not part of an error message. Anything that looks like a key, a bearer token or a
credential header, and every exact copy of a configured key, is replaced by ``[REDACTED]`` before
a response or an error message is kept.

SDK failures are not wrapped here. The exception travels up and :func:`classify_exception` turns
it into a :class:`CallFailure` that says whether the call timed out and whether another try can
help: rate limits, server errors and lost connections can, a request the API refuses (the 400
family) cannot. The wrappers switch the SDKs' own retries off, because the retry policy and the
cassette must see every attempt.
"""

import functools
import json
import os
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from types import SimpleNamespace
from typing import Any, Final, Literal, Protocol

from auction_night.contracts.config import EndpointConfig, EndpointKind, NightConfig
from auction_night.llm.parse import RawReply, reply_from_anthropic, reply_from_openai
from auction_night.llm.request import BuiltRequest

REDACTED: Final = "[REDACTED]"
# The OpenAI SDK insists on a key string even when a local server does not check it.
_NO_KEY: Final = "none"
_MAX_MESSAGE_CHARS: Final = 300

# ------------------------------------------------------------------------------ scrubbing

# The patterns are written so that this file does not look like it holds a key.
_KEY_SHAPES: Final = (
    re.compile(r"(?<![A-Za-z0-9])sk-an[t]-[A-Za-z0-9_*-]{4,}"),
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9][A-Za-z0-9_*-]{7,}"),
    re.compile(r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"(?<![\w-])AIza[\w-]{30,}"),
    re.compile(r"(?<![A-Z0-9])AKIA[0-9A-Z]{16}(?![A-Z0-9])"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
)
# A credential header or assignment keeps its name and loses its value.
_NAMED_CREDENTIAL: Final = re.compile(
    r"(?i)((?:x-api-key|api[_-]?key|authorization|auth[_-]?token|access[_-]?token"
    r"|secret[_-]?key)[\"']?\s*[:=]\s*[\"']?(?:(?:basic|bearer|token|digest)\s+)?)"
    r"[^\s\"',;]{4,}"
)
_CREDENTIAL_KEYS: Final = re.compile(
    r"(?i)^(?:authorization|proxy-authorization|x-api-key|api[_-]?key|cookie|set-cookie)$"
)
_MIN_SECRET_CHARS: Final = 6


def _named_credential(match: re.Match[str]) -> str:
    return match.group(0) if match.group(0).endswith(REDACTED) else match.group(1) + REDACTED


class Scrubber:
    """Replaces key-like text, and the exact keys it was given, by ``[REDACTED]``.

    The secrets are kept privately: ``repr`` only counts them, so a scrubber can be logged.
    """

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets = tuple(
            sorted(
                {s for s in secrets if isinstance(s, str) and len(s) >= _MIN_SECRET_CHARS},
                key=len,
                reverse=True,
            )
        )

    def __repr__(self) -> str:
        return f"Scrubber({len(self._secrets)} secrets)"

    def merged(self, *others: "Scrubber") -> "Scrubber":
        """A scrubber that knows the secrets of all of them."""
        combined = list(self._secrets)
        for other in others:
            combined.extend(other._secrets)
        return Scrubber(combined)

    def text(self, value: str) -> str:
        """``value`` with every exact secret and every key-shaped substring replaced."""
        for secret in self._secrets:
            value = value.replace(secret, REDACTED)
        for shape in _KEY_SHAPES:
            value = shape.sub(REDACTED, value)
        return _NAMED_CREDENTIAL.sub(_named_credential, value)

    def json(self, obj: Any) -> Any:
        """A copy of a JSON-like structure with every string (keys included) scrubbed.

        Mapping entries whose key names a credential header (``authorization``, ``x-api-key``,
        ``cookie`` and the like) are dropped altogether.
        """
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, Mapping):
            return {
                self.text(str(key)): self.json(value)
                for key, value in obj.items()
                if not _CREDENTIAL_KEYS.match(str(key))
            }
        if isinstance(obj, list | tuple):
            return [self.json(value) for value in obj]
        return obj


_DEFAULT_SCRUBBER: Final = Scrubber()


def scrub_text(value: str) -> str:
    """:meth:`Scrubber.text` without any exact secret."""
    return _DEFAULT_SCRUBBER.text(value)


def scrub_json(obj: Any) -> Any:
    """:meth:`Scrubber.json` without any exact secret."""
    return _DEFAULT_SCRUBBER.json(obj)


# ------------------------------------------------------------------------------- the reply


@dataclass(frozen=True)
class Usage:
    """Tokens of one call, normalised over the vendors.

    ``input_tokens`` are the prompt tokens that were neither read from nor written to a cache,
    so a call's whole prompt is ``input_tokens + cache_read_input_tokens +
    cache_write_input_tokens``. ``output_tokens`` is the billed output, reasoning included.
    ``reasoning_tokens`` says how many of them were reasoning, or None when the provider does not
    say.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    reasoning_tokens: int | None = None

    def __add__(self, other: "Usage") -> "Usage":
        reasoning = None
        if self.reasoning_tokens is not None or other.reasoning_tokens is not None:
            reasoning = (self.reasoning_tokens or 0) + (other.reasoning_tokens or 0)
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_input_tokens + other.cache_read_input_tokens,
            self.cache_write_input_tokens + other.cache_write_input_tokens,
            reasoning,
        )

    @property
    def prompt_tokens(self) -> int:
        """The whole prompt: new, cache-read and cache-written tokens."""
        return self.input_tokens + self.cache_read_input_tokens + self.cache_write_input_tokens

    def as_dict(self) -> dict[str, int | None]:
        """The fields in a fixed order, for the cassette."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Usage":
        """Read back what :meth:`as_dict` wrote."""
        reasoning = data.get("reasoning_tokens")
        return cls(
            input_tokens=_count(data.get("input_tokens")),
            output_tokens=_count(data.get("output_tokens")),
            cache_read_input_tokens=_count(data.get("cache_read_input_tokens")),
            cache_write_input_tokens=_count(data.get("cache_write_input_tokens")),
            reasoning_tokens=None if reasoning is None else _count(reasoning),
        )


@dataclass(frozen=True)
class ClientReply:
    """The outcome of one successful SDK call.

    ``usage`` is None when the provider sent no usage block (the cost of the call is then
    unknown). ``model`` is the model id the API returned, which is the one that counts when
    prices are looked up.
    """

    reply: RawReply
    usage: Usage | None
    latency_ms: int
    model: str | None = None
    response_id: str | None = None
    response: Mapping[str, Any] | None = None

    @property
    def refusal(self) -> bool:
        """True when the API itself declined to answer."""
        return self.reply.refusal

    @property
    def stop_reason(self) -> str | None:
        return self.reply.stop_reason


FailureKind = Literal["timeout", "error"]


@dataclass(frozen=True)
class CallFailure:
    """A call that produced no reply.

    ``kind`` is ``timeout`` for a call that ran out of time and ``error`` for everything else.
    ``retryable`` is True when another try of the same request may succeed (rate limit, server
    error, lost connection). ``message`` is scrubbed and cut short.
    """

    kind: FailureKind
    error_type: str
    message: str = ""
    status: int | None = None
    api_type: str | None = None
    retryable: bool = False
    retry_after_s: float | None = None

    def as_dict(self) -> dict[str, Any]:
        """The fields in a fixed order, for the cassette."""
        return {
            "kind": self.kind,
            "type": self.error_type,
            "status": self.status,
            "api_type": self.api_type,
            "retryable": self.retryable,
            "retry_after_s": self.retry_after_s,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CallFailure":
        """Read back what :meth:`as_dict` wrote."""
        kind = data.get("kind")
        retry_after = data.get("retry_after_s")
        return cls(
            kind="timeout" if kind == "timeout" else "error",
            error_type=str(data.get("type") or "Error"),
            message=str(data.get("message") or ""),
            status=data.get("status") if isinstance(data.get("status"), int) else None,
            api_type=data.get("api_type") if isinstance(data.get("api_type"), str) else None,
            retryable=bool(data.get("retryable")),
            retry_after_s=float(retry_after) if isinstance(retry_after, int | float) else None,
        )


class LlmClient(Protocol):
    """What the orchestrator needs from a client: one blocking call.

    Implementations raise the SDK's exception on failure. The orchestrator runs ``complete`` in
    a worker thread. A client may also have a ``scrubber`` (:class:`Scrubber`) that knows its
    API key; the cassette uses it to keep the key out of the recording.
    """

    def complete(self, request: BuiltRequest, *, timeout_s: float) -> ClientReply: ...


# ------------------------------------------------------------------------- normalisation


def _count(value: Any) -> int:
    """A token count: a non-negative whole number, else 0."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return max(0, int(value))


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _to_plain(obj: Any) -> Any:
    """Reduce an SDK object (or a stand-in for one) to JSON types. Null entries are dropped."""
    if obj is None or isinstance(obj, bool | int | float | str):
        return obj
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        return _to_plain(dump(mode="json", exclude_none=True))
    if isinstance(obj, Mapping):
        return {str(k): _to_plain(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list | tuple):
        return [_to_plain(v) for v in obj]
    if is_dataclass(obj) and not isinstance(obj, type):
        return _to_plain(asdict(obj))
    if hasattr(obj, "__dict__"):
        return {
            k: _to_plain(v) for k, v in vars(obj).items() if not k.startswith("_") and v is not None
        }
    return str(obj)


def _without_opaque_blocks(response: dict[str, Any]) -> dict[str, Any]:
    """Drop the signature and the encrypted payload of thinking blocks: they carry no content."""
    content = response.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("thinking", "redacted_thinking"):
                block.pop("signature", None)
                block.pop("data", None)
    return response


def clean_response(raw: Any, scrubber: Scrubber) -> dict[str, Any]:
    """An SDK response as plain, scrubbed JSON (a dict). Everything downstream reads this."""
    plain = _to_plain(raw)
    if not isinstance(plain, dict):
        plain = {"value": plain}
    cleaned = scrubber.json(_without_opaque_blocks(plain))
    return json.loads(json.dumps(cleaned, ensure_ascii=False, allow_nan=False))


def anthropic_usage(usage: Any) -> Usage | None:
    """Normalise ``message.usage`` (an object or a mapping). None when there is none."""
    plain = _to_plain(usage)
    if not isinstance(plain, dict):
        return None
    details = plain.get("output_tokens_details")
    reasoning = details.get("thinking_tokens") if isinstance(details, dict) else None
    return Usage(
        input_tokens=_count(plain.get("input_tokens")),
        output_tokens=_count(plain.get("output_tokens")),
        cache_read_input_tokens=_count(plain.get("cache_read_input_tokens")),
        cache_write_input_tokens=_count(plain.get("cache_creation_input_tokens")),
        reasoning_tokens=None if reasoning is None else _count(reasoning),
    )


def openai_usage(usage: Any) -> Usage | None:
    """Normalise ``completion.usage``. The prompt total of the API includes cached tokens."""
    plain = _to_plain(usage)
    if not isinstance(plain, dict):
        return None
    prompt_details = plain.get("prompt_tokens_details")
    completion_details = plain.get("completion_tokens_details")
    prompt_details = prompt_details if isinstance(prompt_details, dict) else {}
    completion_details = completion_details if isinstance(completion_details, dict) else {}
    cached = _count(prompt_details.get("cached_tokens")) or _count(
        plain.get("prompt_cache_hit_tokens")
    )
    written = _count(prompt_details.get("cache_write_tokens"))
    reasoning = completion_details.get("reasoning_tokens")
    return Usage(
        input_tokens=max(0, _count(plain.get("prompt_tokens")) - cached - written),
        output_tokens=_count(plain.get("completion_tokens")),
        cache_read_input_tokens=cached,
        cache_write_input_tokens=written,
        reasoning_tokens=None if reasoning is None else _count(reasoning),
    )


def normalise_anthropic(
    message: Any, *, latency_ms: int, scrubber: Scrubber | None = None
) -> ClientReply:
    """A :class:`ClientReply` from an Anthropic ``Message`` (or the same shape as a mapping)."""
    response = clean_response(message, scrubber or _DEFAULT_SCRUBBER)
    return ClientReply(
        reply=reply_from_anthropic(response),
        usage=anthropic_usage(response.get("usage")),
        latency_ms=latency_ms,
        model=_text(response.get("model")),
        response_id=_text(response.get("id")),
        response=response,
    )


def normalise_openai(
    completion: Any, *, latency_ms: int, scrubber: Scrubber | None = None
) -> ClientReply:
    """A :class:`ClientReply` from a chat completion (or the same shape as a mapping)."""
    response = clean_response(completion, scrubber or _DEFAULT_SCRUBBER)
    return ClientReply(
        reply=reply_from_openai(response),
        usage=openai_usage(response.get("usage")),
        latency_ms=latency_ms,
        model=_text(response.get("model")),
        response_id=_text(response.get("id")),
        response=response,
    )


def reply_from_response(
    kind: EndpointKind,
    response: Mapping[str, Any],
    usage: Usage | None,
    *,
    latency_ms: int = 0,
) -> ClientReply:
    """Rebuild a :class:`ClientReply` from a stored response (what the cassette replays)."""
    plain = dict(response)
    reply = reply_from_anthropic(plain) if kind == "anthropic" else reply_from_openai(plain)
    return ClientReply(
        reply=reply,
        usage=usage,
        latency_ms=latency_ms,
        model=_text(plain.get("model")),
        response_id=_text(plain.get("id")),
        response=plain,
    )


# ------------------------------------------------------------------------------- clients


class AnthropicClient:
    """``anthropic.Anthropic().messages.create(**payload)``, normalised."""

    def __init__(
        self,
        sdk: Any,
        *,
        scrubber: Scrubber | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._sdk = sdk
        self._clock = clock
        self.scrubber = scrubber or Scrubber()

    def __repr__(self) -> str:
        return "AnthropicClient()"

    def complete(self, request: BuiltRequest, *, timeout_s: float) -> ClientReply:
        if request.kind != "anthropic":
            raise ValueError(f"an Anthropic client cannot send a {request.kind!r} request")
        started = self._clock()
        message = self._sdk.messages.create(**request.payload, timeout=timeout_s)
        latency_ms = round((self._clock() - started) * 1000)
        return normalise_anthropic(message, latency_ms=latency_ms, scrubber=self.scrubber)


class OpenAIClient:
    """``openai.OpenAI(base_url=...).chat.completions.create(**payload)``, normalised."""

    def __init__(
        self,
        sdk: Any,
        *,
        scrubber: Scrubber | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._sdk = sdk
        self._clock = clock
        self.scrubber = scrubber or Scrubber()

    def __repr__(self) -> str:
        return "OpenAIClient()"

    def complete(self, request: BuiltRequest, *, timeout_s: float) -> ClientReply:
        if request.kind != "openai_compatible":
            raise ValueError(f"an OpenAI client cannot send a {request.kind!r} request")
        started = self._clock()
        completion = self._sdk.chat.completions.create(**request.payload, timeout=timeout_s)
        latency_ms = round((self._clock() - started) * 1000)
        return normalise_openai(completion, latency_ms=latency_ms, scrubber=self.scrubber)


class MissingCredentialError(RuntimeError):
    """An environment variable named by the configuration is not set. Names it, never a value."""


SdkFactory = Callable[..., Any]


def _read_env(environ: Mapping[str, str], name: str, what: str, where: str) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise MissingCredentialError(
            f"{where}: environment variable {name} ({what}) is not set or empty"
        )
    return value


def _real_sdk(kind: EndpointKind) -> SdkFactory:
    if kind == "anthropic":
        import anthropic

        return anthropic.Anthropic
    import openai

    return openai.OpenAI


def make_client(
    endpoint: EndpointConfig,
    *,
    name: str = "endpoint",
    environ: Mapping[str, str] | None = None,
    factories: Mapping[EndpointKind, SdkFactory] | None = None,
    http_client: Any = None,
) -> AnthropicClient | OpenAIClient:
    """The client of one configured endpoint.

    The key and the base URL come from the environment variables the configuration names
    (``environ`` defaults to the process environment). The SDK's own retries are switched off.
    ``factories`` replaces the SDK constructors, for tests; ``http_client`` is handed to the SDK
    as it is (for a proxy, say).
    """
    env = os.environ if environ is None else environ
    where = f"endpoint {name!r}"
    key: str | None = None
    if endpoint.api_key_env is not None:
        key = _read_env(env, endpoint.api_key_env, "API key", where)
    base_url = endpoint.base_url
    if endpoint.base_url_env is not None:
        base_url = _read_env(env, endpoint.base_url_env, "base URL", where)
    if endpoint.kind == "anthropic" and key is None:
        raise MissingCredentialError(f"{where}: an Anthropic endpoint needs api_key_env")

    factory = (factories or {}).get(endpoint.kind) or _real_sdk(endpoint.kind)
    options: dict[str, Any] = {"api_key": key or _NO_KEY, "max_retries": 0}
    if base_url is not None:
        options["base_url"] = base_url
    if http_client is not None:
        options["http_client"] = http_client
    sdk = factory(**options)
    scrubber = Scrubber([key] if key else [])
    if endpoint.kind == "anthropic":
        return AnthropicClient(sdk, scrubber=scrubber)
    return OpenAIClient(sdk, scrubber=scrubber)


def endpoints_in_use(config: NightConfig) -> list[str]:
    """Names of the endpoints that a model seat or an llm show role of ``config`` calls."""
    names = {seat.endpoint for seat in config.seats if seat.kind == "llm" and seat.endpoint}
    show = config.show
    roles = [show.host_offline, *show.judges, show.content_scan.classifier]
    names |= {role.endpoint for role in roles if role and role.kind == "llm" and role.endpoint}
    return sorted(names)


def make_clients(
    config: NightConfig,
    *,
    environ: Mapping[str, str] | None = None,
    factories: Mapping[EndpointKind, SdkFactory] | None = None,
    http_client: Any = None,
    names: Iterable[str] | None = None,
) -> dict[str, AnthropicClient | OpenAIClient]:
    """One client for every endpoint of ``config`` that a model is going to call.

    ``names`` restricts the clients to those endpoints (an endpoint whose variables are not
    set is only an error if it is asked for).
    """
    wanted = endpoints_in_use(config) if names is None else sorted(set(names))
    return {
        name: make_client(
            config.endpoints[name],
            name=name,
            environ=environ,
            factories=factories,
            http_client=http_client,
        )
        for name in wanted
    }


# ----------------------------------------------------------------------------- failures

# 408 and 409 are retried by the SDKs too; 429 and every 5xx are the documented transient codes.
_TRANSIENT_STATUS: Final = frozenset({408, 409, 429})


@functools.cache
def _sdk_errors() -> SimpleNamespace:
    """The exception classes of the installed SDKs, grouped by what they mean."""
    timeouts: list[type[BaseException]] = []
    connections: list[type[BaseException]] = []
    statuses: list[type[BaseException]] = []
    for module_name in ("anthropic", "openai"):
        try:
            module = __import__(module_name)
        except ImportError:  # pragma: no cover - both are runtime dependencies
            continue
        timeouts.append(module.APITimeoutError)
        connections.append(module.APIConnectionError)
        statuses.append(module.APIStatusError)
    return SimpleNamespace(
        timeouts=tuple(timeouts), connections=tuple(connections), statuses=tuple(statuses)
    )


def _retry_after(exc: BaseException) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", None)
    try:
        value = headers.get("retry-after") if headers is not None else None
        seconds = float(value) if value is not None else None
    except (TypeError, ValueError, AttributeError):
        return None
    return seconds if seconds is not None and 0 <= seconds < 3600 else None


def _message_of(exc: BaseException) -> str:
    message = getattr(exc, "message", None)
    text = message if isinstance(message, str) and message else str(exc)
    text = " ".join(scrub_text(text).split())
    return text[:_MAX_MESSAGE_CHARS]


def classify_exception(exc: BaseException) -> CallFailure:
    """Turn the exception of a failed call into a :class:`CallFailure`.

    A timeout is not retried by the orchestrator (a late model would only be late again). Rate
    limits (429), server errors (5xx, including 529 overloaded), request timeouts (408),
    conflicts (409) and lost connections are transient. Every other status, the 400 family
    first of all, is final: the same request would be refused again.
    """
    errors = _sdk_errors()
    name = type(exc).__name__
    message = _message_of(exc)
    if isinstance(exc, (*errors.timeouts, TimeoutError)):
        return CallFailure("timeout", name, message)
    if isinstance(exc, errors.connections):
        return CallFailure("error", name, message, retryable=True)
    if isinstance(exc, errors.statuses):
        status = getattr(exc, "status_code", None)
        status = status if isinstance(status, int) else None
        api_type = getattr(exc, "type", None)
        transient = status is not None and (status >= 500 or status in _TRANSIENT_STATUS)
        return CallFailure(
            "error",
            name,
            message,
            status=status,
            api_type=api_type if isinstance(api_type, str) else None,
            retryable=transient,
            retry_after_s=_retry_after(exc),
        )
    return CallFailure("error", name, message)


def merged_scrubber(clients: Iterable[Any]) -> Scrubber:
    """One scrubber that knows the secrets of every client that has one."""
    scrubbers = [c.scrubber for c in clients if isinstance(getattr(c, "scrubber", None), Scrubber)]
    return Scrubber().merged(*scrubbers)


__all__ = (
    "REDACTED",
    "SdkFactory",
    "AnthropicClient",
    "CallFailure",
    "ClientReply",
    "LlmClient",
    "MissingCredentialError",
    "OpenAIClient",
    "Scrubber",
    "Usage",
    "classify_exception",
    "make_client",
    "make_clients",
    "merged_scrubber",
    "normalise_anthropic",
    "normalise_openai",
    "scrub_json",
    "scrub_text",
)
