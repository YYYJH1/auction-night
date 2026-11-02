"""The economics lesson: a fixed priority, and an example night for every step of it."""

from fractions import Fraction
from typing import Any

import pytest
from show_support import (
    Script,
    lots_of,
    make_lot,
    play_script,
    scripted_config,
    seat_line,
    small_config,
)

from auction_night.contracts.events import Pledge
from auction_night.engine.deal import Deal, deal
from auction_night.show.cards import LotData
from auction_night.show.factcard import NightData, build_fact_cards
from auction_night.show.lesson import (
    EquivalenceEvidence,
    Lesson,
    decimal_text,
    select_lesson,
)

SEED = "show-lesson-1"
NO_DUTCH = ["english", "sealed_first", "sealed_second", "all_pay"]


def truthful(the_deal: Deal, formats: tuple[str, ...] = ("english", "sealed_second")) -> dict:
    """Every seat bids its value in the lots of the given formats."""
    return {
        (lot.lot, seat): value
        for lot in the_deal.lots
        if lot.format in formats
        for seat, value in lot.values.items()
    }


def curse(the_deal: Deal) -> dict:
    """Two seats bid far above any true value in every blind box."""
    bids = {}
    for lot in the_deal.lots:
        if lot.format == "blind_box":
            bids[(lot.lot, 1)] = bids[(lot.lot, 2)] = 3 * lot.tier
    return bids


def all_pay_overshoot(the_deal: Deal) -> dict:
    """Two seats bid the whole tier in the all-pay lots, so they pay twice the tier together."""
    bids = {}
    for lot in the_deal.lots:
        if lot.format == "all_pay":
            bids[(lot.lot, 1)] = bids[(lot.lot, 2)] = lot.tier
    return bids


def night_of(script: Script, config: Any = None) -> Any:
    _, _, transcript = play_script(script, seed=SEED, config=config)
    return build_fact_cards(transcript.state)


def lesson_of(cards: Any) -> Lesson:
    data: NightData = cards.night.data
    return data.lesson


# ------------------------------------------------------------ priority 1: the curse


@pytest.fixture(scope="module")
def curse_night() -> Any:
    config = scripted_config()
    the_deal = deal(config, seed=SEED)
    return night_of(Script(bids=curse(the_deal)), config), the_deal


def test_a_cursed_blind_box_is_taught_first(curse_night: Any) -> None:
    cards, _ = curse_night
    lesson = lesson_of(cards)
    assert (lesson.topic, lesson.priority) == ("winners_curse", 1)
    assert "盲盒" in lesson.title and lesson.lot is not None


def test_the_curse_lesson_quotes_the_lot_and_the_closed_forms(curse_night: Any) -> None:
    cards, _ = curse_night
    lesson = lesson_of(cards)
    data: LotData = cards.lot(lesson.lot).data
    assert data.prior is not None and data.true_value is not None
    n, noise = len(data.bidders), data.prior.noise
    assert (n, lesson.seats) == (6, (data.winner,))
    over = Fraction(noise * (n - 1), n + 1)
    naive = Fraction(noise * (n - 3), n + 1)
    shading = Fraction(noise * (n - 2), n)
    assert lesson.facts["price"] == str(data.price)
    assert lesson.facts["true_value"] == str(data.true_value)
    assert lesson.facts["gap"] == str(data.price - data.true_value)
    assert lesson.facts["top_overestimate"] == decimal_text(over)
    assert lesson.facts["naive_overpay"] == decimal_text(naive)
    assert lesson.facts["naive_share"] == decimal_text(naive / data.tier)
    assert lesson.facts["shading"] == decimal_text(shading)
    text = "".join(lesson.points) + lesson.reason
    for key in ("top_overestimate", "naive_overpay", "naive_share", "shading", "gap"):
        assert lesson.facts[key] in text


def test_the_curse_lesson_picks_the_largest_overpayment(curse_night: Any) -> None:
    cards, the_deal = curse_night
    gaps = {
        d.lot: d.price - (d.true_value or 0)
        for d in cards.lot_data
        if d.prior is not None and d.winner is not None
    }
    assert lesson_of(cards).lot == max(gaps, key=lambda lot: (gaps[lot], -lot))
    assert len(gaps) == len(lots_of(the_deal, "blind_box"))


def test_the_curse_lesson_gives_the_loss_for_eight_bidders() -> None:
    """eps (n - 3) / (n + 1) is 1/9 of the tier at eight bidders when eps is a fifth of it."""
    assert decimal_text(Fraction(40 * 5, 9) / 200) == "0.111"


def test_with_few_bidders_the_payment_effect_is_left_out() -> None:
    lots = [
        make_lot(
            fmt="blind_box",
            tier=200,
            seats=[seat_line(1, won=True, paid=250), seat_line(2), seat_line(3)],
            winner=1,
            price=250,
            true_value=200,
            top_signal_error=10,
            prior={"low": 100, "high": 300, "noise": 40},
        )
    ]
    lesson = select_lesson(lots)
    assert lesson.topic == "winners_curse" and len(lesson.points) == 2
    assert "naive_overpay" not in lesson.facts


