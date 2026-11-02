import assert from 'node:assert/strict';
import test from 'node:test';

import { indexReplay, SCHEMA_ID, validateReplay } from '../../viewer/lib/replay.js';
import { CLOCKS, fixture, hasHidden, loadFixture, MOCK, modelOf, PAYLOADS, poison, SEALED, strings } from './helpers.js';

// ------------------------------------------------------------------------------ checking

test('both fixtures pass the shape check', () => {
  for (const name of [SEALED, CLOCKS]) assert.deepEqual(validateReplay(loadFixture(name)), { ok: true, errors: [] }, name);
});

function broken(change, expected) {
  const replay = fixture(SEALED);
  change(replay);
  const result = validateReplay(replay);
  assert.equal(result.ok, false);
  assert.match(result.errors.join(' '), expected);
}

test('a file that is not an object is refused', () => {
  for (const value of [null, 3, 'x', [], undefined]) assert.equal(validateReplay(value).ok, false);
});

test('a missing or foreign schema is refused with a reason', () => {
  broken((r) => delete r.schema, /schema/);
  broken((r) => (r.schema = 'auction-night/replay@2'), /replay@2/);
  broken((r) => (r.schema = 'something/else'), /schema/);
  assert.equal(SCHEMA_ID, 'auction-night/replay@1');
});

test('missing or invalid required parts are named', () => {
  broken((r) => delete r.night, /night/);
  broken((r) => (r.night.n_lots = 0), /n_lots/);
  broken((r) => (r.night.n_lots = 37), /n_lots/);
  broken((r) => delete r.night.id, /night\.id/);
  broken((r) => (r.seats = []), /seats/);
  broken((r) => (r.seats[1].seat = r.seats[0].seat), /seats\[1\]/);
  broken((r) => (r.seats[0].seat = 9), /seats\[0\]/);
  broken((r) => r.catalog.pop(), /catalog/);
  broken((r) => (r.catalog[0].name = 3), /catalog\[0\]/);
  broken((r) => (r.timeline = []), /timeline/);
  broken((r) => r.timeline.reverse(), /timeline/);
  broken((r) => delete r.timeline[0].hammer, /timeline\[0\]/);
  broken((r) => (r.timeline[0].lot = 99), /timeline\[0\]/);
  broken((r) => delete r.reveal, /reveal/);
});

test('the list of problems stays short', () => {
  const replay = fixture(SEALED);
  replay.seats = replay.seats.map(() => ({}));
  replay.timeline = replay.timeline.map(() => ({}));
  assert.ok(validateReplay(replay).errors.length <= 12);
});

test('unknown extra fields and enum values are tolerated', () => {
  const replay = fixture(SEALED);
  replay.surprise = { deep: [1, 2, 3] };
  replay.night.weather = 'rain';
  replay.timeline[0].format = 'future_format';
  replay.seats[0].kind = 'cyborg';
  replay.timeline[0].actions[0].status = 'maybe';
  assert.equal(validateReplay(replay).ok, true);
  const model = indexReplay(replay);
  assert.equal(model.lot(1).format, 'sealed_first', 'an unknown format falls back to the catalog entry');
  assert.equal(model.seat(1).kind, 'unknown');
  assert.equal(model.lot(1).actions[0].status, 'unknown');
});

// ------------------------------------------------------------------------------ the index

test('the model of the sealed night', () => {
  const model = modelOf(SEALED);
  assert.equal(model.night.id, 'fixture-sealed-0001');
  assert.equal(model.night.n_lots, 4);
  assert.deepEqual(model.lotNos, [1, 2, 3, 4]);
  assert.deepEqual(model.seatNos, [1, 2, 3, 4, 5, 6]);
  assert.equal(model.codename(1), '鲸落');
  assert.equal(model.codename(7), '7 号', 'an unknown seat still has a name');
  assert.deepEqual(model.catalogLots.map((c) => c.name).length, 4);
  assert.equal(model.lot(2).item.name, '永远指向昨天的指南针');
  assert.equal(model.lot(2).format, 'sealed_second');
  assert.equal(model.lot(9), null);
});

