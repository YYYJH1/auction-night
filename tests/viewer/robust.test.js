// A replay file can be wrong in many small ways. The viewer must still open every file that
// passes the shape check and play it to the end without throwing, and what it draws must stay
// inert text. These tests damage the fixtures at random (the same damage every run, from a
// fixed seed) and play the result.

import assert from 'node:assert/strict';
import test from 'node:test';

import { duration as dutchDuration, frameAt as dutchFrame, grabsOf, layout as dutchLayout } from '../../viewer/anim/dutch.js';
import { duration as englishDuration, frameAt as englishFrame, layout as englishLayout } from '../../viewer/anim/english.js';
import { animationFor } from '../../viewer/anim/index.js';
import { awardsFor } from '../../viewer/lib/awards.js';
import { highlightsFor, momentStart } from '../../viewer/lib/highlights.js';
import { overlayFor } from '../../viewer/lib/overlay.js';
import { planRecap, recapAt, segmentStart } from '../../viewer/lib/recap.js';
import { indexReplay, validateReplay } from '../../viewer/lib/replay.js';
import { reviewFor } from '../../viewer/lib/review.js';
import { buildScript } from '../../viewer/lib/script.js';
import { findingsFor, sidesOf } from '../../viewer/lib/verdicts.js';
import { mountAwards } from '../../viewer/ui/awards.js';
import { mountHighlights } from '../../viewer/ui/highlights.js';
import { mountRecapBar } from '../../viewer/ui/recap.js';
import { mountOpeningCard, mountReview } from '../../viewer/ui/review.js';
import { createStage } from '../../viewer/ui/stage.js';
import { FakeDocument, serialize, tagNames } from './fake_dom.js';
import { CLOCKS, fixture, MOCK, SEALED } from './helpers.js';

/** A small seeded generator: the same numbers on every run and every machine. */
function generator(seed) {
  let state = seed >>> 0;
  const next = () => {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
    return state / 2 ** 32;
  };
  return {
    next,
    int: (n) => Math.floor(next() * n),
    pick: (list) => list[Math.floor(next() * list.length)],
  };
}

const WRONG = [null, 'x', '12', -5, 0, 1.5, 1e300, 2 ** 53, true, false, [], {}, [1, 2], { a: 1 }, 'a'.repeat(5000), '<img src=x onerror=alert(1)>', 9, 1, 3, 400];

/** Every object or array in a tree, with the way to it. */
function nodes(value, path = [], out = []) {
  if (value !== null && typeof value === 'object') {
    out.push({ value, path });
    for (const [key, child] of Object.entries(value)) nodes(child, [...path, key], out);
  }
  return out;
}

function damage(raw, rng, parts = ['timeline', 'reveal', 'seats', 'catalog']) {
  for (let i = 0, n = 1 + rng.int(4); i < n; i += 1) {
    const targets = nodes(raw[rng.pick(parts)]).filter((n) => !(n.path.length === 0));
    const { value } = rng.pick(targets.length > 0 ? targets : nodes(raw));
    const keys = Object.keys(value);
    if (keys.length === 0) continue;
    const key = rng.pick(keys);
    const how = rng.int(5);
    if (how === 0) value[key] = rng.pick(WRONG);
    else if (how === 1) delete value[key];
    else if (how === 2 && Array.isArray(value)) value.splice(rng.int(value.length + 1), 0, structuredClone(value[rng.int(value.length)]));
    else if (how === 3 && Array.isArray(value)) value.splice(rng.int(value.length), 1);
    else value[key] = rng.pick(WRONG);
  }
  return raw;
}

function playThrough(model) {
  const doc = new FakeDocument();
  const root = doc.createElement('main');
  const stage = createStage(doc, root, model, {});
  for (const lotNo of model.lotNos) {
    for (const lens of [true, false]) {
      stage.render(lotNo, lens, { opening: true });
      const end = stage.durationOf(lotNo);
      assert.ok(Number.isFinite(end) && end > 0, `lot ${lotNo} has a length`);
      for (const t of [0, end * 0.25, end * 0.5, end * 0.75, end]) stage.update(t);
      for (const tag of tagNames(root)) assert.ok(/^[a-z0-9]+$/.test(tag), tag);
      const markup = serialize(root);
      for (const needle of ['<img', '<script', '<svg', '<iframe', '<a ', '<style']) assert.ok(!markup.includes(needle), needle);
    }
  }
}

