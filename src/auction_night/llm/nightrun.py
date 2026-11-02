"""Running a configured night from end to end, and replaying it from its cassette.

This is the layer the command line stands on. :func:`build_rig` assembles everything one night
needs from a configuration and a cassette: the deal, the mock seats (if any), the driver, the
session and the clients. :func:`play_night` plays it. :func:`run_directory` records a night into a
directory (``events.jsonl`` and ``calls.jsonl``) and :func:`replay_directory` plays the night
again from that cassette, without any client, and compares the new log with the old one byte for
byte.

A night with a person at a seat is played by :func:`play_directory`. Besides the log and the
cassette it writes ``human.jsonl``, the record of what the person typed (see
:mod:`auction_night.llm.human`). The person's seat need not be a ``human`` seat in the
configuration: it is named at the command line and takes the place of a model seat. A replay finds
the record next to the log and answers for the person from it, so it asks nobody anything.

A replay needs the same configuration and the same deal seed as the run. The seed of a published
season lives in an environment variable, never in a file, so a replay of such a night needs the
variable too. Everything else comes from the cassette, and from the record of the person if there
was one.

The command line saves the configuration file next to the log, byte for byte, as ``config.yaml``
(``config_file`` of :func:`run_directory` and :func:`play_directory`), so that a replay, an export
and a season find it in the directory of the night. :func:`config_path_for` names the file that
such a command reads.
"""

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from auction_night.contracts.config import (
    EndpointKind,
    NightConfig,
    SeedResolutionError,
    find_placeholders,
    load_config,
)
from auction_night.contracts.events import NightStartEvent
from auction_night.engine.deal import Deal, deal
from auction_night.engine.night import ItemInfo
from auction_night.engine.rules_text import common_rule_text, rule_with_example
from auction_night.llm.accounting import MissingPriceError, PriceTable
from auction_night.llm.cassette import Cassette
from auction_night.llm.client import (
    LlmClient,
    MissingCredentialError,
    SdkFactory,
    make_clients,
    merged_scrubber,
)
from auction_night.llm.driver import DriverError, EngineDriver, NightResult, llm_routes
from auction_night.llm.human import (
    HumanPlayer,
    HumanSeat,
    HumanSeatError,
    HumanTapeError,
    InputClosed,
    InputSource,
    Presenter,
    ReplayedHuman,
    TapeWriter,
    interruptible,
    load_tape,
    recap_lines,
    seat_a_person,
)
from auction_night.llm.mock import MockModels, MockOptions
from auction_night.llm.orchestrate import ModelRunner, NightSession, RunnerSettings
from auction_night.llm.personas import PERSONAS
from auction_night.llm.render import Persona, RenderSettings

__all__ = [
    "CALLS_FILE",
    "CONFIG_FILE",
    "EVENTS_FILE",
    "HUMAN_FILE",
    "NightRig",
    "NightRunError",
    "ReplayReport",
    "build_rig",
    "config_path_for",
    "describe",
    "first_difference",
    "load_night_config",
    "play_directory",
    "play_night",
    "real_clients",
    "replay_directory",
    "run_directory",
]

EVENTS_FILE = "events.jsonl"
CALLS_FILE = "calls.jsonl"
HUMAN_FILE = "human.jsonl"
CONFIG_FILE = "config.yaml"
_SHOWN_LINES = 12


class NightRunError(RuntimeError):
    """A night cannot be run or replayed as asked. The message is meant for the person at the
    command line."""


# ------------------------------------------------------------------------------- the rig


@dataclass
class NightRig:
    """Everything one night needs. ``clients`` maps endpoint names to the clients that a runner
    must be given (mock clients included); a replay needs none."""

    config: NightConfig
    driver: EngineDriver
    session: NightSession
    clients: dict[str, LlmClient] = field(default_factory=dict)
    mock: MockModels | None = None

    def runner(self, **options: Any) -> ModelRunner:
        """A runner for this night. ``options`` go to :class:`ModelRunner`."""
        return ModelRunner(self.clients, **options)

    async def play(self, runner: ModelRunner | None = None) -> NightResult:
        """Play the night through and close its log. A runner that is passed in stays open."""
        own = runner is None
        active = runner or self.runner()
        try:
            run = await active.run_night(self.driver, self.session)
        finally:
            if own:
                active.close()
        return self.driver.finish(run)


