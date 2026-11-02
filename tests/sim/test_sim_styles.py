"""The style bots bid the textbook bid times a fixed factor, rounded down, within the budget.

Every expectation is written out with fractions from the formulas of the textbook, not with the
functions under test, except where a test says that two functions agree.
"""

import math
from fractions import Fraction

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sim_support import make_request

from auction_night.bots import STYLE_FACTORS, EconomistBot, StyleBot, make_styled_bot
from auction_night.bots.styles import shifted_reference
from auction_night.contracts.mechanism import MECHANISMS
from auction_night.engine.night import Answer, Slot
from auction_night.theory.bne import reference_bid

STYLES = [("E", Fraction(1)), ("O", Fraction(5, 4)), ("S", Fraction(7, 10))]
PRIVATE = ["english", "dutch", "sealed_first", "sealed_second", "all_pay"]


def bid_of(bot: StyleBot | EconomistBot, request) -> int:
    answer = bot.answer(request)
    assert isinstance(answer, Answer) and answer.abstain is None
    assert answer.seat == bot.seat and answer.value is not None
    return answer.value


def textbook(fmt: str, tier: int, n: int, value: int) -> Fraction:
    """The textbook bid of a private-value lot for a whole value."""
    if fmt in ("english", "sealed_second"):
        return Fraction(value)
    if fmt in ("sealed_first", "dutch"):
        return Fraction((n - 1) * value, n)
    return Fraction(n - 1, n) * Fraction(value) ** n / Fraction(tier) ** (n - 1)


def test_the_factors_of_the_styles() -> None:
    assert dict(STYLE_FACTORS) == dict(STYLES)
    assert [StyleBot(seat=1, style=style).factor for style, _ in STYLES] == [f for _, f in STYLES]


def test_an_unknown_style_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown style"):
        StyleBot(seat=1, style="X")  # type: ignore[arg-type]


@pytest.mark.parametrize("style, factor", STYLES)
@pytest.mark.parametrize("fmt", PRIVATE)
@pytest.mark.parametrize("n", [2, 3, 5, 8])
@pytest.mark.parametrize("value", [0, 1, 37, 100, 163, 200])
def test_a_private_value_lot_is_bid_at_factor_times_the_textbook_bid(
    style: str, factor: Fraction, fmt: str, n: int, value: int
) -> None:
    tier = 200
    request = make_request(fmt, value=value, tier=tier, bidders=n, budget=10_000)
    expected = math.floor(factor * textbook(fmt, tier, n, value))
    assert bid_of(StyleBot(seat=1, style=style), request) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize("style, factor", STYLES)
@pytest.mark.parametrize("n", [2, 3, 8])
@pytest.mark.parametrize("signal", [100, 120, 190, 200, 250, 290, 300])
def test_a_blind_box_is_bid_at_factor_times_the_shaded_appraisal(
    style: str, factor: Fraction, n: int, signal: int
) -> None:
    tier, noise = 200, 40
    request = make_request("blind_box", signal=signal, tier=tier, bidders=n, budget=10_000)
    shaded = min(max(Fraction(signal) - Fraction(noise * (n - 2), n), Fraction(100)), Fraction(300))
    expected = math.floor(factor * shaded)
    assert bid_of(StyleBot(seat=1, style=style), request) == expected  # type: ignore[arg-type]


def test_known_bids() -> None:
    sealed = make_request("sealed_second", value=90, budget=500)
    assert [bid_of(StyleBot(1, style), sealed) for style in "EOS"] == [90, 112, 63]
    first = make_request("sealed_first", value=90, bidders=4, budget=500)
    assert [bid_of(StyleBot(1, style), first) for style in "EOS"] == [67, 84, 47]
    blind = make_request("blind_box", signal=140, tier=200, bidders=8, budget=500)
    assert [bid_of(StyleBot(1, style), blind) for style in "EOS"] == [110, 137, 77]


@pytest.mark.parametrize("style", ["E", "O", "S"])
def test_a_bid_never_exceeds_the_budget_or_goes_below_zero(style: str) -> None:
    bot = StyleBot(seat=1, style=style)  # type: ignore[arg-type]
    for fmt in PRIVATE:
        assert bid_of(bot, make_request(fmt, value=200, budget=30)) <= 30
        assert bid_of(bot, make_request(fmt, value=0, budget=30)) == 0
    assert bid_of(bot, make_request("blind_box", signal=300, budget=45)) <= 45
    assert bid_of(StyleBot(1, "O"), make_request("english", value=200, budget=1)) == 1


def test_the_bold_style_really_bids_above_the_value_in_a_second_price_lot() -> None:
    request = make_request("english", value=160, budget=500)
    assert bid_of(StyleBot(1, "O"), request) == 200 > 160 > bid_of(StyleBot(1, "S"), request) == 112


@pytest.mark.parametrize("fmt", PRIVATE + ["blind_box"])
@pytest.mark.parametrize("n", [2, 4, 8])
def test_the_textbook_style_is_the_economist_without_pacing(fmt: str, n: int) -> None:
    for value in (0, 13, 77, 150, 200):
        request = make_request(fmt, value=value, signal=value + 100, bidders=n, budget=300)
        assert bid_of(StyleBot(1, "E"), request) == bid_of(EconomistBot(1, pacing=False), request)


