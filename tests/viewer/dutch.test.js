import assert from 'node:assert/strict';
import test from 'node:test';

import { buyerOf, duration, frameAt, grabsOf, ladderOf, layout, stopOf, TIMING } from '../../viewer/anim/dutch.js';
import { CLOCKS, fixture, loadFixture, modelOf } from './helpers.js';

const deepFreeze = (x) => {
  if (x && typeof x === 'object') for (const v of Object.values(x)) deepFreeze(v);
  return Object.freeze(x);
};

const LADDER = [400, 360, 320, 280, 240, 200, 160, 120, 80, 40];

/** A Dutch lot in the shape of a replay timeline entry. */
function lotOf({ ladder = LADDER, level = null, buyer = null, actions = [], winner = buyer, price = level === null ? 0 : ladder[level - 1] } = {}) {
  const unsold = level === null;
  return {
    lot: 2,
    format: 'dutch',
    active: [1, 2, 4],
    actions,
    dutch: { ladder, level, price: unsold ? null : price, buyer: unsold ? null : buyer, unsold },
    hammer: {
      format: 'dutch',
      winner: unsold ? null : winner,
      price: unsold ? 0 : price,
      payments: unsold ? {} : { [winner]: price },
      bids: null,
      unsold,
      tie_broken: false,
      tied: [],
      newly_out: [],
    },
  };
}

const sold = lotOf({ level: 4, buyer: 2 });
const none = lotOf({});
const fixtureLot = modelOf(CLOCKS).lot(2);

// ---------------------------------------------------------------------------------- reading

test('the ladder, the cell of the sale and the buyer come from the replay', () => {
  assert.deepEqual(ladderOf(fixtureLot), LADDER);
  assert.equal(stopOf(fixtureLot), 3, 'level 4 is the fourth cell');
  assert.deepEqual(buyerOf(fixtureLot), { seat: 2, price: 280, level: 4 });
  assert.equal(stopOf(none), null);
  assert.equal(buyerOf(none), null);
});

test('a ladder with fewer levels (a reserve price) is as long as the replay says', () => {
  const lot = lotOf({ ladder: [400, 360, 320, 280, 240, 200], level: 2, buyer: 1 });
  assert.equal(layout(lot).ladder.length, 6);
  assert.equal(frameAt(lot, duration(lot)).cells.length, 6);
});

test('nonsense in the result is read as no sale, never as a guess', () => {
  for (const level of [0, -1, 11, 2.5, '4', null]) {
    const lot = lotOf({ level: 4, buyer: 2 });
    lot.dutch.level = level;
    assert.equal(stopOf(lot), null, String(level));
  }
  const unsold = lotOf({ level: 4, buyer: 2 });
  unsold.dutch.unsold = true;
  assert.equal(stopOf(unsold), null);
  assert.equal(stopOf({ format: 'dutch', dutch: null }), null);
  assert.deepEqual(ladderOf({ format: 'dutch' }), []);
  assert.deepEqual(ladderOf({ dutch: { ladder: [400, 'x', null, 360, -5] } }), [400, 360]);
});

test('the grab prices come from the Dutch actions, with the cell where each seat would have bought', () => {
  assert.deepEqual(grabsOf(fixtureLot).map((g) => [g.seat, g.grab, g.cell, g.status]), [
    [1, 150, 7, 'ok'],
    [2, 280, 3, 'ok'],
  ]);
  const lot = lotOf({
    level: 1,
    buyer: 1,
    actions: [
      { seq: 5, seat: 4, phase: 'dutch', status: 'abstain', reason: 'timeout', value: null, raw_value: null },
      { seq: 6, seat: 2, phase: 'dutch', status: 'clamped', value: 150, raw_value: 390 },
      { seq: 7, seat: 1, phase: 'dutch', status: 'ok', value: 20 },
      { seq: 8, seat: 3, phase: 'sealed', status: 'ok', value: 99 },
    ],
  });
  assert.deepEqual(grabsOf(lot).map((g) => [g.seat, g.grab, g.cell, g.reason, g.raw]), [
    [1, 20, null, null, null],
    [2, 150, 7, null, 390],
    [4, null, null, 'timeout', null],
  ]);
});

// --------------------------------------------------------------------------------- the plan

