"""Helpers of the show tests: scripted nights, hand-made lots and a fake host client.

* ``play_script`` plays a real night through the engine with seats that do exactly what a script
  says (a bid for a lot and seat, a line, a pledge). The budget scale is large, so no bid is
  ever clamped and a script can use any number it likes. This is how the tests get a winner's
  curse, an all-pay lot that takes in more than its tier, or a broken pledge on purpose.
* ``make_lot`` builds the data of one lot by hand, for the rules that only read a few fields.
* ``FakeHostClient`` stands in for a model behind the client interface. It never touches a
  network.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from night_support import Transcript, make_config, play

from auction_night.bots import make_bot
from auction_night.contracts import NightConfig
from auction_night.contracts.config import EndpointConfig, RoleConfig, ThinkingConfig
from auction_night.contracts.events import Pledge
from auction_night.contracts.mechanism import MECHANISMS, FormatKey
from auction_night.engine.deal import Deal, deal
from auction_night.engine.night import Answer, Request
from auction_night.llm.client import ClientReply, normalise_anthropic, normalise_openai
from auction_night.llm.request import BuiltRequest
from auction_night.show.cards import LotData, SeatLine

SEATS_SIX = [
    {"seat": n, "codename": name, "kind": "mock", "mock_policy": "honest"}
    for n, name in enumerate(["鲸落", "阿铁", "小满", "拾光", "青柠", "麦芽"], start=1)
]


def scripted_config(seats: list[dict[str, Any]] | None = None, **night: Any) -> NightConfig:
    """The mock night with a huge budget scale (no bid is ever clamped) and six model seats."""
    night.setdefault("budget_c", 5000)
    return make_config(seats if seats is not None else SEATS_SIX, **night)


def small_config(formats: Sequence[str], **night: Any) -> NightConfig:
    """A night with two lots of each given format (four formats, eight lots) and no bots.

    Tiers are 100 (four lots), 200 (two) and 400 (two), so it only fits four formats.
    """
    if len(formats) != 4:
        raise ValueError("small_config is made for four formats")
    night.setdefault("tiers", {100: 4, 200: 2, 400: 2})
    return scripted_config(formats=list(formats), lots_per_format=2, **night)


@dataclass
class Script:
    """What the scripted seats do.

    ``bids`` maps ``(lot, seat)`` to the number of every bid phase of that lot (None is an
    abstention). Anything not listed is ``default``. ``say`` and ``pledges`` are keyed the same
    way and belong to the talk phase of that lot. ``muted`` holds ``(lot, seat, kind)`` for the
    actions the cost gate skips: ``kind`` is ``talk`` or ``bid`` (every bid phase of the lot).
    """

    bids: dict[tuple[int, int], int | None] = field(default_factory=dict)
    say: dict[tuple[int, int], str] = field(default_factory=dict)
    pledges: dict[tuple[int, int], Pledge] = field(default_factory=dict)
    plans: dict[int, tuple[tuple[int, ...], tuple[int, ...]]] = field(default_factory=dict)
    default: int | None = 0
    muted: set[tuple[int, int, str]] = field(default_factory=set)

    def mute(self, lots: Iterable[int], seats: Iterable[int], *kinds: str) -> "Script":
        """Have the gate skip these seats in these lots (talk and bid unless ``kinds`` says)."""
        wanted, seats = kinds or ("talk", "bid"), tuple(seats)
        self.muted |= {(lot, seat, kind) for lot in lots for seat in seats for kind in wanted}
        return self


class ScriptedSeat:
    """A model seat without the model."""

    def __init__(self, seat: int, script: Script) -> None:
        self.seat = seat
        self.script = script

    def answer(self, request: Request) -> Answer:
        seat, script = self.seat, self.script
        if request.phase == "plan":
            n = request.n_lots
            stars, plan = script.plans.get(seat, ((1,) * n, (50,) * n))
            return Answer(seat=seat, priorities=stars, budget_plan=plan, persona="稳")
        assert request.lot is not None
        facts = request.facts_of(seat)
        kind = "talk" if request.phase == "talk" else "bid"
        if (request.lot, seat, kind) in script.muted:
            return Answer.abstained(seat, "budget_aborted")
        if request.phase == "talk":
            guess = 100 if facts.guess_target is not None else None
            return Answer(
                seat=seat,
                say=script.say.get((request.lot, seat), f"第{request.lot}件"),
                pledge=script.pledges.get((request.lot, seat)),
                guess=guess,
            )
        if request.phase == "english_revise":
            return Answer(seat=seat, value=facts.exit_price)
        value = script.bids.get((request.lot, seat), script.default)
        if value is None:
            return Answer.abstained(seat, "timeout")
        return Answer(seat=seat, value=value)


def play_script(
    script: Script, seed: str = "show-script-1", config: NightConfig | None = None
) -> tuple[NightConfig, Deal, Transcript]:
    """Deal with ``seed`` and play the night: scripted seats for the model seats, rule bots for
    the bots."""
    config = config or scripted_config()
    the_deal = deal(config, seed=seed)
    players = {
        seat.seat: make_bot(seat).answer
        if seat.kind == "bot"
        else ScriptedSeat(seat.seat, script).answer
        for seat in config.seats
    }
    return config, the_deal, play(config, the_deal, players)


def lots_of(the_deal: Deal, fmt: FormatKey) -> list[int]:
    """The numbers of the lots of one format, in lot order."""
    return [lot.lot for lot in the_deal.lots if lot.format == fmt]


# ---------------------------------------------------------------------------- hand-made lots


def seat_line(
    seat: int,
    *,
    role: str = "player",
    played: int | None = None,
    ref: int | None = None,
    asked: bool = True,
    abstained: bool = False,
    value: int | None = None,
    won: bool = False,
    paid: int = 0,
    **extra: Any,
) -> SeatLine:
    """One seat of a hand-made lot."""
    return SeatLine(
        seat=seat,
        codename=f"选手{seat}",
        role=role,  # type: ignore[arg-type]
        asked=asked,
        budget_before=300,
        budget_after=300 - paid,
        value=value,
        played=played,
        ref_bid=ref,
        abstained=abstained,
        won=won,
        paid=paid,
        **extra,
    )


def make_lot(
    lot: int = 1,
    fmt: FormatKey = "sealed_first",
    tier: int = 200,
    seats: Sequence[SeatLine] = (),
    **overrides: Any,
) -> LotData:
    """The data of one lot, written out by hand. Unlisted fields take plain defaults."""
    mech = MECHANISMS[fmt]
    fields: dict[str, Any] = {
        "lot": lot,
        "item": f"item-{lot}",
        "name": f"拍品{lot}",
        "emoji": "📦",
        "tag": "海洋",
        "tier": tier,
        "format": fmt,
        "title": mech.title_zh,
        "mechanism": mech.ref,
        "reserve": 0,
        "rule": "规则",
        "bidders": tuple(line.seat for line in seats if line.asked),
        "sitting_out": tuple(line.seat for line in seats if not line.asked),
        "seats": tuple(seats),
    }
    fields.update(overrides)
    return LotData(**fields)


# ------------------------------------------------------------------------------ the host


def host_endpoint(kind: str = "openai_compatible") -> EndpointConfig:
    """A local OpenAI-compatible endpoint, or an Anthropic one (never called)."""
    if kind == "anthropic":
        return EndpointConfig(kind="anthropic", api_key_env="HOST_TEST_KEY")
    return EndpointConfig(kind="openai_compatible", base_url="http://localhost:9/v1")


def host_role(kind: str = "openai_compatible") -> RoleConfig:
    """A model host on that endpoint."""
    if kind == "anthropic":
        return RoleConfig(
            kind="llm",
            endpoint="host_ep",
            model="claude-sonnet-5-5",
            vendor="vendor-h",
            thinking=ThinkingConfig(type="adaptive"),
            effort="medium",
            max_tokens=600,
        )
    return RoleConfig(kind="llm", endpoint="host_ep", model="host-model", vendor="vendor-h")


def completion(text: str, *, model: str = "host-model-0001") -> dict[str, Any]:
    """A chat completion as a plain mapping."""
    return {
        "id": "fake-1",
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }


def message(text: str) -> dict[str, Any]:
    """An Anthropic message as a plain mapping."""
    return {
        "id": "msg_fake",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }


Responder = Callable[[BuiltRequest, int], "str | BaseException"]


class FakeHostClient:
    """A model behind the client interface: ``respond(request, call_number)`` gives its answer.

    An exception returned by ``respond`` is raised, like the SDK would raise it. Every request
    is kept in ``requests``.
    """

    def __init__(self, respond: Responder, kind: str = "openai_compatible") -> None:
        self.respond = respond
        self.kind = kind
        self.requests: list[BuiltRequest] = []

    def complete(self, request: BuiltRequest, *, timeout_s: float) -> ClientReply:
        self.requests.append(request)
        answer = self.respond(request, len(self.requests))
        if isinstance(answer, BaseException):
            raise answer
        if self.kind == "anthropic":
            return normalise_anthropic(message(answer), latency_ms=4)
        return normalise_openai(completion(answer), latency_ms=4)


def last_user_text(request: BuiltRequest) -> str:
    """The text of the last user message of a request."""
    messages: Sequence[Mapping[str, Any]] = request.payload["messages"]
    return str(next(m["content"] for m in reversed(messages) if m["role"] == "user"))


def is_rewrite(request: BuiltRequest) -> bool:
    """Whether a request is the second try (it ends with the follow-up about the problems)."""
    return "有问题" in last_user_text(request)


def lot_of_request(request: BuiltRequest) -> int | None:
    """The lot a host request is about, read from its first user message."""
    first = next(m["content"] for m in request.payload["messages"] if m["role"] == "user")
    text = str(first)
    if "整夜的事实卡" in text:
        return None
    return int(text.split("第 ", 1)[1].split(" 件", 1)[0])
