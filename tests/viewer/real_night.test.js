// The viewer plays real output: the mock night that the exporter wrote
// (tests/fixtures/replays/mock_night.json, made by tools/gen_mock_replay.py).
//
// The hand-written fixtures are small and tidy. The real file is a whole night of twelve lots and
// eight seats, canonical JSON with every field written (so `null` where a hand-written file has
// no key), mock seats next to bots, real ties, and every part of the show layer: judges,
// findings, awards and highlights. These tests open it the way a visitor does and check the
// pieces that the exporter promised the page:
// every English action, so that the lens can draw the winner's exit price; every Dutch action,
// the bots' included, so that it can draw the grab prices; and one line, one monologue and one
// reading per seat and lot, which is how the chat pairs them.

import assert from 'node:assert/strict';
import test from 'node:test';

import { grabsOf, ladderOf, stopOf } from '../../viewer/anim/dutch.js';
import { frameAt as englishFrame, duration as englishDuration, winnerExitOf } from '../../viewer/anim/english.js';
import { animationFor } from '../../viewer/anim/index.js';
import { clock } from '../../viewer/lib/format.js';
import { momentStart } from '../../viewer/lib/highlights.js';
import { indexReplay, validateReplay } from '../../viewer/lib/replay.js';
import { createStage } from '../../viewer/ui/stage.js';
import { launch, withPage } from './app_harness.js';
import { FakeDocument, serialize, tagNames } from './fake_dom.js';
import { fixture, hasHidden, loadFixture, strings } from './helpers.js';

const MOCK = 'mock_night.json';
const NIGHT = 'mock-0001';
const raw = () => fixture(MOCK);
const model = () => indexReplay(loadFixture(MOCK));
const okResponse = (text) => async (url) => ({ ok: true, status: 200, url, text: async () => text });
const sampleText = JSON.stringify(loadFixture(MOCK));
const loaded = (options = {}) => ({
  search: '?replay=../tests/fixtures/replays/mock_night.json',
  fetchImpl: okResponse(sampleText),
  ...options,
});
const pageHash = (name) => `#/n/${NIGHT}/${name}`;
const lotHash = (n) => `#/n/${NIGHT}/lot/${n}`;
const PLAYERS = [1, 2, 3, 4, 5, 6];
const BOTS = [7, 8];

// ------------------------------------------------------------------------- the file

test('the real sample passes the shape check and indexes to the night it records', () => {
  assert.deepEqual(validateReplay(loadFixture(MOCK)), { ok: true, errors: [] });
  const m = model();
  assert.equal(m.night.id, NIGHT);
  assert.equal(m.night.n_lots, 12);
  assert.deepEqual(m.lotNos, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]);
  assert.deepEqual(m.seatNos, [1, 2, 3, 4, 5, 6, 7, 8]);
  assert.deepEqual(m.seats.map((s) => s.kind), ['mock', 'mock', 'mock', 'mock', 'mock', 'mock', 'bot', 'bot']);
  assert.equal(m.seat(1).model_returned, 'mock-honest');
  assert.equal(m.seat(8).policy, 'economist');
  assert.equal(m.catalogLots.length, 12);
  assert.equal(m.lots.filter((l) => l.format === 'english').length, 2);
  assert.equal(m.lots.filter((l) => l.format === 'dutch').length, 2);
  assert.equal(m.reveal.thoughts.length > 100, true, 'the monologues of a whole night');
  assert.deepEqual([m.awards.length, m.botsReference.length, m.highlights.length], [10, 4, 15]);
  assert.equal(m.recap !== null && m.recap.length > 0, true, 'the host recap is there');
});

test('null fields of the canonical file read as missing, not as zero or as text', () => {
  const m = model();
  const lot = m.lot(1);
  assert.equal(lot.hammer.true_value, null, 'an ordinary lot has no true value');
  assert.equal(lot.dutch, null);
  assert.equal(lot.overlay.curse_gap, null);
  assert.equal(m.seat(7).model, null);
  assert.equal(m.seat(7).vendor, null);
  assert.equal(m.night.season_id, null);
  assert.equal(m.night.rotation, null);
  assert.equal(m.lot(3).hammer.true_value, loadFixture(MOCK).reveal.true_values['3'], 'a blind box has one');
  assert.equal(m.lot(3).overlay.curse_gap, m.lot(3).hammer.price - m.lot(3).hammer.true_value);
});

test('no text of the sample holds a hidden or control character', () => {
  for (const text of strings(loadFixture(MOCK))) assert.equal(hasHidden(text), false, JSON.stringify(text));
});

// ---------------------------------------------------- one line, one monologue, one reading

