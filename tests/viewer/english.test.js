import assert from 'node:assert/strict';
import test from 'node:test';

import {
  axisTop,
  climbSeconds,
  duration,
  exitsOf,
  frameAt,
  layout,
  seatsOf,
  skipsOf,
  ticksFor,
  TIMING,
  winnerExitOf,
} from '../../viewer/anim/english.js';
import { animationFor } from '../../viewer/anim/index.js';
import { CLOCKS, fixture, loadFixture, modelOf } from './helpers.js';

const deepFreeze = (x) => {
  if (x && typeof x === 'object') for (const v of Object.values(x)) deepFreeze(v);
  return Object.freeze(x);
};

/** An English lot in the shape of a replay timeline entry. */
function lotOf({ exits = [], skips = [], winner = null, price = 0, actions = [], reserve = 0 }) {
  const dropouts = [
    ...skips.map((seat) => ({ seat, price: 0, remaining: [], participated: false })),
    ...exits.map(([seat, at]) => ({ seat, price: at, remaining: [], participated: true })),
  ];
  const active = [...new Set([...dropouts.map((d) => d.seat), ...(winner === null ? [] : [winner])])].sort((a, b) => a - b);
  return {
    lot: 1,
    format: 'english',
    active,
    item: reserve > 0 ? { reserve } : null,
    actions,
    dropouts,
    hammer: {
      format: 'english',
      winner,
      price,
      payments: winner === null ? {} : { [winner]: price },
      bids: null,
      unsold: winner === null,
      tie_broken: false,
      tied: [],
      newly_out: [],
    },
  };
}

const bar = lotOf({
  skips: [4],
  exits: [[1, 95], [2, 140], [5, 165]],
  winner: 3,
  price: 165,
  reserve: 20,
  actions: [
    { seq: 26, seat: 1, phase: 'english_init', status: 'ok', value: 95 },
    { seq: 28, seat: 3, phase: 'english_init', status: 'clamped', value: 190, raw_value: 300 },
    { seq: 32, seat: 3, phase: 'english_revise', status: 'ok', value: 190 },
    { seq: 34, seat: 3, phase: 'english_revise', status: 'ok', value: 180 },
  ],
});

const plan = layout(bar);
const at = (stop, key) => plan.stops[stop][key];

// ---------------------------------------------------------------------------------- reading

test('the exits are the seats that took part, lowest price first; skips and seats come with them', () => {
  assert.deepEqual(exitsOf(bar).map((e) => [e.seat, e.price]), [[1, 95], [2, 140], [5, 165]]);
  assert.deepEqual(skipsOf(bar), [4]);
  assert.deepEqual(seatsOf(bar), [1, 2, 3, 4, 5]);
});

test('exits come in price order whatever the order of the file, equal prices keep the file order', () => {
  const lot = lotOf({ exits: [[3, 120], [1, 50], [2, 120], [4, 80]], winner: 5, price: 120 });
  assert.deepEqual(exitsOf(lot).map((e) => e.seat), [1, 4, 3, 2]);
});

test('remaining is the number of seats left, or null when the file does not say', () => {
  const lot = lotOf({ exits: [[1, 50]], winner: 2, price: 50 });
  lot.dropouts[0].remaining = [2];
  assert.equal(exitsOf(lot)[0].remaining, 1);
  delete lot.dropouts[0].remaining;
  assert.equal(exitsOf(lot)[0].remaining, null);
});

test('the winner exit price is the last English exit price of the winner', () => {
  assert.equal(winnerExitOf(bar), 180);
  const shuffled = { ...bar, actions: [...bar.actions].reverse() };
  assert.equal(winnerExitOf(shuffled), 180, 'by sequence number, not by position in the list');
  assert.equal(winnerExitOf(lotOf({ exits: [[1, 50]], winner: 2, price: 50 })), null, 'no action of the winner');
  assert.equal(winnerExitOf(lotOf({ exits: [[1, 50]] })), null, 'unsold');
  const other = lotOf({ exits: [[1, 50]], winner: 2, price: 50, actions: [{ seq: 3, seat: 2, phase: 'sealed', status: 'ok', value: 77 }, { seq: 4, seat: 1, phase: 'english_init', status: 'ok', value: 90 }] });
  assert.equal(winnerExitOf(other), null, 'other phases and other seats do not count');
});

