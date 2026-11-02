"""Prices, the cost of a call, the running totals of a night, the cost gate and the estimate.

Prices
------
A price list is a dated YAML file (``configs/prices.example.yaml``): dollars per million tokens
for input, output, cache reads and cache writes, with the date and the source of every number.
Nothing here knows a price by heart. A model that is not in the list has no price, and nothing is
made up for it.

A price may be in another currency (``currency: CNY``, as in ``configs/prices.bailian.yaml``). The
list then carries the exchange rate of that currency, with its date and source, and the price is
divided by the rate when the cost of a call is worked out, so the ledger and the cost cap stay in
dollars. A list that uses a currency without giving its rate is refused, and no rate is assumed.

The cost of a call
------------------
In micro-dollars (``cost_microusd``), which is exactly ``tokens x price per million tokens``:

    input x P_in + cache_read x P_cache_read + cache_write x P_cache_write + output x P_out

A cache price that the list leaves out is the input price. A price in another currency is divided
by the units of that currency to the dollar, in exact decimal arithmetic, and the call is rounded
up afterwards, so the conversion never lowers a cost. Reasoning tokens are *part of*
``output_tokens`` (both APIs count them in the output total that is billed), so they are priced
as output through ``output_tokens`` and are not added a second time. A call is rounded up to a
whole micro-dollar, so a tally never undercounts.

The cost gate
-------------
A :class:`Ledger` keeps per-seat and per-night totals. When the night has a cap, the gate fires
once, at the first check where the total has reached the cap. It emits one ``cost_event``
(``cap_reached``), and from then on every model action is an abstention with the reason
``budget_aborted``. The orchestrator checks the gate before each phase and after the costs of a
phase (or of one sequential call) have been added, never in the middle of a concurrent phase, so
where the gate fires does not depend on the order in which replies arrived. The overshoot is at
most one phase. A night is ``budget_aborted`` when the gate fired *and* an action was skipped
because of it. A cap on a model that has no price cannot be enforced, so :meth:`Ledger.
require_prices` refuses to start such a night.

The estimate
------------
:func:`estimate_night` turns a configuration into calls and tokens per model seat and prices them.
The number of calls follows the format mix: one plan call, one talk call per lot, one bid per
sealed, Dutch and English lot, and the revision rounds of the English lots (about 27.25 calls
per seat for the default twelve-lot night). Token sizes are the averages of a measured
twelve-lot night, listed in :class:`EstimateAssumptions`. The high estimate adds thinking tokens
and a tokenizer factor for Anthropic models.

The show layer is estimated the way it is called after the night (see
:mod:`auction_night.show.showrun`), for the roles that are models: the host once for each lot and
once for the recap, each judge once (twice in the high estimate, for the second try that an
unreadable first answer earns) and the content classifier once for each batch of up to 40
texts. The number of texts grows with the model seats and their calls. Its prompts are billed as
plain input, with no cache discount. A rewrite of a host text, an unreadable reply of the
classifier and the retries after an error are not counted. The cap holds for the seats (``run``)
and for the show layer (``show``), each on its own. The verdict of the report compares their sum
with it, which is the stricter check.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Final, Literal

from auction_night.contracts.config import (
    ExchangeRate,
    ModelPrice,
    NightConfig,
    PricesFile,
    RoleConfig,
    find_placeholders,
    load_prices,
)
from auction_night.contracts.events import CostEventData, NightStatus
from auction_night.llm.cassette import CallKey
from auction_night.llm.client import Usage

MICRO: Final = Decimal(1_000_000)


class MissingPriceError(LookupError):
    """A model has no price although the night has a cost cap."""


class ExchangeRateError(ValueError):
    """A price is in a currency whose exchange rate is not known. No rate is ever assumed."""


def _dec(value: float | int) -> Decimal:
    return Decimal(str(value))


# ----------------------------------------------------------------------------- prices


@dataclass(frozen=True)
class PriceTable:
    """The price list, keyed by model id, with the exchange rates of its currencies.

    ``prices`` holds the prices as the list writes them. A price in dollars is used as it is. A
    price in another currency needs the rate of that currency in ``rates`` (units of the currency
    to one dollar), and a table that has such a price without its rate cannot be made.
    """

    prices: Mapping[str, ModelPrice] = field(default_factory=dict)
    source: str | None = None
    rates: Mapping[str, ExchangeRate] = field(default_factory=dict)

    def __post_init__(self) -> None:
        used = {price.currency for price in self.prices.values()} - {"USD"}
        missing = sorted(used - set(self.rates))
        if missing:
            where = f" in {self.source}" if self.source else ""
            raise ExchangeRateError(
                f"no exchange rate{where} for {', '.join(missing)}: a price in that currency "
                "cannot be counted in dollars, and no rate is assumed"
            )

    @classmethod
    def from_file(cls, path: str | Path) -> "PriceTable":
        """Read and validate a price list."""
        return cls.from_prices(load_prices(path), source=str(path))

    @classmethod
    def from_prices(cls, prices: PricesFile, *, source: str | None = None) -> "PriceTable":
        return cls(dict(prices.prices), source, dict(prices.exchange_rates))

    def units_per_usd(self, price: ModelPrice) -> Decimal | None:
        """The units of the currency of ``price`` that one dollar buys, or None for a dollar price.

        Pass the result to the cost functions. A price whose currency has no rate in the table
        raises :class:`ExchangeRateError`: nothing is guessed.
        """
        if price.currency == "USD":
            return None
        rate = self.rates.get(price.currency)
        if rate is None:
            raise ExchangeRateError(f"no exchange rate for {price.currency}")
        return _dec(rate.units_per_usd)

    def lookup(self, *models: str | None) -> tuple[str, ModelPrice] | None:
        """The first of ``models`` that has a price, with the id it was found under."""
        for model in models:
            if model is not None and model in self.prices:
                return model, self.prices[model]
        return None

    def missing(self, models: Iterable[str]) -> list[str]:
        """The models without a price, sorted and without repeats."""
        return sorted({m for m in models if m not in self.prices})


def _price_in_microusd(
    price: ModelPrice, units_per_usd: Decimal | None = None
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Input, output, cache read and cache write price in micro-dollars per token.

    A missing cache price is the input price. A price in another currency is divided by
    ``units_per_usd``, the units of that currency to the dollar, and without it the price is
    refused: the currency is never taken for dollars.
    """
    input_price = _dec(price.input_per_mtok)
    read = input_price if price.cache_read_per_mtok is None else _dec(price.cache_read_per_mtok)
    write = input_price if price.cache_write_per_mtok is None else _dec(price.cache_write_per_mtok)
    output_price = _dec(price.output_per_mtok)
    if price.currency == "USD":
        return input_price, output_price, read, write
    if units_per_usd is None:
        raise ExchangeRateError(
            f"a price in {price.currency} cannot be counted in dollars without its exchange rate"
        )
    rate = units_per_usd
    return input_price / rate, output_price / rate, read / rate, write / rate


