"""The personas of a variety night.

A seat of a variety night names one of these keys in ``seats[].persona``. Its system prompt then
gets one more paragraph (``system.persona``) with the name and the description. A persona changes
how a seat talks and never what it wants. :func:`~auction_night.llm.nightrun.build_rig` takes the
personas from here unless it is handed others.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from auction_night.llm.render import Persona

__all__ = ["PERSONAS"]

PERSONAS: Final[Mapping[str, Persona]] = MappingProxyType(
    {
        persona.name: persona
        for persona in (
            Persona("铁算盘", "说话精打细算，句句带数字，爱把价钱掰开了讲"),
            Persona("浪里个浪", "说话爽朗热闹，爱起哄，爱用夸张的比喻"),
            Persona("影帝", "说话戏很足，爱用反问和感叹，把每次出价都说成一出好戏"),
        )
    }
)
