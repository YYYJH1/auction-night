"""A model judge goes through the client and the cassette: a replay needs no network."""

from datetime import date
from pathlib import Path
from typing import Any

import httpx2
import openai
import pytest
from judge_support import FakeJudgeClient, judge_config, mock_night, user_text
from show_support import completion

from auction_night.contracts import NightConfig
from auction_night.contracts.config import ModelPrice
from auction_night.contracts.events import canonical_json
from auction_night.engine.night import NightState
from auction_night.llm.accounting import Ledger, PriceTable
from auction_night.llm.cassette import Cassette, CassetteDrift, CassetteMiss
from auction_night.llm.request import BuiltRequest
from auction_night.prompts import TemplateSet
from auction_night.show.judge_input import JudgeInput, build_judge_input
from auction_night.show.judges import (
    JudgeBackend,
    JudgeRun,
    MockJudge,
    ModelJudge,
    judge_events,
    judge_job,
    load_judge_templates,
    make_judges,
    run_judges,
)

SEED = "judge-cassette-1"
CLOCK = "2026-11-01T00:00:00.000Z"
GARBAGE = "今晚的拍卖很有意思，我就不一条条列了。"


@pytest.fixture(scope="module")
def played() -> tuple[NightState, JudgeInput]:
    state = mock_night(SEED)
    return state, build_judge_input(state.events)


@pytest.fixture(scope="module")
def good_text(played: tuple[NightState, JudgeInput]) -> str:
    """What a sensible judge would write for this night: the mock judge's answer."""
    return MockJudge().write(judge_job(0, played[1])).text


def clients_for(*responders: Any, kind: str = "openai_compatible") -> dict[str, FakeJudgeClient]:
    """One client per judge endpoint; each answers with its own responder."""
    return {f"judge_ep_{i}": FakeJudgeClient(r, kind) for i, r in enumerate(responders)}


def make(
    config: NightConfig,
    cassette: Cassette,
    clients: dict[str, FakeJudgeClient] | None,
    **extra: Any,
) -> tuple[JudgeBackend, ...]:
    extra.setdefault("clock", lambda: CLOCK)
    extra.setdefault("sleep", lambda s: None)
    return make_judges(config, cassette=cassette, clients=clients, **extra)


@pytest.fixture
def recorded(
    played: tuple[NightState, JudgeInput], good_text: str, tmp_path: Path
) -> tuple[Path, JudgeRun, dict[str, FakeJudgeClient]]:
    """Both judges on the cassette; judge 0 first writes words only and is asked again."""

    def first(request: BuiltRequest, n: int) -> str:
        return GARBAGE if n == 1 else good_text

    clients = clients_for(first, lambda r, n: good_text)
    path = tmp_path / "judge_calls.jsonl"
    judges = make(judge_config(), Cassette.record(path), clients)
    return path, run_judges(played[1], judges), clients


# ------------------------------------------------------------------------------ record


def test_each_judge_writes_through_its_client_and_every_attempt_is_an_entry(recorded: Any) -> None:
    path, run, clients = recorded
    assert len(clients["judge_ep_0"].requests) == 2  # the first answer could not be used
    assert len(clients["judge_ep_1"].requests) == 1
    assert run.both and [r.attempts for r in run.results] == [2, 1]
    cassette = Cassette.replay(path)
    keys = [k.as_tuple() for k in cassette.keys()]  # noqa: SIM118 - a method, not a dict
    assert keys == [
        ("mock-0001", None, "judge_extract", 0, None, 1),
        ("mock-0001", None, "judge_extract", 0, None, 2),
        ("mock-0001", None, "judge_extract", 1, None, 1),
    ]
    entries = cassette.entries
    assert [e.role for e in entries] == ["judge:0", "judge:0", "judge:1"]
    assert [e.outcome for e in entries] == ["ok", "ok", "ok"]
    assert [e.model for e in entries] == ["judge-model-0", "judge-model-0", "judge-model-1"]
    assert entries[0].model_returned == "judge-model-0-0001"
    assert all(e.usage is not None and e.request_hash.startswith("sha256:") for e in entries)
    assert [d.model_returned for d in run.extracts] == ["judge-model-0-0001", "judge-model-1-0001"]
    assert [d.vendor for d in run.extracts] == ["vendor-j0", "vendor-j1"]


