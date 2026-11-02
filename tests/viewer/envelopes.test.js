import assert from 'node:assert/strict';
import test from 'node:test';

import { duration, envelopeOrder, frameAt, layout, TIMING } from '../../viewer/anim/envelopes.js';
import { fixture, loadFixture, modelOf, SEALED } from './helpers.js';

const STEP = TIMING.open + TIMING.gap;

/** A sealed lot in the shape of a replay timeline entry (string keys, as in the JSON file). */
function lotOf(format, bids, options = {}) {
  const { winner = null, price = 0, payments, actions = [], true_value, unsold = winner === null, tied = [] } = options;
  const hammer = {
    format,
    winner,
    price,
    payments: payments ?? (winner === null ? {} : { [winner]: price }),
    bids,
    unsold,
    tie_broken: tied.length > 0,
    tied,
    newly_out: [],
  };
  if (true_value !== undefined) hammer.true_value = true_value;
  return { lot: 1, format, active: Object.keys(bids).map(Number), actions, hammer };
}

const sealedAt = (_lot, order) => TIMING.intro + order * STEP;
const verdictAt = (lot) => layout(lot).verdictAt;
const deepFreeze = (x) => {
  if (x && typeof x === 'object') for (const v of Object.values(x)) deepFreeze(v);
  return Object.freeze(x);
};

// -------------------------------------------------------------------------------- order

test('envelopes open from the lowest bid to the highest', () => {
  const lot = lotOf('sealed_first', { 1: 50, 2: 10, 3: 30, 4: 90 }, { winner: 4, price: 90 });
  assert.deepEqual(envelopeOrder(lot).map((r) => r.seat), [2, 3, 1, 4]);
  assert.deepEqual(envelopeOrder(lot).map((r) => r.bid), [10, 30, 50, 90]);
});

test('equal bids: the winner named by the replay opens last, otherwise the lower seat first', () => {
  const bids = { 1: 40, 2: 40, 3: 10, 4: 40 };
  assert.deepEqual(envelopeOrder(lotOf('sealed_first', bids, { winner: 1, price: 40, tied: [1, 2, 4] })).map((r) => r.seat), [3, 2, 4, 1]);
  assert.deepEqual(envelopeOrder(lotOf('sealed_first', bids, { winner: 2, price: 40, tied: [1, 2, 4] })).map((r) => r.seat), [3, 1, 4, 2]);
  assert.deepEqual(envelopeOrder(lotOf('sealed_first', bids, { winner: 4, price: 40, tied: [1, 2, 4] })).map((r) => r.seat), [3, 1, 2, 4]);
  assert.deepEqual(envelopeOrder(lotOf('sealed_first', { 1: 5, 2: 5 })).map((r) => r.seat), [1, 2], 'unsold: by seat');
});

test('zero bids and abstentions open first', () => {
  const lot = lotOf('sealed_second', { 1: 0, 2: 0, 3: 5 }, {
    winner: 3,
    price: 0,
    actions: [{ seq: 1, seat: 1, phase: 'sealed', status: 'abstain', reason: 'timeout' }],
  });
  assert.deepEqual(envelopeOrder(lot).map((r) => [r.seat, r.status]), [[1, 'abstain'], [2, 'ok'], [3, 'ok']]);
});

test('the order depends on the bids only: the same bids in another seat order open the same way', () => {
  const a = envelopeOrder(lotOf('all_pay', { 1: 7, 2: 3, 3: 9 }, { winner: 3, price: 9 }));
  const b = envelopeOrder(lotOf('all_pay', { 3: 9, 2: 3, 1: 7 }, { winner: 3, price: 9 }));
  assert.deepEqual(a, b);
});

// ---------------------------------------------------------------------------------- frames

test('at the start every envelope is closed and shows nothing', () => {
  const lot = lotOf('sealed_first', { 1: 60, 2: 70 }, { winner: 2, price: 70, actions: [{ seq: 1, seat: 1, phase: 'sealed', status: 'clamped', value: 60, raw_value: 99 }] });
  const frame = frameAt(lot, 0);
  assert.equal(frame.phase, 'intro');
  assert.equal(frame.t, 0);
  for (const e of frame.envelopes) {
    assert.equal(e.state, 'sealed');
    assert.equal(e.progress, 0);
    assert.equal(e.bid, null);
    assert.equal(e.status, null);
    assert.equal(e.reason, null);
    assert.equal(e.raw_value, null);
    assert.equal(e.winner, false);
    assert.equal(e.pays, null);
  }
  assert.equal(frame.focus, null);
  assert.equal(frame.leader, null);
  assert.equal(frame.verdict, null);
  assert.equal(frame.reveal, null);
  assert.ok(!JSON.stringify(frame).includes('70'), 'no bid leaks into a closed frame');
});