test('the schedule: dark cells, one light after another, a rest on the last cell, then the hammer', () => {
  const plan = layout(sold);
  assert.equal(plan.last, 3);
  assert.equal(plan.cellAt(0), TIMING.intro);
  assert.ok(Math.abs(plan.cellAt(3) - (TIMING.intro + 3 * TIMING.step)) < 1e-9);
  assert.equal(plan.restAt, plan.cellAt(3));
  assert.ok(Math.abs(plan.verdictAt - (plan.restAt + TIMING.hold)) < 1e-9);
  assert.ok(Math.abs(plan.total - (plan.verdictAt + TIMING.verdict + TIMING.tail)) < 1e-9);
  assert.equal(duration(sold), plan.total);
});

test('an unsold lot runs the whole ladder, a lot with no ladder goes straight to the hammer', () => {
  assert.equal(layout(none).last, 9);
  assert.ok(Math.abs(duration(none) - (TIMING.intro + 9 * TIMING.step + TIMING.hold + TIMING.verdict + TIMING.tail)) < 1e-9);
  const empty = lotOf({ ladder: [] });
  assert.equal(duration(empty), TIMING.intro + TIMING.verdict + TIMING.tail);
  const frame = frameAt(empty, TIMING.intro);
  assert.equal(frame.phase, 'verdict');
  assert.equal(frame.cells.length, 0);
  assert.equal(frame.verdict.unsold, true);
});

// ----------------------------------------------------------------------------------- frames

test('at the start every cell is dark and the prices are all on show', () => {
  const frame = frameAt(sold, 0);
  assert.equal(frame.phase, 'intro');
  assert.equal(frame.lit, 0);
  assert.equal(frame.price, null);
  assert.deepEqual(frame.cells.map((c) => c.price), LADDER);
  assert.ok(frame.cells.every((c) => c.state === 'dim' && c.buyer === null));
  assert.equal(frame.buyer, null);
  assert.equal(frame.verdict, null);
  assert.equal(frame.reveal, null);
  assert.equal(frame.rested, false);
});

test('the cells light from the highest price down, one at a time, the newest one marked', () => {
  const plan = layout(sold);
  for (let i = 0; i < 3; i += 1) {
    const frame = frameAt(sold, plan.cellAt(i));
    assert.equal(frame.phase, 'falling');
    assert.equal(frame.lit, i + 1);
    assert.equal(frame.price, LADDER[i]);
    assert.deepEqual(frame.cells.map((c) => c.state), [...Array(i).fill('lit'), 'now', ...Array(9 - i).fill('dim')]);
    const between = frameAt(sold, plan.cellAt(i) + TIMING.step * 0.5);
    assert.equal(between.lit, i + 1, 'a cell stays the current one for its whole step');
  }
});

test('the clock stops on the cell of the sale, and nothing below it ever lights', () => {
  const plan = layout(sold);
  const frame = frameAt(sold, plan.restAt);
  assert.equal(frame.phase, 'rest');
  assert.equal(frame.rested, true);
  assert.equal(frame.lit, 4);
  assert.deepEqual(frame.cells.map((c) => c.state), ['lit', 'lit', 'lit', 'stop', ...Array(6).fill('dim')]);
  assert.deepEqual(frame.buyer, { seat: 2, price: 280, level: 4 });
  assert.equal(frame.cells[3].buyer, 2, 'the buyer is named on the cell where it bought');
  assert.ok(frame.cells.filter((c) => c.buyer !== null).length === 1);
  for (const t of [plan.restAt + 0.3, plan.verdictAt - 0.01, plan.verdictAt, duration(sold)]) {
    const later = frameAt(sold, t);
    assert.equal(later.lit, 4);
    assert.equal(later.cells[3].state, 'stop');
    assert.equal(later.price, 280);
  }
});

test('an unsold clock lights every cell and rests on the last one with nobody on it', () => {
  const plan = layout(none);
  const frame = frameAt(none, plan.restAt);
  assert.equal(frame.lit, 10);
  assert.equal(frame.rested, true);
  assert.equal(frame.buyer, null);
  assert.ok(frame.cells.every((c) => c.state === 'lit' && c.buyer === null));
  assert.equal(frame.price, 40);
  const hammer = frameAt(none, plan.verdictAt);
  assert.equal(hammer.verdict.unsold, true);
  assert.equal(hammer.verdict.winner, null);
});