test('each model seat has one line, one monologue and one reading in every lot, and a bot has neither', () => {
  const m = model();
  const readings = loadFixture(MOCK).reveal.guesses;
  assert.equal(readings.length, 12 * PLAYERS.length);
  for (const lot of m.lots) {
    assert.deepEqual(lot.talk.map((t) => t.seat), lot.talk_order, `the lines of lot ${lot.lot} come in speaking order`);
    for (const seat of PLAYERS) {
      assert.equal(lot.talk.filter((t) => t.seat === seat).length, 1, `lot ${lot.lot} seat ${seat}: one line`);
      const spoken = m.thoughts(lot.lot, seat).filter((t) => t.phase === 'talk');
      assert.equal(spoken.length, 1, `lot ${lot.lot} seat ${seat}: one monologue of the talk phase`);
      const reading = m.guess(lot.lot, seat);
      assert.notEqual(reading, null, `lot ${lot.lot} seat ${seat}: a reading`);
      assert.equal(reading.abs_error, Math.abs(reading.guess - reading.truth));
      assert.equal(reading.truth, m.remainingBefore(reading.target, lot.lot), 'the truth is the budget left when it was asked');
    }
    for (const seat of BOTS) {
      assert.equal(lot.talk.filter((t) => t.seat === seat).length, 1, 'a bot speaks too');
      assert.deepEqual(m.thoughts(lot.lot, seat), [], 'but it has no monologue');
      assert.equal(m.guess(lot.lot, seat), null, 'and reads no minds');
    }
  }
});

test('with the lens on, the chat hangs the monologue and the reading under the line of its seat', () => {
  const m = model();
  const doc = new FakeDocument();
  const root = doc.createElement('main');
  const stage = createStage(doc, root, m, {});
  for (const lens of [true, false]) {
    stage.render(1, lens);
    const bubbles = root.querySelectorAll('li.bubble');
    assert.equal(bubbles.length, 8);
    const withMono = bubbles.filter((b) => b.querySelector('p.mono') !== null);
    const withReading = bubbles.filter((b) => b.querySelector('p.guess') !== null);
    assert.equal(withMono.length, lens ? 6 : 0);
    assert.equal(withReading.length, lens ? 6 : 0);
    if (lens) {
      assert.deepEqual(withMono.map((b) => b.getAttribute('data-seat')), PLAYERS.map(String));
      for (const b of withMono) assert.ok(b.querySelector('p.mono').textContent.includes('第1件'));
    }
  }
});

// ---------------------------------------------------------------------- the English clock

test('the winner of an English lot has its exit prices in the lot, and only the lens draws the last one', () => {
  const m = model();
  const english = m.lots.filter((l) => l.format === 'english');
  assert.equal(english.length, 2);
  for (const lot of english) {
    const winner = lot.hammer.winner;
    assert.notEqual(winner, null);
    const mine = lot.actions.filter((a) => a.seat === winner && a.value !== null);
    assert.ok(mine.length >= 2, 'the first exit price and its revisions');
    assert.deepEqual(new Set(mine.map((a) => a.phase)), new Set(['english_init', 'english_revise']));
    const last = mine.reduce((a, b) => (b.seq > a.seq ? b : a));
    assert.equal(winnerExitOf(lot), last.value);
    assert.ok(winnerExitOf(lot) >= lot.hammer.price, 'the winner would have stayed in for at least what it paid');
    const end = englishDuration(lot);
    assert.equal(englishFrame(lot, end, { lens: true }).winnerExit, last.value);
    assert.equal(englishFrame(lot, end, { lens: false }).winnerExit, null);
    assert.equal(englishFrame(lot, 0, { lens: true }).winnerExit, null, 'and not before the hammer');
  }
});

test('every seat that took part in an English lot has an action in it, in event order', () => {
  const m = model();
  for (const lot of m.lots.filter((l) => l.format === 'english')) {
    const seqs = lot.actions.map((a) => a.seq);
    assert.deepEqual(seqs, [...seqs].sort((a, b) => a - b));
    const active = new Set(lot.active);
    const init = lot.actions.filter((a) => a.phase === 'english_init').map((a) => a.seat);
    assert.deepEqual(new Set(init), active, 'everybody who was asked named an exit price');
    for (const seat of lot.dropouts.filter((d) => d.participated).map((d) => d.seat)) assert.ok(active.has(seat));
  }
});

// ----------------------------------------------------------------------- the Dutch clock

