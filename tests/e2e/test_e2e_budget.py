"""The cost gate, end to end: a night that reaches its cost cap is aborted and flagged.

Mock models are priced at one micro-dollar per token, so the calls of the night have a cost and
a cap can be hit on purpose. When the cap is reached, no further model call is sent: every
remaining action of a model seat is an abstention with the reason ``budget_aborted``, the rule
bots play on, the night is written to its end, and ``night_end`` says ``budget_aborted``.
"""

import pytest
from e2e_support import MODEL_SEATS, Played, make_config, mock_prices, play

from auction_night.contracts import NightConfig, validate_log
from auction_night.llm.accounting import MissingPriceError
from auction_night.llm.cassette import Cassette
from auction_night.llm.nightrun import play_night


def capped(micro: int | float) -> NightConfig:
    """The mock night with a cost cap of ``micro`` micro-dollars."""
    return make_config(limits={"max_cost_per_night_usd": micro / 1_000_000})


def cap_events(played: Played) -> list:  # noqa: ANN401
    return [e for e in played.of_type("cost_event") if e.data.kind == "cap_reached"]


# ============================================================================ the accounting


def test_the_costs_of_a_night_add_up(priced: Played) -> None:
    run = priced.result.run
    assert run.status == "completed" and run.calls == len(priced.cassette)
    assert run.spent_microusd == sum(e.cost_microusd or 0 for e in priced.cassette.entries) > 0
    (tally,) = [e for e in priced.of_type("cost_event") if e.data.kind == "tally"]
    assert tally is priced.events[-1]
    assert tally.data.spent_microusd == run.spent_microusd and tally.data.calls == run.calls
    assert tally.data.cap_microusd is None and tally.data.role is None
    assert cap_events(priced) == []


def test_tokens_are_counted_with_the_cache_apart(priced: Played) -> None:
    total = priced.result.run.summary["total"]
    assert total["calls"] == len(priced.cassette) and total["unpriced_calls"] == 0
    assert total["cache_read_input_tokens"] > total["input_tokens"] > 0
    assert total["cache_write_input_tokens"] > 0 and total["output_tokens"] > 0
    by_role = priced.result.run.summary["by_role"]
    assert set(by_role) == {f"seat:{seat}" for seat in MODEL_SEATS}
    assert sum(r["cost_microusd"] for r in by_role.values()) == total["cost_microusd"]


def test_a_cap_that_is_not_reached_changes_nothing(priced: Played, config: NightConfig) -> None:
    roomy = make_config(limits={"max_cost_per_night_usd": 5.0})
    played = play(roomy, prices=mock_prices())
    assert played.result.status == "completed" and cap_events(played) == []
    assert played.events[-1].data.cap_microusd == 5_000_000  # type: ignore[union-attr]
    # apart from the cap in the tally, it is the same night
    assert played.result.events_text().count('"cap_microusd":5000000') == 1
    assert [e.request_hash for e in played.cassette.entries] == [
        e.request_hash for e in priced.cassette.entries
    ]


def test_a_cap_needs_a_price_for_every_mock_model() -> None:
    with pytest.raises(MissingPriceError, match="mock-honest"):
        play(capped(1000))  # the cap could never be enforced


# ================================================================================ the gate


@pytest.fixture(scope="module")
def aborted() -> Played:
    return play(capped(1), prices=mock_prices())


def test_a_night_over_its_cap_is_flagged_in_the_log(aborted: Played) -> None:
    assert validate_log(aborted.events) == []
    assert aborted.result.status == "budget_aborted"
    assert aborted.result.run.status == "budget_aborted"
    (end,) = aborted.of_type("night_end")
    assert end.data.status == "budget_aborted"
    assert len(aborted.of_type("hammer")) == 12  # the night is still played to the end


def test_the_gate_fires_once_at_the_moment_the_cap_is_reached(aborted: Played) -> None:
    (fired,) = cap_events(aborted)
    assert fired.vis == "system"
    assert fired.data.cap_microusd == 1 and fired.data.spent_microusd >= 1
    # the opening plan of the six model seats is the only phase that was paid for
    assert fired.data.calls == 6 == len(aborted.cassette)
    assert {e.key.phase for e in aborted.cassette.entries} == {"plan"}
    first_skipped = next(
        e.seq for e in aborted.of_type("action") if e.data.reason == "budget_aborted"
    )
    assert fired.seq < first_skipped


def test_every_action_after_the_gate_is_an_abstention_for_the_cap(aborted: Played) -> None:
    after = [
        a
        for a in aborted.of_type("action")
        if a.data.phase != "plan" and a.data.seat in MODEL_SEATS
    ]
    assert after
    for action in after:
        if action.data.status == "kept_previous":  # an English revision keeps its exit price
            continue
        assert (action.data.status, action.data.reason) == ("abstain", "budget_aborted")
        assert action.data.attempts == 1 and action.data.call_id is not None
        assert action.data.value is None and action.data.thought == ""
    assert all(a.data.status == "ok" for a in aborted.actions(phase="plan"))
    # the skipped actions are counted, and no request was sent for them
    assert aborted.result.run.summary["aborted_actions"] == len(
        [a for a in after if a.data.reason == "budget_aborted"]
    )
    assert len(aborted.cassette) == 6


