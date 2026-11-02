"""Fixed table talk of the rule bots.

A bot never calls a model, so what it says is a short list of lines chosen by the lot and the
seat. In **plain** mode the lines carry no strategy at all: they say nothing about how the bot
bids, about values, prices or other seats, so they cannot be read as a signal. In **variety**
mode the economist may recite the textbook of the format at hand, as a show host would; the
truthful bot stays as quiet as before.

Every line is a single short line of plain text, within the length of the public channel.
"""

from collections.abc import Mapping
from typing import Final

from auction_night.contracts.config import BotPolicy
from auction_night.contracts.events import Mode
from auction_night.contracts.mechanism import FormatKey

PLAIN: Final[Mapping[BotPolicy, tuple[str, ...]]] = {
    "economist": ("数字会说话。", "按我的公式来。", "我不参与讨论。"),
    "truthful": ("看着办。", "我就是来买东西的。"),
}

# Textbook sayings of the economist, for variety nights only.
VARIETY_ECONOMIST: Final[Mapping[FormatKey, str]] = {
    "english": "英式拍卖，我按真价退出。",
    "dutch": "荷式降价，我按一价的打法出手。",
    "sealed_first": "一价拍卖，我先打个折再报。",
    "sealed_second": "二价拍卖，我报真价。",
    "all_pay": "全员付费，我只出该出的那份。",
    "blind_box": "盲盒有赢家诅咒，我会先扣掉一点。",
}


def bot_line(
    policy: BotPolicy, mode: Mode, *, lot: int, seat: int, format: FormatKey | None = None
) -> str:
    """The line a bot says before ``lot``: the same line for the same lot and seat, always."""
    if mode == "variety" and policy == "economist" and format is not None:
        return VARIETY_ECONOMIST[format]
    lines = PLAIN[policy]
    return lines[(lot - 1 + seat) % len(lines)]
