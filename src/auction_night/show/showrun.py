"""Recording the model calls of the show layer: the host, the judges and the content classifier.

``auction-night run`` and ``play`` record the calls of the seats (``calls.jsonl``). The show layer
works after the night, from the finished log: the host comments on it, two judges read the public
record and the content classifier reads every public text. When one of these roles is a model
(``kind: llm``), :func:`~auction_night.show.replay_export.export_directory` never calls it. It
reads what the role said from a recording of its own, ``host_calls.jsonl``, ``judge_calls.jsonl``
or ``scan_calls.jsonl`` in the run directory. :func:`record_show` makes those files.

What it does
------------
1. The night is played again from ``calls.jsonl``, so the seats call no model, and what they cost
   is read from the recording.
2. The fact cards are built, and then the roles are called in the order the export uses them. The
   host writes the commentary. The judges read the night, and the replay is assembled from their
   answers. The content classifier reads the public texts of the replay in batches. A role that
   is a ``mock`` runs on the spot, exactly as in an export, and leaves no file.
3. Every call of a model role goes through its client, into a cassette in memory.
4. The recordings are written to ``<name>.part`` files, and the export is run on them (nothing is
   written) to see that it reads them back. Only then are they moved over their real names, one
   by one, with :func:`os.replace`. Whatever goes wrong before that leaves nothing behind.

Cost
----
The calls go through the same price list and the same :class:`~auction_night.llm.accounting.Ledger`
as the seats, under the labels ``host``, ``judge:0``, ``judge:1`` and ``classifier``. The cap
``limits.max_cost_per_night_usd`` holds for the show layer as it held for the seats: the ledger of
this command starts at zero and the show layer may spend the cap, on its own. What the seats cost
is not deducted. The log of the night records the cap it was run with (the closing ``cost_event``),
so the seats can only be replayed under that very cap, and a cap that was lower or higher here
could not be told apart from a night that differs. The seats' cost, read from ``calls.jsonl``, is
reported next to the cost of the show layer.

The gate is checked before the first call and after every call of a role (one call of a role is one
text of the host, one answer of a judge, one batch of the classifier), the way the gate of the
seats is checked after each phase, so the cap can be passed by at most one call. A call that the
closed gate refuses is not made, and the recording stops with :class:`CostCapReached`. A cap that
the very last call reaches stops nothing, as for the seats. A cap needs a price for every model
that is called, or the recording does not start.

Failure
-------
A call that gets no answer at all (a timeout, an error that no retry cured, a refusal by the API)
stops the recording at once with :class:`RoleCallFailed`, and later calls are not made. An answer
that a role cannot use (a host text with a number that is not on the card, a judge's JSON that
cannot be read) is not a failure of the call. The role has its own way to deal with it, a second
try and then a fallback or an absence, and the recording keeps what happened.

Neither failure writes a file. Findings of the content scan do not stop the recording either: the
scan says what the classifier said, and ``export`` is the command that refuses a night.
"""

import contextlib
import dataclasses
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from auction_night.contracts.config import EndpointKind, NightConfig, RoleConfig
from auction_night.contracts.replay import JudgeAbsence
from auction_night.llm.accounting import Ledger, MissingPriceError, PriceTable, Tally, show_roles
from auction_night.llm.cassette import Cassette, CassetteEntry, CassetteError
from auction_night.llm.client import (
    LlmClient,
    MissingCredentialError,
    SdkFactory,
    make_clients,
    merged_scrubber,
)
from auction_night.show.content_scan import (
    Classifier,
    ContentGate,
    ContentScanRejected,
    Flag,
    Probe,
    ScanReport,
)
from auction_night.show.factcard import build_fact_cards
from auction_night.show.host import HostBackend, HostJob, Rewrite, make_host, run_host
from auction_night.show.judges import (
    JudgeBackend,
    JudgeJob,
    JudgeReply,
    JudgeRetry,
    ModelJudge,
    make_judges,
)
from auction_night.show.replay_export import (
    HOST_CALLS_FILE,
    JUDGE_CALLS_FILE,
    SCAN_CALLS_FILE,
    build_replay,
    export_directory,
    replay_run,
    show_parts,
)
from auction_night.theory import analyse_night

