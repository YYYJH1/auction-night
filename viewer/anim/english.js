// The English price bar, as a pure function of the lot and a time.
//
// The price starts at the reserve (or at 0) and climbs. Every seat that took part leaves at its
// exit price, in the order the replay announced; the bar pauses at that price for a moment, then
// climbs on. When the runner-up has left, the seat that is still standing wins and the hammer
// falls. The prices, the order of leaving, the winner and the price paid are all read from the
// replay (lot.dropouts, lot.hammer). Nothing here decides who won or what was paid.
//
// The exit price of the winner is the one number the table never learns: the winner pays what
// the runner-up left at. It is in the replay (the last English action of the winner) and the
// hole-card lens may show it. frameAt only returns it when ctx.lens is true, and only from the
// hammer on. The length of the animation and the picture without the lens do not depend on it.
//
// frameAt(lot, t, ctx) returns the whole picture at time t (seconds at normal speed) and draws
// nothing. english_view.js turns a frame into DOM. The lot is a timeline entry of the replay,
// raw or indexed (an indexed lot also knows its reserve price).

import { clamp } from '../lib/safe.js';
import { verdictOf } from './verdict.js';

export const TIMING = Object.freeze({
  intro: 1.0, // the bar sits at its starting price
  climbBase: 0.4, // a climb lasts this long plus a little for every coin
  climbPerCoin: 0.008,
  climbMin: 0.5,
  climbMax: 1.6,
  hold: 0.9, // the bar rests after a seat has left
  verdict: 2.2, // the hammer, the winner and the price
  tail: 0.6,
});

const ENGLISH_PHASES = new Set(['english_init', 'english_revise']);

function toSeat(key) {
  const n = typeof key === 'number' ? key : Number(key);
  return Number.isInteger(n) && n >= 1 ? n : null;
}

const takesPart = (dropout) => dropout.participated !== false;

/** Seconds a climb of `delta` coins lasts. No climb takes no time. */
export function climbSeconds(delta) {
  if (!(delta > 0)) return 0;
  return clamp(TIMING.climbBase + TIMING.climbPerCoin * delta, TIMING.climbMin, TIMING.climbMax);
}

/** Every seat of the lot in seat order: the seats that were asked, those that left, the winner. */
export function seatsOf(lot) {
  const seats = new Set((lot.active ?? []).map(toSeat));
  for (const d of lot.dropouts ?? []) seats.add(toSeat(d.seat));
  seats.add(toSeat(lot.hammer?.winner));
  seats.delete(null);
  return [...seats].sort((a, b) => a - b);
}

/** The seats that did not take part (announced at price 0), in the order announced. */
export function skipsOf(lot) {
  return (lot.dropouts ?? []).filter((d) => !takesPart(d)).map((d) => toSeat(d.seat)).filter((s) => s !== null);
}

/**
 * The exits while the bar climbed, lowest price first (the order of the replay among equal
 * prices): [{seat, price, remaining}] where remaining is how many seats were left, or null.
 */
export function exitsOf(lot) {
  const exits = (lot.dropouts ?? [])
    .filter(takesPart)
    .map((d, index) => ({
      seat: toSeat(d.seat),
      price: Number.isFinite(d.price) ? Math.max(0, d.price) : 0,
      remaining: Array.isArray(d.remaining) ? d.remaining.length : null,
      index,
    }))
    .filter((e) => e.seat !== null);
  exits.sort((a, b) => a.price - b.price || a.index - b.index);
  return exits.map(({ seat, price, remaining }) => ({ seat, price, remaining }));
}

/**
 * What the winner would have stayed in for: the last English exit price of the winner, or null.
 * Private to the winner during the night. Only the hole-card lens may show it.
 */
export function winnerExitOf(lot) {
  const winner = toSeat(lot.hammer?.winner);
  if (winner === null || lot.hammer?.unsold === true) return null;
  const mine = (lot.actions ?? [])
    .filter((a) => a.seat === winner && ENGLISH_PHASES.has(a.phase) && Number.isFinite(a.value))
    .sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0));
  return mine.length === 0 ? null : mine.at(-1).value;
}

function reserveOf(lot) {
  const reserve = lot.item?.reserve;
  return Number.isFinite(reserve) && reserve > 0 ? reserve : 0;
}

/**
 * The top of the price scale: the highest figure plus a little room, rounded up to a round
 * number (a step of 1, 2, 5 or 10 times a power of ten, halved below the first digit).
 */
export function axisTop(highest) {
  if (!(highest > 0)) return 10;
  const power = 10 ** Math.floor(Math.log10(highest));
  const step = power >= 2 ? power / 2 : 1;
  return Math.ceil((highest * 1.08) / step) * step;
}

/** Tick values from 0 up to `top`, about five of them, on a round step. */
export function ticksFor(top) {
  const rough = top / 5;
  const power = 10 ** Math.floor(Math.log10(Math.max(rough, 1)));
  const step = [1, 2, 5, 10].map((m) => m * power).find((s) => s >= rough) ?? power * 10;
  const ticks = [];
  for (let value = 0; value <= top; value += step) ticks.push(value);
  return ticks;
}

const smooth = (u) => u * u * (3 - 2 * u);