// --------------------------------------------------------------------------------- the plan

test('climbs take longer for longer distances, within bounds, and none takes no time', () => {
  assert.equal(climbSeconds(0), 0);
  assert.equal(climbSeconds(-5), 0);
  assert.equal(climbSeconds(1), TIMING.climbMin);
  assert.equal(climbSeconds(1e9), TIMING.climbMax);
  let before = 0;
  for (const delta of [1, 5, 20, 50, 100, 200, 500]) {
    const seconds = climbSeconds(delta);
    assert.ok(seconds >= before && seconds >= TIMING.climbMin && seconds <= TIMING.climbMax);
    before = seconds;
  }
});

test('the schedule: intro, then for every stop a climb and a rest, then the hammer', () => {
  assert.deepEqual(plan.stops.map((s) => [s.kind, s.seat, s.price]), [['exit', 1, 95], ['exit', 2, 140], ['exit', 5, 165]]);
  assert.equal(plan.start, 20, 'the bar starts at the reserve price of the lot');
  let clock = TIMING.intro;
  let from = 20;
  plan.stops.forEach((stop, i) => {
    assert.ok(Math.abs(at(i, 't0') - clock) < 1e-9);
    assert.ok(Math.abs(at(i, 't1') - (clock + climbSeconds(stop.price - from))) < 1e-9);
    assert.ok(Math.abs(at(i, 't2') - (at(i, 't1') + TIMING.hold)) < 1e-9);
    clock = at(i, 't2');
    from = stop.price;
  });
  assert.ok(Math.abs(plan.verdictAt - clock) < 1e-9);
  assert.ok(Math.abs(plan.total - (plan.verdictAt + TIMING.verdict + TIMING.tail)) < 1e-9);
  assert.equal(duration(bar), plan.total);
});

test('the log rows are the skips at the start and the exits when they happen', () => {
  assert.deepEqual(plan.rows.map((r) => [r.kind, r.seat, r.price]), [['skip', 4, 0], ['left', 1, 95], ['left', 2, 140], ['left', 5, 165]]);
  assert.deepEqual(plan.rows.map((r) => r.at), [0, at(0, 't1'), at(1, 't1'), at(2, 't1')]);
});

// ----------------------------------------------------------------------------------- frames

test('at the start the bar sits at the reserve price and everybody is in, except those who did not take part', () => {
  const frame = frameAt(bar, 0);
  assert.equal(frame.phase, 'intro');
  assert.equal(frame.price, 20);
  assert.equal(frame.shown, 0);
  assert.equal(frame.rowsShown, 1, 'the seat that did not take part is already announced');
  assert.deepEqual(frame.seats.map((s) => [s.seat, s.state]), [[1, 'in'], [2, 'in'], [3, 'in'], [4, 'skip'], [5, 'in']]);
  assert.equal(frame.standing, 4);
  assert.equal(frame.last, null);
  assert.equal(frame.verdict, null);
  assert.equal(frame.reveal, null);
  assert.equal(frame.winnerExit, null);
});

test('the price climbs, never falls, and ends exactly on the price of the hammer', () => {
  let previous = -1;
  const total = duration(bar);
  for (let i = 0; i <= 600; i += 1) {
    const frame = frameAt(bar, (total * i) / 600);
    assert.ok(frame.price >= previous, `fell at ${frame.t}: ${frame.price} < ${previous}`);
    assert.ok(Number.isInteger(frame.price));
    assert.ok(frame.fill >= 0 && frame.fill <= 1);
    previous = frame.price;
  }
  assert.equal(previous, 165);
  assert.equal(frameAt(bar, total).verdict.price, 165);
});

