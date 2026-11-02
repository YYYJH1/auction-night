"""Scores of a night: surplus, mission bonus and total, kept apart.

* **Surplus** of a seat is the value of the lots it won minus everything it paid. The value of
  an ordinary lot is the seat's own value, the value of a blind box is its true value, which
  every seat shares. All-pay lots charge the losers too, and that money is part of what they
  paid. Budget that was not spent is worth nothing.
* **Mission bonus** is paid once for a completed mission and is never mixed into the surplus.
* **Total** is surplus plus mission bonus. It decides tonight's ranking, the season leaderboard
  uses the surplus alone.

The books balance by construction: what the seats win in value, they pay to the seller, so the
sum of all surpluses is the value of everything sold minus the seller's revenue. A check of that
identity from the raw deal and hammers is :func:`conservation_gap`.
"""

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass

from auction_night.contracts.events import HammerData, SeatScore
from auction_night.engine.deal import LotDeal


@dataclass(frozen=True, slots=True)
class SeatTally:
    """What one seat won and paid over the lots played so far."""

    seat: int
    values_won: int
    paid: int
    lots_won: tuple[int, ...]

    @property
    def surplus(self) -> int:
        """Value of the lots won minus all payments, all-pay losses included."""
        return self.values_won - self.paid


def tally_night(
    lots: Sequence[LotDeal], hammers: Sequence[HammerData], seats: Iterable[int]
) -> dict[int, SeatTally]:
    """Add up the results of the lots played so far. ``hammers[i]`` is the hammer of lot ``i+1``."""
    if len(hammers) > len(lots):
        raise ValueError("more hammers than lots")
    values = dict.fromkeys(seats, 0)
    paid = dict.fromkeys(values, 0)
    won: dict[int, list[int]] = {seat: [] for seat in values}
    for lot, hammer in zip(lots, hammers, strict=False):
        for seat, amount in hammer.payments.items():
            paid[seat] += amount
        if hammer.winner is not None:
            values[hammer.winner] += lot.values[hammer.winner]
            won[hammer.winner].append(lot.lot)
    return {
        seat: SeatTally(seat, values[seat], paid[seat], tuple(sorted(won[seat]))) for seat in values
    }


def rank_totals(totals: Mapping[int, int]) -> dict[int, int]:
    """Competition ranks, best first: equal totals share a rank and the next rank is skipped."""
    return {
        seat: 1 + sum(1 for other in totals.values() if other > total)
        for seat, total in totals.items()
    }


def seat_scores(
    tallies: Mapping[int, SeatTally],
    bonuses: Mapping[int, int],
    ranked: Collection[int],
) -> list[SeatScore]:
    """The final tally of every seat, in seat order.

    ``bonuses`` maps a seat to the bonus of its completed mission (absent means none).
    ``ranked`` are the seats that get a ``rank_tonight``: the model seats. Bots are scored
    like everyone else but stay outside the ranking, so they do not push a model down.
    """
    totals = {seat: t.surplus + bonuses.get(seat, 0) for seat, t in tallies.items()}
    ranks = rank_totals({seat: totals[seat] for seat in tallies if seat in ranked})
    return [
        SeatScore(
            seat=seat,
            surplus=tallies[seat].surplus,
            mission_bonus=bonuses.get(seat, 0),
            total=totals[seat],
            rank_tonight=ranks.get(seat),
            spent=tallies[seat].paid,
            lots_won=list(tallies[seat].lots_won),
        )
        for seat in sorted(tallies)
    ]


def seller_revenue(hammers: Iterable[HammerData]) -> int:
    """Everything the seller took in: every payment of every lot."""
    return sum(sum(hammer.payments.values()) for hammer in hammers)


def value_sold(lots: Sequence[LotDeal], hammers: Sequence[HammerData]) -> int:
    """The value of everything that was sold, each lot at its winner's value."""
    return sum(
        lot.values[hammer.winner]
        for lot, hammer in zip(lots, hammers, strict=False)
        if hammer.winner is not None
    )


def conservation_gap(
    scores: Iterable[SeatScore], lots: Sequence[LotDeal], hammers: Sequence[HammerData]
) -> int:
    """Zero when the scores balance: their sum is the value sold minus the seller's revenue
    plus the mission bonuses. A positive number says the scores hold more than that."""
    scores = list(scores)
    expected = (
        value_sold(lots, hammers)
        - seller_revenue(hammers)
        + sum(score.mission_bonus for score in scores)
    )
    return sum(score.total for score in scores) - expected