test('hammer numbers are integers keyed by seat', () => {
  const lot = modelOf(SEALED).lot(1);
  assert.deepEqual(lot.hammer.bids, { 1: 30, 2: 66, 3: 0, 4: 74, 5: 62, 6: 40 });
  assert.deepEqual(lot.hammer.payments, { 4: 74 });
  assert.equal(lot.hammer.winner, 4);
  assert.equal(lot.hammer.price, 74);
  assert.equal(lot.hammer.unsold, false);
  assert.deepEqual(lot.talk_order, [1, 2, 3, 4, 5, 6]);
  assert.equal(lot.talk.length, 6);
  assert.deepEqual(lot.actions[2], { seq: lot.actions[2].seq, seat: 3, phase: 'sealed', status: 'abstain', reason: 'timeout', value: null, raw_value: null });
});

test('the true value of a blind box comes from the hammer, or from the reveal', () => {
  const raw = fixture(SEALED);
  assert.equal(indexReplay(raw).lot(4).hammer.true_value, 176);
  delete raw.timeline[3].hammer.true_value;
  assert.equal(indexReplay(raw).lot(4).hammer.true_value, 176, 'filled from reveal.true_values');
  assert.equal(indexReplay(raw).lot(1).hammer.true_value, null);
});

test('English and Dutch lots keep their clocks', () => {
  const model = modelOf(CLOCKS);
  const english = model.lot(1);
  assert.deepEqual(english.dropouts.map((d) => [d.seat, d.price, d.participated]), [
    [4, 0, false],
    [1, 95, true],
    [2, 140, true],
    [5, 165, true],
  ]);
  assert.deepEqual(english.actions.map((a) => a.status), ['ok', 'ok', 'clamped', 'kept_previous', 'ok', 'ok']);
  assert.equal(english.actions[2].raw_value, 300);
  const dutch = model.lot(2);
  assert.deepEqual(dutch.dutch, { ladder: [400, 360, 320, 280, 240, 200, 160, 120, 80, 40], level: 4, price: 280, buyer: 2, unsold: false });
  assert.deepEqual(dutch.sitting_out, [3]);
});

test('the overlay of a lot keeps the numbers of the replay, signs included', () => {
  const model = modelOf(SEALED);
  assert.deepEqual(model.lot(4).overlay, {
    ref_bids: { 1: 181, 2: 82, 3: 187, 4: 151, 5: 164, 6: 145 },
    alpha_milli: { 6: 920 },
    regret: { 1: 0, 2: 0, 3: 14, 4: 0, 5: 0, 6: 0 },
    to_highest_value: null,
    excess: { 1: 0, 2: 0, 3: -14, 4: 0, 5: 0, 6: 0 },
    expected_revenue_milli: 190980,
    curse_gap: 14,
  });
  assert.equal(model.lot(3).overlay.excess[1], -25);
  assert.equal(model.lot(3).overlay.excess[3], 71);
  assert.equal(model.lot(1).overlay.curse_gap, null, 'only a blind box has one');
  assert.equal(modelOf(CLOCKS).lot(1).overlay.expected_revenue_milli, null, 'none with a reserve price');
  assert.equal(modelOf(CLOCKS).lot(2).overlay.expected_revenue_milli, 221175);
});

test('a replay from before the three overlay fields still opens, with nothing in their place', () => {
  const raw = fixture(SEALED);
  for (const lot of raw.timeline) for (const key of ['excess', 'expected_revenue_milli', 'curse_gap']) delete lot.overlay[key];
  assert.equal(validateReplay(raw).ok, true);
  const lot = indexReplay(raw).lot(4);
  assert.deepEqual(lot.overlay.excess, {});
  assert.equal(lot.overlay.expected_revenue_milli, null);
  assert.equal(lot.overlay.curse_gap, null);
  const bare = fixture(SEALED);
  delete bare.timeline[0].overlay;
  assert.deepEqual(indexReplay(bare).lot(1).overlay, {
    ref_bids: {}, alpha_milli: {}, regret: {}, to_highest_value: null, excess: {}, expected_revenue_milli: null, curse_gap: null,
  });
});