__all__ = [
    "CostCapReached",
    "RoleCallFailed",
    "RoleRecord",
    "ShowFailed",
    "ShowRecorded",
    "ShowSetupError",
    "describe_show",
    "record_show",
]

PART_SUFFIX: Final = ".part"
_FAILED: Final = frozenset({"timeout", "error", "refusal"})  # outcomes of an attempt with no answer
_MICRO: Final = 1_000_000
_SHOWN_FINDINGS: Final = 10


class ShowSetupError(RuntimeError):
    """The show layer cannot be recorded as asked: a recording is in the way, a key or a price is
    missing, a directory cannot be written. The message is meant for the person at the command
    line, and the command exits with status 2."""


class ShowFailed(RuntimeError):
    """The recording was stopped and nothing was written. The command exits with status 1."""


class RoleCallFailed(ShowFailed):
    """A model role got no answer at all from its endpoint."""


class CostCapReached(ShowFailed):
    """The cost cap of the night is used up, and a call that was still to be made was not made."""


# ------------------------------------------------------------------------------- what is said


def _usd(microusd: int) -> str:
    return f"${microusd / _MICRO:.4f}"


def _who(role: str) -> str:
    """A role label in words."""
    if role == "host":
        return "the host"
    if role == "classifier":
        return "the content classifier"
    return f"judge {role.split(':', 1)[-1]}"


def _failure_text(entry: CassetteEntry) -> str:
    """Why the last attempt of a call got no answer, in one line (the text is scrubbed already)."""
    if entry.outcome == "refusal":
        return "the API refused to answer"
    failure = entry.failure
    if failure is None:
        return entry.outcome
    what = f"{failure.kind}, {failure.error_type}"
    if failure.status is not None:
        what += f", HTTP {failure.status}"
    return f"{what}: {failure.message}" if failure.message else what


def _place(entry: CassetteEntry) -> str:
    """Where in the show a recorded call was: a lot or the recap, or a batch of texts."""
    if entry.role == "host":
        return "the recap" if entry.key.lot is None else f"lot {entry.key.lot}"
    if entry.role == "classifier":
        return f"batch {entry.key.step}"
    return "the night"


def _cap_message(cap_microusd: int, who: str, spent_microusd: int) -> str:
    """Why a role was not called when the gate was closed."""
    return (
        f"the cost cap of {_usd(cap_microusd)} is reached: the show layer has spent "
        f"{_usd(spent_microusd)}, so {who} was not called. No recording was written. The cap "
        "is that of the night (limits.max_cost_per_night_usd) and holds for the show layer on "
        "its own. It cannot be raised here, because the log of the night records it. Use "
        "cheaper models for the show roles in a copy of the configuration and give it with -c."
    )


@dataclass
class _Guard:
    """Watches one role that is a model while it is called: the ledger of the show layer and the
    cassette that its calls go into. Call ``mark`` before a call and ``check`` after it."""

    role: str
    cassette: Cassette
    ledger: Ledger

    def mark(self) -> tuple[int, int]:
        return len(self.cassette), self.ledger.aborted_actions

    def check(self, mark: tuple[int, int]) -> None:
        recorded, skipped = mark
        who = _who(self.role)
        if self.ledger.aborted_actions > skipped:  # the role found the gate closed
            assert self.ledger.cap_microusd is not None
            raise CostCapReached(
                _cap_message(self.ledger.cap_microusd, who, self.ledger.spent_microusd)
            )
        fresh = self.cassette.entries[recorded:]
        if fresh and fresh[-1].outcome in _FAILED:
            last = fresh[-1]
            hint = ""
            if last.outcome == "timeout":
                hint = (
                    " A longer limits.call_timeout_s in a copy of the configuration, given "
                    "with -c, may help."
                )
            raise RoleCallFailed(
                f"{who} got no answer for {_place(last)} after {len(fresh)} "
                f"attempt{'s' if len(fresh) != 1 else ''} ({_failure_text(last)}). "
                f"No recording was written.{hint}"
            )
        self.ledger.gate()  # after the cost of the call is in, as for the seats


