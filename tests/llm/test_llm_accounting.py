"""Prices, the cost of a call, the ledger, the cost gate, the estimate and its command."""

import asyncio
import random
import re
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from sdk_fakes import (
    FIXED_TIME,
    REPO,
    FakeNight,
    ScriptedSdk,
    Step,
    api_config,
    clients_for,
    example_prices,
    openai_completion,
    valid_args,
)
from typer.testing import CliRunner

from auction_night.cli import app
from auction_night.contracts import NightConfig, load_config, load_prices
from auction_night.contracts.config import ModelPrice
from auction_night.llm.accounting import (
    Charge,
    EstimateAssumptions,
    Ledger,
    MissingPriceError,
    PriceTable,
    call_cost_microusd,
    calls_breakdown,
    estimate_night,
    exact_cost_microusd,
    format_estimate,
)
from auction_night.llm.cassette import CallKey, Cassette
from auction_night.llm.client import Usage
from auction_night.llm.orchestrate import ModelRunner, NightSession, RunnerSettings

FAST = RunnerSettings(timeout_s=5.0, backoff_base_s=0.0, backoff_cap_s=0.0, wall_clock_slack_s=0.5)
cli = CliRunner()


def price(
    inp: float, out: float, read: float | None = None, write: float | None = None
) -> ModelPrice:
    return ModelPrice(
        input_per_mtok=inp,
        output_per_mtok=out,
        cache_read_per_mtok=read,
        cache_write_per_mtok=write,
        as_of="2026-11-01",  # type: ignore[arg-type]
        source="https://example.com/prices",
    )


HAIKU = price(0.10, 0.50, 0.01)
M = 1_000_000


# ==================================================================== the price list


def test_the_example_price_list_is_dated_and_sourced() -> None:
    table = PriceTable.from_file(REPO / "configs" / "prices.example.yaml")
    found = table.lookup("claude-haiku-5-5")
    assert found is not None
    model, haiku = found
    assert model == "claude-haiku-5-5"
    assert (haiku.input_per_mtok, haiku.output_per_mtok, haiku.cache_read_per_mtok) == (
        0.10,
        0.50,
        0.01,
    )
    assert str(haiku.as_of) == "2026-10-06" and haiku.source.startswith("https://")
    assert table.lookup("claude-sonnet-5-5") is not None
    assert table.source and table.source.endswith("prices.example.yaml")
    assert table.lookup("claude-unknown-9") is None and table.lookup(None) is None


def test_the_returned_model_id_is_tried_before_the_requested_one() -> None:
    table = PriceTable({"dated-id": HAIKU, "alias": price(9, 9)})
    assert table.lookup("dated-id", "alias") == ("dated-id", HAIKU)
    assert table.lookup("unknown", "alias") is not None and table.lookup("unknown", "x") is None
    assert table.missing(["alias", "x", "x", "y"]) == ["x", "y"]


# ================================================================== the cost of a call


def test_cost_is_tokens_times_the_price_per_million() -> None:
    assert call_cost_microusd(HAIKU, Usage(input_tokens=M)) == 100_000  # $0.10
    assert call_cost_microusd(HAIKU, Usage(output_tokens=M)) == 500_000  # $0.50
    assert call_cost_microusd(HAIKU, Usage(cache_read_input_tokens=M)) == 10_000  # $0.01
    mixed = Usage(input_tokens=120, output_tokens=60, cache_read_input_tokens=800)
    assert call_cost_microusd(HAIKU, mixed) == 12 + 30 + 8
    assert call_cost_microusd(HAIKU, Usage()) == 0
    assert call_cost_microusd(price(0, 0), mixed) == 0


def test_a_missing_cache_price_is_the_input_price() -> None:
    no_cache = price(2, 10)
    usage = Usage(cache_read_input_tokens=M, cache_write_input_tokens=M)
    assert call_cost_microusd(no_cache, usage) == 2_000_000 + 2_000_000
    assert call_cost_microusd(HAIKU, Usage(cache_write_input_tokens=M)) == 100_000
    explicit = price(0.10, 0.50, 0.01, 0.125)
    assert call_cost_microusd(explicit, Usage(cache_write_input_tokens=M)) == 125_000


