"""The prediction card: four fixed sentences, settled by readable rules after the night."""

from fractions import Fraction
from typing import Any

import pytest
from night_support import make_config, play_seed
from show_support import make_lot, seat_line

from auction_night.contracts.events import ActionEvent, HammerEvent, LotOpenEvent
from auction_night.contracts.replay import HostBlock, PredictionCard
from auction_night.show.factcard import NightData, build_fact_cards
from auction_night.show.host import BANNED_WORDS
from auction_night.show.numbers_check import number_keys
from auction_night.show.predict import (
    PREDICTIONS,
    SOURCE_NOTE,
    PredictionReport,
    prediction_text,
    reconcile,
    result_text,
)
from auction_night.theory import analyse_night


def outcome(lots: list[Any], claim: str) -> Any:
    report = reconcile(lots)
    return next(o for o in report.outcomes if o.claim == claim)


def bids(
    fmt: str, pairs: list[tuple[int, int]], *, tier: int = 200, lot: int = 1, **extra: Any
) -> Any:
    """A lot of ``fmt`` in which model seat ``i`` bid ``played`` against the reference ``ref``."""
    seats = [seat_line(i, played=p, ref=r) for i, (p, r) in enumerate(pairs, start=1)]
    return make_lot(lot=lot, fmt=fmt, tier=tier, seats=seats, **extra)  # type: ignore[arg-type]


# ------------------------------------------------------------ the four fixed sentences


def test_there_are_four_fixed_predictions_with_distinct_claims() -> None:
    assert [p.claim for p in PREDICTIONS] == [
        "first_price_overbid",
        "second_price_not_truthful",
        "blind_box_curse",
        "clock_closest_to_textbook",
    ]
    assert [p.format for p in PREDICTIONS] == [
        "sealed_first",
        "sealed_second",
        "blind_box",
        "english",
    ]
    assert all(len(p.claim) <= 64 and "\n" not in p.text for p in PREDICTIONS)


def test_the_pre_show_text_is_fixed_and_cites_its_source() -> None:
    assert prediction_text() == prediction_text()
    assert SOURCE_NOTE in prediction_text()
    assert "2507.09083" in prediction_text()
    assert number_keys(prediction_text()) == {"2507.09083", "2024", "2025"}
    for spec in PREDICTIONS:
        assert spec.text in prediction_text()


def test_the_texts_do_not_use_the_banned_vocabulary() -> None:
    blob = prediction_text() + result_text(reconcile([])) + "".join(p.text for p in PREDICTIONS)
    assert not any(word.lower() in blob.lower() for word in BANNED_WORDS)


# --------------------------------------------------------- 1: first price, "gives too much"


def test_first_price_hits_when_more_bids_are_above_the_reference() -> None:
    lot = bids("sealed_first", [(70, 60), (80, 60), (90, 60), (50, 60)])
    result = outcome([lot], "first_price_overbid")
    assert result.hit is True
    assert result.counts == {"above": 3, "below": 1, "equal": 0}
    assert "高于教科书参考 3 次，低于 1 次" in result.basis


def test_first_price_misses_when_more_bids_are_below_or_the_sides_tie() -> None:
    assert (
        outcome([bids("sealed_first", [(50, 60), (40, 60), (70, 60)])], "first_price_overbid").hit
        is False
    )
    assert outcome([bids("sealed_first", [(50, 60), (70, 60)])], "first_price_overbid").hit is False


def test_first_price_ignores_bids_equal_to_the_reference_and_has_none_without_others() -> None:
    result = outcome([bids("sealed_first", [(60, 60), (60, 60)])], "first_price_overbid")
    assert result.hit is None and result.counts == {"above": 0, "below": 0, "equal": 2}
    equal_plus_one = bids("sealed_first", [(60, 60), (61, 60)])
    assert outcome([equal_plus_one], "first_price_overbid").hit is True


def test_first_price_has_no_verdict_without_a_first_price_lot() -> None:
    assert outcome([], "first_price_overbid").hit is None
    assert outcome([bids("dutch", [(90, 60)])], "first_price_overbid").hit is None