def real_clients(
    config: NightConfig,
    *,
    environ: Mapping[str, str] | None = None,
    factories: Mapping[EndpointKind, SdkFactory] | None = None,
    http_client: Any = None,
) -> dict[str, LlmClient]:
    """The clients of the endpoints that the ``llm`` seats of ``config`` call.

    The endpoints of the show roles (host, judges, classifier) are left out: a night does not
    call them, so their keys need not be set.
    """
    names = sorted({s.endpoint for s in config.seats if s.kind == "llm" and s.endpoint})
    return dict(
        make_clients(
            config, environ=environ, factories=factories, http_client=http_client, names=names
        )
    )


def build_rig(
    config: NightConfig,
    cassette: Cassette,
    *,
    the_deal: Deal | None = None,
    mock: MockOptions | None = None,
    clients: Mapping[str, LlmClient] | None = None,
    prices: PriceTable | None = None,
    runner_settings: RunnerSettings | None = None,
    render_settings: RenderSettings | None = None,
    items: Sequence[ItemInfo] | None = None,
    talk_offset: int = 0,
    personas: Mapping[str, Persona] | None = None,
    humans: Mapping[int, HumanPlayer] | None = None,
    environ: Mapping[str, str] | None = None,
    factories: Mapping[EndpointKind, SdkFactory] | None = None,
    http_client: Any = None,
) -> NightRig:
    """Assemble the driver, the session and the clients of one night.

    The deal is drawn from the configuration's seed unless ``the_deal`` is given. Recording a
    night creates the clients of its ``llm`` seats from the environment (or uses ``clients``),
    which fails when a key is missing. Replaying creates none. A configuration with ``mock`` seats
    gets mock models, shaped by ``mock``. A ``human`` seat needs its person in ``humans``. The
    personas of a variety night are those of :data:`~auction_night.llm.personas.PERSONAS` unless
    ``personas`` says otherwise.
    """
    recording = cassette.mode == "record"
    the_deal = the_deal or deal(config)
    models = (
        MockModels(config, the_deal, mock) if any(s.kind == "mock" for s in config.seats) else None
    )
    routes = {**llm_routes(config), **(models.routes if models else {})}
    driver = EngineDriver(
        config,
        the_deal,
        routes,
        items=items,
        talk_offset=talk_offset,
        personas=PERSONAS if personas is None else personas,
        humans=humans,
        settings=render_settings,
    )
    if models is not None:
        models.bind(lambda: driver.request)

    session = NightSession.open(config, cassette, prices=prices, settings=runner_settings)
    live: dict[str, LlmClient] = {}
    if recording:
        live = dict(
            clients
            if clients is not None
            else real_clients(config, environ=environ, factories=factories, http_client=http_client)
        )
        cassette.scrubber = cassette.scrubber.merged(merged_scrubber(live.values()))
    if models is not None:
        session.endpoints = {**session.endpoints, **models.endpoints}
        if recording:
            live.update(models.clients)
            session.ledger.require_prices(models.models)
    return NightRig(config, driver, session, live, models)


def play_night(
    config: NightConfig,
    cassette: Cassette,
    *,
    runner_options: Mapping[str, Any] | None = None,
    **rig: Any,
) -> NightResult:
    """Assemble a night with :func:`build_rig` (``rig`` holds its options) and play it."""
    built = build_rig(config, cassette, **rig)
    runner = built.runner(**dict(runner_options or {}))
    try:
        return asyncio.run(built.play(runner))
    finally:
        runner.close()


# ------------------------------------------------------------------------ the config file


