import assert from 'node:assert/strict';
import test from 'node:test';

import { animationFor } from '../../viewer/anim/index.js';
import { indexReplay } from '../../viewer/lib/replay.js';
import { planRecap, RECAP, recapAt, segmentStart } from '../../viewer/lib/recap.js';
import { formatPageHash } from '../../viewer/lib/route.js';
import { buildScript } from '../../viewer/lib/script.js';
import { STR } from '../../viewer/lib/strings.js';
import { mountRecapBar } from '../../viewer/ui/recap.js';
import { FakeDocument } from './fake_dom.js';
import { CLOCKS, fixture, modelOf, SEALED, twelveLotReplay } from './helpers.js';

const sealed = modelOf(SEALED);
const clocks = modelOf(CLOCKS);
const near = (a, b, message) => assert.ok(Math.abs(a - b) < 1e-6, `${message ?? ''} ${a} is not ${b}`);
const round = (x) => Math.round(x * 1000) / 1000;

/** What the stage would answer: the script of each lot that was played. */
function scriptsOf(model) {
  const scripts = new Map(model.lots.map((lot) => [lot.lot, buildScript(lot, animationFor(lot.format).duration(lot))]));
  return (lotNo) => scripts.get(lotNo) ?? null;
}
const planOf = (model, options) => planRecap(model, scriptsOf(model), options);

/** A night of twelve lots, with a highlight of the given score on each lot. */
const twelveLots = (scores) => indexReplay(twelveLotReplay(scores));

// ------------------------------------------------------------------------ the constants

test('about three minutes, a short card, and a window of the lot that is neither a blink nor the whole lot', () => {
  assert.equal(RECAP.budget, 180);
  assert.equal(RECAP.card, 2);
  assert.ok(RECAP.minSegment >= 10 && RECAP.maxSegment <= 40 && RECAP.minSegment < RECAP.maxSegment);
  assert.ok(Object.isFrozen(RECAP));
});

// ---------------------------------------------------------------------------- the plan

test('the plan of the sealed night, locked: which lots, which highlights, from where, how long', () => {
  const plan = planOf(sealed);
  assert.deepEqual(plan.segments.map((s) => [s.index, s.lot, s.ids]), [[1, 1, [1, 2]], [2, 2, [3, 4]], [3, 3, [5, 6]], [4, 4, [7]]]);
  assert.deepEqual(plan.segments.map((s) => [round(s.from), round(s.to), round(s.length), round(s.start)]), [
    [1.2, 27.77, 28.57, 0],
    [0, 27.865, 29.865, 28.57],
    [0, 29.615, 31.615, 58.435],
    [14.45, 32.15, 19.7, 90.05],
  ]);
  assert.equal(round(plan.length), 109.75);
  assert.deepEqual(plan.segments.map((s) => s.score), [6, 9, 5, 8], 'the best score of each segment');
});

test('the plan of the English and Dutch night, locked', () => {
  const plan = planOf(clocks);
  assert.deepEqual(plan.segments.map((s) => [s.index, s.lot, s.ids, round(s.from), round(s.to)]), [
    [1, 1, [1, 4, 2, 3], 0, 22.07],
    [2, 2, [5], 1.2, 19.655],
  ]);
  assert.equal(round(plan.length), 44.525);
});

test('the same replay always gives the same segments and the same running time', () => {
  const first = JSON.stringify(planOf(sealed));
  for (let i = 0; i < 5; i += 1) {
    assert.equal(JSON.stringify(planOf(modelOf(SEALED))), first, `run ${i}`);
  }
  // The order of the file does not matter.
  const raw = fixture(SEALED);
  raw.highlights.reverse();
  assert.equal(JSON.stringify(planOf(indexReplay(raw))), first);
  const other = fixture(SEALED);
  other.highlights = [...other.highlights.slice(3), ...other.highlights.slice(0, 3)];
  assert.equal(JSON.stringify(planOf(indexReplay(other))), first);
});