def test_the_rule_bots_play_on_after_the_gate(aborted: Played) -> None:
    hammers = aborted.of_type("hammer")
    assert any(h.data.winner in (7, 8) for h in hammers)
    bot_bids = [a for a in aborted.of_type("action") if a.data.seat in (7, 8)]
    assert bot_bids and all(a.data.status in ("ok", "clamped") for a in bot_bids)
    say = {e.data.seat: e.data.text for e in aborted.of_type("talk") if e.lot == 12}
    assert say[7] and say[8] and all(say[seat] == "" for seat in MODEL_SEATS)
    assert sum(s.surplus for s in aborted.result.scores) != 0 or hammers


def test_a_cap_of_zero_is_reached_before_the_first_call() -> None:
    played = play(capped(0), prices=mock_prices())
    assert len(played.cassette) == 0 and played.result.run.calls == 0
    assert played.result.status == "budget_aborted"
    (fired,) = cap_events(played)
    assert (fired.data.cap_microusd, fired.data.spent_microusd, fired.data.calls) == (0, 0, 0)
    assert played.of_type("plan") == []
    assert {a.data.reason for a in played.actions(phase="plan")} == {"budget_aborted"}


def test_a_night_aborted_halfway_stops_at_the_phase_that_crossed_the_cap(
    priced: Played,
) -> None:
    # the cap is the cost of everything up to the last call of the first English lot
    last = max(e.key.step for e in priced.cassette.entries if e.key.lot == 2)
    cap = sum(e.cost_microusd or 0 for e in priced.cassette.entries if e.key.step <= last)
    played = play(capped(cap), prices=mock_prices())
    assert played.result.status == "budget_aborted"
    assert [e.key for e in played.cassette.entries] == [
        e.key for e in priced.cassette.entries if e.key.step <= last
    ]
    (fired,) = cap_events(played)
    assert fired.data.spent_microusd == cap and fired.data.calls == len(played.cassette)
    # lots 1 and 2 were played by the models, the lots after them were not
    early = [a for a in played.of_type("action") if a.lot in (1, 2) and a.data.seat in MODEL_SEATS]
    assert early and all(a.data.reason != "budget_aborted" for a in early)
    late = [a for a in played.of_type("action") if (a.lot or 0) >= 3 and a.data.seat in MODEL_SEATS]
    assert all(a.data.reason == "budget_aborted" or a.data.status == "kept_previous" for a in late)
    first = next(a.seq for a in late if a.data.reason == "budget_aborted")
    assert fired.seq < first


def test_a_night_aborted_in_the_middle_of_the_talk_of_a_lot(priced: Played) -> None:
    """The gate is checked before every call of the talk phase, so it cuts a lot in two."""
    talks = [e for e in priced.cassette.entries if e.key.phase == "talk" and e.key.lot == 1]
    third = sorted(talks, key=lambda e: e.key.step)[2]  # the third talk call of lot 1
    cap = sum(e.cost_microusd or 0 for e in priced.cassette.entries if e.key.step <= third.key.step)
    played = play(capped(cap), prices=mock_prices())
    assert played.result.status == "budget_aborted"
    assert [e.key for e in played.cassette.entries] == [
        e.key for e in priced.cassette.entries if e.key.step <= third.key.step
    ]
    said = {e.data.seat: e.data.text for e in played.of_type("talk") if e.lot == 1}
    assert all(said[seat] for seat in (1, 2, 3))  # the first three speakers were heard
    assert all(said[seat] == "" for seat in (4, 5, 6))  # then the models fall silent
    assert said[7] and said[8]  # the bots go on talking
    for seat in (4, 5, 6):
        (action,) = played.actions(seat, "talk", 1)
        assert (action.data.status, action.data.reason) == ("abstain", "budget_aborted")
    (fired,) = cap_events(played)
    assert fired.data.calls == len(played.cassette)


def test_an_english_lot_cut_off_by_the_gate_keeps_the_exit_prices_in_force(priced: Played) -> None:
    init = [e for e in priced.cassette.entries if e.key.phase == "english_init" and e.key.lot == 2]
    last = max(e.key.step for e in init)
    cap = sum(e.cost_microusd or 0 for e in priced.cassette.entries if e.key.step <= last)
    played = play(capped(cap), prices=mock_prices())
    revisions = [a for a in played.actions(phase="english_revise", lot=2) if a.data.seat <= 6]
    assert revisions and {a.data.status for a in revisions} == {"kept_previous"}
    assert all(a.data.reason is None for a in revisions)  # an English revision keeps no reason
    # the exit price in force is the one from the first round, so the lot ends as it would have
    assert [h.data for h in played.of_type("hammer") if h.lot == 2] == [
        h.data for h in priced.of_type("hammer") if h.lot == 2
    ]
    skipped = played.result.run.summary["aborted_actions"]
    assert skipped >= len(revisions)  # the ledger counts them, the log does not name the cause
    assert played.result.status == "budget_aborted"  # later actions name it


# ================================================================================ replaying


@pytest.mark.parametrize("micro", [0, 1])
def test_an_aborted_night_replays_byte_for_byte(micro: int) -> None:
    played = play(capped(micro), prices=mock_prices())
    cassette = Cassette.from_text(played.cassette.text())
    replayed = play_night(capped(micro), cassette)  # no price list is needed
    assert replayed.events_text() == played.result.events_text()
    assert replayed.status == "budget_aborted" and cassette.unused() == []
