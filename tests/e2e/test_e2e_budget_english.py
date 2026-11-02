"""A cost cap that only cuts off the English revisions of the last lot, end to end.

An English revision that does not come keeps the exit price in force. The log records it as a
``kept_previous`` action, which carries no reason, so the log cannot say that the cost gate was
the cause. When the cap is reached by the first English round of the last lot of the night, the
revision rounds of that lot are all that the gate skips: no action anywhere in the log names the
cap, yet the ledger has counted the skipped actions and calls the night ``budget_aborted``.
``night_end`` has to agree with the ledger all the same. The engine takes its status from the
answers it is handed (an abstention for the cap marks the night) and not from the log.

The night is the mock night with a deal seed under which its last lot is an English one and the
model seats revise their exit prices in it (found by trying seeds; if the mock or the deal changes,
try others). The tests assert that premise, so a change fails here instead of leaving tests that
check nothing.
"""

import pytest
from e2e_support import MODEL_SEATS, Played, make_config, mock_prices, play

from auction_night.contracts import NightConfig, validate_log
from auction_night.llm.cassette import Cassette
from auction_night.llm.nightrun import describe, play_night

SEED = "budget-english-012"
LAST = 12  # the last lot of the mock night


def night_config(cap: int | None = None) -> NightConfig:
    """The mock night of this file, with a cost cap of ``cap`` micro-dollars if one is given."""
    limits = {} if cap is None else {"max_cost_per_night_usd": cap / 1_000_000}
    return make_config(night={"seed": SEED}, limits=limits)


def steps_of(plain: Played, *phases: str) -> list[int]:
    """The requests of the last lot that were paid for in ``phases``, by position in the night."""
    return sorted(
        {e.key.step for e in plain.cassette.entries if e.key.lot == LAST and e.key.phase in phases}
    )


def cost_through(plain: Played, step: int) -> int:
    """What the night without a cap had cost when the request ``step`` had been paid for."""
    return sum(e.cost_microusd or 0 for e in plain.cassette.entries if e.key.step <= step)


def revisions(played: Played) -> list:
    """The English revisions of the model seats in the last lot."""
    return [
        a for a in played.actions(phase="english_revise", lot=LAST) if a.data.seat in MODEL_SEATS
    ]


def named_for_the_cap(played: Played) -> list:
    """The actions whose reason says the cap: all that the log can show of the gate."""
    return [a for a in played.of_type("action") if a.data.reason == "budget_aborted"]


def warnings(played: Played) -> list[str]:
    """The lines of the terminal report that warn about a status that disagrees with the ledger."""
    return [line for line in describe(played.result) if line.startswith("warning")]


@pytest.fixture(scope="module")
def plain() -> Played:
    """The night without a cap, every mock token priced at one micro-dollar."""
    return play(night_config(), prices=mock_prices())


@pytest.fixture(scope="module")
def first_round_cap(plain: Played) -> int:
    """The cap that is reached exactly by the first English round of the last lot."""
    (first_round,) = steps_of(plain, "english_init")
    return cost_through(plain, first_round)


@pytest.fixture(scope="module")
def cut(plain: Played, first_round_cap: int) -> Played:
    """The night with that cap: the gate fires after the first English round of the last lot."""
    return play(night_config(first_round_cap), prices=mock_prices())


# =============================================================================== the premise


def test_the_night_ends_on_an_english_lot_that_the_models_revise(plain: Played) -> None:
    opened = plain.of_type("lot_open")
    assert len(opened) == LAST and opened[-1].lot == LAST and opened[-1].data.format == "english"
    assert plain.result.status == plain.result.run.status == "completed"
    assert revisions(plain) and {a.data.status for a in revisions(plain)} <= {"ok", "clamped"}
    assert len(steps_of(plain, "english_revise")) >= 3  # the cap points below need three rounds