def test_requests_carry_no_tools_and_the_record(recorded: Any, played: Any) -> None:
    _, _, clients = recorded
    _, record = played
    for client in clients.values():
        for request in client.requests:
            assert "tools" not in request.payload and "tool_choice" not in request.payload
            assert request.payload["messages"][0]["role"] == "system"
            assert record.json() in user_text(request)


def test_the_second_try_repeats_the_request_and_appends_the_exchange(recorded: Any) -> None:
    _, _, clients = recorded
    first, second = clients["judge_ep_0"].requests
    kept = first.payload["messages"]
    assert second.payload["messages"][: len(kept)] == kept
    assert [m["role"] for m in second.payload["messages"]] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert second.payload["messages"][2]["content"] == GARBAGE
    assert "读不出一个 JSON 对象" in second.payload["messages"][3]["content"]
    assert first.request_hash != second.request_hash


def test_both_judges_start_from_the_same_system_prompt(recorded: Any) -> None:
    _, _, clients = recorded
    systems = {r.payload["messages"][0]["content"] for c in clients.values() for r in c.requests}
    assert len(systems) == 1


def test_the_cassette_holds_no_credentials(recorded: Any) -> None:
    path, _, _ = recorded
    text = path.read_text(encoding="utf-8")
    assert "extra_headers" not in text and "api_key" not in text.lower()


# ------------------------------------------------------------------------------ replay


def test_a_replay_needs_no_client_and_gives_the_same_bytes(played: Any, recorded: Any) -> None:
    state, record = played
    path, run, _ = recorded
    slept: list[float] = []
    again = run_judges(
        record, make(judge_config(), Cassette.replay(path), None, sleep=slept.append)
    )
    assert again.extracts == run.extracts and slept == []
    one = "".join(canonical_json(e) for e in judge_events(run))
    two = "".join(canonical_json(e) for e in judge_events(again))
    assert one == two


def test_a_replay_serves_every_recorded_attempt_and_no_other(played: Any, recorded: Any) -> None:
    _, record = played
    path, _, _ = recorded
    cassette = Cassette.replay(path)
    run_judges(record, make(judge_config(), cassette, None))
    assert cassette.unused() == [] and cassette.drifted == ()


def test_a_changed_prompt_is_a_drift(played: Any, recorded: Any) -> None:
    _, record = played
    path, _, _ = recorded
    base = load_judge_templates()
    texts = dict(base.texts)
    texts["judge.user"] = texts["judge.user"] + "\n多说一句。"
    changed = TemplateSet(base.lang, base.version, texts)
    judges = make(judge_config(), Cassette.replay(path), None)
    with pytest.raises(CassetteDrift):
        run_judges(record, judges, templates=changed)


def test_another_night_is_a_drift(recorded: Any) -> None:
    path, _, _ = recorded
    other = build_judge_input(mock_night("judge-cassette-2").events)
    judges = make(judge_config(), Cassette.replay(path), None)
    with pytest.raises(CassetteDrift):
        run_judges(other, judges)


def test_a_missing_attempt_is_a_miss(played: Any, recorded: Any) -> None:
    _, record = played
    path, _, _ = recorded
    lines = path.read_text(encoding="utf-8").splitlines()
    kept = [line for line in lines if '"attempt":2' not in line]
    assert len(kept) == len(lines) - 1
    judges = make(judge_config(), Cassette.from_text("\n".join(kept) + "\n"), None)
    with pytest.raises(CassetteMiss):
        run_judges(record, judges)


def test_recording_without_a_client_is_refused(played: Any, tmp_path: Path) -> None:
    _, record = played
    judges = make(judge_config(), Cassette.record(tmp_path / "c.jsonl"), None)
    with pytest.raises(RuntimeError, match="needs a client"):
        run_judges(record, judges)


