"""The export of a night as replay.json: what is in it, where it comes from, what stops it.

The night is the mock night, played once offline. Most checks compare the replay with the engine
itself (the final state, the records of the lots, the deal) and not with a second reading of the
log, so a field that is read from the wrong place shows up.
"""

import dataclasses
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from export_support import (
    NO_GIT,
    STAMP,
    config_with,
    mock_night,
    play,
    raw_config,
    replay_json,
    with_events,
)
from judge_support import FakeJudgeClient, ScriptedJudge

import auction_night
from auction_night.contracts import NightConfig, Replay, canonical_json
from auction_night.contracts.config import config_hash
from auction_night.contracts.events import (
    AWARD_KEYS,
    ActionEvent,
    AnyEvent,
    AwardData,
    GuessEvent,
    JudgeVerdictData,
    SeedCommitData,
    SeedCommitEvent,
    TalkEvent,
    dumps_events,
    seed_commitment,
)
from auction_night.contracts.mechanism import BID_PHASES
from auction_night.contracts.replay import (
    Highlight,
    HostBlock,
    JudgeAbsence,
    JudgeAccuracy,
    Usage,
)
from auction_night.llm.cassette import CallKey, Cassette, CassetteEntry, CassetteError
from auction_night.llm.nightrun import CALLS_FILE, EVENTS_FILE, HUMAN_FILE, run_directory
from auction_night.prompts import templates_digest
from auction_night.show.content_scan import ContentGate, public_texts, scan_prompt_digest
from auction_night.show.host import host_templates_digest
from auction_night.show.judge_input import build_judge_input
from auction_night.show.judges import (
    JudgeBackend,
    MockJudge,
    judge_job,
    judge_templates_digest,
    make_judges,
    run_judges,
)
from auction_night.show.pledges import judge_pledges
from auction_night.show.replay_export import (
    JUDGE_CALLS_FILE,
    ExportError,
    GitInfo,
    PremiseError,
    RecordingNotFound,
    ShowParts,
    build_replay,
    check_premises,
    check_replay,
    export_directory,
    export_replay,
    git_info,
    last_finished,
    play_offline,
    replay_run,
    replay_text,
    show_parts,
    usage_from_cassettes,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "llm"))
from human_support import sit  # noqa: E402

PLAYERS = (1, 2, 3, 4, 5, 6)
BOTS = (7, 8)


def build(**options: Any) -> Replay:
    """The replay of the mock night, with the options that a test changes."""
    night = mock_night()
    options.setdefault("git", NO_GIT)
    options.setdefault("cards", night.cards)
    return build_replay(
        night.config,
        night.state,
        options.pop("host", night.host.block),
        theory=night.theory,
        cassettes=[night.cassette],
        **options,
    )


@pytest.fixture(scope="module")
def replay() -> Replay:
    return build()


def lot_of(replay: Replay, number: int) -> Any:
    return next(lot for lot in replay.timeline if lot.lot == number)


# ================================================================================== the shape


def test_the_replay_is_valid_and_survives_the_file(replay: Replay) -> None:
    text = replay_text(replay)
    assert text == canonical_json(replay) + "\n" and text.endswith("}\n")
    assert Replay.model_validate_json(text) == replay
    assert replay.schema_id == "auction-night/replay@1"
    json.loads(text, parse_float=lambda s: pytest.fail(f"a float in the file: {s}"))


def test_the_same_night_exports_the_same_bytes_however_often_it_is_played() -> None:
    first = replay_text(build())
    assert replay_text(build()) == first
    again = play(config_with())  # a night played from the start again, with a new cassette
    second = build_replay(
        again.config,
        again.state,
        again.host.block,
        theory=again.theory,
        cards=again.cards,
        cassettes=[again.cassette],
        git=NO_GIT,
    )
    assert replay_text(second) == first


def test_night_and_catalog_come_from_the_log(replay: Replay) -> None:
    night = mock_night()
    start = night.state.events[0].data
    assert replay.night.id == "mock-0001" and replay.night.n_lots == 12
    assert (replay.night.status, replay.night.mode, replay.night.lang) == (
        "completed",
        "plain",
        "zh",
    )
    assert (replay.night.budget_lo, replay.night.budget_hi) == (start.budget_lo, start.budget_hi)
    assert (replay.night.collectors, replay.night.shills) == (3, 3)
    assert replay.night.mission_bonus == 150
    assert replay.night.mechanisms == dict.fromkeys(replay.night.mechanisms, "v1")
    assert set(replay.night.mechanisms) == {lot.format for lot in replay.catalog}
    assert list(replay.catalog) == list(start.lots)
    assert replay.night.ranked is True, "a completed night with nobody at the table counts"
    assert (replay.night.season_id, replay.night.deck_id, replay.night.rotation) == (None,) * 3


def test_seats_are_the_configuration_as_played(replay: Replay) -> None:
    kinds = {seat.seat: seat.kind for seat in replay.seats}
    assert kinds == {**dict.fromkeys(PLAYERS, "mock"), **dict.fromkeys(BOTS, "bot")}
    assert [seat.codename for seat in replay.seats] == [
        s.codename for s in mock_night().config.seats
    ]
    mock = replay.seats[0]
    assert (mock.policy, mock.model, mock.model_returned) == (
        "honest",
        "mock-honest",
        "mock-honest",
    )
    assert mock.vendor is None and mock.endpoint is None
    bot = replay.seats[7]
    assert (bot.policy, bot.model, bot.model_returned) == ("economist", None, None)


# =============================================================================== the timeline


def test_a_lot_runs_from_its_opening_to_its_hammer(replay: Replay) -> None:
    events = mock_night().state.events
    opens = {e.lot: e.seq for e in events if e.type == "lot_open"}
    hammers = {e.lot: e.seq for e in events if e.type == "hammer"}
    assert [lot.lot for lot in replay.timeline] == list(range(1, 13))
    for lot in replay.timeline:
        assert (lot.first_seq, lot.last_seq) == (opens[lot.lot], hammers[lot.lot])
        assert lot.mechanism == f"{lot.format}@v1"
    assert check_replay(replay) == []


def test_the_lines_of_a_lot_come_in_speaking_order(replay: Replay) -> None:
    for lot in replay.timeline:
        assert [t.seat for t in lot.talk] == lot.talk_order
        assert sorted(lot.active + lot.sitting_out) == sorted(lot.talk_order), "everyone speaks"