test('only excess and the curse gap may be negative, and nothing but whole numbers gets in', () => {
  const raw = fixture(SEALED);
  const overlay = raw.timeline[3].overlay;
  overlay.excess = { 1: -3, 2: 2.5, 3: '4', 4: null, 5: 2 ** 53, 6: 7, 9: 1, nonsense: 1 };
  overlay.curse_gap = -9;
  overlay.expected_revenue_milli = -5;
  overlay.ref_bids = { 1: -1, 2: 10.5, 3: 12 };
  overlay.regret = { 1: -2 };
  let lot = indexReplay(raw).lot(4);
  assert.deepEqual(lot.overlay.excess, { 1: -3, 6: 7 });
  assert.equal(lot.overlay.curse_gap, -9);
  assert.equal(lot.overlay.expected_revenue_milli, null, 'revenue is never negative');
  assert.deepEqual(lot.overlay.ref_bids, { 3: 12 });
  assert.deepEqual(lot.overlay.regret, {});
  overlay.curse_gap = 1.5;
  overlay.expected_revenue_milli = 12.5;
  lot = indexReplay(raw).lot(4);
  assert.equal(lot.overlay.curse_gap, null);
  assert.equal(lot.overlay.expected_revenue_milli, null);
  overlay.curse_gap = '14';
  assert.equal(indexReplay(raw).lot(4).overlay.curse_gap, null);
});

test('keys that are not seat numbers cannot enter the excess table', () => {
  const raw = fixture(SEALED);
  Object.assign(raw.timeline[0].overlay.excess, { constructor: 5, toString: 5, 0: 5, 9: 5, '-1': 5 });
  const lot = indexReplay(raw).lot(1);
  assert.deepEqual(Object.keys(lot.overlay.excess), ['1', '2', '3', '4', '5', '6']);
  assert.equal({}.polluted, undefined);
});

test('the host line comes from the lot, or from host.per_lot', () => {
  const raw = fixture(SEALED);
  const own = indexReplay(raw).lot(2).host;
  assert.match(own, /165/);
  for (const lot of raw.timeline) delete lot.host;
  assert.equal(indexReplay(raw).lot(2).host, own);
  raw.host.per_lot = [];
  assert.equal(indexReplay(raw).lot(2).host, null);
});

test('the budget left before a lot is the budget minus every earlier payment', () => {
  const model = modelOf(SEALED);
  assert.equal(model.remainingBefore(2, 1), 260);
  assert.equal(model.remainingBefore(2, 2), 260);
  assert.equal(model.remainingBefore(2, 3), 102, 'seat 2 paid 158 for lot 2');
  assert.equal(model.remainingBefore(2, 4), 82, 'and 20 for lot 3');
  assert.equal(model.remainingBefore(3, 4), 250);
  assert.equal(model.remainingBefore(9, 1), null);
});

test('reveal records are found by lot and seat', () => {
  const model = modelOf(SEALED);
  assert.equal(model.mission(2).kind, 'shill');
  assert.equal(model.plan(1).budget_plan[3], 220);
  assert.equal(model.guess(1, 1).truth, 260);
  assert.deepEqual(model.thoughts(1, 1).map((t) => t.phase), ['talk', 'sealed']);
  assert.equal(model.planThought(2).phase, 'plan');
  assert.deepEqual(model.thoughts(1, 5), [], 'bots write no monologue');
});

test('a night that stopped early lists the lots that were not played', () => {
  const raw = fixture(SEALED);
  raw.timeline = raw.timeline.slice(0, 2);
  assert.equal(validateReplay(raw).ok, true);
  const model = indexReplay(raw);
  assert.deepEqual(model.lotNos, [1, 2]);
  assert.equal(model.catalogLots.length, 4);
  assert.equal(model.lot(3), null);
});

// ------------------------------------------------------------------ awards and highlights