test('a seat leaves the moment the bar reaches its exit price, and the bar rests there', () => {
  plan.stops.forEach((stop, i) => {
    const before = frameAt(bar, at(i, 't1') - 0.01);
    assert.equal(before.seats.find((s) => s.seat === stop.seat).state, 'in', `seat ${stop.seat} still in`);
    assert.equal(before.phase, 'climb');
    assert.ok(before.price < stop.price || climbSeconds(stop.price - (i === 0 ? 20 : plan.stops[i - 1].price)) < 0.02);
    const after = frameAt(bar, at(i, 't1'));
    const seat = after.seats.find((s) => s.seat === stop.seat);
    assert.deepEqual([seat.state, seat.left_at], ['left', stop.price]);
    assert.equal(after.price, stop.price);
    assert.equal(after.settled, stop.price);
    assert.equal(after.phase, 'hold');
    assert.equal(after.shown, i + 1);
    assert.deepEqual(after.last, { seat: stop.seat, price: stop.price, remaining: stop.remaining });
    const late = frameAt(bar, at(i, 't2') - 0.01);
    assert.equal(late.phase, 'hold');
    assert.equal(late.price, stop.price);
  });
});

test('the standing count goes down with every exit and one seat is left at the end', () => {
  const counts = plan.stops.map((_, i) => frameAt(bar, at(i, 't1')).standing);
  assert.deepEqual(counts, [3, 2, 1]);
  const end = frameAt(bar, duration(bar));
  assert.equal(end.standing, 1);
  assert.deepEqual(end.seats.filter((s) => s.state === 'won').map((s) => s.seat), [3]);
});

test('the winner is marked from the hammer on and the verdict is the one of the replay', () => {
  const before = frameAt(bar, plan.verdictAt - 0.01);
  assert.equal(before.verdict, null);
  assert.ok(before.seats.every((s) => s.state !== 'won'));
  const hammer = frameAt(bar, plan.verdictAt);
  assert.equal(hammer.phase, 'verdict');
  assert.equal(hammer.verdict.winner, 3);
  assert.equal(hammer.verdict.price, 165);
  assert.deepEqual(hammer.verdict.payments, [{ seat: 3, amount: 165 }]);
  assert.equal(hammer.seats.find((s) => s.seat === 3).state, 'won');
  assert.equal(frameAt(bar, duration(bar)).phase, 'done');
});

test('the winner and the price are read, never worked out from the exits', () => {
  const odd = lotOf({ exits: [[1, 95], [2, 140]], winner: 1, price: 77, reserve: 0 });
  const frame = frameAt(odd, duration(odd));
  assert.equal(frame.verdict.winner, 1, 'the replay says seat 1, although seat 1 left first');
  assert.equal(frame.verdict.price, 77);
});

test('the phases only move forward', () => {
  const rank = { intro: 0, climb: 1, hold: 1, verdict: 2, done: 3 };
  let phase = 0;
  const total = duration(bar);
  for (let i = 0; i <= 500; i += 1) {
    const frame = frameAt(bar, (total * i) / 500);
    assert.ok(rank[frame.phase] >= phase, `back at ${frame.t}`);
    phase = rank[frame.phase];
  }
  assert.equal(phase, 3);
});

test('rows and marks are due as the exits are reached', () => {
  assert.deepEqual([0, 1, 2, 3].map((i) => frameAt(bar, i === 0 ? 0 : at(i - 1, 't1')).rowsShown), [1, 2, 3, 4]);
  assert.deepEqual([0, 1, 2, 3].map((i) => frameAt(bar, i === 0 ? 0 : at(i - 1, 't1')).shown), [0, 1, 2, 3]);
});

// ------------------------------------------------------------------ the winner's exit price

const SECRET = 377;
const guarded = lotOf({
  exits: [[1, 60], [2, 120], [4, 135]],
  winner: 3,
  price: 135,
  actions: [{ seq: 9, seat: 3, phase: 'english_init', status: 'ok', value: SECRET }],
});

test('the exit price of the winner appears with the lens and from the hammer on, never before', () => {
  assert.equal(winnerExitOf(guarded), SECRET);
  const total = duration(guarded);
  const hammerAt = layout(guarded).verdictAt;
  for (let i = 0; i <= 200; i += 1) {
    const t = (total * i) / 200;
    const lens = frameAt(guarded, t, { lens: true });
    assert.equal(lens.winnerExit, t >= hammerAt ? SECRET : null, `at ${t}`);
  }
});

