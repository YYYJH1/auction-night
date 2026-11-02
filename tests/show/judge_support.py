"""Helpers of the judge tests: nights played by scripted mock policies, scripted judges, answers.

* ``mock_night`` plays a whole night through the engine with the mock policies of the model layer
  (``honest``, ``collude``, ``backstab``, ``random``) for the model seats and rule bots for the
  others. Nothing is recorded and nothing needs a network.
* ``hybrid_night`` gives a seat the talk of one policy and the bids of another, so that a ring
  that talks without bidding like one (or bids like one without a word) can be played.
* ``ScriptedJudge`` is a judge that answers what the test tells it to, call by call.
* ``extraction``, ``promise``, ``claim``, ``guess`` and ``answer`` write the judges' JSON.
* ``marked_night`` is a night whose every private number, mission word, inner monologue and plan
  persona is a unique marker, for the test that proves what a judge never sees.
"""

import copy
import dataclasses
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from engine_support import PINNED_SEATS, raw_config
from night_support import Player, make_config, play
from show_support import completion, message

from auction_night.bots import make_bot
from auction_night.contracts import NightConfig
from auction_night.engine.deal import Deal, deal
from auction_night.engine.night import Answer, NightState
from auction_night.llm.client import ClientReply, normalise_anthropic, normalise_openai
from auction_night.llm.mock import MockModels
from auction_night.llm.request import BuiltRequest
from auction_night.show.judges import JudgeJob, JudgeReply, JudgeRetry

MODEL_SEATS = (1, 2, 3, 4, 5, 6)


def seats_with(policies: Mapping[int, str]) -> list[dict[str, Any]]:
    """The seats of the mock night, with some model seats given another mock policy."""
    seats = copy.deepcopy(PINNED_SEATS)
    for seat in seats:
        if seat["seat"] in policies:
            seat["mock_policy"] = policies[seat["seat"]]
    return seats


def night_config(policies: Mapping[int, str] | None = None, **night: Any) -> NightConfig:
    """The mock night, with the policies of some seats replaced."""
    return make_config(seats_with(policies or {}), **night)


def hybrid_players(
    config: NightConfig,
    the_deal: Deal,
    *,
    talk: Mapping[int, str] | None = None,
    bids: Mapping[int, str] | None = None,
) -> dict[int, Player]:
    """A player for every seat. A model seat plays the policy of the configuration, except that
    ``talk`` replaces the policy of its plan and talk phases and ``bids`` that of its bid phases."""
    models = MockModels(config, the_deal)
    players: dict[int, Player] = {}
    for seat in config.seats:
        if seat.kind == "bot":
            players[seat.seat] = make_bot(seat).answer
            continue
        base = models.players[seat.seat]
        talker = dataclasses.replace(base, policy=(talk or {}).get(seat.seat, base.policy))
        bidder = dataclasses.replace(base, policy=(bids or {}).get(seat.seat, base.policy))

        def answer(request: Any, talker: Any = talker, bidder: Any = bidder) -> Answer | None:
            side = talker if request.phase in ("plan", "talk") else bidder
            return side.answer(request)

        players[seat.seat] = answer
    return players


def hybrid_night(
    seed: str,
    *,
    policies: Mapping[int, str] | None = None,
    talk: Mapping[int, str] | None = None,
    bids: Mapping[int, str] | None = None,
    **night: Any,
) -> NightState:
    """Play a night of mock policies (see :func:`hybrid_players`) and return its final state."""
    config = night_config(policies, **night)
    the_deal = deal(config, seed=seed)
    return play(config, the_deal, hybrid_players(config, the_deal, talk=talk, bids=bids)).state


def mock_night(seed: str, policies: Mapping[int, str] | None = None) -> NightState:
    """A night of the mock policies of the configuration (or of ``policies``)."""
    return hybrid_night(seed, policies=policies)


# ------------------------------------------------------------------------ scripted judges


@dataclass
class ScriptedJudge:
    """A judge that answers what it was told: ``replies[n]`` is the answer to call ``n`` (the
    last one repeats). A string is text as it is, a mapping is written as JSON, ``None`` is no
    answer at all. ``calls`` records ``retry is not None`` of every call and ``jobs`` the jobs."""

    index: int = 0
    vendor: str = "scripted"
    replies: Sequence[Any] = ()
    calls: list[bool] = field(default_factory=list)
    jobs: list[JudgeJob] = field(default_factory=list)
    retries: list[JudgeRetry] = field(default_factory=list)

    def write(self, job: JudgeJob, retry: JudgeRetry | None = None) -> JudgeReply | None:
        number = len(self.calls)
        self.calls.append(retry is not None)
        self.jobs.append(job)
        if retry is not None:
            self.retries.append(retry)
        reply = self.replies[min(number, len(self.replies) - 1)]
        if reply is None:
            return None
        text = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
        return JudgeReply(text, f"scripted-{self.index}")