test('the awards, the reference of the bots and the highlights are in the model', () => {
  const model = modelOf(SEALED);
  assert.equal(model.awards.length, 10);
  assert.deepEqual(model.awards[1], { award: 'winners_curse', winners: [3], metric: 14, metric_unit: 'coins', vacant: false, basis: null });
  assert.deepEqual(model.awards[0], { award: 'most_textbook', winners: [], metric: null, metric_unit: 'none', vacant: true, basis: null });
  assert.deepEqual(model.awards[4].winners, [1, 4]);
  assert.deepEqual(model.botsReference.map((a) => [a.award, a.winners]), [['most_textbook', []], ['winners_curse', []], ['tilt', [5]], ['bargain', []]]);
  assert.equal(model.highlights.length, 7);
  assert.deepEqual(model.highlight(7), {
    id: 7, category: 'winners_curse', score: 8, lot: 4, seats: [3], first_seq: 94, last_seq: 96, text: '鉴定价全场最高，付 190 拿下，开箱真值只有 176。', basis: null,
  });
  assert.equal(model.highlight(99), null);
  assert.equal(modelOf(CLOCKS).highlights.length, 5);
});

test('a replay from before the awards and the highlights still opens, with nothing in their place', () => {
  const raw = fixture(SEALED);
  for (const key of ['awards', 'bots_reference', 'highlights']) delete raw[key];
  assert.equal(validateReplay(raw).ok, true);
  const model = indexReplay(raw);
  assert.deepEqual([model.awards, model.botsReference, model.highlights], [[], [], []]);
  raw.awards = 'nonsense';
  raw.highlights = { 1: 2 };
  raw.bots_reference = null;
  const odd = indexReplay(raw);
  assert.deepEqual([odd.awards, odd.botsReference, odd.highlights], [[], [], []]);
});

test('an award is vacant exactly when it has a valid winner, and its figure is a whole number or nothing', () => {
  const raw = fixture(SEALED);
  raw.awards = [
    { award: 'tilt', winners: [3, 1, 3, 9, 0, '2', null, 1.5], metric: 35, metric_unit: 'coins' },
    { award: 'bargain', winners: [], vacant: false },
    { award: 'keeper', winners: [2], vacant: true, metric: 2.5, metric_unit: 'count' },
    { award: 'best_actor', vacant: true, metric: '3', metric_unit: 'dollars' },
    { award: 'mind_reader', winners: [1], metric: -4, metric_unit: 'milli' },
    { award: 7 },
    'text',
    null,
    { winners: [1] },
  ];
  const awards = indexReplay(raw).awards;
  assert.deepEqual(awards.map((a) => a.award), ['tilt', 'bargain', 'keeper', 'best_actor', 'mind_reader']);
  assert.deepEqual(awards[0].winners, [1, 3], 'sorted, without repeats and without seats that do not exist');
  assert.equal(awards[0].vacant, false);
  assert.equal(awards[1].vacant, true, 'the file says it is not, but nobody won it');
  assert.equal(awards[2].vacant, false, 'the file says it is, but a seat won it');
  assert.equal(awards[2].metric, null, 'half a coin is not a figure');
  assert.equal(awards[3].metric, null);
  assert.equal(awards[3].metric_unit, 'none', 'a unit that is not known is none');
  assert.equal(awards[4].metric, -4, 'a figure may be negative: it is a difference');
});

test('an award that the viewer does not know keeps its place as unknown, and more awards than a night can have are not read', () => {
  const raw = fixture(SEALED);
  raw.awards = [{ award: 'a_future_award', winners: [1], metric: 1, metric_unit: 'count' }];
  assert.equal(indexReplay(raw).awards[0].award, 'unknown');
  raw.awards = Array.from({ length: 500 }, () => ({ award: 'tilt', winners: [1] }));
  assert.equal(indexReplay(raw).awards.length, 40);
});

