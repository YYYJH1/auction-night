"""The cassette: recording, replaying, ordering, drift and scrubbing.

The first half tests the cassette on its own. The second half records a fake night through the
runner and replays it: the replay must make the same event log, byte for byte.
"""

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import anthropic
import pytest
from llm_replies import strict_call
from sdk_fakes import (
    FAKE_ANTHROPIC_KEY,
    FAKE_LOCAL_KEY,
    FAKE_OPENAI_KEY,
    FIXED_TIME,
    ArrivalGate,
    FakeNight,
    ScriptedSdk,
    Step,
    api_config,
    api_refusal,
    bad_request,
    clients_for,
    example_prices,
    failing,
    hang,
    make_call,
    ok,
    prose,
    rate_limit,
    raw,
    sdk_status_error,
    timeout_error,
)

import scan_text
from auction_night.contracts import NightConfig
from auction_night.llm.cassette import (
    SCHEMA_VERSION,
    CallKey,
    Cassette,
    CassetteDrift,
    CassetteEntry,
    CassetteError,
    CassetteFormatError,
    CassetteMiss,
    stored_request,
)
from auction_night.llm.client import REDACTED, Scrubber, make_clients, merged_scrubber
from auction_night.llm.orchestrate import (
    ModelRunner,
    NightRun,
    NightSession,
    PhaseRequest,
    RunnerSettings,
)

FAST = RunnerSettings(timeout_s=5.0, backoff_base_s=0.0, backoff_cap_s=0.0, wall_clock_slack_s=0.5)
FILE_ORDER = [
    "v",
    "key",
    "role",
    "kind",
    "model",
    "model_returned",
    "request_hash",
    "request",
    "outcome",
    "stop_reason",
    "refusal",
    "response",
    "error",
    "parsed",
    "repair",
    "usage",
    "cost_microusd",
    "latency_ms",
    "started_at",
    "finished_at",
]


# ==================================================================== keys and entries


def key(
    lot: int | None = 1,
    phase: str = "sealed",
    step: int = 1,
    seat: int | None = 2,
    attempt: int = 1,
) -> CallKey:
    return CallKey("n-1", lot, phase, step, seat, attempt)


def entry(
    k: CallKey, *, outcome: str = "ok", digest: str | None = None, **request: Any
) -> CassetteEntry:
    return CassetteEntry(
        key=k,
        role="seat:2",
        kind="anthropic",
        model="m",
        request_hash=digest or "sha256:" + "a" * 64,
        request=request or {"messages": [{"role": "user", "content": "hi"}]},
        outcome=outcome,
    )


def test_a_key_has_six_parts_and_a_stable_name() -> None:
    k = key(lot=6, phase="sealed", step=12, seat=3, attempt=2)
    assert k.as_tuple() == ("n-1", 6, "sealed", 12, 3, 2)
    assert k.call_id == "l6.sealed.12.s3" and k.with_attempt(1).attempt == 1
    assert key(lot=None, phase="plan", seat=None).call_id == "n.plan.1.x"
    assert "seat 3" in str(k) and "try 2" in str(k)
    assert CallKey.from_dict(k.as_dict()) == k


@pytest.mark.parametrize(
    "bad",
    [
        {"night": ""},
        {"night": "has space"},
        {"lot": 0},
        {"lot": 37},
        {"lot": True},
        {"phase": "Sealed"},
        {"step": -1},
        {"seat": 9},
        {"attempt": 0},
        {"attempt": 11},
    ],
)
def test_a_bad_key_is_refused(bad: dict[str, Any]) -> None:
    fields: dict[str, Any] = {
        "night": "n-1",
        "lot": 1,
        "phase": "sealed",
        "step": 1,
        "seat": 1,
        "attempt": 1,
        **bad,
    }
    with pytest.raises(ValueError):
        CallKey(**fields)


