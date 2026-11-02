import assert from 'node:assert/strict';
import test from 'node:test';

import { duration as dutchDuration, frameAt as dutchFrame, layout as dutchLayout } from '../../viewer/anim/dutch.js';
import { mountDutch } from '../../viewer/anim/dutch_view.js';
import { duration as englishDuration, frameAt as englishFrame, layout as englishLayout, TIMING } from '../../viewer/anim/english.js';
import { mountEnglish } from '../../viewer/anim/english_view.js';
import { STR } from '../../viewer/lib/strings.js';
import { createStage } from '../../viewer/ui/stage.js';
import { FakeDocument, serialize, textNodes } from './fake_dom.js';
import { CLOCKS, fixture, indexModel, modelOf } from './helpers.js';

const model = modelOf(CLOCKS);
const englishLot = model.lot(1);
const dutchLot = model.lot(2);
const text = (el) => el.textContent;
const all = (el, selector) => el.querySelectorAll(selector);
const one = (el, selector) => el.querySelector(selector);
const BID_PHASES = new Set(['english_init', 'english_revise', 'dutch', 'sealed']);

function mountEnglishOf(lot, { lens = true, reduceMotion = false, m = model } = {}) {
  const doc = new FakeDocument();
  const parent = doc.createElement('div');
  const bidThoughts = lens ? m.reveal.thoughts.filter((t) => t.lot === lot.lot && BID_PHASES.has(t.phase)) : null;
  const view = mountEnglish(doc, parent, lot, { model: m, lens, reduceMotion, bidThoughts });
  return { view, parent, plan: englishLayout(lot, { lens }), draw: (t) => view.update(englishFrame(lot, t, { lens })) };
}

function mountDutchOf(lot, { lens = true, m = model } = {}) {
  const doc = new FakeDocument();
  const parent = doc.createElement('div');
  const bidThoughts = lens ? m.reveal.thoughts.filter((t) => t.lot === lot.lot && BID_PHASES.has(t.phase)) : null;
  const view = mountDutch(doc, parent, lot, { model: m, lens, reduceMotion: false, bidThoughts });
  return { view, parent, plan: dutchLayout(lot), draw: (t) => view.update(dutchFrame(lot, t)) };
}

const chipStates = (root) => Object.fromEntries(all(root, '.eng-chip').map((c) => [c.getAttribute('data-seat'), ['is-in', 'is-left', 'is-skip', 'is-won'].find((k) => c.classList.contains(k))]));
const visible = (items) => items.filter((el) => !el.classList.contains('is-pending'));

// ============================================================================ English

test('the price bar is built once: five seats, three marks, five ticks, and no secret without the lens', () => {
  const { view } = mountEnglishOf(englishLot, { lens: false });
  const root = view.root;
  assert.equal(all(root, '.eng-chip').length, 5);
  assert.deepEqual(all(root, '.eng-chip').map((c) => c.getAttribute('data-seat')), ['1', '2', '3', '4', '5']);
  assert.equal(all(root, '.eng-mark').length, 3);
  assert.deepEqual(all(root, '.eng-tick').map(text), ['0', '50', '100', '150', '200']);
  assert.equal(all(root, '.eng-ghost').length, 0);
  assert.equal(all(root, '.eng-ghost-note').length, 0);
  assert.equal(all(root, '.thought').length, 0);
  assert.ok(!text(root).includes('180'), 'the exit price of the winner is nowhere in the markup');
});

test('with the lens the winner exit price is drawn on the bar and noted in words, both pending until the hammer', () => {
  const { view, plan, draw } = mountEnglishOf(englishLot, { lens: true });
  const root = view.root;
  const ghost = one(root, '.eng-ghost');
  const note = one(root, '.eng-ghost-note');
  assert.ok(ghost.classList.contains('is-pending') && note.classList.contains('is-pending'));
  assert.equal(text(one(ghost, '.eng-ghost-label')), STR.english.ghostMark(180));
  assert.equal(text(note), STR.english.ghost(3, '「小满」', 180, 165));
  assert.equal(ghost.style.getPropertyValue('--at'), String(Math.round((180 / plan.top) * 1000) / 1000));
  draw(plan.verdictAt - 0.01);
  assert.ok(ghost.classList.contains('is-pending'));
  draw(plan.verdictAt);
  assert.ok(!ghost.classList.contains('is-pending') && !note.classList.contains('is-pending'));
  draw(0);
  assert.ok(ghost.classList.contains('is-pending'), 'and pending again when the time goes back');
});