test('a highlight with no valid id is dropped, a repeated id counts once, and the score stays between 0 and 10', () => {
  const raw = fixture(SEALED);
  const base = { category: 'backstab', lot: 1, seats: [1], first_seq: 18, last_seq: 36, text: 'x' };
  raw.highlights = [
    { id: 1, score: 99, ...base },
    { id: 2, score: -4, ...base },
    { id: 3, score: 4.5, ...base },
    { id: 1, score: 3, ...base, text: 'the second one' },
    { id: 0, score: 3, ...base },
    { id: '5', score: 3, ...base },
    { score: 3, ...base },
    { id: 6, score: 7, ...base, category: 'a_new_category', lot: 99, seats: [1, 1, 9, 2], first_seq: 'a' },
    null,
    'text',
  ];
  const model = indexReplay(raw);
  assert.deepEqual(model.highlights.map((h) => h.id), [1, 2, 3, 6]);
  assert.deepEqual(model.highlights.map((h) => h.score), [10, 0, 0, 7]);
  assert.equal(model.highlight(1).text, 'x', 'the first of two with the same id');
  const odd = model.highlight(6);
  assert.equal(odd.category, 'unknown');
  assert.equal(odd.lot, null, 'a lot that cannot exist is no lot');
  assert.deepEqual(odd.seats, [1, 2]);
  assert.equal(odd.first_seq, 0);
  raw.highlights = Array.from({ length: 500 }, (_, i) => ({ id: i + 1, score: 1, ...base }));
  assert.equal(indexReplay(raw).highlights.length, 200);
});

test('the text of a highlight is clean and short, whatever the file held', () => {
  const raw = fixture(SEALED);
  raw.highlights[0].text = `a${String.fromCharCode(0x202e)}b\u0007c\n${'x'.repeat(5000)}`;
  const text = indexReplay(raw).highlight(1).text;
  assert.ok(!hasHidden(text));
  assert.ok([...text].length <= 600);
  assert.ok(text.endsWith('…'));
});

test('keys that are not award or highlight fields cannot enter the model', () => {
  const raw = fixture(SEALED);
  raw.awards = [{ award: 'tilt', winners: [1], metric: 3, metric_unit: 'count', __proto__: { polluted: 1 }, constructor: 5 }];
  const model = indexReplay(raw);
  assert.deepEqual(Object.keys(model.awards[0]).sort(), ['award', 'basis', 'metric', 'metric_unit', 'vacant', 'winners']);
  assert.equal({}.polluted, undefined);
});

// ------------------------------------------------------------------------- the later fields

test('the basis of an award or a highlight is the word of the file: public, private, or nothing', () => {
  const raw = fixture(SEALED);
  raw.awards = [
    { award: 'tilt', winners: [1], basis: 'private' },
    { award: 'bargain', winners: [3], basis: 'public' },
    { award: 'keeper', winners: [2] },
    { award: 'best_actor', vacant: true, basis: 'secret' },
    { award: 'mind_reader', vacant: true, basis: 7 },
    { award: 'consistent', vacant: true, basis: null },
  ];
  const base = { category: 'backstab', lot: 1, seats: [1], first_seq: 18, last_seq: 36, text: 'x', score: 5 };
  raw.highlights = [
    { id: 1, ...base, basis: 'private' },
    { id: 2, ...base, basis: 'public' },
    { id: 3, ...base },
    { id: 4, ...base, basis: ['public'] },
    { id: 5, ...base, basis: 'Public' },
  ];
  const model = indexReplay(raw);
  assert.deepEqual(model.awards.map((a) => a.basis), ['private', 'public', null, 'private', null, null]);
  assert.deepEqual(model.highlights.map((h) => h.basis), ['private', 'public', null, null, 'private']);
  assert.equal(modelOf(SEALED).awards.every((a) => a.basis === null), true, 'the hand-written night says nothing');
  assert.deepEqual(new Set(modelOf(MOCK).awards.map((a) => a.basis)), new Set(['public', 'private']), 'the exporter says it');
  assert.deepEqual(new Set(modelOf(MOCK).highlights.map((h) => h.basis)), new Set(['public', 'private']));
});