test('the lens has a grab price for every seat of a Dutch lot, the bots among them', () => {
  const m = model();
  const dutch = m.lots.filter((l) => l.format === 'dutch');
  assert.equal(dutch.length, 2);
  for (const lot of dutch) {
    const grabs = grabsOf(lot);
    assert.deepEqual(grabs.map((g) => g.seat), [...lot.active].sort((a, b) => a - b));
    const bots = lot.active.filter((seat) => BOTS.includes(seat));
    assert.ok(bots.length > 0, 'a bot is still in');
    for (const seat of bots) {
      const bot = grabs.find((g) => g.seat === seat);
      assert.ok(Number.isInteger(bot.grab), `the bot at seat ${seat} has a grab price`);
    }
    const stop = stopOf(lot);
    assert.notEqual(stop, null);
    const cells = grabs.map((g) => g.cell).filter((c) => c !== null);
    assert.equal(Math.min(...cells), stop, 'the first cell that somebody reaches sells the lot');
    const buyer = grabs.find((g) => g.seat === lot.hammer.winner);
    assert.ok(buyer.grab >= ladderOf(lot)[stop], 'and the buyer was willing to pay the price');
  }
});

// ------------------------------------------------------------------------ playing it

const BANNED_MARKUP = ['<img', '<script', '<svg', '<iframe', '<a ', '<style'];

test('every lot plays to its end with the lens on and off, and the stage draws only plain tags', () => {
  const m = model();
  const doc = new FakeDocument();
  const root = doc.createElement('main');
  const stage = createStage(doc, root, m, {});
  for (const lotNo of m.lotNos) {
    for (const lens of [true, false]) {
      stage.render(lotNo, lens);
      const end = stage.durationOf(lotNo);
      assert.ok(Number.isFinite(end) && end > 0, `lot ${lotNo} has a length`);
      const shown = [];
      for (let i = 0; i <= 8; i += 1) {
        const state = stage.update((end * i) / 8);
        shown.push(state.talks.filter((t) => t.shown).length);
      }
      assert.deepEqual(shown, [...shown].sort((a, b) => a - b), 'lines only ever appear');
      assert.equal(shown.at(-1), 8, 'and all eight are out at the end');
      for (const tag of tagNames(root)) assert.ok(/^[a-z0-9]+$/.test(tag), tag);
      const markup = serialize(root);
      for (const needle of BANNED_MARKUP) assert.ok(!markup.includes(needle), needle);
    }
  }
});

test('the length of a lot does not depend on the lens', () => {
  const m = model();
  for (const lot of m.lots) {
    const plain = animationFor(lot.format).duration(lot);
    assert.ok(Number.isFinite(plain) && plain > 0, `lot ${lot.lot}`);
  }
});

// ------------------------------------------------------------------------ the whole page

test('a visitor opens the sample, walks through its twelve lots and visits every part', () =>
  withPage(loaded(), async (page) => {
    assert.ok(page.text('night-label').startsWith(NIGHT));
    assert.equal(page.el('lot-list').querySelectorAll('a.lot-link').length, 12);
    assert.equal(page.currentLot(), '1');
    for (let lot = 2; lot <= 12; lot += 1) {
      page.click('btn-next');
      assert.equal(page.currentLot(), String(lot));
      assert.equal(page.hash, lotHash(lot));
    }
    page.click('lens');
    page.click('lens');
    page.navigate(pageHash('awards'));
    assert.ok(!page.el('page').textContent.includes('这场回放没有颁奖记录'));
    page.navigate(pageHash('highlights'));
    assert.equal(page.el('page').hidden, false);
    page.navigate(lotHash(7));
    assert.equal(page.currentLot(), '7');
    assert.equal(page.el('page').hidden, true);
  }));

// ------------------------------------------------------------------- the later fields, on show

test('a link to a highlight lands on the line, the bids or the hammer that its first event belongs to', async () => {
  const m = model();
  const doc = new FakeDocument();
  const stage = createStage(doc, doc.createElement('main'), m, {});
  const seen = new Set();
  for (const id of [3, 10, 7, 12]) {
    const item = m.highlight(id);
    const start = momentStart(m.lot(item.lot), stage.scriptOf(item.lot), item);
    await withPage(loaded({ hash: `#/n/${NIGHT}/moment/${id}` }), async (page) => {
      assert.equal(page.currentLot(), String(item.lot), `highlight ${id}`);
      assert.equal(page.text('time').split(' / ')[0], clock(start), `highlight ${id}`);
      assert.equal(page.hash, `#/n/${NIGHT}/moment/${id}`);
    });
    seen.add(start);
  }
  assert.ok(seen.size >= 3, 'the four highlights do not all begin in the same second');
  const lot2 = m.lot(2);
  const script2 = stage.scriptOf(2);
  const line = script2.beats.filter((b) => b.kind === 'talk').find((b) => b.seat === 5);
  assert.equal(momentStart(lot2, script2, m.highlight(3)), line.start, 'the broken pledge of seat 5 is at its line in lot 2');
  assert.ok(line.start > 0 && line.start < script2.bidStart);
});

