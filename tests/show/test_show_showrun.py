"""Recording the show layer: ``record_show`` and ``auction-night show``, with fake models.

Nothing here touches a network. The night is the mock night of ``configs/night_mock.yaml``, run
into a directory by ``run_directory``. The configuration is changed so that its host, its two
judges and its content classifier are models on fake endpoints, and the fake models answer the way
the mock roles do, so what a recording holds can be compared with what the mock roles give.

The tests check what the command promises: the three recordings are written and ``export`` reads
them with no other option, they give the same replay as the same calls made in memory, roles that
are mocks write nothing, a recording that is in the way is refused, a role that gets no answer or a
cost cap that is reached stops the recording before any file is written, and the cost of the show
layer is held to the cap of the night on its own, with the cost of the seats reported next to it.
"""

import copy
import json
import re
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest
import yaml
from export_support import MOCK_CONFIG, NO_GIT, REPO, raw_config
from show_support import completion, lot_of_request, message
from typer.testing import CliRunner

from auction_night.cli import app
from auction_night.contracts import NightConfig, Replay
from auction_night.contracts.config import ModelPrice
from auction_night.llm.accounting import PriceTable
from auction_night.llm.cassette import Cassette, CassetteDrift
from auction_night.llm.client import ClientReply, Scrubber, normalise_openai
from auction_night.llm.nightrun import CALLS_FILE, run_directory
from auction_night.llm.request import BuiltRequest
from auction_night.show.content_scan import ContentGate
from auction_night.show.factcard import FactCards, build_fact_cards
from auction_night.show.host import make_host, mock_lot_text, mock_recap_text, run_host
from auction_night.show.judge_input import build_judge_input
from auction_night.show.judges import MockJudge, judge_job, make_judges
from auction_night.show.replay_export import (
    HOST_CALLS_FILE,
    JUDGE_CALLS_FILE,
    SCAN_CALLS_FILE,
    ExportError,
    export_directory,
    export_replay,
    replay_run,
    show_parts,
)
from auction_night.show.showrun import (
    CostCapReached,
    RoleCallFailed,
    ShowFailed,
    ShowRecorded,
    ShowSetupError,
    describe_show,
    record_show,
)
from auction_night.theory import analyse_night

runner = CliRunner()
STAMP = "2026-11-01T00:00:00.000Z"
RECORDINGS = (HOST_CALLS_FILE, JUDGE_CALLS_FILE, SCAN_CALLS_FILE)
SHOW_MODELS = ("host-model", "judge-model-0", "judge-model-1", "scan-model")
MOCK_MODELS = ("mock-honest", "mock-collude", "mock-backstab", "mock-random", "mock-abstain")
SHOW_PRICE = 1_000.0  # micro-dollars a token, for the fake show models
PER_CALL = 120_000  # 100 + 20 tokens at that price: the cost of every fake call
CALLS = 13 + 1 + 1 + 13  # twelve lots and the recap, one answer per judge, thirteen batches


# ============================================================================ the fake models


class Stub:
    """A model behind the client interface: ``respond(request, call_number)`` gives its answer.

    The answer is a text, a pair of a text and a finish reason (``content_filter`` is a refusal),
    or an exception, which is raised as the SDK would raise it. Every reply reports 100 prompt
    tokens and 20 output tokens. Every request is kept in ``requests``. The client has a scrubber
    that knows ``secret``, as a real client knows its key.
    """

    def __init__(self, respond: Callable[[BuiltRequest, int], Any], secret: str | None = None):
        self.respond = respond
        self.requests: list[BuiltRequest] = []
        self.scrubber = Scrubber([secret] if secret else [])

    def complete(self, request: BuiltRequest, *, timeout_s: float) -> ClientReply:
        self.requests.append(request)
        answer = self.respond(request, len(self.requests))
        if isinstance(answer, BaseException):
            raise answer
        text, finish = (answer, "stop") if isinstance(answer, str) else answer
        body = completion(text, model=f"{request.model}-0001")
        body["choices"][0]["finish_reason"] = finish
        return normalise_openai(body, latency_ms=3, scrubber=self.scrubber)


def refused() -> tuple[str, str]:
    """An answer that the API itself declined to give."""
    return "", "content_filter"


def down() -> BaseException:
    """What an endpoint that does not answer raises."""
    return TimeoutError("too slow")


def connection_lost() -> BaseException:
    """An error that another try may cure."""
    return openai.APIConnectionError(request=httpx2.Request("POST", "http://localhost:9/v1"))


ENDPOINTS = ("host_ep", "judge_ep_0", "judge_ep_1", "scan_ep")
HOST = {"kind": "llm", "endpoint": "host_ep", "model": "host-model", "vendor": "vendor-h"}
SCAN = {"kind": "llm", "endpoint": "scan_ep", "model": "scan-model", "vendor": "vendor-s"}
JUDGES = [
    {
        "kind": "llm",
        "endpoint": f"judge_ep_{i}",
        "model": f"judge-model-{i}",
        "vendor": f"vendor-j{i}",
    }
    for i in (0, 1)
]


def show_raw(
    *,
    host: bool = True,
    judges: bool = True,
    scan: bool = True,
    cap_microusd: int | None = None,
) -> dict[str, Any]:
    """The mock night, with the roles of the show layer that are asked for turned into models."""
    raw = raw_config()
    raw["endpoints"] = {
        name: {"kind": "openai_compatible", "base_url": f"http://localhost:{9 + number}/v1"}
        for number, name in enumerate(ENDPOINTS)
    }
    if host:
        raw["show"]["host_offline"] = dict(HOST)
    if judges:
        raw["show"]["judges"] = [dict(judge) for judge in JUDGES]
    if scan:
        raw["show"]["content_scan"] = {"wordlist": None, "classifier": dict(SCAN)}
    raw["limits"]["transient_retries"] = 2
    if cap_microusd is not None:
        raw["limits"]["max_cost_per_night_usd"] = cap_microusd / 1e6
    return raw


def price(per_mtok: float) -> ModelPrice:
    return ModelPrice(
        input_per_mtok=per_mtok,
        output_per_mtok=per_mtok,
        as_of=date(2026, 11, 1),
        source="https://example.com/prices",
    )