def load_night_config(path: Path) -> NightConfig:
    """Read a configuration for a run or a replay, with a message for each way it can fail."""
    import yaml

    if not path.is_file():
        raise NightRunError(f"configuration not found: {path}")
    try:
        config = load_config(path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        lines = str(exc).splitlines()
        raise NightRunError(f"cannot read {path}:\n" + "\n".join(lines[:_SHOWN_LINES])) from None
    placeholders = find_placeholders(config)
    if placeholders:
        shown = ", ".join(placeholders[:_SHOWN_LINES])
        raise NightRunError(f"{path} still holds placeholders to replace before a run: {shown}")
    return config


def config_path_for(directory: Path, given: Path | None = None) -> Path:
    """The configuration file of the night recorded in ``directory``.

    That is ``given`` when the caller named a file, and otherwise the ``config.yaml`` that a run
    or a play saved there. Without ``given``, a directory that does not exist or holds no such file
    is a :class:`NightRunError`. The file itself is read by :func:`load_night_config`.
    """
    if given is not None:
        return given
    if not directory.is_dir():
        raise NightRunError(f"not a directory: {directory}")
    stored = directory / CONFIG_FILE
    if not stored.is_file():
        raise NightRunError(
            f"no configuration: give --config, or put the configuration of the night in "
            f"{directory} as {CONFIG_FILE}"
        )
    return stored


def _config_copy(config_file: Path | None, out: Path) -> tuple[Path, bytes] | None:
    """Where the configuration file is to be saved in ``out`` and what is to be saved there.

    The bytes are read now, before the night starts, so that a file that is edited while the night
    runs does not change what is saved. None when no file was given, and also when the file is the
    ``config.yaml`` of ``out`` itself (a night run again in its own directory).
    """
    if config_file is None:
        return None
    target = out / CONFIG_FILE
    try:
        if target.exists() and config_file.samefile(target):
            return None
        return target, config_file.read_bytes()
    except OSError as exc:
        raise NightRunError(f"cannot read {config_file}: {exc}") from None


def _save_config(copy: tuple[Path, bytes] | None) -> None:
    """Write the bytes that :func:`_config_copy` read to the place it named."""
    if copy is None:
        return
    target, data = copy
    try:
        target.write_bytes(data)
    except OSError as exc:
        raise NightRunError(f"cannot write {target}: {exc}") from None


# ----------------------------------------------------------------------------- running


def run_directory(
    config: NightConfig,
    out: Path,
    *,
    prices: PriceTable | None = None,
    overwrite: bool = False,
    mock: MockOptions | None = None,
    environ: Mapping[str, str] | None = None,
    factories: Mapping[EndpointKind, SdkFactory] | None = None,
    config_file: Path | None = None,
) -> NightResult:
    """Play the night of ``config`` live and write ``events.jsonl`` and ``calls.jsonl`` to ``out``.

    Earlier files in ``out`` are refused unless ``overwrite`` is set. The cassette grows while
    the night is played, so a night that fails halfway leaves the calls it paid for on disk. A
    configuration with a ``human`` seat is refused: that night is played with
    :func:`play_directory`.

    ``config_file`` is the file that ``config`` was read from. Given, it is saved in ``out`` as
    ``config.yaml``, byte for byte, once the night has been played. An earlier ``config.yaml``
    is one of the files of an earlier run: it is refused, or with ``overwrite`` removed when the
    new run starts, so that a failed rerun leaves no configuration of another night behind. Not
    given, no configuration is saved and an existing ``config.yaml`` is left alone.
    """
    human = [seat.seat for seat in config.seats if seat.kind == "human"]
    if human:
        raise NightRunError(f"seat {human[0]} is a human seat: take it with `auction-night play`")
    events_path, calls_path, tape_path = _outputs(out)
    saved = _config_copy(config_file, out)
    earlier = (events_path, calls_path, tape_path) + (() if saved is None else (saved[0],))
    _refuse_existing(earlier, overwrite)
    try:
        config.night.resolve_seed()
        clients = real_clients(config, environ=environ, factories=factories)
    except (SeedResolutionError, MissingCredentialError) as exc:
        raise NightRunError(str(exc)) from None
    out.mkdir(parents=True, exist_ok=True)
    events_path.unlink(missing_ok=True)  # an earlier log must not outlive a failed rerun
    tape_path.unlink(missing_ok=True)  # nor the inputs of a person who is no longer at the table
    if saved is not None:
        saved[0].unlink(missing_ok=True)  # nor the configuration of the night before
    cassette = Cassette.record(
        calls_path, overwrite=True, scrubber=merged_scrubber(clients.values())
    )
    try:
        result = play_night(config, cassette, prices=prices, clients=clients, mock=mock)
    except (DriverError, MissingPriceError) as exc:
        _discard_if_empty(calls_path)
        raise NightRunError(str(exc)) from None
    _save_config(saved)
    events_path.write_text(result.events_text(), encoding="utf-8", newline="\n")
    return result


def _outputs(out: Path) -> tuple[Path, Path, Path]:
    """The three files of a night in a directory: log, cassette and the inputs of a person."""
    return out / EVENTS_FILE, out / CALLS_FILE, out / HUMAN_FILE


def _refuse_existing(paths: Sequence[Path], overwrite: bool) -> None:
    if overwrite:
        return
    for path in paths:
        if path.exists() and path.stat().st_size > 0:
            raise NightRunError(f"{path} already exists (use --overwrite to replace it)")


def _discard_if_empty(cassette: Path) -> None:
    """Remove a cassette that holds no call: a night that could not be set up leaves none."""
    if cassette.is_file() and cassette.stat().st_size == 0:
        cassette.unlink()


def play_directory(
    config: NightConfig,
    out: Path,
    *,
    source: InputSource,
    presenter: Presenter,
    seat: int | None = None,
    timeout_s: float | None = None,
    prices: PriceTable | None = None,
    overwrite: bool = False,
    mock: MockOptions | None = None,
    environ: Mapping[str, str] | None = None,
    factories: Mapping[EndpointKind, SdkFactory] | None = None,
    clock: Callable[[], float] = time.monotonic,
    config_file: Path | None = None,
) -> NightResult:
    """Play the night of ``config`` live with a person at a seat, and write the files to ``out``.

    The person takes the place of the model seat ``seat`` (or sits at the ``human`` seat that the
    configuration names, when ``seat`` is None). ``source`` is the person's keyboard and
    ``presenter`` the screen. ``timeout_s`` is the time for each phase; without it the limit is
    ``limits.human_timeout_s`` of the configuration, and without that there is none. It does not
    touch the timeout of the model calls.

    Besides ``events.jsonl`` and ``calls.jsonl`` the directory gets ``human.jsonl``, the record of
    the person's inputs that a replay needs. All three are written as the night goes, except the
    log, which is written at the end. When the input ends before the night does, the night is
    abandoned and the error says so. A night with a person is not ranked.

    ``config_file`` is saved in ``out`` as ``config.yaml`` once the night is over, exactly as for
    :func:`run_directory`. It is the configuration as the file has it, so the seat of the person is
    still the model seat of the file: a replay finds the seat in ``human.jsonl``.
    """
    try:
        seated, number = seat_a_person(config, seat)
    except HumanSeatError as exc:
        raise NightRunError(str(exc)) from None
    events_path, calls_path, tape_path = _outputs(out)
    saved = _config_copy(config_file, out)
    earlier = (events_path, calls_path, tape_path) + (() if saved is None else (saved[0],))
    _refuse_existing(earlier, overwrite)
    try:
        seated.night.resolve_seed()
        clients = real_clients(seated, environ=environ, factories=factories)
    except (SeedResolutionError, MissingCredentialError) as exc:
        raise NightRunError(str(exc)) from None
    out.mkdir(parents=True, exist_ok=True)
    events_path.unlink(missing_ok=True)  # an earlier log must not outlive a failed rerun
    if saved is not None:
        saved[0].unlink(missing_ok=True)  # nor the configuration of the night before
    limit = timeout_s if timeout_s is not None else seated.limits.human_timeout_s
    cassette = Cassette.record(
        calls_path, overwrite=True, scrubber=merged_scrubber(clients.values())
    )
    with TapeWriter(tape_path, seated.night.id, number) as tape:
        person = HumanSeat(number, source, presenter, timeout_s=limit, clock=clock, tape=tape)
        try:
            with interruptible():
                result = play_night(
                    seated,
                    cassette,
                    humans={number: person},
                    prices=prices,
                    clients=clients,
                    mock=mock,
                )
        except (DriverError, MissingPriceError) as exc:
            _discard_if_empty(calls_path)
            raise NightRunError(str(exc)) from None
        except InputClosed:
            kept = " and ".join(path.name for path in (calls_path, tape_path) if path.exists())
            raise NightRunError(
                "the input ended before the night did, so the night is abandoned"
                + (f" ({kept} keep what was played)" if kept else "")
            ) from None
    _save_config(saved)
    events_path.write_text(result.events_text(), encoding="utf-8", newline="\n")
    settings = RenderSettings.from_config(seated, rule_with_example, common_rules=common_rule_text)
    presenter.summary("这一夜", recap_lines(result.events, number, settings))
    return result


@dataclass(frozen=True)
class ReplayReport:
    """The outcome of a replay. ``matches`` is True when the new log equals the old one byte for
    byte and the replay used every recorded attempt (and every recorded input of a person).
    ``difference`` says where they part."""

    matches: bool
    events: int
    unused_attempts: int
    difference: str | None = None
    unused_inputs: int = 0


def _lines(data: bytes) -> list[str]:
    lines = data.decode("utf-8", errors="replace").split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # the log ends with a newline
    return lines


def first_difference(expected: bytes, actual: bytes) -> str | None:
    """Where two logs part, as a short sentence; None when they are the same bytes."""
    if expected == actual:
        return None
    old, new = _lines(expected), _lines(actual)
    for number, (a, b) in enumerate(zip(old, new, strict=False), start=1):
        if a != b:
            return f"line {number} differs: recorded {a[:120]!r}, replayed {b[:120]!r}"
    return f"the recorded log has {len(old)} lines and the replayed one {len(new)}"


def replay_directory(config: NightConfig, source: Path) -> ReplayReport:
    """Play the night again from ``source/calls.jsonl`` and compare with ``source/events.jsonl``.

    No client is created and no model is called. A cassette that does not fit the night (a prompt
    template or the configuration changed since the recording) raises
    :class:`~auction_night.llm.cassette.CassetteDrift`, and a missing attempt
    :class:`~auction_night.llm.cassette.CassetteMiss`. When ``source`` holds ``human.jsonl`` the
    night had a person: the seat in the record is played by the record, and nobody is asked
    anything. A record that does not fit the night raises
    :class:`~auction_night.llm.human.HumanTapeError`, a kind of cassette error.
    """
    events_path, calls_path, tape_path = _outputs(source)
    missing = [str(path) for path in (events_path, calls_path) if not path.is_file()]
    if missing:
        raise NightRunError("not found: " + ", ".join(missing))
    recorded: ReplayedHuman | None = None
    humans: dict[int, HumanPlayer] | None = None
    if tape_path.is_file():
        tape = load_tape(tape_path)
        if tape.night != config.night.id:
            raise HumanTapeError(
                f"the input record belongs to night {tape.night!r}, not {config.night.id!r}"
            )
        try:
            config, number = seat_a_person(config, tape.seat)
        except HumanSeatError as exc:
            raise NightRunError(str(exc)) from None
        recorded = ReplayedHuman(tape)
        humans = {number: recorded}
    elif any(seat.kind == "human" for seat in config.seats):
        raise NightRunError(f"the night had a person, but {tape_path} (their inputs) is missing")
    cassette = Cassette.replay(calls_path)
    try:
        result = play_night(config, cassette, humans=humans)
    except (DriverError, SeedResolutionError) as exc:
        raise NightRunError(str(exc)) from None
    expected = events_path.read_bytes()
    actual = result.events_text().encode("utf-8")
    unused = len(cassette.unused())
    unused_inputs = recorded.unused() if recorded is not None else 0
    difference = first_difference(expected, actual)
    if difference is None and unused:
        difference = f"{unused} recorded attempts were never asked for"
    if difference is None and unused_inputs:
        difference = f"{unused_inputs} recorded inputs of the person were never asked for"
    return ReplayReport(difference is None, len(result.events), unused, difference, unused_inputs)


# ------------------------------------------------------------------------------- report


def describe(result: NightResult) -> list[str]:
    """A short account of a night for the terminal: status, size, cost and the scores."""
    start = result.events[0]
    assert isinstance(start, NightStartEvent)
    names = {seat.seat: seat.codename for seat in start.data.seats}
    counters = result.run.counters
    attempts = sum(int(c.get("attempts", 0)) for c in counters.values())
    lines = [
        f"night {result.night}: {result.status}",
        f"events {len(result.events)}, model attempts {attempts}, "
        f"cost {result.run.spent_microusd} micro-USD",
    ]
    if result.run.status == "budget_aborted" and result.status != "budget_aborted":
        # the engine marks every night whose cap skipped an action, so this would be a bug
        lines.append("warning: the cost cap was reached, yet the log says the night was completed")
    for score in result.scores:
        rank = "bot" if score.rank_tonight is None else f"rank {score.rank_tonight}"
        lines.append(
            f"  seat {score.seat} {names[score.seat]}: surplus {score.surplus}, "
            f"mission bonus {score.mission_bonus}, total {score.total} ({rank})"
        )
    return lines