def test_reasoning_tokens_are_billed_once_as_output() -> None:
    plain = Usage(input_tokens=100, output_tokens=1000)
    reasoning = Usage(input_tokens=100, output_tokens=1000, reasoning_tokens=400)
    assert call_cost_microusd(HAIKU, plain) == call_cost_microusd(HAIKU, reasoning) == 10 + 500
    heavy = Usage(output_tokens=1000, reasoning_tokens=1000)  # all output was reasoning
    assert call_cost_microusd(HAIKU, heavy) == 500


def test_a_call_is_rounded_up_to_a_whole_micro_dollar() -> None:
    assert exact_cost_microusd(HAIKU, Usage(cache_read_input_tokens=7)) == Decimal("0.07")
    assert call_cost_microusd(HAIKU, Usage(cache_read_input_tokens=7)) == 1
    assert call_cost_microusd(HAIKU, Usage(output_tokens=1)) == 1  # 0.5


def test_prices_with_decimals_cost_exactly() -> None:
    tenth = price(0.1, 0.2)
    assert exact_cost_microusd(tenth, Usage(input_tokens=10)) == Decimal("1.0")
    assert call_cost_microusd(tenth, Usage(input_tokens=10)) == 1  # not 2, as 10 * 0.1 in floats
    assert call_cost_microusd(tenth, Usage(input_tokens=3, output_tokens=3)) == 1  # 0.9
    assert call_cost_microusd(tenth, Usage(input_tokens=30, output_tokens=30)) == 9  # 3 + 6


# ======================================================================= the ledger


UNIT = Usage(1, 2)


def charge(seat: int, lot: int, cost: int | None, usage: Usage | None = UNIT) -> Charge:
    key = CallKey("n-1", lot, "sealed", lot, seat)
    return Charge(key, f"seat:{seat}", usage, cost)


def test_totals_are_kept_per_seat_and_per_night() -> None:
    ledger = Ledger(example_prices())
    ledger.fold(
        [
            charge(1, 1, 30, Usage(10, 5, 100, 0, 2)),
            charge(2, 1, 20, Usage(20, 8, 0, 50)),
            charge(1, 2, 7, Usage(1, 1)),
        ]
    )
    assert ledger.spent_microusd == 57 and ledger.calls == 3
    one, two = ledger.by_label["seat:1"], ledger.by_label["seat:2"]
    assert (one.calls, one.cost_microusd, one.input_tokens, one.output_tokens) == (2, 37, 11, 6)
    assert (one.cache_read_input_tokens, one.reasoning_tokens) == (100, 2)
    assert (two.calls, two.cost_microusd, two.cache_write_input_tokens) == (1, 20, 50)
    assert ledger.total.input_tokens == 31 and ledger.total.reasoning_tokens == 2


def test_totals_do_not_depend_on_the_order_of_arrival() -> None:
    charges = [charge(s, lot, s * 10 + lot) for s in (1, 2, 3) for lot in (1, 2, 3)]
    summaries = []
    for seed in range(5):
        shuffled = charges[:]
        random.Random(seed).shuffle(shuffled)
        ledger = Ledger()
        ledger.fold(shuffled)
        summaries.append(ledger.summary())
    assert all(s == summaries[0] for s in summaries)
    assert list(summaries[0]["by_role"]) == ["seat:1", "seat:2", "seat:3"]


def test_calls_without_a_price_or_without_usage_are_counted_apart() -> None:
    ledger = Ledger()
    ledger.fold([charge(1, 1, None), charge(1, 2, None, usage=None), charge(1, 3, 5)])
    total = ledger.total
    assert (total.calls, total.unpriced_calls, total.unmetered_calls) == (3, 1, 1)
    assert total.cost_microusd == 5
    assert ledger.cost_of(Usage(1, 1), "claude-haiku-5-5") is None  # no price list at all
    assert Ledger(example_prices()).cost_of(None, "claude-haiku-5-5") is None