class _WatchedHost:
    """The host, checked after each text it writes."""

    def __init__(self, inner: HostBackend, guard: _Guard) -> None:
        self._inner = inner
        self._guard = guard

    def write(self, job: HostJob, rewrite: Rewrite | None = None) -> str | None:
        mark = self._guard.mark()
        text = self._inner.write(job, rewrite)
        self._guard.check(mark)
        return text


class _WatchedJudge:
    """A judge, checked after each answer. It has the index and the vendor of the judge."""

    def __init__(self, inner: JudgeBackend, guard: _Guard) -> None:
        self.index = inner.index
        self.vendor = inner.vendor
        self._inner = inner
        self._guard = guard

    @property
    def miss(self) -> str | None:
        return getattr(self._inner, "miss", None)

    def write(self, job: JudgeJob, retry: JudgeRetry | None = None) -> JudgeReply | None:
        mark = self._guard.mark()
        reply = self._inner.write(job, retry)
        self._guard.check(mark)
        return reply


class _WatchedClassifier:
    """The content classifier, checked after each batch."""

    def __init__(self, inner: Classifier, guard: _Guard) -> None:
        self.label = inner.label
        self._inner = inner
        self._guard = guard

    def classify(
        self, batch: Sequence[Probe], index: int, *, codenames: Sequence[str] = ()
    ) -> list[Flag] | None:
        mark = self._guard.mark()
        flags = self._inner.classify(batch, index, codenames=codenames)
        self._guard.check(mark)
        return flags


# ------------------------------------------------------------------------------- the result


@dataclass(frozen=True)
class RoleRecord:
    """One model role that was recorded: its label, its model, the file its calls are in, and the
    calls, tokens and cost that the ledger counted for it."""

    role: str
    model: str
    file: str
    tally: Tally


@dataclass(frozen=True)
class ShowRecorded:
    """What :func:`record_show` did.

    ``roles`` are the roles that are models, in the order they are called, and ``mocks`` the
    labels of the roles that are not (nothing is recorded for them). ``files`` are the recordings
    written, in the order host, judges, classifier: none when no role is a model.
    ``seats_microusd`` is what the seats cost, as ``calls.jsonl`` has it. It is reported, and not
    deducted from the cap. ``scan`` is what the content gate made of the night with the recorded
    classifier and with its word list, which is not recorded: when it has findings, ``export``
    will refuse the night. ``host_texts`` and ``host_fallbacks`` count the texts of the host and
    those that are played from the fact card. ``absent`` lists the judges that gave no usable
    answer.
    """

    night: str
    roles: tuple[RoleRecord, ...]
    mocks: tuple[str, ...]
    files: tuple[Path, ...]
    seats_microusd: int
    cap_microusd: int | None
    scan: ScanReport | None = None
    host_texts: int = 0
    host_fallbacks: int = 0
    absent: tuple[JudgeAbsence, ...] = ()

    @property
    def show_microusd(self) -> int:
        """What the model roles of the show layer cost."""
        return sum(record.tally.cost_microusd for record in self.roles)

    @property
    def calls(self) -> int:
        """The calls that were made, retries included."""
        return sum(record.tally.calls for record in self.roles)

    @property
    def unpriced_calls(self) -> int:
        """Calls whose model has no price: their cost is not in :attr:`show_microusd`."""
        return sum(record.tally.unpriced_calls for record in self.roles)


def _tokens(tally: Tally) -> str:
    prompt = tally.input_tokens + tally.cache_read_input_tokens + tally.cache_write_input_tokens
    return f"{prompt:,} tokens in, {tally.output_tokens:,} out"