test('envelopes open one after the other, each swinging open over its own second', () => {
  const lot = lotOf('sealed_first', { 1: 50, 2: 10, 3: 30 }, { winner: 1, price: 50 });
  const order = envelopeOrder(lot).map((r) => r.seat);
  for (let i = 0; i < 3; i += 1) {
    const frame = frameAt(lot, sealedAt(lot, i) + TIMING.open / 2);
    assert.equal(frame.phase, 'opening');
    assert.equal(frame.focus, order[i]);
    frame.envelopes.forEach((e, k) => {
      assert.equal(e.state, k < i ? 'open' : k === i ? 'opening' : 'sealed', `envelope ${k} while ${i} opens`);
    });
    assert.equal(frame.envelopes[i].progress, 0.5);
    assert.equal(frame.envelopes[i].bid, [10, 30, 50][i], 'the bid shows as soon as the envelope starts to open');
  }
});

test('between two envelopes nobody is opening', () => {
  const lot = lotOf('sealed_first', { 1: 50, 2: 10 }, { winner: 1, price: 50 });
  const frame = frameAt(lot, TIMING.intro + TIMING.open + TIMING.gap / 2);
  assert.equal(frame.focus, null);
  assert.deepEqual(frame.envelopes.map((e) => e.state), ['open', 'sealed']);
});

test('the leader is the highest open bid, and a tie goes to the one opened later', () => {
  const lot = lotOf('sealed_first', { 1: 50, 2: 10, 3: 30 }, { winner: 1, price: 50 });
  assert.equal(frameAt(lot, sealedAt(lot, 0) + TIMING.open + 0.01).leader, 2);
  assert.equal(frameAt(lot, sealedAt(lot, 1) + TIMING.open + 0.01).leader, 3);
  assert.equal(frameAt(lot, sealedAt(lot, 2) + TIMING.open + 0.01).leader, 1);
  const tie = lotOf('sealed_first', { 1: 40, 2: 40 }, { winner: 2, price: 40, tied: [1, 2] });
  assert.equal(frameAt(tie, sealedAt(tie, 1) - 0.01).leader, 1, 'the first of the tie is ahead until the second opens');
  assert.equal(frameAt(tie, sealedAt(tie, 1) + TIMING.open + 0.01).leader, 2);
});

test('a lot where nobody bid has no leader', () => {
  const lot = lotOf('sealed_first', { 1: 0, 2: 0 });
  assert.equal(frameAt(lot, sealedAt(lot, 1) + TIMING.open + 0.01).leader, null);
});

// ------------------------------------------------------------- winner and price: the replay

test('the hammer shows the winner, price and payments of the replay', () => {
  const lot = lotOf('sealed_second', { 1: 50, 2: 70, 3: 30 }, { winner: 2, price: 50 });
  const frame = frameAt(lot, verdictAt(lot));
  assert.equal(frame.phase, 'verdict');
  assert.deepEqual(frame.verdict, {
    unsold: false,
    winner: 2,
    price: 50,
    payments: [{ seat: 2, amount: 50 }],
    rule: 'second',
    blind: false,
    tie_broken: false,
    tied: [],
    newly_out: [],
  });
  assert.equal(frame.leader, 2);
  assert.deepEqual(frame.envelopes.filter((e) => e.winner).map((e) => e.seat), [2]);
  assert.equal(frame.envelopes.find((e) => e.seat === 2).pays, 50);
});

test('the winner and the price are read, never recomputed', () => {
  // The bids say seat 2 is highest and a first price auction would charge 70. The replay says
  // otherwise, and the frame must follow the replay.
  const lot = lotOf('sealed_first', { 1: 50, 2: 70, 3: 30 }, { winner: 1, price: 33, payments: { 1: 33 } });
  const frame = frameAt(lot, verdictAt(lot));
  assert.equal(frame.verdict.winner, 1);
  assert.equal(frame.verdict.price, 33);
  assert.deepEqual(frame.verdict.payments, [{ seat: 1, amount: 33 }]);
  assert.deepEqual(frame.envelopes.filter((e) => e.winner).map((e) => e.seat), [1]);
  assert.equal(frame.leader, 1);
  assert.equal(frame.envelopes.find((e) => e.seat === 2).pays, 0);
});