# ------------------------------------------------------------------------------ faults


def test_a_timeout_makes_that_judge_absent_and_is_recorded(
    played: Any, good_text: str, tmp_path: Path
) -> None:
    _, record = played
    clients = clients_for(lambda r, n: good_text, lambda r, n: TimeoutError("too slow"))
    path = tmp_path / "c.jsonl"
    run = run_judges(record, make(judge_config(), Cassette.record(path), clients))
    assert [r.status for r in run.results] == ["ok", "absent"]
    assert (run.results[1].reason, run.results[1].attempts) == ("no_answer", 1)
    assert not run.both and len(judge_events(run)) == 1
    assert len(clients["judge_ep_1"].requests) == 1  # no second try after a timeout
    entry = next(e for e in Cassette.replay(path).entries if e.key.step == 1)
    assert entry.outcome == "timeout" and entry.error is not None and entry.response is None
    again = run_judges(record, make(judge_config(), Cassette.replay(path), None))
    assert [r.status for r in again.results] == ["ok", "absent"]
    assert again.extracts == run.extracts
    row = run.absences[0]
    assert (row.judge, row.vendor, row.reason) == (1, "vendor-j1", "timeout")
    assert again.absences == run.absences, "a replay says the same, from the recording alone"


def test_a_refusal_makes_the_judge_absent(played: Any, good_text: str, tmp_path: Path) -> None:
    _, record = played
    refusal = {
        "id": "refused-1",
        "model": "judge-model-1-0001",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": None, "refusal": "blocked"},
                "finish_reason": "content_filter",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 0, "total_tokens": 10},
    }
    clients = clients_for(lambda r, n: good_text, lambda r, n: refusal)
    path = tmp_path / "c.jsonl"
    run = run_judges(record, make(judge_config(), Cassette.record(path), clients))
    assert run.results[1].status == "absent" and run.results[1].reason == "no_answer"
    assert len(clients["judge_ep_1"].requests) == 1
    entry = next(e for e in Cassette.replay(path).entries if e.key.step == 1)
    assert entry.outcome == "refusal" and entry.refusal
    assert [(a.judge, a.reason) for a in run.absences] == [(1, "refusal")]
    again = run_judges(record, make(judge_config(), Cassette.replay(path), None))
    assert again.absences == run.absences


def test_a_dropped_connection_is_retried_after_a_pause(
    played: Any, good_text: str, tmp_path: Path
) -> None:
    _, record = played
    boom = openai.APIConnectionError(request=httpx2.Request("POST", "http://localhost:9/v1"))
    clients = clients_for(lambda r, n: boom if n == 1 else good_text, lambda r, n: good_text)
    path = tmp_path / "c.jsonl"
    slept: list[float] = []
    judges = make(judge_config(), Cassette.record(path), clients, sleep=slept.append)
    run = run_judges(record, judges)
    assert run.both and slept == [1.0] and len(clients["judge_ep_0"].requests) == 2
    entries = [e for e in Cassette.replay(path).entries if e.key.step == 0]
    assert [(e.key.attempt, e.outcome) for e in entries] == [(1, "error"), (2, "ok")]
    replay_slept: list[float] = []
    judges = make(judge_config(), Cassette.replay(path), None, sleep=replay_slept.append)
    assert run_judges(record, judges).extracts == run.extracts and replay_slept == []


def test_retries_stop_after_the_limit(played: Any, good_text: str, tmp_path: Path) -> None:
    _, record = played
    boom = openai.APIConnectionError(request=httpx2.Request("POST", "http://localhost:9/v1"))
    clients = clients_for(lambda r, n: boom, lambda r, n: good_text)
    judges = make(
        judge_config(), Cassette.record(tmp_path / "c.jsonl"), clients, transient_retries=1
    )
    run = run_judges(record, judges)
    assert run.results[0].status == "absent" and len(clients["judge_ep_0"].requests) == 2
    assert [(a.judge, a.reason) for a in run.absences] == [(0, "other")]  # an error, not a timeout