def describe_show(done: ShowRecorded) -> list[str]:
    """A short account for the terminal: one line for each recorded role, the cost, and what the
    host, the judges and the content scan made of the night."""
    if not done.roles:
        return [
            f"night {done.night}: every role of the show layer is a mock, "
            "so there is nothing to record"
        ]
    lines = [f"night {done.night}: the model calls of the show layer are recorded"]
    width = max(len(record.role) for record in done.roles)
    for record in done.roles:
        tally = record.tally
        cost = "no price" if tally.unpriced_calls else _usd(tally.cost_microusd)
        calls = f"{tally.calls} call{'s' if tally.calls != 1 else ''}"
        lines.append(
            f"  {record.role:<{width}}  {record.model}: {calls}, {_tokens(tally)}, {cost}"
            f"  -> {record.file}"
        )
    if done.mocks:
        lines.append("  mock roles, nothing recorded: " + ", ".join(done.mocks))
    if done.unpriced_calls and done.unpriced_calls == done.calls:
        total = "no model of the show layer has a price, so its cost is not counted"
    else:
        total = f"the show layer cost {_usd(done.show_microusd)}"
        if done.unpriced_calls:
            total += f" ({done.unpriced_calls} calls have no price and are not in it)"
    if done.cap_microusd is not None:
        total += f" of its {_usd(done.cap_microusd)} cap"
    if done.seats_microusd:
        total += f"; the seats cost {_usd(done.seats_microusd)}"
    lines.append(total)
    if any(record.role == "host" for record in done.roles):
        lines.append(
            f"host: {done.host_fallbacks} of {done.host_texts} texts are played from the fact card"
        )
    if any(record.role.startswith("judge:") for record in done.roles):
        absent = ", ".join(f"judge {a.judge} ({a.reason})" for a in done.absent)
        lines.append(f"judges: {'absent: ' + absent if absent else 'both gave a usable answer'}")
    if done.scan is not None:
        if done.scan.ok:
            lines.append(done.scan.summary())
        else:
            lines.append(
                f"{done.scan.summary()}; `auction-night export` will refuse this night "
                "until they are cleared (`export --clear FILE`)"
            )
            lines += done.scan.lines(_SHOWN_FINDINGS)[1:]
    return lines


# --------------------------------------------------------------------------------- the files


def _target(directory: Path, role: str) -> Path:
    if role == "host":
        return directory / HOST_CALLS_FILE
    if role == "classifier":
        return directory / SCAN_CALLS_FILE
    return directory / JUDGE_CALLS_FILE


def _kind(role: str) -> str:
    """Which cassette a role's calls go into: the judges share one."""
    return {"host": "host", "classifier": "scan"}.get(role, "judge")


def _refuse_existing(paths: Sequence[Path], overwrite: bool) -> None:
    """A recording that is already there is in the way, as the files of an earlier run are."""
    if overwrite:
        return
    for path in paths:
        if path.exists() and path.stat().st_size > 0:
            raise ShowSetupError(f"{path} already exists (use --overwrite to replace it)")


def _write_part(target: Path, text: str) -> Path:
    scratch = target.with_name(target.name + PART_SUFFIX)
    try:
        with scratch.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        with contextlib.suppress(OSError):
            scratch.unlink(missing_ok=True)
        raise ShowSetupError(f"cannot write {scratch}: {exc}") from None
    return scratch


def _check_writable(paths: Sequence[Path]) -> None:
    """Find out before the first call, which costs money, that the files can be written."""
    for path in paths:
        _write_part(path, "").unlink(missing_ok=True)


def _clients_for(
    config: NightConfig,
    roles: Sequence[tuple[str, RoleConfig]],
    environ: Mapping[str, str] | None,
    factories: Mapping[EndpointKind, SdkFactory] | None,
) -> dict[str, LlmClient]:
    names = sorted({role.endpoint for _, role in roles if role.endpoint})
    try:
        return dict(make_clients(config, environ=environ, factories=factories, names=names))
    except MissingCredentialError as exc:
        raise ShowSetupError(str(exc)) from None