def test_the_gate_fires_once_when_the_cap_is_reached() -> None:
    ledger = Ledger(cap_microusd=100)
    ledger.fold([charge(1, 1, 99)])
    assert ledger.gate() is None and not ledger.blocked and not ledger.cap_reached
    ledger.fold([charge(2, 1, 1)])
    event = ledger.gate()
    assert event is not None and event.kind == "cap_reached"
    assert (event.spent_microusd, event.cap_microusd, event.calls) == (100, 100, 2)
    assert event.role is None
    assert ledger.blocked and ledger.gate() is None  # once
    ledger.fold([charge(3, 1, 500)])
    assert ledger.gate() is None and ledger.spent_microusd == 600


def test_a_night_is_budget_aborted_only_if_an_action_was_skipped() -> None:
    ledger = Ledger(cap_microusd=10)
    assert ledger.status == "completed"
    ledger.fold([charge(1, 1, 10)])
    assert ledger.gate() is not None and ledger.status == "completed"
    ledger.note_aborted(3)
    assert ledger.status == "budget_aborted" and ledger.aborted_actions == 3


def test_a_cap_of_zero_is_reached_from_the_start() -> None:
    ledger = Ledger(cap_microusd=0)
    event = ledger.gate()
    assert event is not None and (event.spent_microusd, event.cap_microusd, event.calls) == (
        0,
        0,
        0,
    )
    assert Ledger().gate() is None and not Ledger().cap_reached
    with pytest.raises(ValueError):
        Ledger(cap_microusd=-1)


def test_a_cap_needs_a_price_for_every_model() -> None:
    ledger = Ledger(PriceTable({"a": HAIKU}, source="prices.yaml"), cap_microusd=5)
    ledger.require_prices(["a"])
    with pytest.raises(MissingPriceError, match=r"in prices.yaml for b, c"):
        ledger.require_prices(["c", "a", "b"])
    Ledger(PriceTable()).require_prices(["anything"])  # no cap: only accounting


def test_tally_events_report_the_night_or_one_role() -> None:
    ledger = Ledger(cap_microusd=1000)
    ledger.fold([charge(1, 1, 30), charge(2, 1, 20), charge(1, 2, 10)])
    night = ledger.tally_event()
    assert (night.kind, night.spent_microusd, night.cap_microusd, night.calls) == (
        "tally",
        60,
        1000,
        3,
    )
    seat = ledger.tally_event("seat:1")
    assert (seat.spent_microusd, seat.calls, seat.role) == (40, 2, "seat:1")
    assert ledger.tally_event("seat:9").calls == 0


# ============================================================ the gate, through the runner


def capped(config: NightConfig, microusd: int | None) -> NightConfig:
    cap = None if microusd is None else microusd / 1_000_000
    limits = config.limits.model_copy(update={"max_cost_per_night_usd": cap})
    return config.model_copy(update={"limits": limits})


async def _no_sleep(seconds: float) -> None:
    return None


# What the fake costs per call: haiku 50, sonnet 1000, the two local models 348 each, so that a
# phase over seats 1 to 4 costs 1746 micro-dollars.
PHASE = 348 + 50 + 1000 + 348


def play_night(
    config: NightConfig,
    *,
    sdk: ScriptedSdk | None = None,
    lots: int = 2,
    sequential: bool = False,
    path: Path | None = None,
) -> tuple[FakeNight, Any, ScriptedSdk, Cassette]:
    sdk = sdk or ScriptedSdk()
    runner = ModelRunner(clients_for(config, sdk), clock=lambda: FIXED_TIME, sleep=_no_sleep)
    cassette = Cassette.record(path)
    session = NightSession.open(config, cassette, prices=example_prices(), settings=FAST)
    driver = FakeNight(config, lots=lots, sealed_sequential=sequential)
    run = asyncio.run(runner.run_night(driver, session))
    runner.close()
    return driver, run, sdk, cassette


