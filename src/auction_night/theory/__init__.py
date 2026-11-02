"""Textbook reference numbers for the lots of a night.

Modules:

* ``bne``           single-lot reference bids, generated from the mechanism definition
* ``pacing``        the multiplicative pacing factor that makes a budget last
* ``exact``         exact expectations of the seller's revenue in the integer engine
* ``common_value``  the blind box: selection and payment effects of the winner's curse
* ``counterfactual``  replaying a lot with one seat's number changed
* ``regret``        the best single-lot deviation, found exactly
* ``lot``           everything above, collected per lot and per night

Arithmetic is exact (integers and fractions), so every number is the same on every machine.
"""

from auction_night.theory.bne import reference_bid, reference_bid_int
from auction_night.theory.exact import expected_revenue, revenue_moments
from auction_night.theory.lot import LotTheory, analyse_lot, analyse_night
from auction_night.theory.pacing import alpha_milli, pacing_alpha
from auction_night.theory.regret import regret

__all__ = [
    "LotTheory",
    "alpha_milli",
    "analyse_lot",
    "analyse_night",
    "expected_revenue",
    "pacing_alpha",
    "reference_bid",
    "reference_bid_int",
    "regret",
    "revenue_moments",
]
