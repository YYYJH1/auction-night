import assert from 'node:assert/strict';
import test from 'node:test';

import { stars } from '../../viewer/lib/format.js';
import { lensFor, missionText } from '../../viewer/lib/lens.js';
import { STR } from '../../viewer/lib/strings.js';
import { CLOCKS, modelOf, SEALED } from './helpers.js';

const sealed = modelOf(SEALED);
const clocks = modelOf(CLOCKS);

test('a player at the first lot: value, budget, mission, plan, opening and lot monologues, guess', () => {
  const card = lensFor(sealed, 1, 1);
  assert.equal(card.valueKind, 'value');
  assert.equal(card.value, 37);
  assert.equal(card.budget, 342);
  assert.equal(card.remaining, 342);
  assert.deepEqual(card.mission, {
    kind: 'collector',
    text: '任务（收藏家）：拍到 1 件「海洋」标签的拍品',
  });
  assert.deepEqual(card.plan, { level: 0, stars: '☆☆☆', budget: 0, persona: '低调的海洋爱好者，对别的都不感兴趣' });
  assert.deepEqual(card.thoughts.map((t) => t.phase), ['plan', 'talk', 'sealed']);
  assert.deepEqual(card.thoughts.map((t) => t.text), [
    '海洋货在第四件，前面的预算要省着，别暴露目标。',
    '先把话放出去，让他们以为我对蜂蜜没兴趣。',
    '30 不贵，试试水，大不了不中。',
  ]);
  assert.deepEqual(card.guess, { lot: 1, seat: 1, target: 2, guess: 180, truth: 260, abs_error: 80 });
});

test('the opening monologue belongs to the first lot only', () => {
  assert.deepEqual(lensFor(sealed, 1, 2).thoughts.map((t) => t.phase), ['talk', 'sealed']);
});

test('the plan of a lot is read at the lot position', () => {
  const card = lensFor(sealed, 1, 4);
  assert.equal(card.plan.level, 3);
  assert.equal(card.plan.stars, '★★★');
  assert.equal(card.plan.budget, 220);
  assert.equal(stars(3), '★★★');
  assert.equal(stars(0), '☆☆☆');
  assert.equal(stars(4), '');
});

test('a blind box shows the appraisal, not a value', () => {
  const card = lensFor(sealed, 3, 4);
  assert.equal(card.valueKind, 'appraisal');
  assert.equal(card.value, 204);
});

test('the budget left follows the payments of the earlier lots', () => {
  assert.equal(lensFor(sealed, 4, 2).remaining, 214, 'seat 4 paid 74 for lot 1');
  assert.equal(lensFor(sealed, 2, 4).remaining, 82);
  assert.equal(lensFor(sealed, 2, 4).budget, 260);
});

test('a bot has cards but no mission, plan or monologue', () => {
  const card = lensFor(sealed, 5, 1);
  assert.equal(card.value, 62);
  assert.equal(card.budget, 220);
  assert.equal(card.mission, null);
  assert.equal(card.plan, null);
  assert.deepEqual(card.thoughts, []);
  assert.equal(card.guess, null);
});

test('missing pieces are null and nothing is invented', () => {
  const card = lensFor(sealed, 7, 1);
  assert.deepEqual(card, {
    seat: 7,
    budget: null,
    remaining: null,
    valueKind: null,
    value: null,
    mission: null,
    plan: null,
    thoughts: [],
    guess: null,
  });
});

test('the shill mission names the lot and the threshold', () => {
  assert.equal(
    missionText(sealed.mission(2)),
    '任务（托儿）：让第 2 件的成交价不低于 190，而且买下它的不是自己',
  );
  assert.equal(missionText(null), '');
  assert.equal(missionText({ kind: 'mystery' }), STR.lens.missionUnknown);
});

test('the English and Dutch night', () => {
  const english = lensFor(clocks, 3, 1);
  assert.deepEqual(english.thoughts.map((t) => t.phase), ['plan', 'talk', 'english_init', 'english_revise', 'english_revise']);
  assert.equal(english.remaining, 190);
  const dutch = lensFor(clocks, 3, 2);
  assert.equal(dutch.remaining, 25, '190 minus the 165 paid for the radio');
  assert.deepEqual(dutch.thoughts.map((t) => t.phase), ['talk']);
});

test('the lens only reads the replay: it never touches the public lot record', () => {
  const before = JSON.stringify(sealed.lot(1));
  lensFor(sealed, 1, 1);
  assert.equal(JSON.stringify(sealed.lot(1)), before);
});