def cap_events(driver: FakeNight) -> list[Any]:
    return [e for e in driver.log if e.type == "cost_event" and e.data.kind == "cap_reached"]  # type: ignore[union-attr]


def test_costs_of_a_night_add_up_per_seat() -> None:
    _, run, sdk, _ = play_night(api_config(), lots=2)
    assert run.status == "completed" and run.calls == sdk.call_count == 4 * 5
    by_seat = {k: v["cost_microusd"] for k, v in run.summary["by_role"].items()}
    assert by_seat == {"seat:1": 348 * 5, "seat:2": 50 * 5, "seat:3": 1000 * 5, "seat:4": 348 * 5}
    assert run.spent_microusd == 5 * PHASE == run.summary["total"]["cost_microusd"]
    assert run.summary["total"]["cache_read_input_tokens"] == 800 * 20
    final = run.events[-1].data
    assert (final.kind, final.spent_microusd, final.calls) == ("tally", 5 * PHASE, 20)  # type: ignore[union-attr]


def test_once_the_cap_is_reached_every_later_action_is_an_abstention() -> None:
    config = capped(api_config(), 4000)
    driver, run, sdk, cassette = play_night(config, lots=2)
    # plan 1746, lot 1 talk 1746 (3492), lot 1 sealed 1746 (5238): over the cap, after the phase
    (fired,) = cap_events(driver)
    assert (fired.data.spent_microusd, fired.data.cap_microusd, fired.data.calls) == (
        5238,
        4000,
        12,
    )
    assert fired.lot == 1 and fired.vis == "system"
    assert sdk.call_count == 12 and len(cassette) == 12  # nothing was sent after the cap
    assert run.status == "budget_aborted" and run.summary["aborted_actions"] == 8
    skipped = [
        e
        for e in driver.log
        if e.type == "action" and e.data.status == "abstain"  # type: ignore[union-attr]
    ]
    assert len(skipped) == 8 and {e.data.reason for e in skipped} == {"budget_aborted"}  # type: ignore[union-attr]
    assert {e.lot for e in skipped} == {2} and all(e.data.attempts == 1 for e in skipped)  # type: ignore[union-attr]
    # the event is in the log right after the actions of the phase that reached the cap
    log = [(e.type, e.lot) for e in driver.log]
    assert log.index(("cost_event", 1)) > max(
        i for i, (kind, lot) in enumerate(log) if kind == "action" and lot == 1
    )
    counters = run.counters
    assert counters["seat:1"]["abstentions"] == {"budget_aborted": 2}


def test_skipped_actions_send_nothing_and_leave_no_entry() -> None:
    config = capped(api_config(), 0)
    driver, run, sdk, cassette = play_night(config, lots=1)
    assert sdk.call_count == 0 and len(cassette) == 0
    (fired,) = cap_events(driver)
    assert (fired.data.spent_microusd, fired.data.cap_microusd, fired.data.calls) == (0, 0, 0)
    assert fired.lot is None and run.status == "budget_aborted" and run.spent_microusd == 0
    outcomes = [r.outcomes for r in driver.results]
    assert all(o.attempts == 0 and o.action_attempts == 1 for phase in outcomes for o in phase)
    assert all(o.reason == "budget_aborted" for phase in outcomes for o in phase)


def test_the_gate_is_checked_after_each_call_of_a_sequential_phase() -> None:
    config = capped(api_config(), 4000)
    driver, run, sdk, _ = play_night(config, lots=1, sequential=True)
    # plan and talk cost 3492; sealed seat 1 -> 3840, seat 2 -> 3890, seat 3 -> 4890: reached
    (fired,) = cap_events(driver)
    assert (fired.data.spent_microusd, fired.data.calls) == (4890, 11)
    last = driver.results[-1]
    assert [(o.seat, o.status, o.reason) for o in last.outcomes] == [
        (1, "ok", None),
        (2, "ok", None),
        (3, "ok", None),
        (4, "abstain", "budget_aborted"),
    ]
    assert [e.type for e in last.events] == ["cost_event"] and sdk.call_count == 11
    assert run.status == "budget_aborted" and run.summary["aborted_actions"] == 1