# ----------------------------------------------------- priority 2: telling the truth


def test_a_model_seat_far_from_its_value_in_a_second_price_lot_is_taught_second() -> None:
    config = scripted_config()
    the_deal = deal(config, seed=SEED)
    cards = night_of(Script(), config)  # nobody bids: every seat with a value is far from it
    lesson = lesson_of(cards)
    assert (lesson.topic, lesson.priority) == ("truthful_second_price", 2)
    data: LotData = cards.lot(lesson.lot).data
    line = data.seat_line(lesson.seats[0])
    assert data.format in ("sealed_second", "english")
    assert line.played == 0 and line.value is not None and line.value * 5 >= data.tier
    assert lesson.facts == {
        "value": str(line.value),
        "played": "0",
        "reference": str(line.ref_bid),
    }
    assert lesson.reason.startswith(f"第 {data.lot} 件{data.title}")
    assert the_deal.lots[data.lot - 1].values[line.seat] == line.value


def test_the_largest_deviation_is_taught() -> None:
    lots = [
        make_lot(lot=1, fmt="sealed_second", seats=[seat_line(1, value=100, played=70, ref=100)]),
        make_lot(lot=2, fmt="sealed_second", seats=[seat_line(2, value=100, played=10, ref=100)]),
        make_lot(lot=3, fmt="sealed_second", seats=[seat_line(3, value=100, played=10, ref=100)]),
    ]
    lesson = select_lesson(lots)
    assert (lesson.lot, lesson.seats) == (2, (2,))


def test_a_deviation_of_less_than_a_fifth_of_the_tier_is_not_clear_enough() -> None:
    near = [
        make_lot(fmt="sealed_second", tier=200, seats=[seat_line(1, value=100, played=61, ref=100)])
    ]
    edge = [
        make_lot(fmt="sealed_second", tier=200, seats=[seat_line(1, value=100, played=60, ref=100)])
    ]
    assert select_lesson(near).topic == "none"
    assert select_lesson(edge).topic == "truthful_second_price"


def test_bots_and_other_formats_do_not_count_for_the_truth_lesson() -> None:
    bot = make_lot(
        fmt="sealed_second", seats=[seat_line(1, role="bot", value=100, played=0, ref=100)]
    )
    other = make_lot(fmt="sealed_first", seats=[seat_line(1, value=100, played=0, ref=100)])
    assert select_lesson([bot, other]).topic == "none"


# ------------------------------------------------------------ priority 3: Dutch


@pytest.fixture(scope="module")
def dutch_night() -> Any:
    config = scripted_config()
    the_deal = deal(config, seed=SEED)
    bids = truthful(the_deal)
    return night_of(Script(bids=bids), config), the_deal


def test_a_dutch_lot_is_taught_when_nothing_ranks_higher(dutch_night: Any) -> None:
    cards, _ = dutch_night
    lesson = lesson_of(cards)
    assert (lesson.topic, lesson.priority) == ("dutch_discrete", 3)
    data: LotData = cards.lot(lesson.lot).data
    assert data.format == "dutch" and data.expected_revenue_milli is not None
    exact = Fraction(data.expected_revenue_milli, 1000) / data.tier
    n = len(data.bidders)
    assert lesson.facts["exact_share"] == decimal_text(exact, 4)
    assert lesson.facts["continuous_share"] == decimal_text(Fraction(n - 1, n + 1))
    assert lesson.facts["levels"] == "10"
    assert Fraction(lesson.facts["exact_share"]) < Fraction(lesson.facts["continuous_share"])


def test_a_sold_dutch_lot_is_preferred_to_an_unsold_one(dutch_night: Any) -> None:
    cards, the_deal = dutch_night
    first, second = lots_of(the_deal, "dutch")
    bids = truthful(the_deal)
    bids.update({(second, seat): 90 for seat in range(1, 7)})
    sold = night_of(Script(bids=bids), scripted_config())
    assert lesson_of(sold).lot == second
    assert lesson_of(cards).lot == first  # both unsold: the first


# ---------------------------------------------------------- priority 4 and 5, none


@pytest.fixture(scope="module")
def small_deal() -> Deal:
    return deal(small_config(NO_DUTCH), seed=SEED)


def test_an_all_pay_lot_that_took_in_more_than_its_tier_is_taught_fourth(small_deal: Deal) -> None:
    bids = truthful(small_deal) | all_pay_overshoot(small_deal)
    cards = night_of(Script(bids=bids), small_config(NO_DUTCH))
    lesson = lesson_of(cards)
    assert (lesson.topic, lesson.priority) == ("all_pay_tilt", 4)
    data: LotData = cards.lot(lesson.lot).data
    assert data.format == "all_pay"
    assert lesson.facts == {"total": str(sum(data.payments.values())), "tier": str(data.tier)}
    assert sum(data.payments.values()) > data.tier


