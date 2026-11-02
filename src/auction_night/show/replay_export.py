"""Exporting a finished night as replay.json v1.

``build_replay`` assembles the contract's :class:`~auction_night.contracts.replay.Replay` from the
parts a finished night leaves behind, and ``export_replay`` adds the content gate and the file::

    run = replay_run(config, directory)                 # the night again, from its cassette
    theory = analyse_night(run.state)                   # theory: the textbook numbers of each lot
    cards = build_fact_cards(run.state, theory=theory)  # show: facts, as the host reads them
    host = run_host(cards, make_host(config)).block     # show: the offline commentary
    parts = show_parts(run.config, run.state, cards)    # show: judges, findings, awards, highlights
    done = export_replay(run.config, run.state, host, theory=theory, cards=cards, parts=parts,
                         cassettes=[run.cassette], gate=ContentGate.from_config(config))
    path.write_text(done.text, encoding="utf-8", newline="\\n")

Nothing here calls a model. The night is replayed from ``calls.jsonl`` (and the inputs of a person
from ``human.jsonl``), and the exported file is a pure function of the log, the cassettes and the
arguments: the same inputs give the same bytes. Nothing reads a clock, the environment or git
unless the caller hands the result in (:class:`GitInfo`, ``created_utc``).

:func:`export_directory` does all of it, the show-layer parts included: a ``mock`` judge needs
nothing, and a model judge is read from its recording (``judge_calls.jsonl``), like the host and
the classifier.

Where each part comes from
--------------------------
``night``, ``catalog``
    ``night_start`` of the log, the end status from ``night_end``, the mechanism versions from
    the lots that were played. ``ranked`` is true for a completed night in plain mode with nobody
    at the table.
``seats``
    The configuration that was played (a person's seat is ``human``). The model id the API
    returned is the one it returned most often for that seat in the cassette.
``timeline``
    One entry per lot. ``first_seq`` is the ``lot_open`` event and ``last_seq`` the ``hammer``
    event (``hammer_seq`` repeats it, for a reader that looks for the hammer by name).
    ``actions`` holds **every** bid-phase action of the lot in log order: the bids of sealed
    formats, the English exit prices of all seats (the winner's last one is the number the table
    never learns, and the hole-card lens draws it) and the Dutch grab prices of the bots as well
    as of the models. ``talk`` is in speaking order, and ``talk_seqs`` maps each seat to the event
    number of its line, which is where a highlight or a verdict about that line jumps to.
``reveal``
    Budgets, values and appraisals of every seat from the deal, the hidden values of the blind
    boxes, the mission results, the opening plans, the mind-reading guesses with the truth at the
    moment they were asked, and the inner monologues. A monologue belongs to the action event that
    carries it (its ``seq`` is the action's), and an empty one is left out.
``scores``
    ``night_end``, plus the counts of the seat's actions that the log shows: ``actions``,
    ``abstentions`` (other than refusals, a kept English exit price included), ``refusals``,
    ``clamps``, ``invalid_inputs`` and ``syntax_repairs`` (attempts with that repair code) and
    ``valid_guesses``. An action that was never asked for (``not_asked``) or that the cost gate
    skipped (``budget_aborted``) is no action of the seat and is in no count. The other columns
    (the ratios, which need the judges and the award rules) come from the show layer and are put
    over these (``ShowParts.stats``).
``pledges``
    Decided by program from the log (:func:`~auction_night.show.pledges.judge_pledges`).
``usage``
    By role from the cassettes: ``calls`` are attempts (retries included, as in the cost events),
    ``retries`` the attempts after a call's first, ``timeouts`` and ``refusals`` the attempts that
    ended so, ``latency_ms`` the sum over the attempts. The cost is the sum of the recorded costs,
    and absent when an attempt with tokens has no price.
``manifest``
    ``config_hash`` of the configuration as played, the digest and label of the seat templates,
    the digest of the ``act`` tool, the digests of the templates of the host, of the judges and
    of the content classifier (the ones in force, written even when that role is a mock, as the
    digest of the seat templates is for mock seats), the engine version, the git commit if known,
    the mechanism versions, the seed commitment (from the configuration or the log) and, when
    asked for, the revealed seed, which must open it. When a person released findings of the
    content gate (:mod:`auction_night.show.clearance`), the manifest lists them in
    ``cleared_findings``.
the show-layer parts
    ``judges``, ``judges_absent``, ``verdicts``, ``judge_accuracy``, ``awards``,
    ``bots_reference`` and ``highlights`` are made by :func:`show_parts` from the finished night
    and the fact cards, and handed in as :class:`ShowParts`, with the ``stats`` columns of
    ``scores``. A judge that gave no usable answer is not in ``judges`` but in
    ``judges_absent``, with the reason. The recording of a model judge is part of ``usage``.

What is checked
---------------
The pydantic model (and with it the cross-field rules of ``docs/contracts.md``); the premise of
the viewer that a seat speaks once per lot (:func:`check_premises`); that no canary word of a
private mission is in any public text; that every sequence number points into its lot; and, in
``export_replay``, that the content gate passes and that the text parses back into the same
replay.
"""

