"""Highlight candidates: found by plain rules on the data of one lot, and never scored."""

from typing import Any

import pytest
from show_support import Script, lots_of, make_lot, play_script, scripted_config, seat_line

from auction_night.contracts.events import Pledge
from auction_night.contracts.replay import PledgeRecord
from auction_night.engine.deal import deal
from auction_night.show.candidates import CATEGORY_ORDER, find_candidates
from auction_night.show.cards import GuessLine, MissionLine, PriorLine
from auction_night.show.factcard import build_fact_cards

MISS = 150


def found(lot: Any, category: str) -> list[Any]:
    return [c for c in find_candidates(lot, miss_error=MISS) if c.category == category]


# -------------------------------------------------------------------- winner's curse


def blind_lot(price: int, true_value: int = 200, **extra: Any) -> Any:
    return make_lot(
        fmt="blind_box",
        tier=200,
        seats=[seat_line(1, won=True, paid=price), seat_line(2)],
        winner=1,
        price=price,
        true_value=true_value,
        prior=PriorLine(low=100, high=300, noise=40),
        **extra,
    )


def test_a_blind_box_sold_above_its_true_value_is_a_winners_curse() -> None:
    (candidate,) = found(blind_lot(250), "winners_curse")
    assert candidate.seats == (1,)
    assert candidate.facts == {"price": 250, "true_value": 200, "gap": 50}
    assert "多付 50" in candidate.reason


@pytest.mark.parametrize("price", [200, 150, 0])
def test_a_blind_box_sold_at_or_below_its_true_value_is_not(price: int) -> None:
    assert found(blind_lot(price), "winners_curse") == []


def test_an_ordinary_lot_has_no_winners_curse() -> None:
    lot = make_lot(seats=[seat_line(1, won=True, paid=99)], winner=1, price=99)
    assert found(lot, "winners_curse") == []


# ------------------------------------------------------------------------- shill stuck


def shill_lot(outcome: str) -> Any:
    mission = MissionLine(
        seat=1,
        kind="shill",
        outcome=outcome,  # type: ignore[arg-type]
        target_lot=1,
        threshold=190,
        price=170,
        buyer=1 if outcome == "stuck" else 2,
    )
    return make_lot(seats=[seat_line(1), seat_line(2)], missions=(mission,))


def test_a_shill_that_bought_its_own_lot_is_stuck() -> None:
    (candidate,) = found(shill_lot("stuck"), "shill_stuck")
    assert candidate.seats == (1,)
    assert candidate.facts == {"price": 170, "threshold": 190}


@pytest.mark.parametrize("outcome", ["reached", "short", "unsold"])
def test_the_other_endings_of_a_shill_mission_are_not(outcome: str) -> None:
    lot = shill_lot("reached")
    other = MissionLine(seat=1, kind="shill", outcome=outcome, target_lot=1, threshold=190)  # type: ignore[arg-type]
    lot = lot.model_copy(update={"missions": (other,)})
    assert found(lot, "shill_stuck") == []


# ---------------------------------------------------------------------------- backstab


def record(outcome: str, seat: int = 1) -> PledgeRecord:
    return PledgeRecord(
        seq=20,
        lot=1,
        seat=seat,
        pledge=Pledge(kind="no_bid", lot=3),
        outcome=outcome,  # type: ignore[arg-type]
        detail="承诺第 3 件不出价，实际出价 30",
    )


def test_a_broken_pledge_about_the_lot_is_a_backstab() -> None:
    lot = make_lot(lot=3, seats=[seat_line(1)], pledges_due=(record("violated"),))
    (candidate,) = found(lot, "backstab")
    assert candidate.seats == (1,)
    assert candidate.facts == {"made_in_lot": 1, "pledged_lot": 3}


@pytest.mark.parametrize("outcome", ["kept", "void"])
def test_a_kept_or_void_pledge_is_not(outcome: str) -> None:
    lot = make_lot(lot=3, seats=[seat_line(1)], pledges_due=(record(outcome),))
    assert found(lot, "backstab") == []


# ---------------------------------------------------------------------- plan against act


@pytest.mark.parametrize(
    ("stars", "won", "played", "abstained", "expected"),
    [
        (0, True, 80, False, True),
        (1, True, 80, False, True),
        (2, True, 80, False, False),
        (0, False, 0, False, False),
        (3, False, 0, False, True),
        (3, False, None, True, True),
        (3, False, 20, False, False),
        (2, False, 0, False, False),
    ],
)
def test_plan_contradictions(
    stars: int, won: bool, played: int | None, abstained: bool, expected: bool
) -> None:
    line = seat_line(
        1,
        won=won,
        paid=80 if won else 0,
        played=played,
        abstained=abstained,
        plan_stars=stars,
    )
    lot = make_lot(seats=[line], winner=1 if won else None, price=80 if won else 0)
    assert bool(found(lot, "plan_contradiction")) is expected


def test_at_most_two_plan_contradictions_per_lot() -> None:
    lines = [seat_line(n, played=0, plan_stars=3) for n in range(1, 6)]
    assert len(found(make_lot(seats=lines), "plan_contradiction")) == 2


# ------------------------------------------------------------------------ all-pay tilt


def all_pay_lot(loser_paid: int, tier: int = 200) -> Any:
    return make_lot(
        fmt="all_pay",
        tier=tier,
        seats=[seat_line(1, won=True, paid=150), seat_line(2, paid=loser_paid), seat_line(3)],
        winner=1,
        price=150,
        payments={1: 150, 2: loser_paid},
    )