def priced(show: float = SHOW_PRICE, seats: float = 1.0) -> PriceTable:
    """A price for every model of the night: the fake show models and the mock seats."""
    prices = {model: price(show) for model in SHOW_MODELS}
    prices.update({model: price(seats) for model in MOCK_MODELS})
    return PriceTable(prices, source="test prices")


def write_prices(path: Path, table: PriceTable) -> Path:
    """A price list as the command reads it."""
    entries = {
        model: {
            "input_per_mtok": p.input_per_mtok,
            "output_per_mtok": p.output_per_mtok,
            "as_of": "2026-11-01",
            "source": p.source,
        }
        for model, p in table.prices.items()
    }
    path.write_text(yaml.safe_dump({"version": 1, "prices": entries}), encoding="utf-8")
    return path


@dataclass
class World:
    """A run directory with the configuration it was run with, and what the fake models know:
    the fact cards for the host, and the answer that the mock judge gives for the judges."""

    config: NightConfig
    raw: dict[str, Any]
    out: Path
    cards: FactCards
    judge_text: str

    def host_answer(self, request: BuiltRequest, n: int = 0) -> str:
        lot = lot_of_request(request)
        if lot is None:
            return mock_recap_text(self.cards.night.data)
        return mock_lot_text(self.cards.lot(lot).data)

    def clients(self, **replace: Stub) -> dict[str, Stub]:
        """One fake model per endpoint. A keyword (``host_ep=...``) puts another in its place."""
        stubs = {
            "host_ep": Stub(self.host_answer),
            "judge_ep_0": Stub(lambda request, n: self.judge_text),
            "judge_ep_1": Stub(lambda request, n: self.judge_text),
            "scan_ep": Stub(lambda request, n: '{"flagged": []}'),
        }
        stubs.update(replace)
        return stubs

    def sdk(self, made: list[dict[str, Any]]) -> Any:
        """The constructor of an SDK whose chat endpoint answers like the fake models. The
        options of every construction are noted in ``made``."""
        world = self

        class Sdk:
            def __init__(self, **options: Any) -> None:
                made.append(options)
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

            @staticmethod
            def create(**kwargs: Any) -> Any:
                model = kwargs["model"]
                request = SimpleNamespace(payload=kwargs)
                if model == "host-model":
                    text = world.host_answer(request)  # type: ignore[arg-type]
                elif model == "scan-model":
                    text = '{"flagged": []}'
                else:
                    text = world.judge_text
                return completion(text, model=f"{model}-0001")

        return Sdk

    def copy_to(self, root: Path) -> "World":
        """The same night in another directory (the files are copied; a test may change them)."""
        target = root / "night"
        shutil.copytree(self.out, target)
        return World(self.config, self.raw, target, self.cards, self.judge_text)

    def seats_microusd(self) -> int:
        """What the seats cost, as the recording of the night has it."""
        cassette = Cassette.replay(self.out / CALLS_FILE)
        return sum(entry.cost_microusd or 0 for entry in cassette.entries)

    def record(self, clients: Mapping[str, Stub] | None = None, **kwargs: Any) -> ShowRecorded:
        kwargs.setdefault("clock", lambda: STAMP)
        kwargs.setdefault("sleep", lambda seconds: None)
        return record_show(self.config, self.out, clients=clients or self.clients(), **kwargs)

    def files(self) -> list[str]:
        return sorted(path.name for path in self.out.iterdir())

    def recordings(self) -> list[str]:
        return [name for name in self.files() if name in RECORDINGS]


def make_world(
    root: Path, raw: dict[str, Any], prices: PriceTable | None = None, **run_options: Any
) -> World:
    config = NightConfig.model_validate(raw)
    root.mkdir(parents=True, exist_ok=True)
    out = root / "night"
    saved = root / "night.yaml"
    saved.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    run_directory(config, out, config_file=saved, prices=prices, **run_options)
    run = replay_run(config, out)
    record = build_judge_input(run.state.events)
    judge_text = MockJudge().write(judge_job(0, record)).text
    return World(config, raw, out, build_fact_cards(run.state), judge_text)


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> World:
    """The night with every role of the show layer a model, run once for the whole module."""
    return make_world(tmp_path_factory.mktemp("show"), show_raw())


@pytest.fixture
def night(world: World, tmp_path: Path) -> World:
    """A private copy of the night that a test may change."""
    return world.copy_to(tmp_path)


@pytest.fixture(scope="module")
def recorded(world: World, tmp_path_factory: pytest.TempPathFactory) -> tuple[World, ShowRecorded]:
    """The night with the show layer recorded once: the world, and what ``record_show`` returned."""
    mine = world.copy_to(tmp_path_factory.mktemp("recorded"))
    return mine, mine.record()


def keep_out(path: Path) -> None:
    """The directory holds none of the recordings and no scratch file."""
    assert not [p for p in path.iterdir() if p.name in RECORDINGS or p.name.endswith(".part")]


# ============================================================================== the recordings


def test_the_three_recordings_are_written_and_export_reads_them_with_no_option(
    recorded: tuple[World, ShowRecorded],
) -> None:
    mine, done = recorded
    assert mine.recordings() == sorted(RECORDINGS)
    assert [path.name for path in done.files] == list(RECORDINGS)
    assert not list(mine.out.glob("*.part"))
    exported = export_directory(mine.config, mine.out, git=NO_GIT)
    assert exported.report.ok and exported.report == done.scan
    replay = exported.replay
    assert [(e.judge, e.vendor) for e in replay.judges] == [(0, "vendor-j0"), (1, "vendor-j1")]
    assert replay.judges_absent == [] and not any(line.fallback for line in replay.host.per_lot)
    calls = {role: usage.calls for role, usage in replay.usage.by_role.items()}
    assert calls["host"] == 13 and calls["judge:0"] == calls["judge:1"] == 1
    assert calls["classifier"] == 13