def test_every_seat_that_spoke_has_the_event_number_of_its_line_and_the_lot_its_hammer(
    replay: Replay,
) -> None:
    events = {e.seq: e for e in mock_night().state.events}
    for lot in replay.timeline:
        assert set(lot.talk_seqs) == {t.seat for t in lot.talk} == set(lot.talk_order)
        for line in lot.talk:
            event = events[lot.talk_seqs[line.seat]]
            assert isinstance(event, TalkEvent) and event.lot == lot.lot
            assert event.data == line, "the number points at the line of that seat"
        seqs = [lot.talk_seqs[seat] for seat in lot.talk_order]
        assert seqs == sorted(set(seqs)), "in speaking order, and all different"
        assert lot.first_seq < seqs[0] and seqs[-1] < lot.last_seq
        hammer = events[lot.hammer_seq]
        assert hammer.type == "hammer" and hammer.lot == lot.lot and lot.hammer_seq == lot.last_seq
        assert hammer.data == lot.hammer


def test_a_lot_holds_every_bid_action_in_log_order(replay: Replay) -> None:
    events = mock_night().state.events
    for lot in replay.timeline:
        logged = [
            (e.seq, e.data.seat, e.data.phase, e.data.status, e.data.value, e.data.raw_value)
            for e in events
            if isinstance(e, ActionEvent) and e.lot == lot.lot and e.data.phase in BID_PHASES
        ]
        held = [(a.seq, a.seat, a.phase, a.status, a.value, a.raw_value) for a in lot.actions]
        assert held == logged, f"lot {lot.lot}"
    bids = [e for e in events if isinstance(e, ActionEvent) and e.data.phase in BID_PHASES]
    assert sum(len(lot.actions) for lot in replay.timeline) == len(bids)


def test_the_exit_price_of_an_english_winner_is_in_the_timeline(replay: Replay) -> None:
    """The lens draws it from the winner's last English action, so it must be the engine's."""
    state = mock_night().state
    english = [r for r in state.records if r.outcome.hammer.format == "english"]
    assert len(english) == 2
    for record in english:
        lot, winner = lot_of(replay, record.lot), record.outcome.hammer.winner
        assert winner is not None and record.outcome.winner_exit is not None
        mine = [a for a in lot.actions if a.seat == winner and a.value is not None]
        assert {a.phase for a in mine} == {"english_init", "english_revise"}
        assert max(mine, key=lambda a: a.seq).value == record.outcome.winner_exit
        assert record.outcome.winner_exit >= record.outcome.hammer.price
        assert len(lot.dropouts) == len(record.outcome.dropouts)


def test_the_grab_prices_of_the_bots_in_a_dutch_lot_are_in_the_timeline(replay: Replay) -> None:
    state = mock_night().state
    dutch = [r for r in state.records if r.outcome.hammer.format == "dutch"]
    assert len(dutch) == 2
    bots_seen = set()
    for record in dutch:
        lot = lot_of(replay, record.lot)
        held = {a.seat: a.value for a in lot.actions if a.phase == "dutch"}
        assert held == {r.seat: r.value for r in record.outcome.records}
        assert set(held) == set(lot.active)
        bots_seen |= set(held) & set(BOTS)
        assert lot.dutch == record.outcome.dutch
    assert bots_seen, "a bot named a grab price"


def test_the_clocks_and_the_hammer_are_the_ones_of_the_log(replay: Replay) -> None:
    state = mock_night().state
    for record in state.records:
        lot = lot_of(replay, record.lot)
        assert lot.hammer == record.outcome.hammer
        assert lot.dropouts == list(record.outcome.dropouts)
        assert (lot.dutch is not None) == (record.outcome.hammer.format == "dutch")


def test_the_overlay_is_the_theory_of_the_lot(replay: Replay) -> None:
    for lot, numbers in zip(replay.timeline, mock_night().theory, strict=True):
        assert lot.overlay == numbers.overlay()
    curses = [lot.overlay.curse_gap for lot in replay.timeline if lot.format == "blind_box"]
    assert any(gap is not None for gap in curses)


def test_the_host_line_of_a_lot_is_in_the_timeline_and_the_block(replay: Replay) -> None:
    block = mock_night().host.block
    assert replay.host == block
    assert [line.lot for line in block.per_lot] == list(range(1, 13))
    for lot in replay.timeline:
        assert lot.host == next(line.text for line in block.per_lot if line.lot == lot.lot)
    without = build(host=None)
    assert without.host == HostBlock() and all(lot.host is None for lot in without.timeline)


# ================================================================================== the reveal


def test_the_reveal_holds_the_hole_cards_of_every_seat(replay: Replay) -> None:
    night = mock_night()
    deal = night.state.deal
    reveal = replay.reveal
    assert reveal.budgets == deal.budgets and set(reveal.budgets) == set(range(1, 9))
    for seat in range(1, 9):
        card = deal.card(seat)
        assert reveal.values[seat] == card.values
        assert reveal.signals.get(seat, {}) == card.signals
    blind = {lot.lot for lot in deal.lots if lot.is_blind}
    assert set(reveal.true_values) == blind
    assert all(reveal.true_values[n] == deal.lot(n).blind_value for n in blind)
    assert reveal.missions == list(night.state.missions) and len(reveal.missions) == 6
    assert {p.seat for p in reveal.plans} == set(PLAYERS)
    assert [(g.lot, g.seat, g.target, g.guess, g.truth) for g in reveal.guesses] == [
        (g.lot, g.seat, g.target, g.guess, g.truth) for g in night.state.guesses
    ]
    assert all(g.abs_error == abs(g.guess - g.truth) for g in reveal.guesses)


def test_a_monologue_belongs_to_the_action_that_carries_it(replay: Replay) -> None:
    actions = {e.seq: e for e in mock_night().state.events if isinstance(e, ActionEvent)}
    said = {seq for seq, e in actions.items() if e.data.thought}
    assert {t.seq for t in replay.reveal.thoughts} == said and len(said) > 100
    for thought in replay.reveal.thoughts:
        event = actions[thought.seq]
        assert (thought.seat, thought.phase, thought.lot) == (
            event.data.seat,
            event.data.phase,
            event.lot,
        )
        assert thought.text == event.data.thought
        assert (thought.lot is None) == (thought.phase == "plan")
    assert [t.seq for t in replay.reveal.thoughts] == sorted(said)
    assert not {t.seat for t in replay.reveal.thoughts} & set(BOTS), "a bot has no monologue"


