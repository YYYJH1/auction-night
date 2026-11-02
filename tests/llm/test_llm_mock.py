"""The mock model: scripted seats, the faults it can inject, and the client interface it fills.

The policies are first played against the engine alone (no prompts, no model path) to show that
every answer is a legal ``act`` call. The client is then called with requests that the driver
built, the way the runner calls a real one.
"""

from dataclasses import replace

import pytest

from auction_night.bots import make_bot
from auction_night.contracts import NightConfig
from auction_night.contracts.act import FIELD_NAMES, ActContext, validate_act
from auction_night.engine.deal import deal
from auction_night.engine.night import Answer, Request, start, step
from auction_night.llm.cassette import Cassette
from auction_night.llm.mock import (
    FAULT_KINDS,
    Fault,
    MockClient,
    MockModels,
    MockOptions,
    SeatPlayer,
    act_args,
)
from auction_night.llm.nightrun import build_rig
from auction_night.llm.parse import parse_reply


@pytest.fixture
def config(mock_config: NightConfig) -> NightConfig:
    return mock_config


def players_of(config: NightConfig) -> dict[int, SeatPlayer]:
    the_deal = deal(config)
    return MockModels(config, the_deal).players


def play_alone(config: NightConfig) -> list[tuple[Request, dict[int, Answer | None]]]:
    """Play a whole night with the players and the bots only, and keep every round."""
    the_deal = deal(config)
    players = MockModels(config, the_deal).players
    bots = {s.seat: make_bot(s) for s in config.seats if s.kind == "bot"}
    state, request = start(config, the_deal)
    rounds = []
    while request is not None:
        answers = {
            seat: players[seat].answer(request) if seat in players else bots[seat].answer(request)
            for seat in request.seats
        }
        rounds.append((request, answers))
        state, _, request = step(state, {s: a for s, a in answers.items() if a is not None})
    return rounds


# ================================================================================= the policies


def test_every_answer_of_every_policy_is_a_legal_act_call(config: NightConfig) -> None:
    rounds = play_alone(config)
    checked = 0
    for request, answers in rounds:
        for seat, answer in answers.items():
            if seat > 6 or answer is None:
                continue
            args = act_args(request, answer)
            assert tuple(args) == FIELD_NAMES
            ctx = ActContext(
                n_lots=request.n_lots,
                n_seats=request.n_seats,
                self_seat=seat,
                current_lot=request.lot,
            )
            assert validate_act(args, request.phase, ctx) == [], (seat, request.phase)
            checked += 1
    assert checked > 150


def test_unused_fields_of_a_call_are_null(config: NightConfig) -> None:
    (request, answers), *_ = play_alone(config)
    plan = act_args(request, answers[1])  # type: ignore[arg-type]
    assert request.phase == "plan"
    assert plan["say"] is None and plan["bid"] is None and plan["pledge_kind"] is None
    assert len(plan["plan_priorities"]) == len(plan["plan_budget"]) == 12
    assert all(0 <= p <= 3 for p in plan["plan_priorities"]) and set(plan["plan_priorities"]) == {
        0,
        1,
        2,
        3,
    }


def test_a_policy_has_no_memory_the_same_request_gets_the_same_answer(config: NightConfig) -> None:
    rounds = play_alone(config)
    players = players_of(config)
    for request, answers in rounds[:40]:
        for seat, answer in answers.items():
            if seat in players:
                assert players[seat].answer(request) == answer


def test_the_bid_numbers_are_the_economist_s_unless_the_policy_changes_them(
    config: NightConfig,
) -> None:
    from auction_night.bots import EconomistBot

    rounds = play_alone(config)
    honest = 1
    for request, answers in rounds:
        if request.phase in ("plan", "talk") or honest not in answers:
            continue
        expected = EconomistBot(seat=honest, pacing=True).answer(request).value
        assert answers[honest].value == expected  # type: ignore[union-attr]


def test_colluders_take_the_lots_in_turn_and_say_so(config: NightConfig) -> None:
    rounds = play_alone(config)
    pledged: dict[int, set[int]] = {2: set(), 4: set()}
    bids: dict[int, dict[int, int]] = {2: {}, 4: {}}
    for request, answers in rounds:
        for seat in (2, 4):
            answer = answers.get(seat)
            if answer is None:
                continue
            assert request.lot is not None or request.phase == "plan"
            if request.phase == "talk" and answer.pledge is not None:
                assert answer.pledge.kind == "let_win"
                assert answer.pledge.to == (4 if seat == 2 else 2)
                pledged[seat].add(request.lot)  # type: ignore[arg-type]
            elif request.phase in ("english_init", "dutch", "sealed"):
                bids[seat][request.lot] = answer.value  # type: ignore[index]
    # the two partners never give way on the same lot, and between them they cover every lot
    assert not pledged[2] & pledged[4]
    assert pledged[2] | pledged[4] == set(range(1, 13))
    for seat in (2, 4):
        assert all(bids[seat][lot] == 0 for lot in pledged[seat] if lot in bids[seat])
        assert any(
            value > 0 for lot, value in bids[seat].items() if lot not in pledged[seat]
        )  # on its own lots a colluder bids as the economist does