test('the hammer is the winner and price of the replay', () => {
  const hammer = frameAt(sold, layout(sold).verdictAt);
  assert.equal(hammer.phase, 'verdict');
  assert.equal(hammer.verdict.winner, 2);
  assert.equal(hammer.verdict.price, 280);
  assert.deepEqual(hammer.verdict.payments, [{ seat: 2, amount: 280 }]);
  assert.equal(frameAt(sold, duration(sold)).phase, 'done');
  assert.equal(frameAt(sold, layout(sold).verdictAt - 0.01).verdict, null);
});

test('the winner and the price are read, not worked out from the ladder', () => {
  const lot = lotOf({ level: 4, buyer: 2, winner: 1, price: 123 });
  const frame = frameAt(lot, duration(lot));
  assert.equal(frame.verdict.winner, 1);
  assert.equal(frame.verdict.price, 123);
  assert.equal(frame.cells[3].price, 280, 'the cell keeps the price of the ladder');
});

test('over time the clock only goes forward, and light is never taken back', () => {
  const rank = { intro: 0, falling: 1, rest: 2, verdict: 3, done: 4 };
  for (const lot of [sold, none]) {
    let phase = 0;
    let lit = 0;
    const total = duration(lot);
    for (let i = 0; i <= 500; i += 1) {
      const frame = frameAt(lot, (total * i) / 500);
      assert.ok(rank[frame.phase] >= phase, `phase went back at ${frame.t}`);
      assert.ok(frame.lit >= lit, `a cell went dark at ${frame.t}`);
      phase = rank[frame.phase];
      lit = frame.lit;
    }
    assert.equal(phase, rank.done);
  }
});

test('until the clock rests the picture names nobody: no buyer and no seat anywhere in a frame', () => {
  const plan = layout(sold);
  for (let i = 0; i < 200; i += 1) {
    const frame = frameAt(sold, plan.restAt * (i / 200));
    assert.equal(frame.buyer, null);
    assert.ok(frame.cells.every((c) => c.buyer === null));
    assert.ok(!/seat/.test(JSON.stringify(frame)), `at ${frame.t}`);
  }
});

test('the grab prices never enter a frame or the plan: only grabsOf has them', () => {
  const lot = lotOf({ level: 1, buyer: 1, actions: [{ seq: 1, seat: 1, phase: 'dutch', status: 'ok', value: 391 }] });
  assert.equal(grabsOf(lot)[0].grab, 391);
  for (let i = 0; i <= 100; i += 1) {
    const frame = frameAt(lot, (duration(lot) * i) / 100);
    assert.ok(!JSON.stringify(frame).includes('391'));
  }
  assert.ok(!JSON.stringify(layout(lot)).includes('391'));
});

test('time outside the animation is clamped and nonsense counts as zero', () => {
  assert.equal(frameAt(sold, -10).t, 0);
  assert.equal(frameAt(sold, 1e9).t, duration(sold));
  assert.equal(frameAt(sold, 1e9).phase, 'done');
  for (const bad of [Number.NaN, undefined, null, 'x', Number.POSITIVE_INFINITY * 0]) assert.equal(frameAt(sold, bad).t, 0);
});

test('frameAt is pure: the lot is not touched and the same time gives the same frame', () => {
  const frozen = deepFreeze(structuredClone(sold));
  for (const t of [0, 1.2, 2.5, 3.1, 100]) {
    const a = frameAt(frozen, t);
    const b = frameAt(frozen, t);
    assert.deepEqual(a, b);
    assert.notEqual(a.cells, b.cells, 'a fresh object each time');
  }
});

// ------------------------------------------------------------------------------ the fixture

test('the Dutch lot of the fixture night, from the file to the hammer', () => {
  const raw = loadFixture(CLOCKS).timeline[1];
  assert.deepEqual(frameAt(raw, duration(raw)), frameAt(fixtureLot, duration(fixtureLot)), 'raw and indexed give the same picture');
  const end = frameAt(fixtureLot, duration(fixtureLot));
  assert.equal(end.verdict.winner, raw.hammer.winner);
  assert.equal(end.verdict.price, raw.hammer.price);
  assert.equal(end.cells[raw.dutch.level - 1].state, 'stop');
  assert.equal(end.cells.filter((c) => c.state === 'lit').length, raw.dutch.level - 1);
});

test('the fixture lot never changes the replay it reads', () => {
  const replay = fixture(CLOCKS);
  const before = JSON.stringify(replay);
  const lot = replay.timeline[1];
  for (let i = 0; i <= 50; i += 1) frameAt(lot, (duration(lot) * i) / 50);
  grabsOf(lot);
  assert.equal(JSON.stringify(replay), before);
});
