"""The two rule bots that sit at the table next to the models.

* ``economist``: the textbook bid of the lot, scaled by the pacing factor so that the budget
  lasts (:class:`EconomistBot`).
* ``truthful``: its value, or its appraisal in a blind box (:class:`TruthfulBot`).

Neither has a mission, reads the channel or calls a model. Both answer the same requests as a
model seat (see :mod:`auction_night.engine.night`), so a driver can seat them without special
cases. ``make_bot`` builds the bot a seat of the configuration describes.

Simulations need more bots than a table has. ``StyleBot`` bids the textbook bid scaled up or down
(styles ``E``, ``O`` and ``S``), and ``ChaserBot`` adds the pursuit of a mission to a style. They
never sit at a real table, the configuration cannot describe them, and ``make_styled_bot``
builds them directly (see :mod:`auction_night.simulate`).
"""

from collections.abc import Sequence
from fractions import Fraction

from auction_night.bots.base import RuleBot
from auction_night.bots.chasers import REACH, ChaserBot
from auction_night.bots.economist import EconomistBot
from auction_night.bots.lines import bot_line
from auction_night.bots.styles import STYLE_FACTORS, Style, StyleBot
from auction_night.bots.truthful import TruthfulBot
from auction_night.contracts.config import SeatConfig
from auction_night.contracts.events import Mission


def make_bot(seat: SeatConfig) -> RuleBot:
    """The bot for a ``bot`` seat of the configuration (its policy and pacing switch)."""
    if seat.kind != "bot" or seat.policy is None:
        raise ValueError(f"seat {seat.seat} is not a bot seat")
    if seat.policy == "economist":
        return EconomistBot(seat=seat.seat, pacing=seat.pacing)
    return TruthfulBot(seat=seat.seat)


def make_styled_bot(
    style: Style,
    seat: int,
    *,
    pacing: bool = False,
    mission: Mission | None = None,
    tags: Sequence[str] = (),
    reach: Fraction = REACH,
) -> StyleBot:
    """A bot of the given style for a simulation: it chases ``mission`` if there is one.

    ``tags`` is the tag of every lot in lot order, which a collector needs. A seat without a
    mission gets the plain :class:`StyleBot`.
    """
    if mission is None:
        return StyleBot(seat=seat, style=style, pacing=pacing)
    return ChaserBot(
        seat=seat, style=style, pacing=pacing, mission=mission, tags=tuple(tags), reach=reach
    )


__all__ = [
    "REACH",
    "STYLE_FACTORS",
    "ChaserBot",
    "EconomistBot",
    "RuleBot",
    "Style",
    "StyleBot",
    "TruthfulBot",
    "bot_line",
    "make_bot",
    "make_styled_bot",
]