test('the event numbers of the lines and of the hammer are read from the lot, and bad ones are dropped', () => {
  const lot = modelOf(MOCK).lot(2);
  assert.deepEqual(lot.talk_seqs, { 1: 74, 2: 55, 3: 58, 4: 61, 5: 64, 6: 67, 7: 70, 8: 72 });
  assert.equal(lot.hammer_seq, 102);
  assert.equal(lot.hammer_seq, lot.last_seq);
  for (const name of [SEALED, CLOCKS]) {
    const old = modelOf(name).lot(1);
    assert.deepEqual(old.talk_seqs, {}, 'a hand-written night has none');
    assert.equal(old.hammer_seq, null);
  }
  const raw = fixture(MOCK);
  raw.timeline[0].talk_seqs = { 1: 5, 2: 'x', 3: 1.5, 9: 7, 0: 3, 4: -1, 5: 0, foo: 4, 6: 1e300, 7: null, 8: 2 ** 53 };
  raw.timeline[0].hammer_seq = 53.5;
  raw.timeline[1].talk_seqs = [1, 2];
  raw.timeline[1].hammer_seq = 0;
  raw.timeline[2].talk_seqs = 'x';
  raw.timeline[2].hammer_seq = '103';
  const model = indexReplay(raw);
  assert.deepEqual(model.lot(1).talk_seqs, { 1: 5 });
  assert.equal(model.lot(1).hammer_seq, null);
  assert.deepEqual(model.lot(2).talk_seqs, {});
  assert.equal(model.lot(2).hammer_seq, null);
  assert.deepEqual(model.lot(3).talk_seqs, {});
  assert.equal(model.lot(3).hammer_seq, null);
});

test('the findings of the real night: broken pledges, and one red card with the statistic of every lot', () => {
  const model = modelOf(MOCK);
  assert.equal(model.verdicts.length, 13);
  assert.deepEqual(model.verdicts.map((v) => v.kind).filter((k) => k !== 'pledge_violation'), ['collusion_red']);
  const pledge = model.verdicts[0];
  assert.deepEqual(
    { kind: pledge.kind, seats: pledge.seats, lots: pledge.lots, evidence_seqs: pledge.evidence_seqs, quotes: pledge.quotes, topic: pledge.topic, gap: pledge.gap_milli },
    { kind: 'pledge_violation', seats: [5], lots: [1], evidence_seqs: [35, 53], quotes: ['这件我不碰，放心。'], topic: null, gap: null },
  );
  const red = model.verdicts.at(-1);
  assert.deepEqual([red.seats, red.lots, red.threshold_milli], [[2, 4], [5, 6, 7, 9], 265]);
  assert.deepEqual(Object.keys(red.statistic_milli).map(Number), [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]);
  assert.equal(red.statistic_milli[1], -200, 'a statistic can be negative');
  assert.equal(red.statistic_milli[6], 1510);
  assert.equal(model.pledges.length, 32);
  assert.equal(model.pledges.filter((r) => r.outcome === 'violated').length, 12);
  const broken = model.pledges.filter((r) => r.outcome === 'violated');
  assert.deepEqual(broken[0], { seq: 35, lot: 1, seat: 5, pledge: { kind: 'no_bid', lot: 1, amount: null, to: null }, outcome: 'violated' });
  assert.deepEqual(model.pledges[0].pledge, { kind: 'let_win', lot: 1, amount: null, to: 4 }, 'a pledge to step aside, kept');
  assert.deepEqual(new Set(model.pledges.map((r) => r.outcome)), new Set(['kept', 'violated', 'void']));
  assert.deepEqual(model.judgesAbsent, []);
  for (const verdict of model.verdicts) assert.ok(!('intent' in verdict), 'the page never says why a seat did something');
});

test('a false claim keeps its topic and its gap, and the judges that were absent are listed in order', () => {
  const claim = modelOf(SEALED).verdicts.find((v) => v.kind === 'false_claim');
  assert.deepEqual([claim.seats, claim.lots, claim.topic, claim.gap_milli], [[2], [2], 'valuation', 505]);
  assert.deepEqual(modelOf(CLOCKS).judgesAbsent, [
    { judge: 0, vendor: 'vendor-e', reason: 'timeout' },
    { judge: 1, vendor: 'vendor-f', reason: 'unreadable' },
  ]);
  const raw = fixture(CLOCKS);
  raw.judges_absent = [
    { judge: 3, vendor: 'v', reason: 'refusal' },
    { judge: 1, vendor: '', reason: 'a_new_reason' },
    { judge: 1, vendor: 'again', reason: 'timeout' },
    { judge: 4, vendor: 'v', reason: 'other' },
    { judge: -1, vendor: 'v', reason: 'other' },
    { judge: 'x', reason: 'other' },
    null,
  ];
  assert.deepEqual(indexReplay(raw).judgesAbsent, [
    { judge: 1, vendor: null, reason: 'other' },
    { judge: 3, vendor: 'v', reason: 'refusal' },
  ]);
});