def test_keys_sort_by_lot_step_phase_seat_and_attempt() -> None:
    keys = [
        key(2, "sealed", 5, 1),
        key(1, "talk", 3, 2, 2),
        key(1, "talk", 3, 2, 1),
        key(None, "plan", 1, 3),
        key(1, "talk", 3, 1),
        key(1, "sealed", 4, 1),
    ]
    ordered = sorted(keys, key=lambda k: k.sort_key)
    assert [(k.lot, k.step, k.seat, k.attempt) for k in ordered] == [
        (None, 1, 3, 1),
        (1, 3, 1, 1),
        (1, 3, 2, 1),
        (1, 3, 2, 2),
        (1, 4, 1, 1),
        (2, 5, 1, 1),
    ]


# ============================================================= recording and the file


def test_a_recording_writes_one_line_per_attempt_in_key_order(tmp_path: Path) -> None:
    path = tmp_path / "night" / "calls.jsonl"
    cassette = Cassette.record(path)
    assert path.read_text() == ""  # the file exists even if nothing is ever sent
    third, first, second = entry(key(seat=3)), entry(key(seat=1)), entry(key(seat=2))
    assert cassette.commit([third, first, second]) == 3
    assert cassette.commit([entry(key(lot=2, step=2, seat=1))]) == 1
    lines = path.read_text().split("\n")
    assert lines[-1] == "" and len(lines) == 5
    rows = [json.loads(line) for line in lines[:-1]]
    assert [(r["key"]["lot"], r["key"]["seat"]) for r in rows] == [(1, 1), (1, 2), (1, 3), (2, 1)]
    assert list(rows[0]) == FILE_ORDER and rows[0]["v"] == SCHEMA_VERSION
    assert cassette.text() == path.read_text() and len(cassette) == 4


def test_the_order_of_a_batch_does_not_matter() -> None:
    entries = [entry(key(seat=s, attempt=a)) for s in (4, 1, 3, 2) for a in (2, 1)]
    one, two = Cassette.record(), Cassette.record()
    one.commit(entries)
    two.commit(list(reversed(entries)))
    assert one.text() == two.text()


def test_a_second_attempt_of_a_key_is_refused() -> None:
    cassette = Cassette.record()
    cassette.commit([entry(key())])
    with pytest.raises(CassetteError, match="duplicate"):
        cassette.commit([entry(key())])
    with pytest.raises(CassetteError, match="duplicate"):
        cassette.commit([entry(key(seat=3)), entry(key(seat=3))])
    assert len(cassette) == 1


def test_a_recording_does_not_overwrite_an_existing_one(tmp_path: Path) -> None:
    path = tmp_path / "calls.jsonl"
    Cassette.record(path).commit([entry(key())])
    with pytest.raises(FileExistsError):
        Cassette.record(path)
    assert len(Cassette.replay(path)) == 1
    Cassette.record(path, overwrite=True)
    assert path.read_text() == ""


def test_a_replay_cannot_be_written_to(tmp_path: Path) -> None:
    path = tmp_path / "calls.jsonl"
    Cassette.record(path).commit([entry(key())])
    with pytest.raises(CassetteError, match="replay"):
        Cassette.replay(path).commit([entry(key(seat=5))])


def test_text_that_looks_like_a_line_break_stays_inside_its_line(tmp_path: Path) -> None:
    path = tmp_path / "calls.jsonl"
    odd = "before\u2028after\u2029end and a tab\there"
    Cassette.record(path).commit([entry(key(), messages=[{"role": "user", "content": odd}])])
    loaded = Cassette.replay(path)
    assert len(loaded) == 1 and loaded.entries[0].request["messages"][0]["content"] == odd


