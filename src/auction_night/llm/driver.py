"""The night driver: the engine's state machine, played by models and rule bots.

:class:`EngineDriver` plugs the pure state machine of :mod:`auction_night.engine.night` into the
runner of :mod:`auction_night.llm.orchestrate`. It implements the runner's ``NightDriver``
protocol, so ``ModelRunner.run_night(driver, session)`` plays a whole night::

    driver = EngineDriver(config, the_deal, routes)
    run = await runner.run_night(driver, session)
    result = driver.finish(run)

What the driver does for each request of the engine:

1. **Bots** answer on the spot (``make_bot(...).answer(request)``). A request that asks bots
   only never reaches the runner.
   A **human** seat answers on the spot too, through a :class:`~auction_night.llm.human.HumanPlayer`
   that the caller supplies: the person is shown the observation a model at that seat would be
   given (the same renderer, the same settings) and answers with numbers and keys, so a request
   that asks only bots and a person never reaches the runner either.
2. **Model seats** (``llm`` and ``mock``) get a :class:`~auction_night.llm.orchestrate.ModelCall`:
   the prompt is rendered from the events that seat may see (public ones and its own), the request
   is built for the seat's endpoint, and the parse context comes from the same events. The key of
   the call is the night, the lot, the phase, the position of the request in the night (the
   engine's ``Request.index``) and the seat. The talk phase asks one speaker at a time, in
   sequence, and a bid phase asks all of its model seats at once.
3. The runner returns one outcome per seat. A valid ``act`` call becomes an
   :class:`~auction_night.engine.night.Answer` (the cleaned text, the counts of what cleaning
   removed, the number of attempts and the call id go with it), and an abstention becomes an
   abstaining answer with its reason. The system events of the phase (``parse_repair`` and
   ``cost_event``) are numbered into the log with ``append_events`` *before* the engine takes the
   answers, so a repair stands next to the action it belongs to.

The closing tally of costs comes from the runner after the last phase and does not pass through
``submit``: :meth:`EngineDriver.finish` appends it and hands back the log, the scores and the
missions.

Two checks keep the layers honest. Before a call is made, the budget and the private number that
the rendered prompt will show are compared with the facts the engine holds, so a prompt can never
quietly disagree with the game. And a model's valid action that the engine still cannot use is
turned into an abstention with a ``parse_repair`` event instead of ending a paid night.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from auction_night.bots import RuleBot, make_bot
from auction_night.contracts.config import EndpointConfig, ModelFields, NightConfig
from auction_night.contracts.events import (
    AnyEvent,
    MissionResultData,
    NightEndEvent,
    NightStatus,
    ParseRepairData,
    SeatScore,
    dumps_events,
    validate_log,
)
from auction_night.engine.deal import Deal
from auction_night.engine.formats.dutch import dutch_ladder
from auction_night.engine.night import (
    Answer,
    AnswerError,
    ItemInfo,
    NightState,
    Request,
    append_events,
    start,
    step,
)
from auction_night.engine.rules_text import common_rule_text, rule_with_example
from auction_night.llm.cassette import CallKey
from auction_night.llm.human import HumanPlayer, HumanTurn
from auction_night.llm.orchestrate import (
    ModelCall,
    NightRun,
    PhaseRequest,
    PhaseResult,
    SeatOutcome,
    SystemEvent,
)
from auction_night.llm.render import (
    Persona,
    RenderSettings,
    build_view,
    english_state,
    parse_context,
    render_observation,
    render_system,
)
from auction_night.llm.request import build_request, user_message

__all__ = [
    "DriverError",
    "EngineDriver",
    "NightResult",
    "SeatRoute",
    "llm_routes",
]


class DriverError(RuntimeError):
    """The driver cannot play the night as configured, or the layers disagree about the game."""


@dataclass(frozen=True)
class SeatRoute:
    """Where the calls of one model seat go.

    ``name`` is the endpoint's name in the session and the runner (it selects the client and the
    rate limiter), ``endpoint`` its configuration and ``ref`` the model fields of the seat
    (model, thinking, effort, token limit and sampling).
    """

    name: str
    endpoint: EndpointConfig
    ref: ModelFields


def llm_routes(config: NightConfig) -> dict[int, SeatRoute]:
    """The routes of the ``llm`` seats: each seat's own endpoint, with the seat as model fields."""
    routes: dict[int, SeatRoute] = {}
    for seat in config.seats:
        if seat.kind == "llm":
            assert seat.endpoint is not None
            routes[seat.seat] = SeatRoute(seat.endpoint, config.endpoints[seat.endpoint], seat)
    return routes


