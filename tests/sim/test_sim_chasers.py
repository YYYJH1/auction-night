"""A chaser bids like its style, plus what its mission adds.

A collector pays more for a lot of its tag, a shill bids the threshold on its target lot. The
premium of a collector is written out below with fractions for every case.
"""

import math
from fractions import Fraction

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sim_support import make_request

from auction_night.bots import REACH, ChaserBot, StyleBot, make_styled_bot
from auction_night.bots.chasers import chance_of_at_least, collector_premium
from auction_night.contracts.events import CollectorMission, HammerData, ShillMission

TAGS = (
    "海洋",
    "机械",
    "海洋",
    "甜品",
    "海洋",
    "怪谈",
    "机械",
    "甜品",
    "怪谈",
    "机械",
    "甜品",
    "怪谈",
)
SEA = CollectorMission(kind="collector", tag="海洋", need=2, bonus=150, canary="K4M9Q2")
SHILL = ShillMission(kind="shill", lot=3, threshold_price=190, bonus=150, canary="R7T2W5")


def bid_of(bot, request) -> int:
    answer = bot.answer(request)
    assert answer.abstain is None and answer.value is not None
    return answer.value


def sold_to(seat: int | None) -> HammerData:
    if seat is None:
        return HammerData(format="english", unsold=True)
    return HammerData(format="english", winner=seat, price=10, payments={seat: 10})


# -------------------------------------------------------------------------- probabilities


def test_the_default_reach_is_fifteen_hundredths() -> None:
    assert Fraction(15, 100) == REACH


@pytest.mark.parametrize(
    "needed, trials, expected",
    [
        (0, 3, Fraction(1)),
        (-1, 0, Fraction(1)),
        (1, 0, Fraction(0)),
        (4, 3, Fraction(0)),
        (1, 1, Fraction(3, 20)),
        (1, 2, 1 - Fraction(17, 20) ** 2),
        (2, 2, Fraction(3, 20) ** 2),
        (2, 3, 3 * Fraction(3, 20) ** 2 * Fraction(17, 20) + Fraction(3, 20) ** 3),
    ],
)
def test_chance_of_at_least(needed: int, trials: int, expected: Fraction) -> None:
    assert chance_of_at_least(needed, trials, Fraction(3, 20)) == expected


# ------------------------------------------------------------------------ the premium


def premium(lot: int, wins: int, *, mission: CollectorMission = SEA) -> Fraction:
    return collector_premium(mission, lot=lot, tags=TAGS, wins=wins)


def test_a_lot_of_another_tag_adds_nothing() -> None:
    assert premium(2, 0) == premium(2, 1) == 0


def test_a_finished_collector_adds_nothing() -> None:
    assert premium(1, 2) == premium(3, 3) == 0


def test_the_lot_that_completes_the_mission_is_worth_the_whole_bonus() -> None:
    assert premium(1, 1) == premium(3, 1) == premium(5, 1) == 150


def test_an_earlier_lot_is_worth_the_bonus_times_the_chance_to_get_the_rest() -> None:
    # the sea lots are 1, 3 and 5: after lot 1 two remain, after lot 3 one, after lot 5 none
    assert premium(1, 0) == 150 * (1 - Fraction(17, 20) ** 2) == Fraction(333, 8)
    assert premium(3, 0) == 150 * Fraction(3, 20) == Fraction(45, 2)
    assert premium(5, 0) == 0


def test_a_larger_need_uses_the_chance_of_several_wins() -> None:
    three = CollectorMission(kind="collector", tag="海洋", need=3, bonus=150, canary="K4M9Q2")
    assert premium(1, 0, mission=three) == 150 * Fraction(3, 20) ** 2  # lots 3 and 5 both needed
    assert premium(3, 0, mission=three) == 0  # one lot left and two wins missing
    assert premium(3, 1, mission=three) == 150 * Fraction(3, 20)
    assert premium(5, 2, mission=three) == 150


def test_the_reach_changes_the_premium() -> None:
    eager = collector_premium(SEA, lot=3, tags=TAGS, wins=0, reach=Fraction(1, 2))
    assert eager == 75