def exact_cost_microusd(
    price: ModelPrice, usage: Usage, units_per_usd: Decimal | None = None
) -> Decimal:
    """The cost of ``usage`` in micro-dollars, not yet rounded.

    A price in another currency needs ``units_per_usd`` (see :meth:`PriceTable.units_per_usd`).
    """
    p_in, p_out, p_read, p_write = _price_in_microusd(price, units_per_usd)
    return (
        Decimal(usage.input_tokens) * p_in
        + Decimal(usage.output_tokens) * p_out
        + Decimal(usage.cache_read_input_tokens) * p_read
        + Decimal(usage.cache_write_input_tokens) * p_write
    )


def call_cost_microusd(
    price: ModelPrice, usage: Usage, units_per_usd: Decimal | None = None
) -> int:
    """The cost of one call in whole micro-dollars, rounded up."""
    cost = exact_cost_microusd(price, usage, units_per_usd)
    return int(cost.to_integral_value(rounding=ROUND_CEILING))


# ------------------------------------------------------------------------------ ledger


@dataclass
class Tally:
    """Totals over some calls. ``calls`` counts attempts, failed ones included."""

    calls: int = 0
    unpriced_calls: int = 0
    unmetered_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    reasoning_tokens: int = 0
    cost_microusd: int = 0

    def add(self, usage: Usage | None, cost: int | None) -> None:
        self.calls += 1
        if usage is None:
            self.unmetered_calls += 1
        else:
            self.input_tokens += usage.input_tokens
            self.output_tokens += usage.output_tokens
            self.cache_read_input_tokens += usage.cache_read_input_tokens
            self.cache_write_input_tokens += usage.cache_write_input_tokens
            self.reasoning_tokens += usage.reasoning_tokens or 0
        if cost is None:
            if usage is not None:
                self.unpriced_calls += 1
        else:
            self.cost_microusd += cost

    def as_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True)
class Charge:
    """The cost of one attempt, waiting to be added to the ledger.

    ``cost_microusd`` is None when the model has no price. ``label`` is the role, for example
    ``seat:3``.
    """

    key: CallKey
    label: str
    usage: Usage | None
    cost_microusd: int | None