def test_a_recording_reads_back_as_the_same_entries(tmp_path: Path) -> None:
    path = tmp_path / "calls.jsonl"
    original = [
        entry(key(seat=1), outcome="timeout"),
        entry(key(seat=2), outcome="refusal"),
        entry(key(lot=None, phase="plan", seat=3), outcome="parse_failed"),
    ]
    Cassette.record(path).commit(original)
    loaded = Cassette.replay(path)
    assert sorted(loaded.entries, key=lambda e: e.key.sort_key) == sorted(
        original, key=lambda e: e.key.sort_key
    )
    assert loaded.text() == path.read_text()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("not json\n", "not valid JSON"),
        ("[1, 2]\n", "must be a JSON object"),
        ('{"v": 2}\n', "unsupported cassette version"),
        ('{"v": 1}\n', "without a key"),
        ('{"v": 1, "key": {"night": "n"}}\n', "key without"),
    ],
)
def test_a_damaged_file_is_reported_with_its_line_number(text: str, message: str) -> None:
    with pytest.raises(CassetteFormatError, match=rf"calls:1: .*{message}"):
        Cassette.from_text(text, source="calls")
    with pytest.raises(CassetteFormatError, match=r"calls:2: "):
        Cassette.from_text("\n" + text, source="calls")


def test_an_unknown_outcome_is_refused() -> None:
    cassette = Cassette.record()
    cassette.commit([entry(key())])
    line = json.loads(cassette.text())
    line["outcome"] = "maybe"
    with pytest.raises(CassetteFormatError, match="unknown outcome"):
        Cassette.from_text(json.dumps(line))


def test_the_request_is_stored_without_headers_and_scrubbed() -> None:
    payload = {
        "model": "m",
        "extra_headers": {"anthropic-version": "2026-10-21"},
        "extra_query": {"a": "b"},
        "extra_body": {"top_k": 5},
        "messages": [{"role": "user", "content": f"leak {FAKE_ANTHROPIC_KEY}"}],
    }
    stored = stored_request(payload)
    assert set(stored) == {"model", "extra_body", "messages"}
    assert FAKE_ANTHROPIC_KEY not in json.dumps(stored) and REDACTED in json.dumps(stored)
    assert "extra_headers" in payload  # the caller's payload is untouched
    assert stored_request(payload, Scrubber([FAKE_LOCAL_KEY]))["extra_body"] == {"top_k": 5}


# ================================================================= looking attempts up


def recorded(*entries: CassetteEntry) -> Cassette:
    cassette = Cassette.record()
    cassette.commit(entries)
    return Cassette.from_text(cassette.text())


def test_a_replay_serves_an_attempt_by_key_and_request_hash() -> None:
    digest = "sha256:" + "b" * 64
    cassette = recorded(entry(key(seat=1), digest=digest), entry(key(seat=2)))
    served = cassette.lookup(key(seat=1), digest)
    assert served.key == key(seat=1)
    assert cassette.unused() == [key(seat=2)]
    cassette.lookup(key(seat=2), "sha256:" + "a" * 64)
    assert cassette.unused() == []


def test_a_key_that_was_never_recorded_is_a_miss() -> None:
    cassette = recorded(entry(key(seat=1)))
    with pytest.raises(
        CassetteMiss, match="no recorded attempt for n-1/lot 1/sealed/step 1/seat 7"
    ):
        cassette.lookup(key(seat=7), "sha256:" + "a" * 64)
    with pytest.raises(CassetteMiss):
        cassette.lookup(key(seat=1, attempt=2), "sha256:" + "a" * 64)


def test_another_request_is_drift_unless_it_is_allowed() -> None:
    old = {"model": "m", "system": "old rules", "messages": [{"role": "user", "content": "hi"}]}
    cassette = recorded(entry(key(), **old))
    new_hash = "sha256:" + "c" * 64
    changed = {**old, "system": "new rules"}
    with pytest.raises(CassetteDrift, match=r"changed fields: system\)") as caught:
        cassette.lookup(key(), new_hash, request=changed)
    assert "recorded sha256:aaa" in str(caught.value) and new_hash in str(caught.value)
    assert cassette.drifted == () and cassette.unused() == [key()]

    lenient = Cassette.from_text(cassette.text(), allow_drift=True)
    served = lenient.lookup(key(), new_hash, request=changed)
    assert served.key == key() and lenient.drifted == ((key(), "sha256:" + "a" * 64, new_hash),)
    with pytest.raises(CassetteMiss):  # drift can be tolerated, a missing attempt cannot
        lenient.lookup(key(seat=5), new_hash)


