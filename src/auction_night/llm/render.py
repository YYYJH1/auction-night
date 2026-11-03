"""Prompts for a seat, rendered from the event log.

A prompt for seat ``N`` is built from the ``public`` events and the ``seat:N`` events of the log
and from nothing else (:func:`visible_log`), so private cards, plans and thoughts of other seats
cannot reach it. Two texts are produced:

* the **system prompt** (:func:`render_system`) holds the rules, the seat's private cards and the
  lot list. It depends only on the opening events, so it stays byte-identical for the whole
  night. That is what makes the request prefix cacheable;
* the **observation** (:func:`render_observation`) is the single user message of one call: the
  lot in progress, the seat's standing, past results, the latest chat and thoughts, and the
  instruction for the phase that is asked now. With ``night.memory`` on it also holds the seat's
  own opening plan and the public pledges that are still in force (:func:`_memory_block`).

The templates are plain data in ``auction_night/prompts/zh``. Rule texts come from an injected
callable (``RenderSettings.rules_text``), because they are generated from the mechanism
definitions elsewhere. Text that models wrote (chat, thoughts) is cleaned again and shown as a
quoted JSON string literal, so it cannot forge a line or a section of the prompt.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from auction_night.contracts.act import PHASE_TABLE, ActContext
from auction_night.contracts.config import MemoryMode, NightConfig
from auction_night.contracts.events import (
    ActionEvent,
    AnyEvent,
    CollectorMission,
    DealPrivateEvent,
    DutchResultEvent,
    EnglishDropoutEvent,
    HammerEvent,
    LotOpenEvent,
    LotPlan,
    NightStartData,
    NightStartEvent,
    PlanData,
    PlanEvent,
    Pledge,
    SeatPublic,
    TalkEvent,
    seat_vis,
    visible_to,
)
from auction_night.contracts.mechanism import (
    BID_PHASES,
    FORMAT_KEYS,
    MECHANISMS,
    FormatKey,
    MechanismDef,
    Phase,
)
from auction_night.llm.parse import ParseContext
from auction_night.llm.sanitize import TextLimits, clean_label, clean_text, quote_literal
from auction_night.prompts import TemplateSet, load_templates

RulesText = Callable[[MechanismDef], str]

_CN_COUNT: Final = {
    1: "一", 2: "两", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九", 10: "十",
    11: "十一", 12: "十二",
}  # fmt: skip
_PERSONA_DESCRIPTION_CHARS: Final = 300


class RenderError(ValueError):
    """The log does not hold what a prompt needs, or the request does not fit the log."""


@dataclass(frozen=True)
class Persona:
    """Variety mode: a persona changes the tone of a seat, never its goal."""

    name: str
    description: str


@dataclass(frozen=True)
class RenderSettings:
    """Everything besides the event log that a prompt depends on.

    ``mechanisms`` are the definitions of this night (reserve, theta and levels applied) for
    every format in play. ``rules_text`` turns one of them into the rule paragraph of the
    system prompt, and ``common_rules`` (optional) returns the paragraph of conventions that all
    formats share. Without it the template's own wording is used. The remaining fields mirror
    the night configuration.
    """

    mechanisms: Mapping[FormatKey, MechanismDef]
    rules_text: RulesText
    common_rules: Callable[[], str] | None = None
    limits: TextLimits = field(default_factory=TextLimits)
    chat_window_lots: int = 2
    thought_keep_last: int = 2
    collector_need: int = 2
    blind_range: tuple[float, float] = (0.5, 1.5)
    blind_noise: float = 0.2
    mindreading: bool = True
    memory: MemoryMode = "off"
    templates: TemplateSet = field(default_factory=load_templates)

    @classmethod
    def from_config(
        cls,
        config: NightConfig,
        rules_text: RulesText,
        *,
        common_rules: Callable[[], str] | None = None,
        templates: TemplateSet | None = None,
    ) -> "RenderSettings":
        """The settings that follow from a night configuration."""
        night = config.night
        return cls(
            mechanisms={key: night.mechanism_for(key) for key in night.formats},
            rules_text=rules_text,
            common_rules=common_rules,
            limits=TextLimits.from_night(night),
            chat_window_lots=night.chat.window_lots,
            thought_keep_last=night.thought.keep_last,
            collector_need=night.missions.collector_need,
            blind_range=night.blind.value_range,
            blind_noise=night.blind.noise,
            mindreading=night.plan_and_mindreading,
            memory=night.memory,
            templates=templates or load_templates(night.lang),
        )


@dataclass(frozen=True)
class RenderedPrompt:
    """The two texts of one call."""

    system: str
    observation: str


# --------------------------------------------------------------------------------- the view


def visible_log(
    events: Iterable[AnyEvent], seat: int, as_of_seq: int | None = None
) -> list[AnyEvent]:
    """The events seat ``seat`` may see, in order: public ones and its own.

    With ``as_of_seq`` only events up to that sequence number count, which lets one finished log
    stand in for the log at any earlier moment.
    """
    viewer = seat_vis(seat)
    kept = [
        event
        for event in events
        if (as_of_seq is None or event.seq <= as_of_seq) and visible_to(event.vis, viewer)
    ]
    return sorted(kept, key=lambda event: event.seq)


@dataclass(frozen=True)
class SeatView:
    """What one seat knows at one moment, derived from its visible events."""

    seat: int
    night: NightStartEvent
    deal: DealPrivateEvent
    lots: Mapping[int, LotPlan]
    seats: Mapping[int, SeatPublic]
    log: Sequence[AnyEvent]
    spent: int = 0
    out: tuple[int, ...] = ()
    won: tuple[tuple[int, int], ...] = ()  # (lot, price) of the lots this seat won
    current: LotOpenEvent | None = None

    @property
    def left(self) -> int:
        """Remaining budget: the dealt budget minus every payment so far."""
        return self.deal.data.budget - self.spent

    def codename(self, seat: int) -> str:
        """The cleaned code name of a seat."""
        try:
            return clean_label(self.seats[seat].codename).text
        except KeyError:
            raise RenderError(f"seat {seat} is not part of the night") from None

    def lot_events(self, lot: int) -> list[AnyEvent]:
        """Visible events that belong to one lot."""
        return [event for event in self.log if event.lot == lot]


def _opening_view(log: list[AnyEvent], seat: int) -> SeatView:
    night = next((e for e in log if isinstance(e, NightStartEvent)), None)
    deal = next((e for e in log if isinstance(e, DealPrivateEvent) and e.data.seat == seat), None)
    if night is None:
        raise RenderError("the log has no night_start event")
    if deal is None:
        raise RenderError(f"seat {seat} has no deal_private event")
    return SeatView(
        seat=seat,
        night=night,
        deal=deal,
        lots={plan.lot: plan for plan in night.data.lots},
        seats={entry.seat: entry for entry in night.data.seats},
        log=log,
    )


def build_view(events: Iterable[AnyEvent], seat: int, as_of_seq: int | None = None) -> SeatView:
    """Fold the visible events of one seat into a :class:`SeatView`."""
    log = visible_log(events, seat, as_of_seq)
    base = _opening_view(log, seat)
    spent = 0
    out: set[int] = set()
    won: list[tuple[int, int]] = []
    current: LotOpenEvent | None = None
    for event in log:
        if isinstance(event, HammerEvent):
            spent += event.data.payments.get(seat, 0)
            out.update(event.data.newly_out)
            if event.data.winner == seat and event.lot is not None:
                won.append((event.lot, event.data.price))
        elif isinstance(event, LotOpenEvent):
            current = event
    return SeatView(
        seat=seat,
        night=base.night,
        deal=base.deal,
        lots=base.lots,
        seats=base.seats,
        log=log,
        spent=spent,
        out=tuple(sorted(out)),
        won=tuple(won),
        current=current,
    )


# ----------------------------------------------------------------------------- small pieces


def _cn_count(count: int) -> str:
    return _CN_COUNT.get(count, str(count))


def _ratio(value: float) -> str:
    return format(Decimal(str(value)).normalize(), "f")


def _percent(value: float) -> str:
    return format((Decimal(str(value)) * 100).normalize(), "f")


def _seat_list(seats: Iterable[int], empty: str = "无") -> str:
    numbers = list(seats)
    if not numbers:
        return empty
    return "、".join(str(number) for number in numbers) + " 号"


def _join_names(items: Sequence[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return "、".join(items[:-1]) + "和 " + items[-1]


def _rule_title(key: FormatKey) -> str:
    return MECHANISMS[key].title_zh


def _formats_in_play(night: NightStartData) -> list[FormatKey]:
    present = {plan.format for plan in night.lots}
    return [key for key in FORMAT_KEYS if key in present]


def _own_number(view: SeatView, lot: int) -> int:
    """The seat's private number for one lot: its value, or its signal for a blind box."""
    plan = view.lots[lot]
    table = view.deal.data.signals if plan.format == "blind_box" else view.deal.data.values
    if lot not in table:
        raise RenderError(f"seat {view.seat} has no private number for lot {lot}")
    return table[lot]


