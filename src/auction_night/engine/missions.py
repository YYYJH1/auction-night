"""Secret missions: who completed theirs, decided from the results of the lots.

A mission is dealt to a model seat (bots have none) and decided once, after the last lot, from
what the hammers said. Two kinds exist:

* a **collector** completes its mission by winning ``need`` lots that carry its tag;
* a **shill** completes its mission when its target lot is sold for at least the threshold
  price and the buyer is somebody else. Pushing the price up is not enough: a shill that wins
  the lot itself has failed, and so has one whose lot goes unsold.

Both pay the same bonus once. The bonus is kept apart from the surplus, in every score and in
every event. Nothing here draws a random number: the missions, their tags, target lots and
thresholds were fixed by the deal.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from auction_night.contracts.events import (
    CollectorMission,
    HammerData,
    Mission,
    MissionResultData,
)
from auction_night.engine.deal import LotDeal


@dataclass(frozen=True, slots=True)
class LotResult:
    """What a mission needs to know about one decided lot.

    ``winner`` is None when the lot went unsold, and then ``price`` is 0.
    """

    lot: int
    tag: str
    winner: int | None
    price: int


def lot_results(lots: Sequence[LotDeal], hammers: Sequence[HammerData]) -> list[LotResult]:
    """The results of the lots played so far. ``hammers[i]`` is the hammer of lot ``i + 1``."""
    if len(hammers) > len(lots):
        raise ValueError("more hammers than lots")
    return [
        LotResult(lot=lot.lot, tag=lot.tag, winner=hammer.winner, price=hammer.price)
        for lot, hammer in zip(lots, hammers, strict=False)
    ]


def evaluate_mission(
    seat: int, mission: Mission, results: Sequence[LotResult]
) -> MissionResultData:
    """Decide one mission. ``results`` may be incomplete, then the mission simply is not done."""
    if isinstance(mission, CollectorMission):
        won = sorted(r.lot for r in results if r.tag == mission.tag and r.winner == seat)
        done = len(won) >= mission.need
        return MissionResultData(
            seat=seat,
            kind="collector",
            completed=done,
            bonus=mission.bonus if done else 0,
            tag=mission.tag,
            need=mission.need,
            won_lots=won,
        )
    target = next((r for r in results if r.lot == mission.lot), None)
    sold = target is not None and target.winner is not None
    buyer = target.winner if target is not None else None
    price = target.price if target is not None and sold else None
    done = sold and buyer != seat and price is not None and price >= mission.threshold_price
    return MissionResultData(
        seat=seat,
        kind="shill",
        completed=done,
        bonus=mission.bonus if done else 0,
        target_lot=mission.lot,
        threshold_price=mission.threshold_price,
        final_price=price,
        buyer=buyer,
    )


def evaluate_missions(
    missions: Mapping[int, Mission], results: Sequence[LotResult]
) -> list[MissionResultData]:
    """Decide every mission, in seat order."""
    return [evaluate_mission(seat, missions[seat], results) for seat in sorted(missions)]


def completion_lot(result: MissionResultData) -> int | None:
    """The lot at which a completed mission was completed, else None.

    A collector completes it with the lot that brings its count to ``need``. A shill completes
    it with the hammer of its target lot. Ties between seats are for the awards to settle.
    """
    if not result.completed:
        return None
    if result.kind == "collector":
        assert result.need is not None
        return sorted(result.won_lots)[result.need - 1]
    return result.target_lot