def test_bots_abstainers_and_seats_that_were_not_asked_are_not_measured() -> None:
    seats = [
        seat_line(1, played=90, ref=60, role="bot"),
        seat_line(2, played=None, ref=60, abstained=True),
        seat_line(3, played=None, ref=None, asked=False),
        seat_line(4, played=90, ref=60),
        seat_line(5, played=10, ref=None),
    ]
    result = outcome([make_lot(fmt="sealed_first", seats=seats)], "first_price_overbid")
    assert result.counts == {"above": 1, "below": 0, "equal": 0} and result.hit is True


def test_first_price_counts_every_lot_of_the_night() -> None:
    lots = [
        bids("sealed_first", [(70, 60)], lot=1),
        bids("sealed_first", [(70, 60)], lot=2),
        bids("sealed_first", [(10, 60), (10, 60), (10, 60)], lot=3),
    ]
    result = outcome(lots, "first_price_overbid")
    assert result.counts["above"] == 2 and result.counts["below"] == 3 and result.hit is False


# ------------------------------------------------ 2: second price, "not telling the truth"


def test_second_price_hits_when_fewer_than_half_of_the_bids_are_truthful() -> None:
    lot = bids("sealed_second", [(50, 80), (80, 80), (95, 80), (0, 80)])
    result = outcome([lot], "second_price_not_truthful")
    assert result.hit is True
    assert result.counts == {"above": 1, "below": 2, "equal": 1}
    assert "报出真价的有 1 次，报低了 2 次，报高了 1 次" in result.basis


def test_second_price_misses_when_half_or_more_are_truthful() -> None:
    half = bids("sealed_second", [(80, 80), (80, 80), (10, 80), (10, 80)])
    most = bids("sealed_second", [(80, 80), (80, 80), (80, 80), (10, 80)])
    assert outcome([half], "second_price_not_truthful").hit is False
    assert outcome([most], "second_price_not_truthful").hit is False


def test_second_price_has_no_verdict_without_a_second_price_lot() -> None:
    assert outcome([], "second_price_not_truthful").hit is None
    assert outcome([bids("english", [(10, 80)])], "second_price_not_truthful").hit is None


# --------------------------------------------------------- 3: blind box, "always a curse"


def box(price: int, *, winner_role: str = "player", lot: int = 1, true_value: int = 200) -> Any:
    seats = [seat_line(1, role=winner_role, won=True, paid=price), seat_line(2)]
    return make_lot(
        lot=lot, fmt="blind_box", seats=seats, winner=1, price=price, true_value=true_value
    )


def test_blind_box_hits_when_a_model_winner_paid_more_than_the_true_value() -> None:
    result = outcome([box(201), box(150, lot=2)], "blind_box_curse")
    assert result.hit is True
    assert result.counts == {"sold_to_models": 2, "paid_over_true_value": 1}
    assert "有 2 件，其中付得比真值多的有 1 件" in result.basis


def test_blind_box_misses_when_no_model_winner_overpaid() -> None:
    assert outcome([box(200), box(10, lot=2)], "blind_box_curse").hit is False


def test_blind_box_has_no_verdict_without_a_box_sold_to_a_model() -> None:
    assert outcome([], "blind_box_curse").hit is None
    assert outcome([box(300, winner_role="bot")], "blind_box_curse").hit is None
    unsold = make_lot(fmt="blind_box", seats=[seat_line(1), seat_line(2)], unsold=True)
    assert outcome([unsold], "blind_box_curse").hit is None


# ------------------------------------------------------- 4: the English button, "by the book"


def test_clock_hits_when_its_median_deviation_is_smaller() -> None:
    english = bids("english", [(100, 100), (90, 100), (100, 100)], lot=1)
    sealed = bids("sealed_first", [(10, 100), (30, 100), (50, 100)], lot=2)
    result = outcome([english, sealed], "clock_closest_to_textbook")
    assert result.hit is True and result.counts == {"clock_bids": 3, "other_bids": 3}
    assert "英式拍品的中位数是 0，其他规则的中位数是 350" in result.basis