def test_a_concurrent_phase_is_never_cut_in_the_middle() -> None:
    config = capped(api_config(), 1000)  # the plan phase alone costs 1746
    driver, run, sdk, _ = play_night(config, lots=1)
    assert sdk.call_count == 4  # all four seats of the plan phase were asked
    assert all(o.ok for o in driver.results[0].outcomes)
    assert [e.type for e in driver.results[0].events] == ["cost_event"]
    assert run.status == "budget_aborted"


def test_a_cap_reached_by_the_last_call_does_not_abort_the_night() -> None:
    config = capped(api_config(), 3 * PHASE)  # lot 1 only: plan, talk, sealed
    driver, run, sdk, _ = play_night(config, lots=1)
    assert len(cap_events(driver)) == 1 and sdk.call_count == 12
    assert run.summary["aborted_actions"] == 0 and run.status == "completed"
    assert run.summary["cap_microusd"] == 3 * PHASE


def test_a_night_without_a_cap_only_keeps_accounts() -> None:
    driver, run, _, _ = play_night(capped(api_config(), None), lots=1)
    assert (
        cap_events(driver) == []
        and run.status == "completed"
        and run.summary["cap_microusd"] is None
    )


def test_a_replay_fires_the_gate_at_the_same_place_without_any_price(tmp_path: Path) -> None:
    config = capped(api_config(), 4000)
    path = tmp_path / "calls.jsonl"
    driver, run, sdk, _ = play_night(config, lots=2, path=path)
    cassette = Cassette.replay(path)
    runner = ModelRunner({}, clock=lambda: FIXED_TIME, sleep=_no_sleep)
    session = NightSession.open(config, cassette, settings=FAST)  # no price list, a cap, a replay
    again = FakeNight(config, lots=2)
    replayed = asyncio.run(runner.run_night(again, session))
    runner.close()
    assert again.events_text() == driver.events_text()
    assert replayed.status == run.status == "budget_aborted"
    assert replayed.summary == run.summary and cassette.unused() == []
    assert len(cap_events(again)) == 1


def test_recording_a_capped_night_needs_a_price_for_every_model() -> None:
    config = capped(api_config(), 4000)
    prices = PriceTable({"claude-haiku-5-5": HAIKU}, source="the test list")
    with pytest.raises(MissingPriceError, match="<model-a1>"):
        NightSession.open(config, Cassette.record(), prices=prices)
    NightSession.open(capped(api_config(), None), Cassette.record(), prices=prices)  # no cap
    NightSession.open(config, Cassette.from_text(""), prices=prices)  # a replay needs none


def test_models_without_a_price_are_counted_but_cost_nothing() -> None:
    config = api_config()
    sdk = ScriptedSdk()
    runner = ModelRunner(clients_for(config, sdk), clock=lambda: FIXED_TIME, sleep=_no_sleep)
    only_haiku = PriceTable({"claude-haiku-5-5": HAIKU})
    session = NightSession.open(config, Cassette.record(), prices=only_haiku, settings=FAST)
    driver = FakeNight(config, lots=1)
    run = asyncio.run(runner.run_night(driver, session))
    runner.close()
    total = run.summary["total"]
    assert total["calls"] == 12 and total["unpriced_calls"] == 9  # seats 1, 3 and 4
    assert run.spent_microusd == 50 * 3 == total["cost_microusd"]
    unpriced = driver.results[0].outcomes[0]
    assert unpriced.cost_microusd is None and driver.results[0].outcomes[1].cost_microusd == 50