def test_an_error_the_api_will_not_repeat_is_not_retried(
    played: Any, good_text: str, tmp_path: Path
) -> None:
    _, record = played
    clients = clients_for(lambda r, n: ValueError("bad"), lambda r, n: good_text)
    run = run_judges(record, make(judge_config(), Cassette.record(tmp_path / "c.jsonl"), clients))
    assert run.results[0].status == "absent" and len(clients["judge_ep_0"].requests) == 1


def test_two_answers_that_cannot_be_used_make_the_judge_absent_and_replay_the_same(
    played: Any, good_text: str, tmp_path: Path
) -> None:
    _, record = played
    clients = clients_for(lambda r, n: GARBAGE, lambda r, n: good_text)
    path = tmp_path / "c.jsonl"
    run = run_judges(record, make(judge_config(), Cassette.record(path), clients))
    assert run.results[0].status == "absent" and run.results[0].reason == "unusable"
    assert len(clients["judge_ep_0"].requests) == 2  # asked again once, and no more
    again = run_judges(record, make(judge_config(), Cassette.replay(path), None))
    assert [r.status for r in again.results] == ["absent", "ok"]
    assert again.results[0].first_problems == run.results[0].first_problems
    assert [(a.judge, a.reason) for a in run.absences] == [(0, "unreadable")]
    assert again.absences == run.absences


def test_an_empty_reply_is_asked_again(played: Any, good_text: str, tmp_path: Path) -> None:
    _, record = played
    clients = clients_for(lambda r, n: "   " if n == 1 else good_text, lambda r, n: good_text)
    path = tmp_path / "c.jsonl"
    run = run_judges(record, make(judge_config(), Cassette.record(path), clients))
    assert run.both and run.results[0].attempts == 2
    outcomes = [e.outcome for e in Cassette.replay(path).entries if e.key.step == 0]
    assert outcomes == ["parse_failed", "ok"]


# ------------------------------------------------------------------------------- costs


def priced() -> PriceTable:
    price = ModelPrice(
        input_per_mtok=1.0,
        output_per_mtok=5.0,
        as_of=date(2026, 11, 1),
        source="https://example.com/prices",
    )
    return PriceTable({"judge-model-0-0001": price, "judge-model-1-0001": price}, source="test")


def test_judge_calls_are_charged_to_their_own_labels(
    played: Any, good_text: str, tmp_path: Path
) -> None:
    _, record = played
    # the reply of the fake counts 100 input and 20 output tokens: 200 micro-dollars a call
    clients = clients_for(lambda r, n: GARBAGE if n == 1 else good_text, lambda r, n: good_text)
    ledger = Ledger(priced())
    path = tmp_path / "c.jsonl"
    judges = make(judge_config(), Cassette.record(path), clients, ledger=ledger)
    run_judges(record, judges)
    assert ledger.by_label["judge:0"].calls == 2 and ledger.by_label["judge:1"].calls == 1
    assert ledger.by_label["judge:0"].cost_microusd == 400
    assert ledger.by_label["judge:1"].cost_microusd == 200 and ledger.spent_microusd == 600
    replay_ledger = Ledger(priced())
    run_judges(record, make(judge_config(), Cassette.replay(path), None, ledger=replay_ledger))
    assert replay_ledger.summary() == ledger.summary()


def test_a_blocked_cost_gate_makes_both_judges_absent(played: Any, tmp_path: Path) -> None:
    _, record = played
    ledger = Ledger(priced(), cap_microusd=0)
    assert ledger.gate() is not None and ledger.blocked
    clients = clients_for(lambda r, n: "never asked", lambda r, n: "never asked")
    judges = make(judge_config(), Cassette.record(None), clients, ledger=ledger)
    run = run_judges(record, judges)
    assert all(client.requests == [] for client in clients.values())
    assert [r.status for r in run.results] == ["absent", "absent"] and run.extracts == ()
    assert ledger.aborted_actions == 2
    assert [(a.judge, a.reason) for a in run.absences] == [
        (0, "budget_aborted"),
        (1, "budget_aborted"),
    ]