def test_each_seat_speaks_once_per_lot_so_a_monologue_and_a_reading_pair_with_a_line(
    replay: Replay,
) -> None:
    for lot in replay.timeline:
        assert Counter(t.seat for t in lot.talk) == dict.fromkeys(range(1, 9), 1)
        for seat in PLAYERS:
            talk_thoughts = [
                t
                for t in replay.reveal.thoughts
                if (t.lot, t.seat, t.phase) == (lot.lot, seat, "talk")
            ]
            readings = [g for g in replay.reveal.guesses if (g.lot, g.seat) == (lot.lot, seat)]
            assert len(talk_thoughts) == 1 and len(readings) == 1
    assert check_premises(mock_night().state.events) == []


# ================================================================================ the scores


def test_scores_are_the_closing_tally_and_carry_the_counts_of_the_log(replay: Replay) -> None:
    night = mock_night()
    end = next(e for e in night.state.events if e.type == "night_end")
    assert [
        (s.seat, s.surplus, s.mission_bonus, s.total, s.rank_tonight, s.spent, s.lots_won)
        for s in replay.scores
    ] == [
        (s.seat, s.surplus, s.mission_bonus, s.total, s.rank_tonight, s.spent, s.lots_won)
        for s in end.data.scores
    ]
    for score in replay.scores:
        mine = [
            e
            for e in night.state.events
            if isinstance(e, ActionEvent) and e.data.seat == score.seat
        ]
        guesses = [
            e for e in night.state.events if isinstance(e, GuessEvent) and e.data.seat == score.seat
        ]
        assert score.stats.actions == len(mine)
        assert score.stats.valid_guesses == len(guesses)
        assert score.stats.clamps == sum(e.data.status == "clamped" for e in mine)
        assert score.stats.abstentions == sum(
            e.data.status in ("abstain", "kept_previous") for e in mine
        )
        assert score.stats.refusals == 0
    clamped = [
        e for e in night.state.events if isinstance(e, ActionEvent) and e.data.status == "clamped"
    ]
    assert clamped and sum(s.stats.clamps for s in replay.scores) == len(clamped)
    repairs = [e for e in night.state.events if e.type == "parse_repair"]
    syntax = sum("syntax" in e.data.codes for e in repairs)
    invalid = sum("invalid_input" in e.data.codes for e in repairs)
    assert sum(s.stats.syntax_repairs for s in replay.scores) == syntax
    assert sum(s.stats.invalid_inputs for s in replay.scores) == invalid


def test_the_stats_of_the_show_layer_are_put_over_the_counts_of_the_log(replay: Replay) -> None:
    update = {"mind_error_milli": 205, "consistency_milli": 900, "judgable_lots": 10, "clamps": 99}
    made = build(parts=ShowParts(stats={1: update}))
    by_seat = {score.seat: score.stats for score in made.scores}
    plain = {score.seat: score.stats for score in replay.scores}
    assert by_seat[1] == plain[1].model_copy(update=update)
    assert by_seat[1].actions == plain[1].actions > 0, "the counts of the log stay"
    assert by_seat[2] == plain[2]


def test_pledges_are_the_ones_the_program_judged(replay: Replay) -> None:
    assert replay.pledges == judge_pledges(mock_night().state.events)
    assert {p.outcome for p in replay.pledges} >= {"kept", "violated"}
    assert build(cards=None).pledges == replay.pledges


# =================================================================================== the usage


def entry(role: str, step: int, attempt: int = 1, **fields: Any) -> CassetteEntry:
    """One attempt of a recorded call: ``step`` tells calls apart, ``attempt`` is the try."""
    seat = int(role.removeprefix("seat:")) if role.startswith("seat:") else None
    values: dict[str, Any] = {
        "key": CallKey("mock-0001", 1, "sealed", step, seat, attempt),
        "role": role,
        "kind": "openai_compatible",
        "model": "m",
        "request_hash": "sha256:" + "0" * 64,
        "request": {},
        "outcome": "ok",
        "model_returned": "m-0001",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 50,
            "cache_write_input_tokens": 10,
            "reasoning_tokens": 5,
        },
        "cost_microusd": 7,
        "latency_ms": 40,
    }
    values.update(fields)
    return CassetteEntry(**values)


def test_usage_is_counted_by_role_from_the_cassette(replay: Replay) -> None:
    night = mock_night()
    assert set(replay.usage.by_role) == {f"seat:{n}" for n in PLAYERS}
    calls = Counter(e.role for e in night.cassette.entries)
    assert {role: row.calls for role, row in replay.usage.by_role.items()} == dict(calls)
    assert replay.usage.total.calls == len(night.cassette) > 100
    summed = sum(row.input_tokens for row in replay.usage.by_role.values())
    assert replay.usage.total.input_tokens == summed > 0
    assert replay.usage == usage_from_cassettes(night.cassette)
    assert replay.usage.cost_microusd is None, "no price list, no cost"


def test_usage_counts_attempts_retries_timeouts_refusals_latency_and_cost() -> None:
    cassette = Cassette.record()
    cassette.commit(
        [
            entry("seat:1", 3, 1, outcome="error", usage=None, cost_microusd=0, latency_ms=None),
            entry("seat:1", 3, 2),
            entry("seat:2", 3, 1, outcome="timeout", usage=None, cost_microusd=0),
            entry("seat:2", 4, 1, outcome="refusal"),
            entry("host", 0, 1, key=CallKey("mock-0001", 2, "host_lot", 0, None, 1)),
        ]
    )
    usage = usage_from_cassettes(cassette)
    one, two, host = (usage.by_role[r] for r in ("seat:1", "seat:2", "host"))
    assert (one.calls, one.retries, one.timeouts, one.refusals) == (2, 1, 0, 0)
    assert (two.calls, two.retries, two.timeouts, two.refusals) == (2, 0, 1, 1)
    assert one.input_tokens == 100 and one.cache_read_tokens == 50 and one.cache_write_tokens == 10
    assert one.output_tokens == 20 and one.reasoning_tokens == 5
    assert one.latency_ms == 40 and host.latency_ms == 40
    assert usage.total.calls == 5 and usage.total.latency_ms == 40 + 40 + 40 + 40
    assert usage.total.timeouts == 1 and usage.total.refusals == 1 and usage.total.retries == 1
    assert usage.cost_microusd == 3 * 7, "failed attempts cost nothing"


def test_a_call_with_tokens_and_no_price_makes_the_cost_unknown() -> None:
    cassette = Cassette.record()
    cassette.commit([entry("seat:1", 3, 1), entry("seat:1", 3, 2, cost_microusd=None)])
    assert usage_from_cassettes(cassette).cost_microusd is None
    assert usage_from_cassettes(Cassette.record()) == Usage(), "no attempt, no usage"


def test_usage_given_by_the_caller_replaces_the_cassette_usage() -> None:
    assert build(usage=Usage(cost_microusd=12)).usage == Usage(cost_microusd=12)