@dataclass(frozen=True)
class NightResult:
    """A night that was played to the end.

    ``events`` is the whole log, system events included, ending with the closing cost tally.
    ``status`` is the one the engine wrote into ``night_end``. ``run`` is the model layer's
    account of the night: its status, counters and cost summary.
    """

    night: str
    status: NightStatus
    events: tuple[AnyEvent, ...]
    scores: tuple[SeatScore, ...]
    missions: tuple[MissionResultData, ...]
    run: NightRun

    def events_text(self) -> str:
        """The log as canonical JSON lines, the content of ``events.jsonl``."""
        return dumps_events(self.events)


class EngineDriver:
    """Plays the night of ``config`` dealt as ``the_deal``: see the module text.

    ``routes`` maps every model seat (``llm`` or ``mock``) to its :class:`SeatRoute`. ``items``
    names the lots and ``talk_offset`` moves the start of the speaking order, as in
    :func:`~auction_night.engine.night.start`. ``personas`` maps the persona keys of a variety
    night to their texts. ``humans`` maps every ``human`` seat to the person (or the record of the
    person) that answers for it. ``settings`` are the render settings; by default they follow the
    configuration and use the generated rule texts. ``cap_reached`` turns True when the cost gate
    has fired during the night.
    """

    def __init__(
        self,
        config: NightConfig,
        the_deal: Deal,
        routes: Mapping[int, SeatRoute],
        *,
        items: list[ItemInfo] | tuple[ItemInfo, ...] | None = None,
        talk_offset: int = 0,
        personas: Mapping[str, Persona] | None = None,
        humans: Mapping[int, HumanPlayer] | None = None,
        settings: RenderSettings | None = None,
    ) -> None:
        self.config = config
        self.night = config.night.id
        self.deal = the_deal
        self.settings = settings or RenderSettings.from_config(
            config, rule_with_example, common_rules=common_rule_text
        )
        self._routes = dict(routes)
        self._humans = dict(humans or {})
        self._personas = dict(personas or {})
        self._seats = {seat.seat: seat for seat in config.seats}
        self._bots: dict[int, RuleBot] = {
            seat.seat: make_bot(seat) for seat in config.seats if seat.kind == "bot"
        }
        self._check_table()
        state, request = start(config, the_deal, items=items, talk_offset=talk_offset)
        self.state: NightState = state
        self._request: Request | None = request
        self._systems: dict[int, str] = {}
        self._calls: tuple[ModelCall, ...] | None = None
        self._local: dict[int, Answer] = {}
        self._system_events: list[SystemEvent] = []
        self._result: NightResult | None = None
        self.cap_reached = False

    # ----------------------------------------------------------------------- the setup

    def _check_table(self) -> None:
        for seat in self.config.seats:
            if seat.kind == "human" and seat.seat not in self._humans:
                raise DriverError(
                    f"seat {seat.seat} is a human seat, which this driver cannot play"
                )
            if seat.kind != "human" and seat.seat in self._humans:
                raise DriverError(f"seat {seat.seat} is not a human seat in the configuration")
            if seat.kind in ("llm", "mock"):
                if seat.seat not in self._routes:
                    raise DriverError(f"seat {seat.seat} has no route to a model")
                if seat.persona is not None and seat.persona not in self._personas:
                    known = ", ".join(self._personas) or "none"
                    raise DriverError(
                        f"seat {seat.seat}: persona {seat.persona!r} is not known (known: {known})"
                    )

    # ------------------------------------------------------------------------- the state

    @property
    def request(self) -> Request | None:
        """The engine request that waits for answers, None once the night is over."""
        return self._request

    @property
    def events(self) -> tuple[AnyEvent, ...]:
        """The log so far."""
        return self.state.events

    @property
    def done(self) -> bool:
        """True once the engine has written the final scores."""
        return self.state.done

    # ----------------------------------------------------------------- the runner's side

    def next_phase(self) -> PhaseRequest | None:
        """The model calls of the next phase, or None when the night is over."""
        if self._calls is not None:
            raise DriverError("the answers of the last phase have not been submitted")
        while self._request is not None:
            request = self._request
            local: dict[int, Answer] = {}
            calls: list[ModelCall] = []
            for seat in request.seats:
                if seat in self._bots:
                    local[seat] = self._bots[seat].answer(request)
                elif seat in self._humans:
                    local[seat] = self._ask_human(request, seat)
                elif seat in self._routes:
                    calls.append(self._call(request, seat))
                else:
                    raise DriverError(f"seat {seat} is asked, and nobody plays it")
            if calls:
                self._local = local
                self._calls = tuple(calls)
                return PhaseRequest(self._calls, sequential=request.phase == "talk")
            self._take(local, ())
        return None

    def submit(self, result: PhaseResult) -> None:
        """Take the outcomes of the phase that :meth:`next_phase` returned last."""
        calls = self._calls
        if calls is None:
            raise DriverError("no phase is waiting for answers")
        asked = sorted(call.seat for call in calls if call.seat is not None)
        given = sorted(outcome.seat for outcome in result.outcomes if outcome.seat is not None)
        if asked != given:
            raise DriverError(f"the phase asked seats {asked} and was answered for {given}")
        answers = dict(self._local)
        extra: list[SystemEvent] = []
        for outcome in result.outcomes:
            assert outcome.seat is not None
            answers[outcome.seat] = self._answer_of(outcome, extra)
        self.cap_reached = self.cap_reached or result.cap_reached
        self._calls = None
        self._local = {}
        self._system_events.extend(result.events)
        self._take(answers, (*result.events, *extra))

    def _take(self, answers: Mapping[int, Answer], system: Sequence[SystemEvent]) -> None:
        """Number the system events into the log, then let the engine take the answers."""
        state = self.state
        if system:
            state, _ = append_events(state, [event.to_event(1) for event in system])
        state, _, self._request = step(state, answers)
        self.state = state

    # ------------------------------------------------------------------------- one call

    def _system(self, seat: int) -> str:
        """The system prompt of a seat. It only depends on the opening events, so it is
        rendered once and then reused for every call of the night."""
        text = self._systems.get(seat)
        if text is None:
            name = self._seats[seat].persona
            persona = self._personas[name] if name is not None else None
            text = render_system(self.state.events, seat, self.settings, persona=persona)
            self._systems[seat] = text
        return text

    def _check_view(self, seat: int, request: Request) -> None:
        """What the prompt will show must be what the engine holds: the budget, the private
        number of the lot, and in an English revision the exit price in force."""
        facts = request.facts_of(seat)
        view = build_view(self.state.events, seat)
        if view.left != facts.budget_left:
            raise DriverError(
                f"seat {seat}: the prompt shows {view.left} coins left, the engine has "
                f"{facts.budget_left}"
            )
        lot = request.lot
        if lot is None:
            return
        blind = facts.signal is not None
        table = view.deal.data.signals if blind else view.deal.data.values
        if table.get(lot) != (facts.signal if blind else facts.value):
            raise DriverError(f"seat {seat}: the prompt and the engine disagree on lot {lot}")
        current = view.current
        if request.phase == "english_revise" and current is not None:
            mine = english_state(view, current).mine
            if mine != facts.exit_price:
                raise DriverError(
                    f"seat {seat}: the prompt and the engine disagree on the exit price"
                )

    def _observation(self, request: Request, seat: int) -> str:
        """The user message of a seat for the pending request: what a model is given, and what a
        person at the same seat is shown."""
        facts = request.facts_of(seat)
        settings = self.settings
        if request.phase == "talk" and facts.guess_target is None and settings.mindreading:
            # Nobody is left to read, so the seat is asked for its line alone.
            settings = replace(settings, mindreading=False)
        return render_observation(
            self.state.events, seat, request.phase, settings, guess_target=facts.guess_target
        )

    def _call(self, request: Request, seat: int) -> ModelCall:
        route = self._routes[seat]
        events = self.state.events
        self._check_view(seat, request)
        observation = self._observation(request, seat)
        built = build_request(
            route.endpoint,
            route.ref,
            system=self._system(seat),
            messages=[user_message(observation)],
        )
        return ModelCall(
            key=CallKey(self.night, request.lot, request.phase, request.index, seat),
            endpoint=route.name,
            request=built,
            ctx=parse_context(events, seat, request.phase, self.settings),
            native=route.endpoint.structured_output,
        )

    def _ask_human(self, request: Request, seat: int) -> Answer:
        """Show a request to the person at ``seat`` and take the answer.

        The turn holds the seat's own numbers and the public facts of the request, never the
        request itself: in a phase that asks several seats it carries the private numbers of all
        of them.
        """
        self._check_view(seat, request)
        facts = request.facts_of(seat)
        turn = HumanTurn(
            seat=seat,
            codename=self._seats[seat].codename,
            index=request.index,
            lot=request.lot,
            ctx=parse_context(self.state.events, seat, request.phase, self.settings),
            observation=self._observation(request, seat),
            system=self._system(seat),
            reserve=request.reserve,
            own_exit=facts.exit_price,
            guess_target=facts.guess_target,
            ladder=self._ladder(request),
        )
        return self._humans[seat].answer(turn)

    def _ladder(self, request: Request) -> tuple[int, ...]:
        """The prices of the levels of a Dutch lot, highest first (empty for other phases)."""
        if request.phase != "dutch":
            return ()
        assert request.tier is not None
        mechanism = self.config.night.mechanism_for("dutch")
        assert mechanism.levels is not None
        return dutch_ladder(request.tier, mechanism.levels, mechanism.reserve)

    # ------------------------------------------------------------------- one outcome

    @staticmethod
    def _answer_of(outcome: SeatOutcome, extra: list[SystemEvent]) -> Answer:
        """The engine's answer for one outcome. An action the engine cannot use is an abstention
        that leaves a ``parse_repair`` event behind."""
        seat = outcome.seat
        assert seat is not None
        if outcome.ok:
            assert outcome.args is not None
            try:
                return Answer.from_act(
                    seat,
                    outcome.args,
                    sanitized_chars=outcome.sanitized_chars,
                    text_sanitized_chars=outcome.text_sanitized_chars,
                    attempts=outcome.action_attempts,
                    call_id=outcome.call_id,
                )
            except AnswerError as exc:
                data = ParseRepairData(
                    role=outcome.role,
                    seat=seat,
                    phase=outcome.key.phase,
                    attempt=min(10, outcome.action_attempts),
                    codes=["invalid_input", "fallback"],
                    status="abstain",
                    detail=f"answer_error={exc}",
                    call_id=outcome.call_id,
                )
                extra.append(SystemEvent("parse_repair", outcome.key.lot, data))
                return Answer.abstained(
                    seat,
                    "parse_failed",
                    attempts=outcome.action_attempts,
                    call_id=outcome.call_id,
                )
        return Answer.abstained(
            seat,
            outcome.reason or "missing",
            attempts=outcome.action_attempts,
            call_id=outcome.call_id,
        )

    # ------------------------------------------------------------------------ the end

    def finish(self, run: NightRun) -> NightResult:
        """Close the log with the runner's final cost tally and hand back the result.

        ``run`` is what ``ModelRunner.run_night`` returned for this driver. Its system events
        must be the ones this driver has been given, followed by the closing tally.
        """
        if self._result is not None:
            if run is not self._result.run:
                raise DriverError("the night has been closed with another run")
            return self._result
        if not self.done or self._request is not None:
            raise DriverError("the night is not over")
        if run.night != self.night:
            raise DriverError(f"the run belongs to night {run.night!r}")
        if not run.events:
            raise DriverError("the run has no closing cost tally")
        *given, tally = run.events
        if (
            tally.type != "cost_event"
            or getattr(tally.data, "kind", None) != "tally"
            or tuple(given) != tuple(self._system_events)
        ):
            raise DriverError("the run does not match what this driver submitted")
        state, _ = append_events(self.state, [tally.to_event(1)])
        self.state = state
        problems = validate_log(state.events)
        if problems:
            raise DriverError("the log is not valid: " + "; ".join(problems))
        end = next(e for e in reversed(state.events) if isinstance(e, NightEndEvent))
        self._result = NightResult(
            night=self.night,
            status=end.data.status,
            events=state.events,
            scores=state.scores,
            missions=state.missions,
            run=run,
        )
        return self._result
