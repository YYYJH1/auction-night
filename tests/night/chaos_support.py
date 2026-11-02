"""Random nights for the law tests: random rules, random tables, answers that misbehave.

A :class:`Case` is everything that makes one night: the shape of the lot list, the table (models
and bots), the reserve price, the English ``theta``, the number of Dutch levels, the size of the
budgets (or budgets set by hand to sit on the edge of a rule), the missions and a seed for the
answers. ``cases()`` draws them. ``players`` gives every seat of the table a player: the rule bot
for a bot seat, and for a model seat :func:`chaos`, which answers the bid phases with nothing, with
an abstention for any reason, with numbers on the edges of the rules and, now and then, with the
bid that a mission asks for. The plan and talk phases are answered by the stand-in of
``night_support``. With ``gate="revisions"`` the cost gate is on: the only abstention for the cap
(``budget_aborted``) is the one that every model seat gives to every English revision. That is the
night of a cap that is reached by the first English round of the last lot, when this lot is
English: the gate skips nothing but the revisions.
"""

import dataclasses
import random
from dataclasses import dataclass
from typing import Any

from hypothesis import strategies as st
from night_support import StandIn, make_config

from auction_night.bots import EconomistBot, TruthfulBot, make_bot
from auction_night.contracts import NightConfig
from auction_night.contracts.events import CollectorMission, ShillMission
from auction_night.engine.deal import Deal, deal
from auction_night.engine.night import Answer, Request

REASONS = ("missing", "parse_failed", "timeout", "refusal", "budget_aborted", "not_asked")

# Three shapes of lot list. Each fixes formats, tiers and what missions it can hold.
SHAPES: dict[str, dict[str, Any]] = {
    "full": {"formats": None, "lots_per_format": 2, "tiers": {100: 5, 200: 4, 400: 3}},
    "short": {
        "formats": ["english", "dutch", "sealed_first", "sealed_second"],
        "lots_per_format": 1,
        "tiers": {100: 2, 200: 1, 400: 1},
    },
    "pay": {
        "formats": ["all_pay", "blind_box"],
        "lots_per_format": 2,
        "tiers": {100: 2, 200: 1, 400: 1},
    },
}
SHILL_LOTS = {"full": 4, "short": 2, "pay": 0}  # lots of the formats a shill may pick


@dataclass(frozen=True)
class Case:
    """Everything that makes one random night."""

    shape: str = "full"
    models: int = 6
    bots: int = 2
    reserve: int = 0
    theta: int = 4
    levels: int = 10
    budget_c: int = 300
    collectors: int = 3
    shills: int = 3
    seed: int = 0
    silent: tuple[int, ...] = ()  # seats that never give a usable answer in a bid phase
    budgets: tuple[int, ...] = ()  # budgets that replace the dealt ones, seat 1 first
    mode: str = "plain"
    mindreading: bool = True
    gate: str = "off"  # "revisions": see the module text

    def config(self) -> NightConfig:
        shape = SHAPES[self.shape]
        seats: list[dict[str, Any]] = []
        for number in range(1, self.models + self.bots + 1):
            if number <= self.models:
                seat: dict[str, Any] = {"seat": number, "kind": "mock", "mock_policy": "honest"}
            else:
                seat = {
                    "seat": number,
                    "kind": "bot",
                    "policy": ("truthful", "economist")[number % 2],
                }
                if seat["policy"] == "economist":
                    seat["pacing"] = bool(number % 3)
            seat["codename"] = f"座位{number}"
            seats.append(seat)
        night: dict[str, Any] = {
            "lots_per_format": shape["lots_per_format"],
            "tiers": shape["tiers"],
            "reserve": self.reserve,
            "dutch": {"levels": self.levels},
            "english": {"revise_when_remaining_le": self.theta},
            "budget_c": self.budget_c,
            "mode": self.mode,
            "plan_and_mindreading": self.mindreading,
            "missions": {
                "collectors": self.collectors,
                "shills": self.shills,
                "collector_need": 2 if self.shape == "full" else 1,
            },
        }
        if shape["formats"] is not None:
            night["formats"] = shape["formats"]
        return make_config(seats, **night)

    def dealt(self, config: NightConfig) -> Deal:
        """The deal of the night, with the budgets set by hand where the case has them."""
        the_deal = deal(config, seed=f"laws-{self.seed}")
        if not self.budgets:
            return the_deal
        edge = dict(enumerate(self.budgets[: the_deal.n_seats], start=1))
        return dataclasses.replace(the_deal, budgets={**the_deal.budgets, **edge})


