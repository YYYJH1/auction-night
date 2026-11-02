import assert from 'node:assert/strict';
import test from 'node:test';

import { animationFor } from '../../viewer/anim/index.js';
import {
  categoryBasis,
  categoryLabel,
  CATEGORY_KEYS,
  highlightsFor,
  isCategory,
  momentStart,
  rankHighlights,
} from '../../viewer/lib/highlights.js';
import { indexReplay } from '../../viewer/lib/replay.js';
import { BEATS, buildScript, talkSeconds } from '../../viewer/lib/script.js';
import { formatMomentHash } from '../../viewer/lib/route.js';
import { STR } from '../../viewer/lib/strings.js';
import { mountHighlights } from '../../viewer/ui/highlights.js';
import { attributeValues, FakeDocument, serialize, tagNames } from './fake_dom.js';
import { CLOCKS, fixture, hasHidden, MOCK, modelOf, PAYLOADS, poison, SEALED, strings } from './helpers.js';

const sealed = modelOf(SEALED);
const clocks = modelOf(CLOCKS);
const mock = modelOf(MOCK);
// A script as the older tests build it: the length of the bid animation, no hammer.
const scriptOf = (model, lotNo) => {
  const lot = model.lot(lotNo);
  return buildScript(lot, animationFor(lot.format).duration(lot));
};
// A script as the stage builds it, which knows the second at which the hammer falls.
const stageScriptOf = (model, lotNo) => {
  const lot = model.lot(lotNo);
  const anim = animationFor(lot.format);
  return buildScript(lot, anim.duration(lot), anim.hammerAt(lot));
};
const talkBeats = (script) => script.beats.filter((beat) => beat.kind === 'talk');
const firstBid = (lot) => Math.min(...lot.actions.filter((a) => ['english_init', 'english_revise', 'dutch', 'sealed'].includes(a.phase)).map((a) => a.seq));

/** The sealed night with the highlights replaced. */
function withHighlights(list) {
  const raw = fixture(SEALED);
  raw.highlights = list;
  return indexReplay(raw);
}
const light = (id, extra = {}) => ({ id, category: 'backstab', score: 5, lot: 1, seats: [1], first_seq: 18, last_seq: 36, text: `moment ${id}`, ...extra });

// ------------------------------------------------------------------------- the categories

test('the eight categories, each with a label and what it rests on', () => {
  assert.deepEqual([...CATEGORY_KEYS], [
    'winners_curse', 'shill_stuck', 'backstab', 'claim_vs_hand', 'plan_contradiction', 'all_pay_tilt', 'bargain', 'mindread_miss',
  ]);
  assert.deepEqual(Object.keys(STR.highlights.categories), [...CATEGORY_KEYS]);
  assert.deepEqual(CATEGORY_KEYS.filter((key) => categoryBasis(key) === 'public'), ['winners_curse', 'backstab', 'all_pay_tilt']);
  assert.equal(categoryLabel('winners_curse'), '赢家诅咒');
  assert.equal(categoryLabel('claim_vs_hand'), '说法与底牌差得多');
  assert.equal(categoryLabel('something_new'), STR.highlights.unknownCategory);
  assert.equal(categoryBasis('something_new'), 'private');
  assert.ok(isCategory('bargain') && !isCategory('Bargain') && !isCategory(undefined) && !isCategory('constructor'));
  assert.equal(new Set(CATEGORY_KEYS.map(categoryLabel)).size, 8, 'the labels differ');
});

// ------------------------------------------------------------------------------ the order

test('the list goes by score, then by lot, then by id', () => {
  const ranked = rankHighlights(sealed.highlights);
  assert.deepEqual(ranked.map((x) => x.id), [3, 7, 1, 4, 5, 6, 2]);
  assert.deepEqual(ranked.map((x) => x.score), [9, 8, 6, 6, 5, 4, 3]);
  assert.deepEqual(rankHighlights(clocks.highlights).map((x) => x.id), [1, 4, 2, 5, 3]);
});