# ================================================================================= the manifest


def test_the_manifest_has_everything_needed_to_audit_the_run(replay: Replay) -> None:
    night = mock_night()
    manifest = replay.manifest
    assert manifest.config_hash == config_hash(night.config)
    assert manifest.prompt_template_version == "zh-v1"
    assert manifest.prompt_hash == templates_digest("zh")
    assert manifest.tool_schema_hash is not None and manifest.tool_schema_hash.startswith("sha256:")
    assert manifest.engine_version == auction_night.__version__
    assert manifest.mechanism_versions == replay.night.mechanisms
    assert manifest.models_returned == {
        f"seat:{n}": replay.seats[n - 1].model_returned for n in PLAYERS
    }
    assert (manifest.git_commit, manifest.git_dirty, manifest.created_utc) == (None, False, None)
    assert (manifest.seed_commitment, manifest.revealed_seed) == (None, None)


def test_the_manifest_names_the_templates_of_the_host_the_judges_and_the_classifier(
    replay: Replay,
) -> None:
    """They are the digests in force, written even though this night's host, judges and
    classifier are mocks, as the digest of the seat templates is written for mock seats."""
    manifest = replay.manifest
    assert manifest.host_prompt_hash == host_templates_digest("zh")
    assert manifest.judge_prompt_hash == judge_templates_digest("zh")
    assert manifest.scan_prompt_hash == scan_prompt_digest()
    hashes = [
        manifest.prompt_hash,
        manifest.host_prompt_hash,
        manifest.judge_prompt_hash,
        manifest.scan_prompt_hash,
    ]
    assert all(h is not None and h.startswith("sha256:") for h in hashes)
    assert len(set(hashes)) == 4, "four different sets of templates"


def test_the_manifest_takes_git_and_the_time_as_they_are_given() -> None:
    commit = "ab" * 20
    made = build(git=GitInfo(commit, True), created_utc="2026-11-01T20:00:00.000Z")
    assert (made.manifest.git_commit, made.manifest.git_dirty) == (commit, True)
    assert made.manifest.created_utc == "2026-11-01T20:00:00.000Z"
    assert last_finished(mock_night().cassette) == STAMP
    assert last_finished(Cassette.record()) is None


def test_the_config_hash_changes_with_the_configuration() -> None:
    other = play(
        config_with(night={"chat": {"max_chars": 50, "window_lots": 2, "persona_max_chars": 60}})
    )
    made = build_replay(other.config, other.state, git=NO_GIT)
    assert made.manifest.config_hash != build().manifest.config_hash


def test_a_night_with_the_memory_section_names_its_texts_and_its_setting_in_the_manifest() -> None:
    other = play(config_with(night={"memory": "plan_pledges"}))
    made = build_replay(other.config, other.state, git=NO_GIT)
    plain = build().manifest
    assert made.manifest.config_hash == config_hash(other.config) != plain.config_hash
    assert made.manifest.prompt_hash == templates_digest("zh", "memory")
    assert plain.prompt_hash == templates_digest("zh") != made.manifest.prompt_hash
    assert made.manifest.prompt_template_version == plain.prompt_template_version


def test_an_inline_seed_can_be_revealed_and_gets_its_digest_as_commitment() -> None:
    made = build(reveal_seed=True)
    assert made.manifest.revealed_seed == "mock-seed-0001"
    assert made.manifest.seed_commitment == seed_commitment("mock-seed-0001")


def test_a_published_commitment_comes_from_the_configuration_and_the_log() -> None:
    commitment = seed_commitment("mock-seed-0001")
    night = play(config_with(night={"deal_seed_commitment": commitment}))
    commits = [e for e in night.state.events if isinstance(e, SeedCommitEvent)]
    assert [c.data.phase for c in commits] == ["commit"]
    kept = build_replay(night.config, night.state, git=NO_GIT)
    assert (kept.manifest.seed_commitment, kept.manifest.revealed_seed) == (commitment, None)
    shown = build_replay(night.config, night.state, git=NO_GIT, reveal_seed=True)
    assert shown.manifest.revealed_seed == "mock-seed-0001"


def test_a_reveal_event_in_the_log_publishes_the_seed() -> None:
    commitment = seed_commitment("mock-seed-0001")
    night = play(config_with(night={"deal_seed_commitment": commitment}))
    last = night.state.events[-1]
    reveal = SeedCommitEvent(
        seq=last.seq + 1,
        vis="public",
        data=SeedCommitData(phase="reveal", commitment=commitment, seed="mock-seed-0001"),
    )
    state = with_events(night, (*night.state.events, reveal))
    made = build_replay(night.config, state, git=NO_GIT)
    assert made.manifest.revealed_seed == "mock-seed-0001"


def test_a_revealed_seed_that_does_not_open_the_published_commitment_is_refused() -> None:
    commitment = seed_commitment("mock-seed-0001")
    night = play(config_with(night={"deal_seed_commitment": commitment}))
    other = "another-seed-0002"
    reveal = SeedCommitEvent(
        seq=night.state.events[-1].seq + 1,
        vis="public",
        data=SeedCommitData(phase="reveal", commitment=seed_commitment(other), seed=other),
    )
    state = with_events(night, (*night.state.events, reveal))
    with pytest.raises(ExportError, match="does not open the commitment"):
        build_replay(night.config, state, git=NO_GIT)


def test_a_commitment_that_the_log_and_the_configuration_dispute_is_refused() -> None:
    night = play(config_with(night={"deal_seed_commitment": seed_commitment("mock-seed-0001")}))
    wrong = seed_commitment("another-seed")
    commit = next(e for e in night.state.events if isinstance(e, SeedCommitEvent))
    forged = commit.model_copy(
        update={"data": commit.data.model_copy(update={"commitment": wrong})}
    )
    events = [forged if e is commit else e for e in night.state.events]
    with pytest.raises(ExportError, match="commitment in the configuration and in the log differ"):
        build_replay(night.config, with_events(night, events), git=NO_GIT)


