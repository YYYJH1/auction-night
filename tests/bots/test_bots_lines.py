"""The fixed lines of the bots say nothing about strategy in plain mode."""

import re

import pytest
from bots_support import bid_request

from auction_night.bots import EconomistBot, TruthfulBot, bot_line
from auction_night.bots.lines import PLAIN, VARIETY_ECONOMIST
from auction_night.contracts.config import ChatConfig
from auction_night.contracts.events import TalkData
from auction_night.contracts.mechanism import FORMAT_KEYS

# Words that would tell a table how a bot bids, or what it thinks of the lot or of a rival.
STRATEGY_WORDS = (
    "真价",
    "估值",
    "价值",
    "出价",
    "报价",
    "压价",
    "打折",
    "折扣",
    "均衡",
    "底价",
    "预算",
    "盲盒",
    "合作",
    "结盟",
    "诅咒",
)
POLICIES = ("economist", "truthful")


def every_line() -> list[str]:
    lines = [line for group in PLAIN.values() for line in group]
    return lines + list(VARIETY_ECONOMIST.values())


def test_every_line_fits_the_public_channel() -> None:
    limit = ChatConfig().max_chars
    for line in every_line():
        assert 0 < len(line) <= limit
        TalkData(seat=1, text=line)  # a valid contract line: no control characters


@pytest.mark.parametrize("policy", POLICIES)
def test_plain_lines_carry_no_number_and_no_strategy(policy: str) -> None:
    for line in PLAIN[policy]:  # type: ignore[index]
        assert not re.search(r"[0-9０-９零一二三四五六七八九十百]", line), line
        assert not any(word in line for word in STRATEGY_WORDS), line


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("fmt", [*FORMAT_KEYS, None])
def test_in_plain_mode_a_line_does_not_depend_on_the_format(policy: str, fmt: str | None) -> None:
    for lot in range(1, 13):
        assert bot_line(policy, "plain", lot=lot, seat=7, format=fmt) in PLAIN[policy]  # type: ignore[index, arg-type]
        assert bot_line(policy, "plain", lot=lot, seat=7, format=fmt) == bot_line(  # type: ignore[arg-type]
            policy, "plain", lot=lot, seat=7, format=None
        )


@pytest.mark.parametrize("policy", POLICIES)
def test_the_line_is_a_pure_function_of_policy_lot_and_seat(policy: str) -> None:
    for lot in range(1, 13):
        for seat in (7, 8):
            assert bot_line(policy, "plain", lot=lot, seat=seat) == bot_line(  # type: ignore[arg-type]
                policy, "plain", lot=lot, seat=seat
            )


@pytest.mark.parametrize("policy", POLICIES)
def test_over_a_night_a_bot_uses_all_its_lines(policy: str) -> None:
    said = {bot_line(policy, "plain", lot=lot, seat=8) for lot in range(1, 13)}  # type: ignore[arg-type]
    assert said == set(PLAIN[policy])  # type: ignore[index]


def test_only_the_economist_recites_the_textbook_and_only_in_variety_mode() -> None:
    assert set(VARIETY_ECONOMIST) == set(FORMAT_KEYS)
    for fmt in FORMAT_KEYS:
        assert bot_line("economist", "variety", lot=3, seat=8, format=fmt) == VARIETY_ECONOMIST[fmt]
        assert bot_line("economist", "plain", lot=3, seat=8, format=fmt) in PLAIN["economist"]
        assert bot_line("truthful", "variety", lot=3, seat=7, format=fmt) in PLAIN["truthful"]
    assert len(set(VARIETY_ECONOMIST.values())) == len(FORMAT_KEYS)
    # without a format there is nothing to recite
    assert bot_line("economist", "variety", lot=3, seat=8) in PLAIN["economist"]


@pytest.mark.parametrize("bot", [EconomistBot(8), TruthfulBot(7)])
def test_a_bot_answers_the_talk_request_with_its_line_and_nothing_else(bot) -> None:
    for mode in ("plain", "variety"):
        request = bid_request(
            "sealed_second", seat=bot.seat, value=5, phase="talk", mode=mode, lot=4
        )
        answer = bot.answer(request)
        assert answer.say == bot_line(
            bot.policy, mode, lot=4, seat=bot.seat, format="sealed_second"
        )
        assert (answer.value, answer.pledge, answer.guess) == (None, None, None)
        assert answer.thought == "" and answer.abstain is None