test('equal scores go to the earlier lot, a highlight of the whole night goes last, and the order does not depend on the file', () => {
  const list = [
    light(1, { score: 5, lot: 3 }),
    light(2, { score: 5, lot: null }),
    light(3, { score: 5, lot: 1 }),
    light(4, { score: 5, lot: 1 }),
    light(5, { score: 9, lot: 4 }),
  ];
  const expected = [5, 3, 4, 1, 2];
  assert.deepEqual(rankHighlights(withHighlights(list).highlights).map((x) => x.id), expected);
  assert.deepEqual(rankHighlights(withHighlights([...list].reverse()).highlights).map((x) => x.id), expected);
  assert.deepEqual(highlightsFor(withHighlights(list), { lens: true }).items.map((x) => x.id), expected);
  const sample = withHighlights(list).highlights;
  const before = JSON.stringify(sample);
  rankHighlights(sample);
  assert.equal(JSON.stringify(sample), before, 'a copy is sorted');
});

// ---------------------------------------------------------------------------- the lens

test('with the lens every highlight shows its text, its seats and its lot', () => {
  const shown = highlightsFor(sealed, { lens: true });
  assert.equal(shown.lens, true);
  assert.deepEqual(shown.items.map((item) => item.id), [3, 7, 1, 4, 5, 6, 2]);
  assert.equal(shown.held, 0);
  const first = shown.items[0];
  assert.deepEqual(
    { id: first.id, category: first.category, label: first.label, score: first.score, lot: first.lot, lotName: first.lotName, jumpable: first.jumpable, held: first.held, seats: first.seats },
    { id: 3, category: 'shill_stuck', label: '托儿砸手里', score: 9, lot: 2, lotName: '永远指向昨天的指南针', jumpable: true, held: false, seats: [2] },
  );
  assert.equal(first.text, '托儿把指南针砸在了自己手里，付 158，门槛 190 没碰到。');
  assert.deepEqual(shown.categories, ['winners_curse', 'shill_stuck', 'backstab', 'claim_vs_hand', 'all_pay_tilt', 'bargain', 'mindread_miss']);
});

test('without the lens a highlight that rests on something private keeps its category, score and lot, and loses its text and seats', () => {
  const shown = highlightsFor(sealed, { lens: false });
  assert.equal(shown.lens, false);
  const held = shown.items.filter((item) => item.held).map((item) => [item.id, item.category]);
  assert.deepEqual(held, [[3, 'shill_stuck'], [4, 'claim_vs_hand'], [6, 'bargain'], [2, 'mindread_miss']]);
  for (const item of shown.items.filter((entry) => entry.held)) {
    assert.equal(item.text, null, `${item.id}`);
    assert.deepEqual(item.seats, [], `${item.id}`);
    assert.ok(item.score >= 0 && item.lot !== null && item.label, 'the rest stays');
  }
  const open = shown.items.filter((item) => !item.held);
  assert.deepEqual(open.map((item) => item.category), ['winners_curse', 'backstab', 'all_pay_tilt']);
  assert.ok(open.every((item) => item.text !== null && item.seats.length === 1));
  assert.equal(shown.held, 4);
});

test('without the lens no private sentence of any highlight is anywhere in the result', () => {
  for (const model of [sealed, clocks]) {
    const text = JSON.stringify(highlightsFor(model, { lens: false }));
    for (const item of model.highlights) {
      if (categoryBasis(item.category) === 'private') assert.ok(!text.includes(item.text), item.text);
    }
  }
  for (const lens of [undefined, null, 0, 'yes', 1]) assert.equal(highlightsFor(sealed, { lens }).lens, false, String(lens));
  assert.equal(highlightsFor(sealed).held, 4, 'no option at all means no lens');
});

test('highlightsFor changes nothing it is given', () => {
  const before = JSON.stringify(sealed.highlights);
  highlightsFor(sealed, { lens: true });
  highlightsFor(sealed, { lens: false });
  assert.equal(JSON.stringify(sealed.highlights), before);
  assert.deepEqual(highlightsFor(sealed, { lens: true }), highlightsFor(sealed, { lens: true }));
});