import asyncio
import re
import subprocess
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from auction_night import __version__
from auction_night.contracts.act import act_tool_fingerprint
from auction_night.contracts.config import NightConfig, SeatConfig, SeedResolutionError, config_hash
from auction_night.contracts.events import (
    ActionData,
    ActionEvent,
    AnyEvent,
    AwardData,
    DutchResultEvent,
    EnglishDropoutEvent,
    GuessEvent,
    HammerEvent,
    JudgeExtractData,
    JudgeVerdictData,
    LotOpenEvent,
    NightEndEvent,
    NightStartEvent,
    ParseRepairEvent,
    PlanEvent,
    SeedCommitData,
    SeedCommitEvent,
    TalkEvent,
    canonical_json,
    seed_commitment,
)
from auction_night.contracts.mechanism import BID_PHASES
from auction_night.contracts.replay import (
    REPLAY_SCHEMA_ID,
    ClearedFinding,
    GuessRecord,
    Highlight,
    HostBlock,
    JudgeAbsence,
    JudgeAccuracy,
    Manifest,
    Replay,
    ReplayNight,
    ReplayScore,
    ReplaySeat,
    Reveal,
    RoleUsage,
    SeatStats,
    ThoughtRecord,
    TimelineAction,
    TimelineLot,
    Usage,
)
from auction_night.engine.muted import MUTED_REASON
from auction_night.engine.night import NightState
from auction_night.llm.cassette import Cassette, CassetteEntry
from auction_night.llm.driver import DriverError
from auction_night.llm.human import HumanSeatError, ReplayedHuman, load_tape, seat_a_person
from auction_night.llm.nightrun import (
    CALLS_FILE,
    EVENTS_FILE,
    HUMAN_FILE,
    build_rig,
    first_difference,
)
from auction_night.prompts import load_templates, templates_digest
from auction_night.show.awards import compute_awards
from auction_night.show.clearance import Clearance, release
from auction_night.show.content_scan import (
    ContentGate,
    ContentScanRejected,
    ScanReport,
    public_texts,
    scan_prompt_digest,
)
from auction_night.show.factcard import FactCards, NightData, build_fact_cards
from auction_night.show.highlights import compute_highlights
from auction_night.show.host import host_templates_digest, make_host, run_host
from auction_night.show.judges import JudgeBackend, judge_templates_digest, make_judges
from auction_night.show.pledges import judge_pledges
from auction_night.show.verdicts import run_judging
from auction_night.theory import LotTheory, analyse_night

__all__ = [
    "HOST_CALLS_FILE",
    "JUDGE_CALLS_FILE",
    "SCAN_CALLS_FILE",
    "ExportError",
    "Exported",
    "GitInfo",
    "PremiseError",
    "RecordingNotFound",
    "ReplayedRun",
    "ShowParts",
    "build_replay",
    "check_premises",
    "check_replay",
    "export_directory",
    "export_replay",
    "git_info",
    "last_finished",
    "play_offline",
    "replay_run",
    "replay_text",
    "show_parts",
    "usage_from_cassettes",
]

HOST_CALLS_FILE: Final = "host_calls.jsonl"  # the recording of a model host, in a run directory
JUDGE_CALLS_FILE: Final = "judge_calls.jsonl"  # that of the model judges
SCAN_CALLS_FILE: Final = "scan_calls.jsonl"  # and that of a model content classifier
_COMMIT: Final = re.compile(r"[0-9a-f]{40}")
_ROLE_FIELDS: Final = (
    "calls",
    "retries",
    "timeouts",
    "refusals",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "latency_ms",
)
_SHOWN: Final = 8  # offenders named in an error message


class ExportError(ValueError):
    """A night cannot be exported, or the export is not a valid replay. The message says why."""


class PremiseError(ExportError):
    """The log breaks a premise of the viewer: a seat spoke twice in a lot, for example."""


class RecordingNotFound(ExportError):
    """A show role is a model, and its recording is not there. Nothing here calls a model."""


# ------------------------------------------------------------------------------- provenance


@dataclass(frozen=True, slots=True)
class GitInfo:
    """The commit the code was at and whether tracked files had uncommitted changes."""

    commit: str | None = None
    dirty: bool = False


def git_info(root: Path) -> GitInfo:
    """The commit of the repository at ``root`` and whether its tracked files are modified.

    Anything that goes wrong (no git, no repository, a commit that is not 40 hex digits) gives an
    empty :class:`GitInfo`: a replay without a commit is valid, one with a made-up commit is not.
    """

    def run(*args: str) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                ["git", "-C", str(root), *args],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    head = run("rev-parse", "HEAD")
    if head is None or head.returncode != 0 or not _COMMIT.fullmatch(head.stdout.strip()):
        return GitInfo()
    status = run("status", "--porcelain", "--untracked-files=no")
    dirty = status is None or status.returncode != 0 or bool(status.stdout.strip())
    return GitInfo(head.stdout.strip(), dirty)


# -------------------------------------------------------------------------- run directories


@dataclass(frozen=True)
class ReplayedRun:
    """A recorded night played again: the configuration as played (a person seated), the final
    state, the cassette it was played from, and the seat of the person if there was one."""

    config: NightConfig
    state: NightState
    cassette: Cassette
    events: int
    human_seat: int | None = None


def replay_run(config: NightConfig, directory: Path) -> ReplayedRun:
    """Play the night of a run directory again from its cassette, with no model and no network.

    ``directory`` holds ``events.jsonl`` and ``calls.jsonl`` and, for a night with a person,
    ``human.jsonl``. The new log must equal ``events.jsonl`` byte for byte and must use every
    recorded attempt; anything else is an :class:`ExportError`. Unlike
    :func:`~auction_night.llm.nightrun.replay_directory`, the state of the finished night is kept.
    The deal seed is needed again, as for the run.
    """
    events_path, calls_path, tape_path = (
        directory / EVENTS_FILE,
        directory / CALLS_FILE,
        directory / HUMAN_FILE,
    )
    missing = [str(path) for path in (events_path, calls_path) if not path.is_file()]
    if missing:
        raise ExportError("not found: " + ", ".join(missing))
    humans: dict[int, ReplayedHuman] | None = None
    human_seat: int | None = None
    if tape_path.is_file():
        tape = load_tape(tape_path)
        if tape.night != config.night.id:
            raise ExportError(
                f"the input record belongs to night {tape.night!r}, not {config.night.id!r}"
            )
        try:
            config, human_seat = seat_a_person(config, tape.seat)
        except HumanSeatError as exc:
            raise ExportError(str(exc)) from None
        humans = {human_seat: ReplayedHuman(tape)}
    elif any(seat.kind == "human" for seat in config.seats):
        raise ExportError(f"the night had a person, but {tape_path} (their inputs) is missing")
    cassette = Cassette.replay(calls_path)
    try:
        rig = build_rig(config, cassette, humans=humans)
        runner = rig.runner()
        try:
            result = asyncio.run(rig.play(runner))
        finally:
            runner.close()
    except (DriverError, SeedResolutionError) as exc:
        raise ExportError(str(exc)) from None
    difference = first_difference(events_path.read_bytes(), result.events_text().encode("utf-8"))
    unused = len(cassette.unused())
    if difference is None and unused:
        difference = f"{unused} recorded attempts were never asked for"
    if difference is None and humans and next(iter(humans.values())).unused():
        difference = "recorded inputs of the person were never asked for"
    if difference is not None:
        raise ExportError(f"the replay of the night differs from {EVENTS_FILE}: {difference}")
    return ReplayedRun(config, rig.driver.state, cassette, len(result.events), human_seat)