test('the findings of the sample as a visitor sees them: twelve pledges, one red card, and the lens', () =>
  withPage(loaded(), async (page) => {
    page.navigate(pageHash('awards'));
    const root = () => page.el('page');
    assert.equal(root().querySelectorAll('.is-pledge .verdict-item').length, 12);
    assert.equal(root().querySelectorAll('.collusion.is-red').length, 1);
    assert.equal(root().querySelectorAll('.collusion.is-suspect').length, 0);
    assert.equal(root().querySelectorAll('.stat-item.is-over').length, 4);
    assert.ok(root().textContent.includes('第 5、6、7、9 件两侧都成立'));
    assert.ok(root().textContent.includes('红牌要至少两件两侧都成立'));
    assert.equal(root().querySelectorAll('.verdict-absent').length, 0);
    page.click('lens');
    assert.equal(root().querySelectorAll('.collusion').length, 0);
    assert.equal(root().querySelectorAll('.stat-item').length, 0);
    assert.equal(root().querySelectorAll('.is-pledge .verdict-item').length, 12, 'the pledges were made in public');
    assert.equal(root().querySelectorAll('.verdict-detail').length, 0);
    assert.equal(root().querySelectorAll('.verdict-held').length, 2);
    assert.ok(!root().textContent.includes('356'), 'the exit price of the winner of an English lot');
    page.click('lens');
    assert.equal(root().querySelectorAll('.collusion.is-red').length, 1);
  }));

test('the review of the sample: the predictions at the opening, the recap and the results at the end', () =>
  withPage(loaded(), async (page) => {
    const opening = () => page.el('lot-stage').querySelectorAll('.opening-card');
    assert.equal(opening().length, 1);
    assert.equal(opening()[0].querySelectorAll('.pred-item').length, 4);
    assert.equal(opening()[0].querySelectorAll('.pred-result').length, 0);
    page.click('btn-next');
    assert.equal(opening().length, 0);
    page.navigate(pageHash('review'));
    assert.equal(page.el('page').querySelectorAll('.gate').length, 1, 'the night has not been watched to its end');
    page.el('page').querySelector('button.gate-open').dispatch('click');
    const paragraphs = page.el('page').querySelectorAll('.review-para').map((p) => p.textContent);
    assert.equal(paragraphs.length, 5);
    assert.equal(paragraphs[0], '今晚 12 件拍品，8 个座位。名次第一的是 2 号阿铁（总分 137）。');
    assert.deepEqual(page.el('page').querySelectorAll('.pred-result').map((p) => p.textContent), ['未中', '未中', '命中', '未中']);
    assert.equal(page.el('page').querySelector('.pred-tally').textContent, '命中 1 条，未中 3 条，无法对账 0 条');
    assert.equal(page.hash, pageHash('review'));
    page.click('lens');
    assert.equal(page.el('page').querySelectorAll('.review-para').length, 0);
    assert.equal(page.el('page').querySelectorAll('.pred-item').length, 4);
    page.navigate(lotHash(1));
    assert.equal(opening().length, 1, 'and back to the first lot: the opening card again');
  }));

const withoutHighlights = JSON.stringify({ ...raw(), highlights: [] });

test('a night without highlights has no three-minute version, and the address says so after any page', () =>
  withPage(loaded({ fetchImpl: okResponse(withoutHighlights) }), async (page) => {
    const recap = page.el('views').children.find((a) => a.getAttribute('data-view') === 'recap');
    assert.equal(recap.getAttribute('aria-disabled'), 'true');
    page.navigate(pageHash('highlights'));
    page.navigate(pageHash('recap'));
    assert.equal(page.el('recap-bar').hidden, true, 'it falls back to the lots');
    assert.equal(page.el('lot-stage').hidden, false);
    assert.equal(page.hash, lotHash(1), 'and the address no longer names a page that is not shown');
  }));

test('opened on the file picker, the page takes the sample as a visitor would give it', async () => {
  const page = await launch({});
  try {
    await page.chooseFile({ size: sampleText.length, text: async () => sampleText });
    assert.equal(page.el('app').hidden, false);
    assert.equal(page.currentLot(), '1');
    assert.equal(page.el('lot-list').querySelectorAll('a.lot-link').length, 12);
  } finally {
    page.close();
  }
});

test('the sample is not changed by being read', () => {
  const before = JSON.stringify(raw());
  model();
  assert.equal(JSON.stringify(raw()), before);
});