def _pledge_text(texts: TemplateSet, pledge: Pledge) -> str:
    if pledge.kind == "no_bid":
        return texts.format("observe.pledge_no_bid", lot=pledge.lot)
    if pledge.kind == "max_bid":
        return texts.format("observe.pledge_max_bid", lot=pledge.lot, amount=pledge.amount)
    if pledge.kind == "let_win":
        return texts.format("observe.pledge_let_win", lot=pledge.lot, to=pledge.to)
    return texts.format("observe.pledge_other", kind=clean_label(str(pledge.kind)).text)


# ------------------------------------------------------------------------- system prompt


def _roster(texts: TemplateSet, data: NightStartData) -> str:
    players = [entry for entry in data.seats if entry.role == "player"]
    bots = [entry for entry in data.seats if entry.role == "bot"]
    if not bots:
        return texts.format("system.roster_all", n_seats=len(data.seats))
    names = [f"{entry.seat} 号「{clean_label(entry.codename).text}」" for entry in bots]
    return texts.format(
        "system.roster_bots",
        n_seats=len(data.seats),
        n_players=len(players),
        bots=_join_names(names),
    )


def _score_block(texts: TemplateSet, data: NightStartData) -> list[str]:
    lines = [texts.get("system.score")]
    if data.collectors + data.shills > 0:
        lines.append(texts.format("system.score_mission", bonus=data.mission_bonus))
    return lines