@st.composite
def cases(draw: st.DrawFn) -> Case:
    shape = draw(st.sampled_from(sorted(SHAPES)))
    models = draw(st.integers(0, 8))
    bots = draw(st.integers(max(0, 2 - models), 8 - models))
    collectors = draw(st.integers(0, min(models, 3)))
    shills = draw(st.integers(0, min(models - collectors, 3, SHILL_LOTS[shape])))
    silent = draw(st.sets(st.integers(1, max(models, 1)), max_size=2)) if models else set()
    reserve = draw(
        st.one_of(
            st.just(0), st.integers(0, 100), st.sampled_from([5, 10, 20, 25, 40, 50, 99, 100])
        )
    )
    levels = draw(st.sampled_from([2, 4, 5, 10, 20]))
    # budgets on the edge of a rule: next to the lowest Dutch level of a tier, or to the reserve
    lowest = {tier // levels for tier in (100, 200, 400)}
    edges = sorted({max(0, x + d) for x in (*lowest, reserve) for d in (-1, 0, 1)} | {1, 2})
    budgets = draw(st.lists(st.sampled_from(edges), max_size=8)) if draw(st.booleans()) else []
    return Case(
        shape=shape,
        models=models,
        bots=bots,
        reserve=reserve,
        theta=draw(st.integers(2, 8)),
        levels=levels,
        budgets=tuple(budgets),
        budget_c=draw(st.sampled_from([20, 45, 90, 150, 300, 600])),
        collectors=collectors,
        shills=shills,
        seed=draw(st.integers(0, 10**6)),
        silent=tuple(sorted(silent & set(range(1, models + 1)))),
        mode=draw(st.sampled_from(["plain", "variety"])),
        mindreading=draw(st.booleans()),
        gate=draw(st.sampled_from(["off", "off", "revisions"])),
    )


def chaos(case: Case, seat: int, stand_in: StandIn, the_deal: Deal) -> Any:
    """A model seat that misbehaves in the bid phases and is ordinary in the others.

    Seats go after missions now and then, so that the edges of a mission (a price equal to the
    threshold, the lot bought by the shill itself, exactly the number of lots a collector
    needs) are met: everybody bids around the threshold of a target lot, and a collector bids
    its whole budget on a lot with its tag.
    """
    rng = random.Random(f"{case.seed}:{seat}")
    silent = seat in case.silent
    gated = case.gate == "revisions"

    def reason() -> Any:
        # with the gate on, the revisions are the only place that names the cap
        chosen = rng.choice(REASONS)
        return "timeout" if gated and chosen == "budget_aborted" else chosen

    targets = {
        m.lot: m.threshold_price for m in the_deal.missions.values() if isinstance(m, ShillMission)
    }
    mission = the_deal.missions.get(seat)
    wanted_tag = mission.tag if isinstance(mission, CollectorMission) else None

    def act(request: Request) -> Answer | None:
        if request.phase in ("plan", "talk"):
            if rng.random() < 0.05:
                return Answer.abstained(seat, reason())
            return stand_in.answer(request)
        facts = request.facts_of(seat)
        if gated and request.phase == "english_revise":
            return Answer.abstained(seat, "budget_aborted")
        if silent:  # never a usable answer, in a different way every time
            return rng.choice([None, Answer.abstained(seat, reason())])
        roll = rng.random()
        if roll < 0.08:
            return None
        if roll < 0.16:
            return Answer.abstained(seat, reason())
        assert request.lot is not None
        if request.lot in targets and rng.random() < 0.4:
            threshold = targets[request.lot]
            return Answer(seat=seat, value=max(0, threshold + rng.choice([-1, 0, 0, 1, 50])))
        if wanted_tag == the_deal.lot(request.lot).tag and rng.random() < 0.5:
            return Answer(seat=seat, value=facts.budget_left)
        edges = [0, 1, facts.budget_left, facts.budget_left + 1, 10**6]
        edges += [request.reserve - 1, request.reserve, request.reserve + 1]
        if request.price is not None:
            edges += [request.price - 1, request.price, request.price + 1]
        if request.phase == "dutch" and request.tier is not None:
            step = request.tier // case.levels  # a grab price on, just below and just above a level
            edges += [step * k + shift for k in range(1, case.levels + 1) for shift in (-1, 0, 1)]
        mine = facts.value if facts.value is not None else facts.signal
        if mine is not None:
            edges += [mine, mine * 7 // 8, mine // 2]
        if roll < 0.55:
            return Answer(seat=seat, value=max(0, rng.choice(edges)))
        return Answer(seat=seat, value=5 * rng.randrange(0, 60))  # a coarse grid makes ties

    return act


def players(case: Case, config: NightConfig, the_deal: Deal) -> dict[int, Any]:
    """A player for every seat: the rule bot for a bot seat, :func:`chaos` for a model seat."""
    out: dict[int, Any] = {}
    for seat in config.seats:
        if seat.kind == "bot":
            out[seat.seat] = make_bot(seat).answer
        else:
            numbers = EconomistBot(seat.seat) if seat.seat % 2 else TruthfulBot(seat.seat)
            out[seat.seat] = chaos(case, seat.seat, StandIn(seat.seat, numbers), the_deal)
    return out
