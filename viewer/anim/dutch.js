// The Dutch clock, as a pure function of the lot and a time.
//
// A Dutch lot is played in one shot. Before the clock starts, every seat has named the price at
// which it would buy (its grab price), and the first level of the ladder that some grab price
// reaches sells the lot. Nobody is asked again while the price falls. The clock is therefore
// only a way to show the result slowly: it lights the cells of the ladder one after the other,
// from the highest price down, and rests on the level where the replay says the lot was bought.
// If no grab price reaches any level, it lights them all and nothing is bought. The picture
// says nothing about who hesitates or who waits, because nobody does.
//
// The ladder, the level, the price and the buyer are read from the replay (lot.dutch,
// lot.hammer). The grab prices of the seats are private until the end of the night; grabsOf
// reads them from the bid-phase actions, and only the hole-card lens may draw them.
//
// frameAt(lot, t) returns the whole picture at time t (seconds at normal speed) and draws
// nothing. dutch_view.js turns a frame into DOM. The lot is a timeline entry of the replay,
// raw or indexed.

import { clamp } from '../lib/safe.js';
import { verdictOf } from './verdict.js';

export const TIMING = Object.freeze({
  intro: 1.0, // the cells stand dark
  step: 0.55, // one cell lights after the other
  hold: 1.0, // the clock rests on its last cell
  verdict: 2.2, // the hammer, the winner and the price
  tail: 0.6,
});

function toSeat(key) {
  const n = typeof key === 'number' ? key : Number(key);
  return Number.isInteger(n) && n >= 1 ? n : null;
}

/** The prices of the levels in play, highest first. */
export function ladderOf(lot) {
  const ladder = lot.dutch?.ladder;
  return Array.isArray(ladder) ? ladder.filter((price) => Number.isFinite(price) && price >= 0) : [];
}

/** The cell (counted from 0) on which the lot was bought, or null when it was not bought. */
export function stopOf(lot) {
  const dutch = lot.dutch;
  if (!dutch || dutch.unsold === true) return null;
  const level = dutch.level;
  return Number.isInteger(level) && level >= 1 && level <= ladderOf(lot).length ? level - 1 : null;
}

/** {seat, price, level} of the sale as the replay recorded it, or null. */
export function buyerOf(lot) {
  const stop = stopOf(lot);
  if (stop === null) return null;
  const seat = toSeat(lot.dutch.buyer) ?? toSeat(lot.hammer?.winner);
  return { seat, price: ladderOf(lot)[stop], level: stop + 1 };
}

/**
 * The grab prices that the seats named, one entry per seat that has a Dutch action, by seat:
 * {seat, grab, cell, status, reason, raw}. `cell` is the first cell whose price is at most the
 * grab price (where the seat would have bought), or null when the grab price is below the
 * lowest level or the seat abstained. Private until the end of the night: lens only.
 */
export function grabsOf(lot) {
  const ladder = ladderOf(lot);
  const bySeat = new Map();
  for (const action of lot.actions ?? []) {
    if (action.phase !== 'dutch') continue;
    const seat = toSeat(action.seat);
    if (seat === null) continue;
    const abstained = action.status === 'abstain' || !Number.isFinite(action.value);
    const grab = abstained ? null : action.value;
    const cell = grab === null ? null : ladder.findIndex((price) => price <= grab);
    bySeat.set(seat, {
      seat,
      grab,
      cell: cell === null || cell < 0 ? null : cell,
      status: action.status ?? 'ok',
      reason: action.reason ?? null,
      raw: action.status === 'clamped' && Number.isFinite(action.raw_value) ? action.raw_value : null,
    });
  }
  return [...bySeat.values()].sort((a, b) => a.seat - b.seat);
}

/**
 * The plan of a lot: the cells and the moments. `last` is the cell the clock rests on (the
 * cell of the sale, else the lowest one), or -1 when the ladder is empty. Cell i lights at
 * cellAt(i); the clock rests from cellAt(last), the hammer falls hold seconds later.
 */
export function layout(lot) {
  const ladder = ladderOf(lot);
  const stop = stopOf(lot);
  const last = stop ?? ladder.length - 1;
  const cellAt = (i) => TIMING.intro + i * TIMING.step;
  const restAt = last >= 0 ? cellAt(last) : TIMING.intro;
  const verdictAt = last >= 0 ? restAt + TIMING.hold : TIMING.intro;
  return {
    ladder,
    stop,
    last,
    cellAt,
    restAt,
    verdictAt,
    verdictEnd: verdictAt + TIMING.verdict,
    total: verdictAt + TIMING.verdict + TIMING.tail,
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
 *   phase    'intro' | 'falling' | 'rest' | 'verdict' | 'done'
 *   cells    one per level, highest price first: {n, price, state, buyer}. state is 'dim'
 *            (not reached yet), 'lit' (the clock has passed), 'now' (the clock is on it) or
 *            'stop' (bought here). buyer is the seat that bought, on the cell of the sale only.
 *   lit      how many cells have lit up; price the price of the cell the clock is on, or null
 *   buyer    {seat, price, level} once the clock has come to rest on a sale, else null
 *   rested   true once the clock has come to rest (also when nothing was bought)
 *   verdict  null before the hammer; then {unsold, winner, price, payments, rule, ...}
 *   reveal   always null: only the blind box has one
 */
export function frameAt(lot, t) {
  const plan = layout(lot);
  const at = clamp(Number.isFinite(t) ? t : 0, 0, plan.total);
  const hammered = at >= plan.verdictAt;
  const lit = plan.last < 0 ? 0 : Math.min(plan.last + 1, at < TIMING.intro ? 0 : Math.floor((at - TIMING.intro) / TIMING.step + 1e-9) + 1);
  const rested = plan.last >= 0 && at >= plan.restAt;
  const sale = buyerOf(lot);
  const cells = plan.ladder.map((price, i) => {
    let state = 'dim';
    if (i < lit) state = 'lit';
    if (i === lit - 1 && !rested) state = 'now';
    if (rested && plan.stop === i) state = 'stop';
    return { n: i + 1, price, state, buyer: rested && plan.stop === i && sale !== null ? sale.seat : null };
  });

  let phase = 'done';
  if (at < TIMING.intro) phase = 'intro';
  else if (!hammered) phase = rested ? 'rest' : 'falling';
  else if (at < plan.verdictEnd) phase = 'verdict';

  return {
    t: at,
    duration: plan.total,
    phase,
    cells,
    lit,
    price: lit > 0 ? plan.ladder[lit - 1] : null,
    buyer: rested ? sale : null,
    rested,
    verdict: hammered ? verdictOf(lot) : null,
    reveal: null,
  };
}