def play_offline(
    config: NightConfig, *, clock: Callable[[], str] | None = None
) -> tuple[NightState, Cassette]:
    """Play a night in memory and return its final state and the cassette it recorded.

    Only for nights without a model seat (mock seats and bots): nothing is sent anywhere. A fixed
    ``clock`` (a function returning a time stamp) makes the stamps of the cassette repeatable.
    """
    if any(seat.kind in ("llm", "human") for seat in config.seats):
        raise ExportError("a night with a model or a person at the table cannot be played offline")
    cassette = Cassette.record()
    rig = build_rig(config, cassette)
    runner = rig.runner(**({} if clock is None else {"clock": clock}))
    try:
        asyncio.run(rig.play(runner))
    finally:
        runner.close()
    return rig.driver.state, cassette


# --------------------------------------------------------------------------------- the log


@dataclass
class _Log:
    """The events of a finished night, sorted into what the replay needs."""

    start: NightStartEvent
    end: NightEndEvent
    opens: dict[int, LotOpenEvent] = field(default_factory=dict)
    hammers: dict[int, HammerEvent] = field(default_factory=dict)
    dutch: dict[int, DutchResultEvent] = field(default_factory=dict)
    talks: dict[int, list[TalkEvent]] = field(default_factory=lambda: defaultdict(list))
    bids: dict[int, list[ActionEvent]] = field(default_factory=lambda: defaultdict(list))
    dropouts: dict[int, list[EnglishDropoutEvent]] = field(
        default_factory=lambda: defaultdict(list)
    )
    plans: list[PlanEvent] = field(default_factory=list)
    actions: list[ActionEvent] = field(default_factory=list)
    guesses: list[GuessEvent] = field(default_factory=list)
    repairs: list[ParseRepairEvent] = field(default_factory=list)
    commits: list[SeedCommitData] = field(default_factory=list)

    @classmethod
    def of(cls, events: Iterable[AnyEvent]) -> "_Log":
        start = end = None
        rest: list[AnyEvent] = []
        for event in events:
            if isinstance(event, NightStartEvent):
                start = event
            elif isinstance(event, NightEndEvent):
                end = event
            else:
                rest.append(event)
        if start is None or end is None:
            raise ExportError("the log has no night_start or no night_end event")
        log = cls(start, end)
        for event in rest:
            lot = event.lot
            if isinstance(event, LotOpenEvent) and lot is not None:
                log.opens[lot] = event
            elif isinstance(event, HammerEvent) and lot is not None:
                log.hammers[lot] = event
            elif isinstance(event, DutchResultEvent) and lot is not None:
                log.dutch[lot] = event
            elif isinstance(event, TalkEvent) and lot is not None:
                log.talks[lot].append(event)
            elif isinstance(event, EnglishDropoutEvent) and lot is not None:
                log.dropouts[lot].append(event)
            elif isinstance(event, ActionEvent):
                log.actions.append(event)
                if lot is not None and event.data.phase in BID_PHASES:
                    log.bids[lot].append(event)
            elif isinstance(event, PlanEvent):
                log.plans.append(event)
            elif isinstance(event, GuessEvent):
                log.guesses.append(event)
            elif isinstance(event, ParseRepairEvent):
                log.repairs.append(event)
            elif isinstance(event, SeedCommitEvent):
                log.commits.append(event.data)
        return log


def check_premises(events: Sequence[AnyEvent]) -> list[str]:
    """The ways the log breaks what the viewer relies on. An empty list means it holds.

    The page pairs the inner monologue and the mind-reading guess of a seat with the line it
    spoke by ``(lot, seat)``, so each seat must speak once per lot:

    * at most one ``talk`` event, one ``guess`` event and one ``talk``-phase action per
      ``(lot, seat)``, and at most one ``plan`` event and one ``plan`` action per seat;
    * the lines of a lot come in the speaking order that ``lot_open`` announced.
    """
    talks: Counter[tuple[int, int]] = Counter()
    guesses: Counter[tuple[int, int]] = Counter()
    voices: Counter[tuple[int, int]] = Counter()
    plans: Counter[int] = Counter()
    plan_actions: Counter[int] = Counter()
    order: dict[int, list[int]] = defaultdict(list)
    announced: dict[int, list[int]] = {}
    for event in events:
        lot = event.lot
        if isinstance(event, LotOpenEvent) and lot is not None:
            announced[lot] = list(event.data.talk_order)
        elif isinstance(event, TalkEvent) and lot is not None:
            talks[lot, event.data.seat] += 1
            order[lot].append(event.data.seat)
        elif isinstance(event, GuessEvent) and lot is not None:
            guesses[lot, event.data.seat] += 1
        elif isinstance(event, PlanEvent):
            plans[event.data.seat] += 1
        elif isinstance(event, ActionEvent):
            if event.data.phase == "talk" and lot is not None:
                voices[lot, event.data.seat] += 1
            elif event.data.phase == "plan":
                plan_actions[event.data.seat] += 1
    problems: list[str] = []
    per_lot = (("talk events", talks), ("guesses", guesses), ("talk actions", voices))
    for label, counts in per_lot:
        problems += [
            f"lot {lot}, seat {seat}: {count} {label}"
            for (lot, seat), count in sorted(counts.items())
            if count > 1
        ]
    for label, per_seat in (("plan events", plans), ("plan actions", plan_actions)):
        problems += [f"seat {seat}: {n} {label}" for seat, n in sorted(per_seat.items()) if n > 1]
    for lot, seats in sorted(order.items()):
        if lot in announced and seats != announced[lot]:
            problems.append(f"lot {lot}: the lines came in the order {seats}, not {announced[lot]}")
    return problems