def test_a_seed_in_the_environment_is_revealed_only_against_a_commitment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    night = mock_night()
    raw = raw_config()
    raw["night"].pop("seed")
    raw["night"]["seed_env"] = "AUCTION_NIGHT_TEST_SEED"
    unpublished = NightConfig.model_validate(raw)
    monkeypatch.delenv("AUCTION_NIGHT_TEST_SEED", raising=False)
    with pytest.raises(ExportError, match="AUCTION_NIGHT_TEST_SEED is not set"):
        build_replay(unpublished, night.state, theory=night.theory, git=NO_GIT, reveal_seed=True)
    monkeypatch.setenv("AUCTION_NIGHT_TEST_SEED", "mock-seed-0001")
    with pytest.raises(ExportError, match="only be revealed against a published commitment"):
        build_replay(unpublished, night.state, theory=night.theory, git=NO_GIT, reveal_seed=True)
    quiet = build_replay(unpublished, night.state, theory=night.theory, git=NO_GIT)
    assert (quiet.manifest.seed_commitment, quiet.manifest.revealed_seed) == (None, None)
    raw["night"]["deal_seed_commitment"] = seed_commitment("mock-seed-0001")
    published = NightConfig.model_validate(raw)
    shown = build_replay(published, night.state, theory=night.theory, git=NO_GIT, reveal_seed=True)
    assert shown.manifest.revealed_seed == "mock-seed-0001"
    monkeypatch.setenv("AUCTION_NIGHT_TEST_SEED", "some-other-seed")
    with pytest.raises(ExportError, match="deal_seed_commitment"):
        build_replay(published, night.state, theory=night.theory, git=NO_GIT, reveal_seed=True)


def test_git_info_reads_the_commit_and_whether_tracked_files_changed(tmp_path: Path) -> None:
    assert git_info(tmp_path / "missing") == GitInfo()
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(tmp_path)}

    def git(*args: str) -> None:
        options = ["-c", "user.name=T", "-c", "user.email=nobody", "-c", "commit.gpgsign=false"]
        subprocess.run(
            ["git", *options, "-C", str(repo), *args], check=True, env=env, capture_output=True
        )

    git("init", "-q")
    assert git_info(repo) == GitInfo(), "no commit yet"
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    git("add", "a.txt")
    git("commit", "-q", "-m", "first")
    clean = git_info(repo)
    assert clean.commit is not None and len(clean.commit) == 40 and clean.dirty is False
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    assert git_info(repo) == GitInfo(clean.commit, True)
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    (repo / "stray.txt").write_text("not tracked\n", encoding="utf-8")
    assert git_info(repo) == clean, "an untracked file is not a change"


# ============================================================================== what stops it


def duplicate(event: AnyEvent, seq: int) -> AnyEvent:
    return event.model_copy(update={"seq": seq})


def test_two_lines_from_one_seat_in_a_lot_are_refused_with_the_place() -> None:
    night = mock_night()
    talk = next(e for e in night.state.events if isinstance(e, TalkEvent) and e.lot == 3)
    state = with_events(night, (*night.state.events, duplicate(talk, 10_000)))
    assert f"lot 3, seat {talk.data.seat}: 2 talk events" in check_premises(state.events)
    with pytest.raises(PremiseError, match=rf"lot 3, seat {talk.data.seat}: 2 talk events"):
        build_replay(night.config, state, git=NO_GIT)


def test_two_readings_or_two_talk_actions_or_two_plans_are_refused() -> None:
    night = mock_night()
    guess = next(e for e in night.state.events if isinstance(e, GuessEvent))
    voice = next(
        e for e in night.state.events if isinstance(e, ActionEvent) and e.data.phase == "talk"
    )
    plan = next(
        e for e in night.state.events if isinstance(e, ActionEvent) and e.data.phase == "plan"
    )
    for extra, text in ((guess, "2 guesses"), (voice, "2 talk actions"), (plan, "2 plan actions")):
        events = (*night.state.events, duplicate(extra, 10_000))
        assert any(text in problem for problem in check_premises(events)), text
        with pytest.raises(PremiseError, match=text):
            build_replay(night.config, with_events(night, events), git=NO_GIT)


def test_lines_that_come_out_of_the_speaking_order_are_refused() -> None:
    night = mock_night()
    talks = [e for e in night.state.events if isinstance(e, TalkEvent) and e.lot == 1]
    swapped = list(night.state.events)
    i, j = swapped.index(talks[0]), swapped.index(talks[1])
    swapped[i], swapped[j] = swapped[j], swapped[i]
    assert any("lot 1: the lines came in the order" in p for p in check_premises(swapped))


def test_the_canary_word_of_a_mission_in_a_public_text_is_refused() -> None:
    night = mock_night()
    holder, mission = next(iter(night.state.deal.missions.items()))
    talk = next(e for e in night.state.events if isinstance(e, TalkEvent) and e.lot == 2)
    for text in (f"我的暗号是 {mission.canary}", f"low {mission.canary.lower()} case"):
        leaked = talk.model_copy(update={"data": talk.data.model_copy(update={"text": text})})
        events = [leaked if e is talk else e for e in night.state.events]
        with pytest.raises(ExportError, match=rf"timeline\[1\]\.talk\[\d\]\.text .*seat {holder}"):
            build_replay(night.config, with_events(night, events), git=NO_GIT)


def test_a_night_that_is_not_over_cannot_be_exported() -> None:
    night = mock_night()
    unfinished = dataclasses.replace(night.state, stage="bid")
    with pytest.raises(ExportError, match="not over"):
        build_replay(night.config, unfinished, git=NO_GIT)


def test_a_highlight_outside_its_lot_is_refused() -> None:
    light = Highlight(
        id=1, category="bargain", score=3, lot=2, seats=[1], first_seq=1, last_seq=2, text="x"
    )
    with pytest.raises(ExportError, match="highlight 1 is not inside lot 2"):
        build(parts=ShowParts(highlights=[light]))


def test_the_sequence_checks_find_a_line_or_a_hammer_that_stands_in_the_wrong_place(
    replay: Replay,
) -> None:
    lot = replay.timeline[2]
    seat, seq = next(iter(lot.talk_seqs.items()))

    def with_lot(**changes: Any) -> Replay:
        timeline = list(replay.timeline)
        timeline[2] = lot.model_copy(update=changes)
        return replay.model_copy(update={"timeline": timeline})

    assert check_replay(replay) == []
    outside = check_replay(with_lot(talk_seqs={**lot.talk_seqs, seat: lot.last_seq + 5}))
    assert any(
        f"the line of seat {seat}" in problem and "is outside" in problem for problem in outside
    )
    missing = {s: q for s, q in lot.talk_seqs.items() if s != seat}
    assert any("talk_seqs and the lines" in p for p in check_replay(with_lot(talk_seqs=missing)))
    elsewhere = check_replay(with_lot(hammer_seq=lot.last_seq - 1))
    assert any(f"hammer_seq {lot.last_seq - 1} is not last_seq" in problem for problem in elsewhere)
    assert check_replay(with_lot(talk_seqs={}, hammer_seq=None)) == [], (
        "a file without them is fine"
    )