test('the number of the winner is nowhere on the bar before the hammer, even with the lens', () => {
  // The monologues of the lens may quote it: they are the player's own words. The bar, the price,
  // the chips, the log and the line for a screen reader may not.
  const { view, plan, draw } = mountEnglishOf(englishLot, { lens: true });
  const parts = ['.eng-plot', '.eng-price', '.eng-seats', '.eng-log', '.eng-live'];
  for (let i = 0; i < 200; i += 1) {
    const t = (plan.verdictAt * i) / 200;
    draw(t);
    for (const part of parts) {
      const shown = [...textNodes(one(view.root, part))].filter((n) => {
        for (let el = n.parentNode; el && el !== view.root; el = el.parentNode) if (el.classList?.contains('is-pending')) return false;
        return true;
      });
      assert.ok(!shown.some((n) => n.data.includes('180')), `a 180 on show in ${part} at ${t}`);
    }
  }
});

test('at the start: the bar at the reserve, every seat in, the one who did not take part marked', () => {
  const { view, draw } = mountEnglishOf(englishLot);
  draw(0);
  const root = view.root;
  assert.equal(root.getAttribute('data-phase'), 'intro');
  assert.equal(text(one(root, '.eng-price-num')), '20');
  assert.equal(text(one(root, '.eng-start')), STR.english.startNote(20));
  assert.equal(one(root, '.eng-fill').style.getPropertyValue('--fill'), String(20 / 200));
  assert.deepEqual(chipStates(root), { 1: 'is-in', 2: 'is-in', 3: 'is-in', 4: 'is-skip', 5: 'is-in' });
  assert.equal(text(one(one(root, '.eng-chip[data-seat="4"]'), '.eng-chip-state')), STR.english.chip.skip);
  assert.equal(visible(all(root, '.eng-mark')).length, 0);
  assert.deepEqual(visible(all(root, '.eng-log-row')).map(text), ['4 号「老实人」没有参加']);
  assert.equal(all(root, '.verdict-body').length, 0);
});

test('the bar climbs to each exit, the seat leaves there, and the mark and the log row appear', () => {
  const { view, plan, draw } = mountEnglishOf(englishLot);
  const root = view.root;
  const stops = plan.stops;
  draw(stops[0].t1 - 0.01);
  assert.equal(chipStates(root)[1], 'is-in');
  draw(stops[0].t1);
  assert.equal(text(one(root, '.eng-price-num')), '95');
  assert.equal(chipStates(root)[1], 'is-left');
  assert.equal(text(one(one(root, '.eng-chip[data-seat="1"]'), '.eng-chip-state')), STR.english.chip.left(95));
  assert.equal(visible(all(root, '.eng-mark')).length, 1);
  assert.equal(text(one(root, '.eng-live')), STR.english.left(1, '「青柠」', 95, 3));
  draw(stops[2].t1);
  assert.equal(text(one(root, '.eng-price-num')), '165');
  assert.equal(visible(all(root, '.eng-mark')).length, 3);
  assert.deepEqual(visible(all(root, '.eng-log-row')).map(text), [
    '4 号「老实人」没有参加',
    '1 号「青柠」在 95 退出，场上还剩 3 人',
    '2 号「麦芽」在 140 退出，场上还剩 2 人',
    '5 号「经济学家」在 165 退出，场上还剩 1 人',
  ]);
  assert.deepEqual(chipStates(root), { 1: 'is-left', 2: 'is-left', 3: 'is-in', 4: 'is-skip', 5: 'is-left' });
});