# -------------------------------------------------------------------------------- cassettes


def usage_from_cassettes(*cassettes: Cassette) -> Usage:
    """Calls and tokens by role (``seat:3``, ``host``, ``judge:0``, ``classifier``) and in total.

    See the module text for what each column counts. A role that has no attempt has no row.
    """
    rows: dict[str, dict[str, int]] = {}
    cost = 0
    unpriced = False
    seen = False
    for cassette in cassettes:
        for entry in cassette.entries:
            seen = True
            row = rows.setdefault(entry.role, dict.fromkeys(_ROLE_FIELDS, 0))
            row["calls"] += 1
            row["retries"] += int(entry.key.attempt > 1)
            row["timeouts"] += int(entry.outcome == "timeout")
            row["refusals"] += int(entry.outcome == "refusal")
            row["latency_ms"] += entry.latency_ms or 0
            tally = entry.usage_tally
            if tally is not None:
                row["input_tokens"] += tally.input_tokens
                row["output_tokens"] += tally.output_tokens
                row["cache_read_tokens"] += tally.cache_read_input_tokens
                row["cache_write_tokens"] += tally.cache_write_input_tokens
                row["reasoning_tokens"] += tally.reasoning_tokens or 0
                unpriced = unpriced or entry.cost_microusd is None
            cost += entry.cost_microusd or 0
    total = dict.fromkeys(_ROLE_FIELDS, 0)
    for row in rows.values():
        for name, value in row.items():
            total[name] += value
    return Usage(
        by_role={role: RoleUsage(**rows[role]) for role in sorted(rows)},
        total=RoleUsage(**total),
        cost_microusd=cost if seen and not unpriced else None,
    )


def last_finished(*cassettes: Cassette) -> str | None:
    """The latest ``finished_at`` of any attempt, or None. A run directory's own time stamp."""
    stamps = [e.finished_at for c in cassettes for e in c.entries if e.finished_at]
    return max(stamps) if stamps else None


def _model_ids(
    cassettes: Sequence[Cassette],
) -> tuple[dict[str, str], dict[str, str]]:
    """The model id asked for and the one returned most often, by role."""
    asked: dict[str, str] = {}
    returned: dict[str, Counter[str]] = defaultdict(Counter)
    entries: list[CassetteEntry] = sorted(
        (e for c in cassettes for e in c.entries), key=lambda e: e.key.sort_key
    )
    for entry in entries:
        asked.setdefault(entry.role, entry.model)
        if entry.model_returned:
            returned[entry.role][entry.model_returned] += 1
    best = {
        role: min(counts.items(), key=lambda pair: (-pair[1], pair[0]))[0]
        for role, counts in returned.items()
    }
    return asked, best


# ------------------------------------------------------------------------------- the parts


@dataclass(frozen=True)
class ShowParts:
    """The parts of the replay that other blocks of the show layer make. All are optional and
    default to empty.

    ``stats`` maps a seat to fields of its :class:`SeatStats`, which are put over the counts that
    the log gives (``SeatMetrics.stats_update()`` of the awards module has this form). Use it for
    the columns that need the judges and the award rules. ``judges_absent`` lists the judges that
    gave no usable answer (:func:`~auction_night.show.judges.judge_absences`).
    """

    judges: Sequence[JudgeExtractData] = ()
    verdicts: Sequence[JudgeVerdictData] = ()
    judge_accuracy: Sequence[JudgeAccuracy] = ()
    awards: Sequence[AwardData] = ()
    bots_reference: Sequence[AwardData] = ()
    highlights: Sequence[Highlight] = ()
    stats: Mapping[int, Mapping[str, int | None]] = field(default_factory=dict)
    judges_absent: Sequence[JudgeAbsence] = ()


def show_parts(
    config: NightConfig,
    state: NightState,
    cards: FactCards,
    *,
    judge_cassette: Cassette | None = None,
    backends: Sequence[JudgeBackend] | None = None,
) -> ShowParts:
    """The show-layer parts of a finished night: judges, findings, awards, highlights, stat columns.

    The two judges of the configuration read the night (``run_judging``), and the awards and the
    highlights are scored from the fact cards and the findings, so the parts always agree with each
    other. A ``mock`` judge needs nothing. A model judge is read from ``judge_cassette``, the
    recording of the judges in replay mode, and nothing here calls a model; without a recording
    that is :class:`RecordingNotFound`. ``backends`` replaces the judges of the configuration (a
    test hands in scripted ones). The result is a pure function of its arguments.
    """
    if backends is not None:
        judges = tuple(backends)
    else:
        if judge_cassette is None and any(role.kind == "llm" for role in config.show.judges):
            raise RecordingNotFound(
                "a judge is a model, so its recording is needed (nothing here calls a model)"
            )
        judges = make_judges(config, cassette=judge_cassette)
    judging = run_judging(state, judges)
    awards = compute_awards(cards, judging.verdicts)
    return ShowParts(
        judges=judging.judges,
        verdicts=judging.verdicts,
        judge_accuracy=judging.accuracy,
        awards=awards.models,
        bots_reference=awards.bots,
        highlights=compute_highlights(cards, state.events, judging.verdicts),
        stats={seat: metrics.stats_update() for seat, metrics in awards.metrics.items()},
        judges_absent=judging.run.absences,
    )