test('planning changes nothing it is given', () => {
  const before = JSON.stringify([sealed.highlights, sealed.lots]);
  planOf(sealed);
  assert.equal(JSON.stringify([sealed.highlights, sealed.lots]), before);
});

test('every segment is a card followed by a window of its lot that ends with the lot', () => {
  for (const model of [sealed, clocks]) {
    const scripts = scriptsOf(model);
    let clock = 0;
    for (const segment of planOf(model).segments) {
      const script = scripts(segment.lot);
      near(segment.to, script.duration, 'the window ends with the lot');
      near(segment.play, segment.to - segment.from);
      near(segment.length, RECAP.card + segment.play);
      assert.ok(segment.from >= 0 && segment.from < segment.to);
      assert.ok(segment.play >= Math.min(RECAP.minSegment, script.duration) - 1e-9, 'not shorter than the least');
      assert.ok(segment.play <= RECAP.maxSegment + 1e-9, 'not longer than the most');
      near(segment.start, clock, 'one segment follows the other without a gap');
      near(segment.end, segment.start + segment.length);
      clock = segment.end;
    }
  }
});

test('the segments play in the order of the night, and a lot is never played twice', () => {
  const lots = planOf(sealed).segments.map((s) => s.lot);
  assert.deepEqual(lots, [...lots].sort((a, b) => a - b));
  assert.equal(new Set(lots).size, lots.length);
  const ids = planOf(sealed).segments.flatMap((s) => s.ids);
  assert.equal(new Set(ids).size, ids.length, 'and a highlight is in one segment only');
});

test('the highlights of one lot share a segment, the best first, and the window opens at the earliest of them', () => {
  const lot2 = planOf(sealed).segments[1];
  assert.deepEqual(lot2.ids, [3, 4]);
  assert.equal(lot2.from, 0, 'the whole-lot highlight opens at the start of the lot');
  const lot1 = planOf(sealed).segments[0];
  assert.deepEqual(lot1.ids, [1, 2], 'by score: 6, then 3');
  near(lot1.from, 1.2, 'the first line of seat 1');
});

// -------------------------------------------------------------------------- the budget

test('a night of twelve lots comes to about three minutes', () => {
  const model = twelveLots([9, 8, 7, 7, 6, 6, 5, 5, 4, 3, 2, 1]);
  const plan = planOf(model);
  assert.ok(plan.length <= RECAP.budget + 1e-9, `${plan.length}`);
  assert.ok(plan.length > RECAP.budget - (RECAP.card + RECAP.minSegment), `${plan.length}: within one short segment of the budget`);
  assert.ok(plan.segments.length >= 5 && plan.segments.length <= 7, `${plan.segments.length} segments`);
  assert.deepEqual(plan.segments.map((s) => s.lot), [...plan.segments.map((s) => s.lot)].sort((a, b) => a - b));
});

test('the best scores get in first: a better lot is never left out for a worse one', () => {
  const model = twelveLots([9, 8, 7, 7, 6, 6, 5, 5, 4, 3, 2, 1]);
  const kept = new Set(planOf(model).segments.map((s) => s.lot));
  const scores = new Map(model.highlights.map((h) => [h.lot, h.score]));
  const worstKept = Math.min(...[...kept].map((lot) => scores.get(lot)));
  for (let lot = 1; lot <= 12; lot += 1) {
    if (kept.has(lot)) continue;
    assert.ok(scores.get(lot) <= worstKept, `lot ${lot} (${scores.get(lot)}) was left out for a worse one`);
  }
  assert.ok(kept.has(1) && kept.has(2), 'the two best are in');
  assert.ok(!kept.has(12), 'the worst is out');
});

test('equal scores go to the earlier lot', () => {
  const model = twelveLots([5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5]);
  const lots = planOf(model).segments.map((s) => s.lot);
  assert.deepEqual(lots, [1, 2, 3, 4, 5, 6].slice(0, lots.length), 'the first lots of the night');
  assert.ok(lots.length >= 5);
});