def test_a_judge_cannot_be_both_present_and_absent(replay: Replay) -> None:
    night = mock_night()
    parts = show_parts(night.config, night.state, night.cards)
    both = JudgeAbsence(judge=0, vendor="mock", reason="other")
    with pytest.raises(ExportError, match="judge 0 is both in judges and in judges_absent"):
        build(parts=dataclasses.replace(parts, judges_absent=[both]))


def test_the_sequence_checks_find_a_thought_that_belongs_to_no_action(replay: Replay) -> None:
    thought = replay.reveal.thoughts[20]
    wrong = thought.model_copy(update={"seq": thought.seq + 100_000})
    thoughts = [*replay.reveal.thoughts[:20], wrong, *replay.reveal.thoughts[21:]]
    bent = replay.model_copy(
        update={"reveal": replay.reveal.model_copy(update={"thoughts": thoughts})}
    )
    assert any("is outside lot" in problem for problem in check_replay(bent))


# ============================================================================ the show parts


def test_the_parts_of_other_blocks_go_into_the_replay_as_they_are() -> None:
    award = AwardData(award="tilt", winners=[3], metric=40, metric_unit="coins")
    vacant = AwardData(award="keeper", vacant=True)
    lights = [
        Highlight(
            id=1,
            category="bargain",
            score=7,
            lot=1,
            seats=[2],
            first_seq=23,
            last_seq=40,
            text="捡漏",
        )
    ]
    verdict = JudgeVerdictData(
        verdict="pledge_violation", seats=[5], lots=[1], evidence_seqs=[35], quotes=["这件我不碰"]
    )
    accuracy = JudgeAccuracy(judge=0, vendor="vendor-x", guesses=3, correct=2, targets=6)
    absent = JudgeAbsence(judge=1, vendor="vendor-y", reason="timeout")
    parts = ShowParts(
        awards=[award, vacant],
        bots_reference=[vacant],
        highlights=lights,
        verdicts=[verdict],
        judge_accuracy=[accuracy],
        judges_absent=[absent],
    )
    made = build(parts=parts)
    assert made.awards == [award, vacant] and made.bots_reference == [vacant]
    assert (
        made.highlights == lights
        and made.verdicts == [verdict]
        and made.judge_accuracy == [accuracy]
    )
    assert made.judges == [] and made.judges_absent == [absent]
    assert replay_json(build())["awards"] == [], "and without them the parts are empty"
    assert replay_json(build())["judges_absent"] == []


# ============================================================================= run directories


@pytest.fixture(scope="module")
def run_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("run") / "night"
    run_directory(mock_night().config, out)
    return out


def test_a_run_directory_is_played_again_to_the_same_state(run_dir: Path) -> None:
    run = replay_run(mock_night().config, run_dir)
    assert run.events == len(run.state.events) == len(mock_night().state.events)
    assert dumps_events(run.state.events) == (run_dir / EVENTS_FILE).read_text(encoding="utf-8")
    assert run.human_seat is None and run.config == mock_night().config
    assert run.cassette.unused() == [] and run.state.done


def test_the_replay_of_a_run_directory_is_the_replay_of_the_night(run_dir: Path) -> None:
    run = replay_run(mock_night().config, run_dir)
    night = mock_night()
    from_dir = build_replay(
        run.config, run.state, night.host.block, cassettes=[run.cassette], git=NO_GIT
    )
    direct = build()
    # the cassette of the directory holds real time stamps, but nothing in the replay reads them
    assert replay_text(from_dir) == replay_text(direct)


def test_a_run_directory_goes_from_the_files_to_the_checked_text_in_one_call(
    run_dir: Path,
) -> None:
    night = mock_night()
    done = export_directory(night.config, run_dir, git=NO_GIT, reveal_seed=True)
    assert done.report.ok and done.replay.night.id == "mock-0001"
    assert done.replay.manifest.created_utc is not None, "the time of the last recorded call"
    parts = show_parts(night.config, night.state, night.cards)
    direct = build(reveal_seed=True, parts=parts)
    stamped = done.replay.manifest.model_copy(update={"created_utc": None})
    assert done.replay.model_copy(update={"manifest": stamped}) == direct
    again = export_directory(night.config, run_dir, git=NO_GIT, reveal_seed=True)
    assert again.text == done.text
    explicit = export_directory(night.config, run_dir, created_utc="2026-11-02T01:00:00Z")
    assert explicit.replay.manifest.created_utc == "2026-11-02T01:00:00Z"


def test_a_run_directory_exports_with_the_judges_the_awards_and_the_highlights(
    run_dir: Path,
) -> None:
    done = export_directory(mock_night().config, run_dir, git=NO_GIT)
    replay = done.replay
    assert [j.judge for j in replay.judges] == [0, 1] and replay.judges_absent == []
    assert replay.verdicts and replay.judge_accuracy and replay.highlights
    assert [a.award for a in replay.awards] == list(AWARD_KEYS)
    assert len(replay.bots_reference) == 4
    assert all(a.basis is not None for a in (*replay.awards, *replay.bots_reference))
    assert all(h.basis is not None for h in replay.highlights)
    assert any(score.stats.mind_error_milli is not None for score in replay.scores)
    assert replay.usage.by_role.keys() == {f"seat:{n}" for n in PLAYERS}, "mock judges call nothing"
    assert check_replay(replay) == [] and done.report.ok


def test_show_parts_given_to_the_export_replace_the_made_ones(run_dir: Path) -> None:
    config = mock_night().config
    parts = ShowParts(awards=[AwardData(award="keeper", vacant=True)])
    with_parts = export_directory(config, run_dir, parts_for=lambda config, state, cards: parts)
    assert with_parts.replay.awards == parts.awards and with_parts.replay.judges == []
    bare = export_directory(config, run_dir, parts_for=lambda config, state, cards: None)
    assert bare.replay.awards == [] and bare.replay.verdicts == [] and bare.replay.highlights == []
    assert bare.replay.judges_absent == []


# ------------------------------------------------------------------------ the made parts


def made_parts() -> ShowParts:
    night = mock_night()
    return show_parts(night.config, night.state, night.cards)