def test_a_backstabber_pledges_not_to_bid_and_bids_all_the_same(config: NightConfig) -> None:
    rounds = play_alone(config)
    broken = 0
    pledged_lots = set()
    for request, answers in rounds:
        answer = answers.get(5)
        if answer is None:
            continue
        if request.phase == "talk":
            assert answer.pledge is not None and answer.pledge.kind == "no_bid"
            assert answer.pledge.lot == request.lot
            pledged_lots.add(request.lot)
        elif request.phase in ("english_init", "dutch", "sealed") and (answer.value or 0) > 0:
            broken += request.lot in pledged_lots
    assert broken >= 3


def test_a_random_player_varies_but_always_the_same_way(config: NightConfig) -> None:
    rounds = play_alone(config)
    bids = [a[6].value for r, a in rounds if r.phase == "sealed" and 6 in a]  # type: ignore[union-attr]
    talks = [a[6] for r, a in rounds if r.phase == "talk" and 6 in a]
    kinds = {answer.pledge.kind for answer in talks if answer is not None and answer.pledge}
    assert len(set(bids)) > 2 and kinds >= {"no_bid", "max_bid", "let_win"}
    assert rounds == play_alone(config)  # nothing in the mock is drawn from a clock or a global


def test_an_abstaining_seat_answers_nothing(config: NightConfig) -> None:
    the_deal = deal(config)
    player = replace(MockModels(config, the_deal).players[1], policy="abstain")
    state, request = start(config, the_deal)
    assert request is not None and player.answer(request) is None


def test_a_player_refuses_a_request_that_does_not_ask_it(config: NightConfig) -> None:
    the_deal = deal(config)
    player = MockModels(config, the_deal).players[1]
    _, request = start(config, the_deal)
    other = replace(request, seats=(2,))
    with pytest.raises(ValueError, match="was not asked"):
        player.answer(other)


# ====================================================================================== faults


def test_a_fault_selects_calls_by_seat_phase_and_lot() -> None:
    fault = Fault("timeout", seat=2, phase="sealed", lot=3)
    assert fault.applies(2, "sealed", 3)
    assert not fault.applies(1, "sealed", 3) and not fault.applies(2, "talk", 3)
    assert not fault.applies(2, "sealed", 4) and not fault.applies(2, "plan", None)
    anywhere = Fault("prose")
    assert anywhere.applies(5, "plan", None) and anywhere.applies(1, "dutch", 7)


def test_some_faults_only_make_sense_in_some_phases() -> None:
    assert Fault("inject_newline").applies(1, "talk", 1)
    assert not Fault("inject_newline").applies(1, "sealed", 1)
    assert Fault("over_budget").applies(1, "english_init", 2)
    assert not Fault("over_budget").applies(1, "talk", 2)
    with pytest.raises(ValueError, match="only applies to the talk phase"):
        Fault("inject_section", phase="sealed")
    with pytest.raises(ValueError, match="only applies to a bid phase"):
        Fault("text_amount", phase="plan")


def test_a_fault_must_be_known_and_spoil_at_least_one_call() -> None:
    with pytest.raises(ValueError, match="unknown fault"):
        Fault("explode")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="at least one"):
        Fault("prose", times=0)
    assert len(FAULT_KINDS) == len(set(FAULT_KINDS)) == 13


# ====================================================================================== the client


def first_call(config: NightConfig, **options: object):  # noqa: ANN201
    """The rig of a night and its first phase: the plan calls of the six mock seats."""
    rig = build_rig(config, Cassette.record(), mock=MockOptions(**options))  # type: ignore[arg-type]
    phase = rig.driver.next_phase()
    assert phase is not None and len(phase.calls) == 6
    return rig, phase


@pytest.mark.parametrize("kind", ["anthropic", "openai_compatible"])
def test_a_reply_reads_like_the_reply_of_a_real_model(config: NightConfig, kind: str) -> None:
    rig, phase = first_call(config, kind=kind)
    call = phase.calls[0]
    client = rig.clients[call.endpoint]
    reply = client.complete(call.request, timeout_s=5)
    assert reply.model == "mock-honest" and reply.latency_ms == 0 and reply.refusal is False
    assert reply.response is not None and reply.usage is not None
    result = parse_reply(reply.reply, call.ctx)
    assert result.ok and result.args is not None and result.args["phase"] == "plan"
    assert result.trace.source == "tool_call" and not result.trace.syntax_fixes
    assert len(result.args["plan_priorities"]) == 12


def test_both_kinds_of_reply_hold_the_same_action(config: NightConfig) -> None:
    args = []
    for kind in ("anthropic", "openai_compatible"):
        rig, phase = first_call(config, kind=kind)
        call = phase.calls[2]
        reply = rig.clients[call.endpoint].complete(call.request, timeout_s=5)
        args.append(parse_reply(reply.reply, call.ctx).args)
    assert args[0] == args[1]