def test_each_recording_holds_the_calls_of_its_roles(
    recorded: tuple[World, ShowRecorded],
) -> None:
    mine, done = recorded
    roles = {}
    for name in RECORDINGS:
        entries = Cassette.replay(mine.out / name).entries
        roles[name] = sorted({entry.role for entry in entries})
        assert {entry.outcome for entry in entries} == {"ok"}
        assert {entry.key.night for entry in entries} == {"mock-0001"}
    assert roles == {
        HOST_CALLS_FILE: ["host"],
        JUDGE_CALLS_FILE: ["judge:0", "judge:1"],
        SCAN_CALLS_FILE: ["classifier"],
    }
    assert [(r.role, r.model, r.file, r.tally.calls) for r in done.roles] == [
        ("host", "host-model", HOST_CALLS_FILE, 13),
        ("judge:0", "judge-model-0", JUDGE_CALLS_FILE, 1),
        ("judge:1", "judge-model-1", JUDGE_CALLS_FILE, 1),
        ("classifier", "scan-model", SCAN_CALLS_FILE, 13),
    ]


def test_the_export_of_the_recordings_is_the_replay_of_the_same_calls_made_in_memory(
    recorded: tuple[World, ShowRecorded],
) -> None:
    """The files lose nothing: the export from them equals the export of calls that fresh copies
    of the same fake models make in memory, and each file is the text of its cassette."""
    mine, _ = recorded
    config = mine.config
    run = replay_run(config, mine.out)
    theory = analyse_night(run.state)
    cards = build_fact_cards(run.state, theory=theory)
    clients = mine.clients()
    host_cassette, judge_cassette, scan_cassette = (
        Cassette.record(),
        Cassette.record(),
        Cassette.record(),
    )
    host_backend = make_host(
        config, cassette=host_cassette, client=clients["host_ep"], clock=lambda: STAMP
    )
    host = run_host(cards, host_backend)
    judges = make_judges(config, cassette=judge_cassette, clients=clients, clock=lambda: STAMP)
    parts = show_parts(config, run.state, cards, backends=judges)
    gate = ContentGate.from_config(
        config, cassette=scan_cassette, client=clients["scan_ep"], clock=lambda: STAMP
    )
    cassettes = {
        HOST_CALLS_FILE: host_cassette,
        JUDGE_CALLS_FILE: judge_cassette,
        SCAN_CALLS_FILE: scan_cassette,
    }
    direct = export_replay(
        config,
        run.state,
        host.block,
        gate=gate,
        theory=theory,
        cards=cards,
        parts=parts,
        cassettes=[run.cassette, *cassettes.values()],
        git=NO_GIT,
        created_utc=STAMP,
    )
    from_files = export_directory(config, mine.out, git=NO_GIT, created_utc=STAMP)
    assert from_files.text == direct.text
    assert Replay.model_validate_json(from_files.text) == direct.replay
    for name, cassette in cassettes.items():
        assert (mine.out / name).read_text(encoding="utf-8") == cassette.text()


def test_no_header_and_no_key_reaches_a_recording(night: World) -> None:
    secret = "alpha-secret-key"  # no digits, so the number check of the host has nothing to say

    def leaky(request: BuiltRequest, n: int) -> str:
        return night.host_answer(request) + secret

    night.record(night.clients(host_ep=Stub(leaky, secret=secret)))
    text = (night.out / HOST_CALLS_FILE).read_text(encoding="utf-8")
    assert secret not in text and "[REDACTED]" in text
    for name in RECORDINGS:
        for line in (night.out / name).read_text(encoding="utf-8").splitlines():
            request = json.loads(line)["request"]
            assert set(request) <= {"model", "messages"}  # no extra_headers, no extra_query


def test_the_seats_are_not_called_and_their_sdk_is_not_even_built(tmp_path: Path) -> None:
    """A night with a model seat is replayed from its cassette. Only the endpoints of the show
    roles get a client; the endpoint of the seat is never touched."""
    raw = show_raw()
    raw["endpoints"]["seat_ep"] = {
        "kind": "openai_compatible",
        "base_url": "http://seat.invalid/v1",
    }
    raw["seats"][0] = {
        "seat": 1, "codename": "鲸落", "kind": "llm", "endpoint": "seat_ep", "model": "seat-model",
        "vendor": "vendor-seat",
    }  # fmt: skip
    ran: list[dict[str, Any]] = []

    def seat_endpoint_down(**options: Any) -> Any:
        ran.append(options)

        def create(**kwargs: Any) -> Any:
            raise RuntimeError("the endpoint is down")

        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    mine = make_world(tmp_path, raw, factories={"openai_compatible": seat_endpoint_down})
    assert [o["base_url"] for o in ran] == ["http://seat.invalid/v1"]  # the night did call it
    made: list[dict[str, Any]] = []
    done = record_show(
        mine.config,
        mine.out,
        environ={},
        factories={"openai_compatible": mine.sdk(made)},
        clock=lambda: STAMP,
    )
    assert len(done.files) == 3
    assert len(made) == 4  # one for each endpoint of a show role
    assert {o["base_url"] for o in made} == {f"http://localhost:{9 + n}/v1" for n in range(4)}
    assert all(o["max_retries"] == 0 for o in made)
    assert export_directory(mine.config, mine.out, git=NO_GIT).report.ok


# ================================================================================ mock roles


@pytest.mark.parametrize(
    ("llm_roles", "files", "mocks"),
    [
        ({"judges": False, "scan": False}, [HOST_CALLS_FILE], ("judge:0", "judge:1")),
        ({"host": False, "scan": False}, [JUDGE_CALLS_FILE], ("host",)),
        ({"host": False, "judges": False}, [SCAN_CALLS_FILE], ("host", "judge:0", "judge:1")),
        ({"scan": False}, [HOST_CALLS_FILE, JUDGE_CALLS_FILE], ()),
    ],
    ids=["host", "judges", "classifier", "host and judges"],
)
def test_a_role_that_is_a_mock_writes_no_file(
    tmp_path: Path, llm_roles: dict[str, bool], files: list[str], mocks: tuple[str, ...]
) -> None:
    mine = make_world(tmp_path, show_raw(**llm_roles))
    done = mine.record()
    assert mine.recordings() == sorted(files)
    assert [path.name for path in done.files] == sorted(files, key=RECORDINGS.index)
    assert done.mocks == mocks
    assert export_directory(mine.config, mine.out, git=NO_GIT).report.ok  # no option, no file


