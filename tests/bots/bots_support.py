"""Hand-built requests for the bot tests, so that a bot is checked against a formula and not
against a night that was played with the bot itself."""

from auction_night.contracts.events import Mode
from auction_night.contracts.mechanism import MECHANISMS, FormatKey
from auction_night.engine.deal import BlindBounds
from auction_night.engine.night import Request, SeatFacts, Slot

PHASE_OF = {"english": "english_init", "dutch": "dutch"}


def bid_request(
    fmt: FormatKey,
    *,
    seat: int = 1,
    tier: int = 200,
    bidders: int = 8,
    budget: int = 400,
    value: int | None = None,
    signal: int | None = None,
    upcoming: tuple[Slot, ...] | None = None,
    blind: BlindBounds | None = None,
    lot: int = 1,
    mode: Mode = "plain",
    phase: str | None = None,
    exit_price: int | None = None,
    price: int | None = None,
) -> Request:
    """A request that asks ``seat`` to bid in a lot of ``fmt``."""
    if MECHANISMS[fmt].value_model == "common" and blind is None:
        blind = BlindBounds(low=tier // 2, high=3 * tier // 2, noise=tier // 5)
    asked = tuple(range(1, bidders + 1)) if seat <= bidders else (seat,)
    if seat not in asked:
        asked = (*asked, seat)
    facts = tuple(
        SeatFacts(
            seat=s,
            budget_left=budget,
            value=value if s == seat else None,
            signal=signal if s == seat else None,
            exit_price=exit_price if s == seat else None,
        )
        for s in asked
    )
    return Request(
        index=0,
        phase=phase or PHASE_OF.get(fmt, "sealed"),  # type: ignore[arg-type]
        lot=lot,
        seats=asked,
        facts=facts,
        n_lots=12,
        n_seats=8,
        mode=mode,
        upcoming=upcoming if upcoming is not None else (Slot(lot, fmt, tier),),
        format=fmt,
        tier=tier,
        reserve=0,
        bidders=bidders,
        blind=blind,
        price=price,
        remaining=asked if phase == "english_revise" else (),
    )


def schedule(*lots: tuple[FormatKey, int]) -> tuple[Slot, ...]:
    """The upcoming lots, numbered from 1, for pacing tests."""
    return tuple(Slot(number, fmt, tier) for number, (fmt, tier) in enumerate(lots, start=1))