test('a tie is reported as the replay recorded it', () => {
  const lot = lotOf('sealed_first', { 1: 40, 2: 40, 3: 10 }, { winner: 2, price: 40, tied: [1, 2] });
  const { verdict } = frameAt(lot, verdictAt(lot));
  assert.equal(verdict.tie_broken, true);
  assert.deepEqual(verdict.tied, [1, 2]);
  assert.equal(verdict.winner, 2);
});

test('seats that ran out of budget are listed as the replay gives them', () => {
  const lot = lotOf('sealed_first', { 1: 40, 2: 10 }, { winner: 1, price: 40 });
  lot.hammer.newly_out = [1];
  assert.deepEqual(frameAt(lot, verdictAt(lot)).verdict.newly_out, [1]);
});

test('an unsold lot has no winner and nobody pays', () => {
  const lot = lotOf('sealed_second', { 1: 0, 2: 0 });
  const frame = frameAt(lot, verdictAt(lot));
  assert.equal(frame.verdict.unsold, true);
  assert.equal(frame.verdict.winner, null);
  assert.equal(frame.verdict.price, 0);
  assert.deepEqual(frame.verdict.payments, []);
  assert.equal(frame.leader, null);
  assert.ok(frame.envelopes.every((e) => !e.winner && e.pays === 0));
});

test('a lot below the reserve is unsold even though bids were made', () => {
  const lot = lotOf('sealed_first', { 1: 5, 2: 8 }, { winner: null, unsold: true });
  const frame = frameAt(lot, verdictAt(lot));
  assert.equal(frame.verdict.unsold, true);
  assert.deepEqual(frame.envelopes.map((e) => e.bid), [5, 8], 'the bids still open, the hammer says unsold');
});

// ---------------------------------------------------------------------------- all-pay

test('all-pay: every seat that bid pays its own bid, and every seat is shown paying', () => {
  const lot = lotOf('all_pay', { 1: 35, 2: 20, 3: 55, 4: 0 }, {
    winner: 3,
    price: 55,
    payments: { 1: 35, 2: 20, 3: 55 },
  });
  const frame = frameAt(lot, verdictAt(lot));
  assert.equal(frame.verdict.rule, 'all_pay');
  assert.deepEqual(frame.verdict.payments, [{ seat: 1, amount: 35 }, { seat: 2, amount: 20 }, { seat: 3, amount: 55 }]);
  const pays = Object.fromEntries(frame.envelopes.map((e) => [e.seat, e.pays]));
  assert.deepEqual(pays, { 1: 35, 2: 20, 3: 55, 4: 0 });
  assert.equal(frame.envelopes.reduce((sum, e) => sum + e.pays, 0), 110);
  assert.equal(frameAt(lot, verdictAt(lot) - 0.01).envelopes.every((e) => e.pays === null), true, 'nothing is paid before the hammer');
});

test('first and second price: only the winner pays', () => {
  for (const [format, rule] of [['sealed_first', 'first'], ['sealed_second', 'second'], ['blind_box', 'second']]) {
    const lot = lotOf(format, { 1: 50, 2: 70 }, { winner: 2, price: format === 'sealed_first' ? 70 : 50, true_value: format === 'blind_box' ? 60 : undefined });
    const frame = frameAt(lot, verdictAt(lot));
    assert.equal(frame.verdict.rule, rule);
    assert.deepEqual(frame.envelopes.map((e) => e.pays > 0), [false, true], format);
  }
});

// -------------------------------------------------------------------------- blind box

test('the blind box opens its true value after the hammer', () => {
  const lot = lotOf('blind_box', { 1: 190, 2: 82, 3: 204 }, { winner: 3, price: 190, true_value: 176 });
  const { revealAt } = layout(lot);
  assert.equal(frameAt(lot, verdictAt(lot) + 0.01).reveal, null, 'the box is still shut at the hammer');
  assert.equal(frameAt(lot, revealAt - 0.01).reveal, null);
  const opened = frameAt(lot, revealAt);
  assert.equal(opened.phase, 'reveal');
  assert.deepEqual(opened.reveal, { true_value: 176, delta: 14 });
  assert.equal(opened.verdict.winner, 3, 'the verdict stays on screen');
  assert.deepEqual(frameAt(lot, duration(lot)).reveal, { true_value: 176, delta: 14 });
});