def _version_of(ref: str) -> str:
    return ref.split("@", 1)[1]


def _night(config: NightConfig, log: _Log, human: bool) -> ReplayNight:
    start = log.start.data
    versions = {}
    for entry in start.lots:
        versions.setdefault(entry.format, _version_of(entry.mechanism))
    season = config.season
    return ReplayNight(
        id=start.night_id,
        lang=start.lang,
        mode=start.mode,
        status=log.end.data.status,
        ranked=log.end.data.status == "completed" and not human and start.mode == "plain",
        n_lots=start.n_lots,
        mechanisms=versions,
        budget_lo=start.budget_lo,
        budget_hi=start.budget_hi,
        collectors=start.collectors,
        shills=start.shills,
        mission_bonus=start.mission_bonus,
        season_id=season.id,
        deck_id=season.deck,
        rotation=season.rotation,
    )


def _seat_entry(
    seat: SeatConfig, asked: Mapping[str, str], returned: Mapping[str, str]
) -> ReplaySeat:
    role = f"seat:{seat.seat}"
    base = {"seat": seat.seat, "codename": seat.codename, "kind": seat.kind}
    if seat.kind == "llm":
        return ReplaySeat(
            **base,
            vendor=seat.vendor,
            endpoint=seat.endpoint,
            model=seat.model,
            model_returned=returned.get(role),
            persona=seat.persona,
            thinking=seat.thinking.type if seat.thinking else None,
            effort=seat.effort,
        )
    if seat.kind == "mock":
        return ReplaySeat(
            **base,
            model=asked.get(role),
            model_returned=returned.get(role),
            persona=seat.persona,
            policy=seat.mock_policy,
        )
    return ReplaySeat(**base, policy=seat.policy)


def _action(event: ActionEvent) -> TimelineAction:
    data = event.data
    return TimelineAction(
        seq=event.seq,
        seat=data.seat,
        phase=data.phase,
        status=data.status,
        reason=data.reason,
        value=data.value,
        raw_value=data.raw_value,
    )


def _timeline(log: _Log, theory: Sequence[LotTheory], host: HostBlock) -> list[TimelineLot]:
    lines = {line.lot: line.text for line in host.per_lot}
    played = sorted(log.opens)
    if len(played) != len(theory):
        raise ExportError("there must be one set of reference numbers for every lot")
    lots = []
    for number, reference in zip(played, theory, strict=True):
        if reference.lot != number:
            raise ExportError(f"the reference numbers of lot {reference.lot} stand at lot {number}")
        opened, hammer = log.opens[number], log.hammers.get(number)
        if hammer is None:
            raise ExportError(f"lot {number} has no hammer")
        data = opened.data
        dutch = log.dutch.get(number)
        lots.append(
            TimelineLot(
                lot=number,
                format=data.format,
                mechanism=data.mechanism,
                first_seq=opened.seq,
                last_seq=hammer.seq,
                active=list(data.active),
                sitting_out=list(data.sitting_out),
                talk_order=list(data.talk_order),
                talk=[event.data for event in log.talks.get(number, [])],
                actions=[_action(event) for event in log.bids.get(number, [])],
                dropouts=[event.data for event in log.dropouts.get(number, [])],
                dutch=None if dutch is None else dutch.data,
                hammer=hammer.data,
                overlay=reference.overlay(),
                host=lines.get(number),
                talk_seqs={event.data.seat: event.seq for event in log.talks.get(number, [])},
                hammer_seq=hammer.seq,
            )
        )
    return lots


def _reveal(state: NightState, log: _Log) -> Reveal:
    deal = state.deal
    cards = deal.cards()
    thoughts = [
        ThoughtRecord(
            seq=event.seq,
            lot=event.lot,
            seat=event.data.seat,
            phase=event.data.phase,
            text=event.data.thought,
        )
        for event in log.actions
        if event.data.thought
    ]
    return Reveal(
        budgets={card.seat: card.budget for card in cards},
        values={card.seat: dict(card.values) for card in cards},
        signals={card.seat: dict(card.signals) for card in cards if card.signals},
        true_values={lot.lot: lot.blind_value for lot in deal.lots if lot.blind_value is not None},
        missions=list(state.missions),
        plans=[event.data for event in log.plans],
        guesses=[
            GuessRecord(
                lot=g.lot,
                seat=g.seat,
                target=g.target,
                guess=g.guess,
                truth=g.truth,
                abs_error=g.abs_error,
            )  # fmt: skip
            for g in state.guesses
        ],
        thoughts=thoughts,
    )


def _counted(data: ActionData) -> bool:
    return not (data.status == "abstain" and data.reason in ("not_asked", MUTED_REASON))


def _stats(log: _Log, extra: Mapping[int, Mapping[str, int | None]]) -> dict[int, SeatStats]:
    """The counts of each seat that the log shows, with the fields of ``extra`` put over them."""
    counts: dict[int, Counter[str]] = defaultdict(Counter)
    for event in log.actions:
        data = event.data
        if not _counted(data):
            continue
        row = counts[data.seat]
        row["actions"] += 1
        row["clamps"] += int(data.status == "clamped")
        row["refusals"] += int(data.status == "abstain" and data.reason == "refusal")
        row["abstentions"] += int(
            data.status == "kept_previous"
            or (data.status == "abstain" and data.reason != "refusal")
        )
    for repair in log.repairs:
        if repair.data.seat is not None:
            row = counts[repair.data.seat]
            row["invalid_inputs"] += int("invalid_input" in repair.data.codes)
            row["syntax_repairs"] += int("syntax" in repair.data.codes)
    for guess in log.guesses:
        counts[guess.data.seat]["valid_guesses"] += 1
    found = {seat: SeatStats(**row) for seat, row in counts.items()}
    for seat, update in extra.items():
        found[seat] = found.get(seat, SeatStats()).model_copy(update=dict(update))
    return found