def extraction(
    seat: int,
    lot: int,
    quote: str,
    kind: str,
    *,
    topic: str | None = None,
    number: int | None = None,
    text_value: str | None = None,
    ambiguous: bool = False,
    promise_class: str | None = None,
    partner: int | None = None,
) -> dict[str, Any]:
    """One item of a judge's ``extractions``."""
    return {
        "seat": seat,
        "lot": lot,
        "kind": kind,
        "quote": quote,
        "ambiguous": ambiguous,
        "topic": topic,
        "number": number,
        "text_value": text_value,
        "promise_class": promise_class,
        "partner": partner,
    }


def promise(
    seat: int,
    lot: int,
    quote: str,
    cls: str = "collusion",
    *,
    partner: int | None = None,
    ambiguous: bool = False,
) -> dict[str, Any]:
    """A promise of a class (``collusion``, ``bid`` or ``other``) and the seat it names, if any."""
    return extraction(
        seat,
        lot,
        quote,
        "promise",
        topic="plan",
        ambiguous=ambiguous,
        promise_class=cls,
        partner=partner,
    )


def claim(
    seat: int,
    lot: int,
    quote: str,
    topic: str,
    *,
    number: int | None = None,
    text_value: str | None = None,
    ambiguous: bool = False,
) -> dict[str, Any]:
    """A claim about one's own cards."""
    return extraction(
        seat,
        lot,
        quote,
        "claim",
        topic=topic,
        number=number,
        text_value=text_value,
        ambiguous=ambiguous,
    )


def guess(
    seat: int, kind: str, *, tag: str | None = None, lot: int | None = None
) -> dict[str, Any]:
    """One item of a judge's ``mission_guesses``."""
    return {"seat": seat, "kind": kind, "tag": tag, "lot": lot}


def answer(
    extractions: Sequence[dict[str, Any]] = (),
    guesses: Sequence[dict[str, Any]] = (),
    flags: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """The JSON object of a judge's whole answer."""
    return {
        "extractions": list(extractions),
        "mission_guesses": list(guesses),
        "injection_flags": list(flags),
    }


def same_for_both(
    reply: Any, *more: Any, vendors: tuple[str, str] = ("judge-a", "judge-b")
) -> list[ScriptedJudge]:
    """Two scripted judges that give the same replies."""
    return [
        ScriptedJudge(index=index, vendor=vendors[index], replies=(reply, *more))
        for index in (0, 1)
    ]


# ------------------------------------------------------------------------- model judges


def judge_config(kind: str = "openai_compatible") -> NightConfig:
    """The mock night with two model judges on two endpoints (nothing is ever called)."""
    raw = raw_config()
    raw["limits"]["transient_retries"] = 2  # as in a configuration of a paid night
    if kind == "anthropic":
        raw["endpoints"] = {
            f"judge_ep_{i}": {"kind": "anthropic", "api_key_env": f"JUDGE_TEST_KEY_{i}"}
            for i in (0, 1)
        }
        raw["show"]["judges"] = [
            {
                "kind": "llm",
                "endpoint": f"judge_ep_{i}",
                "model": "claude-sonnet-5-5",
                "vendor": f"vendor-j{i}",
                "thinking": {"type": "adaptive"},
                "effort": "medium",
                "max_tokens": 900,
            }
            for i in (0, 1)
        ]
    else:
        raw["endpoints"] = {
            f"judge_ep_{i}": {
                "kind": "openai_compatible",
                "base_url": f"http://localhost:{9 + i}/v1",
            }
            for i in (0, 1)
        }
        raw["show"]["judges"] = [
            {
                "kind": "llm",
                "endpoint": f"judge_ep_{i}",
                "model": f"judge-model-{i}",
                "vendor": f"vendor-j{i}",
            }
            for i in (0, 1)
        ]
    return NightConfig.model_validate(raw)


class FakeJudgeClient:
    """A model behind the client interface: ``respond(request, call_number)`` gives its answer.

    An exception returned by ``respond`` is raised, like the SDK would raise it. Every request is
    kept in ``requests``. The model id of the reply is the requested one with ``-0001`` added.
    """

    def __init__(self, respond: Any, kind: str = "openai_compatible") -> None:
        self.respond = respond
        self.kind = kind
        self.requests: list[BuiltRequest] = []

    def complete(self, request: BuiltRequest, *, timeout_s: float) -> ClientReply:
        self.requests.append(request)
        reply = self.respond(request, len(self.requests))
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, dict):  # a whole response, as the API sends it
            return normalise_openai(reply, latency_ms=4)
        if self.kind == "anthropic":
            return normalise_anthropic(message(reply), latency_ms=4)
        return normalise_openai(completion(reply, model=f"{request.model}-0001"), latency_ms=4)