test('the blind box reveal says how far the price was from the truth, either way', () => {
  const under = lotOf('blind_box', { 1: 100, 2: 120 }, { winner: 2, price: 100, true_value: 160 });
  assert.equal(frameAt(under, duration(under)).reveal.delta, -60);
  const exact = lotOf('blind_box', { 1: 100, 2: 120 }, { winner: 2, price: 100, true_value: 100 });
  assert.equal(frameAt(exact, duration(exact)).reveal.delta, 0);
});

test('the true value may come from the context when the hammer lacks it', () => {
  const lot = lotOf('blind_box', { 1: 100, 2: 120 }, { winner: 2, price: 100 });
  assert.equal(frameAt(lot, duration(lot)).reveal.true_value, null);
  assert.deepEqual(frameAt(lot, duration(lot), { trueValue: 90 }).reveal, { true_value: 90, delta: 10 });
});

test('an unsold blind box still opens, with no price to compare', () => {
  const lot = lotOf('blind_box', { 1: 0, 2: 0 }, { true_value: 150 });
  assert.deepEqual(frameAt(lot, duration(lot)).reveal, { true_value: 150, delta: null });
});

test('other formats have no reveal beat', () => {
  for (const format of ['sealed_first', 'sealed_second', 'all_pay']) {
    const lot = lotOf(format, { 1: 5, 2: 9 }, { winner: 2, price: 9, true_value: 77 });
    assert.equal(frameAt(lot, duration(lot)).reveal, null, format);
    assert.equal(duration(lot), TIMING.intro + 2 * STEP + TIMING.verdict + TIMING.tail);
  }
});

// ------------------------------------------------------------------ abstain and clamp

test('abstentions and clamped bids are explained once their envelope is open', () => {
  const lot = lotOf('blind_box', { 1: 0, 2: 82, 3: 150 }, {
    winner: 3,
    price: 82,
    true_value: 100,
    actions: [
      { seq: 1, seat: 1, phase: 'sealed', status: 'abstain', reason: 'timeout', value: null, raw_value: null },
      { seq: 2, seat: 2, phase: 'sealed', status: 'clamped', value: 82, raw_value: 140 },
    ],
  });
  const early = frameAt(lot, sealedAt(lot, 0) - 0.01);
  assert.ok(early.envelopes.every((e) => e.status === null && e.reason === null && e.raw_value === null));
  const open = frameAt(lot, sealedAt(lot, 2) + TIMING.open);
  const bySeat = Object.fromEntries(open.envelopes.map((e) => [e.seat, e]));
  assert.deepEqual([bySeat[1].status, bySeat[1].reason, bySeat[1].bid], ['abstain', 'timeout', 0]);
  assert.deepEqual([bySeat[2].status, bySeat[2].raw_value, bySeat[2].bid], ['clamped', 140, 82]);
  assert.deepEqual([bySeat[3].status, bySeat[3].reason, bySeat[3].raw_value], ['ok', null, null], 'a seat with no action record is a bot: plain bid');
});

// ----------------------------------------------------------------------------- edges

test('time outside the animation is clamped and nonsense counts as zero', () => {
  const lot = lotOf('sealed_first', { 1: 5, 2: 9 }, { winner: 2, price: 9 });
  assert.equal(frameAt(lot, -10).t, 0);
  assert.equal(frameAt(lot, 1e9).t, duration(lot));
  assert.equal(frameAt(lot, 1e9).phase, 'done');
  for (const bad of [Number.NaN, undefined, null, 'x', Number.POSITIVE_INFINITY * 0]) assert.equal(frameAt(lot, bad).t, 0);
});

test('without a bids record the actions and the active seats stand in', () => {
  const lot = lotOf('sealed_second', { 1: 5, 2: 9, 3: 7 }, { winner: 2, price: 7 });
  lot.hammer.bids = null;
  lot.active = [1, 2, 3, 4];
  lot.actions = [
    { seq: 1, seat: 1, phase: 'sealed', status: 'ok', value: 5 },
    { seq: 2, seat: 2, phase: 'sealed', status: 'ok', value: 9 },
    { seq: 3, seat: 3, phase: 'sealed', status: 'abstain', reason: 'missing' },
  ];
  assert.deepEqual(envelopeOrder(lot).map((r) => [r.seat, r.bid]), [[3, 0], [4, 0], [1, 5], [2, 9]]);
  assert.equal(frameAt(lot, duration(lot)).verdict.winner, 2);
});

test('a lot with no envelopes still reaches its hammer', () => {
  const lot = lotOf('sealed_first', {});
  assert.equal(duration(lot), TIMING.intro + TIMING.verdict + TIMING.tail);
  assert.equal(frameAt(lot, 0).envelopes.length, 0);
  assert.equal(frameAt(lot, TIMING.intro).verdict.unsold, true);
});