def test_the_made_parts_agree_with_each_other_and_with_the_scores() -> None:
    replay = build(parts=made_parts())
    assert [e.judge for e in replay.judges] == [a.judge for a in replay.judge_accuracy] == [0, 1]
    kinds = {v.verdict for v in replay.verdicts}
    assert kinds == {"pledge_violation", "collusion_red"}
    assert [h.id for h in replay.highlights] == list(range(1, len(replay.highlights) + 1))
    assert check_replay(replay) == []
    stats = {score.seat: score.stats for score in replay.scores}
    awards = {a.award: a for a in replay.awards}
    # the columns of the scores are the figures that the awards rank
    guesses = {n: stats[n].mind_error_milli for n in PLAYERS if stats[n].valid_guesses >= 8}
    assert awards["mind_reader"].metric == min(guesses.values())
    assert awards["mind_reader"].winners == sorted(
        n for n, v in guesses.items() if v == min(guesses.values())
    )
    books = {n: stats[n].textbook_deviation_milli for n in PLAYERS if stats[n].textbook_bids >= 6}
    assert awards["most_textbook"].metric == min(books.values())
    plans = {n: stats[n].consistency_milli for n in PLAYERS if stats[n].judgable_lots >= 10}
    assert awards["consistent"].metric == max(plans.values())
    assert stats[7].valid_guesses == 0 and stats[7].mind_error_milli is None, "a bot never guesses"
    assert all(stats[n].valid_guesses == len(mock_night().state.guesses) // 6 for n in PLAYERS)


def test_the_made_parts_are_a_pure_function_of_the_night() -> None:
    first, second = made_parts(), made_parts()
    assert first == second
    assert replay_text(build(parts=first)) == replay_text(build(parts=second))


def test_judges_that_gave_no_usable_answer_are_listed_as_absent_with_the_reason() -> None:
    night = mock_night()
    judges: list[JudgeBackend] = [
        ScriptedJudge(0, "judge-a", [None]),
        MockJudge(1, fault="always", spoil="garbage"),
    ]
    parts = show_parts(night.config, night.state, night.cards, backends=judges)
    replay = build(parts=parts)
    assert replay.judges == [] and replay.judge_accuracy == []
    assert [(a.judge, a.vendor, a.reason) for a in replay.judges_absent] == [
        (0, "judge-a", "other"),
        (1, "mock", "unreadable"),
    ]
    assert "collusion_red" not in {v.verdict for v in replay.verdicts}, "no chat, no red card"
    assert {"pledge_violation"} <= {v.verdict for v in replay.verdicts}
    assert [a.award for a in replay.awards] == list(AWARD_KEYS) and check_replay(replay) == []


def test_one_judge_that_delivers_is_listed_and_the_other_is_absent() -> None:
    night = mock_night()
    judges: list[JudgeBackend] = [MockJudge(0), ScriptedJudge(1, "judge-b", [None])]
    replay = build(parts=show_parts(night.config, night.state, night.cards, backends=judges))
    assert [e.judge for e in replay.judges] == [0]
    assert [(a.judge, a.reason) for a in replay.judges_absent] == [(1, "other")]
    assert [a.judge for a in replay.judge_accuracy] == [0]


# ------------------------------------------------------------------------- model judges


def judged_config() -> NightConfig:
    """The mock night, but the two judges are models on two endpoints (nothing is ever called)."""
    raw = raw_config()
    raw["endpoints"] = {
        f"judge_ep_{i}": {"kind": "openai_compatible", "base_url": f"http://localhost:{9 + i}/v1"}
        for i in (0, 1)
    }
    raw["show"]["judges"] = [
        {
            "kind": "llm",
            "endpoint": f"judge_ep_{i}",
            "model": f"judge-model-{i}",
            "vendor": f"vendor-j{i}",
        }
        for i in (0, 1)
    ]
    return NightConfig.model_validate(raw)


@pytest.fixture(scope="module")
def judged(tmp_path_factory: pytest.TempPathFactory) -> tuple[NightConfig, Path]:
    """A run directory of the mock night with two model judges, and the recording of their calls
    (made with a fake model that answers like the mock judge)."""
    config = judged_config()
    out = tmp_path_factory.mktemp("judged") / "night"
    run_directory(config, out)
    record = build_judge_input(mock_night().state.events)
    text = MockJudge().write(judge_job(0, record)).text
    clients = {f"judge_ep_{i}": FakeJudgeClient(lambda request, n: text) for i in (0, 1)}
    cassette = Cassette.record(out / JUDGE_CALLS_FILE)
    judges = make_judges(config, cassette=cassette, clients=clients, clock=lambda: STAMP)
    assert run_judges(record, judges).both
    return config, out


def test_a_model_judge_is_read_from_its_recording_and_its_calls_are_in_the_usage(
    judged: tuple[NightConfig, Path],
) -> None:
    config, out = judged
    done = export_directory(config, out, git=NO_GIT)
    replay = done.replay
    assert [(e.judge, e.vendor) for e in replay.judges] == [(0, "vendor-j0"), (1, "vendor-j1")]
    assert [e.model_returned for e in replay.judges] == ["judge-model-0-0001", "judge-model-1-0001"]
    assert replay.judges_absent == []
    assert {"judge:0", "judge:1"} <= set(replay.usage.by_role)
    assert replay.usage.by_role["judge:0"].calls == 1
    assert replay.manifest.models_returned["judge:1"] == "judge-model-1-0001"
    # the judges read the same night in the same words as the mock judge, so they find the same
    mock = build(parts=made_parts())
    assert replay.verdicts == mock.verdicts and replay.awards == mock.awards
    assert replay.highlights == mock.highlights
    assert [e.extractions for e in replay.judges] == [e.extractions for e in mock.judges]
    again = export_directory(config, out, git=NO_GIT)
    assert again.text == done.text


def test_the_recording_of_the_judges_can_be_anywhere(
    judged: tuple[NightConfig, Path], tmp_path: Path
) -> None:
    config, out = judged
    elsewhere = tmp_path / "judges.jsonl"
    shutil.copy(out / JUDGE_CALLS_FILE, elsewhere)
    moved = tmp_path / "night"
    shutil.copytree(out, moved)
    (moved / JUDGE_CALLS_FILE).unlink()
    with pytest.raises(RecordingNotFound, match=r"judge is a model.*judge_calls\.jsonl"):
        export_directory(config, moved)
    done = export_directory(config, moved, judge_calls=elsewhere, git=NO_GIT)
    assert [e.judge for e in done.replay.judges] == [0, 1]


def test_show_parts_of_a_model_judge_need_the_recording_and_never_call_a_model() -> None:
    night = mock_night()
    with pytest.raises(RecordingNotFound, match="a judge is a model"):
        show_parts(judged_config(), night.state, night.cards)


def test_a_recording_of_another_night_does_not_fit(
    judged: tuple[NightConfig, Path], tmp_path: Path
) -> None:
    config, out = judged
    other = tmp_path / "night"
    shutil.copytree(out, other)
    text = (other / JUDGE_CALLS_FILE).read_text(encoding="utf-8")
    (other / JUDGE_CALLS_FILE).write_text(text.replace("mock-0001", "mock-0002"), encoding="utf-8")
    with pytest.raises(CassetteError):
        export_directory(config, other)


def test_a_judge_that_timed_out_on_the_night_is_absent_in_the_export(
    judged: tuple[NightConfig, Path], tmp_path: Path
) -> None:
    config, out = judged
    timed_out = tmp_path / "night"
    shutil.copytree(out, timed_out)
    record = build_judge_input(mock_night().state.events)
    text = MockJudge().write(judge_job(0, record)).text
    clients = {
        "judge_ep_0": FakeJudgeClient(lambda request, n: text),
        "judge_ep_1": FakeJudgeClient(lambda request, n: TimeoutError("too slow")),
    }
    path = timed_out / JUDGE_CALLS_FILE
    path.unlink()
    judges = make_judges(
        config, cassette=Cassette.record(path), clients=clients, clock=lambda: STAMP
    )
    run_judges(record, judges)
    done = export_directory(config, timed_out, git=NO_GIT)
    replay = done.replay
    assert [e.judge for e in replay.judges] == [0]
    assert [(a.judge, a.vendor, a.reason) for a in replay.judges_absent] == [
        (1, "vendor-j1", "timeout")
    ]
    assert "collusion_red" not in {v.verdict for v in replay.verdicts}
    assert replay.usage.by_role["judge:1"].timeouts == 1


def test_a_model_role_without_its_recording_is_not_found_and_never_called(run_dir: Path) -> None:
    raw = raw_config()
    raw["show"]["host_offline"] = {
        "kind": "llm", "endpoint": "ep", "model": "m", "vendor": "vendor-s"
    }  # fmt: skip
    raw["endpoints"] = {"ep": {"kind": "openai_compatible", "base_url": "http://localhost:9/v1"}}
    config = NightConfig.model_validate(raw)
    with pytest.raises(RecordingNotFound, match=r"offline host is a model.*host_calls\.jsonl"):
        export_directory(config, run_dir)
    assert issubclass(RecordingNotFound, ExportError)


def test_a_log_that_differs_from_the_replay_is_refused(run_dir: Path, tmp_path: Path) -> None:
    copy = tmp_path / "night"
    copy.mkdir()
    for name in (EVENTS_FILE, CALLS_FILE):
        (copy / name).write_bytes((run_dir / name).read_bytes())
    lines = (copy / EVENTS_FILE).read_text(encoding="utf-8").splitlines(keepends=True)
    lines[30] = lines[30].replace("1", "2", 1)
    (copy / EVENTS_FILE).write_text("".join(lines), encoding="utf-8")
    with pytest.raises(ExportError, match="differs from events.jsonl: line 31 differs"):
        replay_run(mock_night().config, copy)


def test_files_that_are_missing_or_unused_are_refused(run_dir: Path, tmp_path: Path) -> None:
    copy = tmp_path / "night"
    copy.mkdir()
    (copy / EVENTS_FILE).write_bytes((run_dir / EVENTS_FILE).read_bytes())
    with pytest.raises(ExportError, match="not found: .*calls.jsonl"):
        replay_run(mock_night().config, copy)
    lines = (run_dir / CALLS_FILE).read_text(encoding="utf-8").splitlines()
    extra = json.loads(lines[-1])
    extra["key"]["attempt"] = 2
    (copy / CALLS_FILE).write_text(
        "\n".join([*lines, json.dumps(extra, ensure_ascii=False)]) + "\n", encoding="utf-8"
    )
    with pytest.raises(ExportError, match="1 recorded attempts were never asked for"):
        replay_run(mock_night().config, copy)


def test_a_night_with_a_person_is_replayed_with_the_person_at_the_table(tmp_path: Path) -> None:
    sitting = sit(mock_night().config, seat=3)
    run_state = sitting.rig.driver.state
    made = build_replay(sitting.config, run_state, git=NO_GIT, cassettes=[sitting.cassette])
    person = made.seats[2]
    assert (person.seat, person.kind, person.model, person.model_returned) == (
        3,
        "human",
        None,
        None,
    )
    assert made.night.ranked is False, "a night with a person does not count"
    assert "seat:3" not in made.usage.by_role, "the person made no model call"
    assert made.reveal.plans and any(p.seat == 3 for p in made.reveal.plans)
    # and from a directory: the log, the cassette and the inputs of the person
    out = tmp_path / "human"
    out.mkdir()
    (out / EVENTS_FILE).write_text(sitting.result.events_text(), encoding="utf-8", newline="\n")
    (out / CALLS_FILE).write_text(sitting.cassette.text(), encoding="utf-8", newline="\n")
    from auction_night.llm.human import TapeWriter

    with TapeWriter(out / HUMAN_FILE, sitting.config.night.id, 3) as writer:
        for tape_entry in sitting.recorded().entries:
            writer.append(tape_entry)
    run = replay_run(
        mock_night().config, out
    )  # the file says mock at seat 3: the tape seats the person
    assert run.human_seat == 3 and run.config.seats[2].kind == "human"
    again = build_replay(run.config, run.state, git=NO_GIT, cassettes=[run.cassette])
    assert again.seats[2].kind == "human" and again.night.ranked is False
    # the whole export, judges and awards included: the person is a player like the models
    done = export_directory(mock_night().config, out, git=NO_GIT)
    assert done.report.ok and done.replay.night.ranked is False
    assert [e.judge for e in done.replay.judges] == [0, 1] and len(done.replay.awards) == 10
    assert done.replay.seats[2].kind == "human"
    with pytest.raises(ExportError, match="their inputs"):
        (out / HUMAN_FILE).unlink()
        replay_run(sitting.config, out)


def test_play_offline_refuses_a_table_with_a_model_or_a_person() -> None:
    raw = raw_config()
    raw["seats"][0] = {"seat": 1, "codename": "鲸落", "kind": "human"}
    with pytest.raises(ExportError, match="cannot be played offline"):
        play_offline(NightConfig.model_validate(raw))


# ================================================================================ the export


def test_the_export_returns_the_text_the_replay_and_the_scan(replay: Replay) -> None:
    night = mock_night()
    done = export_replay(
        night.config,
        night.state,
        night.host.block,
        gate=ContentGate.builtin(),
        theory=night.theory,
        cards=night.cards,
        cassettes=[night.cassette],
        git=NO_GIT,
    )
    assert done.replay == replay and done.text == replay_text(replay)
    assert done.report.ok and done.report.scanned == len(public_texts(replay)) > 300