def _scores(log: _Log, stats: Mapping[int, SeatStats]) -> list[ReplayScore]:
    return [
        ReplayScore(**score.model_dump(), stats=stats.get(score.seat, SeatStats()))
        for score in log.end.data.scores
    ]


def _seed_fields(
    config: NightConfig, log: _Log, reveal_seed: bool
) -> tuple[str | None, str | None]:
    """The seed commitment and the revealed seed for the manifest.

    The commitment comes from the configuration (when it is a real digest) and from the log, and
    the two must agree. The seed is revealed when the log carries a ``reveal`` event or the caller
    asks for it, and then it must open the commitment. A configuration with an inline seed (a mock
    or test night, where there is nothing to hide) that never published a commitment gets the
    digest of its seed; a ``seed_env`` configuration without a commitment cannot reveal its seed.
    """
    night = config.night
    published = night.deal_seed_commitment
    from_config = published if published and published.startswith("sha256:") else None
    from_log = next((c.commitment for c in log.commits if c.phase == "commit"), None)
    if from_config and from_log and from_config != from_log:
        raise ExportError("the commitment in the configuration and in the log differ")
    commitment = from_config or from_log
    revealed = next((c.seed for c in log.commits if c.phase == "reveal"), None)
    if revealed is None and reveal_seed:
        try:
            revealed = night.resolve_seed()
        except SeedResolutionError as exc:
            raise ExportError(str(exc)) from None
    if revealed is None:
        return commitment, None
    if commitment is None:
        if night.seed is None:
            raise ExportError("a seed can only be revealed against a published commitment")
        commitment = seed_commitment(revealed)
    if seed_commitment(revealed) != commitment:
        raise ExportError("the revealed seed does not open the commitment")
    return commitment, revealed


def _manifest(
    config: NightConfig,
    log: _Log,
    returned: Mapping[str, str],
    git: GitInfo,
    created_utc: str | None,
    reveal_seed: bool,
    night: ReplayNight,
    cleared: Sequence[ClearedFinding] = (),
) -> Manifest:
    lang = config.night.lang
    commitment, revealed = _seed_fields(config, log, reveal_seed)
    return Manifest(
        config_hash=config_hash(config),
        prompt_template_version=load_templates(lang).version,
        prompt_hash=templates_digest(lang),
        tool_schema_hash=act_tool_fingerprint(),
        engine_version=__version__,
        git_commit=git.commit,
        git_dirty=git.dirty,
        mechanism_versions=dict(night.mechanisms),
        seed_commitment=commitment,
        revealed_seed=revealed,
        models_returned=dict(sorted(returned.items())),
        created_utc=created_utc,
        host_prompt_hash=host_templates_digest(lang),
        judge_prompt_hash=judge_templates_digest(lang),
        scan_prompt_hash=scan_prompt_digest(),
        cleared_findings=list(cleared),
    )


# ----------------------------------------------------------------------------- the replay


def build_replay(
    config: NightConfig,
    state: NightState,
    host: HostBlock | None = None,
    *,
    theory: Sequence[LotTheory] | None = None,
    cards: FactCards | None = None,
    parts: ShowParts | None = None,
    cassettes: Sequence[Cassette] = (),
    usage: Usage | None = None,
    git: GitInfo | None = None,
    created_utc: str | None = None,
    reveal_seed: bool = False,
    cleared: Sequence[ClearedFinding] = (),
) -> Replay:
    """Assemble the replay of a finished night. See the module text for the sources.

    ``config`` is the configuration the night was played with (with a person seated, if there was
    one). ``host`` is the ``host`` block of the show layer (none: no commentary). ``theory`` is
    :func:`~auction_night.theory.analyse_night` of the state, computed when not given, and
    ``cards`` the fact cards built from it (the pledges are read from them; otherwise the program
    judges them again). ``cassettes`` are the recordings of the night and of the show stages: they
    give the usage by role and the model ids that the APIs returned (``usage`` replaces the
    former). ``git`` and ``created_utc`` go into the manifest as given. With ``reveal_seed`` the
    deal seed is published in the manifest. ``cleared`` are the findings of the content gate that
    a person released, which the manifest lists.

    Raises :class:`PremiseError` when the log breaks a premise of the viewer, and
    :class:`ExportError` for everything else that makes the replay unfit to publish.
    """
    if not state.done:
        raise ExportError("the night is not over")
    problems = check_premises(state.events)
    if problems:
        shown = "; ".join(problems[:_SHOWN])
        raise PremiseError(
            f"the log breaks a premise of the viewer ({len(problems)} problem(s)): {shown}"
        )
    log = _Log.of(state.events)
    reference = list(analyse_night(state) if theory is None else theory)
    host = host or HostBlock()
    parts = parts or ShowParts()
    asked, returned = _model_ids(cassettes)
    night = _night(config, log, any(seat.kind == "human" for seat in config.seats))
    pledges = judge_pledges(state.events)
    if cards is not None:
        card = cards.night.data
        assert isinstance(card, NightData)
        pledges = list(card.pledges)
    seats = sorted(config.seats, key=lambda seat: seat.seat)
    replay = Replay(
        schema_id=REPLAY_SCHEMA_ID,
        night=night,
        seats=[_seat_entry(seat, asked, returned) for seat in seats],
        catalog=list(log.start.data.lots),
        timeline=_timeline(log, reference, host),
        reveal=_reveal(state, log),
        scores=_scores(log, _stats(log, parts.stats)),
        manifest=_manifest(
            config, log, returned, git or GitInfo(), created_utc, reveal_seed, night, cleared
        ),
        pledges=pledges,
        judges=list(parts.judges),
        judges_absent=list(parts.judges_absent),
        verdicts=list(parts.verdicts),
        judge_accuracy=list(parts.judge_accuracy),
        awards=list(parts.awards),
        bots_reference=list(parts.bots_reference),
        highlights=list(parts.highlights),
        host=host,
        usage=usage if usage is not None else usage_from_cassettes(*cassettes),
    )
    _refuse(check_replay(replay) + _canary_problems(replay, state))
    return replay