class Ledger:
    """Running totals of one night, and the cost gate."""

    def __init__(
        self, prices: PriceTable | None = None, *, cap_microusd: int | None = None
    ) -> None:
        if cap_microusd is not None and cap_microusd < 0:
            raise ValueError("the cap cannot be negative")
        self.prices = prices or PriceTable()
        self.cap_microusd = cap_microusd
        self.total = Tally()
        self.by_label: dict[str, Tally] = {}
        self.aborted_actions = 0
        self._fired = False

    # --------------------------------------------------------------------------- costs

    def cost_of(self, usage: Usage | None, *models: str | None) -> int | None:
        """The cost of ``usage`` at the price of the first of ``models`` that has one.

        None when no model has a price, or when there is no usage to price. Pass the model id
        the API returned first and the requested one second.
        """
        if usage is None:
            return None
        found = self.prices.lookup(*models)
        if found is None:
            return None
        price = found[1]
        return call_cost_microusd(price, usage, self.prices.units_per_usd(price))

    def require_prices(self, models: Iterable[str]) -> None:
        """Refuse a night that has a cap and a model without a price."""
        if self.cap_microusd is None:
            return
        missing = self.prices.missing(models)
        if missing:
            where = f" in {self.prices.source}" if self.prices.source else ""
            raise MissingPriceError(
                f"the cost cap cannot be enforced: no price{where} for " + ", ".join(missing)
            )

    # ------------------------------------------------------------------------- totals

    def fold(self, charges: Iterable[Charge]) -> None:
        """Add the cost of some attempts, in key order, so the result never depends on arrival."""
        for charge in sorted(charges, key=lambda c: c.key.sort_key):
            self.total.add(charge.usage, charge.cost_microusd)
            self.by_label.setdefault(charge.label, Tally()).add(charge.usage, charge.cost_microusd)

    @property
    def spent_microusd(self) -> int:
        return self.total.cost_microusd

    @property
    def calls(self) -> int:
        return self.total.calls

    @property
    def cap_reached(self) -> bool:
        """True when the total has reached the cap (a cap of zero is reached from the start)."""
        return self.cap_microusd is not None and self.spent_microusd >= self.cap_microusd

    # --------------------------------------------------------------------------- gate

    def gate(self) -> CostEventData | None:
        """Check the cap. Returns the ``cap_reached`` event the first time it is reached."""
        if self._fired or not self.cap_reached:
            return None
        self._fired = True
        return CostEventData(
            kind="cap_reached",
            spent_microusd=self.spent_microusd,
            cap_microusd=self.cap_microusd,
            calls=self.calls,
        )

    @property
    def blocked(self) -> bool:
        """True once the gate has fired: every model action is now an abstention."""
        return self._fired

    def note_aborted(self, actions: int = 1) -> None:
        """Count model actions that were skipped because the gate had fired."""
        self.aborted_actions += actions

    @property
    def status(self) -> NightStatus:
        """``budget_aborted`` when the gate fired and an action was skipped because of it."""
        return "budget_aborted" if self.blocked and self.aborted_actions else "completed"

    def tally_event(self, role: str | None = None) -> CostEventData:
        """A ``tally`` event: the total of the night, or of one role such as ``seat:3``."""
        tally = self.total if role is None else self.by_label.get(role, Tally())
        return CostEventData(
            kind="tally",
            spent_microusd=tally.cost_microusd,
            cap_microusd=self.cap_microusd,
            calls=tally.calls,
            role=role,
        )

    def summary(self) -> dict[str, Any]:
        """Plain integers and strings, with sorted labels: the cost section of a manifest."""
        return {
            "status": self.status,
            "cap_microusd": self.cap_microusd,
            "aborted_actions": self.aborted_actions,
            "total": self.total.as_dict(),
            "by_role": {label: self.by_label[label].as_dict() for label in sorted(self.by_label)},
        }


# ---------------------------------------------------------------------------- estimate


@dataclass(frozen=True)
class EstimateAssumptions:
    """Token sizes of a call, averaged over a measured plain-mode night of twelve lots.

    ``prefix_tokens`` is the system prompt that every call of a seat repeats (cached after the
    first call). The observation and the output are given per night and spread over
    ``reference_calls``. Thinking tokens are added to the output of every call, and an Anthropic
    model counts the same text with up to ``anthropic_token_factor_high`` times as many tokens as
    the measuring tokenizer did. The low estimate uses no thinking and a factor of one.

    The show layer has its own sizes. They are the prompts of the same mock night, system prompt
    included, counted at one token per two characters (the rule of the mock model), and the
    answers a role is asked for. A call of the host is the system prompt and one fact card (one
    lot, or the whole night for the recap), and it is asked for at most 80 characters (a lot) or
    500 (the recap). A call of a judge is the system prompt and the whole public record, and the
    answer is a list of extractions. ``judge_retry_tokens`` is what a second try adds to the
    first one: the first answer, repeated at most 3000 tokens long, and the follow-up. A call of
    the content classifier is the system prompt and one batch of 40 texts, and the answer is a
    short JSON object. ``scan_texts_per_night`` is the number of public texts of the mock night
    (six model seats, 27.25 calls each), and grows and shrinks with the calls of the model seats.
    A batch holds at most ``scan_batch_items`` texts.
    """

    prefix_tokens: int = 2132
    observation_tokens_per_night: int = 26447
    output_tokens_per_night: int = 3331
    reference_calls: Decimal = Decimal("27.25")
    thinking_tokens_low: int = 0
    thinking_tokens_high: int = 1000
    anthropic_token_factor_high: Decimal = Decimal("1.3")
    reference_model_seats: int = 6
    host_lot_input_tokens: int = 1042
    host_recap_input_tokens: int = 2595
    host_lot_output_tokens: int = 40
    host_recap_output_tokens: int = 250
    judge_input_tokens: int = 5628
    judge_output_tokens: int = 3321
    judge_retry_tokens: int = 3100
    scan_batch_input_tokens: int = 1348
    scan_batch_output_tokens: int = 20
    scan_texts_per_night: int = 497
    scan_batch_items: int = 40

    @property
    def observation_per_call(self) -> Decimal:
        return Decimal(self.observation_tokens_per_night) / self.reference_calls

    @property
    def output_per_call(self) -> Decimal:
        return Decimal(self.output_tokens_per_night) / self.reference_calls