def _common_block(settings: RenderSettings, data: NightStartData) -> list[str]:
    texts = settings.templates
    lines = [texts.get("system.common_head"), texts.get("system.common_values")]
    if "blind_box" in _formats_in_play(data):
        low, high = settings.blind_range
        lines.append(
            texts.format(
                "system.common_blind",
                low=_ratio(low),
                high=_ratio(high),
                noise=_percent(settings.blind_noise),
            )
        )
    lines.append(texts.format("system.common_budget", low=data.budget_lo, high=data.budget_hi))
    holders = data.collectors + data.shills
    if holders:
        n_players = sum(1 for entry in data.seats if entry.role == "player")
        parts: list[str] = []
        if data.collectors:
            parts.append(
                texts.format(
                    "system.mission_part_collector",
                    count=data.collectors,
                    need=settings.collector_need,
                )
            )
        if data.shills:
            parts.append(texts.format("system.mission_part_shill", count=data.shills))
        everyone = holders == n_players
        lines.append(
            texts.format(
                "system.common_missions_all" if everyone else "system.common_missions_some",
                n_players=n_players,
                parts="；".join(parts),
            )
        )
    return lines


def _private_block(view: SeatView, texts: TemplateSet) -> list[str]:
    deal = view.deal.data
    lines = [
        texts.get("system.private_head"),
        texts.format("system.private_budget", budget=deal.budget),
    ]
    mission = deal.mission
    if isinstance(mission, CollectorMission):
        lines.append(
            texts.format(
                "system.private_collector",
                need=mission.need,
                tag=clean_label(mission.tag).text,
                canary=clean_label(mission.canary).text,
            )
        )
    elif mission is not None:
        target = view.lots.get(mission.lot)
        if target is None:
            raise RenderError(f"the shill mission names lot {mission.lot}, which is not listed")
        lines.append(
            texts.format(
                "system.private_shill",
                lot=mission.lot,
                rule=_rule_title(target.format),
                tier=target.tier,
                threshold=mission.threshold_price,
                canary=clean_label(mission.canary).text,
            )
        )
    lines.append(texts.get("system.private_values"))
    return lines


