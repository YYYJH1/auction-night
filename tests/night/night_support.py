"""Helpers of the night, bot and theory tests: configurations, drivers and played nights.

A night is played by a *driver*: a mapping from seat to a player with an ``answer(request)``
method. Rule bots are players as they are. A model seat is stood in for by :class:`StandIn`,
which bids like a rule bot and makes up a plan, a line, a guess and now and then a pledge, so
that every phase of the engine is exercised without any model.
"""

import copy
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from engine_support import raw_config

from auction_night.bots import EconomistBot, TruthfulBot, make_bot
from auction_night.contracts import NightConfig
from auction_night.contracts.events import Pledge
from auction_night.engine.deal import Deal, deal
from auction_night.engine.night import Answer, NightState, Request, start, step

Player = Callable[[Request], Answer | None]  # None: the seat sends nothing at all


def make_config(seats: list[dict[str, Any]] | None = None, **night: Any) -> NightConfig:
    """The mock night with some fields replaced. ``night`` keys replace whole ``night`` keys,
    except ``budget_c`` (the budget scale) and ``missions`` (merged into the mission rules)."""
    raw = raw_config()
    budget_c = night.pop("budget_c", None)
    if budget_c is not None:
        raw["night"]["budget"]["c"] = budget_c
    missions = night.pop("missions", None)
    if missions is not None:
        raw["night"]["missions"].update(missions)
    raw["night"].update(copy.deepcopy(night))
    if seats is not None:
        raw["seats"] = copy.deepcopy(seats)
    return NightConfig.model_validate(raw)


def bot_seats(policies: list[tuple[str, bool]]) -> list[dict[str, Any]]:
    """Seat dicts for a table of bots only: ``(policy, pacing)`` per seat."""
    seats = []
    for number, (policy, pacing) in enumerate(policies, start=1):
        seat: dict[str, Any] = {
            "seat": number,
            "codename": f"机器人{number}",
            "kind": "bot",
            "policy": policy,
        }
        if policy == "economist":
            seat["pacing"] = pacing
        seats.append(seat)
    return seats


def all_bots_config(
    policy: str = "economist", *, pacing: bool = True, n_seats: int = 8, **night: Any
) -> NightConfig:
    """Eight (or ``n_seats``) bots of one policy and no missions: nobody holds one."""
    night.setdefault("missions", {"collectors": 0, "shills": 0})
    return make_config(bot_seats([(policy, pacing)] * n_seats), **night)


def players_for(config: NightConfig, *, pacing: bool = True) -> dict[int, Player]:
    """A player for every seat: the rule bot for a bot seat, a stand-in for the others."""
    players: dict[int, Player] = {}
    for seat in config.seats:
        if seat.kind == "bot":
            players[seat.seat] = make_bot(seat).answer
        else:
            numbers = (
                EconomistBot(seat.seat, pacing=pacing) if seat.seat % 2 else TruthfulBot(seat.seat)
            )
            players[seat.seat] = StandIn(seat.seat, numbers).answer
    return players


@dataclass
class StandIn:
    """A model seat without the model: bids like a rule bot, talks in made-up lines."""

    seat: int
    numbers: EconomistBot | TruthfulBot
    pledges: bool = True

    def answer(self, request: Request) -> Answer:
        facts = request.facts_of(self.seat)
        if request.phase == "plan":
            n = request.n_lots
            return Answer(
                seat=self.seat,
                priorities=tuple((self.seat + k) % 4 for k in range(n)),
                budget_plan=tuple(10 * self.seat + k for k in range(n)),
                persona=f"座位{self.seat}的打算",
                thought=f"{self.seat}号先看看形势",
            )
        if request.phase == "talk":
            assert request.lot is not None
            pledge = None
            if self.pledges and request.lot < request.n_lots and (self.seat + request.lot) % 4 == 0:
                pledge = Pledge(kind="no_bid", lot=request.lot + 1)
            elif self.pledges and (self.seat * request.lot) % 7 == 3:
                pledge = Pledge(kind="max_bid", lot=request.lot, amount=40 + self.seat)
            elif self.pledges and (self.seat + request.lot) % 9 == 5:
                other = 1 + self.seat % request.n_seats
                pledge = Pledge(kind="let_win", lot=request.lot, to=other)
            guess = 100 + 7 * self.seat if facts.guess_target is not None else None
            return Answer(
                seat=self.seat,
                say=f"第{request.lot}件，{self.seat}号发言",
                pledge=pledge,
                guess=guess,
                thought=f"{self.seat}号想：第{request.lot}件",
            )
        reply = self.numbers.answer(request)
        return Answer(seat=self.seat, value=reply.value, thought=f"{self.seat}号出{reply.value}")


@dataclass
class Transcript:
    """A night that was played: the final state, every request and every set of answers."""

    state: NightState
    requests: list[Request] = field(default_factory=list)
    answers: list[dict[int, Answer]] = field(default_factory=list)

    @property
    def events(self):
        return self.state.events


def play(
    config: NightConfig,
    the_deal: Deal,
    players: Mapping[int, Player] | None = None,
    **start_args: Any,
) -> Transcript:
    """Play a whole night. ``players`` defaults to :func:`players_for`."""
    players = players or players_for(config)
    state, request = start(config, the_deal, **start_args)
    transcript = Transcript(state)
    current: Request | None = request
    while current is not None:
        replies = {seat: players[seat](current) for seat in current.seats}
        answers = {seat: reply for seat, reply in replies.items() if reply is not None}
        transcript.requests.append(current)
        transcript.answers.append(answers)
        state, _, current = step(state, answers)
    transcript.state = state
    return transcript


def play_seed(config: NightConfig, seed: str, **start_args: Any) -> Transcript:
    """Deal with ``seed`` and play the night with the default players."""
    return play(config, deal(config, seed=seed), **start_args)


def replay(config: NightConfig, the_deal: Deal, transcript: Transcript, **start_args: Any):
    """Feed the recorded answers back into a fresh night and return the final state."""
    state, _ = start(config, the_deal, **start_args)
    for answers in transcript.answers:
        state, _, _ = step(state, answers)
    return state


def wild_players(config: NightConfig, seed: int) -> dict[int, Player]:
    """Players that answer at random: numbers anywhere, nothing, a timeout, far too much.

    They bid with a narrow grid, so that ties and exact-price revisions happen often. Plans,
    lines and guesses come from the stand-ins. The same ``seed`` gives the same behaviour.
    """
    rng = random.Random(seed)
    base = players_for(config)

    def wild(seat: int) -> Player:
        def act(request: Request) -> Answer | None:
            if request.phase in ("plan", "talk"):
                return base[seat](request)
            roll = rng.random()
            if roll < 0.06:
                return None
            if roll < 0.12:
                return Answer.abstained(seat, "timeout")
            if roll < 0.18:
                return Answer(seat=seat, value=10_000)
            return Answer(seat=seat, value=5 * rng.randrange(0, 80))

        return act

    return {seat.seat: wild(seat.seat) for seat in config.seats}