# ----------------------------------------------------------------------------- the bids


def chaser(style: str = "E", *, mission=SEA, won: tuple[int, ...] = ()) -> ChaserBot:
    return ChaserBot(seat=1, style=style, mission=mission, tags=TAGS, won=won)  # type: ignore[arg-type]


def test_a_collector_adds_the_premium_to_the_value_in_a_second_price_lot() -> None:
    request = make_request("english", value=100, lot=3, budget=500)
    assert bid_of(chaser("E"), request) == math.floor(100 + Fraction(45, 2)) == 122
    assert bid_of(chaser("O"), request) == math.floor(Fraction(5, 4) * (100 + Fraction(45, 2)))
    assert bid_of(chaser("S"), request) == math.floor(Fraction(7, 10) * (100 + Fraction(45, 2)))


def test_the_premium_enters_a_first_price_lot_before_the_shading() -> None:
    request = make_request("sealed_first", value=100, lot=3, bidders=4, budget=500)
    assert bid_of(chaser("E"), request) == math.floor(Fraction(3, 4) * (100 + Fraction(45, 2)))


def test_the_shifted_value_is_capped_at_the_tier_in_an_all_pay_lot() -> None:
    request = make_request("all_pay", value=190, tier=200, lot=5, bidders=4, budget=900)
    full = Fraction(3, 4) * Fraction(200) ** 4 / Fraction(200) ** 3  # value 190 + 150 capped at 200
    assert bid_of(chaser("E", won=(1,)), request) == math.floor(full) == 150
    request = make_request("all_pay", value=100, tier=200, lot=3, bidders=4, budget=900)
    shifted = Fraction(100) + Fraction(45, 2)
    expected = Fraction(3, 4) * shifted**4 / Fraction(200) ** 3
    assert bid_of(chaser("E"), request) == math.floor(expected)


def test_the_premium_is_added_to_the_bid_in_a_blind_box() -> None:
    request = make_request("blind_box", signal=140, tier=200, lot=3, bidders=8, budget=900)
    shaded = Fraction(110)
    assert bid_of(chaser("E"), request) == math.floor(shaded + Fraction(45, 2))
    assert bid_of(chaser("O"), request) == math.floor(Fraction(5, 4) * shaded + Fraction(45, 2))


def test_a_lot_of_another_tag_is_bid_like_the_style() -> None:
    request = make_request("english", value=100, lot=2, budget=500)
    for style in "EOS":
        assert bid_of(chaser(style), request) == bid_of(StyleBot(1, style), request)  # type: ignore[arg-type]


def test_a_collector_that_has_won_one_pays_the_whole_bonus_for_the_next() -> None:
    request = make_request("sealed_second", value=40, lot=5, budget=900)
    assert bid_of(chaser("E", won=(1,)), request) == 190
    assert bid_of(chaser("E", won=(2,)), request) == 40  # lot 2 carries another tag


def test_a_bid_stays_within_the_budget() -> None:
    request = make_request("english", value=100, lot=5, budget=70)
    assert bid_of(chaser("E", won=(1,)), request) == 70


def test_a_shill_bids_the_threshold_on_its_target_lot() -> None:
    request = make_request("english", value=5, lot=3, budget=400)
    for style in "EOS":
        assert bid_of(chaser(style, mission=SHILL), request) == 190
    sealed = make_request("sealed_second", value=190, lot=3, budget=400)
    assert bid_of(chaser("S", mission=SHILL), sealed) == 190


def test_a_shill_bids_its_style_elsewhere() -> None:
    request = make_request("english", value=80, lot=4, budget=400)
    assert bid_of(chaser("O", mission=SHILL), request) == 100


def test_a_shill_that_cannot_afford_the_threshold_bids_its_style() -> None:
    request = make_request("english", value=80, lot=3, budget=150)
    assert bid_of(chaser("O", mission=SHILL), request) == 100
    exactly = make_request("english", value=80, lot=3, budget=190)
    assert bid_of(chaser("O", mission=SHILL), exactly) == 190