def test_a_mock_role_runs_on_the_spot_and_is_never_given_a_client(tmp_path: Path) -> None:
    mine = make_world(tmp_path, show_raw(judges=False, scan=False))
    done = mine.record({"host_ep": Stub(mine.host_answer)})  # nothing else is offered
    assert [r.role for r in done.roles] == ["host"]
    replay = export_directory(mine.config, mine.out, git=NO_GIT).replay
    mock_night = NightConfig.model_validate(raw_config())  # the judges are the mock judges
    mock_replay = export_directory(mock_night, mine.out, git=NO_GIT).replay
    assert replay.judges == mock_replay.judges and replay.awards == mock_replay.awards


def test_one_judge_that_is_a_mock_and_one_that_is_a_model_share_the_judge_file(
    tmp_path: Path,
) -> None:
    raw = show_raw(host=False, scan=False)
    raw["show"]["judges"] = [{"kind": "mock"}, dict(JUDGES[1])]
    mine = make_world(tmp_path, raw)
    done = mine.record({"judge_ep_1": Stub(lambda request, n: mine.judge_text)})
    assert [r.role for r in done.roles] == ["judge:1"] and done.mocks == ("host", "judge:0")
    entries = Cassette.replay(mine.out / JUDGE_CALLS_FILE).entries
    assert [entry.role for entry in entries] == ["judge:1"]
    replay = export_directory(mine.config, mine.out, git=NO_GIT).replay
    assert [(e.judge, e.vendor) for e in replay.judges] == [(0, "mock"), (1, "vendor-j1")]


def test_when_no_role_is_a_model_nothing_is_written_and_no_client_is_needed(tmp_path: Path) -> None:
    out = tmp_path / "night"
    config = NightConfig.model_validate(raw_config())
    run_directory(config, out, config_file=MOCK_CONFIG)
    before = sorted(path.name for path in out.iterdir())
    done = record_show(config, out)
    assert done.roles == () and done.files == () and done.mocks == ("host", "judge:0", "judge:1")
    assert sorted(path.name for path in out.iterdir()) == before
    assert describe_show(done) == [
        "night mock-0001: every role of the show layer is a mock, so there is nothing to record"
    ]


def test_the_stale_recording_of_a_role_that_is_now_a_mock_is_neither_refused_nor_touched(
    night: World,
) -> None:
    only_host = NightConfig.model_validate(show_raw(judges=False, scan=False))
    (night.out / JUDGE_CALLS_FILE).write_text("stale\n", encoding="utf-8")
    record_show(only_host, night.out, clients={"host_ep": Stub(night.host_answer)})
    assert (night.out / JUDGE_CALLS_FILE).read_text(encoding="utf-8") == "stale\n"
    assert (night.out / HOST_CALLS_FILE).is_file()


# ============================================================================ files in the way


def test_a_recording_that_exists_is_refused_unless_overwrite_is_given(night: World) -> None:
    night.record()
    before = {name: (night.out / name).read_bytes() for name in RECORDINGS}
    clients = night.clients()
    with pytest.raises(ShowSetupError, match=r"host_calls\.jsonl already exists.*--overwrite"):
        night.record(clients)
    assert all(not client.requests for client in clients.values()), "refused before any call"
    assert {name: (night.out / name).read_bytes() for name in RECORDINGS} == before


def test_overwrite_replaces_the_recordings_of_an_earlier_show(night: World) -> None:
    for name in RECORDINGS:
        (night.out / name).write_text("stale\n", encoding="utf-8")
    done = night.record(overwrite=True)
    assert len(done.files) == 3 and not list(night.out.glob("*.part"))
    for name in RECORDINGS:
        assert "stale" not in (night.out / name).read_text(encoding="utf-8")
    assert export_directory(night.config, night.out, git=NO_GIT).report.ok


def test_an_empty_file_is_not_in_the_way(night: World) -> None:
    (night.out / HOST_CALLS_FILE).write_text("", encoding="utf-8")
    assert len(night.record().files) == 3


def test_a_directory_that_cannot_be_written_is_found_before_the_first_call(night: World) -> None:
    (night.out / (HOST_CALLS_FILE + ".part")).mkdir()  # a directory where the scratch file goes
    clients = night.clients()
    with pytest.raises(ShowSetupError, match="cannot write"):
        night.record(clients)
    assert all(not client.requests for client in clients.values())


def test_a_directory_that_is_not_there_is_a_setup_error(night: World, tmp_path: Path) -> None:
    with pytest.raises(ShowSetupError, match="not a directory"):
        record_show(night.config, tmp_path / "nowhere", clients=night.clients())


# ================================================================================== failures


def test_a_host_that_gets_no_answer_stops_the_recording_at_once(night: World) -> None:
    host = Stub(lambda request, n: down() if n == 3 else night.host_answer(request))
    clients = night.clients(host_ep=host)
    with pytest.raises(RoleCallFailed) as caught:
        night.record(clients)
    message = str(caught.value)
    assert "the host got no answer for lot 3 after 1 attempt" in message
    assert "timeout, TimeoutError: too slow" in message and "No recording was written" in message
    assert "A longer limits.call_timeout_s in a copy of the configuration" in message
    assert len(host.requests) == 3, "no call is made after the one that failed"
    assert not clients["judge_ep_0"].requests and not clients["scan_ep"].requests
    keep_out(night.out)


@pytest.mark.parametrize(
    ("endpoint", "who"),
    [
        ("judge_ep_0", "judge 0 got no answer for the night"),
        ("judge_ep_1", "judge 1 got no answer for the night"),
        ("scan_ep", "the content classifier got no answer for batch 0"),
    ],
)
def test_a_judge_or_the_classifier_that_gets_no_answer_stops_the_recording(
    night: World, endpoint: str, who: str
) -> None:
    clients = night.clients(**{endpoint: Stub(lambda request, n: down())})
    with pytest.raises(RoleCallFailed, match=who):
        night.record(clients)
    keep_out(night.out)