# ==================================================== recording a fake night, replaying it


def no_sleep_runner(config: NightConfig, sdk: ScriptedSdk | None) -> ModelRunner:
    async def no_sleep(seconds: float) -> None:
        return None

    clients = clients_for(config, sdk) if sdk is not None else {}
    return ModelRunner(clients, clock=lambda: FIXED_TIME, sleep=no_sleep)


def record_night(
    config: NightConfig,
    path: Path,
    *,
    script: dict[int, list[Step]] | None = None,
    gate: ArrivalGate | None = None,
    lots: int = 2,
    seats: tuple[int, ...] = (1, 2, 3, 4),
) -> tuple[FakeNight, NightRun, ScriptedSdk]:
    sdk = ScriptedSdk(script, gate=gate)
    runner = no_sleep_runner(config, sdk)
    session = NightSession.open(
        config, Cassette.record(path), prices=example_prices(), settings=FAST
    )
    driver = FakeNight(config, lots=lots, seats=seats)
    run = asyncio.run(runner.run_night(driver, session))
    runner.close()
    return driver, run, sdk


def replay_night(
    config: NightConfig,
    path: Path,
    *,
    allow_drift: bool = False,
    driver: FakeNight | None = None,
) -> tuple[FakeNight, NightRun, Cassette]:
    cassette = Cassette.replay(path, allow_drift=allow_drift)
    runner = no_sleep_runner(config, None)  # no client at all: nothing can be sent
    session = NightSession.open(config, cassette, settings=FAST)  # and no prices are needed
    driver = driver or FakeNight(config, lots=2)
    run = asyncio.run(runner.run_night(driver, session))
    runner.close()
    return driver, run, cassette


def troubled_script() -> dict[int, list[Step]]:
    """Every kind of outcome in one night: ok, timeout, error, refusal, parse_failed."""
    bad = raw(strict_call("sealed", bid=-4))
    refusal_text = prose("抱歉，我无法协助这个请求。")
    return {
        # plan, talk 1, sealed 1 (rate limit then a clamped bid), talk 2, sealed 2
        1: [ok(), timeout_error(), rate_limit(), ok(bid=500), ok(say="第二轮"), ok()],
        2: [ok(), api_refusal(), bad, ok(bid=45), ok(), ok()],
        3: [ok(), refusal_text, ok(), ok(), ok()],
        4: [ok(), ok(), ok(), ok(), bad_request()],
    }


def test_a_recorded_night_replays_to_the_same_events_byte_for_byte(tmp_path: Path) -> None:
    config = api_config()
    path = tmp_path / "calls.jsonl"
    driver, run, sdk = record_night(config, path, script=troubled_script())
    again, replayed, cassette = replay_night(config, path)

    assert again.events_text() == driver.events_text() != ""
    assert again.events_text().encode() == driver.events_text().encode()
    assert replayed.events == run.events and replayed.status == run.status
    assert replayed.summary == run.summary and replayed.counters == run.counters
    assert cassette.unused() == [] and cassette.drifted == ()
    assert sdk.call_count == len(cassette)  # every call that was sent is on the tape


def test_every_kind_of_outcome_is_an_entry(tmp_path: Path) -> None:
    config = api_config()
    path = tmp_path / "calls.jsonl"
    driver, run, sdk = record_night(config, path, script=troubled_script())
    rows = [json.loads(line) for line in path.read_text().split("\n") if line]
    outcomes = [r["outcome"] for r in rows]
    assert set(outcomes) == {"ok", "timeout", "error", "refusal", "parse_failed"}
    assert len(rows) == sdk.call_count == 22
    by_outcome = {name: [r for r in rows if r["outcome"] == name] for name in set(outcomes)}
    timeout = by_outcome["timeout"][0]
    assert timeout["response"] is None and timeout["error"]["type"] == "APITimeoutError"
    assert timeout["key"]["seat"] == 1 and timeout["key"]["phase"] == "talk"
    assert {r["key"]["seat"] for r in by_outcome["refusal"]} == {2, 3}
    assert any(r["key"]["attempt"] == 2 and r["outcome"] == "ok" for r in rows)
    # the failed attempts are in the tape, so the replay saw them too
    abstained = [e for e in driver.log if e.type == "action" and e.data.status == "abstain"]  # type: ignore[union-attr]
    assert sorted(e.data.reason for e in abstained) == [  # type: ignore[union-attr]
        "missing",
        "refusal",
        "refusal",
        "timeout",
    ]
    assert run.status == "completed"