def _lot_block(view: SeatView, texts: TemplateSet) -> list[str]:
    lines = [texts.get("system.lots_head")]
    for plan in view.lots.values():
        mine = texts.format(
            "system.lot_signal" if plan.format == "blind_box" else "system.lot_value",
            value=_own_number(view, plan.lot),
        )
        line = texts.format(
            "system.lot_line",
            lot=plan.lot,
            emoji=clean_label(plan.emoji).text,
            name=clean_text(plan.name, None).text,
            tag=clean_label(plan.tag).text,
            tier=plan.tier,
            rule=_rule_title(plan.format),
            mine=mine,
        )
        if plan.reserve > 0:
            line += texts.format("system.lot_reserve", reserve=plan.reserve)
        lines.append(line)
    return lines


def _rules_block(settings: RenderSettings, data: NightStartData) -> list[str]:
    texts = settings.templates
    keys = _formats_in_play(data)
    lines = [texts.format("system.rules_head", count=_cn_count(len(keys)))]
    for key in keys:
        try:
            mechanism = settings.mechanisms[key]
        except KeyError:
            raise RenderError(f"no mechanism definition for format {key!r}") from None
        lines.append("- " + settings.rules_text(mechanism).strip())
    if settings.common_rules is not None:
        lines.append("- " + settings.common_rules().strip())
    else:
        lines.append(texts.get("system.rules_common"))
    return lines


def render_system(
    events: Iterable[AnyEvent],
    seat: int,
    settings: RenderSettings,
    *,
    persona: Persona | None = None,
) -> str:
    """The system prompt of one seat.

    It reads only the opening events (``night_start`` and the seat's ``deal_private``), so every
    later event leaves it unchanged.
    """
    view = _opening_view(visible_log(events, seat), seat)
    texts = settings.templates
    limits = settings.limits
    data = view.night.data
    blocks: list[list[str]] = [
        [
            texts.format(
                "system.intro",
                seat=seat,
                codename=view.codename(seat),
                n_seats=len(data.seats),
                n_lots=data.n_lots,
                roster=_roster(texts, data),
            )
        ],
        _score_block(texts, data),
        _common_block(settings, data),
        _private_block(view, texts),
    ]
    if persona is not None:
        description = clean_text(persona.description, _PERSONA_DESCRIPTION_CHARS).text
        blocks.append(
            [
                texts.format(
                    "system.persona",
                    name=clean_label(persona.name).text,
                    description=description,
                )
            ]
        )
    blocks += [
        _lot_block(view, texts),
        _rules_block(settings, data),
        [texts.format("system.channel", say_max=limits.say)],
    ]
    if settings.mindreading:
        blocks.append([texts.get("system.plan")])
    blocks += [
        [texts.format("system.thought", thought_max=limits.thought)],
        [texts.get("system.donts")],
        [texts.get("system.reply")],
    ]
    return "\n\n".join("\n".join(block) for block in blocks)


# ------------------------------------------------------------------------------ observation


def _progress_text(view: SeatView, texts: TemplateSet) -> str | None:
    mission = view.deal.data.mission
    if mission is None:
        return None
    if isinstance(mission, CollectorMission):
        done = sum(1 for lot, _ in view.won if view.lots[lot].tag == mission.tag)
        detail = texts.format(
            "observe.progress_collector",
            tag=clean_label(mission.tag).text,
            done=done,
            need=mission.need,
        )
    else:
        detail = texts.format(
            "observe.progress_shill", lot=mission.lot, threshold=mission.threshold_price
        )
    return texts.format("observe.progress", text=detail)


