"""The blind box: why the winner of a common-value auction tends to overpay.

Every bidder sees the same hidden value ``V`` through its own appraisal ``s = V + e``, with the
noise ``e`` uniform on ``-eps..eps`` (``eps`` is ``noise``, in coins). Two effects are easy to
mix up, so they are kept apart:

* **Selection.** The highest appraisal of ``n`` bidders is not an average appraisal: it is the
  one with the luckiest noise. It overestimates ``V`` by ``eps (n - 1) / (n + 1)`` on average.
* **Payment.** In a second-price auction the winner pays the second-highest bid. A bidder that
  simply bids its appraisal pays ``eps (n - 3) / (n + 1)`` more than ``V`` on average. Bidding
  ``s - eps (n - 2) / n`` instead is the symmetric equilibrium of the textbook model, and
  removes the loss on average.

The closed forms hold in the interior of the prior, away from its edges (where the true value
and the clamps of the bids interfere). They are the numbers the audience hears; the engine's
exact discrete values differ slightly, and :func:`expected_top_noise` and
:func:`expected_second_noise` give those for the noise alone.
"""

from fractions import Fraction


def top_signal_overestimate(noise: int, n: int) -> Fraction:
    """Mean of (highest appraisal - ``V``): ``eps (n - 1) / (n + 1)``."""
    return Fraction(noise * (n - 1), n + 1)


def equilibrium_shading(noise: int, n: int) -> Fraction:
    """How far below its appraisal a textbook bidder bids: ``eps (n - 2) / n``."""
    return Fraction(noise * (n - 2), n)


def naive_winner_overpay(noise: int, n: int) -> Fraction:
    """Mean of (price - ``V``) for bidders that bid their appraisal: ``eps (n - 3) / (n + 1)``."""
    return Fraction(noise * (n - 3), n + 1)


def _expected_order_noise(noise: int, n: int, rank: int) -> Fraction:
    """Mean of the ``rank``-th highest of ``n`` noises uniform on ``-noise..noise``, exactly."""
    width = 2 * noise + 1
    total = Fraction(0)
    for x in range(-noise, noise):  # E[X] = -noise + sum over x of P(X > x)
        reach = (x + noise + 1) ** n
        if rank == 2:
            reach += n * (x + noise + 1) ** (n - 1) * (width - (x + noise + 1))
        total += 1 - Fraction(reach, width**n)
    return Fraction(-noise) + total


def expected_top_noise(noise: int, n: int) -> Fraction:
    """Mean of the highest of ``n`` whole-number noises, exactly."""
    return _expected_order_noise(noise, n, 1)


def expected_second_noise(noise: int, n: int) -> Fraction:
    """Mean of the second-highest of ``n`` whole-number noises, exactly."""
    return _expected_order_noise(noise, n, 2)