test('the marks sit where the prices are: at the price over the scale, on three rows in turn', () => {
  const { view, plan } = mountEnglishOf(englishLot);
  const marks = all(view.root, '.eng-mark');
  assert.deepEqual(marks.map((m) => m.style.getPropertyValue('--at')), [95, 140, 165].map((p) => String(Math.round((p / plan.top) * 1000) / 1000)));
  assert.deepEqual(marks.map((m) => m.style.getPropertyValue('--row')), ['0', '1', '2']);
  assert.deepEqual(marks.map((m) => text(one(m, '.eng-mark-price'))), ['95', '140', '165']);
  assert.deepEqual(marks.map((m) => text(one(m, '.seat-no'))), ['1', '2', '5']);
  assert.equal(one(view.root, '.eng-plot').style.getPropertyValue('--rows'), '3');
  assert.deepEqual(all(view.root, '.eng-tick').map((t) => t.style.getPropertyValue('--at')), ['0', '0.25', '0.5', '0.75', '1']);
});

test('a lot with one exit asks for one row of marks, and a lot with none for none', () => {
  const one_ = { ...englishLot, dropouts: englishLot.dropouts.slice(0, 2) };
  assert.equal(one(mountEnglishOf(one_).view.root, '.eng-plot').style.getPropertyValue('--rows'), '1');
  const none = { ...englishLot, dropouts: englishLot.dropouts.slice(0, 1) };
  assert.equal(one(mountEnglishOf(none).view.root, '.eng-plot').style.getPropertyValue('--rows'), '0');
});

test('the line for a screen reader follows the bar: the start, each exit, then the hammer', () => {
  const { view, plan, draw } = mountEnglishOf(englishLot);
  const live = () => text(one(view.root, '.eng-live'));
  draw(0);
  assert.equal(live(), STR.english.intro(20));
  draw(plan.stops[1].t1);
  assert.equal(live(), STR.english.left(2, '「麦芽」', 140, 2));
  draw(plan.stops[2].t1 + 0.3);
  assert.equal(live(), STR.english.left(5, '「经济学家」', 165, 1));
  draw(plan.verdictAt);
  assert.equal(live(), STR.bids.sold(3, '「小满」', 165));
  assert.ok(one(view.root, '.eng-live').classList.contains('visually-hidden'));
  assert.equal(one(view.root, '.eng-live').getAttribute('aria-live'), 'polite');
});

test('the hammer: the winner is marked, the verdict shows, the price stays where the bar stopped', () => {
  const { view, draw } = mountEnglishOf(englishLot);
  const root = view.root;
  draw(englishDuration(englishLot));
  assert.equal(root.getAttribute('data-phase'), 'done');
  assert.equal(chipStates(root)[3], 'is-won');
  assert.equal(text(one(one(root, '.eng-chip[data-seat="3"]'), '.eng-chip-state')), STR.english.chip.won);
  assert.equal(text(one(root, '.verdict-main')), '🔨 3 号「小满」拍得，成交价 165');
  assert.equal(text(one(root, '.eng-price-num')), '165');
});

test('the monologues of the lens wait for the intro, and are not there without it', () => {
  const on = mountEnglishOf(englishLot, { lens: true });
  assert.equal(all(on.view.root, '.sum-mono li').length, 5);
  on.draw(0);
  assert.equal(visible(all(on.view.root, '.sum-mono li')).length, 0);
  on.draw(TIMING.intro + 0.1);
  assert.equal(visible(all(on.view.root, '.sum-mono li')).length, 5);
  assert.equal(all(mountEnglishOf(englishLot, { lens: false }).view.root, '.sum-mono').length, 0);
});

test('the verdict comes before the monologues, so that the hammer is not pushed out of sight', () => {
  const { view } = mountEnglishOf(englishLot);
  const order = view.root.children.map((c) => c.className.split(' ')[0]);
  assert.ok(order.indexOf('verdict') < order.indexOf('sum-mono'));
});

test('reduced motion: the bar jumps from exit to exit and never shows a price in between', () => {
  const { view, plan } = mountEnglishOf(englishLot, { reduceMotion: true });
  const seen = new Set();
  const fills = new Set();
  const total = englishDuration(englishLot);
  for (let t = 0; t <= total; t += 0.04) {
    view.update(englishFrame(englishLot, t, { lens: true }));
    seen.add(text(one(view.root, '.eng-price-num')));
    fills.add(one(view.root, '.eng-fill').style.getPropertyValue('--fill'));
  }
  assert.deepEqual([...seen].sort((a, b) => a - b), ['20', '95', '140', '165']);
  assert.equal(fills.size, 4);
  const smooth = mountEnglishOf(englishLot);
  const seenSmooth = new Set();
  for (let t = 0; t <= total; t += 0.04) {
    smooth.view.update(englishFrame(englishLot, t, { lens: true }));
    seenSmooth.add(text(one(smooth.view.root, '.eng-price-num')));
  }
  assert.ok(seenSmooth.size > 20, 'without reduced motion the price runs through the values in between');
  assert.ok(plan.stops.length === 3);
});