def _result_line(view: SeatView, lot: int, texts: TemplateSet) -> str | None:
    """One line about a finished lot, from public events only. None while it is running."""
    events = view.lot_events(lot)
    hammer = next((e for e in reversed(events) if isinstance(e, HammerEvent)), None)
    if hammer is None:
        return None
    plan = view.lots[lot]
    data = hammer.data
    key = data.format
    parts: list[str] = []
    if key == "english":
        drops = [e.data for e in events if isinstance(e, EnglishDropoutEvent)]
        items = [
            texts.format("result.dropout_item", seat=d.seat, price=d.price)
            for d in drops
            if d.participated
        ]
        parts.append(texts.format("result.english_dropouts", items="、".join(items) or "无"))
        absent = [d.seat for d in drops if not d.participated]
        if absent:
            parts.append(texts.format("result.english_absent", seats=_seat_list(absent)))
    elif key == "dutch":
        fired = next((e for e in events if isinstance(e, DutchResultEvent)), None)
        if fired is not None and fired.data.level is not None:
            parts.append(texts.format("result.dutch_level", level=fired.data.level))
    elif data.bids:
        items = [
            texts.format("result.bid_item", seat=seat, amount=amount)
            for seat, amount in sorted(data.bids.items())
        ]
        parts.append(texts.format("result.bids", items="、".join(items)))
    if key == "blind_box" and data.true_value is not None:
        parts.append(texts.format("result.true_value", value=data.true_value))

    if data.unsold or data.winner is None:
        parts.append(texts.get("result.unsold"))
    else:
        sold = texts.format("result.sold", winner=data.winner, price=data.price)
        if data.tie_broken:
            sold += texts.format("result.tie", tied=_seat_list(data.tied))
        parts.append(sold)
        if key == "all_pay":
            parts.append(texts.get("result.all_pay"))
    if data.newly_out:
        parts.append(texts.format("result.newly_out", seats=_seat_list(data.newly_out)))
    return texts.format(
        "result.line",
        lot=lot,
        emoji=clean_label(plan.emoji).text,
        name=clean_text(plan.name, None).text,
        rule=_rule_title(key),
        parts=texts.get("result.separator").join(parts),
    )


def render_results(events: Iterable[AnyEvent], seat: int, settings: RenderSettings) -> list[str]:
    """One line per finished lot, as ``seat`` reads them in the 【往期结果】 section.

    The lines come from the events that seat may see, so a person at a terminal can be shown
    the same account of the night that a model gets in its observation.
    """
    view = build_view(events, seat)
    lines = (_result_line(view, lot, settings.templates) for lot in sorted(view.lots))
    return [line for line in lines if line is not None]


def _chat_lines(view: SeatView, settings: RenderSettings) -> list[str]:
    texts = settings.templates
    if view.current is None or view.current.lot is None:
        return []
    last = view.current.lot
    first = last - settings.chat_window_lots + 1
    lines = []
    for event in view.log:
        if not isinstance(event, TalkEvent) or event.lot is None or not first <= event.lot <= last:
            continue
        data = event.data
        text = clean_text(data.text, settings.limits.say).text
        pledge = ""
        if data.pledge is not None:
            pledge = texts.format("observe.pledge_suffix", text=_pledge_text(texts, data.pledge))
        lines.append(
            texts.format(
                "observe.chat_line",
                lot=event.lot,
                seat=data.seat,
                codename=view.codename(data.seat),
                said=quote_literal(text) if text else texts.get("observe.chat_silent"),
                pledge=pledge,
            )
        )
    return lines


def _own_thoughts(view: SeatView, settings: RenderSettings) -> list[str]:
    thoughts = [
        clean_text(event.data.thought, settings.limits.thought).text
        for event in view.log
        if isinstance(event, ActionEvent) and event.data.seat == view.seat
    ]
    thoughts = [thought for thought in thoughts if thought]
    keep = settings.thought_keep_last
    return thoughts[-keep:] if keep > 0 else []


