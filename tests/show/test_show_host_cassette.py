"""A model host goes through the client and the cassette: a replay needs no network."""

from datetime import date
from pathlib import Path
from typing import Any

import httpx2
import openai
import pytest
from night_support import make_config, play_seed
from show_support import (
    FakeHostClient,
    host_endpoint,
    host_role,
    is_rewrite,
    last_user_text,
    lot_of_request,
)

from auction_night.contracts import NightConfig
from auction_night.contracts.config import ModelPrice
from auction_night.contracts.events import dumps_events
from auction_night.engine.night import append_events
from auction_night.llm.accounting import Ledger, PriceTable
from auction_night.llm.cassette import (
    Cassette,
    CassetteDrift,
    CassetteMiss,
)
from auction_night.llm.request import BuiltRequest
from auction_night.prompts import TemplateSet
from auction_night.show.factcard import FactCards, build_fact_cards
from auction_night.show.host import (
    MockHost,
    ModelHost,
    load_host_templates,
    lot_job,
    make_host,
    mock_lot_text,
    mock_recap_text,
    run_host,
)

SEED = "show-host-cassette-1"


@pytest.fixture(scope="module")
def played() -> Any:
    transcript = play_seed(make_config(), SEED)
    return transcript.state, build_fact_cards(transcript.state)


def good_text(cards: FactCards, request: BuiltRequest) -> str:
    lot = lot_of_request(request)
    if lot is None:
        return mock_recap_text(cards.night.data)
    return mock_lot_text(cards.lot(lot).data)


def responder(cards: FactCards, *, wrong_first_for: tuple[int, ...] = (2,)) -> Any:
    def respond(request: BuiltRequest, call_number: int) -> str | BaseException:
        if lot_of_request(request) in wrong_first_for and not is_rewrite(request):
            return "这一件观众有 7001 人。"
        return good_text(cards, request)

    return respond


def make_model_host(
    cassette: Cassette, client: Any, kind: str = "openai_compatible", **extra: Any
) -> ModelHost:
    return ModelHost(
        host_role(kind),
        host_endpoint(kind),
        cassette=cassette,
        night="mock-0001",
        client=client,
        clock=lambda: "2026-11-01T00:00:00.000Z",
        sleep=extra.pop("sleep", lambda s: None),
        **extra,
    )


@pytest.fixture
def recorded(played: Any, tmp_path: Path) -> Any:
    _, cards = played
    path = tmp_path / "host_calls.jsonl"
    client = FakeHostClient(responder(cards))
    cassette = Cassette.record(path)
    out = run_host(cards, make_model_host(cassette, client))
    return path, out, client, cards


# ------------------------------------------------------------------------------ record


def test_a_model_host_writes_through_the_client_with_one_rewrite(recorded: Any) -> None:
    _, out, client, _ = recorded
    assert len(client.requests) == 14  # 12 lots + the recap + one rewrite
    commentary = out.commentary
    assert [r.attempts for r in commentary.lots] == [
        2 if r.lot == 2 else 1 for r in commentary.lots
    ]
    assert commentary.fallbacks == ()
    assert "7001" in commentary.lots[1].rejected[0].text and "7001" not in commentary.lots[1].text


def test_every_attempt_is_a_cassette_entry_with_the_six_part_key(recorded: Any) -> None:
    path, _, _, _ = recorded
    cassette = Cassette.replay(path)
    keys = [k.as_tuple() for k in cassette.keys()]  # noqa: SIM118 - a method, not a dict
    assert len(keys) == 14
    assert ("mock-0001", 2, "host_lot", 0, None, 1) in keys
    assert ("mock-0001", 2, "host_lot", 0, None, 2) in keys
    assert ("mock-0001", None, "host_recap", 0, None, 1) in keys
    for entry in cassette.entries:
        assert entry.role == "host" and entry.outcome == "ok"
        assert entry.model == "host-model" and entry.model_returned == "host-model-0001"
        assert entry.usage is not None and entry.request_hash.startswith("sha256:")


def test_host_requests_carry_no_tools(recorded: Any) -> None:
    _, _, client, _ = recorded
    for request in client.requests:
        assert "tools" not in request.payload and "tool_choice" not in request.payload
        assert request.payload["messages"][0]["role"] == "system"