_SEALED_FORMATS: Final = ("sealed_first", "sealed_second", "all_pay", "blind_box")


def calls_breakdown(config: NightConfig) -> dict[str, Fraction]:
    """Calls per model seat and night, by kind.

    English revision rounds ask every remaining seat each time the field is down to ``theta``
    seats or fewer: ``2 + 3 + ... + theta`` calls per lot (capped by the table size), shared
    over the seats of the table.
    """
    night = config.night
    lots = night.lots_per_format
    seats = len(config.seats)
    result: dict[str, Fraction] = {
        "plan": Fraction(1 if night.plan_and_mindreading else 0),
        "talk": Fraction(night.n_lots),
        "sealed": Fraction(lots * sum(f in night.formats for f in _SEALED_FORMATS)),
        "dutch": Fraction(lots if "dutch" in night.formats else 0),
    }
    english = Fraction(0)
    if "english" in night.formats:
        rounds = min(night.english.revise_when_remaining_le, seats - 1)
        revisions = Fraction(max(0, rounds * (rounds + 1) // 2 - 1), seats)
        english = lots * (1 + revisions)
    result["english"] = english
    return result


def _to_decimal(value: Fraction) -> Decimal:
    return Decimal(value.numerator) / Decimal(value.denominator)


@dataclass(frozen=True)
class TokenBill:
    """Tokens to pay for, by price line. Cache writes are the first prefix of a seat."""

    input: Decimal
    cache_write: Decimal
    cache_read: Decimal
    output: Decimal

    def cost_microusd(self, price: ModelPrice, units_per_usd: Decimal | None = None) -> Decimal:
        p_in, p_out, p_read, p_write = _price_in_microusd(price, units_per_usd)
        return (
            self.input * p_in + self.output * p_out + self.cache_read * p_read
            + self.cache_write * p_write
        )  # fmt: skip

    @property
    def new_input(self) -> Decimal:
        """Prompt tokens that are not read from the cache."""
        return self.input + self.cache_write


@dataclass(frozen=True)
class SeatEstimate:
    """The estimate for one model seat and one night. Costs are None without a price."""

    seat: int
    endpoint: str
    model: str
    calls: Decimal
    low: TokenBill
    high: TokenBill
    cost_low_microusd: Decimal | None
    cost_high_microusd: Decimal | None

    @property
    def priced(self) -> bool:
        return self.cost_low_microusd is not None


@dataclass(frozen=True)
class RoleEstimate:
    """The estimate for one show-layer role that is a model, and one night.

    ``role`` is the label the recording and the replay use: ``host``, ``judge:0``, ``judge:1`` or
    ``classifier``. The calls and the high estimate differ from the low one when a second try is
    possible (a judge). Costs are None without a price.
    """

    role: str
    endpoint: str
    model: str
    calls_low: Decimal
    calls_high: Decimal
    low: TokenBill
    high: TokenBill
    cost_low_microusd: Decimal | None
    cost_high_microusd: Decimal | None

    @property
    def priced(self) -> bool:
        return self.cost_low_microusd is not None


@dataclass(frozen=True)
class NightEstimate:
    """The estimate of a night (or of ``nights`` nights played with the same line-up).

    ``show`` holds the roles of the show layer that are models, and ``show_mocks`` counts those
    that are not (host, two judges and the classifier, if there is one). ``scan_texts`` is the
    number of public texts that the content classifier is expected to read in one night.
    ``rates`` are the exchange rates of the price list: when it has one, the report shows the
    amounts in that currency beside the dollars.
    """

    night_id: str
    nights: int
    seats: tuple[SeatEstimate, ...]
    breakdown: Mapping[str, Fraction]
    assumptions: EstimateAssumptions
    prices_source: str | None
    cap_microusd: int | None
    placeholders: tuple[str, ...]
    seats_without_calls: int
    show: tuple[RoleEstimate, ...] = ()
    show_mocks: int = 0
    scan_texts: int = 0
    rates: Mapping[str, ExchangeRate] = field(default_factory=dict)

    @property
    def priced_seats(self) -> tuple[SeatEstimate, ...]:
        return tuple(s for s in self.seats if s.priced)

    @property
    def unpriced_models(self) -> list[str]:
        return sorted({s.model for s in self.seats if not s.priced})

    @property
    def priced_show(self) -> tuple[RoleEstimate, ...]:
        return tuple(r for r in self.show if r.priced)

    @property
    def unpriced_show_models(self) -> list[str]:
        return sorted({r.model for r in self.show if not r.priced})

    def total_microusd(self) -> tuple[Decimal, Decimal]:
        """Low and high cost of all nights, over the priced seats only."""
        low = sum((s.cost_low_microusd or Decimal(0) for s in self.seats), Decimal(0))
        high = sum((s.cost_high_microusd or Decimal(0) for s in self.seats), Decimal(0))
        return low * self.nights, high * self.nights

    def show_total_microusd(self) -> tuple[Decimal, Decimal]:
        """Low and high cost of the show layer of all nights, over the priced roles only."""
        low = sum((r.cost_low_microusd or Decimal(0) for r in self.show), Decimal(0))
        high = sum((r.cost_high_microusd or Decimal(0) for r in self.show), Decimal(0))
        return low * self.nights, high * self.nights

    def night_total_microusd(self) -> tuple[Decimal, Decimal]:
        """Low and high cost of all nights, seats and show layer together, priced parts only."""
        seat_low, seat_high = self.total_microusd()
        show_low, show_high = self.show_total_microusd()
        return seat_low + show_low, seat_high + show_high


def _bill(
    calls: Decimal, assumptions: EstimateAssumptions, factor: Decimal, thinking: int
) -> TokenBill:
    prefix = Decimal(assumptions.prefix_tokens) * factor
    first = Decimal(min(calls, Decimal(1)))
    return TokenBill(
        input=assumptions.observation_per_call * calls * factor,
        cache_write=prefix * first,
        cache_read=prefix * max(calls - 1, Decimal(0)),
        output=assumptions.output_per_call * calls * factor + Decimal(thinking) * calls,
    )


def _plain_bill(
    prompt: int, answer: int, calls: Decimal, factor: Decimal, thinking: int
) -> TokenBill:
    """A bill without a cache: every prompt token is plain input. Thinking tokens come on top of
    the answer, once per call, and the tokenizer factor does not touch them."""
    return TokenBill(
        input=Decimal(prompt) * factor,
        cache_write=Decimal(0),
        cache_read=Decimal(0),
        output=Decimal(answer) * factor + Decimal(thinking) * calls,
    )


def show_roles(config: NightConfig) -> list[tuple[str, RoleConfig]]:
    """The roles of the show layer that the night has, in the order they are called, with the
    labels that recordings and replays use: ``host``, ``judge:0``, ``judge:1`` and, when the
    configuration has one, ``classifier``. A role is a model (``llm``) or a ``mock``."""
    show = config.show
    named = [("host", show.host_offline)]
    named += [(f"judge:{index}", judge) for index, judge in enumerate(show.judges)]
    if show.content_scan.classifier is not None:
        named.append(("classifier", show.content_scan.classifier))
    return named


def scan_texts_expected(
    config: NightConfig, breakdown: Mapping[str, Fraction], assumptions: EstimateAssumptions
) -> int:
    """The public texts that the content classifier is expected to read in one night.

    The texts of a night are mostly the lines and the inner monologues of the model seats, so
    their number is taken to follow the calls of the model seats: the reference number of the
    mock night, scaled by the calls of this table over the calls of the reference table.
    """
    seat_calls = Decimal(len(config.model_seats)) * _to_decimal(
        sum(breakdown.values(), Fraction(0))
    )
    reference = Decimal(assumptions.reference_model_seats) * assumptions.reference_calls
    texts = Decimal(assumptions.scan_texts_per_night) * seat_calls / reference
    return int(texts.to_integral_value(rounding=ROUND_HALF_UP))


def _role_estimate(
    label: str,
    role: RoleConfig,
    config: NightConfig,
    prices: PriceTable,
    assumptions: EstimateAssumptions,
    texts: int,
) -> RoleEstimate:
    """One show-layer role: how often it is called and what that comes to. See the module text."""
    assert role.endpoint is not None and role.model is not None
    a = assumptions
    anthropic = config.endpoints[role.endpoint].kind == "anthropic"
    factor = a.anthropic_token_factor_high if anthropic else Decimal(1)
    one, two = Decimal(1), Decimal(2)
    low_think, high_think = a.thinking_tokens_low, a.thinking_tokens_high
    if label == "host":
        lots = config.night.n_lots
        calls_low = calls_high = Decimal(lots + 1)
        prompt = lots * a.host_lot_input_tokens + a.host_recap_input_tokens
        answer = lots * a.host_lot_output_tokens + a.host_recap_output_tokens
        bills = [(prompt, answer)] * 2
    elif label == "classifier":
        batches = max(1, -(-texts // a.scan_batch_items))
        calls_low = calls_high = Decimal(batches)
        bills = [(batches * a.scan_batch_input_tokens, batches * a.scan_batch_output_tokens)] * 2
    else:  # a judge: one call, and in the high estimate the second try as well
        calls_low, calls_high = one, two
        first = a.judge_input_tokens
        bills = [
            (first, a.judge_output_tokens),
            (2 * first + a.judge_retry_tokens, 2 * a.judge_output_tokens),
        ]
    low = _plain_bill(*bills[0], calls_low, one, low_think)
    high = _plain_bill(*bills[1], calls_high, factor, high_think)
    found = prices.lookup(role.model)
    rate = None if found is None else prices.units_per_usd(found[1])
    return RoleEstimate(
        role=label,
        endpoint=role.endpoint,
        model=role.model,
        calls_low=calls_low,
        calls_high=calls_high,
        low=low,
        high=high,
        cost_low_microusd=None if found is None else low.cost_microusd(found[1], rate),
        cost_high_microusd=None if found is None else high.cost_microusd(found[1], rate),
    )


def estimate_night(
    config: NightConfig,
    prices: PriceTable,
    *,
    assumptions: EstimateAssumptions | None = None,
    nights: int = 1,
) -> NightEstimate:
    """Calls, tokens and cost of one night, from the configuration alone: the model seats, and
    the roles of the show layer that are models."""
    assumptions = assumptions or EstimateAssumptions()
    breakdown = calls_breakdown(config)
    calls = _to_decimal(sum(breakdown.values(), Fraction(0)))
    rows = []
    for seat in sorted(config.seats, key=lambda s: s.seat):
        if seat.kind != "llm" or seat.endpoint is None or seat.model is None:
            continue
        anthropic = config.endpoints[seat.endpoint].kind == "anthropic"
        high_factor = assumptions.anthropic_token_factor_high if anthropic else Decimal(1)
        low = _bill(calls, assumptions, Decimal(1), assumptions.thinking_tokens_low)
        high = _bill(calls, assumptions, high_factor, assumptions.thinking_tokens_high)
        found = prices.lookup(seat.model)
        rate = None if found is None else prices.units_per_usd(found[1])
        rows.append(
            SeatEstimate(
                seat=seat.seat,
                endpoint=seat.endpoint,
                model=seat.model,
                calls=calls,
                low=low,
                high=high,
                cost_low_microusd=None if found is None else low.cost_microusd(found[1], rate),
                cost_high_microusd=None if found is None else high.cost_microusd(found[1], rate),
            )
        )
    texts = scan_texts_expected(config, breakdown, assumptions)
    roles = show_roles(config)
    mocks = sum(role.kind == "mock" for _, role in roles)
    show = tuple(
        _role_estimate(label, role, config, prices, assumptions, texts)
        for label, role in roles
        if role.kind == "llm"
    )
    return NightEstimate(
        night_id=config.night.id,
        nights=nights,
        seats=tuple(rows),
        breakdown=breakdown,
        assumptions=assumptions,
        prices_source=prices.source,
        cap_microusd=config.limits.max_cost_microusd,
        placeholders=tuple(find_placeholders(config)),
        seats_without_calls=len(config.seats) - len(rows),
        show=show,
        show_mocks=mocks,
        scan_texts=texts,
        rates=dict(prices.rates),
    )


def _whole(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_HALF_UP))


def _range(low: int, high: int) -> str:
    return f"{low:,}" if low == high else f"{low:,} - {high:,}"


def _usd(microusd: Decimal) -> str:
    return f"${microusd / MICRO:.4f}"


def _usd_range(low: Decimal, high: Decimal) -> str:
    first, second = _usd(low), _usd(high)
    return first if first == second else f"{first} - {second}"


_SYMBOLS: Final = {"CNY": "¥"}


@dataclass(frozen=True)
class _Beside:
    """The currency shown beside the dollars when the price list has a rate for it.

    An amount is the dollar amount times the units of the currency to the dollar, so a price list
    in yuan prints the yuan it was written in (to the rounding of four decimals).
    """

    code: str
    rate: ExchangeRate

    @property
    def units_per_usd(self) -> Decimal:
        return _dec(self.rate.units_per_usd)

    def amount(self, microusd: Decimal) -> str:
        symbol = _SYMBOLS.get(self.code, f"{self.code} ")
        return f"{symbol}{microusd / MICRO * self.units_per_usd:.4f}"

    def range(self, low: Decimal, high: Decimal) -> str:
        first, second = self.amount(low), self.amount(high)
        return first if first == second else f"{first} - {second}"

    def rate_line(self) -> str:
        units = format(self.units_per_usd.normalize(), "f")  # 7.0 reads as 7
        return (
            f"Exchange rate: 1 USD = {units} {self.code} "
            f"(as of {self.rate.as_of.isoformat()}, {self.rate.source})"
        )


def _beside(estimate: NightEstimate) -> _Beside | None:
    """The first currency, by code, that the price list has a rate for. None when it has none."""
    for code in sorted(estimate.rates):
        return _Beside(code, estimate.rates[code])
    return None


def _with(local: _Beside | None, low: Decimal, high: Decimal) -> str:
    """`` (¥0.10 - ¥0.20)`` to put after a range of dollars, or nothing."""
    return "" if local is None else f" ({local.range(low, high)})"


def _cost_header(local: _Beside | None) -> list[str]:
    return ["cost per night"] if local is None else ["cost per night", f"in {local.code}"]


def _cost_cells(low: Decimal | None, high: Decimal | None, local: _Beside | None) -> list[str]:
    """The cells of a row for its cost: dollars, and the other currency when there is one."""
    if low is None or high is None:
        return ["no price"] * (1 if local is None else 2)
    cells = [_usd_range(low, high)]
    if local is not None:
        cells.append(local.range(low, high))
    return cells


def _number(value: Fraction | Decimal) -> str:
    text = f"{float(value):.2f}".rstrip("0").rstrip(".")
    return text or "0"


Align = Literal["<", ">"]


def _table(
    header: Sequence[str], rows: Sequence[Sequence[str]], align: Sequence[Align]
) -> list[str]:
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header))]

    def line(cells: Sequence[str]) -> str:
        return "  ".join(f"{cell:{align[i]}{widths[i]}}" for i, cell in enumerate(cells)).rstrip()

    return [line(header), line(["-" * w for w in widths]), *(line(row) for row in rows)]