// The real night of twelve lots is a longer play than the small ones, so it gets fewer rounds.
const NIGHTS = [
  { name: 'sealed', file: SEALED, seeds: [12345, 2468], rounds: 250 },
  { name: 'clocks', file: CLOCKS, seeds: [67890, 1357], rounds: 250 },
  { name: 'real', file: MOCK, seeds: [24680, 97531], rounds: 40 },
];

for (const { name, file, seeds, rounds } of NIGHTS) {
  test(`a damaged ${name} night that still passes the shape check plays to its end`, () => {
    const rng = generator(seeds[0]);
    let played = 0;
    for (let round = 0; round < rounds; round += 1) {
      const raw = damage(fixture(file), rng);
      if (!validateReplay(raw).ok) continue;
      played += 1;
      playThrough(indexReplay(raw));
    }
    assert.ok(played > rounds * 0.48, `${played} damaged nights were playable`);
  });
}

test('the clocks and the overlay take any lot that the index can make without throwing', () => {
  const rng = generator(424242);
  for (let round = 0; round < 300; round += 1) {
    const raw = damage(fixture(CLOCKS), rng);
    if (!validateReplay(raw).ok) continue;
    const model = indexReplay(raw);
    for (const lot of model.lots) {
      for (const lens of [true, false]) {
        overlayFor(lot, { lens });
        const english = englishLayout(lot, { lens });
        const dutch = dutchLayout(lot);
        grabsOf(lot);
        for (let i = 0; i <= 8; i += 1) {
          const frame = englishFrame(lot, (englishDuration(lot) * i) / 8, { lens });
          assert.ok(Number.isFinite(frame.price) && frame.fill >= 0, 'a finite price on the bar');
          assert.ok(frame.seats.every((s) => Number.isInteger(s.seat)));
          const clock = dutchFrame(lot, (dutchDuration(lot) * i) / 8);
          assert.ok(clock.cells.every((c) => Number.isFinite(c.price)));
        }
        assert.ok(english.top > 0 && Number.isFinite(english.total));
        assert.ok(Number.isFinite(dutch.total));
      }
    }
  }
});

// ------------------------------------------------------------ awards, highlights, the recap

function overEverything(model) {
  const scripts = new Map(
    model.lots.map((lot) => {
      const anim = animationFor(lot.format);
      return [lot.lot, buildScript(lot, anim.duration(lot), anim.hammerAt(lot))];
    }),
  );
  const scriptOf = (lotNo) => scripts.get(lotNo) ?? null;
  const doc = new FakeDocument();
  const root = doc.createElement('div');
  for (const lens of [true, false]) {
    const awards = awardsFor(model, { lens });
    assert.ok(awards.items.every((item) => ['won', 'tied', 'vacant', 'held'].includes(item.status)));
    for (const item of awards.items) assert.ok(item.status !== 'vacant' || item.winners.length === 0);
    const lights = highlightsFor(model, { lens });
    assert.ok(lights.items.every((item) => Number.isInteger(item.score) && item.score >= 0 && item.score <= 10));
    mountAwards(doc, root, model, { lens });
    mountHighlights(doc, root, model, { lens, revealed: true });
    mountHighlights(doc, root, model, { lens, revealed: false });
    const found = findingsFor(model, { lens });
    if (!lens) assert.ok(found.claims.held && found.collusion.held && found.claims.items.length + found.collusion.red.length === 0);
    for (const finding of [...found.collusion.red, ...found.collusion.suspect]) {
      assert.ok(sidesOf(finding).every((sentence) => typeof sentence === 'string' && sentence !== ''));
      assert.ok(finding.stats.every((s) => (s.value === null) === (s.text === null)));
    }
    const review = reviewFor(model, { lens });
    assert.ok(!lens || review.recap.paragraphs.length > 0 || !review.recap.present);
    for (const revealed of [true, false]) mountReview(doc, root, model, { lens, revealed });
    mountOpeningCard(doc, root, model);
  }
  for (const light of model.highlights) {
    const lot = model.lot(light.lot);
    if (lot === null) continue;
    const start = momentStart(lot, scripts.get(light.lot), light);
    assert.ok(Number.isFinite(start) && start >= 0 && start <= scripts.get(light.lot).duration, `moment ${light.id}`);
  }
  const plan = planRecap(model, scriptOf);
  assert.ok(Number.isFinite(plan.length) && plan.length <= 180 + 1e-9);
  const bar = mountRecapBar(doc, root, model, plan);
  for (const segment of plan.segments) {
    assert.ok(segment.from >= 0 && segment.from < segment.to && Number.isFinite(segment.length));
    for (const lens of [true, false]) bar.show(segment, lens);
  }
  for (let i = 0; i <= 20; i += 1) {
    const at = recapAt(plan, (plan.length * i) / 20);
    assert.ok(Number.isFinite(at.lotT));
    bar.update(at);
    segmentStart(plan, at.t, 1);
    segmentStart(plan, at.t, -1);
  }
  const markup = serialize(root);
  for (const tag of tagNames(root)) assert.ok(/^[a-z0-9]+$/.test(tag), tag);
  for (const needle of ['<img', '<script', '<svg', '<iframe', '<style']) assert.ok(!markup.includes(needle), needle);
}