def test_the_gate_fires_at_the_end_of_the_first_english_round_of_the_last_lot(
    plain: Played, cut: Played, first_round_cap: int
) -> None:
    (fired,) = [e for e in cut.of_type("cost_event") if e.data.kind == "cap_reached"]
    assert fired.lot == LAST and fired.vis == "system"
    assert fired.data.spent_microusd == fired.data.cap_microusd == first_round_cap
    (first_round,) = steps_of(plain, "english_init")
    # nothing was paid for after that round, so the revisions were not asked for
    assert [e.key for e in cut.cassette.entries] == [
        e.key for e in plain.cassette.entries if e.key.step <= first_round
    ]


# ========================================================================== what the log shows


def test_only_the_english_revisions_are_skipped_and_the_log_does_not_name_the_cap(
    plain: Played, cut: Played
) -> None:
    skipped = revisions(cut)
    assert skipped
    # a revision that does not come keeps the exit price in force, and the action has no reason
    assert {(a.data.status, a.data.reason) for a in skipped} == {("kept_previous", None)}
    assert named_for_the_cap(cut) == []
    # the ledger knows: it counted one skipped action for every one of those revisions
    run = cut.result.run
    assert run.summary["aborted_actions"] == len(skipped)
    counted = sum(c["abstentions"].get("budget_aborted", 0) for c in run.counters.values())
    assert counted == len(skipped)
    # apart from the revisions, every action is the one of the night without a cap
    assert [a.data for a in cut.of_type("action") if a.data.phase != "english_revise"] == [
        a.data for a in plain.of_type("action") if a.data.phase != "english_revise"
    ]


def test_the_status_in_night_end_agrees_with_the_ledger(cut: Played) -> None:
    result = cut.result
    assert validate_log(cut.events) == []
    (end,) = cut.of_type("night_end")
    assert end.data.status == result.status == "budget_aborted"
    assert result.run.status == result.run.summary["status"] == "budget_aborted"
    assert len(cut.of_type("hammer")) == LAST  # the night is still played to the end
    # the terminal report no longer has a disagreement to warn about
    assert describe(result)[0].endswith("budget_aborted") and warnings(cut) == []


def test_the_night_replays_byte_for_byte_with_the_same_status(
    cut: Played, first_round_cap: int
) -> None:
    cassette = Cassette.from_text(cut.cassette.text())
    replayed = play_night(night_config(first_round_cap), cassette)  # no price list is needed
    assert replayed.events_text() == cut.result.events_text()
    assert replayed.status == replayed.run.status == "budget_aborted"
    assert replayed.scores == cut.result.scores and cassette.unused() == []


# ================================================================ wherever the cap falls


def cap_after(plain: Played, where: str) -> int:
    """The cap that is reached by the request named ``where`` in the last lot."""
    talks, revise = steps_of(plain, "talk"), steps_of(plain, "english_revise")
    step = {
        "last_talk": talks[-1],
        "first_revision": revise[0],
        "second_revision": revise[1],
        "last_call": revise[-1],
    }[where]
    return cost_through(plain, step)


@pytest.mark.parametrize(
    ("where", "status"),
    [
        ("last_talk", "budget_aborted"),  # the first English round is skipped: the log shows it
        ("first_revision", "budget_aborted"),  # the later revision rounds are skipped: it does not
        ("second_revision", "budget_aborted"),  # only the last revision round is skipped
        ("last_call", "completed"),  # the cap is reached by the last call: nothing is skipped
    ],
)
def test_the_status_follows_the_ledger_wherever_the_cap_falls(
    plain: Played, where: str, status: str
) -> None:
    played = play(night_config(cap_after(plain, where)), prices=mock_prices())
    (end,) = played.of_type("night_end")
    assert end.data.status == played.result.status == played.result.run.status == status
    assert (played.result.run.summary["aborted_actions"] > 0) == (status == "budget_aborted")
    # only a cap that falls before the first English round leaves a trace that the log can show
    assert bool(named_for_the_cap(played)) == (where == "last_talk")
    assert warnings(played) == [] and validate_log(played.events) == []