test('duration is the sum of its parts', () => {
  for (const n of [1, 2, 6, 8]) {
    const bids = Object.fromEntries(Array.from({ length: n }, (_, i) => [i + 1, i + 1]));
    const first = lotOf('sealed_first', bids, { winner: n, price: n });
    const blind = lotOf('blind_box', bids, { winner: n, price: n - 1, true_value: 5 });
    assert.ok(Math.abs(duration(first) - (TIMING.intro + n * STEP + TIMING.verdict + TIMING.tail)) < 1e-9);
    assert.ok(Math.abs(duration(blind) - (TIMING.intro + n * STEP + TIMING.verdict + TIMING.reveal + TIMING.tail)) < 1e-9);
  }
});

test('over time the picture only moves forward', () => {
  const lot = lotOf('blind_box', { 1: 10, 2: 20, 3: 30, 4: 40 }, { winner: 4, price: 30, true_value: 35 });
  const rank = { intro: 0, opening: 1, verdict: 2, reveal: 3, done: 4 };
  let phase = 0;
  let opened = 0;
  let hammered = false;
  let revealed = false;
  const total = duration(lot);
  for (let i = 0; i <= 400; i += 1) {
    const frame = frameAt(lot, (total * i) / 400);
    assert.ok(rank[frame.phase] >= phase, `phase went back at ${frame.t}`);
    phase = rank[frame.phase];
    const open = frame.envelopes.filter((e) => e.state === 'open').length;
    assert.ok(open >= opened);
    opened = open;
    for (const e of frame.envelopes) assert.ok(e.progress >= 0 && e.progress <= 1);
    assert.ok(!hammered || frame.verdict !== null);
    assert.ok(!revealed || frame.reveal !== null);
    hammered = frame.verdict !== null;
    revealed = frame.reveal !== null;
  }
  assert.equal(phase, rank.done);
  assert.ok(hammered && revealed);
});

test('frameAt is pure: the lot is not touched and the same time gives the same frame', () => {
  const lot = deepFreeze(lotOf('all_pay', { 1: 35, 2: 20, 3: 55 }, { winner: 3, price: 55, payments: { 1: 35, 2: 20, 3: 55 } }));
  for (const t of [0, 1.7, 4.2, 9, 100]) {
    const a = frameAt(lot, t);
    const b = frameAt(lot, t);
    assert.deepEqual(a, b);
    assert.notEqual(a, b, 'a fresh object each time');
    assert.notEqual(a.envelopes, b.envelopes);
  }
});

// ----------------------------------------------------------------------- the fixture

test('every sealed lot of the fixture night ends on exactly what the replay recorded', () => {
  const model = modelOf(SEALED);
  const raw = loadFixture(SEALED);
  for (const entry of raw.timeline) {
    const indexed = model.lot(entry.lot);
    const end = duration(entry);
    const frame = frameAt(entry, end);
    assert.deepEqual(frame, frameAt(indexed, end), `lot ${entry.lot}: a raw entry and an indexed lot give the same picture`);
    assert.equal(frame.verdict.winner, entry.hammer.winner);
    assert.equal(frame.verdict.price, entry.hammer.price);
    assert.deepEqual(frame.verdict.payments.map((p) => [p.seat, p.amount]), Object.entries(entry.hammer.payments).map(([s, a]) => [Number(s), a]));
    const bids = frame.envelopes.map((e) => e.bid);
    assert.equal(bids.length, Object.keys(entry.hammer.bids).length);
    assert.deepEqual(bids, [...bids].sort((a, b) => a - b), 'opened from lowest to highest');
    assert.equal(frame.envelopes.at(-1).seat, entry.hammer.winner, 'the winner opened last (no ties in this night)');
    assert.deepEqual(frame.envelopes.filter((e) => e.winner).map((e) => e.seat), [entry.hammer.winner]);
  }
});

test('the blind box of the fixture night opens at 176 and the winner overpaid by 14', () => {
  const lot = modelOf(SEALED).lot(4);
  assert.deepEqual(frameAt(lot, duration(lot)).reveal, { true_value: 176, delta: 14 });
});

test('the fixture night never changes the replay it reads', () => {
  const replay = fixture(SEALED);
  const before = JSON.stringify(replay);
  for (const entry of replay.timeline) for (let i = 0; i <= 50; i += 1) frameAt(entry, (duration(entry) * i) / 50);
  assert.equal(JSON.stringify(replay), before);
});
