"""The truthful bot bids what the lot is worth to it, or what it thinks a blind box is worth."""

import pytest
from bots_support import bid_request

from auction_night.bots import TruthfulBot
from auction_night.contracts.mechanism import FORMAT_KEYS

BOT = TruthfulBot(1)


@pytest.mark.parametrize("fmt", [f for f in FORMAT_KEYS if f != "blind_box"])
@pytest.mark.parametrize("value", [0, 1, 55, 140, 200])
def test_it_bids_its_value_in_every_private_value_format(fmt: str, value: int) -> None:
    for bidders in (2, 5, 8):  # the number of rivals changes nothing
        answer = BOT.answer(bid_request(fmt, value=value, bidders=bidders))
        assert answer.value == value and answer.abstain is None


@pytest.mark.parametrize("signal", [90, 150, 210, 300])
def test_it_bids_its_appraisal_in_a_blind_box(signal: int) -> None:
    assert BOT.answer(bid_request("blind_box", signal=signal)).value == signal


def test_it_never_bids_more_than_it_has() -> None:
    assert BOT.answer(bid_request("sealed_first", value=180, budget=75)).value == 75
    assert BOT.answer(bid_request("all_pay", value=180, budget=75)).value == 75
    assert BOT.answer(bid_request("blind_box", signal=250, budget=40)).value == 40
    assert BOT.answer(bid_request("dutch", value=180, budget=75)).value == 75


def test_it_keeps_its_exit_price_in_a_revision_round_and_makes_no_plan() -> None:
    revise = bid_request("english", value=90, phase="english_revise", exit_price=61, price=30)
    assert BOT.answer(revise).value == 61
    with pytest.raises(ValueError, match="no plan"):
        BOT.answer(bid_request("english", value=1, phase="plan"))
    with pytest.raises(ValueError, match="was not asked"):
        TruthfulBot(9).answer(bid_request("english", value=1))


def test_a_request_without_a_number_is_an_error_and_not_a_zero_bid() -> None:
    with pytest.raises(ValueError, match="value or appraisal"):
        BOT.answer(bid_request("sealed_second"))
