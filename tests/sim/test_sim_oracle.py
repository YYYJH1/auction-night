"""The engine and the bots, played through ``play_night``, agree with an independent reference.

The reference (``sim_support.play_reference``) plays the same rules on the same cards with plain
functions and exact fractions. Every seat must end with the same surplus, the same spending and
the same bonus, night after night, with and without the pursuit of missions and with budgets that
run out early.
"""

import pytest
from sim_support import play_reference

from auction_night.engine.deal import deal
from auction_night.simulate import play_night, preset_config

NIGHTS = 25


@pytest.mark.parametrize("c", [300, 120, 1500])
@pytest.mark.parametrize("chase", [True, False])
def test_the_engine_with_style_bots_agrees_with_the_reference_night(c: int, chase: bool) -> None:
    config = preset_config(c)
    for k in range(NIGHTS):
        seed = f"oracle-{c}:{k}"
        result = play_night(config, seed, chase=chase)
        books = play_reference(
            deal(config, seed=seed), {r.seat: r.style for r in result.seats}, chase=chase
        )
        for r in result.seats:
            got = (r.surplus, r.spent, r.bonus)
            want = (books[r.seat].surplus, books[r.seat].spent, books[r.seat].bonus)
            assert got == want, f"{seed} seat {r.seat} ({r.style})"


def test_the_reference_notices_a_change_of_rule() -> None:
    """If the economist did not pace, the books would differ: the comparison has teeth."""
    config = preset_config(120)
    differing = 0
    for k in range(NIGHTS):
        seed = f"oracle-teeth:{k}"
        result = play_night(config, seed, chase=True)
        styles = {r.seat: r.style for r in result.seats}
        unpaced = {seat: "E" if style == "P" else style for seat, style in styles.items()}
        the_deal = deal(config, seed=seed)
        if play_reference(the_deal, styles, chase=True) != play_reference(
            the_deal, unpaced, chase=True
        ):
            differing += 1
    assert differing >= NIGHTS // 3