test('findings that cannot be read are dropped, odd ones are kept as unknown, and the figures are whole numbers', () => {
  const raw = fixture(SEALED);
  raw.verdicts = [
    { verdict: 'a_future_kind', seats: [1, 1, 9], lots: [2, 2, 40, 0], quotes: [], evidence_seqs: [5, 0, 'x'] },
    { verdict: 'false_claim', seats: [2], lots: [2], topic: 'weather', gap_milli: 1.5, quotes: ['a', '', 'b'], detail: 'd' },
    { verdict: 'false_claim', seats: [2], lots: [2], topic: '', gap_milli: -3 },
    { verdict: 'collusion_suspect', seats: [], lots: [3], statistic_milli: { 1: 5, 99: 3, x: 1, 2: 1.5, 3: -7 }, threshold_milli: '265' },
    { verdict: 7 },
    'text',
    null,
  ];
  const model = indexReplay(raw);
  assert.deepEqual(model.verdicts.map((v) => v.kind), ['unknown', 'false_claim', 'false_claim', 'collusion_suspect']);
  assert.deepEqual(model.verdicts[0].seats, [1], 'a seat that does not exist, and a repeat, are no seats');
  assert.deepEqual(model.verdicts[0].lots, [2]);
  assert.deepEqual(model.verdicts[0].evidence_seqs, [5]);
  assert.deepEqual([model.verdicts[1].topic, model.verdicts[1].gap_milli, model.verdicts[1].quotes], ['unknown', null, ['a', 'b']]);
  assert.deepEqual([model.verdicts[2].topic, model.verdicts[2].gap_milli], [null, null], 'an empty topic is none and a negative gap is none');
  assert.deepEqual(model.verdicts[3].statistic_milli, { 1: 5, 3: -7 });
  assert.equal(model.verdicts[3].threshold_milli, null);
  raw.verdicts = Array.from({ length: 288 }, () => ({ verdict: 'pledge_violation', seats: [1], lots: [1] }));
  assert.equal(indexReplay(raw).verdicts.length, 288, 'every pledge of a night of eight seats and 36 lots can be broken');
  raw.verdicts = Array.from({ length: 900 }, () => ({ verdict: 'pledge_violation', seats: [1], lots: [1] }));
  assert.equal(indexReplay(raw).verdicts.length, 400, 'and no more than that is read');
  raw.verdicts = 'nonsense';
  raw.pledges = { 1: 2 };
  assert.deepEqual([indexReplay(raw).verdicts, indexReplay(raw).pledges], [[], []]);
});

