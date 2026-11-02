// The sealed-envelope animation, as a pure function of the lot and a time.
//
// First price, second price, all-pay and the blind box share it: everyone's bid sits in an
// envelope, the envelopes open from the lowest bid to the highest, then the hammer falls. The
// winner, the price and the payments are read from the replay (lot.hammer). Nothing here
// decides who won or what was paid. The blind box adds one more beat, the true value.
//
// frameAt(lot, t) returns the whole picture at time t (seconds at normal speed) and draws
// nothing. envelopes_view.js turns a frame into DOM. The lot is a timeline entry of the
// replay, raw or indexed.

import { clamp } from '../lib/safe.js';
import { paymentsOf, verdictOf } from './verdict.js';

export const TIMING = Object.freeze({
  intro: 1.2, // envelopes lie closed on the table
  open: 1.0, // one envelope opens
  gap: 0.3, // pause before the next one
  verdict: 2.2, // the hammer, the winner and the price
  reveal: 2.4, // blind box only: the true value
  tail: 0.6,
});

const SEALED_PHASE = 'sealed';

function sealedAction(lot) {
  const bySeat = new Map();
  for (const action of lot.actions ?? []) {
    if (action.phase === SEALED_PHASE) bySeat.set(action.seat, action);
  }
  return bySeat;
}

function toSeat(key) {
  const n = typeof key === 'number' ? key : Number(key);
  return Number.isInteger(n) ? n : null;
}

/**
 * The seats that have an envelope, in opening order: lowest bid first. Among equal bids the
 * seat that the replay names as winner opens last, so a tie ends on the winner.
 * Each entry: {seat, bid, status, reason, raw_value, winner}.
 */
export function envelopeOrder(lot) {
  const hammer = lot.hammer ?? {};
  const bids = hammer.bids ?? null;
  const actions = sealedAction(lot);
  let seats;
  if (bids !== null) seats = Object.keys(bids).map(toSeat);
  else if ((lot.active ?? []).length > 0) seats = [...lot.active];
  else seats = [...actions.keys()];
  const rows = [...new Set(seats.filter((seat) => seat !== null))].map((seat) => {
    const action = actions.get(seat) ?? null;
    const recorded = bids !== null ? bids[seat] : undefined;
    const fallback = action !== null && action.status !== 'abstain' ? (action.value ?? 0) : 0;
    return {
      seat,
      bid: recorded ?? fallback,
      status: action?.status ?? 'ok',
      reason: action?.reason ?? null,
      raw_value: action?.raw_value ?? null,
      winner: seat === (hammer.winner ?? null),
    };
  });
  return rows.sort((a, b) => a.bid - b.bid || Number(a.winner) - Number(b.winner) || a.seat - b.seat);
}

/** Moments of the animation, in seconds. */
export function layout(lot) {
  const rows = envelopeOrder(lot);
  const blind = lot.format === 'blind_box';
  const verdictAt = TIMING.intro + rows.length * (TIMING.open + TIMING.gap);
  const verdictEnd = verdictAt + TIMING.verdict;
  const revealEnd = blind ? verdictEnd + TIMING.reveal : verdictEnd;
  return {
    rows,
    blind,
    verdictAt,
    revealAt: verdictEnd,
    total: revealEnd + TIMING.tail,
    openStart: (order) => TIMING.intro + order * (TIMING.open + TIMING.gap),
  };
}

/** Length of the animation in seconds at normal speed. */
export function duration(lot) {
  return layout(lot).total;
}

/** Seconds into the animation at which the hammer falls. */
export function hammerAt(lot) {
  return layout(lot).verdictAt;
}

/**
 * The picture at time t. Fields:
 *   phase      'intro' | 'opening' | 'verdict' | 'reveal' | 'done'
 *   envelopes  one per seat in opening order: {seat, order, state: 'sealed'|'opening'|'open',
 *              progress 0..1, bid, status, reason, raw_value, winner, pays}. A closed
 *              envelope shows nothing (bid, status and reason are null).
 *   focus      the seat whose envelope is opening, else null
 *   leader     the seat with the highest bid among the open envelopes, else null; once the
 *              hammer has fallen the replay's winner takes over
 *   verdict    null before the hammer; then {unsold, winner, price, payments, rule, blind,
 *              tie_broken, tied, newly_out}, all taken from the replay
 *   reveal     blind box only, from its beat on: {true_value, delta} where delta is price
 *              minus true value, null when the lot was not sold
 * `ctx.trueValue` supplies the true value when the hammer record lacks it.
 */
export function frameAt(lot, t, ctx = {}) {
  const plan = layout(lot);
  const at = clamp(Number.isFinite(t) ? t : 0, 0, plan.total);
  const hammer = lot.hammer ?? {};
  const verdict = verdictOf(lot);
  const hammered = at >= plan.verdictAt;
  const paid = new Map(paymentsOf(hammer).map((p) => [p.seat, p.amount]));

  const envelopes = plan.rows.map((row, order) => {
    const start = plan.openStart(order);
    const state = at < start ? 'sealed' : at < start + TIMING.open ? 'opening' : 'open';
    const shown = state !== 'sealed';
    return {
      seat: row.seat,
      order,
      state,
      progress: clamp((at - start) / TIMING.open, 0, 1),
      bid: shown ? row.bid : null,
      status: shown ? row.status : null,
      reason: shown ? row.reason : null,
      raw_value: shown ? row.raw_value : null,
      winner: hammered && row.seat === verdict.winner,
      pays: hammered ? (paid.get(row.seat) ?? 0) : null,
    };
  });

  let top = null;
  for (const e of envelopes) {
    if (e.state === 'open' && e.bid > 0 && (top === null || e.bid >= top.bid)) top = e;
  }
  const opening = envelopes.find((e) => e.state === 'opening');

  const trueValue = hammer.true_value ?? ctx.trueValue ?? null;
  const revealed = plan.blind && at >= plan.revealAt;
  let phase = 'done';
  if (at < TIMING.intro) phase = 'intro';
  else if (!hammered) phase = 'opening';
  else if (at < plan.revealAt) phase = 'verdict';
  else if (plan.blind && at < plan.revealAt + TIMING.reveal) phase = 'reveal';

  return {
    t: at,
    duration: plan.total,
    phase,
    envelopes,
    focus: opening?.seat ?? null,
    leader: hammered ? verdict.winner : (top?.seat ?? null),
    verdict: hammered ? verdict : null,
    reveal: revealed
      ? {
          true_value: trueValue,
          delta: !verdict.unsold && trueValue !== null ? verdict.price - trueValue : null,
        }
      : null,
  };
}
