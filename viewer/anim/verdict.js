// The hammer, as the replay recorded it. Every bid animation ends with this picture.
//
// Nothing is worked out here: the winner, the price and the payments are copied from the
// lot's hammer record. A lot with no winner is unsold.

import { PRICING } from '../lib/format.js';

function toSeat(key) {
  const n = typeof key === 'number' ? key : Number(key);
  return Number.isInteger(n) ? n : null;
}

/** [{seat, amount}] in seat order, from hammer.payments. */
export function paymentsOf(hammer) {
  return Object.entries(hammer?.payments ?? {})
    .map(([seat, amount]) => ({ seat: toSeat(seat), amount }))
    .filter((p) => p.seat !== null && Number.isFinite(p.amount))
    .sort((a, b) => a.seat - b.seat);
}

/** {unsold, winner, price, payments, rule, blind, tie_broken, tied, newly_out}. */
export function verdictOf(lot) {
  const hammer = lot.hammer ?? {};
  const unsold = hammer.unsold === true || (hammer.winner ?? null) === null;
  return {
    unsold,
    winner: unsold ? null : hammer.winner,
    price: unsold ? 0 : (hammer.price ?? 0),
    payments: unsold ? [] : paymentsOf(hammer),
    rule: PRICING[lot.format] ?? 'second',
    blind: lot.format === 'blind_box',
    tie_broken: hammer.tie_broken === true,
    tied: [...(hammer.tied ?? [])],
    newly_out: [...(hammer.newly_out ?? [])],
  };
}