def test_a_longer_timeout_in_a_copy_of_the_configuration_replays_the_night(night: World) -> None:
    """What the message of a timeout offers does work: the timeout is not part of the replay of
    the seats, so a copy of the configuration with another one replays the night."""
    raw = copy.deepcopy(night.raw)
    raw["limits"]["call_timeout_s"] = 600
    clients = night.clients()
    done = record_show(NightConfig.model_validate(raw), night.out, clients=clients)
    assert len(done.files) == 3 and all(client.requests for client in clients.values())


def test_the_roles_that_come_after_a_failed_role_are_not_called(night: World) -> None:
    clients = night.clients(judge_ep_0=Stub(lambda request, n: down()))
    with pytest.raises(RoleCallFailed, match="judge 0"):
        night.record(clients)
    assert not clients["judge_ep_1"].requests and not clients["scan_ep"].requests


def test_a_refusal_by_the_api_is_a_failure_too(night: World) -> None:
    clients = night.clients(judge_ep_1=Stub(lambda request, n: refused()))
    with pytest.raises(RoleCallFailed, match="judge 1 got no answer.*the API refused to answer"):
        night.record(clients)
    keep_out(night.out)


def test_an_error_that_another_try_cures_is_no_failure(night: World) -> None:
    def lost_once(request: BuiltRequest, n: int) -> Any:
        return connection_lost() if n == 1 else night.host_answer(request)

    done = night.record(night.clients(host_ep=Stub(lost_once)))
    entries = Cassette.replay(night.out / HOST_CALLS_FILE).entries
    assert [(e.key.lot, e.key.attempt, e.outcome) for e in entries[:2]] == [
        (1, 1, "error"),
        (1, 2, "ok"),
    ]
    assert done.roles[0].tally.calls == 14 and len(entries) == 14
    assert export_directory(night.config, night.out, git=NO_GIT).report.ok


def test_an_error_that_no_try_cures_stops_the_recording(night: World) -> None:
    host = Stub(lambda request, n: connection_lost() if n >= 2 else night.host_answer(request))
    with pytest.raises(RoleCallFailed, match=r"the host got no answer for lot 2 after 3 attempts"):
        night.record(night.clients(host_ep=host))
    assert len(host.requests) == 4  # lot 1, then lot 2 and its two retries
    keep_out(night.out)


def test_an_answer_that_cannot_be_used_is_not_a_failed_call(night: World) -> None:
    """A wrong host text is rewritten and then played from the card, and a judge whose answers
    cannot be read is absent. The recordings keep what happened and export reproduces it."""

    def wrong(request: BuiltRequest, n: int) -> str:
        return "观众有 7001 人。" if lot_of_request(request) == 2 else night.host_answer(request)

    clients = night.clients(
        host_ep=Stub(wrong), judge_ep_1=Stub(lambda request, n: "words only, no object")
    )
    done = night.record(clients)
    assert done.host_fallbacks == 1 and done.host_texts == 13
    assert [(a.judge, a.reason) for a in done.absent] == [(1, "unreadable")]
    assert done.roles[0].tally.calls == 14 and done.roles[2].tally.calls == 2
    replay = export_directory(night.config, night.out, git=NO_GIT).replay
    assert [line.fallback for line in replay.host.per_lot].count(True) == 1
    assert [(a.judge, a.reason) for a in replay.judges_absent] == [(1, "unreadable")]
    lines = describe_show(done)
    assert "host: 1 of 13 texts are played from the fact card" in lines
    assert "judges: absent: judge 1 (unreadable)" in lines


def test_a_failed_recording_leaves_the_files_of_an_earlier_one_alone(night: World) -> None:
    for name in RECORDINGS:
        (night.out / name).write_text("earlier\n", encoding="utf-8")
    clients = night.clients(scan_ep=Stub(lambda request, n: down()))
    with pytest.raises(RoleCallFailed):
        night.record(clients, overwrite=True)
    for name in RECORDINGS:
        assert (night.out / name).read_text(encoding="utf-8") == "earlier\n"
    assert not list(night.out.glob("*.part"))