def _own_plan(view: SeatView) -> PlanData | None:
    """The opening plan of the seat itself, if it handed one in.

    The view holds the public events and the seat's own, so a plan of another seat cannot be in
    it. The check of the seat is a second lock, not the first one.
    """
    plans = [
        event.data
        for event in view.log
        if isinstance(event, PlanEvent) and event.data.seat == view.seat
    ]
    return plans[-1] if plans else None


def _was_asked_for_a_plan(view: SeatView) -> bool:
    """Whether the seat went through the plan phase. The engine records an action for it whether
    or not a plan came out, so a night without a plan phase has none."""
    return any(
        isinstance(event, ActionEvent)
        and event.data.phase == "plan"
        and event.data.seat == view.seat
        for event in view.log
    )


def _decided(view: SeatView) -> set[int]:
    """The lots whose hammer has fallen."""
    return {e.lot for e in view.log if isinstance(e, HammerEvent) and e.lot is not None}


def _live_pledges(view: SeatView, lot: int) -> list[tuple[TalkEvent, Pledge]]:
    """Pledges about the lot in progress or a later lot whose hammer has not fallen, whoever made
    them. They come in the order of the lots they are about, and within a lot in the order they
    were made. A pledge about a lot that is decided or past is over and is not listed."""
    decided = _decided(view)
    found = [
        (event, event.data.pledge)
        for event in view.log
        if isinstance(event, TalkEvent)
        and event.data.pledge is not None
        and event.data.pledge.lot >= lot
        and event.data.pledge.lot not in decided
    ]
    return sorted(found, key=lambda item: (item[1].lot, item[0].seq))


def _memory_block(view: SeatView, texts: TemplateSet, lot: int) -> str:
    """The section of ``night.memory = "plan_pledges"``: what the seat planned for the lot in
    progress, how much of the plan is still to be spent, and the pledges in force.

    The plan is the seat's own and nobody else's. The pledges were made in public, so every seat
    may be told all of them, and each is listed with who made it. The part of the plan that is
    still to be spent is the sum of the planned amounts of the lots whose hammer has not fallen,
    the lot in progress included. A seat that was asked for a plan and handed in none is told so.
    A seat that was never asked, in a night without the plan phase, is told nothing about a plan.
    """
    lines = [texts.get("observe.memory_head")]
    plan = _own_plan(view)
    if plan is None:
        if _was_asked_for_a_plan(view):
            lines.append(texts.get("observe.memory_plan_none"))
    elif lot <= len(plan.priorities):
        decided = _decided(view)
        ahead = [n for n in sorted(view.lots) if n not in decided and n <= len(plan.budget_plan)]
        lines.append(
            texts.format(
                "observe.memory_plan",
                lot=lot,
                priority=plan.priorities[lot - 1],
                budget=plan.budget_plan[lot - 1],
            )
        )
        lines.append(
            texts.format(
                "observe.memory_plan_left", total=sum(plan.budget_plan[n - 1] for n in ahead)
            )
        )
    lines.append(texts.get("observe.memory_pledges_head"))
    pledges = [
        texts.format(
            "observe.memory_pledge",
            seat=event.data.seat,
            codename=view.codename(event.data.seat),
            text=_pledge_text(texts, pledge),
        )
        for event, pledge in _live_pledges(view, lot)
    ]
    lines.extend(pledges or [texts.get("observe.none")])
    return "\n".join(lines)


@dataclass(frozen=True)
class EnglishState:
    """Where an English lot stands for one seat: who left, the price, who is still in."""

    dropped: list[EnglishDropoutEvent]  # participants that left, in order
    price: int  # the current price
    remaining: list[int]  # seats still in
    mine: int  # the seat's own latest exit price