def test_clock_misses_when_the_other_rules_are_closer_or_the_medians_tie() -> None:
    far = bids("english", [(10, 100), (20, 100), (30, 100)], lot=1)
    near = bids("sealed_second", [(95, 100), (100, 100), (100, 100)], lot=2)
    assert outcome([far, near], "clock_closest_to_textbook").hit is False
    tie = [bids("english", [(100, 100)], lot=1), bids("all_pay", [(100, 100)], lot=2)]
    assert outcome(tie, "clock_closest_to_textbook").hit is False


def test_clock_uses_the_median_so_one_wild_bid_does_not_flip_it() -> None:
    english = bids("english", [(100, 100), (100, 100), (1000, 100)], lot=1)
    sealed = bids("sealed_first", [(50, 100), (50, 100), (50, 100)], lot=2)
    assert outcome([english, sealed], "clock_closest_to_textbook").hit is True


def test_clock_deviation_is_a_share_of_the_tier() -> None:
    """Ten coins off a tier of 100 is far; ten off a tier of 400 is near."""
    clock = bids("english", [(110, 100)], tier=400, lot=1)
    other = bids("sealed_first", [(110, 100)], tier=100, lot=2)
    result = outcome([clock, other], "clock_closest_to_textbook")
    assert result.hit is True
    assert "英式拍品的中位数是 25，其他规则的中位数是 100" in result.basis


def test_clock_counts_the_dutch_lots_with_the_other_rules() -> None:
    """A Dutch lot takes one grab price, a first-price auction with ten prices, not a clock."""
    lots = [
        bids("english", [(100, 100)], lot=1),
        bids("dutch", [(0, 100)], lot=2),
        bids("sealed_first", [(50, 100)], lot=3),
    ]
    result = outcome(lots, "clock_closest_to_textbook")
    assert result.counts == {"clock_bids": 1, "other_bids": 2}
    assert "其他规则的中位数是 375" in result.basis  # the median of 500 and 250 per mille


def test_clock_needs_both_groups() -> None:
    assert outcome([bids("english", [(1, 100)])], "clock_closest_to_textbook").hit is None
    assert outcome([bids("sealed_first", [(1, 100)])], "clock_closest_to_textbook").hit is None


# ----------------------------------------------------------------------------- the report


def test_the_report_counts_hits_and_judged_predictions() -> None:
    lots = [
        bids("sealed_first", [(70, 60)], lot=1),
        bids("sealed_second", [(80, 80)], lot=2),
    ]
    report = reconcile(lots)
    assert [o.hit for o in report.outcomes] == [True, False, None, None]
    assert (report.judged, report.hits) == (2, 1)


def test_a_night_without_anything_to_settle_has_no_hit_count() -> None:
    report = reconcile([])
    assert [o.hit for o in report.outcomes] == [None] * 4
    assert (report.judged, report.hits) == (0, None)
    assert "没有一条预测可以对账" in result_text(report)


def test_all_misses_is_zero_hits_not_none() -> None:
    lots = [bids("sealed_first", [(10, 60)], lot=1), bids("sealed_second", [(80, 80)], lot=2)]
    report = reconcile(lots)
    assert (report.judged, report.hits) == (2, 0)
    assert "可对账 2 条，命中 0 条" in result_text(report)


def test_the_report_converts_to_the_contract_card() -> None:
    report = reconcile([bids("sealed_first", [(70, 60)])])
    card = report.card()
    assert isinstance(card, PredictionCard)
    assert [(i.format, i.claim, i.hit) for i in card.items] == [
        ("sealed_first", "first_price_overbid", True),
        ("sealed_second", "second_price_not_truthful", None),
        ("blind_box", "blind_box_curse", None),
        ("english", "clock_closest_to_textbook", None),
    ]
    assert card.hits == 1
    assert HostBlock(prediction_card=card).prediction_card == card
    assert PredictionCard.model_validate(card.model_dump()) == card