def _refuse(problems: Sequence[str]) -> None:
    if problems:
        shown = "; ".join(problems[:_SHOWN])
        raise ExportError(f"the replay is not fit to publish ({len(problems)} problem(s)): {shown}")


def _canary_problems(replay: Replay, state: NightState) -> list[str]:
    """Places where the canary word of a private mission appears in a public text."""
    words = {seat: mission.canary for seat, mission in state.deal.missions.items()}
    problems = []
    for item in public_texts(replay):
        folded = item.text.casefold()
        for seat, word in sorted(words.items()):
            if word.casefold() in folded:
                problems.append(f"{item.path} holds the canary word of the mission of seat {seat}")
    return problems


def check_replay(replay: Replay) -> list[str]:
    """Problems beyond the contract's own rules; an empty list means the replay is sound.

    Every sequence number must point into its lot: actions and the lines of the channel between
    the opening and the hammer (``talk_seqs`` names the line of each seat that spoke), the hammer
    at the end of the lot (``hammer_seq`` is ``last_seq``), a pledge in the lot where it was made,
    a highlight inside its lot; the monologues of the plans before the first lot. Every seat
    named in the timeline must sit at the table, and a monologue of a bid phase must belong to a
    recorded action. A judge is either in ``judges`` or in ``judges_absent``, never in both.
    """
    problems: list[str] = []
    seats = {seat.seat for seat in replay.seats}
    ranges = {lot.lot: (lot.first_seq, lot.last_seq) for lot in replay.timeline}
    previous = 0
    bids: dict[int, tuple[int, str, int]] = {}
    for lot in replay.timeline:
        first, last = lot.first_seq, lot.last_seq
        if first <= previous:
            problems.append(f"lot {lot.lot} opens at event {first}, not after event {previous}")
        previous = last
        named = {*lot.active, *lot.sitting_out, *lot.talk_order, *(t.seat for t in lot.talk)}
        named |= {a.seat for a in lot.actions} | {d.seat for d in lot.dropouts}
        if not named <= seats:
            problems.append(f"lot {lot.lot} names a seat that is not at the table")
        for action in lot.actions:
            if not first < action.seq < last:
                problems.append(f"lot {lot.lot}: action {action.seq} is outside the lot")
            bids[action.seq] = (action.seat, action.phase, lot.lot)
        spoke = {line.seat for line in lot.talk}
        if lot.talk_seqs and set(lot.talk_seqs) != spoke:
            problems.append(f"lot {lot.lot}: talk_seqs and the lines are not the same seats")
        for seat, seq in sorted(lot.talk_seqs.items()):
            if not first < seq < last:
                problems.append(f"lot {lot.lot}: the line of seat {seat} (event {seq}) is outside")
        if lot.hammer_seq is not None and lot.hammer_seq != last:
            problems.append(f"lot {lot.lot}: hammer_seq {lot.hammer_seq} is not last_seq {last}")
    opening = min((first for first, _ in ranges.values()), default=0)
    for thought in replay.reveal.thoughts:
        span = None if thought.lot is None else ranges.get(thought.lot)
        if thought.lot is None:
            if thought.phase != "plan" or thought.seq >= opening:
                problems.append(f"thought {thought.seq} has no lot but is not an opening plan")
        elif span is None or not span[0] < thought.seq < span[1]:
            problems.append(f"thought {thought.seq} is outside lot {thought.lot}")
        elif (
            thought.phase in BID_PHASES
            and bids.get(thought.seq) != (thought.seat, thought.phase, thought.lot)
        ):  # fmt: skip
            problems.append(f"thought {thought.seq} belongs to no recorded action")
    for record in replay.pledges:
        span = ranges.get(record.lot)
        if span is None or not span[0] < record.seq < span[1]:
            problems.append(f"pledge at event {record.seq} is outside lot {record.lot}")
    for light in replay.highlights:
        span = None if light.lot is None else ranges.get(light.lot)
        if span is not None and not span[0] <= light.first_seq <= light.last_seq <= span[1]:
            problems.append(f"highlight {light.id} is not inside lot {light.lot}")
    for line in replay.host.per_lot:
        if line.lot not in ranges:
            problems.append(f"the host has a line for lot {line.lot}, which was not played")
    gave = {extract.judge for extract in replay.judges}
    for absence in replay.judges_absent:
        if absence.judge in gave:
            problems.append(f"judge {absence.judge} is both in judges and in judges_absent")
    return problems


def replay_text(replay: Replay) -> str:
    """The file text of a replay: canonical JSON and a final newline. Same replay, same bytes."""
    return canonical_json(replay) + "\n"


# ------------------------------------------------------------------------------ the export


@dataclass(frozen=True)
class Exported:
    """A replay that passed the gate: the model, the text of the file, and the scan report."""

    replay: Replay
    text: str
    report: ScanReport