test('a highlight of the whole night, of a lot that was not played, or of a category that is new, is still listed', () => {
  const raw = fixture(SEALED);
  raw.timeline = raw.timeline.slice(0, 2);
  raw.highlights = [
    light(1, { lot: null }),
    light(2, { lot: 3 }),
    light(3, { category: 'a_new_category', lot: 2 }),
    light(4, { lot: 2, category: 'winners_curse' }),
  ];
  const shown = highlightsFor(indexReplay(raw), { lens: false });
  const byId = Object.fromEntries(shown.items.map((item) => [item.id, item]));
  assert.equal(byId[1].jumpable, false);
  assert.equal(byId[1].lotName, null);
  assert.equal(byId[2].jumpable, false, 'lot 3 was not played');
  assert.equal(byId[2].lotName, '越烧越长的蜡烛', 'but it is in the catalog');
  assert.equal(byId[3].label, STR.highlights.unknownCategory);
  assert.equal(byId[3].held, true, 'an unknown category is private');
  assert.equal(byId[4].jumpable, true);
  assert.deepEqual(shown.categories, ['winners_curse', 'backstab', 'unknown']);
});

// ------------------------------------------------------------------------------- the basis

test('the basis that the replay gives wins over the table, and the table is for a replay that has none', () => {
  const model = withHighlights([
    light(1, { category: 'bargain', basis: 'public' }),
    light(2, { category: 'winners_curse', basis: 'private' }),
    light(3, { category: 'backstab' }),
    light(4, { category: 'claim_vs_hand' }),
    light(5, { category: 'a_new_category', basis: 'public' }),
    light(6, { category: 'backstab', basis: 'secret' }),
    light(7, { category: 'bargain', basis: 7 }),
  ]);
  const off = highlightsFor(model, { lens: false });
  const byId = Object.fromEntries(off.items.map((item) => [item.id, item]));
  assert.deepEqual(
    [1, 2, 3, 4, 5, 6, 7].map((id) => [id, byId[id].held, byId[id].basis]),
    [
      [1, false, 'public'], // the table says private, the file says public
      [2, true, 'private'], // the table says public, the file says private
      [3, false, 'public'], // no basis in the file: the table
      [4, true, 'private'],
      [5, false, 'public'], // a category that is new, and the file knows what it rests on
      [6, true, 'private'], // a word that is not a basis: private
      [7, true, 'private'], // not a text: the table (private)
    ],
  );
  assert.equal(off.held, 4);
  assert.equal(byId[1].text, 'moment 1');
  assert.equal(byId[2].text, null);
  assert.deepEqual(byId[2].seats, []);
  assert.equal(highlightsFor(model, { lens: true }).held, 0);
});

test('categoryBasis takes the basis of the replay first and the table only when there is none', () => {
  assert.equal(categoryBasis('backstab'), 'public');
  assert.equal(categoryBasis('backstab', null), 'public');
  assert.equal(categoryBasis('backstab', 'private'), 'private');
  assert.equal(categoryBasis('bargain', 'public'), 'public');
  assert.equal(categoryBasis('bargain', 'weird'), 'private');
  assert.equal(categoryBasis('something_new', 'public'), 'public');
  assert.equal(categoryBasis('something_new'), 'private');
});

test('the real night: the basis in the file decides what is held back', () => {
  const shown = highlightsFor(mock, { lens: false });
  assert.ok(mock.highlights.every((item) => item.basis === 'public' || item.basis === 'private'));
  assert.deepEqual(
    shown.items.filter((item) => item.held).map((item) => item.id).sort((a, b) => a - b),
    mock.highlights.filter((item) => item.basis === 'private').map((item) => item.id),
  );
  const text = JSON.stringify(shown);
  for (const item of mock.highlights) if (item.basis === 'private') assert.ok(!text.includes(item.text), item.text);
  assert.equal(shown.held, mock.highlights.filter((item) => item.basis === 'private').length);
  assert.equal(highlightsFor(mock, { lens: true }).held, 0);
});

// ----------------------------------------------------------------------- the moment

test('a highlight that opens with the lot begins at the start of the lot', () => {
  for (const [id, lotNo] of [[3, 2], [5, 3]]) {
    const lot = sealed.lot(lotNo);
    assert.equal(momentStart(lot, scriptOf(sealed, lotNo), sealed.highlight(id)), 0, `highlight ${id}`);
  }
  const lot = sealed.lot(2);
  const script = scriptOf(sealed, 2);
  assert.equal(momentStart(lot, script, { first_seq: lot.first_seq - 5, seats: [] }), 0);
  assert.equal(momentStart(lot, script, { first_seq: lot.first_seq, seats: [2] }), 0);
});