def test_a_reply_without_a_usage_block_is_unmetered() -> None:
    def make(kind: str, ctx: Any) -> Any:
        completion = openai_completion(valid_args(ctx.phase), model=ctx.model)
        return completion.model_copy(update={"usage": None})

    config = api_config()
    sdk = ScriptedSdk({1: [Step(make)]})
    runner = ModelRunner(clients_for(config, sdk), clock=lambda: FIXED_TIME, sleep=_no_sleep)
    cassette = Cassette.record()
    session = NightSession.open(config, cassette, prices=example_prices(), settings=FAST)
    driver = FakeNight(config, lots=0, seats=(1,))
    run = asyncio.run(runner.run_night(driver, session))
    runner.close()
    assert run.summary["total"]["unmetered_calls"] == 1 and run.spent_microusd == 0
    assert cassette.entries[0].usage is None and cassette.entries[0].cost_microusd is None


# ========================================================================= the estimate


def test_the_default_night_asks_a_model_seat_27_and_a_quarter_times() -> None:
    parts = calls_breakdown(api_config())
    assert parts == {
        "plan": Fraction(1),
        "talk": Fraction(12),
        "sealed": Fraction(8),
        "dutch": Fraction(2),
        "english": Fraction(17, 4),
    }
    assert sum(parts.values(), Fraction(0)) == Fraction(109, 4)


def test_calls_follow_the_format_mix_and_the_table_size() -> None:
    config = api_config()
    night = config.night.model_copy(update={"formats": ("english", "sealed_first")})
    small = config.model_copy(update={"night": night.model_copy(update={"lots_per_format": 1})})
    # 1 plan, 2 talk, 1 sealed, and one English lot with 9 / 8 revision calls per seat
    assert sum(calls_breakdown(small).values(), Fraction(0)) == 1 + 2 + 1 + 1 + Fraction(9, 8)
    no_plan = config.model_copy(
        update={"night": config.night.model_copy(update={"plan_and_mindreading": False})}
    )
    assert calls_breakdown(no_plan)["plan"] == 0
    four = config.model_copy(update={"seats": config.seats[:4]})
    assert calls_breakdown(four)["english"] == 2 * (1 + Fraction(5, 4))  # 3 + 2 asks, 4 seats


def close(value: Decimal | None, expected: str) -> bool:
    return value is not None and abs(value - Decimal(expected)) < Decimal("0.001")


def test_the_estimate_matches_hand_arithmetic_for_the_two_claude_seats() -> None:
    table = PriceTable.from_file(REPO / "configs" / "prices.example.yaml")
    estimate = estimate_night(api_config(), table)
    rows = {row.seat: row for row in estimate.seats}
    assert sorted(rows) == [1, 2, 3, 4, 5, 6] and all(
        r.calls == Decimal("27.25") for r in rows.values()
    )
    haiku, sonnet = rows[2], rows[3]
    # prefix 2132 once as input, then 26.25 x 2132 cache reads, 26447 observation, 3331 output
    assert close(haiku.cost_low_microusd, "5083.05")  # 213.2 + 559.65 + 2644.7 + 1665.5
    assert close(haiku.cost_high_microusd, "20232.965")  # x1.3, plus 1000 thinking tokens a call
    assert close(sonnet.cost_low_microusd, "101661")
    assert close(sonnet.cost_high_microusd, "404659.3")
    low, high = estimate.total_microusd()
    assert not (rows[1].priced or rows[4].priced)  # nothing is invented for the others
    assert close(low, "106744.05") and close(high, "424892.265")