def judge_index_of(request: BuiltRequest) -> int:
    """Which judge a request is for: the number at the end of its model id."""
    return int(request.model.rsplit("-", 1)[-1]) if request.kind != "anthropic" else 0


def user_text(request: BuiltRequest) -> str:
    """The text of the first user message of a request."""
    return str(next(m["content"] for m in request.payload["messages"] if m["role"] == "user"))


# --------------------------------------------------------------------------- marked nights

NUMBER = re.compile(r"\d{5,}")
CANARY = re.compile(r"CANARY-S\d+-ZXQ")
THOUGHT_MARK = re.compile(r"THOUGHT-S\d+-[a-z_]+-L\w+")
PERSONA_MARK = re.compile(r"PERSONA-S\d+")


def numbers_in(text: str) -> set[int]:
    """Every run of five digits or more in a text, as numbers."""
    return {int(found) for found in NUMBER.findall(text)}


@dataclass(frozen=True)
class Marks:
    """The markers of a marked deal: the private numbers and the mission words, by owner."""

    private: dict[int, set[int]]  # every private number of a seat, its budget included
    canaries: dict[int, str]
    blind_values: dict[int, int]  # lot -> hidden value

    def numbers(self) -> set[int]:
        """Every private number of the night, the hidden values of the blind boxes included."""
        found = set(self.blind_values.values())
        for numbers in self.private.values():
            found |= numbers
        return found


def marked_deal(config: NightConfig) -> tuple[Deal, Marks]:
    """The deal of ``config`` with every private number replaced by a unique, searchable one.

    Budgets are eight-digit numbers, values and appraisals six-digit numbers that differ for every
    seat and lot, the hidden value of a blind box is another six-digit number, and the canary of a
    mission is a word that names its seat. Nothing else of the deal changes.
    """
    base = deal(config)
    seats = range(1, base.n_seats + 1)
    budgets = {seat: 40_000_000 + 100_000 * seat + 11 for seat in seats}
    private = {seat: {budgets[seat]} for seat in seats}
    blind_values: dict[int, int] = {}
    lots = []
    for lot in base.lots:
        values: dict[int, int] = {}
        signals: dict[int, int] = {}
        if lot.is_blind:
            hidden = 500_000 + 100 * lot.lot + 7
            blind_values[lot.lot] = hidden
            values = dict.fromkeys(seats, hidden)
            for seat in seats:
                signals[seat] = 100_000 + 10_000 * seat + 100 * lot.lot + 3
                private[seat].add(signals[seat])
        else:
            for seat in seats:
                values[seat] = 100_000 + 10_000 * seat + 100 * lot.lot + 1
                private[seat].add(values[seat])
        lots.append(
            dataclasses.replace(
                lot, values=values, signals=signals, blind_value=blind_values.get(lot.lot)
            )
        )
    canaries = {seat: f"CANARY-S{seat}-ZXQ" for seat in base.missions}
    missions = {
        seat: mission.model_copy(update={"canary": canaries[seat]})
        for seat, mission in base.missions.items()
    }
    marked = dataclasses.replace(base, budgets=budgets, lots=tuple(lots), missions=missions)
    return marked, Marks(private, canaries, blind_values)


def _with_markers(seat: int, player: Player) -> Player:
    """A player whose thoughts and plan persona are unique markers."""

    def marked(request: Any) -> Answer | None:
        reply = player(request)
        if reply is None or reply.abstain is not None:
            return reply
        lot = "x" if request.lot is None else str(request.lot)
        return dataclasses.replace(
            reply,
            thought=f"THOUGHT-S{seat}-{request.phase}-L{lot}",
            persona=f"PERSONA-S{seat}" if request.phase == "plan" else reply.persona,
        )

    return marked


def marked_night() -> tuple[NightState, Marks]:
    """A night of ten lots in which every private thing is a unique marker.

    The five formats without an all-pay lot are used, so that nobody runs out of money. The
    random seat is made honest, which keeps every bid and exit price equal to a marker.
    """
    config = night_config(
        {6: "honest"},
        formats=["english", "dutch", "sealed_first", "sealed_second", "blind_box"],
        tags=["海洋", "机械", "甜品", "怪谈", "星空"],
        tiers={100: 4, 200: 3, 400: 3},
    )
    the_deal, marks = marked_deal(config)
    base = hybrid_players(config, the_deal)
    players = {
        seat: base[seat]
        if config.seats[seat - 1].kind == "bot"
        else _with_markers(seat, base[seat])
        for seat in base
    }
    return play(config, the_deal, players).state, marks