test('a highlight that opens in the talk begins at the first line of the first seat it names', () => {
  const lot1 = sealed.lot(1);
  const script1 = scriptOf(sealed, 1);
  const talks = script1.beats.filter((beat) => beat.kind === 'talk');
  assert.equal(momentStart(lot1, script1, sealed.highlight(1)), talks[0].start, 'seat 1 speaks first in lot 1');
  assert.equal(talks[0].start, BEATS.card);
  assert.equal(momentStart(lot1, script1, sealed.highlight(2)), talks[1].start, 'seat 2 speaks second');
  assert.equal(talks[1].start, BEATS.card + talkSeconds(lot1.talk[0].text));
  assert.equal(momentStart(sealed.lot(2), scriptOf(sealed, 2), sealed.highlight(4)), BEATS.card, 'seat 2 speaks first in lot 2');
  const earliest = momentStart(lot1, script1, { first_seq: 20, seats: [4, 2, 3] });
  assert.equal(earliest, talks[1].start, 'of several seats, the one that speaks first');
  assert.equal(momentStart(lot1, script1, { first_seq: 20, seats: [] }), talks[0].start, 'no seat named: the first line');
  assert.equal(momentStart(lot1, script1, { first_seq: 20, seats: [8] }), talks[0].start, 'a seat that did not speak: the first line');
});

test('a highlight that opens in the bidding begins with the bid animation', () => {
  for (const [id, lotNo] of [[6, 3], [7, 4]]) {
    const lot = sealed.lot(lotNo);
    const script = scriptOf(sealed, lotNo);
    assert.equal(momentStart(lot, script, sealed.highlight(id)), script.bidStart, `highlight ${id}`);
    assert.ok(script.bidStart > 0);
  }
  const lot = sealed.lot(3);
  const script = scriptOf(sealed, 3);
  const firstBid = Math.min(...lot.actions.map((a) => a.seq));
  assert.equal(momentStart(lot, script, { first_seq: firstBid, seats: [3] }), script.bidStart, 'the first bid itself is bidding');
  assert.notEqual(momentStart(lot, script, { first_seq: firstBid - 1, seats: [3] }), script.bidStart, 'one event earlier is still talk');
  assert.equal(momentStart(lot, script, { first_seq: lot.last_seq, seats: [] }), script.bidStart);
});

test('a lot with no talk, or with no bids, still has a start for every highlight', () => {
  const lot = { ...sealed.lot(1), talk: [], actions: [] };
  const script = buildScript(lot, 10);
  assert.equal(momentStart(lot, script, { first_seq: 20, seats: [1] }), script.bidStart, 'nobody spoke');
  const quiet = { ...sealed.lot(1), actions: [] };
  const talkScript = buildScript(quiet, 10);
  assert.equal(momentStart(quiet, talkScript, { first_seq: 20, seats: [1] }), BEATS.card, 'no bids: everything after the start is talk');
});

test('every highlight of both nights begins inside its lot', () => {
  for (const model of [sealed, clocks]) {
    for (const item of model.highlights) {
      const start = momentStart(model.lot(item.lot), scriptOf(model, item.lot), item);
      assert.ok(start >= 0 && start < scriptOf(model, item.lot).duration, `${item.id}: ${start}`);
    }
  }
});