test('the budget is a limit that the plan keeps, and fills to within one short segment', () => {
  const model = twelveLots([9, 8, 7, 7, 6, 6, 5, 5, 4, 3, 2, 1]);
  const shortest = RECAP.card + RECAP.minSegment;
  for (const budget of [0, 10, 15.9, 16, 30, 60, 90, 120, 150, 180, 400]) {
    const plan = planOf(model, { budget });
    assert.ok(plan.length <= budget + 1e-9, `${budget}: ${plan.length}`);
    assert.equal(plan.segments.length === 0, budget < shortest, `${budget}`);
    if (budget >= shortest && budget <= 330) assert.ok(plan.length > budget - shortest, `${budget}: ${plan.length} leaves room for another`);
  }
  const wide = planOf(model, { budget: 10000 });
  assert.equal(wide.segments.length, 12, 'room for every lot');
  assert.ok(wide.segments.every((s) => s.play >= RECAP.minSegment - 1e-9));
});

test('a segment that does not fit is cut from the front to the room that is left, and keeps the lot up to its end', () => {
  // Lot 4 scores best, lot 3 next, lot 1 after them. Together the first two do not fit.
  const model = twelveLots([7, 0, 8, 9, 0, 0, 0, 0, 0, 0, 0, 0]);
  const need = Object.fromEntries(planOf(model, { budget: 10000 }).segments.map((s) => [s.lot, s]));
  const room = need[4].length + 20;
  assert.ok(room < need[4].length + need[3].length);
  const plan = planOf(model, { budget: room });
  assert.deepEqual(plan.segments.map((s) => s.lot), [3, 4], 'the second best is cut, not dropped for a lesser lot');
  const cut = plan.segments[0];
  near(cut.length, 20);
  near(cut.play, 18);
  near(cut.to, need[3].to, 'still ends with the lot');
  near(cut.from, need[3].to - 18);
  assert.ok(cut.from > need[3].from, 'it lost the front');
  assert.deepEqual(plan.segments[1], { ...need[4], index: 2, start: 20, end: 20 + need[4].length }, 'the better one is whole');
  near(plan.length, room, 'and the budget is used up');
});

test('a segment that cannot be cut to the least that is shown is skipped, and a shorter one after it gets in', () => {
  const model = twelveLots([7, 0, 8, 9, 0, 0, 0, 0, 0, 0, 0, 0]);
  const need = Object.fromEntries(planOf(model, { budget: 10000 }).segments.map((s) => [s.lot, s.length]));
  // 4 is whole, then 3 would have to be cut below the least. The room is just enough for lot 1 cut to the least.
  const room = need[4] + RECAP.card + RECAP.minSegment - 1;
  const plan = planOf(model, { budget: room });
  assert.deepEqual(plan.segments.map((s) => s.lot), [4], `${plan.segments.map((s) => s.lot)}`);
  const more = planOf(model, { budget: need[4] + RECAP.card + RECAP.minSegment });
  assert.deepEqual(more.segments.map((s) => s.lot), [3, 4], 'one second more room and the next one is cut to the least');
  near(more.segments[0].play, RECAP.minSegment);
});

// ------------------------------------------------------------------- what is left out

test('highlights with nothing to play are left out: no lot, a lot that was not played', () => {
  const raw = fixture(SEALED);
  raw.timeline = raw.timeline.slice(0, 2);
  raw.highlights = [
    { id: 1, category: 'backstab', score: 9, lot: null, seats: [1], first_seq: 1, last_seq: 2, text: 'night' },
    { id: 2, category: 'backstab', score: 8, lot: 3, seats: [1], first_seq: 57, last_seq: 76, text: 'not played' },
    { id: 3, category: 'backstab', score: 2, lot: 2, seats: [1], first_seq: 37, last_seq: 56, text: 'played' },
  ];
  const plan = planOf(indexReplay(raw));
  assert.deepEqual(plan.segments.map((s) => [s.lot, s.ids]), [[2, [3]]]);
});