def english_state(view: SeatView, lot_open: LotOpenEvent) -> EnglishState:
    """The standing of the English lot ``lot_open`` for the seat of ``view``."""
    events = view.lot_events(lot_open.lot or 0)
    drops = [e for e in events if isinstance(e, EnglishDropoutEvent)]
    dropped = [e for e in drops if e.data.participated]
    absent = {e.data.seat for e in drops if not e.data.participated}
    mine: int | None = None
    for event in events:
        if not isinstance(event, ActionEvent) or event.data.seat != view.seat:
            continue
        if event.data.phase in ("english_init", "english_revise") and event.data.value is not None:
            mine = event.data.value
    if mine is None:
        raise RenderError(f"seat {view.seat} has no exit price on lot {lot_open.lot}")
    if dropped:
        return EnglishState(dropped, dropped[-1].data.price, list(dropped[-1].data.remaining), mine)
    remaining = [seat for seat in lot_open.data.active if seat not in absent]
    return EnglishState([], lot_open.data.reserve, remaining, mine)


def _english_block(texts: TemplateSet, state: EnglishState) -> str:
    items = [
        texts.format("observe.dropped_item", seat=e.data.seat, price=e.data.price)
        for e in state.dropped
    ]
    body = texts.format(
        "observe.inprogress_english",
        dropped="、".join(items) or texts.get("observe.dropped_none"),
        price=state.price,
        present=_seat_list(state.remaining),
        mine=state.mine,
    )
    return "\n".join([texts.get("observe.inprogress_head"), body])


def _instruction(
    view: SeatView, phase: Phase, settings: RenderSettings, english: EnglishState | None
) -> str:
    texts = settings.templates
    limits = settings.limits
    if phase == "plan":
        return texts.format("phase.plan", n_lots=view.night.data.n_lots, persona_max=limits.persona)
    if phase == "talk":
        key = "phase.talk" if settings.mindreading else "phase.talk_without_guess"
        return texts.format(key, say_max=limits.say)
    if phase == "english_revise" and english is not None:
        lead = ""
        if english.dropped:
            last = english.dropped[-1].data
            lead = texts.format("phase.english_revise_lead", seat=last.seat, price=last.price)
        return texts.format(
            "phase.english_revise",
            lead=lead,
            price=english.price,
            remaining=len(english.remaining),
            seats=_seat_list(english.remaining),
            mine=english.mine,
        )
    return texts.format(PHASE_TABLE[phase].instruction_key)


def _standing(view: SeatView, texts: TemplateSet, lot_open: LotOpenEvent, plan: LotPlan) -> str:
    lot = plan.lot
    header = texts.format(
        "observe.lot_header",
        lot=lot,
        n_lots=view.night.data.n_lots,
        emoji=clean_label(plan.emoji).text,
        name=clean_text(plan.name, None).text,
        tag=clean_label(plan.tag).text,
        tier=lot_open.data.tier,
        rule=_rule_title(lot_open.data.format),
        reserve=lot_open.data.reserve,
    )
    status = texts.format(
        "observe.status_signal" if plan.format == "blind_box" else "observe.status_value",
        value=_own_number(view, lot),
        left=view.left,
        present=len(view.seats) - len(view.out),
        out=_seat_list(view.out, texts.get("observe.out_none")),
    )
    won = [
        texts.format(
            "observe.won_item",
            lot=won_lot,
            emoji=clean_label(view.lots[won_lot].emoji).text,
            name=clean_text(view.lots[won_lot].name, None).text,
            price=price,
        )
        for won_lot, price in view.won
    ]
    line = texts.format("observe.won_line", won="、".join(won) or texts.get("observe.won_none"))
    progress = _progress_text(view, texts)
    if progress is not None:
        line += "\u3000" + progress
    return "\n".join([header, status, line])


def _mindread(view: SeatView, texts: TemplateSet, target: int | None) -> str:
    if target is None:
        raise RenderError("the talk phase needs guess_target while mind reading is on")
    if target == view.seat or target not in view.seats:
        raise RenderError(f"seat {target} cannot be the guess target of seat {view.seat}")
    return texts.format("observe.mindread", seat=target, codename=view.codename(target))