test('seeking back and forth gives the same picture as playing straight to that time', () => {
  const a = mountEnglishOf(englishLot);
  const b = mountEnglishOf(englishLot);
  const end = englishDuration(englishLot);
  for (const t of [end, 1.5, end * 0.7, 0, end * 0.4]) a.draw(t);
  b.draw(end * 0.4);
  assert.equal(serialize(a.view.root, { canonical: true }), serialize(b.view.root, { canonical: true }));
});

test('an English lot that nobody entered shows the bar at rest and then says so', () => {
  const raw = fixture(CLOCKS);
  const lot = raw.timeline[0];
  lot.dropouts = lot.active.map((seat) => ({ seat, price: 0, remaining: [], participated: false }));
  lot.hammer = { format: 'english', winner: null, price: 0, payments: {}, unsold: true, tie_broken: false, tied: [], newly_out: [] };
  lot.actions = [];
  const m = indexModel(raw);
  const { view, draw } = mountEnglishOf(m.lot(1), { m });
  draw(0);
  assert.equal(text(one(view.root, '.eng-live')), '', 'nothing to announce while the bar is still at rest');
  draw(englishDuration(m.lot(1)));
  assert.equal(text(one(view.root, '.eng-live')), STR.english.nobody);
  assert.equal(text(one(view.root, '.verdict-main')), `🔨 ${STR.bids.unsold}`);
  assert.equal(visible(all(view.root, '.eng-mark')).length, 0);
  assert.ok(all(view.root, '.eng-chip').every((c) => c.classList.contains('is-skip')));
});

test('the markup of the bar holds only elements and attributes that are expected', () => {
  const { view, draw } = mountEnglishOf(englishLot);
  draw(englishDuration(englishLot));
  const tags = new Set();
  const attrs = new Set();
  (function walk(el) {
    tags.add(el.localName);
    for (const name of el.attrs.keys()) attrs.add(name);
    for (const child of el.children) walk(child);
  })(view.root);
  assert.deepEqual([...tags].sort(), ['div', 'li', 'ol', 'p', 'span', 'strong', 'ul']);
  assert.deepEqual([...attrs].sort(), ['aria-hidden', 'aria-live', 'class', 'data-phase', 'data-seat']);
});

// =============================================================================== Dutch

test('the clock is built once: ten dark cells with the prices of the ladder', () => {
  const { view } = mountDutchOf(dutchLot, { lens: false });
  const root = view.root;
  assert.deepEqual(all(root, '.dut-price').map(text), ['400', '360', '320', '280', '240', '200', '160', '120', '80', '40']);
  assert.deepEqual(all(root, '.dut-level').map(text), Array.from({ length: 10 }, (_, i) => `第 ${i + 1} 档`));
  assert.ok(all(root, '.dut-cell').every((c) => c.classList.contains('is-dim')));
  assert.equal(text(one(root, '.env-rule')), STR.dutch.rule(10));
});