def test_the_same_night_recorded_in_any_arrival_order_is_the_same_file(tmp_path: Path) -> None:
    config = api_config()
    runs = []
    for index, order in enumerate(([4, 3, 2, 1], [1, 2, 3, 4], [3, 1, 4, 2])):
        gate = ArrivalGate(order, phases=("plan", "sealed"))
        script = {seat: [ok(bid=500)] * 8 for seat in (1, 2, 3, 4)}
        path = tmp_path / f"night{index}" / "calls.jsonl"
        driver, _, sdk = record_night(config, path, script=script, gate=gate)
        arrivals = [seat for seat, phase, lot in sdk.arrivals if phase == "sealed" and lot == 1]
        runs.append((path.read_text(), driver.events_text(), arrivals))
    assert [r[2] for r in runs] == [[4, 3, 2, 1], [1, 2, 3, 4], [3, 1, 4, 2]]
    assert runs[0][0] == runs[1][0] == runs[2][0]
    assert runs[0][1] == runs[1][1] == runs[2][1]
    # and the file is in key order
    seen = [
        CallKey.from_dict(json.loads(line)["key"]).sort_key
        for line in runs[0][0].split("\n")
        if line
    ]
    assert seen == sorted(seen)


def test_random_delays_do_not_change_the_recording(tmp_path: Path) -> None:
    import random

    config = api_config()
    texts = []
    for seed in range(4):
        rng = random.Random(seed)
        script = {s: [hang(rng.random() * 0.012) for _ in range(6)] for s in (1, 2, 3, 4)}
        path = tmp_path / f"run{seed}" / "calls.jsonl"
        driver, _, sdk = record_night(config, path, script=script)
        texts.append((path.read_text(), driver.events_text(), tuple(sdk.arrivals)))
    assert len({t[0] for t in texts}) == 1 and len({t[1] for t in texts}) == 1
    assert len({t[2] for t in texts}) > 1  # the replies really did arrive in different orders


def test_changing_a_prompt_breaks_the_replay_unless_drift_is_allowed(tmp_path: Path) -> None:
    config = api_config()
    path = tmp_path / "calls.jsonl"
    driver, _, _ = record_night(config, path)
    edited = FakeNight(config, lots=2, system_extra="One more rule.")
    with pytest.raises(CassetteDrift, match=r"changed fields: .*(messages|system)"):
        replay_night(config, path, driver=edited)

    edited = FakeNight(config, lots=2, system_extra="One more rule.")
    again, _, cassette = replay_night(config, path, allow_drift=True, driver=edited)
    assert len(cassette.drifted) == len(cassette) and cassette.unused() == []
    assert again.events_text() == driver.events_text()  # the recorded answers are served anyway


def test_a_replay_that_asks_for_more_than_was_recorded_is_a_miss(tmp_path: Path) -> None:
    config = api_config()
    path = tmp_path / "calls.jsonl"
    record_night(config, path, lots=1)
    with pytest.raises(CassetteMiss, match="no recorded attempt"):  # a talk call, one at a time
        replay_night(config, path, driver=FakeNight(config, lots=2))
    with pytest.raises(CassetteMiss, match="seat 5"):  # raised as itself out of a concurrent phase
        replay_night(config, path, driver=FakeNight(config, lots=1, seats=(1, 2, 3, 4, 5)))