def test_the_published_cost_ranges_come_out_of_the_default_assumptions() -> None:
    estimate = estimate_night(api_config(), example_prices())
    haiku, sonnet = estimate.seats[1], estimate.seats[2]
    assert haiku.cost_low_microusd is not None and haiku.cost_high_microusd is not None
    assert sonnet.cost_low_microusd is not None and sonnet.cost_high_microusd is not None
    assert f"{haiku.cost_low_microusd / 1_000_000:.3f}" == "0.005"
    assert f"{haiku.cost_high_microusd / 1_000_000:.3f}" == "0.020"
    assert f"{sonnet.cost_low_microusd / 1_000_000:.2f}" == "0.10"
    assert f"{sonnet.cost_high_microusd / 1_000_000:.2f}" == "0.40"
    a = EstimateAssumptions()
    assert abs(a.observation_per_call * a.reference_calls - 26447) < Decimal("0.0001")


def test_other_models_have_no_tokenizer_factor() -> None:
    estimate = estimate_night(api_config(), example_prices())
    local = estimate.seats[0]  # <model-a1>, priced in the test list
    assert local.low.input == local.high.input  # factor 1.0 on a non-Anthropic endpoint
    assert estimate.seats[1].high.input > estimate.seats[1].low.input


def test_the_report_lists_unpriced_models_and_invents_no_price() -> None:
    table = PriceTable.from_file(REPO / "configs" / "prices.example.yaml")
    text = format_estimate(estimate_night(api_config(), table))
    assert "27.25 (plan 1, talk 12, sealed 8, dutch 2, english 4.25)" in text
    assert "2 bot, mock or human seats make no model calls." in text
    assert "$0.0051 - $0.0202" in text and "$0.1017 - $0.4047" in text
    assert "Total for 1 night, 2 of 6 model seats priced: $0.1067 - $0.4249" in text
    assert "No price for: <model-a1>, <model-a2>, <model-b1>, <model-b2>." in text
    # the rows of the show layer come below those of the seats and start with a letter
    seat_rows = [line for line in text.splitlines() if line[:1].isdigit()]
    unpriced_rows = [line for line in seat_rows if line.rstrip().endswith("no price")]
    assert len(unpriced_rows) == 4 and not any("$" in row for row in unpriced_rows)
    assert "Cost cap per night: none" in text and "Placeholders still in the configuration" in text


def test_without_any_price_nothing_is_estimated() -> None:
    text = format_estimate(estimate_night(api_config(), PriceTable()))
    assert "No model seat has a price, so there is no cost estimate." in text
    rows = [line for line in text.splitlines() if line[:1].isdigit() and line.endswith("no price")]
    assert len(rows) == 6 and "$" not in text


def test_a_night_without_model_seats_says_so() -> None:
    text = format_estimate(
        estimate_night(load_config(REPO / "configs" / "night_mock.yaml"), PriceTable())
    )
    assert "0 model seats" in text and "makes no model calls" in text


def test_several_nights_scale_the_total_only() -> None:
    one = estimate_night(api_config(), example_prices())
    twelve = estimate_night(api_config(), example_prices(), nights=12)
    assert twelve.total_microusd()[0] == one.total_microusd()[0] * 12
    assert [s.cost_low_microusd for s in twelve.seats] == [s.cost_low_microusd for s in one.seats]
    assert "Total for 12 nights" in format_estimate(twelve)


def test_the_cap_is_compared_with_the_high_estimate() -> None:
    low_cap = format_estimate(estimate_night(capped(api_config(), 200_000), example_prices()))
    assert (
        "Cost cap per night: $0.2000 (the high estimate for one night is above the cap)" in low_cap
    )
    high_cap = format_estimate(estimate_night(capped(api_config(), 5_000_000), example_prices()))
    assert (
        "Cost cap per night: $5.0000 (the high estimate for one night is within the cap)"
        in high_cap
    )


# ===================================================================== the command


def run_estimate(*args: str) -> Any:
    return cli.invoke(app, ["estimate", *args])


def test_the_command_prints_the_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPO)
    result = run_estimate("--config", "configs/night_api.example.yaml")
    assert result.exit_code == 0, result.output
    text = result.output
    assert text.startswith("Estimate for night s1-d03-r1: 6 model seats, 1 night")
    assert "Prices: configs/prices.example.yaml" in text
    assert "claude-haiku-5-5" in text and "$0.0051 - $0.0202" in text
    assert text.count("no price") >= 5  # four rows, the "No price for" line, the totals note