test('the cells light from the top down, the newest one marked, and the clock stops on the sale', () => {
  const { view, plan, draw } = mountDutchOf(dutchLot);
  const cells = all(view.root, '.dut-cell');
  const state = () => cells.map((c) => ['is-dim', 'is-lit', 'is-now', 'is-stop'].find((k) => c.classList.contains(k)).slice(3));
  draw(0);
  assert.equal(view.root.getAttribute('data-phase'), 'intro');
  assert.equal(text(one(view.root, '.env-caption')), STR.dutch.intro);
  draw(plan.cellAt(1));
  assert.deepEqual(state(), ['lit', 'now', ...Array(8).fill('dim')]);
  assert.equal(text(one(view.root, '.env-caption')), STR.dutch.fall(2, 360));
  draw(plan.restAt);
  assert.deepEqual(state(), ['lit', 'lit', 'lit', 'stop', ...Array(6).fill('dim')]);
  assert.equal(text(one(view.root, '.env-caption')), STR.dutch.stop(4, 280, 2, '「麦芽」'));
  assert.deepEqual(cells.map((c) => text(one(c, '.dut-who'))), ['', '', '', '2 号', '', '', '', '', '', '']);
  draw(plan.verdictAt);
  assert.equal(text(one(view.root, '.verdict-main')), '🔨 2 号「麦芽」拍得，成交价 280');
  assert.deepEqual(state(), ['lit', 'lit', 'lit', 'stop', ...Array(6).fill('dim')], 'the clock stays where it stopped');
});

test('the lens draws each seat at the cell where its grab price would fire, from the very first frame', () => {
  const { view, draw } = mountDutchOf(dutchLot, { lens: true });
  draw(0);
  const cells = all(view.root, '.dut-cell');
  const badges = cells.map((c) => all(c, '.dut-grab').map(text));
  assert.deepEqual(badges, [[], [], [], ['2'], [], [], [], ['1'], [], []]);
  assert.deepEqual(all(view.root, '.dut-grab-list li').map(text), [
    '1 号「青柠」：150，落在第 8 档',
    '2 号「麦芽」：280，落在第 4 档',
  ]);
  assert.equal(text(one(view.root, '.dut-grab-title')), STR.dutch.grabs);
});

test('without the lens no grab price is anywhere in the clock', () => {
  const { view, plan, draw } = mountDutchOf(dutchLot, { lens: false });
  assert.equal(all(view.root, '.dut-grab').length, 0);
  assert.equal(all(view.root, '.dut-grab-list').length, 0);
  assert.equal(all(view.root, '.thought').length, 0);
  for (let i = 0; i <= 100; i += 1) {
    draw((plan.verdictAt * i) / 100);
    assert.ok(!text(view.root).includes('150'), 'the grab price of seat 1');
  }
});

test('nothing in the clock says that a seat waits, hesitates or decides at a level', () => {
  const { view, plan, draw } = mountDutchOf(dutchLot, { lens: false });
  const words = /犹豫|等待|等着|观望|纠结|考虑|决定|要不要|谁会|会不会|没有人出手/;
  for (let i = 0; i <= 200; i += 1) {
    draw((plan.total * i) / 200);
    const clockText = [text(one(view.root, '.env-rule')), text(one(view.root, '.env-caption')), ...all(view.root, '.dut-cell').map(text)].join(' ');
    assert.ok(!words.test(clockText), clockText);
  }
  assert.ok(STR.dutch.rule(10).includes('开拍前就一次报好'), 'and the rule line says that the price was named before the clock started');
});

test('an unsold clock lights every cell, says that no price was reached, and no seat is named', () => {
  const raw = fixture(CLOCKS);
  const lot = raw.timeline[1];
  lot.dutch = { ladder: lot.dutch.ladder, unsold: true };
  lot.hammer = { format: 'dutch', winner: null, price: 0, payments: {}, unsold: true, tie_broken: false, tied: [], newly_out: [] };
  lot.actions = [];
  const m = indexModel(raw);
  const view = mountDutchOf(m.lot(2), { m });
  view.draw(view.plan.restAt);
  assert.ok(all(view.view.root, '.dut-cell').every((c) => c.classList.contains('is-lit') || c.classList.contains('is-now') === false));
  assert.equal(all(view.view.root, '.dut-cell.is-stop').length, 0);
  assert.equal(text(one(view.view.root, '.env-caption')), STR.dutch.none(10));
  assert.ok(all(view.view.root, '.dut-who').every((w) => text(w) === ''));
  view.draw(dutchDuration(m.lot(2)));
  assert.equal(text(one(view.view.root, '.verdict-main')), `🔨 ${STR.bids.unsold}`);
});