test('a night without highlights has an empty plan, and the clock of an empty plan is done', () => {
  const raw = fixture(SEALED);
  raw.highlights = [];
  const plan = planOf(indexReplay(raw));
  assert.deepEqual(plan, { segments: [], length: 0 });
  const at = recapAt(plan, 5);
  assert.equal(at.segment, null);
  assert.equal(at.done, true);
  assert.equal(segmentStart(plan, 0, 1), null);
  assert.equal(segmentStart(plan, 0, -1), null);
});

test('a lot that has no script is not planned', () => {
  const plan = planRecap(sealed, (lot) => (lot === 4 ? null : scriptsOf(sealed)(lot)));
  assert.deepEqual(plan.segments.map((s) => s.lot), [1, 2, 3]);
});

// --------------------------------------------------------------------------- the clock

test('the card of a segment comes first, then the lot plays from its window to its end', () => {
  const plan = planOf(sealed);
  const second = plan.segments[1];
  const card = recapAt(plan, second.start);
  assert.equal(card.index, 2);
  assert.equal(card.phase, 'card');
  assert.equal(card.local, 0);
  assert.equal(card.lotT, second.from, 'the lot waits at the first moment of its window under the card');
  near(recapAt(plan, second.start + RECAP.card - 0.01).lotT, second.from);
  assert.equal(recapAt(plan, second.start + RECAP.card - 0.01).phase, 'card');
  const play = recapAt(plan, second.start + RECAP.card);
  assert.equal(play.phase, 'play');
  near(play.lotT, second.from);
  const later = recapAt(plan, second.start + RECAP.card + 5);
  near(later.lotT, second.from + 5);
  assert.equal(later.done, false);
  const last = recapAt(plan, second.end - 0.001);
  assert.equal(last.index, 2);
  assert.ok(Math.abs(last.lotT - second.to) < 0.01, 'the lot is at its end when the segment is');
});

test('a segment ends where the next one begins, and the end of the plan is the end of the last lot', () => {
  const plan = planOf(sealed);
  for (const [a, b] of [[0, 1], [1, 2], [2, 3]]) {
    near(plan.segments[a].end, plan.segments[b].start);
    assert.equal(recapAt(plan, plan.segments[b].start).index, b + 1, 'the boundary belongs to the next segment');
    assert.equal(recapAt(plan, plan.segments[b].start).phase, 'card');
  }
  const end = recapAt(plan, plan.length);
  assert.equal(end.done, true);
  assert.equal(end.index, 4);
  near(end.lotT, plan.segments[3].to);
  assert.equal(recapAt(plan, plan.length + 100).t, plan.length, 'time past the end is the end');
  assert.equal(recapAt(plan, -3).t, 0);
  assert.equal(recapAt(plan, Number.NaN).t, 0);
  assert.equal(recapAt(plan, 0).done, false);
});

test('sweeping the whole version visits the lots in order and the lot clock never runs backwards inside a segment', () => {
  for (const model of [sealed, clocks]) {
    const plan = planOf(model);
    let lastIndex = 0;
    let lastLotT = -1;
    for (let t = 0; t <= plan.length; t += 0.05) {
      const at = recapAt(plan, t);
      assert.ok(at.index >= lastIndex, 'segments never go back');
      if (at.index === lastIndex) assert.ok(at.lotT >= lastLotT - 1e-9, 'the lot clock never goes back');
      assert.ok(at.lotT >= at.segment.from - 1e-9 && at.lotT <= at.segment.to + 1e-9, 'and stays inside its window');
      lastIndex = at.index;
      lastLotT = at.lotT;
    }
    assert.equal(lastIndex, plan.segments.length);
  }
});

test('previous and next move between segments, and a previous a little into a segment goes to its own start', () => {
  const plan = planOf(sealed);
  const [a, b, c, d] = plan.segments;
  assert.equal(segmentStart(plan, 0, 1), b.start);
  assert.equal(segmentStart(plan, a.start + 5, 1), b.start);
  assert.equal(segmentStart(plan, d.start + 5, 1), null, 'no next after the last one');
  assert.equal(segmentStart(plan, plan.length, 1), null);
  assert.equal(segmentStart(plan, c.start + 5, -1), c.start, 'a few seconds in: back to the start of this one');
  assert.equal(segmentStart(plan, c.start + 0.5, -1), b.start, 'at the start: the one before');
  assert.equal(segmentStart(plan, a.start + 0.5, -1), null, 'none before the first');
  assert.equal(segmentStart(plan, plan.length, -1), d.start, 'from the end: back to the start of the last one');
});