test('without the lens the number is nowhere: not in a frame, not in the plan, not in the scale', () => {
  const total = duration(guarded);
  for (const ctx of [undefined, {}, { lens: false }, { lens: 'yes' }, { lens: 1 }]) {
    for (let i = 0; i <= 100; i += 1) {
      const frame = frameAt(guarded, (total * i) / 100, ctx);
      assert.equal(frame.winnerExit, null);
      assert.ok(!JSON.stringify(frame).includes(String(SECRET)), JSON.stringify(ctx));
    }
    const text = JSON.stringify(layout(guarded, ctx));
    assert.ok(!text.includes(String(SECRET)), 'nor in the plan');
  }
  assert.ok(layout(guarded).top < SECRET, 'the scale ends below the secret without the lens');
});

test('with the lens the scale grows to hold the number, from the first frame on', () => {
  const lens = layout(guarded, { lens: true });
  const plain = layout(guarded);
  assert.ok(lens.top >= SECRET);
  assert.ok(lens.top > plain.top);
  assert.equal(frameAt(guarded, 0, { lens: true }).fill, plain.start / lens.top);
  assert.equal(duration(guarded), layout(guarded, { lens: true }).total, 'the lens never changes how long the lot lasts');
});

test('the fixture lot: the winner left no trace of 180 on the table, and the lens shows it at the hammer', () => {
  const lot = modelOf(CLOCKS).lot(1);
  assert.equal(winnerExitOf(lot), 180);
  const end = duration(lot);
  assert.equal(frameAt(lot, end).winnerExit, null);
  assert.equal(frameAt(lot, end, { lens: true }).winnerExit, 180);
  assert.equal(frameAt(lot, end, { lens: true }).verdict.price, 165);
});

// ------------------------------------------------------------------------------- the scale

test('the scale holds every price with room above, and the ticks are round and inside it', () => {
  for (const highest of [1, 5, 9, 10, 37, 74, 95, 100, 165, 199, 200, 400, 1234, 99999]) {
    const top = axisTop(highest);
    assert.ok(top > highest, `${highest} fits under ${top}`);
    assert.ok(top <= highest * 1.6 + 5, `${top} is not far above ${highest}`);
    const ticks = ticksFor(top);
    assert.equal(ticks[0], 0);
    assert.ok(ticks.length >= 3 && ticks.length <= 7, `${ticks}`);
    assert.ok(ticks.every((t) => Number.isInteger(t) && t <= top));
  }
  assert.equal(axisTop(165), 200);
  assert.equal(axisTop(74), 80);
  assert.deepEqual(ticksFor(200), [0, 50, 100, 150, 200]);
  assert.deepEqual(ticksFor(80), [0, 20, 40, 60, 80]);
  assert.equal(axisTop(0), 10);
  assert.equal(axisTop(Number.NaN), 10);
});

test('the scale of the fixture lot', () => {
  assert.equal(plan.top, 200);
  assert.deepEqual(plan.ticks, [0, 50, 100, 150, 200]);
});

// ----------------------------------------------------------------------------------- shapes

test('a lot where nobody took part is a bar that stays put, and then an unsold hammer', () => {
  const lot = lotOf({ skips: [1, 2, 3] });
  assert.equal(layout(lot).stops.length, 0);
  assert.equal(duration(lot), TIMING.intro + TIMING.verdict + TIMING.tail);
  const frame = frameAt(lot, TIMING.intro);
  assert.equal(frame.verdict.unsold, true);
  assert.ok(frame.seats.every((s) => s.state === 'skip'));
  assert.equal(frame.price, 0);
});