/**
 * The plan of a lot: its stops and their times. Each stop is a price the bar climbs to and
 * rests at: {kind: 'exit', seat, price, remaining} for a seat leaving, or {kind: 'price'} for
 * the last rise to the price of the hammer when no exit ends there. Times are in seconds:
 * t0 the climb starts, t1 the price is reached (the seat leaves), t2 the rest ends.
 * ctx.lens widens the scale so that the winner's exit price fits on it.
 */
export function layout(lot, ctx = {}) {
  const verdict = verdictOf(lot);
  const exits = exitsOf(lot).map((e) => ({ kind: 'exit', ...e }));
  const reserve = reserveOf(lot);
  const topExit = exits.length === 0 ? reserve : exits.at(-1).price;
  const stops = [...exits];
  if (!verdict.unsold && verdict.price > topExit) {
    stops.push({ kind: 'price', seat: null, price: verdict.price, remaining: null });
  }
  const start = Math.min(reserve, stops.length === 0 ? reserve : stops[0].price);

  let at = TIMING.intro;
  let from = start;
  const timed = stops.map((stop) => {
    const t0 = at;
    const t1 = t0 + climbSeconds(stop.price - from);
    const t2 = t1 + TIMING.hold;
    const row = { ...stop, from, t0, t1, t2 };
    at = t2;
    from = stop.price;
    return row;
  });
  const verdictAt = at;
  const finalPrice = timed.length === 0 ? start : timed.at(-1).price;
  const winnerExit = ctx.lens === true ? winnerExitOf(lot) : null;
  const top = axisTop(Math.max(start, finalPrice, winnerExit ?? 0));

  const seats = seatsOf(lot);
  const skips = skipsOf(lot).filter((seat, i, all) => all.indexOf(seat) === i);
  const rows = [
    ...skips.map((seat) => ({ kind: 'skip', seat, price: 0, remaining: null, at: 0 })),
    ...timed.filter((s) => s.kind === 'exit').map((s) => ({ kind: 'left', seat: s.seat, price: s.price, remaining: s.remaining, at: s.t1 })),
  ];
  return {
    start,
    top,
    ticks: ticksFor(top),
    seats,
    skips,
    stops: timed,
    rows,
    winnerExit,
    verdictAt,
    verdictEnd: verdictAt + TIMING.verdict,
    total: verdictAt + TIMING.verdict + TIMING.tail,
  };
}

/** Length of the animation in seconds at normal speed. */
export function duration(lot) {
  return layout(lot).total;
}

/** Seconds into the animation at which the hammer falls (the lens does not move it). */
export function hammerAt(lot) {
  return layout(lot).verdictAt;
}

/**
 * The picture at time t. Fields:
 *   phase       'intro' | 'climb' | 'hold' | 'verdict' | 'done'
 *   price       the price on the bar now, whole coins; settled the price of the last stop reached
 *   fill        price / top, 0..1; settledFill the same for settled (for reduced motion)
 *   shown       how many exit marks have been reached; rowsShown how many log rows are due
 *   seats       one per seat of the lot: {seat, state: 'in'|'left'|'skip'|'won', left_at}
 *   standing    how many seats are still in the lot (not skipped, not left)
 *   last        the exit reached last, {seat, price, remaining}, else null
 *   winnerExit  the winner's own exit price: only with ctx.lens, and only from the hammer on
 *   verdict     null before the hammer; then {unsold, winner, price, payments, rule, ...}
 *   reveal      always null: only the blind box has one
 */
export function frameAt(lot, t, ctx = {}) {
  const plan = layout(lot, ctx);
  const at = clamp(Number.isFinite(t) ? t : 0, 0, plan.total);
  const verdict = verdictOf(lot);
  const hammered = at >= plan.verdictAt;

  let price = plan.start;
  let settled = plan.start;
  let reached = 0;
  let moving = false;
  for (const stop of plan.stops) {
    if (at >= stop.t1) {
      price = stop.price;
      settled = stop.price;
      reached += 1;
    } else {
      if (at >= stop.t0) {
        price = stop.from + Math.floor((stop.price - stop.from) * smooth((at - stop.t0) / (stop.t1 - stop.t0)));
        moving = true;
      }
      break;
    }
  }
  const done = plan.stops.slice(0, reached);
  const exitsDone = done.filter((s) => s.kind === 'exit');
  const left = new Map(exitsDone.map((s) => [s.seat, s.price]));
  const seats = plan.seats.map((seat) => {
    let state = 'in';
    if (plan.skips.includes(seat)) state = 'skip';
    else if (left.has(seat)) state = 'left';
    else if (hammered && seat === verdict.winner) state = 'won';
    return { seat, state, left_at: state === 'left' ? left.get(seat) : null };
  });
  const final = exitsDone.at(-1) ?? null;

  let phase = 'done';
  if (at < TIMING.intro) phase = 'intro';
  else if (!hammered) phase = moving ? 'climb' : 'hold';
  else if (at < plan.verdictEnd) phase = 'verdict';

  return {
    t: at,
    duration: plan.total,
    phase,
    price,
    settled,
    fill: price / plan.top,
    settledFill: settled / plan.top,
    shown: exitsDone.length,
    rowsShown: plan.rows.filter((row) => row.at <= at).length,
    seats,
    standing: seats.filter((s) => s.state === 'in' || s.state === 'won').length,
    last: final === null ? null : { seat: final.seat, price: final.price, remaining: final.remaining },
    winnerExit: hammered ? plan.winnerExit : null,
    verdict: hammered ? verdict : null,
    reveal: null,
  };
}