test('with the numbers of the lines, a highlight in the talk begins at the line that its first event belongs to', () => {
  const lot = mock.lot(2);
  const script = stageScriptOf(mock, 2);
  const startOf = (seat) => talkBeats(script).find((beat) => beat.seat === seat).start;
  assert.deepEqual(lot.talk_order, [2, 3, 4, 5, 6, 7, 8, 1], 'seat 1 speaks last in this lot');
  assert.equal(momentStart(lot, script, mock.highlight(3)), startOf(5), 'the broken pledge, at the line that made it');
  assert.equal(momentStart(lot, script, mock.highlight(2)), startOf(4), 'the plan, at the line of the seat');
  assert.equal(momentStart(lot, script, { first_seq: lot.talk_seqs[5] + 1, seats: [] }), startOf(5), 'an event after a line belongs to it');
  assert.equal(momentStart(lot, script, { first_seq: lot.talk_seqs[5] + 2, seats: [] }), startOf(5));
  assert.equal(momentStart(lot, script, { first_seq: lot.talk_seqs[5], seats: [8, 1] }), startOf(5), 'the seats that are named do not matter when the numbers speak');
  assert.equal(momentStart(lot, script, { first_seq: lot.talk_seqs[1], seats: [2] }), startOf(1), 'the last line of the lot');
  assert.ok(startOf(1) > startOf(2));
  // The older rule would have said the seat that is named, which here is another line.
  const old = { ...lot, talk_seqs: {}, hammer_seq: null };
  assert.equal(momentStart(old, script, { first_seq: lot.talk_seqs[5], seats: [1] }), startOf(1));
  assert.equal(momentStart(lot, script, { first_seq: lot.talk_seqs[5], seats: [1] }), startOf(5));
});

test('a table that holds only some lines still finds the nearest line before the event, and falls back before the first', () => {
  const lot = { ...mock.lot(2), talk_seqs: { 5: 64, 3: 58, 9: 70 } };
  const script = stageScriptOf(mock, 2);
  const startOf = (seat) => talkBeats(script).find((beat) => beat.seat === seat).start;
  assert.equal(momentStart(lot, script, { first_seq: 60, seats: [] }), startOf(3));
  assert.equal(momentStart(lot, script, { first_seq: 70, seats: [] }), startOf(5), 'a seat that did not speak has no line');
  assert.equal(momentStart(lot, script, { first_seq: 56, seats: [5] }), startOf(5), 'before every numbered line: the seat that is named');
  assert.equal(momentStart(lot, script, { first_seq: 56, seats: [] }), talkBeats(script)[0].start, 'or the first line');
});

test('a highlight that opens at the hammer or after it begins when the hammer falls', () => {
  const lot = mock.lot(3);
  const script = stageScriptOf(mock, 3);
  assert.equal(lot.hammer_seq, lot.last_seq);
  assert.ok(script.hammerAt > script.bidStart && script.hammerAt < script.hostStart, 'inside the bid animation');
  assert.equal(momentStart(lot, script, { first_seq: lot.hammer_seq, seats: [6] }), script.hammerAt);
  assert.equal(momentStart(lot, script, { first_seq: lot.hammer_seq + 5, seats: [] }), script.hammerAt);
  assert.equal(momentStart(lot, script, { first_seq: lot.hammer_seq - 1, seats: [6] }), script.bidStart, 'one event earlier is still the bidding');
  assert.equal(momentStart(lot, script, mock.highlight(7)), script.bidStart, 'the curse of this lot begins with the bids');
  assert.equal(momentStart(lot, script, { first_seq: lot.first_seq, seats: [] }), 0, 'and a highlight of the whole lot at its start');
});

test('a script that does not know the hammer, or a lot without its number, keeps the approximation', () => {
  const lot = mock.lot(3);
  const plain = scriptOf(mock, 3);
  assert.equal(plain.hammerAt, null);
  assert.equal(momentStart(lot, plain, { first_seq: lot.hammer_seq, seats: [6] }), plain.bidStart);
  const old = { ...lot, hammer_seq: null, talk_seqs: {} };
  const script = stageScriptOf(mock, 3);
  assert.equal(momentStart(old, script, { first_seq: lot.hammer_seq, seats: [] }), script.bidStart);
  const talks = talkBeats(script);
  assert.equal(momentStart(old, script, { first_seq: lot.talk_seqs[5] + 1, seats: [5, 3] }), talks[0].start, 'of two named seats, the one that speaks first');
  assert.equal(momentStart(old, script, { first_seq: lot.talk_seqs[5] + 1, seats: [] }), talks[0].start);
  assert.equal(talks[0].seat, 3);
  // hand-written nights have no numbers at all
  for (const [model, lotNo] of [[sealed, 2], [clocks, 1]]) {
    const hand = model.lot(lotNo);
    assert.deepEqual([hand.talk_seqs, hand.hammer_seq], [{}, null]);
    assert.equal(momentStart(hand, stageScriptOf(model, lotNo), { first_seq: hand.last_seq, seats: [] }), stageScriptOf(model, lotNo).bidStart);
  }
});