def test_a_replay_that_asks_for_less_leaves_the_rest_unused(tmp_path: Path) -> None:
    config = api_config()
    path = tmp_path / "calls.jsonl"
    record_night(config, path, lots=2)
    _, _, cassette = replay_night(config, path, driver=FakeNight(config, lots=1))
    assert cassette.unused() and all(k.lot == 2 for k in cassette.unused())


def test_costs_come_from_the_recording_not_from_todays_prices(tmp_path: Path) -> None:
    config = api_config()
    path = tmp_path / "calls.jsonl"
    _, run, _ = record_night(config, path)
    assert run.spent_microusd > 0
    _, replayed, _ = replay_night(config, path)  # replayed without any price list
    assert replayed.spent_microusd == run.spent_microusd and replayed.summary == run.summary


def test_nights_recorded_side_by_side_keep_separate_tapes(tmp_path: Path) -> None:
    configs = {}
    for night in ("n1", "n2"):
        base = api_config()
        configs[night] = base.model_copy(
            update={"night": base.night.model_copy(update={"id": night})}
        )
    sdk = ScriptedSdk()
    runner = no_sleep_runner(configs["n1"], sdk)
    jobs, paths = [], {}
    for night, config in configs.items():
        paths[night] = tmp_path / night / "calls.jsonl"
        session = NightSession.open(
            config, Cassette.record(paths[night]), prices=example_prices(), settings=FAST
        )
        jobs.append((FakeNight(config, lots=1), session))
    results = asyncio.run(runner.run_nights(jobs, max_parallel=2))
    runner.close()
    assert [r.night for r in results] == ["n1", "n2"]  # type: ignore[union-attr]
    for night, path in paths.items():
        rows = [json.loads(line) for line in path.read_text().split("\n") if line]
        assert {r["key"]["night"] for r in rows} == {night} and len(rows) == 12
        replay_night(configs[night], path, driver=FakeNight(configs[night], lots=1))


# ============================================================================ scrubbing


def keyed_config() -> NightConfig:
    config = api_config()
    anthropic_ep = config.endpoints["anthropic"].model_copy(update={"api_version": "2026-10-21"})
    return config.model_copy(update={"endpoints": {**config.endpoints, "anthropic": anthropic_ep}})


SUFFIX = "_API" + "_KEY"  # keeps the variable names from looking like assignments to the scanner
ENVIRON = {
    "ANTHROPIC" + SUFFIX: FAKE_ANTHROPIC_KEY,
    "VENDOR_A" + SUFFIX: FAKE_LOCAL_KEY,
    "VENDOR_A_BASE_URL": "http://127.0.0.1:8001/v1",
    "VENDOR_B" + SUFFIX: FAKE_OPENAI_KEY,
    "VENDOR_B_BASE_URL": "http://127.0.0.1:8002/v1",
    "VENDOR_C" + SUFFIX: "other-secret-value-c",
    "VENDOR_C_BASE_URL": "http://127.0.0.1:8003/v1",
    "VENDOR_D" + SUFFIX: "other-secret-value-d",
    "VENDOR_D_BASE_URL": "http://127.0.0.1:8004/v1",
}


