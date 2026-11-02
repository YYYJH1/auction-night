"""The six auction formats and the registry that drives them.

The formats come in three kinds, chosen by the clock of the mechanism definition:

* ``sealed``   (first price, second price, all-pay, blind box): one round of bids.
* ``english``  (button auction): an initial round and revision rounds.
* ``dutch``    (descending levels with one-shot grab prices): one round of grab prices.

Each kind has a pure ``open_*`` function that says whom to ask first and a pure ``step_*``
function that takes the answers and says whom to ask next, or that the lot is decided. The
registry puts the three behind one interface, so that the night loop does not care which
format a lot has::

    step = open_lot(mech, tier=200, budgets=budgets, tie_key=keys)
    while not step.done:
        step = step_lot(step, collect_answers(step.ask, step.phase))
    outcome = step.outcome

The sealed and Dutch settlements are also available as plain functions of all the answers
(:func:`settle_sealed`, :func:`settle_dutch`).
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final, Literal

from auction_night.contracts.mechanism import MECHANISMS, FormatKey, MechanismDef
from auction_night.engine.formats.base import (
    Answers,
    BidRecord,
    LotOutcome,
    LotState,
    LotStep,
    TieKey,
    active_seats,
    floor_amount,
    normalize_bid,
)
from auction_night.engine.formats.dutch import (
    DutchState,
    dutch_asked,
    dutch_ladder,
    open_dutch,
    settle_dutch,
    step_dutch,
)
from auction_night.engine.formats.english import EnglishState, open_english, step_english
from auction_night.engine.formats.sealed import SealedState, open_sealed, settle_sealed, step_sealed

__all__ = [
    "REGISTRY",
    "Answers",
    "BidRecord",
    "DutchState",
    "EnglishState",
    "FormatImpl",
    "LotOutcome",
    "LotState",
    "LotStep",
    "SealedState",
    "TieKey",
    "active_seats",
    "dutch_asked",
    "dutch_ladder",
    "floor_amount",
    "normalize_bid",
    "open_dutch",
    "open_english",
    "open_lot",
    "open_sealed",
    "play_lot",
    "settle_dutch",
    "settle_sealed",
    "step_dutch",
    "step_english",
    "step_lot",
    "step_sealed",
]

Kind = Literal["sealed", "english", "dutch"]
_KIND_BY_CLOCK: Final[Mapping[str, Kind]] = MappingProxyType(
    {"none": "sealed", "ascending_button": "english", "descending_levels": "dutch"}
)


@dataclass(frozen=True, slots=True)
class FormatImpl:
    """How one format is opened and stepped."""

    key: FormatKey
    kind: Kind
    open: Callable[..., LotStep]
    step: Callable[[Any, Answers], LotStep]


def _no_hidden_value(true_value: int | None) -> None:
    if true_value is not None:
        raise ValueError("only the blind box has a hidden true value")


def _open_sealed(
    mech: MechanismDef,
    tier: int,
    budgets: Mapping[int, int],
    tie_key: TieKey,
    true_value: int | None,
) -> LotStep:
    return open_sealed(mech, budgets, tie_key, true_value=true_value)


def _open_english(
    mech: MechanismDef,
    tier: int,
    budgets: Mapping[int, int],
    tie_key: TieKey,
    true_value: int | None,
) -> LotStep:
    _no_hidden_value(true_value)
    return open_english(mech, budgets, tie_key)


def _open_dutch(
    mech: MechanismDef,
    tier: int,
    budgets: Mapping[int, int],
    tie_key: TieKey,
    true_value: int | None,
) -> LotStep:
    _no_hidden_value(true_value)
    return open_dutch(mech, tier, budgets, tie_key)


_IMPLS: Final[Mapping[Kind, tuple[Callable[..., LotStep], Callable[[Any, Answers], LotStep]]]] = {
    "sealed": (_open_sealed, step_sealed),
    "english": (_open_english, step_english),
    "dutch": (_open_dutch, step_dutch),
}


def _impl(key: FormatKey) -> FormatImpl:
    kind = _KIND_BY_CLOCK[MECHANISMS[key].clock]
    return FormatImpl(key, kind, *_IMPLS[kind])


REGISTRY: Final[Mapping[FormatKey, FormatImpl]] = MappingProxyType(
    {key: _impl(key) for key in MECHANISMS}
)


def open_lot(
    mech: MechanismDef,
    *,
    tier: int,
    budgets: Mapping[int, int],
    tie_key: TieKey,
    true_value: int | None = None,
) -> LotStep:
    """Open a lot of any format.

    ``budgets`` holds the remaining budget of every seat still in the night, ``tie_key`` the
    tie rank of each of them (lowest wins), ``tier`` the tier of the lot and ``true_value`` the
    hidden value of a blind box lot (and nothing else).
    """
    return REGISTRY[mech.key].open(mech, tier, budgets, tie_key, true_value)


def step_lot(step: LotStep, answers: Answers) -> LotStep:
    """Hand the answers to the seats that ``step`` asked and get the next step."""
    if step.done:
        raise ValueError("the lot is already decided")
    return REGISTRY[step.state.mech.key].step(step.state, answers)


def play_lot(
    mech: MechanismDef,
    *,
    tier: int,
    budgets: Mapping[int, int],
    tie_key: TieKey,
    answer: Callable[[LotStep], Answers],
    true_value: int | None = None,
) -> LotOutcome:
    """Play a lot to the end. ``answer`` is called with every step that asks questions."""
    step = open_lot(mech, tier=tier, budgets=budgets, tie_key=tie_key, true_value=true_value)
    while not step.done:
        step = step_lot(step, answer(step))
    assert step.outcome is not None
    return step.outcome