test('a lot with a single taker is sold at the reserve and the bar does not climb', () => {
  const lot = lotOf({ skips: [2, 3], exits: [], winner: 1, price: 20, reserve: 20 });
  assert.equal(layout(lot).stops.length, 0);
  const frame = frameAt(lot, duration(lot));
  assert.equal(frame.price, 20);
  assert.equal(frame.verdict.winner, 1);
  assert.equal(frame.seats.find((s) => s.seat === 1).state, 'won');
});

test('a sale above the last exit ends with one more rise, to the price of the hammer', () => {
  const lot = lotOf({ exits: [[1, 50]], winner: 2, price: 80 });
  const stops = layout(lot).stops;
  assert.deepEqual(stops.map((s) => [s.kind, s.price]), [['exit', 50], ['price', 80]]);
  assert.equal(frameAt(lot, layout(lot).verdictAt).price, 80);
  assert.equal(frameAt(lot, layout(lot).verdictAt).shown, 1, 'the rise is not an exit');
});

test('two seats leaving at one price leave one after the other, with no climb between them', () => {
  const lot = lotOf({ exits: [[1, 60], [2, 60]], winner: 3, price: 60 });
  const stops = layout(lot).stops;
  assert.equal(stops[1].t1, stops[1].t0);
  assert.equal(frameAt(lot, stops[1].t1).shown, 2);
});

test('an unsold lot keeps its last exit on the bar and ends in an unsold hammer', () => {
  const lot = lotOf({ exits: [[1, 40], [2, 55]], winner: null, price: 0 });
  const frame = frameAt(lot, duration(lot));
  assert.equal(frame.price, 55);
  assert.equal(frame.verdict.unsold, true);
  assert.ok(frame.seats.every((s) => s.state === 'left'));
});

test('time outside the animation is clamped and nonsense counts as zero', () => {
  assert.equal(frameAt(bar, -10).t, 0);
  assert.equal(frameAt(bar, 1e9).t, duration(bar));
  assert.equal(frameAt(bar, 1e9).phase, 'done');
  for (const bad of [Number.NaN, undefined, null, 'x', Number.POSITIVE_INFINITY * 0]) assert.equal(frameAt(bar, bad).t, 0);
});

test('frameAt is pure: the lot is not touched and the same time gives the same frame', () => {
  const frozen = deepFreeze(structuredClone(bar));
  for (const t of [0, 1.7, 3.3, 6, 100]) {
    for (const ctx of [{}, { lens: true }]) {
      const a = frameAt(frozen, t, ctx);
      const b = frameAt(frozen, t, ctx);
      assert.deepEqual(a, b);
      assert.notEqual(a.seats, b.seats, 'a fresh object each time');
    }
  }
});

test('English lots get this animation, other formats keep theirs', () => {
  const entry = animationFor('english');
  assert.equal(entry.id, 'english');
  assert.equal(entry.duration(bar), duration(bar));
  assert.equal(animationFor('dutch').id, 'dutch');
  assert.equal(animationFor('sealed_first').id, 'envelopes');
  assert.equal(animationFor('something_new').id, 'summary');
});

// ------------------------------------------------------------------------------ the fixture

test('the English lot of the fixture night, read from the file', () => {
  const raw = loadFixture(CLOCKS).timeline[0];
  const lot = modelOf(CLOCKS).lot(1);
  assert.deepEqual(exitsOf(lot).map((e) => [e.seat, e.price, e.remaining]), [[1, 95, 3], [2, 140, 2], [5, 165, 1]]);
  assert.deepEqual(skipsOf(lot), [4]);
  const end = frameAt(lot, duration(lot));
  assert.equal(end.verdict.winner, raw.hammer.winner);
  assert.equal(end.verdict.price, raw.hammer.price);
  assert.equal(layout(lot).start, 20, 'the reserve comes from the catalog entry of an indexed lot');
  assert.equal(layout(raw).start, 0, 'a raw entry does not know it');
});

test('the fixture lot never changes the replay it reads', () => {
  const replay = fixture(CLOCKS);
  const before = JSON.stringify(replay);
  const lot = replay.timeline[0];
  for (let i = 0; i <= 50; i += 1) frameAt(lot, (duration(lot) * i) / 50, { lens: true });
  assert.equal(JSON.stringify(replay), before);
});