def test_recordings_that_do_not_read_back_are_never_installed(
    night: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def drifting(*args: Any, **kwargs: Any) -> Any:
        raise CassetteDrift("the request for judge 0 differs from the recording")

    monkeypatch.setattr("auction_night.show.showrun.export_directory", drifting)
    with pytest.raises(ShowFailed, match="do not read back.*differs from the recording"):
        night.record()
    keep_out(night.out)


def test_findings_of_the_scan_do_not_stop_the_recording(night: World) -> None:
    flag = '{"flagged": [{"id": 1, "category": "real_person", "reason": "真人"}]}'
    clients = night.clients(scan_ep=Stub(lambda request, n: flag if n == 1 else '{"flagged": []}'))
    done = night.record(clients)
    assert done.scan is not None and not done.scan.ok
    assert (night.out / SCAN_CALLS_FILE).is_file()
    lines = describe_show(done)
    assert any("`auction-night export` will refuse this night" in line for line in lines)
    assert any("classifier: real_person: 真人" in line for line in lines)
    result = runner.invoke(app, ["export", str(night.out), "--no-git"])
    assert result.exit_code == 1 and "classifier: real_person: 真人" in result.output
    assert not (night.out / "replay.json").exists()


def test_a_finding_of_the_recorded_scan_is_released_by_a_person_and_the_night_exports(
    night: World, tmp_path: Path
) -> None:
    flag = '{"flagged": [{"id": 1, "category": "real_person", "reason": "真人"}]}'
    clients = night.clients(scan_ep=Stub(lambda request, n: flag if n == 1 else '{"flagged": []}'))
    done = night.record(clients)
    assert done.scan is not None and not done.scan.ok
    assert any("`export --clear FILE`" in line for line in describe_show(done))
    assert runner.invoke(app, ["export", str(night.out), "--no-git"]).exit_code == 1
    sheet = tmp_path / "clear.toml"
    sheet.write_text(
        '[[clear]]\npath = "seats[0].codename"\ncategory = "real_person"\n'
        'reason = "它是座位代号，不是真人"\n',
        encoding="utf-8",
    )
    result = runner.invoke(app, ["export", str(night.out), "--no-git", "--clear", str(sheet)])
    assert result.exit_code == 0, result.output
    assert "1 finding(s) cleared by hand" in result.output
    replay = Replay.model_validate_json((night.out / "replay.json").read_text(encoding="utf-8"))
    (record,) = replay.manifest.cleared_findings
    assert (record.path, record.category, record.detail) == (
        "seats[0].codename",
        "real_person",
        "真人",
    )
    assert record.reason == "它是座位代号，不是真人"
    assert replay.usage.by_role["classifier"].calls == 13, "the recorded scan is counted as before"


# ======================================================================================= cost


@pytest.fixture(scope="module")
def seat_cost(tmp_path_factory: pytest.TempPathFactory) -> int:
    """What the seats of the mock night cost at the test prices. The mock model's token counts
    do not change from run to run, so every night below costs the same."""
    raw = show_raw(cap_microusd=1_000_000_000)
    return make_world(tmp_path_factory.mktemp("cost"), raw, priced()).seats_microusd()


def capped_night(root: Path, cap_microusd: int) -> World:
    """A night run under a cap. The cap is above what the seats cost, so they do not reach it."""
    return make_world(root, show_raw(cap_microusd=cap_microusd), priced())


def test_the_calls_are_priced_and_counted_against_the_cap_of_the_show_layer(
    seat_cost: int, tmp_path: Path
) -> None:
    mine = capped_night(tmp_path, 10 * CALLS * PER_CALL)
    assert mine.seats_microusd() == seat_cost > 0
    done = mine.record(prices=priced())
    assert done.show_microusd == CALLS * PER_CALL and done.seats_microusd == seat_cost
    assert [r.tally.cost_microusd for r in done.roles] == [
        13 * PER_CALL, PER_CALL, PER_CALL, 13 * PER_CALL
    ]  # fmt: skip
    cassette = Cassette.replay(mine.out / HOST_CALLS_FILE)
    assert {e.cost_microusd for e in cassette.entries} == {PER_CALL}
    cost, cap = CALLS * PER_CALL / 1e6, 10 * CALLS * PER_CALL / 1e6
    shown = f"the show layer cost ${cost:.4f} of its ${cap:.4f} cap"
    assert f"{shown}; the seats cost ${seat_cost / 1e6:.4f}" in describe_show(done)


@pytest.mark.parametrize("calls_made", [4, 5])
def test_the_cap_stops_the_recording_and_no_file_is_left(
    seat_cost: int, tmp_path: Path, calls_made: int
) -> None:
    """The gate is checked after every call, so the cap is passed by one call at most."""
    cap = calls_made * PER_CALL - 1
    assert cap > seat_cost, "the night itself stays under its cap"
    mine = capped_night(tmp_path, cap)
    clients = mine.clients()
    with pytest.raises(CostCapReached) as caught:
        mine.record(clients, prices=priced())
    message = str(caught.value)
    assert f"the cost cap of ${cap / 1e6:.4f} is reached" in message
    assert f"the show layer has spent ${calls_made * PER_CALL / 1e6:.4f}" in message
    assert "so the host was not called" in message and "No recording was written" in message
    assert "limits.max_cost_per_night_usd" in message and "with -c" in message
    assert len(clients["host_ep"].requests) == calls_made
    assert not clients["judge_ep_0"].requests and not clients["scan_ep"].requests
    keep_out(mine.out)


def test_a_cap_that_the_last_call_reaches_stops_nothing(tmp_path: Path) -> None:
    short = capped_night(tmp_path / "short", (CALLS - 1) * PER_CALL)
    with pytest.raises(CostCapReached, match="the content classifier was not called"):
        short.record(prices=priced())
    keep_out(short.out)
    reached = capped_night(tmp_path / "reached", CALLS * PER_CALL - 1)
    done = reached.record(prices=priced())  # the last call is the one that reaches the cap
    assert done.calls == CALLS and len(done.files) == 3


def test_the_seats_cost_is_not_deducted_from_the_cap_of_the_show_layer(
    seat_cost: int, tmp_path: Path
) -> None:
    """A cap that fits the seats and the show layer apart but not together lets both through."""
    cap = CALLS * PER_CALL + 1_000
    assert seat_cost < cap < seat_cost + CALLS * PER_CALL
    mine = capped_night(tmp_path, cap)
    done = mine.record(prices=priced())
    assert done.calls == CALLS and done.show_microusd == CALLS * PER_CALL


def test_a_night_that_was_cut_short_by_its_cap_is_replayed_and_shown_under_the_same_cap(
    tmp_path: Path,
) -> None:
    mine = capped_night(tmp_path, 1)  # the cap is reached in the first phase of the night
    clients = mine.clients()
    with pytest.raises(CostCapReached, match="the show layer has spent"):
        mine.record(clients, prices=priced())
    assert len(clients["host_ep"].requests) == 1, "one call passes the cap, none after it"
    keep_out(mine.out)


def test_a_cap_of_zero_is_reached_before_the_first_call(tmp_path: Path) -> None:
    mine = capped_night(tmp_path, 0)
    clients = mine.clients()
    with pytest.raises(CostCapReached, match=r"cap of \$0.0000 is reached"):
        mine.record(clients, prices=priced())
    assert all(not client.requests for client in clients.values())


def test_the_cap_cannot_be_changed_for_the_replay_of_the_seats(
    seat_cost: int, tmp_path: Path
) -> None:
    """The closing cost event of the log holds the cap, so another cap is another night. That is
    why the message offers cheaper models and not a higher cap."""
    mine = capped_night(tmp_path, 4 * PER_CALL)
    roomy = mine.config.limits.model_copy(update={"max_cost_per_night_usd": 1000.0})
    clients = mine.clients()
    with pytest.raises(ExportError, match="differs from events.jsonl.*cap_microusd"):
        record_show(
            mine.config.model_copy(update={"limits": roomy}),
            mine.out,
            prices=priced(),
            clients=clients,
        )
    assert all(not client.requests for client in clients.values())
    keep_out(mine.out)


def test_cheaper_models_for_the_show_roles_in_a_copy_of_the_configuration_do_replay(
    seat_cost: int, tmp_path: Path
) -> None:
    """What the message offers does work: the show section is not part of the replay of the
    seats, so a copy with other models for the show roles replays the night."""
    mine = capped_night(tmp_path, 4 * PER_CALL)
    with pytest.raises(CostCapReached):
        mine.record(prices=priced())
    raw = copy.deepcopy(mine.raw)
    raw["show"]["host_offline"]["model"] = "cheap-host"
    for index, judge in enumerate(raw["show"]["judges"]):
        judge["model"] = f"cheap-judge-{index}"
    raw["show"]["content_scan"]["classifier"]["model"] = "cheap-scan"
    names = ("cheap-host", "cheap-judge-0", "cheap-judge-1", "cheap-scan")
    table = PriceTable({name: price(1.0) for name in names})
    done = record_show(
        NightConfig.model_validate(raw), mine.out, prices=table, clients=mine.clients()
    )
    assert done.calls == CALLS and [r.model for r in done.roles] == list(names)
    assert done.show_microusd == CALLS * 120 and len(done.files) == 3


def test_a_cap_needs_a_price_for_every_model_that_is_called(tmp_path: Path) -> None:
    mine = capped_night(tmp_path, 100 * CALLS * PER_CALL)
    table = PriceTable({m: p for m, p in priced().prices.items() if m != "scan-model"})
    clients = mine.clients()
    with pytest.raises(ShowSetupError, match="the cost cap cannot be enforced.*scan-model"):
        mine.record(clients, prices=table)
    assert all(not client.requests for client in clients.values())
    keep_out(mine.out)


def test_without_a_cap_a_model_without_a_price_is_counted_and_costs_nothing(night: World) -> None:
    done = night.record()
    assert done.cap_microusd is None and done.show_microusd == 0
    assert done.calls == CALLS == done.unpriced_calls
    lines = describe_show(done)
    assert "no model of the show layer has a price, so its cost is not counted" in lines


# ============================================================================== keys and clients


def test_clients_are_made_from_the_environment_for_the_roles_that_are_models_only(
    tmp_path: Path,
) -> None:
    raw = show_raw(judges=False, scan=False)
    raw["endpoints"]["host_ep"]["api_key_env"] = "HOST_TEST_KEY"
    raw["endpoints"]["judge_ep_0"] = {"kind": "anthropic", "api_key_env": "UNUSED_TEST_KEY"}
    mine = make_world(tmp_path, raw)
    made: list[dict[str, Any]] = []
    key = "a-made-up-key-for-a-test"
    done = record_show(
        mine.config,
        mine.out,
        environ={"HOST_TEST_KEY": key},  # the key of the unused Anthropic endpoint is not needed
        factories={"openai_compatible": mine.sdk(made)},
    )
    assert [r.role for r in done.roles] == ["host"]
    assert [(o["api_key"], o["base_url"], o["max_retries"]) for o in made] == [
        (key, "http://localhost:9/v1", 0)
    ]
    assert key not in (mine.out / HOST_CALLS_FILE).read_text(encoding="utf-8")


def test_a_host_on_an_anthropic_endpoint_is_recorded_and_read_back(tmp_path: Path) -> None:
    """The same path with the other API: a system block, a thinking mode pinned, no tools."""
    raw = show_raw(judges=False, scan=False)
    raw["endpoints"]["host_ep"] = {"kind": "anthropic", "api_key_env": "HOST_TEST_KEY"}
    raw["show"]["host_offline"] = {
        **HOST,
        "model": "claude-sonnet-5-5",
        "thinking": {"type": "adaptive"},
        "effort": "medium",
        "max_tokens": 600,
    }
    mine = make_world(tmp_path, raw)
    sent: list[dict[str, Any]] = []

    def sdk(**options: Any) -> Any:
        def create(**kwargs: Any) -> Any:
            sent.append(kwargs)
            return message(mine.host_answer(SimpleNamespace(payload=kwargs)))  # type: ignore[arg-type]

        return SimpleNamespace(messages=SimpleNamespace(create=create))

    done = record_show(
        mine.config,
        mine.out,
        environ={"HOST_TEST_KEY": "a-made-up-key-for-a-test"},
        factories={"anthropic": sdk},
    )
    assert [(r.role, r.model, r.tally.calls) for r in done.roles] == [
        ("host", "claude-sonnet-5-5", 13)
    ]
    assert len(sent) == 13 and all("tools" not in kwargs for kwargs in sent)
    assert sent[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
    entries = Cassette.replay(mine.out / HOST_CALLS_FILE).entries
    assert {e.kind for e in entries} == {"anthropic"} and len(entries) == 13
    assert export_directory(mine.config, mine.out, git=NO_GIT).report.ok


def test_a_missing_key_is_named_before_anything_is_replayed_or_written(tmp_path: Path) -> None:
    raw = show_raw(judges=False, scan=False)
    raw["endpoints"]["host_ep"]["api_key_env"] = "HOST_TEST_KEY"
    mine = make_world(tmp_path, raw)
    with pytest.raises(ShowSetupError, match="HOST_TEST_KEY"):
        record_show(mine.config, mine.out, environ={})
    keep_out(mine.out)


def test_a_client_that_is_missing_for_an_endpoint_is_a_setup_error(night: World) -> None:
    clients = night.clients()
    del clients["scan_ep"]
    with pytest.raises(ShowSetupError, match="no client for the endpoint 'scan_ep'"):
        night.record(clients)
    keep_out(night.out)


# ================================================================================= the command


class Desk:
    """Puts fake models in place of the clients that the command makes from the environment, and
    notes which endpoints it asked for."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.asked: list[list[str]] = []

    def use(self, clients: Mapping[str, Stub]) -> None:
        def fake_make_clients(config: Any, *, names: Any = None, **options: Any) -> Any:
            self.asked.append(sorted(names))
            return {name: clients[name] for name in names}

        self.monkeypatch.setattr("auction_night.show.showrun.make_clients", fake_make_clients)


@pytest.fixture
def desk(monkeypatch: pytest.MonkeyPatch) -> Desk:
    return Desk(monkeypatch)


def invoke(*args: str | Path) -> Any:
    return runner.invoke(app, ["show", *(str(a) for a in args)])


def test_the_command_records_and_export_needs_no_option(night: World, desk: Desk) -> None:
    desk.use(night.clients())
    result = invoke(night.out)
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "night mock-0001: the model calls of the show layer are recorded"
    assert [re.match(r"  (\S+)\s", line)[1] for line in lines[1:5]] == [  # type: ignore[index]
        "host", "judge:0", "judge:1", "classifier"
    ]  # fmt: skip
    assert "content scan: " in result.output and "nothing found" in result.output
    assert lines[-1] == (
        f"wrote {night.out / HOST_CALLS_FILE}, {night.out / JUDGE_CALLS_FILE} "
        f"and {night.out / SCAN_CALLS_FILE}"
    )
    assert night.recordings() == sorted(RECORDINGS) and not list(night.out.glob("*.part"))
    exported = runner.invoke(app, ["export", str(night.out), "--no-git"])
    assert exported.exit_code == 0, exported.output
    assert (night.out / "replay.json").is_file()


def test_the_command_asks_for_the_clients_of_the_roles_that_are_models_only(
    tmp_path: Path, desk: Desk
) -> None:
    mine = make_world(tmp_path, show_raw(judges=False, scan=False))
    desk.use({"host_ep": Stub(mine.host_answer)})
    result = invoke(mine.out)
    assert result.exit_code == 0, result.output
    assert desk.asked == [["host_ep"]] and mine.recordings() == [HOST_CALLS_FILE]
    assert "mock roles, nothing recorded: judge:0, judge:1" in result.output
    assert result.output.splitlines()[-1] == f"wrote {mine.out / HOST_CALLS_FILE}"


def test_the_command_says_when_no_role_is_a_model(tmp_path: Path) -> None:
    out = tmp_path / "night"
    assert runner.invoke(app, ["run", "-c", str(MOCK_CONFIG), "-o", str(out)]).exit_code == 0
    result = invoke(out)
    assert result.exit_code == 0, result.output
    assert result.output.strip() == (
        "night mock-0001: every role of the show layer is a mock, so there is nothing to record"
    )
    assert sorted(path.name for path in out.iterdir()) == [
        "calls.jsonl",
        "config.yaml",
        "events.jsonl",
    ]


def test_the_command_refuses_a_recording_in_the_way_and_replaces_it_when_told_to(
    night: World, desk: Desk
) -> None:
    desk.use(night.clients())
    assert invoke(night.out).exit_code == 0
    again = invoke(night.out)
    assert again.exit_code == 2
    assert "auction-night show: " in again.output and "already exists" in again.output
    assert "--overwrite" in again.output
    desk.use(night.clients())
    replaced = invoke(night.out, "--overwrite")
    assert replaced.exit_code == 0, replaced.output


def test_a_role_with_no_answer_exits_1_and_writes_no_file(night: World, desk: Desk) -> None:
    desk.use(night.clients(host_ep=Stub(lambda request, n: down())))
    result = invoke(night.out)
    assert result.exit_code == 1
    assert "auction-night show: the host got no answer for lot 1" in result.output
    keep_out(night.out)


def test_the_cost_cap_exits_1_and_writes_no_file(
    seat_cost: int, tmp_path: Path, desk: Desk
) -> None:
    mine = capped_night(tmp_path, 4 * PER_CALL - 1)
    prices = write_prices(tmp_path / "prices.yaml", priced())
    desk.use(mine.clients())
    result = invoke(mine.out, "--prices", prices)
    assert result.exit_code == 1, result.output
    assert "auction-night show: the cost cap of $0.4800 is reached" in result.output
    keep_out(mine.out)
    unpriced = invoke(mine.out)  # the cap cannot be enforced without a price list
    assert unpriced.exit_code == 2
    assert "auction-night show: the cost cap cannot be enforced: no price" in unpriced.output


def test_a_night_that_does_not_replay_exits_1(night: World, desk: Desk) -> None:
    path = night.out / "events.jsonl"
    path.write_text(path.read_text(encoding="utf-8").replace('"seq":9,', '"seq":90,', 1))
    desk.use(night.clients())
    result = invoke(night.out)
    assert result.exit_code == 1
    assert "auction-night show: the replay of the night differs from events.jsonl" in result.output
    keep_out(night.out)


def test_the_command_checks_its_files_and_options(night: World, tmp_path: Path) -> None:
    nowhere = invoke(tmp_path / "nowhere")
    assert nowhere.exit_code == 2 and "auction-night show: not a directory: " in nowhere.output
    (night.out / "config.yaml").unlink()
    absent = invoke(night.out)
    assert absent.exit_code == 2 and "no configuration: give --config" in absent.output
    named = invoke(night.out, "-c", tmp_path / "none.yaml")
    assert named.exit_code == 2 and "configuration not found: " in named.output
    broken = invoke(night.out, "-c", REPO / "configs" / "prices.example.yaml")
    assert broken.exit_code == 2 and "cannot read" in broken.output
    example = invoke(night.out, "-c", REPO / "configs" / "night_api.example.yaml")
    assert example.exit_code == 2 and "still holds placeholders" in example.output
    prices = invoke(night.out, "-c", MOCK_CONFIG, "--prices", tmp_path / "none.yaml")
    assert prices.exit_code == 2 and "price list not found" in prices.output
    assert runner.invoke(app, ["show"]).exit_code == 2  # the directory is required
    keep_out(night.out)


def test_the_configuration_can_be_given_instead_of_the_saved_one(
    night: World, desk: Desk, tmp_path: Path
) -> None:
    given = tmp_path / "given.yaml"
    shutil.move(night.out / "config.yaml", given)
    desk.use(night.clients())
    result = invoke(night.out, "--config", given)
    assert result.exit_code == 0, result.output
    assert night.recordings() == sorted(RECORDINGS)


def test_a_missing_key_exits_2_naming_the_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = show_raw(judges=False, scan=False)
    raw["endpoints"]["host_ep"]["api_key_env"] = "HOST_TEST_KEY"
    mine = make_world(tmp_path, raw)
    monkeypatch.delenv("HOST_TEST_KEY", raising=False)
    result = invoke(mine.out)
    assert result.exit_code == 2
    assert "auction-night show: " in result.output and "HOST_TEST_KEY" in result.output
    keep_out(mine.out)
