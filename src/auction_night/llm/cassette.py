"""The cassette: every attempt of a night, recorded once and replayed byte for byte.

``calls.jsonl`` holds one JSON line per attempt. An attempt is one request that was sent to a
model, whatever came of it: a reply that read well, a reply that did not, a refusal, a timeout, an
error. A retry is an attempt of its own, with the next attempt number. A line has these fields, in
this order:

``v``                the format version (1)
``key``              ``night``, ``lot`` (null for night-level calls), ``phase``, ``step``,
                     ``seat`` and ``attempt``: the six-part key that identifies the attempt
``role``             who asked, for example ``seat:3``
``kind``, ``model``  the endpoint kind and the model id that was requested
``model_returned``   the model id the API reports (the one that counts for prices)
``request_hash``     :func:`~auction_night.llm.request.request_hash` of the request
``request``          the SDK keyword arguments: no headers, no keys, scrubbed
``outcome``          ``ok``, ``timeout``, ``error``, ``refusal`` or ``parse_failed``
``stop_reason``, ``refusal``  as the API reported them
``response``         the raw response as plain JSON, scrubbed (null for a timeout or an error)
``error``            the failure of a timeout or an error (null otherwise)
``parsed``           what the parsing ladder made of the reply (null when there was no reply)
``repair``           the repair codes of the attempt
``usage``            input, output, cache read, cache write and reasoning tokens
``cost_microusd``    the cost of the attempt at the prices in force when it was recorded
``latency_ms``, ``started_at``, ``finished_at``  timing, which no event ever depends on

Recording and replaying
-----------------------
A recording is written in batches. A batch is sorted by key before it is appended, so the file
does not depend on the order in which concurrent replies arrived. The orchestrator writes one
batch per phase (per call for the sequential talk phase).

A replay serves the stored attempt for a key and compares the request hash: a request that is
not byte-for-byte the one that was recorded is a :class:`CassetteDrift`, unless ``allow_drift``
is set, in which case the stored attempt is served anyway and the key is listed in
:attr:`Cassette.drifted`. A key that was never recorded is a :class:`CassetteMiss`. The cost of
an attempt comes from the recording, not from today's price list, so the cost gate fires at the
same place in a replay as it did live.

Nothing secret is written. The request keeps no headers (``extra_headers``, ``extra_query``) and
every string goes through a :class:`~auction_night.llm.client.Scrubber` that removes key-shaped
text and the exact keys it was told about.
"""

import copy
import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, get_args

from auction_night.contracts.mechanism import MAX_LOTS, MAX_SEATS, PHASES
from auction_night.llm.client import CallFailure, ClientReply, Scrubber, Usage, reply_from_response

SCHEMA_VERSION: Final = 1
Outcome = Literal["ok", "timeout", "error", "refusal", "parse_failed"]
OUTCOMES: Final[tuple[str, ...]] = get_args(Outcome)
MAX_ATTEMPTS: Final = 10

# Request fields that are headers or query strings and so never recorded.
_TRANSPORT_FIELDS: Final = ("extra_headers", "extra_query")
_NIGHT_ID: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PHASE_NAME: Final = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_PHASE_RANK: Final = {name: index for index, name in enumerate(PHASES)}


class CassetteError(RuntimeError):
    """Base class of the errors of a recording or a replay."""


class CassetteFormatError(CassetteError):
    """A line of a cassette file cannot be read."""


class CassetteMiss(CassetteError):
    """A replay asked for an attempt that was never recorded."""