def test_the_rewrite_repeats_the_request_and_appends_the_exchange(recorded: Any) -> None:
    _, out, client, _ = recorded
    first = next(r for r in client.requests if lot_of_request(r) == 2 and not is_rewrite(r))
    second = next(r for r in client.requests if lot_of_request(r) == 2 and is_rewrite(r))
    assert second.payload["messages"][: len(first.payload["messages"])] == first.payload["messages"]
    roles = [m["role"] for m in second.payload["messages"]]
    assert roles == ["system", "user", "assistant", "user"]
    assert "7001" in second.payload["messages"][2]["content"]
    assert "7001" in last_user_text(second) and "不在事实卡里" in last_user_text(second)
    assert first.request_hash != second.request_hash


def test_the_first_request_of_every_text_starts_from_the_same_system_prompt(recorded: Any) -> None:
    _, _, client, _ = recorded
    systems = {r.payload["messages"][0]["content"] for r in client.requests}
    assert len(systems) == 1


def test_the_card_is_in_the_request(recorded: Any) -> None:
    _, _, client, cards = recorded
    request = next(r for r in client.requests if lot_of_request(r) == 5)
    assert cards.lot(5).text in request.payload["messages"][1]["content"]


# ------------------------------------------------------------------------------ replay


def test_a_replay_needs_no_client_and_gives_the_same_bytes(played: Any, recorded: Any) -> None:
    state, cards = played
    path, out, _, _ = recorded
    replayed = run_host(cards, make_model_host(Cassette.replay(path), None))
    assert replayed.commentary == out.commentary
    assert replayed.block == out.block
    one = dumps_events(append_events(state, out.events)[0].events)
    two = dumps_events(append_events(state, replayed.events)[0].events)
    assert one == two


def test_a_replay_serves_every_recorded_attempt_and_no_other(played: Any, recorded: Any) -> None:
    _, cards = played
    path, _, _, _ = recorded
    cassette = Cassette.replay(path)
    run_host(cards, make_model_host(cassette, None))
    assert cassette.unused() == [] and cassette.drifted == ()


def test_a_changed_prompt_is_a_drift(played: Any, recorded: Any) -> None:
    _, cards = played
    path, _, _, _ = recorded
    base = load_host_templates()
    texts = dict(base.texts)
    texts["host.lot_user"] = texts["host.lot_user"] + "\n多说一句。"
    changed = TemplateSet(base.lang, base.version, texts)
    with pytest.raises(CassetteDrift):
        run_host(cards, make_model_host(Cassette.replay(path), None), templates=changed)


def test_a_missing_attempt_is_a_miss(played: Any, recorded: Any) -> None:
    _, cards = played
    path, _, _, _ = recorded
    lines = path.read_text(encoding="utf-8").splitlines()
    kept = [line for line in lines if '"attempt":2' not in line]
    assert len(kept) == len(lines) - 1
    cassette = Cassette.from_text("\n".join(kept) + "\n")
    with pytest.raises(CassetteMiss):
        run_host(cards, make_model_host(cassette, None))


def test_a_replay_does_not_sleep_or_charge_the_network(played: Any, recorded: Any) -> None:
    _, cards = played
    path, _, _, _ = recorded
    slept: list[float] = []
    host = make_model_host(Cassette.replay(path), None, sleep=slept.append)
    run_host(cards, host)
    assert slept == []


def test_recording_without_a_client_is_refused(played: Any, tmp_path: Path) -> None:
    _, cards = played
    host = make_model_host(Cassette.record(tmp_path / "c.jsonl"), None)
    with pytest.raises(RuntimeError, match="needs a client"):
        run_host(cards, host)


# ------------------------------------------------------------------------------- faults


def test_a_timeout_plays_the_card_and_is_recorded(played: Any, tmp_path: Path) -> None:
    _, cards = played

    def respond(request: BuiltRequest, n: int) -> str | BaseException:
        if lot_of_request(request) == 1:
            return TimeoutError("too slow")
        return good_text(cards, request)

    path = tmp_path / "c.jsonl"
    client = FakeHostClient(respond)
    out = run_host(cards, make_model_host(Cassette.record(path), client))
    first = out.commentary.lots[0]
    assert (first.fallback, first.attempts, first.text) == (True, 1, cards.lot(1).headline)
    assert len(client.requests) == 13  # no rewrite after a timeout
    entry = next(e for e in Cassette.replay(path).entries if e.key.lot == 1)
    assert entry.outcome == "timeout" and entry.error is not None and entry.response is None
    again = run_host(cards, make_model_host(Cassette.replay(path), None))
    assert again.commentary == out.commentary