# ----------------------------------------------------------------------------- the recording


def record_show(
    config: NightConfig,
    directory: Path,
    *,
    prices: PriceTable | None = None,
    overwrite: bool = False,
    search_dirs: Sequence[Path] = (),
    environ: Mapping[str, str] | None = None,
    factories: Mapping[EndpointKind, SdkFactory] | None = None,
    clients: Mapping[str, LlmClient] | None = None,
    clock: Callable[[], str] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> ShowRecorded:
    """Call the model roles of the show layer on the night of ``directory`` and record them.

    ``directory`` is a run directory (``events.jsonl`` and ``calls.jsonl``, and ``human.jsonl``
    for a night with a person); ``config`` is the configuration the night was run with. The
    recordings go to ``host_calls.jsonl``, ``judge_calls.jsonl`` and ``scan_calls.jsonl`` there,
    for the roles that are models. An existing recording is refused unless ``overwrite`` is set.
    ``prices`` is the price list (none: no call has a price, which a cost cap does not allow).
    ``search_dirs`` are the places the word list of the configuration is looked for, as for the
    export. ``clients`` maps endpoint names to clients and replaces the ones that are made from
    ``environ`` (the process environment by default) and ``factories`` (the SDK constructors).
    ``clock`` and ``sleep`` go to the roles, for tests.

    Raises :class:`ShowSetupError` for what is wrong with the arguments or the files, and
    :class:`ShowFailed` (:class:`RoleCallFailed`, :class:`CostCapReached`, or recordings that the
    export does not read back) when the recording is stopped. In both cases nothing is written.
    The errors of the export of a night (a replay that differs from the log, a recording of the
    seats that does not fit, a replay that is not valid) are raised as they are.
    """
    if not directory.is_dir():
        raise ShowSetupError(f"not a directory: {directory}")
    everyone = show_roles(config)
    roles = [(label, role) for label, role in everyone if role.kind == "llm"]
    mocks = tuple(label for label, role in everyone if role.kind == "mock")
    targets = {label: _target(directory, label) for label, _ in roles}
    files = sorted(set(targets.values()))
    _refuse_existing(files, overwrite)
    table = prices or PriceTable()
    cap = config.limits.max_cost_microusd
    try:
        wanted = sorted({str(role.model) for _, role in roles})
        Ledger(table, cap_microusd=cap).require_prices(wanted)
    except MissingPriceError as exc:
        raise ShowSetupError(str(exc)) from None
    live = dict(clients) if clients is not None else _clients_for(config, roles, environ, factories)
    for _, role in roles:
        if role.endpoint not in live:
            raise ShowSetupError(f"no client for the endpoint {role.endpoint!r}")
    _check_writable(files)

    run = replay_run(config, directory)  # the seats call nothing; a night that differs is refused
    seats = sum(entry.cost_microusd or 0 for entry in run.cassette.entries)
    if not roles:
        return ShowRecorded(config.night.id, (), mocks, (), seats, cap)

    ledger = Ledger(table, cap_microusd=cap)
    ledger.gate()  # a cap of zero closes the gate before the first call
    cassettes = {
        kind: Cassette.record(scrubber=merged_scrubber(live.values()))
        for kind in ("host", "judge", "scan")
    }
    options: dict[str, object] = {}
    if clock is not None:
        options["clock"] = clock
    if sleep is not None:
        options["sleep"] = sleep

    def guard(role: str) -> _Guard:
        return _Guard(role, cassettes[_kind(role)], ledger)

    played = run.config  # the table as it was played, a person seated
    scan_role = played.show.content_scan.classifier
    scan_client = None
    if scan_role is not None and scan_role.kind == "llm":
        assert scan_role.endpoint is not None
        scan_client = live[scan_role.endpoint]
    # the gate is made first: a word list that cannot be found is found before any call is paid
    gate = ContentGate.from_config(
        played,
        search_dirs=(directory, *search_dirs),
        cassette=cassettes["scan"],
        client=scan_client,
        ledger=ledger,
        **options,
    )
    theory = analyse_night(run.state)
    cards = build_fact_cards(run.state, theory=theory)

    host_role = played.show.host_offline
    if host_role.kind == "llm":
        assert host_role.endpoint is not None
        backend = make_host(
            played,
            cassette=cassettes["host"],
            client=live[host_role.endpoint],
            ledger=ledger,
            **options,
        )
        host = run_host(cards, _WatchedHost(backend, guard("host")))
    else:
        host = run_host(cards, make_host(played))

    judges: list[JudgeBackend] = []
    for judge in make_judges(
        played, cassette=cassettes["judge"], clients=live, ledger=ledger, **options
    ):
        judges.append(
            _WatchedJudge(judge, guard(judge.label)) if isinstance(judge, ModelJudge) else judge
        )
    parts = show_parts(played, run.state, cards, backends=judges)

    if scan_client is not None:
        assert gate.classifier is not None
        watched = _WatchedClassifier(gate.classifier, guard("classifier"))
        replay = build_replay(
            played, run.state, host.block, theory=theory, cards=cards, parts=parts
        )
        dataclasses.replace(gate, classifier=watched).scan(replay)

    texts = {label: cassettes[_kind(label)].text() for label, _ in roles}
    written_files, report = _install(directory, config, targets, texts, search_dirs)
    records = tuple(
        RoleRecord(label, str(role.model), targets[label].name, ledger.by_label.get(label, Tally()))
        for label, role in roles
    )
    return ShowRecorded(
        night=config.night.id,
        roles=records,
        mocks=mocks,
        files=written_files,
        seats_microusd=seats,
        cap_microusd=cap,
        scan=report,
        host_texts=len(host.commentary.lots) + 1,
        host_fallbacks=len(host.commentary.fallbacks),
        absent=tuple(parts.judges_absent),
    )


def _install(
    directory: Path,
    config: NightConfig,
    targets: Mapping[str, Path],
    texts: Mapping[str, str],
    search_dirs: Sequence[Path],
) -> tuple[tuple[Path, ...], ScanReport]:
    """Write the recordings as ``.part`` files, check that the export reads them back, and move
    them over their names. Nothing is left behind when a step fails.

    The two judges share one file, so a file is written once. The scan report is the one of the
    export on the new files.
    """
    final: dict[Path, str] = {}
    for label, target in targets.items():
        final.setdefault(target, texts[label])
    scratch: dict[Path, Path] = {}
    try:
        for target, text in final.items():
            scratch[target] = _write_part(target, text)
        report = _readback(directory, config, targets, scratch, search_dirs)
        replaced: list[str] = []
        for target, part in scratch.items():
            try:
                os.replace(part, target)
            except OSError as exc:
                done = ", ".join(replaced) or "none"
                raise ShowSetupError(
                    f"cannot write {target}: {exc} (already replaced: {done})"
                ) from None
            replaced.append(target.name)
    finally:
        for part in scratch.values():
            part.unlink(missing_ok=True)
    return tuple(final), report


def _readback(
    directory: Path,
    config: NightConfig,
    targets: Mapping[str, Path],
    scratch: Mapping[Path, Path],
    search_dirs: Sequence[Path],
) -> ScanReport:
    """Run the export on the files just written, as ``auction-night export`` will, and return
    what its content gate made of the night. Recordings that do not read back are a failure."""

    def part(label: str) -> Path | None:
        return scratch[targets[label]] if label in targets else None

    judge = next((label for label in ("judge:0", "judge:1") if label in targets), "")
    try:
        done = export_directory(
            config,
            directory,
            host_calls=part("host"),
            judge_calls=part(judge),
            scan_calls=part("classifier"),
            search_dirs=search_dirs,
        )
    except ContentScanRejected as refused:
        return refused.report  # the recordings were read; the night is for `export` to refuse
    except CassetteError as exc:
        raise ShowFailed(
            f"the recordings do not read back as the export asks for them: {exc}. "
            "No recording was written."
        ) from None
    return done.report