def export_replay(
    config: NightConfig,
    state: NightState,
    host: HostBlock | None = None,
    *,
    gate: ContentGate,
    theory: Sequence[LotTheory] | None = None,
    cards: FactCards | None = None,
    parts: ShowParts | None = None,
    cassettes: Sequence[Cassette] = (),
    usage: Usage | None = None,
    git: GitInfo | None = None,
    created_utc: str | None = None,
    reveal_seed: bool = False,
    clearances: Sequence[Clearance] = (),
) -> Exported:
    """Build the replay, pass it through the content gate and serialise it.

    The arguments are those of :func:`build_replay`. The gate runs on the assembled replay; a
    night that fails it is refused with ``ContentScanRejected`` (from
    :mod:`auction_night.show.content_scan`) and nothing is returned. The recordings the
    classifier made (``cassettes`` should include its cassette) are counted in the usage of the
    final replay. The text is checked to parse back into the same replay.

    ``clearances`` are the findings that a person has read and released
    (:mod:`auction_night.show.clearance`). The night is refused as before when a finding is left
    after them, and a clearance that matches no finding of the scan raises
    :class:`~auction_night.show.clearance.ClearanceError`. The released findings are in
    ``manifest.cleared_findings`` of the replay and in ``cleared`` of the report.
    """
    reference = list(analyse_night(state) if theory is None else theory)

    def build(cleared: Sequence[ClearedFinding] = ()) -> Replay:
        return build_replay(
            config,
            state,
            host,
            theory=reference,
            cards=cards,
            parts=parts,
            cassettes=cassettes,
            usage=usage,
            git=git,
            created_utc=created_utc,
            reveal_seed=reveal_seed,
            cleared=cleared,
        )

    report = gate.scan(build())
    cleared: tuple[ClearedFinding, ...] = ()
    if clearances:
        report, cleared = release(report, clearances)
    if not report.ok:
        raise ContentScanRejected(report)
    final = build(cleared)  # again: the classifier's recording is part of the usage now
    text = replay_text(final)
    if Replay.model_validate_json(text) != final:
        raise ExportError("the serialised replay does not parse back into the same replay")
    return Exported(final, text, report)


# ------------------------------------------------------------------------- a run directory


def _recording(path: Path | None, default: Path, role: str, used: bool) -> Cassette | None:
    """The recording of a show role that is a model, in replay mode; None for any other role."""
    if not used:
        return None
    found = path if path is not None else default
    if not found.is_file():
        raise RecordingNotFound(
            f"the {role} is a model, so its recording is needed: {found} does not exist "
            "(nothing here calls a model)"
        )
    return Cassette.replay(found)


def export_directory(
    config: NightConfig,
    directory: Path,
    *,
    host_calls: Path | None = None,
    scan_calls: Path | None = None,
    wordlist: Path | None = None,
    search_dirs: Sequence[Path] = (),
    git: GitInfo | None = None,
    created_utc: str | None = None,
    reveal_seed: bool = False,
    parts_for: Callable[[NightConfig, NightState, FactCards], ShowParts | None] | None = None,
    judge_calls: Path | None = None,
    clearances: Sequence[Clearance] = (),
) -> Exported:
    """Export the night of a run directory: the whole path from the files to the checked text.

    The night is played again from the cassette (:func:`replay_run`), the fact cards, the offline
    host and the judges are run (:func:`show_parts`), and the replay goes through the content gate
    of the configuration. A ``mock`` host, judge or classifier needs nothing. A model host, judge
    or classifier is read from its recording, ``host_calls`` or ``host_calls.jsonl``,
    ``judge_calls`` or ``judge_calls.jsonl`` and ``scan_calls`` or ``scan_calls.jsonl`` in the
    directory (a missing one is :class:`RecordingNotFound`). ``wordlist`` is a list in addition to
    the configured one, which is looked for in the working directory, in ``directory`` and in
    ``search_dirs``.

    ``created_utc`` is the time of the last recorded call when not given. ``parts_for`` replaces
    :func:`show_parts` as the maker of the show-layer parts (judges, awards, highlights) from the
    configuration, the state and the fact cards; it may return None for a replay without them.
    ``clearances`` are the findings of the content gate that a person released, as
    :func:`export_replay` takes them.
    Raises :class:`ExportError` and its subclasses,
    :class:`~auction_night.show.content_scan.ContentScanRejected`,
    :class:`~auction_night.show.content_scan.ContentScanError` and
    :class:`~auction_night.show.clearance.ClearanceError`, and
    :class:`~auction_night.llm.cassette.CassetteError` for a recording that does not fit.
    """
    show = config.show
    scan_role = show.content_scan.classifier
    host_cassette = _recording(
        host_calls, directory / HOST_CALLS_FILE, "offline host", show.host_offline.kind == "llm"
    )
    judge_cassette = _recording(
        judge_calls,
        directory / JUDGE_CALLS_FILE,
        "judge",
        any(role.kind == "llm" for role in show.judges),
    )
    scan_cassette = _recording(
        scan_calls,
        directory / SCAN_CALLS_FILE,
        "content classifier",
        scan_role is not None and scan_role.kind == "llm",
    )
    run = replay_run(config, directory)
    theory = analyse_night(run.state)
    cards = build_fact_cards(run.state, theory=theory)
    host = run_host(cards, make_host(run.config, cassette=host_cassette))
    gate = ContentGate.from_config(
        run.config,
        search_dirs=(directory, *search_dirs),
        wordlist=wordlist,
        cassette=scan_cassette,
    )
    cassettes = [
        c for c in (run.cassette, host_cassette, judge_cassette, scan_cassette) if c is not None
    ]
    if parts_for is None:
        parts: ShowParts | None = show_parts(
            run.config, run.state, cards, judge_cassette=judge_cassette
        )
    else:
        parts = parts_for(run.config, run.state, cards)
    return export_replay(
        run.config,
        run.state,
        host.block,
        gate=gate,
        theory=theory,
        cards=cards,
        parts=parts,
        cassettes=cassettes,
        git=git,
        created_utc=created_utc if created_utc is not None else last_finished(*cassettes),
        reveal_seed=reveal_seed,
        clearances=clearances,
    )