test('every highlight of the real night begins where its first event is: a line, the bids, or the hammer', () => {
  const seen = { line: 0, bids: 0, hammer: 0, lot: 0 };
  for (const item of mock.highlights) {
    const lot = mock.lot(item.lot);
    const script = stageScriptOf(mock, item.lot);
    const start = momentStart(lot, script, item);
    assert.ok(start >= 0 && start < script.duration, `${item.id}`);
    if (!(item.first_seq > lot.first_seq)) {
      assert.equal(start, 0);
      seen.lot += 1;
    } else if (item.first_seq >= lot.hammer_seq) {
      assert.equal(start, script.hammerAt);
      seen.hammer += 1;
    } else if (item.first_seq < firstBid(lot)) {
      const beat = talkBeats(script).find((b) => b.start === start);
      assert.ok(beat, `${item.id}: a line`);
      const number = lot.talk_seqs[beat.seat];
      assert.ok(number <= item.first_seq, `${item.id}: the line is not after the event`);
      for (const other of talkBeats(script)) {
        const later = lot.talk_seqs[other.seat];
        if (later > number) assert.ok(later > item.first_seq, `${item.id}: no line between the line and the event`);
      }
      seen.line += 1;
    } else {
      assert.equal(start, script.bidStart);
      seen.bids += 1;
    }
  }
  assert.ok(seen.line >= 8 && seen.bids >= 3, JSON.stringify(seen));
});

test('the hammer is where each animation lets it fall: a verdict from that second on, not a moment before', () => {
  for (const lot of mock.lots) {
    const anim = animationFor(lot.format);
    const at = anim.hammerAt(lot);
    assert.ok(at > 0 && at < anim.duration(lot), `lot ${lot.lot}`);
    assert.equal(anim.frameAt(lot, at - 0.01, { lens: false }).verdict, null, `lot ${lot.lot}: before`);
    assert.notEqual(anim.frameAt(lot, at, { lens: false }).verdict, null, `lot ${lot.lot}: at the hammer`);
    const script = stageScriptOf(mock, lot.lot);
    assert.equal(script.hammerAt, script.bidStart + at);
  }
  const unknown = { ...mock.lot(1), format: 'a_future_format' };
  const summary = animationFor(unknown.format);
  assert.equal(summary.id, 'summary');
  assert.ok(summary.hammerAt(unknown) < summary.duration(unknown));
  assert.equal(summary.frameAt(unknown, summary.hammerAt(unknown) - 0.01).verdict, null);
  assert.notEqual(summary.frameAt(unknown, summary.hammerAt(unknown)).verdict, null);
});

// -------------------------------------------------------------------------- the list

function mount(model, options = {}) {
  const doc = new FakeDocument();
  const parent = doc.createElement('div');
  const view = mountHighlights(doc, parent, model, { lens: true, revealed: true, ...options });
  return { doc, parent, view, root: view.root };
}
const all = (el, selector) => el.querySelectorAll(selector);
const one = (el, selector) => el.querySelector(selector);
const text = (el) => el.textContent;
const visible = (root) => all(root, '.light').filter((el) => !el.classList.contains('is-filtered'));

test('until it is unfolded the page holds a gate and no highlight', () => {
  let unfolded = 0;
  const { root } = mount(sealed, { revealed: false, onReveal: () => (unfolded += 1) });
  assert.equal(text(one(root, '.page-title')), '高光时刻');
  assert.equal(all(root, '.light').length, 0);
  assert.equal(all(root, '.filter').length, 0);
  assert.equal(text(one(root, '.gate .section-title')), STR.gate.title);
  assert.equal(text(one(root, '.gate p')), STR.gate.body);
  const button = one(root, 'button.gate-open');
  assert.equal(text(button), STR.gate.open);
  assert.equal(unfolded, 0);
  button.dispatch('click');
  assert.equal(unfolded, 1);
  const spoilers = text(root);
  for (const item of sealed.highlights) assert.ok(!spoilers.includes(item.text), 'nothing of the night is on the gate');
});