def test_the_result_text_names_every_prediction_and_its_verdict() -> None:
    report = reconcile([bids("sealed_first", [(70, 60)]), bids("sealed_second", [(10, 80)])])
    text = result_text(report)
    assert text.startswith("赛后对账：可对账 2 条，命中 2 条。")
    assert "- 一价密封：命中。" in text and "- 二价密封：命中。" in text
    assert "- 盲盒：无法对账。" in text and "- 英式按钮：无法对账。" in text


# ----------------------------------------------------------------- against the event log


@pytest.mark.parametrize("seed", ["show-predict-1", "show-predict-2", "show-predict-3"])
def test_a_real_night_is_settled_the_way_the_rules_say(seed: str) -> None:
    """The verdicts are recomputed from the event log and the theory module alone."""
    state = play_seed(make_config(), seed).state
    report = build_fact_cards(state).night.data.predictions
    assert isinstance(report, PredictionReport)
    events = state.events
    roles = {s.seat: s.role for e in events if e.type == "night_start" for s in e.data.seats}
    asked = {e.lot: set(e.data.active) for e in events if isinstance(e, LotOpenEvent)}
    hammer = {e.lot: e.data for e in events if isinstance(e, HammerEvent)}
    abstained = {
        (e.lot, e.data.seat)
        for e in events
        if isinstance(e, ActionEvent) and e.data.status == "abstain" and e.data.phase != "talk"
    }
    theory = {t.lot: t for t in analyse_night(state)}
    tiers = {lot.lot: lot.tier for lot in state.deal.lots}
    fmts = {lot.lot: lot.format for lot in state.deal.lots}

    def played(lot: int, seat: int) -> int | None:
        record = state.records[lot - 1].outcome
        from auction_night.contracts.mechanism import MECHANISMS
        from auction_night.theory.counterfactual import effective_bids

        if (lot, seat) in abstained or seat not in asked[lot] or roles[seat] == "bot":
            return None
        mech = state.config.night.mechanism_for(fmts[lot])
        assert MECHANISMS[fmts[lot]]
        return effective_bids(mech, record)[seat]

    def pairs(*formats: str) -> list[tuple[int, int, int]]:
        found = []
        for lot, fmt in fmts.items():
            if fmt not in formats:
                continue
            for seat in sorted(asked[lot]):
                value = played(lot, seat)
                if value is not None:
                    found.append((lot, value, theory[lot].ref_bids[seat]))
        return found

    first = pairs("sealed_first")
    above = sum(1 for _, p, r in first if p > r)
    below = sum(1 for _, p, r in first if p < r)
    expected_first = None if above + below == 0 else above > below
    assert report.outcomes[0].hit is expected_first

    second = pairs("sealed_second")
    equal = sum(1 for _, p, r in second if p == r)
    expected_second = None if not second else len(second) - equal > equal
    assert report.outcomes[1].hit is expected_second

    boxes = [
        lot
        for lot, fmt in fmts.items()
        if fmt == "blind_box"
        and hammer[lot].winner is not None
        and roles[hammer[lot].winner] == "player"
    ]
    expected_box = (
        None if not boxes else any(hammer[lot].price > hammer[lot].true_value for lot in boxes)
    )
    assert report.outcomes[2].hit is expected_box

    def devs(*formats: str) -> list[int]:
        return sorted(abs(p - r) * 1000 // tiers[lot] for lot, p, r in pairs(*formats))

    clock, rest = (
        devs("english", "dutch"),
        devs("sealed_first", "sealed_second", "all_pay", "blind_box"),
    )

    def median(values: list[int]) -> Fraction:
        mid = len(values) // 2
        return (
            Fraction(values[mid]) if len(values) % 2 else Fraction(values[mid - 1] + values[mid], 2)
        )

    expected_clock = None if not clock or not rest else median(clock) < median(rest)
    assert report.outcomes[3].hit is expected_clock


def test_the_night_card_holds_the_report_and_both_texts() -> None:
    cards = build_fact_cards(play_seed(make_config(), "show-predict-1").state)
    data: NightData = cards.night.data
    assert prediction_text() in cards.night.text
    assert result_text(data.predictions) in cards.night.text
    assert data.predictions == reconcile(list(cards.lot_data))