def _calls_cell(role: RoleEstimate) -> str:
    low, high = _number(role.calls_low), _number(role.calls_high)
    return low if low == high else f"{low} - {high}"


def _show_lines(estimate: NightEstimate) -> list[str]:
    """The show-layer section of the report: one row per role that is a model, the totals and
    how often each role is called."""
    nights = f"{estimate.nights} night{'s' if estimate.nights != 1 else ''}"
    show = estimate.show
    local = _beside(estimate)
    out = ["", "Show layer (host, judges and content scan, the roles that are models):"]
    rows = []
    for r in show:
        cells = [
            r.role,
            r.model,
            _calls_cell(r),
            _range(_whole(r.low.new_input), _whole(r.high.new_input)),
            _range(_whole(r.low.output), _whole(r.high.output)),
        ]
        cells += _cost_cells(r.cost_low_microusd, r.cost_high_microusd, local)
        rows.append(cells)
    cost_columns = _cost_header(local)
    header = ["role", "model", "calls", "input", "output", *cost_columns]
    align: list[Align] = ["<", "<", ">", ">", ">", *[">"] * len(cost_columns)]
    out += _table(header, rows, align)
    out.append("")
    a = estimate.assumptions
    labels = {r.role: r for r in show}
    calls = []
    if "host" in labels:
        lots = int(labels["host"].calls_low) - 1
        host = _calls_cell(labels["host"])
        calls.append(f"host {host} (one for each of the {lots} lots, one for the recap)")
    if any(r.role.startswith("judge:") for r in show):
        calls.append("each judge 1, or 2 if its first answer cannot be read (high estimate)")
    if "classifier" in labels:
        calls.append(
            f"content scan {_calls_cell(labels['classifier'])} (batches of up to "
            f"{a.scan_batch_items} texts, about {estimate.scan_texts:,} texts)"
        )
    out.append("Calls per night: " + "; ".join(calls) + ".")
    if estimate.show_mocks:
        count = estimate.show_mocks
        out.append(
            "1 show role is a mock and makes no model calls."
            if count == 1
            else f"{count} show roles are mocks and make no model calls."
        )
    priced = estimate.priced_show
    if priced:
        low, high = estimate.show_total_microusd()
        which = "all" if len(priced) == len(show) else f"{len(priced)} of {len(show)}"
        out.append(
            f"Show layer for {nights}, {which} model roles priced: "
            f"{_usd_range(low, high)}{_with(local, low, high)}"
        )
    else:
        out.append("No model role of the show layer has a price, so it has no cost estimate.")
    if estimate.unpriced_show_models:
        out.append(
            "No price for the show layer: " + ", ".join(estimate.unpriced_show_models)
            + ". Add them to the price list; no price is assumed."
        )  # fmt: skip
    if priced and estimate.priced_seats:
        low, high = estimate.night_total_microusd()
        out.append(
            f"Seats and show layer together, {nights}: "
            f"{_usd_range(low, high)}{_with(local, low, high)}"
        )
    return out