test('unfolded, the list shows score, category, lot, sentence and seats of each highlight, best first', () => {
  const { root } = mount(sealed);
  const rows = all(root, '.light');
  assert.deepEqual(rows.map((row) => row.getAttribute('data-id')), ['3', '7', '1', '4', '5', '6', '2']);
  const first = rows[0];
  assert.equal(first.getAttribute('data-category'), 'shill_stuck');
  assert.equal(text(one(first, '.light-score strong')), '9');
  assert.equal(text(one(first, '.light-score small')), '/10');
  assert.equal(text(one(first, '.light-score')), '趣味分 9/10', 'a reader of the screen hears the words as well');
  assert.ok(one(first, '.light-score .visually-hidden'));
  assert.equal(text(one(first, '.chip-cat')), '托儿砸手里');
  assert.equal(text(one(first, '.light-lot')), '第 2 件 永远指向昨天的指南针');
  assert.equal(text(one(first, '.light-text')), '托儿把指南针砸在了自己手里，付 158，门槛 190 没碰到。');
  assert.deepEqual(all(first, '.seat-chip').map(text), ['2阿铁']);
  assert.equal(text(one(first, '.light-go')), '跳到这一刻 ›');
  assert.ok(text(one(root, '.page-head')).includes('不评价谁的表现'));
  assert.ok(text(root).includes(STR.highlights.order));
});

test('a click on a highlight is a link to its moment', () => {
  const { root } = mount(sealed);
  for (const row of all(root, '.light')) {
    const link = one(row, 'a.light-link');
    assert.equal(link.getAttribute('href'), formatMomentHash('fixture-sealed-0001', Number(row.getAttribute('data-id'))));
    assert.equal(link.getAttribute('data-moment'), row.getAttribute('data-id'));
  }
  assert.equal(all(root, 'a').length, 7);
});

test('a highlight without a lot to jump to is a plain row, not a link', () => {
  const raw = fixture(SEALED);
  raw.timeline = raw.timeline.slice(0, 1);
  raw.highlights = [light(1, { lot: null }), light(2, { lot: 3 }), light(3, { lot: 1 })];
  const { root } = mount(indexReplay(raw));
  assert.equal(all(root, 'a').length, 1);
  const statics = all(root, '.light-link.is-static');
  assert.equal(statics.length, 2);
  assert.equal(statics[0].getAttribute('title'), STR.highlights.noJump);
  assert.equal(all(root, '.light-go').length, 1, 'only a link says that it jumps');
  assert.ok(text(root).includes(STR.highlights.wholeNight));
});

test('the list can be narrowed to one category, and back', () => {
  const { root } = mount(sealed);
  const buttons = all(root, '.filter button');
  assert.deepEqual(buttons.map(text), ['全部', '赢家诅咒', '托儿砸手里', '背刺', '说法与底牌差得多', '全员付费上头', '捡漏', '读心大失败']);
  assert.equal(buttons[0].getAttribute('aria-pressed'), 'true');
  assert.equal(text(one(root, '.light-count')), '共 7 条');
  const backstab = buttons.find((b) => b.getAttribute('data-category') === 'backstab');
  backstab.dispatch('click');
  assert.deepEqual(visible(root).map((el) => el.getAttribute('data-category')), ['backstab']);
  assert.equal(backstab.getAttribute('aria-pressed'), 'true');
  assert.equal(buttons[0].getAttribute('aria-pressed'), 'false');
  assert.equal(text(one(root, '.light-count')), '显示 1 条，共 7 条');
  buttons[0].dispatch('click');
  assert.equal(visible(root).length, 7);
  assert.equal(text(one(root, '.light-count')), '共 7 条');
});

test('a night with one category has no filter, and the choice survives the lens being switched', () => {
  const single = withHighlights([light(1), light(2, { lot: 2, first_seq: 38 })]);
  assert.equal(all(mount(single).root, '.filter').length, 0);
  const ui = { filter: null };
  const first = mount(sealed, { ui });
  all(first.root, '.filter button').find((b) => b.getAttribute('data-category') === 'bargain').dispatch('click');
  assert.equal(ui.filter, 'bargain');
  const again = mount(sealed, { ui, lens: false });
  assert.deepEqual(visible(again.root).map((el) => el.getAttribute('data-category')), ['bargain']);
  assert.equal(ui.filter, 'bargain');
  const other = mount(withHighlights([light(1), light(2, { category: 'winners_curse' })]), { ui });
  assert.equal(ui.filter, null, 'a category that this night does not have falls back to all');
  assert.equal(visible(other.root).length, 2);
});

