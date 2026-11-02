// The overlay of one lot: the reference numbers that the replay keeps next to the result.
//
// Every number here belongs to one lot. They are shown side by side with the lot and never
// added up over lots, divided by each other or turned into a score for the night. The replay
// holds them in lot.overlay (lib/replay.js has already checked that they are whole numbers).
//
// What the table could see and what only the hole-card lens may show:
//   public      the expected revenue of the lot (it follows from the rule, the tier and the
//               number of bidders, which the lot announced) and, for a blind box, the price
//               minus the true value (both are public once the box has been opened);
//   lens only   the textbook bid of each seat, its pacing factor, its regret, its excess over
//               the textbook bid, and whether the lot went to the highest value. Each of them
//               depends on the private value or the private budget of a seat.
// overlayFor returns only what the reader may see. A view that draws what it is given cannot
// leak a private number, whatever the stage does with the lens.

/** The money the seller actually received for the lot: the sum of the payments, null if unsold. */
export function actualRevenue(lot) {
  const hammer = lot.hammer;
  if (!hammer || hammer.unsold === true || (hammer.winner ?? null) === null) return null;
  const paid = Object.values(hammer.payments ?? {});
  return paid.length > 0 ? paid.reduce((total, amount) => total + amount, 0) : hammer.price;
}

/**
 * What the overlay of a lot may show, or null when the replay has nothing for the lot.
 *   lens      the switch it was made for
 *   held      true when numbers exist that need the lens (so the view can say so)
 *   seats     lens only: [{seat, ref, alpha_milli, regret, excess}] by seat, missing parts null
 *   highest   lens only: true, false, or null when it does not apply
 *   revenue   {expected_milli, actual, all_pay} or null; actual is null for an unsold lot
 *   curse_gap price minus true value of a sold blind box, else null
 */
export function overlayFor(lot, { lens = false } = {}) {
  const o = lot?.overlay;
  if (!o) return null;
  const seatNos = [
    ...new Set([o.ref_bids, o.alpha_milli, o.regret, o.excess].flatMap((table) => Object.keys(table).map(Number))),
  ].sort((a, b) => a - b);
  const rows = seatNos.map((seat) => ({
    seat,
    ref: o.ref_bids[seat] ?? null,
    alpha_milli: o.alpha_milli[seat] ?? null,
    regret: o.regret[seat] ?? null,
    excess: o.excess[seat] ?? null,
  }));
  const hasPrivate = rows.length > 0 || o.to_highest_value !== null;
  const revenue =
    o.expected_revenue_milli === null
      ? null
      : { expected_milli: o.expected_revenue_milli, actual: actualRevenue(lot), all_pay: lot.format === 'all_pay' };
  const curse = lot.format === 'blind_box' ? o.curse_gap : null;
  if (!hasPrivate && revenue === null && curse === null) return null;
  return {
    lens: lens === true,
    held: lens !== true && hasPrivate,
    seats: lens === true ? rows : [],
    highest: lens === true ? o.to_highest_value : null,
    revenue,
    curse_gap: curse,
  };
}