def format_estimate(estimate: NightEstimate) -> str:
    """The estimate as text: a header, one row per model seat, the show layer, the totals and the
    assumptions."""
    a = estimate.assumptions
    seats = estimate.seats
    show = estimate.show
    nights = f"{estimate.nights} night{'s' if estimate.nights != 1 else ''}"
    local = _beside(estimate)
    out = [f"Estimate for night {estimate.night_id}: {len(seats)} model seats, {nights}"]
    if estimate.prices_source:
        out.append(f"Prices: {estimate.prices_source}")
    if local is not None:
        out.append(local.rate_line())
    parts = ", ".join(f"{name} {_number(n)}" for name, n in estimate.breakdown.items() if n)
    per_seat = sum(estimate.breakdown.values(), Fraction(0))
    out.append(f"Calls per model seat and night: {_number(per_seat)} ({parts})")
    if estimate.seats_without_calls:
        out.append(f"{estimate.seats_without_calls} bot, mock or human seats make no model calls.")
    out.append("")
    if not seats and not show:
        out.append("No seat of this night is played by a model, so it makes no model calls.")
        return "\n".join(out)

    priced = estimate.priced_seats
    if seats:
        rows = []
        for s in seats:
            cells = [
                str(s.seat),
                s.model,
                _number(s.calls),
                _range(_whole(s.low.new_input), _whole(s.high.new_input)),
                _range(_whole(s.low.cache_read), _whole(s.high.cache_read)),
                _range(_whole(s.low.output), _whole(s.high.output)),
            ]
            cells += _cost_cells(s.cost_low_microusd, s.cost_high_microusd, local)
            rows.append(cells)
        cost_columns = _cost_header(local)
        header = ["seat", "model", "calls", "new input", "cache read", "output", *cost_columns]
        align: list[Align] = ["<", "<", ">", ">", ">", ">", *[">"] * len(cost_columns)]
        out += _table(header, rows, align)
        out.append("")

        if priced:
            low, high = estimate.total_microusd()
            which = "all" if len(priced) == len(seats) else f"{len(priced)} of {len(seats)}"
            out.append(
                f"Total for {nights}, {which} model seats priced: "
                f"{_usd_range(low, high)}{_with(local, low, high)}"
            )
        else:
            out.append("No model seat has a price, so there is no cost estimate.")
        if estimate.unpriced_models:
            out.append(
                "No price for: " + ", ".join(estimate.unpriced_models)
                + ". Add them to the price list; no price is assumed."
            )  # fmt: skip
    else:
        out.append("No seat of this night is played by a model.")
    if show:
        out += _show_lines(estimate)
    if estimate.cap_microusd is None:
        out.append("Cost cap per night: none")
    else:
        cap = f"Cost cap per night: {_usd(Decimal(estimate.cap_microusd))}"
        if local is not None:
            cap += f" = {local.amount(Decimal(estimate.cap_microusd))}"
        if priced or estimate.priced_show:
            high_night = estimate.night_total_microusd()[1] / estimate.nights
            verdict = "within" if high_night <= estimate.cap_microusd else "above"
            cap += f" (the high estimate for one night is {verdict} the cap)"
        out.append(cap)
        if show:
            out.append(
                "The cap holds for the seats (run) and the show layer (show) on their own. "
                "This verdict counts both together."
            )
    if estimate.placeholders:
        out.append("Placeholders still in the configuration: " + ", ".join(estimate.placeholders))
    out += [
        "",
        "Low: no thinking tokens. High: "
        f"{a.thinking_tokens_high:,} thinking tokens per call, and Anthropic models count "
        f"{a.anthropic_token_factor_high}x as many tokens.",
    ]
    if seats:
        out.append(
            f"Per call: prefix {a.prefix_tokens:,} tokens (cache write on the first call, cache "
            f"reads after), observation {_whole(a.observation_per_call):,}, "
            f"output {_whole(a.output_per_call):,}."
        )
    out.append("A cache price missing from the price list is billed at the input price.")
    if local is not None:
        out.append(
            f"Amounts in {local.code} are the dollar amounts at the exchange rate above, and a "
            f"price written in {local.code} is divided by it. The ledger and the cost cap count "
            "US dollars."
        )
    if show:
        out += [
            "Show-layer prompts are billed as plain input, with no cache discount. Their sizes "
            "are the prompts of the mock night, counted at one token per two characters.",
            "A rewrite of a host text, an unreadable reply of the classifier and retries after "
            "an error are not counted.",
        ]
    else:
        out.append("The show layer (host, judges, content scan) is played by mocks, with no model.")
    return "\n".join(out)