test('without the lens the held highlights say so, and a note counts them', () => {
  const { root } = mount(sealed, { lens: false });
  const rows = all(root, '.light');
  assert.equal(rows.length, 7);
  const held = rows.filter((row) => one(row, '.light-held'));
  assert.deepEqual(held.map((row) => row.getAttribute('data-id')), ['3', '4', '6', '2']);
  for (const row of held) {
    assert.equal(text(one(row, '.light-text')), STR.highlights.held);
    assert.equal(all(row, '.seat-chip').length, 0);
    assert.ok(one(row, 'a.light-link'), 'the jump itself is not private');
  }
  assert.equal(text(one(root, '.light-held-note')), '另有 4 条的细节需要打开底牌镜头。');
  const shown = text(root);
  for (const item of sealed.highlights) if (categoryBasis(item.category) === 'private') assert.ok(!shown.includes(item.text));
  assert.equal(all(mount(sealed).root, '.light-held-note').length, 0);
});

test('a night without highlights says so', () => {
  const { root } = mount(withHighlights([]));
  assert.equal(text(one(root, '.empty')), STR.highlights.empty);
  assert.equal(all(root, '.light').length, 0);
  const raw = fixture(CLOCKS);
  delete raw.highlights;
  assert.equal(all(mount(indexReplay(raw)).root, '.light').length, 0, 'a replay from before the highlights still opens');
});

test('the page shows no total and no ranking of seats, and keeps the banned words out', () => {
  for (const model of [sealed, clocks]) {
    for (const lens of [true, false]) {
      const shown = text(mount(model, { lens }).root);
      assert.ok(!/合计|总计|总分|平均|比率|效果/.test(shown));
      assert.ok(!/效率|福利|PoA|welfare|efficiency/i.test(shown));
    }
  }
  assert.ok(!/效率|福利|PoA|welfare|efficiency/i.test(JSON.stringify(STR.highlights)));
});

// ------------------------------------------------------------------------- hostile text

const ALLOWED_TAGS = new Set(['section', 'header', 'div', 'p', 'h2', 'h3', 'span', 'strong', 'small', 'ol', 'li', 'a', 'button']);
const ALLOWED_ATTRIBUTES = new Set(['class', 'data-category', 'data-id', 'data-moment', 'aria-label', 'aria-pressed', 'role', 'href', 'title', 'type']);

for (const [name, payload] of Object.entries(PAYLOADS)) {
  test(`hostile text "${name}" in a highlight or a code name stays text on the highlights page`, () => {
    for (const lens of [true, false]) {
      for (const revealed of [true, false]) {
        const model = indexReplay(poison(fixture(SEALED), payload));
        const { root } = mount(model, { lens, revealed });
        for (const tag of tagNames(root)) assert.ok(ALLOWED_TAGS.has(tag), `<${tag}>`);
        for (const { name: attr, value } of attributeValues(root)) {
          assert.ok(ALLOWED_ATTRIBUTES.has(attr), attr);
          assert.ok(!/[<>"]/.test(value) && !/javascript:/i.test(value), `${attr}=${value}`);
          if (attr === 'href') assert.match(value, /^#\/n\/[\w%.~-]+\/moment\/\d+$/);
        }
        const markup = serialize(root);
        for (const needle of ['<img', '<script', '<svg', '<iframe', '<b ', '<style']) assert.ok(!markup.includes(needle), needle);
      }
    }
  });
}

test('text that the page draws on the highlights page holds no control or invisible character', () => {
  for (const key of ['ansi', 'osc', 'bidi', 'invisible']) {
    const model = indexReplay(poison(fixture(SEALED), PAYLOADS[key]));
    assert.ok(!hasHidden(text(mount(model).root)), key);
    for (const string of strings(highlightsFor(model, { lens: true }))) assert.ok(!hasHidden(string), key);
  }
});