def test_a_dropped_connection_is_retried_after_a_pause(played: Any, tmp_path: Path) -> None:
    _, cards = played
    boom = openai.APIConnectionError(request=httpx2.Request("POST", "http://localhost:9/v1"))

    def respond(request: BuiltRequest, n: int) -> str | BaseException:
        return boom if n == 1 else good_text(cards, request)

    path = tmp_path / "c.jsonl"
    slept: list[float] = []
    client = FakeHostClient(respond)
    host = make_model_host(Cassette.record(path), client, sleep=slept.append)
    out = run_host(cards, host)
    assert out.commentary.lots[0].fallback is False and out.commentary.lots[0].attempts == 1
    assert slept == [1.0] and len(client.requests) == 14
    entries = [e for e in Cassette.replay(path).entries if e.key.lot == 1]
    assert [(e.key.attempt, e.outcome) for e in entries] == [(1, "error"), (2, "ok")]
    replay_slept: list[float] = []
    again = run_host(cards, make_model_host(Cassette.replay(path), None, sleep=replay_slept.append))
    assert again.commentary == out.commentary and replay_slept == []


def test_retries_stop_after_the_limit(played: Any, tmp_path: Path) -> None:
    _, cards = played
    boom = openai.APIConnectionError(request=httpx2.Request("POST", "http://localhost:9/v1"))
    client = FakeHostClient(lambda r, n: boom if lot_of_request(r) == 1 else good_text(cards, r))
    host = make_model_host(Cassette.record(tmp_path / "c.jsonl"), client, transient_retries=1)
    out = run_host(cards, host)
    assert out.commentary.lots[0].fallback and out.commentary.lots[0].attempts == 1
    assert sum(1 for r in client.requests if lot_of_request(r) == 1) == 2


def test_an_error_the_api_will_not_repeat_is_not_retried(played: Any, tmp_path: Path) -> None:
    _, cards = played
    client = FakeHostClient(
        lambda r, n: ValueError("bad") if lot_of_request(r) == 1 else good_text(cards, r)
    )
    out = run_host(cards, make_model_host(Cassette.record(tmp_path / "c.jsonl"), client))
    assert out.commentary.lots[0].fallback
    assert sum(1 for r in client.requests if lot_of_request(r) == 1) == 1


def test_an_empty_reply_is_rewritten(played: Any, tmp_path: Path) -> None:
    _, cards = played

    def respond(request: BuiltRequest, n: int) -> str | BaseException:
        if lot_of_request(request) == 1 and not is_rewrite(request):
            return "   "
        return good_text(cards, request)

    path = tmp_path / "c.jsonl"
    out = run_host(cards, make_model_host(Cassette.record(path), FakeHostClient(respond)))
    assert out.commentary.lots[0].attempts == 2 and not out.commentary.lots[0].fallback
    outcomes = [e.outcome for e in Cassette.replay(path).entries if e.key.lot == 1]
    assert outcomes == ["parse_failed", "ok"]


def test_a_wrong_answer_twice_plays_the_card_and_replays_the_same(
    played: Any, tmp_path: Path
) -> None:
    _, cards = played
    client = FakeHostClient(
        lambda r, n: "观众有 7001 人" if lot_of_request(r) == 4 else good_text(cards, r)
    )
    path = tmp_path / "c.jsonl"
    out = run_host(cards, make_model_host(Cassette.record(path), client))
    assert out.commentary.lots[3].fallback and out.commentary.lots[3].attempts == 2
    again = run_host(cards, make_model_host(Cassette.replay(path), None))
    assert again.commentary == out.commentary and again.events == out.events


# ---------------------------------------------------------------------------- Anthropic