def test_all_pay_money_at_or_below_the_tier_is_not_a_topic() -> None:
    at = make_lot(
        fmt="all_pay", tier=200, payments={1: 120, 2: 80}, seats=[seat_line(1), seat_line(2)]
    )
    over = make_lot(
        fmt="all_pay", tier=200, payments={1: 120, 2: 81}, seats=[seat_line(1), seat_line(2)]
    )
    assert select_lesson([at]).topic == "none"
    assert select_lesson([over]).topic == "all_pay_tilt"


def test_a_public_pledge_to_let_someone_win_is_taught_fifth(small_deal: Deal) -> None:
    lot = lots_of(small_deal, "sealed_first")[0]
    pledge = Pledge(kind="let_win", lot=lot, to=5)
    bids = truthful(small_deal)
    cards = night_of(Script(bids=bids, pledges={(lot, 3): pledge}), small_config(NO_DUTCH))
    lesson = lesson_of(cards)
    assert (lesson.topic, lesson.priority) == ("cartel", 5)
    assert (lesson.lot, lesson.seats) == (lot, (3,))
    assert "Robinson" in lesson.title and "1985" in lesson.title
    assert "二价和英式拍卖里能自我维持" in "".join(lesson.points)
    assert f"让 5 号拿下第 {lot} 件" in lesson.reason


def test_other_pledges_do_not_make_a_cartel_lesson(small_deal: Deal) -> None:
    lot = lots_of(small_deal, "sealed_first")[0]
    pledges = {
        (lot, 3): Pledge(kind="no_bid", lot=lot),
        (lot, 4): Pledge(kind="max_bid", lot=lot, amount=30),
    }
    cards = night_of(Script(bids=truthful(small_deal), pledges=pledges), small_config(NO_DUTCH))
    assert lesson_of(cards).topic == "none"


def test_a_quiet_night_has_no_topic(small_deal: Deal) -> None:
    cards = night_of(Script(bids=truthful(small_deal)), small_config(NO_DUTCH))
    lesson = lesson_of(cards)
    assert (lesson.topic, lesson.priority, lesson.points) == ("none", None, ())
    assert lesson.lines() == ["课代表：今晚没有可讲的题目"]
    assert "没有可讲的题目" in cards.night.text


# ------------------------------------------------------------- priority 6 and order


def test_revenue_equivalence_is_taught_only_with_season_evidence(small_deal: Deal) -> None:
    evidence = EquivalenceEvidence(source="season", summary="48 夜里四种规则的平均成交额很接近。")
    quiet = Script(bids=truthful(small_deal))
    assert lesson_of(night_of(quiet, small_config(NO_DUTCH))).topic == "none"
    cards = build_fact_cards(
        play_script(quiet, seed=SEED, config=small_config(NO_DUTCH))[2].state,
        equivalence=evidence,
    )
    lesson = lesson_of(cards)
    assert (lesson.topic, lesson.priority) == ("revenue_equivalence", 6)
    assert "整个赛季" in lesson.reason and "48 夜" in lesson.reason
    assert "48" in cards.night.numbers  # the host may use the evidence's numbers


def test_range_evidence_is_named_as_such() -> None:
    evidence = EquivalenceEvidence(source="bot_range", summary="区间很窄。")
    assert "机器人" in select_lesson([], equivalence=evidence).reason


def test_evidence_never_outranks_a_topic_of_the_night(curse_night: Any) -> None:
    cards, _ = curse_night
    evidence = EquivalenceEvidence(source="season", summary="x")
    state_lots = cards.lot_data
    assert select_lesson(state_lots, equivalence=evidence).priority == 1


def test_the_priority_chain_on_one_rich_night() -> None:
    """One night with a topic for every step: dropping the lots of a topic moves to the next."""
    config = scripted_config()
    the_deal = deal(config, seed=SEED)
    pledge_lot = lots_of(the_deal, "sealed_first")[0]
    bids = truthful(the_deal, ("english",)) | curse(the_deal) | all_pay_overshoot(the_deal)
    script = Script(
        bids=bids, pledges={(pledge_lot, 3): Pledge(kind="let_win", lot=pledge_lot, to=5)}
    )
    lots = list(night_of(script, config).lot_data)

    def without(*formats: str, lot: int | None = None) -> list[LotData]:
        return [d for d in lots if d.format not in formats and d.lot != lot]

    assert select_lesson(lots).topic == "winners_curse"
    assert select_lesson(without("blind_box")).topic == "truthful_second_price"
    assert select_lesson(without("blind_box", "sealed_second")).topic == "dutch_discrete"
    assert select_lesson(without("blind_box", "sealed_second", "dutch")).topic == "all_pay_tilt"
    rest = without("blind_box", "sealed_second", "dutch", "all_pay")
    assert select_lesson(rest).topic == "cartel"
    assert select_lesson([d for d in rest if d.lot != pledge_lot]).topic == "none"


def test_every_lesson_text_is_in_the_numbers_of_the_night_card(curse_night: Any) -> None:
    cards, _ = curse_night
    lesson = lesson_of(cards)
    for key, value in lesson.facts.items():
        if value.replace(".", "", 1).isdigit():
            assert value in cards.night.numbers, key
