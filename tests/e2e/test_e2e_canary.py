"""Private information stays private: the canary test of the model layer.

Every private number of a night (budgets, values, appraisals, the hidden value of each blind box)
and every mission canary is replaced by a unique marker, a long number or a word that names its
seat. The night is played through the whole model path, and at the moment each prompt is built
the test works out which markers the asking seat may not know: those of the other seats, the
hidden value of a blind box, the exit prices that the engine holds, the budgets of the others,
minus whatever the events that seat may see have already published. No prompt (system text, user
message or tool definition) may hold any of them.

The check is also run against a renderer that leaks on purpose, to show that it can fail.
"""

import dataclasses
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import pytest
from e2e_support import MODEL_SEATS, Marks, canary_config, marked_deal, play

import auction_night.llm.driver as driver_module
from auction_night.contracts import NightConfig
from auction_night.contracts.events import DealPrivateEvent, canonical_json
from auction_night.llm.driver import EngineDriver
from auction_night.llm.orchestrate import ModelCall
from auction_night.llm.render import visible_log

NUMBER = re.compile(r"\d{5,}")
CANARY = re.compile(r"CANARY-S\d+-ZXQ")
ENGINE_NAMES = (
    "tie_key",
    "exits",
    "blind_value",
    "true_value",
    "budgets_before",
    "lot_step",
    "winner_exit",
    "LotStep",
    "EnglishState",
    "SealedState",
    "DutchState",
    "NightState",
)


def numbers_in(text: str) -> set[int]:
    return {int(found) for found in NUMBER.findall(text)}


def big_ints(obj: Any) -> Iterator[int]:
    """Every integer of five digits or more inside an engine object (dataclasses, mappings,
    sequences). This is how the hidden fields of a lot are read without naming them."""
    if isinstance(obj, bool):
        return
    if isinstance(obj, int):
        if obj >= 10_000:
            yield obj
    elif dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        for field in dataclasses.fields(obj):
            yield from big_ints(getattr(obj, field.name))
    elif isinstance(obj, Mapping):
        for key, value in obj.items():
            yield from big_ints(key)
            yield from big_ints(value)
    elif isinstance(obj, list | tuple | set | frozenset):
        for item in obj:
            yield from big_ints(item)


@dataclass
class Seen:
    """One prompt, and what its reader may not know when it is built."""

    call: ModelCall
    seat: int
    forbidden: set[int]
    foreign_canaries: set[str]
    engine_numbers: set[int]

    @property
    def text(self) -> str:
        return json.dumps(self.call.request.payload, ensure_ascii=False)


def install_spy(monkeypatch: pytest.MonkeyPatch, marks: Marks) -> list[Seen]:
    """Record, for every call the driver builds, the numbers its reader may not know."""
    seen: list[Seen] = []
    original = EngineDriver._call

    def spy(self: EngineDriver, request: Any, seat: int) -> ModelCall:
        call = original(self, request, seat)
        state = self.state
        engine = set(big_ints(state.lot_step.state)) if state.lot_step is not None else set()
        engine |= set(state.budgets.values())
        hidden = marks.numbers() | engine
        own = marks.private[seat] | {state.budgets[seat]}
        published = numbers_in(
            "\n".join(canonical_json(e) for e in visible_log(state.events, seat))
        )
        seen.append(
            Seen(
                call,
                seat,
                hidden - own - published,
                {word for owner, word in marks.canaries.items() if owner != seat},
                engine - own - published,
            )
        )
        return call

    monkeypatch.setattr(EngineDriver, "_call", spy)
    return seen


def leaks(seen: list[Seen]) -> list[tuple[str, int, str, Any]]:
    """Every marker that a prompt holds and its reader may not know."""
    found: list[tuple[str, int, str, Any]] = []
    for item in seen:
        key = item.call.key
        for number in sorted(numbers_in(item.text) & item.forbidden):
            found.append((key.call_id, item.seat, "number", number))
        for word in sorted(set(CANARY.findall(item.text)) & item.foreign_canaries):
            found.append((key.call_id, item.seat, "canary", word))
    return found


@pytest.fixture(scope="module")
def config() -> NightConfig:
    return canary_config()


@pytest.fixture(scope="module")
def night(config: NightConfig) -> tuple[Any, Marks, list[Seen]]:
    """The marked night, played once, with the record of what every prompt may not hold."""
    the_deal, marks = marked_deal(config)
    with pytest.MonkeyPatch.context() as patch:
        seen = install_spy(patch, marks)
        played = play(config, the_deal=the_deal)
    return played, marks, seen