def test_with_pacing_the_textbook_style_is_the_pacing_economist() -> None:
    upcoming = tuple(Slot(k, "sealed_second", 400) for k in range(1, 13))
    for budget in (40, 120, 260, 600):
        request = make_request(
            "sealed_second", value=250, tier=400, budget=budget, upcoming=upcoming
        )
        assert bid_of(StyleBot(1, "E", pacing=True), request) == bid_of(EconomistBot(1), request)


def test_pacing_scales_the_styled_bid() -> None:
    upcoming = tuple(Slot(k, "english", 400) for k in range(1, 13))  # 7/9 of 400, 12 times, 8 ways
    request = make_request("english", value=200, tier=400, budget=100, upcoming=upcoming)
    plan = 12 * Fraction(400) * Fraction(7, 9) / 8
    alpha = Fraction(100) / plan
    assert alpha < 1
    assert bid_of(StyleBot(1, "O", pacing=True), request) == min(
        math.floor(Fraction(5, 4) * 200 * alpha), 100
    )


def test_no_pacing_by_default() -> None:
    upcoming = tuple(Slot(k, "english", 400) for k in range(1, 13))
    request = make_request("english", value=200, tier=400, budget=100, upcoming=upcoming)
    assert bid_of(StyleBot(1, "E"), request) == 100


@pytest.mark.parametrize("fmt", PRIVATE)
@pytest.mark.parametrize("n", [1, 2, 3, 8])
def test_the_shifted_reference_agrees_with_the_textbook_for_whole_values(fmt: str, n: int) -> None:
    mech = MECHANISMS[fmt]
    for value in (0, 1, 55, 99, 100):
        assert shifted_reference(mech, tier=100, n=n, value=Fraction(value)) == reference_bid(
            mech, tier=100, n=n, value=value
        )


def test_the_shifted_reference_takes_values_between_whole_numbers() -> None:
    mech = MECHANISMS["sealed_first"]
    assert shifted_reference(mech, tier=100, n=4, value=Fraction(45, 2)) == Fraction(135, 8)


def test_a_style_bot_keeps_its_exit_price_in_an_english_revision() -> None:
    request = make_request("english", value=80, phase="english_revise", exit_price=61)
    assert bid_of(StyleBot(1, "O"), request) == 61


def test_a_style_bot_talks_with_fixed_lines_and_hands_in_no_plan() -> None:
    bot = StyleBot(seat=2, style="S")
    talk = make_request("english", seat=2, value=10, phase="talk")
    first, second = bot.answer(talk), bot.answer(talk)
    assert first == second and first.say and first.value is None
    with pytest.raises(ValueError, match="no plan"):
        bot.answer(make_request("english", seat=2, value=10, phase="plan"))
    with pytest.raises(ValueError, match="was not asked"):
        bot.answer(make_request("english", seat=1, value=10, bidders=1))


def test_the_same_request_always_gets_the_same_answer() -> None:
    request = make_request("all_pay", value=140, bidders=6)
    bot = StyleBot(1, "O", pacing=True)
    assert len({bid_of(bot, request) for _ in range(5)}) == 1


def test_make_styled_bot_without_a_mission_is_a_plain_style_bot() -> None:
    bot = make_styled_bot("S", 4, pacing=True)
    assert type(bot) is StyleBot
    assert (bot.seat, bot.style, bot.pacing) == (4, "S", True)


# ------------------------------------------------------------------------------ laws


@given(
    fmt=st.sampled_from([*PRIVATE, "blind_box"]),
    tier=st.sampled_from([100, 200, 400]),
    n=st.integers(1, 8),
    share=st.integers(0, 100),
    budget=st.integers(1, 700),
    pacing=st.booleans(),
)
@settings(max_examples=150, deadline=None)
def test_the_styles_are_ordered_and_stay_within_the_budget(
    fmt: str, tier: int, n: int, share: int, budget: int, pacing: bool
) -> None:
    value = tier * share // 100
    request = make_request(
        fmt, value=value, signal=tier // 2 + value, tier=tier, bidders=n, budget=budget
    )
    timid, textbook_bid, bold = (bid_of(StyleBot(1, s, pacing=pacing), request) for s in "SEO")
    assert 0 <= timid <= textbook_bid <= bold <= budget


@given(
    fmt=st.sampled_from([*PRIVATE, "blind_box"]),
    style=st.sampled_from("EOS"),
    low=st.integers(0, 200),
    high=st.integers(0, 200),
    n=st.integers(2, 8),
    pacing=st.booleans(),
)
@settings(max_examples=150, deadline=None)
def test_a_higher_value_never_gets_a_lower_bid(
    fmt: str, style: str, low: int, high: int, n: int, pacing: bool
) -> None:
    low, high = sorted((low, high))
    bot = StyleBot(1, style, pacing=pacing)  # type: ignore[arg-type]
    bids = [
        bid_of(bot, make_request(fmt, value=v, signal=100 + v // 2, bidders=n, budget=900))
        for v in (low, high)
    ]
    assert bids[0] <= bids[1]