class CassetteDrift(CassetteError):
    """A replay would send a request that differs from the recorded one."""


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class CallKey:
    """The six-part key of an attempt: night, lot, phase, step, seat and attempt number.

    ``lot`` is None for night-level calls (the opening plan). ``step`` is a number that is
    unique, together with the lot, phase and seat, within the night: the position of the request
    in the night is the natural choice. ``seat`` is None for a call that no seat makes (a host or a
    judge). ``attempt`` starts at 1.
    """

    night: str
    lot: int | None
    phase: str
    step: int
    seat: int | None
    attempt: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.night, str) or not _NIGHT_ID.match(self.night):
            raise ValueError(f"bad night id {self.night!r}")
        if self.lot is not None and not (_is_int(self.lot) and 1 <= self.lot <= MAX_LOTS):
            raise ValueError(f"lot out of range: {self.lot!r}")
        if not isinstance(self.phase, str) or not _PHASE_NAME.match(self.phase):
            raise ValueError(f"bad phase name {self.phase!r}")
        if not _is_int(self.step) or self.step < 0:
            raise ValueError(f"step must be a non-negative integer, got {self.step!r}")
        if self.seat is not None and not (_is_int(self.seat) and 1 <= self.seat <= MAX_SEATS):
            raise ValueError(f"seat out of range: {self.seat!r}")
        if not _is_int(self.attempt) or not 1 <= self.attempt <= MAX_ATTEMPTS:
            raise ValueError(f"attempt must lie between 1 and {MAX_ATTEMPTS}, got {self.attempt!r}")

    def with_attempt(self, attempt: int) -> "CallKey":
        """The same call, another attempt."""
        return CallKey(self.night, self.lot, self.phase, self.step, self.seat, attempt)

    def as_tuple(self) -> tuple[str, int | None, str, int, int | None, int]:
        """``(night, lot, phase, step, seat, attempt)``."""
        return (self.night, self.lot, self.phase, self.step, self.seat, self.attempt)

    @property
    def call_id(self) -> str:
        """A short name for the call (not the attempt), usable as ``call_id`` in events."""
        lot = "n" if self.lot is None else f"l{self.lot}"
        seat = "x" if self.seat is None else f"s{self.seat}"
        return f"{lot}.{self.phase}.{self.step}.{seat}"

    @property
    def sort_key(self) -> tuple[str, int, int, int, str, int, int]:
        """The order of a file: by night, lot, step, phase, seat and attempt."""
        return (
            self.night,
            self.lot or 0,
            self.step,
            _PHASE_RANK.get(self.phase, len(PHASES)),
            self.phase,
            self.seat or 0,
            self.attempt,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "night": self.night,
            "lot": self.lot,
            "phase": self.phase,
            "step": self.step,
            "seat": self.seat,
            "attempt": self.attempt,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CallKey":
        try:
            return cls(
                night=data["night"],
                lot=data["lot"],
                phase=data["phase"],
                step=data["step"],
                seat=data["seat"],
                attempt=data["attempt"],
            )
        except KeyError as exc:
            raise CassetteFormatError(f"key without {exc.args[0]!r}") from None
        except ValueError as exc:
            raise CassetteFormatError(str(exc)) from None

    def __str__(self) -> str:
        lot = "-" if self.lot is None else self.lot
        seat = "-" if self.seat is None else self.seat
        return (
            f"{self.night}/lot {lot}/{self.phase}/step {self.step}/seat {seat}/try {self.attempt}"
        )


def stored_request(payload: Mapping[str, Any], scrubber: Scrubber | None = None) -> dict[str, Any]:
    """The request as the cassette keeps it: a copy without headers, scrubbed."""
    copied = {k: copy.deepcopy(v) for k, v in payload.items() if k not in _TRANSPORT_FIELDS}
    return (scrubber or Scrubber()).json(copied)


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class CassetteEntry:
    """One attempt, as it is stored. See the module docstring for the fields."""

    key: CallKey
    role: str
    kind: str
    model: str
    request_hash: str
    request: Mapping[str, Any]
    outcome: str
    model_returned: str | None = None
    stop_reason: str | None = None
    refusal: bool = False
    response: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | None = None
    parsed: Mapping[str, Any] | None = None
    repair: tuple[str, ...] = ()
    usage: Mapping[str, Any] | None = None
    cost_microusd: int | None = None
    latency_ms: int | None = None
    started_at: str = ""
    finished_at: str = ""

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome {self.outcome!r}")
        if not self.request_hash.startswith("sha256:"):
            raise ValueError("request_hash must be a sha256: digest")

    def to_dict(self) -> dict[str, Any]:
        """The line as a dict, with the fields in file order."""
        return {
            "v": SCHEMA_VERSION,
            "key": self.key.as_dict(),
            "role": self.role,
            "kind": self.kind,
            "model": self.model,
            "model_returned": self.model_returned,
            "request_hash": self.request_hash,
            "request": self.request,
            "outcome": self.outcome,
            "stop_reason": self.stop_reason,
            "refusal": self.refusal,
            "response": self.response,
            "error": self.error,
            "parsed": self.parsed,
            "repair": list(self.repair),
            "usage": self.usage,
            "cost_microusd": self.cost_microusd,
            "latency_ms": self.latency_ms,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    def to_line(self, scrubber: Scrubber | None = None) -> str:
        """The JSON line (no newline), with every string scrubbed once more."""
        return _dumps((scrubber or Scrubber()).json(self.to_dict()))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CassetteEntry":
        """Validate and read one line that :meth:`to_dict` wrote."""
        if data.get("v") != SCHEMA_VERSION:
            raise CassetteFormatError(f"unsupported cassette version {data.get('v')!r}")
        key_data = data.get("key")
        if not isinstance(key_data, Mapping):
            raise CassetteFormatError("line without a key")
        call_key = CallKey.from_dict(key_data)

        def mapping_or_none(name: str) -> Mapping[str, Any] | None:
            value = data.get(name)
            if value is not None and not isinstance(value, Mapping):
                raise CassetteFormatError(f"{name} must be an object or null")
            return value

        def text(name: str) -> str:
            value = data.get(name)
            if not isinstance(value, str):
                raise CassetteFormatError(f"{name} must be a string")
            return value

        request = mapping_or_none("request")
        if request is None:
            raise CassetteFormatError("line without a request")
        cost = data.get("cost_microusd")
        latency = data.get("latency_ms")
        if cost is not None and not _is_int(cost):
            raise CassetteFormatError("cost_microusd must be an integer or null")
        if latency is not None and not _is_int(latency):
            raise CassetteFormatError("latency_ms must be an integer or null")
        repair = data.get("repair") or []
        if not isinstance(repair, list) or not all(isinstance(c, str) for c in repair):
            raise CassetteFormatError("repair must be a list of codes")
        returned = data.get("model_returned")
        stop = data.get("stop_reason")
        try:
            return cls(
                key=call_key,
                role=text("role"),
                kind=text("kind"),
                model=text("model"),
                request_hash=text("request_hash"),
                request=request,
                outcome=text("outcome"),
                model_returned=returned if isinstance(returned, str) else None,
                stop_reason=stop if isinstance(stop, str) else None,
                refusal=bool(data.get("refusal")),
                response=mapping_or_none("response"),
                error=mapping_or_none("error"),
                parsed=mapping_or_none("parsed"),
                repair=tuple(repair),
                usage=mapping_or_none("usage"),
                cost_microusd=cost,
                latency_ms=latency,
                started_at=str(data.get("started_at") or ""),
                finished_at=str(data.get("finished_at") or ""),
            )
        except ValueError as exc:
            raise CassetteFormatError(str(exc)) from None

    # ------------------------------------------------------------------ what a replay serves

    @property
    def failure(self) -> CallFailure | None:
        """The failure of a timeout or error attempt, else None."""
        return None if self.error is None else CallFailure.from_dict(self.error)

    @property
    def usage_tally(self) -> Usage | None:
        return None if self.usage is None else Usage.from_dict(self.usage)

    def client_reply(self) -> ClientReply:
        """The stored reply, read back exactly like a live one."""
        if self.response is None:
            raise CassetteError(f"{self.key}: this attempt has no response ({self.outcome})")
        kind = "anthropic" if self.kind == "anthropic" else "openai_compatible"
        return reply_from_response(
            kind, self.response, self.usage_tally, latency_ms=self.latency_ms or 0
        )


def _changed_fields(stored: Mapping[str, Any], payload: Mapping[str, Any]) -> list[str]:
    now = stored_request(payload)
    return sorted(name for name in set(stored) | set(now) if stored.get(name) != now.get(name))


class Cassette:
    """A recording of a night, or a replay of one.

    Use :meth:`record` or :meth:`replay`/:meth:`from_text`. ``path`` None records in memory.
    """

    def __init__(
        self,
        mode: Literal["record", "replay"],
        path: Path | None,
        entries: Iterable[CassetteEntry] = (),
        *,
        scrubber: Scrubber | None = None,
        allow_drift: bool = False,
    ) -> None:
        self.mode = mode
        self.path = path
        self.allow_drift = allow_drift
        self.scrubber = scrubber or Scrubber()
        self._entries: list[CassetteEntry] = []
        self._by_key: dict[CallKey, CassetteEntry] = {}
        self._lines: list[str] = []
        self._served: set[CallKey] = set()
        self._drifted: list[tuple[CallKey, str, str]] = []
        for entry in entries:
            self._add(entry)

    # --------------------------------------------------------------------- construction

    @classmethod
    def record(
        cls,
        path: str | Path | None = None,
        *,
        overwrite: bool = False,
        scrubber: Scrubber | None = None,
    ) -> "Cassette":
        """Start a recording. An existing, non-empty file is refused unless ``overwrite``."""
        target = None if path is None else Path(path)
        if target is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and target.stat().st_size > 0 and not overwrite:
                raise FileExistsError(f"{target} already holds a recording")
            target.write_text("", encoding="utf-8")  # an empty file exists even if nothing is sent
        return cls("record", target, scrubber=scrubber)

    @classmethod
    def replay(cls, path: str | Path, *, allow_drift: bool = False) -> "Cassette":
        """Load a recording from a file."""
        source = Path(path)
        with source.open(encoding="utf-8", newline="") as handle:
            text = handle.read()
        return cls.from_text(text, allow_drift=allow_drift, source=str(source.name), path=source)

    @classmethod
    def from_text(
        cls,
        text: str,
        *,
        allow_drift: bool = False,
        source: str = "cassette",
        path: Path | None = None,
    ) -> "Cassette":
        """Load a recording from the text of ``calls.jsonl``."""
        entries = []
        for number, line in enumerate(text.split("\n"), start=1):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except ValueError:
                raise CassetteFormatError(f"{source}:{number}: not valid JSON") from None
            if not isinstance(data, dict):
                raise CassetteFormatError(f"{source}:{number}: a line must be a JSON object")
            try:
                entries.append(CassetteEntry.from_dict(data))
            except CassetteFormatError as exc:
                raise CassetteFormatError(f"{source}:{number}: {exc}") from None
        return cls("replay", path, entries, allow_drift=allow_drift)

    def _add(self, entry: CassetteEntry) -> None:
        if entry.key in self._by_key:
            raise CassetteError(f"duplicate key in the cassette: {entry.key}")
        self._by_key[entry.key] = entry
        self._entries.append(entry)

    # ----------------------------------------------------------------------- recording

    def commit(self, entries: Iterable[CassetteEntry]) -> int:
        """Append a batch of attempts, sorted by key. Returns how many were written.

        The file is flushed to disk before the call returns.
        """
        if self.mode != "record":
            raise CassetteError("a replay cannot be written to")
        batch = sorted(entries, key=lambda e: e.key.sort_key)
        keys = [e.key for e in batch]
        if len(set(keys)) != len(keys) or any(k in self._by_key for k in keys):
            raise CassetteError("duplicate key in a batch of attempts")
        lines = [e.to_line(self.scrubber) for e in batch]
        for entry in batch:
            self._add(entry)
        self._lines.extend(lines)
        if self.path is not None and lines:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write("".join(line + "\n" for line in lines))
                handle.flush()
                os.fsync(handle.fileno())
        return len(batch)

    def text(self) -> str:
        """The content of ``calls.jsonl`` as recorded (or as loaded, re-serialised)."""
        if self.mode == "record":
            return "".join(line + "\n" for line in self._lines)
        return "".join(e.to_line() + "\n" for e in self._entries)

    # ------------------------------------------------------------------------- replay

    def lookup(
        self, key: CallKey, request_hash: str, *, request: Mapping[str, Any] | None = None
    ) -> CassetteEntry:
        """The stored attempt for ``key``, after the request hash has been compared.

        ``request`` (the payload about to be sent) is only used to say in the error which fields
        differ. Raises :class:`CassetteMiss` for an unknown key and :class:`CassetteDrift` for a
        different request, unless ``allow_drift`` is set.
        """
        entry = self._by_key.get(key)
        if entry is None:
            raise CassetteMiss(
                f"no recorded attempt for {key} ({len(self._by_key)} attempts are recorded)"
            )
        if entry.request_hash != request_hash:
            if not self.allow_drift:
                changed = _changed_fields(entry.request, request) if request is not None else []
                where = f" (changed fields: {', '.join(changed)})" if changed else ""
                raise CassetteDrift(
                    f"the request for {key} differs from the recording{where}: "
                    f"recorded {entry.request_hash}, now {request_hash}"
                )
            self._drifted.append((key, entry.request_hash, request_hash))
        self._served.add(key)
        return entry

    @property
    def entries(self) -> tuple[CassetteEntry, ...]:
        return tuple(self._entries)

    @property
    def drifted(self) -> tuple[tuple[CallKey, str, str], ...]:
        """Keys served despite a different request hash: ``(key, recorded, now)``."""
        return tuple(self._drifted)

    def unused(self) -> list[CallKey]:
        """Recorded attempts that a replay never asked for, in key order."""
        return sorted((k for k in self._by_key if k not in self._served), key=lambda k: k.sort_key)

    def __len__(self) -> int:
        return len(self._entries)

    def keys(self) -> Sequence[CallKey]:
        return sorted(self._by_key, key=lambda k: k.sort_key)