test('a shorter ladder (a reserve price) makes a shorter clock, and the rule line counts its cells', () => {
  const lot = { ...dutchLot, dutch: { ...dutchLot.dutch, ladder: [400, 360, 320, 280, 240, 200], level: 4, price: 280 } };
  const { view } = mountDutchOf(lot);
  assert.equal(all(view.root, '.dut-cell').length, 6);
  assert.equal(text(one(view.root, '.env-rule')), STR.dutch.rule(6));
});

test('a clamped grab price and an abstention are told in words, under the lens only', () => {
  const lot = {
    ...dutchLot,
    actions: [
      { seq: 49, seat: 1, phase: 'dutch', status: 'clamped', value: 150, raw_value: 390, reason: null },
      { seq: 50, seat: 2, phase: 'dutch', status: 'abstain', value: null, raw_value: null, reason: 'timeout' },
    ],
  };
  const { view } = mountDutchOf(lot, { lens: true });
  assert.deepEqual(all(view.root, '.dut-grab-list li').map(text), [
    `1 号「青柠」：150，落在第 8 档（${STR.bids.clamped(390, 150)}）`,
    `2 号「麦芽」：弃权（${STR.bids.reasons.timeout}）`,
  ]);
  assert.equal(all(mountDutchOf(lot, { lens: false }).view.root, '.dut-grab-list').length, 0);
});

test('seeking back and forth gives the same clock as playing straight to that time', () => {
  const a = mountDutchOf(dutchLot);
  const b = mountDutchOf(dutchLot);
  const end = dutchDuration(dutchLot);
  for (const t of [end, 1.2, end * 0.7, 0, end * 0.5]) a.draw(t);
  b.draw(end * 0.5);
  assert.equal(serialize(a.view.root, { canonical: true }), serialize(b.view.root, { canonical: true }));
});

// ============================================================================ the stage

test('the stage plays both clocks to the end, with and without the lens, and the overlay follows the hammer', () => {
  const doc = new FakeDocument();
  const root = doc.createElement('main');
  const stage = createStage(doc, root, model);
  for (const lotNo of [1, 2]) {
    for (const lens of [true, false]) {
      stage.render(lotNo, lens);
      const overlay = one(root, '.overlay');
      stage.update(0);
      assert.ok(overlay.classList.contains('is-pending'), 'not before the hammer');
      stage.update(stage.durationOf(lotNo));
      assert.ok(!overlay.classList.contains('is-pending'), 'after it');
      assert.equal(all(root, '.verdict-body').length, 1);
    }
  }
});

test('the stage hands the lens to the animation: the winner exit price is on the bar only with the lens', () => {
  const doc = new FakeDocument();
  const root = doc.createElement('main');
  const stage = createStage(doc, root, model);
  stage.render(1, true);
  stage.update(stage.durationOf(1));
  assert.equal(visible(all(root, '.eng-ghost')).length, 1);
  stage.render(1, false);
  stage.update(stage.durationOf(1));
  assert.equal(all(root, '.eng-ghost').length, 0);
  assert.ok(![...textNodes(root)].some((n) => /退出价是 180|赢家的退出价/.test(n.data)));
});

test('the length of a lot does not depend on the lens', () => {
  const doc = new FakeDocument();
  const root = doc.createElement('main');
  const stage = createStage(doc, root, model);
  const lengths = [1, 2].map((lotNo) => stage.durationOf(lotNo));
  stage.render(1, true);
  stage.render(1, false);
  assert.deepEqual([1, 2].map((lotNo) => stage.durationOf(lotNo)), lengths);
});

test('the note about the winner exit price reads right whether it was above, at or below the price paid', () => {
  assert.equal(STR.english.ghost(3, '「小满」', 180, 165), '底牌镜头：3 号「小满」的退出价是 180。这个数没有公开，成交价只由倒数第二个离场的人定在 165。');
  assert.equal(STR.english.ghost(3, '「小满」', 165, 165), '底牌镜头：3 号「小满」的退出价是 165，和成交价一样高。');
  assert.equal(STR.english.ghost(3, '「小满」', 100, 165), '底牌镜头：3 号「小满」的退出价是 100。');
  assert.equal(STR.english.left(1, '「青柠」', 95, null), '1 号「青柠」在 95 退出');
  assert.equal(STR.english.left(1, '「青柠」', 95, 0), '1 号「青柠」在 95 退出，场上还剩 0 人');
});