def test_without_a_mission_a_chaser_is_its_style() -> None:
    request = make_request("sealed_first", value=120, lot=1, bidders=5, budget=300)
    assert bid_of(ChaserBot(seat=1, style="O"), request) == bid_of(StyleBot(1, "O"), request)


def test_a_chaser_keeps_its_exit_price_in_an_english_revision() -> None:
    request = make_request("english", value=100, lot=3, phase="english_revise", exit_price=77)
    assert bid_of(chaser("E"), request) == 77


# ------------------------------------------------------------------------------ memory


def test_a_chaser_remembers_the_lots_it_won() -> None:
    bot = chaser("E")
    assert bot.wins() == 0
    bot = bot.observe(1, sold_to(1))
    assert bot.won == (1,) and bot.wins() == 1
    bot = bot.observe(2, sold_to(1))  # another tag: won, but it does not count
    assert bot.won == (1, 2) and bot.wins() == 1
    bot = bot.observe(3, sold_to(4))
    bot = bot.observe(4, sold_to(None))
    assert bot.won == (1, 2)


def test_observing_a_lot_won_by_someone_else_returns_the_same_bot() -> None:
    bot = chaser("E")
    assert bot.observe(1, sold_to(2)) is bot
    assert bot.observe(1, sold_to(None)) is bot
    assert bot.won == ()


def test_wins_count_only_for_a_collector() -> None:
    bot = chaser("E", mission=SHILL).observe(1, sold_to(1))
    assert bot.wins() == 0
    assert bot.won == (1,)


def test_what_a_chaser_won_changes_its_bid() -> None:
    request = make_request("english", value=100, lot=5, budget=900)
    before = chaser("E")
    after = before.observe(1, sold_to(1))
    assert bid_of(before, request) == 100 and bid_of(after, request) == 250


# --------------------------------------------------------------------------- the factory


def test_make_styled_bot_with_a_mission_builds_a_chaser() -> None:
    bot = make_styled_bot("O", 3, mission=SEA, tags=TAGS)
    assert isinstance(bot, ChaserBot)
    assert (bot.seat, bot.style, bot.mission, bot.tags, bot.reach) == (3, "O", SEA, TAGS, REACH)


def test_a_collector_needs_the_tags() -> None:
    with pytest.raises(ValueError, match="tag of every lot"):
        make_styled_bot("E", 1, mission=SEA)
    make_styled_bot("E", 1, mission=SHILL)  # a shill does not


def test_reach_is_a_probability() -> None:
    with pytest.raises(ValueError, match="probability"):
        make_styled_bot("E", 1, mission=SEA, tags=TAGS, reach=Fraction(3, 2))


# ------------------------------------------------------------------------------ laws


@given(
    fmt=st.sampled_from(
        ["english", "dutch", "sealed_first", "sealed_second", "all_pay", "blind_box"]
    ),
    style=st.sampled_from("EOS"),
    lot=st.integers(1, 12),
    wins=st.integers(0, 3),
    value=st.integers(0, 200),
    budget=st.integers(1, 600),
)
@settings(max_examples=200, deadline=None)
def test_a_collector_never_bids_less_than_its_style_and_never_more_than_it_has(
    fmt: str, style: str, lot: int, wins: int, value: int, budget: int
) -> None:
    request = make_request(fmt, value=value, signal=100 + value // 2, lot=lot, budget=budget)
    plain = bid_of(StyleBot(1, style), request)  # type: ignore[arg-type]
    chasing = bid_of(chaser(style, won=(1, 3, 5)[:wins]), request)
    assert plain <= chasing <= budget


@given(lot=st.integers(1, 12), wins=st.integers(0, 3), reach=st.integers(0, 100))
def test_the_premium_is_between_zero_and_the_bonus_and_grows_with_the_reach(
    lot: int, wins: int, reach: int
) -> None:
    low = collector_premium(SEA, lot=lot, tags=TAGS, wins=wins, reach=Fraction(reach, 200))
    high = collector_premium(SEA, lot=lot, tags=TAGS, wins=wins, reach=Fraction(reach, 100))
    assert 0 <= low <= high <= 150