# ======================================================================================= isolation


def test_no_prompt_holds_anything_that_belongs_to_another_seat(night: Any) -> None:
    played, marks, seen = night
    assert played.result.status == "completed" and len(seen) == len(played.cassette)
    assert leaks(seen) == []


def test_the_check_is_not_vacuous(night: Any) -> None:
    played, marks, seen = night
    assert {item.seat for item in seen} == set(MODEL_SEATS)
    # a long list of numbers is forbidden for every reader; it shrinks as results are published
    assert seen[0].forbidden and max(len(item.forbidden) for item in seen) > 60
    assert all(len(item.forbidden) > 20 for item in seen)
    # the hidden state of the lots held numbers of its own: the exit prices that the engine keeps
    assert any(item.engine_numbers for item in seen)
    assert max(len(item.engine_numbers) for item in seen) >= 8


def test_a_seat_sees_its_own_cards_and_only_those(night: Any) -> None:
    played, marks, seen = night
    for seat in MODEL_SEATS:
        first = next(item for item in seen if item.seat == seat)
        system = json.dumps(first.call.request.payload["system"], ensure_ascii=False)
        owned = marks.private[seat]
        assert numbers_in(system) >= owned  # budget, every value and every appraisal
        assert marks.canaries[seat] in system  # the mission is told to its holder
        others = {word for owner, word in marks.canaries.items() if owner != seat}
        assert not set(CANARY.findall(system)) & others


def test_the_hidden_value_of_a_blind_box_is_unknown_until_the_hammer(night: Any) -> None:
    played, marks, seen = night
    assert marks.blind_values
    for lot, hidden in marks.blind_values.items():
        before = [item for item in seen if (item.call.key.lot or 0) <= lot]
        after = [item for item in seen if (item.call.key.lot or 0) > lot]
        assert before and after
        assert not any(hidden in numbers_in(item.text) for item in before)
        # once the hammer has fallen the value is public: it is in the results of every seat
        assert all(hidden in numbers_in(item.text) for item in after)


def test_the_winning_exit_price_of_an_english_lot_is_never_published(night: Any) -> None:
    played, marks, seen = night
    english = [e for e in played.of_type("lot_open") if e.data.format == "english"]
    assert english
    for opening in english:
        hammer = next(h for h in played.of_type("hammer") if h.lot == opening.lot)
        winner = hammer.data.winner
        inits = [a for a in played.actions(winner, "english_init", opening.lot)]
        exit_price = inits[0].data.value
        assert exit_price in marks.private[winner]  # the economist's exit price is its value
        for item in seen:
            if item.seat != winner:
                assert exit_price not in numbers_in(item.text)


def test_the_tool_definition_is_the_same_for_everybody(night: Any) -> None:
    played, marks, seen = night
    tools = {json.dumps(item.call.request.payload["tools"], sort_keys=True) for item in seen}
    assert len(tools) == 1
    assert numbers_in(next(iter(tools))) == set()


def test_no_prompt_names_a_field_of_the_engine_state(night: Any) -> None:
    played, marks, seen = night
    for item in seen:
        for name in ENGINE_NAMES:
            assert name not in item.text, name


# ========================================================================= the check can fail


def test_the_check_catches_a_renderer_that_leaks(
    monkeypatch: pytest.MonkeyPatch, config: NightConfig
) -> None:
    the_deal, marks = marked_deal(config)
    seen = install_spy(monkeypatch, marks)
    original = driver_module.render_observation

    def leaky(events: Any, seat: int, *args: Any, **kwargs: Any) -> str:
        text = original(events, seat, *args, **kwargs)
        neighbour = seat % 6 + 1  # reads another seat's card out of the full log
        card = next(
            e for e in events if isinstance(e, DealPrivateEvent) and e.data.seat == neighbour
        )
        return text + f"\n（偷看到的预算 {card.data.budget}）"

    monkeypatch.setattr(driver_module, "render_observation", leaky)
    play(config, the_deal=the_deal)
    found = leaks(seen)
    assert found
    assert {kind for _, _, kind, _ in found} == {"number"}
    assert {seat for _, seat, _, _ in found} == set(MODEL_SEATS)
    assert {number for _, _, _, number in found} <= set(marks.budgets.values())