def test_a_loser_who_paid_a_tenth_of_the_tier_for_nothing_is_a_tilt() -> None:
    (candidate,) = found(all_pay_lot(20), "all_pay_tilt")
    assert candidate.seats == (2,) and candidate.facts == {"paid": 20, "tier": 200}


def test_a_smaller_loss_is_not() -> None:
    assert found(all_pay_lot(19), "all_pay_tilt") == []


def test_only_all_pay_lots_can_tilt() -> None:
    lot = make_lot(seats=[seat_line(1, won=True, paid=99)], winner=1, price=99, payments={1: 99})
    assert found(lot, "all_pay_tilt") == []


# ---------------------------------------------------------------------------- bargain


def test_a_gain_of_a_fifth_of_the_tier_is_a_bargain() -> None:
    lot = make_lot(tier=200, seats=[seat_line(1, won=True, paid=60, value=100)], winner=1, price=60)
    (candidate,) = found(lot, "bargain")
    assert candidate.facts == {"value": 100, "price": 60, "gain": 40}


def test_a_smaller_gain_is_not() -> None:
    lot = make_lot(tier=200, seats=[seat_line(1, won=True, paid=61, value=100)], winner=1, price=61)
    assert found(lot, "bargain") == []


# ------------------------------------------------------------------------ mind reading


def reader(seat: int, target: int, error: int) -> Any:
    return seat_line(
        seat, guess=GuessLine(target=target, guess=100, truth=100 + error, abs_error=error)
    )


def test_the_worst_guess_of_a_lot_is_the_miss() -> None:
    lot = make_lot(seats=[reader(1, 2, 160), reader(2, 1, 300), reader(3, 1, 20)])
    (candidate,) = found(lot, "mindread_miss")
    assert candidate.seats == (2, 1)
    assert candidate.facts == {"guess": 100, "truth": 400, "error": 300}


def test_small_errors_are_not_misses() -> None:
    assert found(make_lot(seats=[reader(1, 2, MISS - 1)]), "mindread_miss") == []
    assert len(found(make_lot(seats=[reader(1, 2, MISS)]), "mindread_miss")) == 1


# ------------------------------------------------------------------------------ order


def test_candidates_come_in_the_order_of_the_categories() -> None:
    lot = blind_lot(
        250,
        pledges_due=(record("violated"),),
    ).model_copy(
        update={"seats": (seat_line(1, won=True, paid=250, plan_stars=0), reader(2, 1, 200))}
    )
    categories = [c.category for c in find_candidates(lot, miss_error=MISS)]
    assert categories == [
        "winners_curse",
        "backstab",
        "plan_contradiction",
        "mindread_miss",
    ]
    assert categories == sorted(categories, key=CATEGORY_ORDER.index)


def test_nothing_happening_gives_no_candidate() -> None:
    assert find_candidates(make_lot(seats=[seat_line(1), seat_line(2)]), miss_error=MISS) == ()


# -------------------------------------------------------------- missions in real nights


def shill_night(outcome: str) -> tuple[Any, int, int]:
    """A night in which the first shill either buys its own target lot or pushes it up."""
    config = scripted_config()
    the_deal = deal(config, seed="show-shill-1")
    seat, mission = next((s, m) for s, m in sorted(the_deal.missions.items()) if m.kind == "shill")
    target = mission.lot
    other = 1 if seat != 1 else 2
    bids: dict[tuple[int, int], int | None] = {}
    if outcome == "stuck":
        bids = {(target, seat): mission.threshold_price + 50}
    elif outcome == "reached":
        bids = {
            (target, seat): mission.threshold_price,
            (target, other): mission.threshold_price + 40,
        }
    elif outcome == "short":
        bids = {(target, seat): 10, (target, other): 50}
    _, _, transcript = play_script(Script(bids=bids), seed="show-shill-1")
    return build_fact_cards(transcript.state), seat, target


@pytest.mark.parametrize("outcome", ["stuck", "reached", "short", "unsold"])
def test_a_shill_mission_shows_in_its_target_lot(outcome: str) -> None:
    cards, seat, target = shill_night(outcome)
    (line,) = [m for m in cards.lot(target).data.missions if m.seat == seat]
    assert line.kind == "shill" and line.outcome == outcome
    stuck = [c for c in cards.lot(target).data.candidates if c.category == "shill_stuck"]
    assert bool(stuck) is (outcome == "stuck")


def test_a_collector_advances_and_completes() -> None:
    config = scripted_config()
    the_deal = deal(config, seed="show-collector-1")
    seat, mission = next(
        (s, m) for s, m in sorted(the_deal.missions.items()) if m.kind == "collector"
    )
    tagged = [lot.lot for lot in the_deal.lots if lot.tag == mission.tag]
    assert len(tagged) >= 2
    bids = {(lot, seat): 5000 for lot in tagged[:2]}
    _, _, transcript = play_script(Script(bids=bids), seed="show-collector-1")
    cards = build_fact_cards(transcript.state)
    first = [m for m in cards.lot(tagged[0]).data.missions if m.seat == seat][0]
    second = [m for m in cards.lot(tagged[1]).data.missions if m.seat == seat][0]
    assert (first.outcome, first.won_so_far) == ("advanced", 1)
    assert (second.outcome, second.won_so_far) == ("done", 2)
    for later in tagged[2:]:
        assert [m for m in cards.lot(later).data.missions if m.seat == seat] == []
    assert first.tag_lots_left == len(tagged) - 1


def test_lots_of_each_format_exist_in_the_scripted_deal() -> None:
    the_deal = deal(scripted_config(), seed="show-collector-1")
    assert all(len(lots_of(the_deal, f)) == 2 for f in ("english", "dutch", "all_pay"))