for (const { name, file, seeds, rounds } of NIGHTS) {
  test(`a damaged ${name} night with damaged awards and highlights still gives awards, highlights and a three-minute plan`, () => {
    const rng = generator(seeds[1]);
    let played = 0;
    for (let round = 0; round < rounds; round += 1) {
      const raw = damage(fixture(file), rng, ['awards', 'bots_reference', 'highlights', 'timeline', 'catalog', 'seats']);
      if (!validateReplay(raw).ok) continue;
      played += 1;
      overEverything(indexReplay(raw));
    }
    assert.ok(played > rounds * 0.4, `${played} damaged nights were readable`);
  });
}

// ----------------------------------------------- findings, the host's extras, event numbers

const SHOW_PARTS = ['verdicts', 'pledges', 'judges_absent', 'host', 'awards', 'highlights', 'timeline'];

for (const { name, file, seeds, rounds } of NIGHTS) {
  test(`a damaged ${name} night with damaged findings, pledges, host extras and event numbers still opens every part`, () => {
    const rng = generator(seeds[0] + seeds[1]);
    let played = 0;
    for (let round = 0; round < rounds; round += 1) {
      const raw = damage(fixture(file), rng, SHOW_PARTS);
      if (!validateReplay(raw).ok) continue;
      played += 1;
      overEverything(indexReplay(raw));
    }
    assert.ok(played > rounds * 0.4, `${played} damaged nights were readable`);
  });
}

test('findings with every field wrong in turn do not stop the page', () => {
  const rng = generator(31337);
  for (const file of [MOCK, SEALED, CLOCKS]) {
    for (let round = 0; round < 120; round += 1) {
      const raw = fixture(file);
      const verdict = rng.pick(raw.verdicts);
      const key = rng.pick(Object.keys(verdict));
      verdict[key] = rng.pick(WRONG);
      const record = rng.pick(raw.pledges ?? [{}]);
      if (Object.keys(record).length > 0) record[rng.pick(Object.keys(record))] = rng.pick(WRONG);
      raw.judges_absent = rng.pick([[], [{ judge: rng.pick(WRONG), vendor: rng.pick(WRONG), reason: rng.pick(WRONG) }], rng.pick(WRONG)]);
      const lot = rng.pick(raw.timeline);
      lot.talk_seqs = rng.pick([lot.talk_seqs, rng.pick(WRONG), { 1: rng.pick(WRONG), 2: rng.pick(WRONG) }]);
      lot.hammer_seq = rng.pick(WRONG);
      if (!validateReplay(raw).ok) continue;
      overEverything(indexReplay(raw));
    }
  }
});