def render_observation(
    events: Iterable[AnyEvent],
    seat: int,
    phase: Phase,
    settings: RenderSettings,
    *,
    guess_target: int | None = None,
    as_of_seq: int | None = None,
) -> str:
    """The user message of one call: the situation of ``seat`` and what it must do now.

    ``guess_target`` is the opponent whose remaining budget the seat is asked to guess. It is
    required for the ``talk`` phase while mind reading is on, and is ignored otherwise.
    """
    if phase not in PHASE_TABLE:
        raise RenderError(f"unknown phase {phase!r}")
    view = build_view(events, seat, as_of_seq)
    texts = settings.templates
    sections: list[str] = []
    english: EnglishState | None = None

    if phase == "plan":
        sections.append(texts.get("observe.plan_intro"))
    else:
        lot_open = view.current
        if lot_open is None or lot_open.lot is None:
            raise RenderError(f"phase {phase!r} needs a lot in progress")
        plan = view.lots.get(lot_open.lot)
        if plan is None:
            raise RenderError(f"lot {lot_open.lot} is not on the lot list")
        if phase in BID_PHASES:
            fmt = lot_open.data.format
            mechanism = settings.mechanisms.get(fmt) or MECHANISMS[fmt]
            if phase not in mechanism.bid_phases:
                raise RenderError(f"phase {phase!r} is not played in a {fmt} lot")
        sections.append(_standing(view, texts, lot_open, plan))
        if phase == "english_revise":
            english = english_state(view, lot_open)

        finished = [
            line
            for lot in sorted(view.lots)
            if lot != lot_open.lot and (line := _result_line(view, lot, texts)) is not None
        ]
        sections.append(
            "\n".join(
                [texts.get("observe.results_head"), *(finished or [texts.get("observe.none")])]
            )
        )
        chat_head = texts.format("observe.chat_head", window=_cn_count(settings.chat_window_lots))
        chat = _chat_lines(view, settings)
        sections.append("\n".join([chat_head, *(chat or [texts.get("observe.none")])]))
        if settings.thought_keep_last > 0:
            head = texts.format(
                "observe.thoughts_head", count=_cn_count(settings.thought_keep_last)
            )
            thoughts = " / ".join(quote_literal(t) for t in _own_thoughts(view, settings))
            sections.append("\n".join([head, thoughts or texts.get("observe.none")]))
        if settings.memory == "plan_pledges":
            sections.append(_memory_block(view, texts, plan.lot))
        if english is not None:
            sections.append(_english_block(texts, english))

    if phase == "talk" and settings.mindreading:
        sections.append(_mindread(view, texts, guess_target))
    instruction = _instruction(view, phase, settings, english)
    sections.append(texts.format("observe.todo", phase=phase, instruction=instruction))
    return "\n\n".join(sections)


def render_prompt(
    events: Iterable[AnyEvent],
    seat: int,
    phase: Phase,
    settings: RenderSettings,
    *,
    persona: Persona | None = None,
    guess_target: int | None = None,
    as_of_seq: int | None = None,
) -> RenderedPrompt:
    """System prompt and observation of one call."""
    log = list(events)
    return RenderedPrompt(
        system=render_system(log, seat, settings, persona=persona),
        observation=render_observation(
            log, seat, phase, settings, guess_target=guess_target, as_of_seq=as_of_seq
        ),
    )


def parse_context(
    events: Iterable[AnyEvent],
    seat: int,
    phase: Phase,
    settings: RenderSettings,
    *,
    as_of_seq: int | None = None,
) -> ParseContext:
    """The context the parsing ladder needs for one call, derived from the same visible events.

    Bid phases get the remaining budget (a bid above it is a legal clamp), an English revision
    also gets the current price (an exit price below it means leaving), and every phase gets the
    ranges that the phase table checks.
    """
    view = build_view(events, seat, as_of_seq)
    lot = view.current.lot if view.current is not None and phase != "plan" else None
    act = ActContext(
        n_lots=view.night.data.n_lots, n_seats=len(view.seats), self_seat=seat, current_lot=lot
    )
    price = None
    if phase == "english_revise" and view.current is not None:
        price = english_state(view, view.current).price
    return ParseContext(
        phase,
        act=act,
        budget_left=view.left if phase in BID_PHASES else None,
        current_price=price,
        limits=settings.limits,
    )