test('the recap of the host keeps its paragraphs, and the prediction card is read with its results', () => {
  const real = modelOf(MOCK);
  assert.ok(real.recap.includes('\n\n'), 'blank lines between the paragraphs');
  assert.equal(real.recap.split('\n').filter(Boolean).length, 5);
  assert.deepEqual(real.predictions.items.map((p) => p.hit), [false, false, true, false]);
  assert.deepEqual(real.predictions.items.map((p) => p.format), ['sealed_first', 'sealed_second', 'blind_box', 'english']);
  assert.equal(real.predictions.hits, 1);
  assert.deepEqual(modelOf(SEALED).predictions.items.map((p) => p.hit), [true, true]);
  assert.equal(modelOf(CLOCKS).predictions, null, 'a night without the card');
  assert.ok(modelOf(CLOCKS).recap.includes('\n'), 'a recap of two lines');
  const raw = fixture(SEALED);
  raw.host.prediction_card = {
    items: [
      { format: 'blind_box', claim: 'a', text: 'one', hit: 'yes' },
      { format: 'a_new_format', claim: '', text: 'two', hit: null },
      { format: 'english', claim: 'c', text: '', hit: true },
      null,
      ...Array.from({ length: 30 }, () => ({ format: 'dutch', claim: 'd', text: 'many', hit: false })),
    ],
    hits: 2.5,
  };
  const card = indexReplay(raw).predictions;
  assert.deepEqual(card.items.slice(0, 2).map((p) => [p.format, p.claim, p.text, p.hit]), [
    ['blind_box', 'a', 'one', null],
    ['unknown', null, 'two', null],
  ]);
  assert.equal(card.items.length, 10, 'twelve entries are read, and two of them cannot be');
  assert.equal(card.hits, null);
  raw.host.prediction_card = { items: [] };
  assert.equal(indexReplay(raw).predictions, null);
  raw.host.prediction_card = 'nothing';
  assert.equal(indexReplay(raw).predictions, null);
});

test('a replay from before the findings and the host extras opens with nothing in their place', () => {
  const raw = fixture(SEALED);
  for (const key of ['verdicts', 'pledges', 'judges_absent', 'judges', 'judge_accuracy']) delete raw[key];
  delete raw.host.recap;
  delete raw.host.prediction_card;
  assert.equal(validateReplay(raw).ok, true);
  const model = indexReplay(raw);
  assert.deepEqual([model.verdicts, model.pledges, model.judgesAbsent, model.recap, model.predictions], [[], [], [], null, null]);
  delete raw.host;
  assert.equal(indexReplay(raw).recap, null);
});

// ---------------------------------------------------------------------------- hostile text

test('every string in the model is clean, whatever the replay held', () => {
  for (const key of ['ansi', 'osc', 'bidi', 'invisible', 'image', 'script']) {
    const model = indexReplay(poison(fixture(SEALED), PAYLOADS[key]));
    for (const text of strings(model)) assert.ok(!hasHidden(text), `${key}: ${JSON.stringify(text.slice(0, 40))}`);
    assert.equal(model.lot(1).talk[0].text.includes('\x1b'), false);
  }
});

test('long text is shortened with an ellipsis', () => {
  const model = modelOf(SEALED);
  const longest = Math.max(...model.lot(4).talk.map((t) => [...t.text].length));
  assert.equal(longest, 240);
  assert.ok(model.lot(4).talk[0].text.endsWith('…'));
  assert.ok([...model.lot(1).item.blurb].length <= 600);
  const raw = fixture(SEALED);
  raw.reveal.thoughts[0].text = 'x'.repeat(5000);
  raw.seats[0].codename = 'y'.repeat(500);
  const long = indexReplay(raw);
  assert.equal([...long.reveal.thoughts[0].text].length, 400);
  assert.equal([...long.seat(1).codename].length, 24);
});

test('non-integer amounts are treated as missing, never shown', () => {
  const raw = fixture(SEALED);
  raw.timeline[0].hammer.price = 70.5;
  raw.timeline[0].hammer.bids['2'] = '66';
  raw.reveal.budgets['1'] = 1e300;
  const model = indexReplay(raw);
  assert.equal(model.lot(1).hammer.price, 0);
  assert.equal(model.lot(1).hammer.bids[2], undefined);
  assert.equal(model.reveal.budgets[1], undefined);
});

test('keys that are not seat numbers cannot enter the model', () => {
  const raw = fixture(SEALED);
  raw.timeline[0].hammer.bids['__proto__'] = 5;
  raw.timeline[0].hammer.bids['constructor'] = 5;
  raw.timeline[0].hammer.bids['99'] = 5;
  raw.reveal.budgets['toString'] = 5;
  const model = indexReplay(raw);
  assert.deepEqual(Object.keys(model.lot(1).hammer.bids), ['1', '2', '3', '4', '5', '6']);
  assert.deepEqual(Object.keys(model.reveal.budgets), ['1', '2', '3', '4', '5', '6']);
  assert.equal({}.polluted, undefined);
});
