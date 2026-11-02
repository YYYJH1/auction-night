"""Determinism: the same seed and the same answers give the same events, byte for byte."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from engine_support import seeds
from hypothesis import given, settings
from night_support import (
    bot_seats,
    make_config,
    play,
    play_seed,
    players_for,
    replay,
)

from auction_night.contracts import dumps_events, loads_events, sha256_digest, validate_log
from auction_night.engine.deal import deal
from auction_night.engine.night import start, step

TESTS = Path(__file__).resolve().parents[1]
BOT_TABLE = bot_seats([("economist", True), ("truthful", False)] * 4)
BOT_NIGHT = make_config(BOT_TABLE, missions={"collectors": 0, "shills": 0})
MIXED_NIGHT = make_config()

# The digests pin the whole log of one night each. They change when the rules, the deal or the
# stand-in players of the tests change, and then they are regenerated on purpose.
GOLDEN = {
    "bots": "sha256:a570814d94c133fc249acc90de11d7fc187c5032fcf14bd7eba3921600b67912",
    "mixed": "sha256:a221ea67fabde67f9348d32980e3af30df3841633d7d16dc0edae523b94a05a0",
}


def digest(config, seed: str) -> str:
    return sha256_digest(dumps_events(play_seed(config, seed).events))


def test_a_night_of_eight_bots_is_the_same_night_every_time() -> None:
    first = dumps_events(play_seed(BOT_NIGHT, "determinism-bots").events)
    second = dumps_events(play_seed(BOT_NIGHT, "determinism-bots").events)
    assert first == second and len(first) > 10_000
    assert first != dumps_events(play_seed(BOT_NIGHT, "determinism-other").events)


def test_a_night_with_six_stand_ins_for_models_is_the_same_night_every_time() -> None:
    first = dumps_events(play_seed(MIXED_NIGHT, "determinism-mixed").events)
    second = dumps_events(play_seed(MIXED_NIGHT, "determinism-mixed").events)
    assert first == second
    assert first != dumps_events(play_seed(MIXED_NIGHT, "determinism-other").events)


@given(seeds)
@settings(max_examples=15, deadline=None)
def test_the_recorded_answers_replay_to_the_identical_night(seed: str) -> None:
    for config in (BOT_NIGHT, MIXED_NIGHT):
        the_deal = deal(config, seed=seed)
        transcript = play(config, the_deal)
        again = replay(config, the_deal, transcript)
        assert dumps_events(again.events) == dumps_events(transcript.events)
        assert again.budgets == transcript.state.budgets
        assert (
            again.scores == transcript.state.scores and again.missions == transcript.state.missions
        )
        assert again.guesses == transcript.state.guesses


def test_the_golden_digests_pin_the_whole_log() -> None:
    assert digest(BOT_NIGHT, "golden-bots") == GOLDEN["bots"]
    assert digest(MIXED_NIGHT, "golden-mixed") == GOLDEN["mixed"]


def test_the_night_does_not_depend_on_the_hash_seed_of_the_interpreter() -> None:
    code = (
        "import sys\n"
        f"sys.path[:0] = [{str(TESTS / 'engine')!r}, {str(TESTS / 'night')!r}]\n"
        "from night_support import make_config, play_seed\n"
        "from auction_night.contracts import dumps_events, sha256_digest\n"
        "print(sha256_digest(dumps_events(play_seed(make_config(), 'golden-mixed').events)))\n"
    )
    digests = set()
    for hash_seed in ("1", "23456"):
        env = {**os.environ, "PYTHONHASHSEED": hash_seed, "CUDA_VISIBLE_DEVICES": ""}
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env
        )
        digests.add(out.stdout.strip())
    assert digests == {GOLDEN["mixed"]}


# ------------------------------------------------------------------------------- purity


def test_start_and_step_are_pure_functions() -> None:
    config = MIXED_NIGHT
    the_deal = deal(config, seed="purity")
    state, request = start(config, the_deal)
    state_again, request_again = start(config, the_deal)
    assert (state, request) == (state_again, request_again)

    players = players_for(config)
    answers = {seat: players[seat](request) for seat in request.seats}
    before = (state.events, dict(state.budgets), state.pending, state.stage, state.n_requests)
    one = step(state, answers)
    two = step(state, answers)
    assert one == two
    assert (state.events, state.budgets, state.pending, state.stage, state.n_requests) == before

    # the events of a step are the end of the new state's log, and the old log is its start
    new_state, events, next_request = one
    assert new_state.events == state.events + events
    assert new_state.pending == next_request
    assert [e.seq for e in new_state.events] == list(range(1, len(new_state.events) + 1))


def test_the_state_does_not_show_its_secrets_when_printed() -> None:
    state, _ = start(MIXED_NIGHT, deal(MIXED_NIGHT, seed="purity"))
    text = repr(state)
    assert "budgets" not in text and "deal" not in text and "values" not in text
    assert len(text) < 600


def test_the_log_validates_and_reads_back_for_many_seeds() -> None:
    for seed in ("a", "b", "c", "d"):
        events = play_seed(MIXED_NIGHT, seed).events
        assert validate_log(events) == []
        assert loads_events(dumps_events(events)) == list(events)


@pytest.mark.parametrize("config", [BOT_NIGHT, MIXED_NIGHT])
def test_events_hold_integers_only(config) -> None:
    # dumps_events refuses floats, so serialising the whole log is the check
    assert dumps_events(play_seed(config, "integers").events)