def test_the_price_list_is_found_from_any_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    result = run_estimate("--config", str(REPO / "configs" / "night_api.example.yaml"))
    assert result.exit_code == 0 and "$0.1017 - $0.4047" in result.output


def test_a_price_list_given_on_the_command_line_replaces_the_one_in_the_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(REPO)
    prices = tmp_path / "prices.yaml"
    prices.write_text(
        "version: 1\nprices:\n"
        '  "<model-a1>": {input_per_mtok: 0, output_per_mtok: 0, as_of: 2026-11-01,'
        " source: https://example.com/p}\n",
        encoding="utf-8",
    )
    result = run_estimate("--config", "configs/night_api.example.yaml", "--prices", str(prices))
    text = result.output
    assert result.exit_code == 0, text
    assert (
        "No price for: <model-a2>, <model-b1>, <model-b2>, claude-haiku-5-5, claude-sonnet-5-5."
        in text
    )
    assert "1 of 6 model seats priced: $0.0000" in text  # a free local model is a price of zero


def test_nights_and_the_high_estimate_can_be_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPO)
    result = run_estimate("-c", "configs/night_api.example.yaml", "--nights", "12")
    assert "Total for 12 nights, 2 of 6 model seats priced: $1.2809 - $5.0987" in result.output
    flat = run_estimate(
        "-c", "configs/night_api.example.yaml", "--thinking-high", "0", "--token-factor-high", "1"
    )
    haiku_row = next(line for line in flat.output.splitlines() if "claude-haiku-5-5" in line)
    assert re.search(r"27\.25\s+28,579\s+55,965\s+3,331\s+\$0\.0051$", haiku_row)
    assert "$0.0202" not in flat.output


def test_the_command_reports_bad_input_with_status_2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(REPO)
    assert run_estimate().exit_code == 2  # --config is required
    missing = run_estimate("--config", "nowhere.yaml")
    assert missing.exit_code == 2 and "configuration not found: nowhere.yaml" in missing.output
    broken = tmp_path / "broken.yaml"
    broken.write_text("night: {}\n", encoding="utf-8")
    result = run_estimate("--config", str(broken))
    assert result.exit_code == 2 and "cannot read" in result.output
    no_prices = run_estimate("--config", "configs/night_api.example.yaml", "--prices", "nope.yaml")
    assert no_prices.exit_code == 2 and "price list not found: nope.yaml" in no_prices.output
    text = (REPO / "configs" / "night_api.example.yaml").read_text(encoding="utf-8")
    renamed = tmp_path / "renamed.yaml"
    renamed.write_text(text.replace("prices.example.yaml", "gone.yaml"), encoding="utf-8")
    gone = run_estimate("--config", str(renamed))
    assert gone.exit_code == 2 and "price list not found: configs/gone.yaml" in gone.output


def test_a_cap_in_the_configuration_shows_in_the_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(REPO)
    text = (REPO / "configs" / "night_api.example.yaml").read_text(encoding="utf-8")
    capped_file = tmp_path / "capped.yaml"
    capped_file.write_text(
        text.replace("max_cost_per_night_usd: null", "max_cost_per_night_usd: 0.2"),
        encoding="utf-8",
    )
    result = run_estimate("--config", str(capped_file))
    assert (
        "Cost cap per night: $0.2000 (the high estimate for one night is above the cap)"
        in result.output
    )


def test_the_mock_night_makes_no_model_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPO)
    result = run_estimate("--config", "configs/night_mock.yaml")
    assert result.exit_code == 0 and "makes no model calls" in result.output


def test_the_example_price_file_is_valid_for_the_loader() -> None:
    assert sorted(load_prices(REPO / "configs" / "prices.example.yaml").prices) == [
        "claude-haiku-5-5",
        "claude-sonnet-5-5",
    ]