def test_an_anthropic_host_sends_a_system_block_and_no_tools(played: Any, tmp_path: Path) -> None:
    _, cards = played
    client = FakeHostClient(responder(cards, wrong_first_for=()), kind="anthropic")
    path = tmp_path / "c.jsonl"
    out = run_host(cards, make_model_host(Cassette.record(path), client, kind="anthropic"))
    assert not out.commentary.fallbacks
    payload = client.requests[0].payload
    assert "tools" not in payload and "tool_choice" not in payload
    assert payload["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert payload["max_tokens"] == 600 and payload["output_config"] == {"effort": "medium"}
    assert [m["role"] for m in payload["messages"]] == ["user"]
    again = run_host(cards, make_model_host(Cassette.replay(path), None, kind="anthropic"))
    assert again.commentary == out.commentary


# ------------------------------------------------------------------------------- costs


def priced() -> PriceTable:
    price = ModelPrice(
        input_per_mtok=1.0,
        output_per_mtok=5.0,
        as_of=date(2026, 11, 1),
        source="https://example.com/prices",
    )
    return PriceTable({"host-model-0001": price}, source="test")


def test_host_calls_are_charged_to_the_host_label(
    played: Any, recorded: Any, tmp_path: Path
) -> None:
    _, cards = played
    ledger = Ledger(priced())
    client = FakeHostClient(responder(cards))
    path = tmp_path / "c.jsonl"
    run_host(cards, make_model_host(Cassette.record(path), client, ledger=ledger))
    assert ledger.by_label["host"].calls == 14
    # 100 input and 20 output tokens at 1 and 5 dollars per million tokens: 200 micro-dollars.
    assert ledger.by_label["host"].cost_microusd == 14 * 200 == ledger.spent_microusd
    replay_ledger = Ledger(priced())
    run_host(cards, make_model_host(Cassette.replay(path), None, ledger=replay_ledger))
    assert replay_ledger.summary() == ledger.summary()


def test_a_blocked_cost_gate_plays_the_cards(played: Any) -> None:
    _, cards = played
    ledger = Ledger(priced(), cap_microusd=0)
    assert ledger.gate() is not None and ledger.blocked
    client = FakeHostClient(lambda r, n: "never asked")
    out = run_host(cards, make_model_host(Cassette.record(None), client, ledger=ledger))
    assert client.requests == []
    assert all(r.fallback for r in (*out.commentary.lots, out.commentary.recap))
    assert ledger.aborted_actions == 13


# ---------------------------------------------------------------------------- configuration


def config_with(host: dict[str, Any]) -> NightConfig:
    from engine_support import raw_config

    raw = raw_config()
    raw["endpoints"] = {
        "host_ep": {"kind": "openai_compatible", "base_url": "http://localhost:9/v1"}
    }
    raw["show"]["host_offline"] = host
    return NightConfig.model_validate(raw)


def test_a_mock_host_needs_nothing_else(played: Any) -> None:
    assert isinstance(make_host(make_config()), MockHost)


def test_a_model_host_needs_the_cassette_of_the_night(played: Any, tmp_path: Path) -> None:
    config = config_with(
        {"kind": "llm", "endpoint": "host_ep", "model": "host-model", "vendor": "vendor-h"}
    )
    with pytest.raises(ValueError, match="cassette"):
        make_host(config)
    host = make_host(config, cassette=Cassette.record(tmp_path / "c.jsonl"), client=None)
    assert isinstance(host, ModelHost) and repr(host) == "ModelHost()"


def test_the_cassette_holds_no_credentials(recorded: Any) -> None:
    path, _, _, _ = recorded
    text = path.read_text(encoding="utf-8")
    assert "extra_headers" not in text and "api_key" not in text.lower()


def test_a_mock_role_is_not_a_model_host(tmp_path: Path) -> None:
    from auction_night.contracts.config import RoleConfig

    with pytest.raises(ValueError, match="llm role"):
        ModelHost(
            RoleConfig(kind="mock"),
            host_endpoint(),
            cassette=Cassette.record(tmp_path / "c.jsonl"),
            night="mock-0001",
        )


def test_make_host_takes_timeout_and_retries_from_the_limits(tmp_path: Path) -> None:
    config = config_with(
        {"kind": "llm", "endpoint": "host_ep", "model": "host-model", "vendor": "vendor-h"}
    )
    limits = config.limits.model_copy(update={"call_timeout_s": 7, "transient_retries": 1})
    config = config.model_copy(update={"limits": limits})
    boom = openai.APIConnectionError(request=httpx2.Request("POST", "http://localhost:9/v1"))
    client = FakeHostClient(lambda request, n: boom)
    host = make_host(config, cassette=Cassette.record(tmp_path / "c.jsonl"), client=client)
    assert (
        host.write(lot_job(build_fact_cards(play_seed(make_config(), SEED).state).lot(1))) is None
    )
    assert len(client.requests) == 2  # one try and one retry, as limits.transient_retries says


def test_an_anthropic_rewrite_keeps_the_cached_prefix(played: Any, tmp_path: Path) -> None:
    _, cards = played
    client = FakeHostClient(responder(cards), kind="anthropic")
    run_host(
        cards, make_model_host(Cassette.record(tmp_path / "c.jsonl"), client, kind="anthropic")
    )
    first = next(r for r in client.requests if lot_of_request(r) == 2 and not is_rewrite(r))
    second = next(r for r in client.requests if lot_of_request(r) == 2 and is_rewrite(r))
    for name in ("system", "model", "max_tokens", "output_config"):
        assert second.payload[name] == first.payload[name]
    assert second.payload["messages"][:1] == first.payload["messages"]
    assert [m["role"] for m in second.payload["messages"]] == ["user", "assistant", "user"]