# ---------------------------------------------------------------------------- Anthropic


def test_anthropic_judges_send_a_system_block_and_no_tools(
    played: Any, good_text: str, tmp_path: Path
) -> None:
    _, record = played
    config = judge_config("anthropic")
    clients = clients_for(
        lambda r, n: GARBAGE if n == 1 else good_text, lambda r, n: good_text, kind="anthropic"
    )
    path = tmp_path / "c.jsonl"
    run = run_judges(record, make(config, Cassette.record(path), clients))
    assert run.both
    first, second = clients["judge_ep_0"].requests
    payload = first.payload
    assert "tools" not in payload and "tool_choice" not in payload
    assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert payload["max_tokens"] == 900 and payload["output_config"] == {"effort": "medium"}
    assert [m["role"] for m in payload["messages"]] == ["user"]
    # the second try keeps the cached prefix as it was
    for name in ("system", "model", "max_tokens", "output_config"):
        assert second.payload[name] == payload[name]
    assert [m["role"] for m in second.payload["messages"]] == ["user", "assistant", "user"]
    again = run_judges(record, make(config, Cassette.replay(path), None))
    assert again.extracts == run.extracts


# ------------------------------------------------------------------------ configuration


def test_a_mock_role_is_a_mock_judge_and_needs_nothing() -> None:
    from auction_night.contracts import load_config

    config = load_config(Path(__file__).resolve().parents[2] / "configs" / "night_mock.yaml")
    judges = make_judges(config)
    assert [type(j) for j in judges] == [MockJudge, MockJudge]
    assert [(j.index, j.vendor) for j in judges] == [(0, "mock"), (1, "mock")]


def test_a_model_judge_needs_the_cassette_of_the_night() -> None:
    with pytest.raises(ValueError, match="cassette"):
        make_judges(judge_config())


def test_a_model_judge_is_built_from_the_role_and_hides_itself_in_repr(tmp_path: Path) -> None:
    judges = make_judges(judge_config(), cassette=Cassette.record(tmp_path / "c.jsonl"))
    assert all(isinstance(j, ModelJudge) for j in judges)
    assert [repr(j) for j in judges] == ["ModelJudge(index=0)", "ModelJudge(index=1)"]
    assert [j.vendor for j in judges] == ["vendor-j0", "vendor-j1"]


def test_a_mock_role_is_not_a_model_judge(tmp_path: Path) -> None:
    from auction_night.contracts.config import EndpointConfig, RoleConfig

    with pytest.raises(ValueError, match="llm role"):
        ModelJudge(
            0,
            RoleConfig(kind="mock"),
            EndpointConfig(kind="openai_compatible", base_url="http://localhost:9/v1"),
            cassette=Cassette.record(tmp_path / "c.jsonl"),
            night="mock-0001",
        )


def test_the_timeout_and_retries_come_from_the_limits(played: Any, tmp_path: Path) -> None:
    _, record = played
    config = judge_config()
    limits = config.limits.model_copy(update={"call_timeout_s": 7, "transient_retries": 1})
    config = config.model_copy(update={"limits": limits})
    boom = openai.APIConnectionError(request=httpx2.Request("POST", "http://localhost:9/v1"))
    clients = clients_for(lambda r, n: boom, lambda r, n: boom)
    judges = make(config, Cassette.record(tmp_path / "c.jsonl"), clients)
    run_judges(record, judges)
    # one try and one retry for each judge, as limits.transient_retries says
    assert [len(c.requests) for c in clients.values()] == [2, 2]


def test_one_client_serves_every_endpoint_that_is_not_named(
    played: Any, good_text: str, tmp_path: Path
) -> None:
    _, record = played
    client = FakeJudgeClient(lambda r, n: good_text)
    judges = make_judges(
        judge_config(),
        cassette=Cassette.record(tmp_path / "c.jsonl"),
        client=client,
        clock=lambda: CLOCK,
    )
    assert run_judges(record, judges).both and len(client.requests) == 2
    assert completion("x")["model"]  # the helper the fake builds its replies with