// --------------------------------------------------------------------------- the caption

function mountBar(model, plan = planOf(model)) {
  const doc = new FakeDocument();
  const parent = doc.createElement('div');
  const bar = mountRecapBar(doc, parent, model, plan);
  return { bar, root: bar.root, plan };
}
const all = (el, selector) => el.querySelectorAll(selector);
const one = (el, selector) => el.querySelector(selector);
const text = (el) => el.textContent;

test('the caption says which segment it is, which lot, and what the highlights of the lot say', () => {
  const { bar, root, plan } = mountBar(sealed);
  bar.show(plan.segments[1], true);
  assert.equal(text(one(root, '.recap-kicker')), '三分钟看完一夜 · 第 2 / 4 段');
  assert.equal(text(one(root, '.recap-lot')), '第 2 件 永远指向昨天的指南针');
  const lights = all(root, '.recap-light');
  assert.deepEqual(lights.map((el) => el.getAttribute('data-category')), ['shill_stuck', 'claim_vs_hand']);
  assert.deepEqual(lights.map((el) => text(one(el, '.chip-cat'))), ['托儿砸手里', '说法与底牌差得多']);
  assert.deepEqual(lights.map((el) => text(all(el, '.chip')[1])), ['趣味分 9/10', '趣味分 6/10']);
  assert.deepEqual(lights.map((el) => text(one(el, '.recap-text'))), [sealed.highlight(3).text, sealed.highlight(4).text]);
});

test('without the lens a highlight that rests on something private keeps its label and loses its sentence', () => {
  const { bar, root, plan } = mountBar(sealed);
  bar.show(plan.segments[0], false);
  const texts = all(root, '.recap-text').map(text);
  assert.deepEqual(texts, [sealed.highlight(1).text, STR.recap.held], 'a backstab is public, a mind-reading guess is not');
  assert.ok(!text(root).includes(sealed.highlight(2).text));
  bar.show(plan.segments[1], false);
  assert.deepEqual(all(root, '.recap-text').map(text), [STR.recap.held, STR.recap.held]);
  assert.ok(!text(root).includes('托儿把指南针'));
});

test('the caption follows the phase of the version, and offers the next steps only at the end', () => {
  const { bar, root, plan } = mountBar(sealed);
  bar.show(plan.segments[3], true);
  bar.update(recapAt(plan, plan.segments[3].start));
  assert.equal(root.getAttribute('data-phase'), 'card');
  assert.ok(one(root, '.recap-end').hasAttribute('hidden'));
  bar.update(recapAt(plan, plan.segments[3].start + 5));
  assert.equal(root.getAttribute('data-phase'), 'play');
  assert.ok(one(root, '.recap-end').hasAttribute('hidden'));
  bar.update(recapAt(plan, plan.length));
  assert.ok(!one(root, '.recap-end').hasAttribute('hidden'));
  assert.ok(text(one(root, '.recap-end')).includes(STR.recap.done));
  assert.deepEqual(all(root, '.recap-go').map((a) => a.getAttribute('href')), [formatPageHash('fixture-sealed-0001', 'highlights'), formatPageHash('fixture-sealed-0001', 'awards')]);
  bar.update(recapAt(plan, 0));
  assert.ok(one(root, '.recap-end').hasAttribute('hidden'), 'and gone again when the time goes back');
});

test('showing a segment replaces the caption of the one before', () => {
  const { bar, root, plan } = mountBar(sealed);
  bar.show(plan.segments[0], true);
  bar.show(plan.segments[2], true);
  assert.equal(all(root, '.recap-kicker').length, 1);
  assert.equal(text(one(root, '.recap-kicker')), '三分钟看完一夜 · 第 3 / 4 段');
  assert.equal(all(root, '.recap-light').length, 2);
});