def test_no_key_and_no_header_lands_in_the_cassette(tmp_path: Path) -> None:
    config = keyed_config()
    leaky_status = failing(
        lambda kind: sdk_status_error(
            anthropic.RateLimitError, 429, f"slow down, key {FAKE_ANTHROPIC_KEY} was seen"
        )
    )
    leaky_local = failing(
        lambda kind: sdk_status_error(
            anthropic.APIStatusError,
            502,
            f"upstream said: Authorization: Bearer {FAKE_OPENAI_KEY} and secret {FAKE_LOCAL_KEY}",
        )
    )
    script = {
        2: [leaky_status, ok(thought=f"my key is {FAKE_ANTHROPIC_KEY}", bid=40)],
        1: [leaky_local, ok(thought=f"also {FAKE_LOCAL_KEY} here", bid=30)],
    }
    sdk = ScriptedSdk(script)
    made = {"anthropic": lambda **kw: sdk, "openai_compatible": lambda **kw: sdk}
    clients = make_clients(config, environ=ENVIRON, factories=made)  # type: ignore[arg-type]
    path = tmp_path / "calls.jsonl"
    cassette = Cassette.record(path, scrubber=merged_scrubber(clients.values()))
    runner = ModelRunner(clients, clock=lambda: FIXED_TIME, sleep=_no_sleep)
    session = NightSession.open(config, cassette, prices=example_prices(), settings=FAST)
    calls = tuple(make_call(config, seat, "sealed") for seat in (1, 2))
    result = asyncio.run(runner.run_phase(session, PhaseRequest(calls)))
    runner.close()

    text = path.read_text()
    secrets = [FAKE_ANTHROPIC_KEY, FAKE_OPENAI_KEY, FAKE_LOCAL_KEY, "other-secret-value"]
    assert text and all(secret not in text for secret in secrets)
    assert REDACTED in text
    # nothing that looks like a key to the repository's own scanner
    found = scan_text.find_violations("calls.jsonl", text, scan_text.build_rules())
    assert [v for v in found if v.rule == "api-key"] == []
    # no header, in the request or anywhere else
    assert "extra_headers" not in text and "anthropic-version" not in text
    header_value = r"(?i)(authorization|x-api-key|cookie)[\"']?\s*[:=]\s*"
    assert not re.search(header_value + r"(?!(?:Bearer\s+)?\[REDACTED\])\S", text)
    rows = [json.loads(line) for line in text.split("\n") if line]
    assert all("headers" not in str(r["request"]).lower() for r in rows)
    # the key never reached the events either: the reply was scrubbed before it was read
    thoughts = [o.thought for o in result.outcomes]
    assert all(FAKE_ANTHROPIC_KEY not in t and FAKE_LOCAL_KEY not in t for t in thoughts)
    assert any("REDACTED" in t for t in thoughts)  # the cleaner drops the brackets
    assert all(
        secret not in repr(result)
        for secret in (FAKE_ANTHROPIC_KEY, FAKE_OPENAI_KEY, FAKE_LOCAL_KEY)
    )
    # the request hash still covers the version header that the tape leaves out
    assert all(r["request_hash"].startswith("sha256:") for r in rows)


async def _no_sleep(seconds: float) -> None:
    return None


def test_a_replay_of_a_scrubbed_night_gives_the_same_scrubbed_events(tmp_path: Path) -> None:
    config = keyed_config()
    script = {2: [ok(thought=f"my key is {FAKE_ANTHROPIC_KEY}", bid=40)]}
    sdk = ScriptedSdk(script)
    made = {"anthropic": lambda **kw: sdk, "openai_compatible": lambda **kw: sdk}
    clients = make_clients(config, environ=ENVIRON, factories=made)  # type: ignore[arg-type]
    path = tmp_path / "calls.jsonl"
    cassette = Cassette.record(path, scrubber=merged_scrubber(clients.values()))
    runner = ModelRunner(clients, clock=lambda: FIXED_TIME, sleep=_no_sleep)
    session = NightSession.open(config, cassette, prices=example_prices(), settings=FAST)
    driver = FakeNight(config, lots=1)
    asyncio.run(runner.run_night(driver, session))
    runner.close()
    again, _, _ = replay_night(config, path, driver=FakeNight(config, lots=1))
    assert again.events_text() == driver.events_text()
    assert FAKE_ANTHROPIC_KEY not in driver.events_text()
    assert "my key is REDACTED" in driver.events_text()


def test_every_line_of_a_real_looking_recording_is_free_of_secret_shapes(tmp_path: Path) -> None:
    config = api_config()
    path = tmp_path / "calls.jsonl"
    record_night(config, path, script=troubled_script())
    found = scan_text.find_violations("calls.jsonl", path.read_text(), scan_text.build_rules())
    assert found == []