def test_the_same_request_gets_the_same_reply_bytes(config: NightConfig) -> None:
    rig, phase = first_call(config)
    call = phase.calls[0]
    client = rig.clients[call.endpoint]
    one = client.complete(call.request, timeout_s=5).response
    two = client.complete(call.request, timeout_s=5).response
    assert one is not None and two is not None
    assert one["id"] == two["id"] and one["id"].startswith("msg_mock_")
    assert one["content"] == two["content"]  # only the usage differs: the cache has been written


def test_the_cache_is_read_when_the_prefix_is_unchanged(config: NightConfig) -> None:
    rig, phase = first_call(config)
    call = phase.calls[0]
    client = rig.clients[call.endpoint]
    first = client.complete(call.request, timeout_s=5).usage
    second = client.complete(call.request, timeout_s=5).usage
    assert first is not None and second is not None
    assert (first.cache_read_input_tokens, first.cache_write_input_tokens) == (
        0,
        second.prompt_tokens - second.input_tokens,
    )
    assert second.cache_read_input_tokens == first.cache_write_input_tokens > 0
    assert second.cache_write_input_tokens == 0
    # another system text is another prefix: the cache is written again
    changed = replace(
        call.request,
        payload={**call.request.payload, "system": [{"type": "text", "text": "another system"}]},
    )
    third = client.complete(changed, timeout_s=5).usage
    assert (
        third is not None
        and third.cache_read_input_tokens == 0
        and third.cache_write_input_tokens > 0
    )


def test_a_fault_spoils_the_next_calls_of_each_seat_separately(config: NightConfig) -> None:
    rig, phase = first_call(config, faults=(Fault("prose", times=2),))
    for call in phase.calls:
        client = rig.clients[call.endpoint]
        replies = [client.complete(call.request, timeout_s=5) for _ in range(3)]
        outcomes = [parse_reply(r.reply, call.ctx).status for r in replies]
        assert outcomes == ["retry", "retry", "ok"]  # the third call is clean for every seat


def test_a_timeout_is_raised_not_returned(config: NightConfig) -> None:
    rig, phase = first_call(config, faults=(Fault("timeout", seat=2),))
    call = next(c for c in phase.calls if c.seat == 2)
    with pytest.raises(TimeoutError):
        rig.clients[call.endpoint].complete(call.request, timeout_s=5)
    other = next(c for c in phase.calls if c.seat == 3)
    assert rig.clients[other.endpoint].complete(other.request, timeout_s=5).reply.tool_calls


def test_a_client_answers_only_while_the_engine_asks_its_seat(config: NightConfig) -> None:
    rig, phase = first_call(config)
    call = phase.calls[0]
    player = rig.mock.players[call.seat]  # type: ignore[index,union-attr]
    idle = MockClient(player, lambda: None, MockOptions())
    with pytest.raises(RuntimeError, match="did not ask it"):
        idle.complete(call.request, timeout_s=5)
    wrong_kind = MockClient(
        player, lambda: rig.driver.request, MockOptions(kind="openai_compatible")
    )
    with pytest.raises(ValueError, match="cannot answer"):
        wrong_kind.complete(call.request, timeout_s=5)


# ====================================================================================== assembly


def test_every_mock_seat_gets_an_endpoint_of_its_own(config: NightConfig) -> None:
    models = MockModels(config, deal(config))
    assert sorted(models.routes) == [1, 2, 3, 4, 5, 6]
    names = {route.name for route in models.routes.values()}
    assert len(names) == 6 and names == set(models.endpoints) == set(models.clients)
    assert all(len(name) <= 64 for name in names)
    assert models.models == ["mock-backstab", "mock-collude", "mock-honest", "mock-random"]
    assert {route.ref.model for route in models.routes.values()} == set(models.models)


def test_two_nights_can_share_one_runner(config: NightConfig) -> None:
    other = config.model_copy(update={"night": config.night.model_copy(update={"id": "mock-0002"})})
    one, two = MockModels(config, deal(config)), MockModels(other, deal(other))
    assert not set(one.clients) & set(two.clients)


def test_a_long_night_id_still_makes_a_valid_endpoint_name(config: NightConfig) -> None:
    long_id = "n" * 64
    other = config.model_copy(update={"night": config.night.model_copy(update={"id": long_id})})
    models = MockModels(other, deal(other))
    assert all(len(route.name) <= 64 for route in models.routes.values())
    assert len({route.name for route in models.routes.values()}) == 6


def test_the_mock_endpoint_holds_nothing_to_connect_with(config: NightConfig) -> None:
    for kind in ("anthropic", "openai_compatible"):
        models = MockModels(config, deal(config), MockOptions(kind=kind))  # type: ignore[arg-type]
        endpoint = next(iter(models.endpoints.values()))
        assert endpoint.kind == kind
        assert endpoint.base_url in (None, "http://localhost/mock")
