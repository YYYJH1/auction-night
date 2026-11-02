"""The bots sit at the table through the same requests and answers as the model seats."""

import ast
from pathlib import Path

import pytest
from night_support import all_bots_config, bot_seats, make_config, play, play_seed

from auction_night.bots import EconomistBot, TruthfulBot, bot_line, make_bot
from auction_night.contracts.config import SeatConfig
from auction_night.engine.deal import deal
from auction_night.engine.night import Answer

SRC = Path(__file__).resolve().parents[2] / "src" / "auction_night" / "bots"
MIXED = make_config()


def seat_config(**fields) -> SeatConfig:
    return SeatConfig.model_validate({"seat": 2, "codename": "测试", **fields})


def test_make_bot_builds_the_bot_a_seat_describes() -> None:
    bot = make_bot(seat_config(kind="bot", policy="economist", pacing=True))
    assert bot == EconomistBot(2, pacing=True)
    bot = make_bot(seat_config(kind="bot", policy="economist"))
    assert bot == EconomistBot(2, pacing=False)  # the configuration's own default is off
    assert make_bot(seat_config(kind="bot", policy="truthful")) == TruthfulBot(2)
    with pytest.raises(ValueError, match="not a bot seat"):
        make_bot(seat_config(kind="mock", mock_policy="honest"))


def test_a_night_of_bots_alone_is_played_through_the_request_and_answer_interface() -> None:
    config = all_bots_config("economist", pacing=True)
    bots = {seat.seat: make_bot(seat) for seat in config.seats}
    transcript = play(
        config, deal(config, seed="bots-alone"), {s: b.answer for s, b in bots.items()}
    )
    state = transcript.state
    assert state.done and state.pending is None
    assert all(isinstance(a, Answer) for answers in transcript.answers for a in answers.values())
    # a bot is asked to talk and to bid, never to plan, and it never guesses
    phases = {r.phase for r in transcript.requests}
    assert "plan" not in phases and "talk" in phases
    assert not [e for e in transcript.events if e.type in ("plan", "guess")]
    assert all(
        a.guess is None and a.pledge is None for ans in transcript.answers for a in ans.values()
    )
    # what the bots said in the channel is what their lines are
    talks = [e for e in transcript.events if e.type == "talk"]
    assert len(talks) == 12 * 8
    for event in talks:
        expected = bot_line(
            "economist",
            "plain",
            lot=event.lot,
            seat=event.data.seat,
            format=state.deal.lot(event.lot).format,
        )
        assert event.data.text == expected and event.data.pledge is None


def test_a_bot_gives_the_same_answer_to_the_same_request() -> None:
    config = make_config(
        bot_seats([("economist", True), ("truthful", False)] * 4),
        missions={"collectors": 0, "shills": 0},
    )
    transcript = play_seed(config, "bots-pure")
    bots = {seat.seat: make_bot(seat) for seat in config.seats}
    checked = 0
    for request in transcript.requests:
        for seat in request.seats:
            assert bots[seat].answer(request) == bots[seat].answer(request)
            assert bots[seat].answer(request) == transcript.answers[request.index][seat]
            checked += 1
    assert checked > 150


def test_the_bots_of_a_mixed_table_never_bid_outside_their_means() -> None:
    transcript = play_seed(MIXED, "bots-means")
    seen = 0
    for request in transcript.requests:
        for seat in (7, 8):
            if seat in request.seats and request.phase not in ("talk", "plan"):
                value = transcript.answers[request.index][seat].value
                assert value is not None and 0 <= value <= request.facts_of(seat).budget_left
                seen += 1
    assert seen > 12
    # and the engine never had to clamp them
    for event in transcript.events:
        if event.type == "action" and event.data.seat in (7, 8):
            assert event.data.status in ("ok", "kept_previous", "abstain")
            assert event.data.status != "clamped" and event.data.thought == ""


# ---------------------------------------------------------------- no model, no network


FORBIDDEN = {
    "openai",
    "anthropic",
    "httpx",
    "requests",
    "urllib",
    "socket",
    "http",
    "asyncio",
    "subprocess",
    "random",
    "secrets",
    "time",
    "datetime",
    "os",
    "pathlib",
    "numpy",
}


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


@pytest.mark.parametrize("path", sorted(SRC.glob("*.py")), ids=lambda p: p.name)
def test_the_bots_import_no_model_client_no_network_no_clock_and_no_randomness(path: Path) -> None:
    modules = imported_modules(path)
    assert modules, path
    for module in modules:
        root = module.split(".")[0]
        assert root not in FORBIDDEN, f"{path.name} imports {module}"
        assert not module.startswith("auction_night.llm"), f"{path.name} imports {module}"
        assert root in {
            "auction_night",
            "collections",
            "dataclasses",
            "fractions",
            "math",
            "typing",
        }, f"{path.name} imports {module}"
