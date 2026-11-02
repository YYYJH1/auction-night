// The review of the host: his recap of the night, and the predictions he made before it with what
// the night made of them. The predictions are on the first lot at the opening, without a result;
// the recap and the results are on a page of their own, behind the gate that holds the highlights,
// and behind the lens. The real night (mock_night.json) has a recap of five paragraphs and four
// predictions; the sealed night has two predictions that were both right; the other has a recap
// and no predictions.

import assert from 'node:assert/strict';
import test from 'node:test';

import { indexReplay } from '../../viewer/lib/replay.js';
import { paragraphsOf, predictionsFor, reviewFor } from '../../viewer/lib/review.js';
import { STR } from '../../viewer/lib/strings.js';
import { mountOpeningCard, mountReview } from '../../viewer/ui/review.js';
import { createStage } from '../../viewer/ui/stage.js';
import { launch, withPage } from './app_harness.js';
import { attributeValues, FakeDocument, serialize, tagNames } from './fake_dom.js';
import { CLOCKS, fixture, hasHidden, loadFixture, MOCK, modelOf, PAYLOADS, poison, SEALED, strings } from './helpers.js';

const mock = modelOf(MOCK);
const sealed = modelOf(SEALED);
const clocks = modelOf(CLOCKS);

/** A night with the host's extras replaced. */
function withHost(change, file = MOCK) {
  const raw = fixture(file);
  change(raw.host);
  return indexReplay(raw);
}

// -------------------------------------------------------------------------- the paragraphs

test('the recap is set in paragraphs: a line each, blank lines only separate, spaces are trimmed', () => {
  assert.deepEqual(paragraphsOf('一\n二\n\n三'), ['一', '二', '三']);
  assert.deepEqual(paragraphsOf('  一  \n\n\n  二\n'), ['一', '二']);
  assert.deepEqual(paragraphsOf('只有一段'), ['只有一段']);
  for (const odd of [null, undefined, '', '\n\n', 3, {}, []]) assert.deepEqual(paragraphsOf(odd), [], String(odd));
  assert.equal(paragraphsOf(mock.recap).length, 5, 'the real recap has five paragraphs');
  assert.equal(paragraphsOf(clocks.recap).length, 2);
  assert.equal(paragraphsOf(sealed.recap).length, 3);
  assert.ok(paragraphsOf(mock.recap)[3].startsWith('课代表时间：'));
});

test('the paragraphs of a recap keep their order and lose nothing but the line breaks', () => {
  for (const model of [mock, sealed, clocks]) {
    const joined = paragraphsOf(model.recap).join('\n');
    assert.equal(joined, model.recap.replace(/\n{2,}/g, '\n'));
  }
});

// ---------------------------------------------------------------------------- the data

test('with the lens the review holds the recap and the results of the predictions of the real night', () => {
  const review = reviewFor(mock, { lens: true });
  assert.equal(review.lens, true);
  assert.equal(review.nothing, false);
  assert.deepEqual([review.recap.present, review.recap.held, review.recap.paragraphs.length], [true, false, 5]);
  const { predictions } = review;
  assert.deepEqual(predictions.items.map((item) => [item.formatTitle, item.result]), [['密封一价', 'miss'], ['密封二价', 'miss'], ['盲盒', 'hit'], ['英式加价', 'miss']]);
  assert.deepEqual(predictions.tally, { hit: 1, miss: 3, open: 0 });
  assert.equal(predictions.tally.hit, loadFixture(MOCK).host.prediction_card.hits, 'the tally is the count of the file');
  assert.equal(predictions.held, false);
  assert.equal(predictionsFor(sealed, { lens: true }).tally.hit, loadFixture(SEALED).host.prediction_card.hits);
  assert.deepEqual(predictionsFor(sealed, { lens: true }).tally, { hit: 2, miss: 0, open: 0 });
});

test('without the lens the recap and every result are held back, and the sentences of the predictions are not', () => {
  const review = reviewFor(mock, { lens: false });
  assert.equal(review.lens, false);
  assert.deepEqual([review.recap.present, review.recap.held, review.recap.paragraphs], [true, true, []]);
  assert.equal(review.predictions.held, true);
  assert.equal(review.predictions.tally, null);
  assert.ok(review.predictions.items.every((item) => item.result === null));
  assert.deepEqual(review.predictions.items.map((item) => item.text), mock.predictions.items.map((item) => item.text));
  const text = JSON.stringify(review);
  for (const secret of ['今晚 12 件拍品', '课代表时间', '"hit"', '"miss"', '"open"']) assert.ok(!text.includes(secret), secret);
  for (const lens of [undefined, null, 0, 'yes', 1, {}]) {
    assert.equal(reviewFor(mock, { lens }).lens, false, String(lens));
    assert.equal(reviewFor(mock, { lens }).recap.held, true);
  }
  assert.equal(reviewFor(mock).predictions.held, true, 'no option at all means no lens');
});

test('a prediction the night could not settle is "nothing to compare", and a night without a card has no predictions', () => {
  const model = withHost((host) => {
    host.prediction_card.items[0].hit = null;
    host.prediction_card.items[1].hit = true;
  });
  const { items, tally } = predictionsFor(model, { lens: true });
  assert.deepEqual(items.map((item) => item.result), ['open', 'hit', 'hit', 'miss']);
  assert.deepEqual(tally, { hit: 2, miss: 1, open: 1 });
  assert.equal(predictionsFor(clocks, { lens: true }), null);
  const review = reviewFor(clocks, { lens: true });
  assert.deepEqual([review.predictions, review.recap.present, review.nothing], [null, true, false]);
});

test('a night with neither a recap nor predictions has nothing to review', () => {
  const none = withHost((host) => {
    delete host.recap;
    delete host.prediction_card;
  });
  for (const lens of [true, false]) {
    const review = reviewFor(none, { lens });
    assert.deepEqual([review.nothing, review.predictions, review.recap.present], [true, null, false]);
  }
  assert.equal(reviewFor(withHost((host) => { host.recap = ''; delete host.prediction_card; }), { lens: true }).nothing, true);
});

test('reviewFor changes nothing it is given', () => {
  const before = JSON.stringify([mock.recap, mock.predictions]);
  reviewFor(mock, { lens: true });
  reviewFor(mock, { lens: false });
  assert.equal(JSON.stringify([mock.recap, mock.predictions]), before);
  assert.deepEqual(reviewFor(mock, { lens: true }), reviewFor(mock, { lens: true }));
});

// ----------------------------------------------------------------------------- the page

function mount(model, options = {}) {
  const doc = new FakeDocument();
  const parent = doc.createElement('div');
  const view = mountReview(doc, parent, model, { lens: true, revealed: true, ...options });
  return { doc, parent, view, root: view.root };
}
const all = (el, selector) => el.querySelectorAll(selector);
const one = (el, selector) => el.querySelector(selector);
const text = (el) => el.textContent;

test('until it is opened the page holds a gate and nothing of the night', () => {
  let opened = 0;
  const { root } = mount(mock, { revealed: false, onReveal: () => (opened += 1) });
  assert.equal(text(one(root, '.page-title')), '主持人复盘');
  assert.equal(text(one(root, '.gate .section-title')), STR.gate.title);
  assert.equal(text(one(root, '.gate p')), STR.gate.body);
  const button = one(root, 'button.gate-open');
  assert.equal(text(button), STR.gate.open);
  button.dispatch('click');
  assert.equal(opened, 1);
  assert.equal(all(root, '.review-recap').length + all(root, '.review-pred').length + all(root, '.pred-item').length, 0);
  const spoilers = text(root);
  for (const paragraph of paragraphsOf(mock.recap)) assert.ok(!spoilers.includes(paragraph), 'no sentence of the recap');
  for (const item of mock.predictions.items) assert.ok(!spoilers.includes(item.text), 'and not the predictions either');
});

test('opened, with the lens: the recap in paragraphs, then the predictions with their results and the tally', () => {
  const { root } = mount(mock);
  assert.deepEqual(root.children.map((child) => child.className.split(' ')[0]), ['page-head', 'review-recap', 'review-pred']);
  const recap = one(root, '.review-recap');
  assert.equal(text(one(recap, '.section-title')), STR.review.recapTitle);
  assert.deepEqual(all(recap, 'p.review-para').map(text), paragraphsOf(mock.recap));
  const predictions = one(root, '.review-pred');
  const items = all(predictions, '.pred-item');
  assert.deepEqual(items.map((el) => el.className.split(' ').slice(1)), [['is-miss'], ['is-miss'], ['is-hit'], ['is-miss']]);
  assert.deepEqual(items.map((el) => text(one(el, '.pred-result'))), ['未中', '未中', '命中', '未中']);
  assert.deepEqual(items.map((el) => text(one(el, '.chip-format'))), ['密封一价', '密封二价', '盲盒', '英式加价']);
  assert.deepEqual(items.map((el) => text(one(el, '.pred-text'))), mock.predictions.items.map((item) => item.text));
  assert.equal(text(one(predictions, '.pred-tally')), '命中 1 条，未中 3 条，无法对账 0 条');
  assert.equal(all(predictions, '.review-held').length, 0);
  assert.equal(one(root, '.empty'), null, 'nothing is said to be missing');
});

test('a prediction with nothing to compare says so and says why', () => {
  const model = withHost((host) => {
    host.prediction_card.items[3].hit = null;
  });
  const { root } = mount(model);
  const open = one(root, '.pred-item.is-open');
  assert.equal(text(one(open, '.pred-result')), '无法对账');
  assert.equal(text(one(root, '.pred-tally')), '命中 1 条，未中 2 条，无法对账 1 条');
  assert.ok(all(root, '.page-note').map(text).includes(STR.review.openWhy));
  assert.ok(!all(mount(mock).root, '.page-note').map(text).includes(STR.review.openWhy), 'only when there is one');
});

test('without the lens the recap and the results are replaced by what they need, and the predictions stay', () => {
  const { root } = mount(mock, { lens: false });
  assert.equal(text(one(one(root, '.review-recap'), '.review-held')), STR.review.recapHeld);
  assert.equal(all(root, '.review-para').length, 0);
  assert.equal(text(one(one(root, '.review-pred'), '.review-held')), STR.review.predHeld);
  const items = all(root, '.pred-item');
  assert.equal(items.length, 4);
  assert.ok(items.every((el) => el.className === 'pred-item' && all(el, '.pred-result').length === 0));
  assert.equal(all(root, '.pred-tally').length, 0);
  const shown = text(root);
  for (const secret of ['命中', '未中', '今晚 12 件拍品', '课代表时间']) assert.ok(!shown.includes(secret), secret);
});

test('a night with a recap and no predictions, and one with neither', () => {
  const { root } = mount(clocks);
  assert.deepEqual(all(root, 'p.review-para').map(text), paragraphsOf(clocks.recap));
  assert.equal(text(one(one(root, '.review-pred'), '.empty')), STR.review.predNone);
  const none = withHost((host) => {
    delete host.recap;
    delete host.prediction_card;
  });
  for (const revealed of [true, false]) {
    const bare = mount(none, { revealed });
    assert.equal(text(one(bare.root, '.empty')), STR.review.empty);
    assert.equal(all(bare.root, '.gate').length, 0, 'nothing to hide, so no gate');
  }
  const onlyCard = mount(withHost((host) => delete host.recap), {});
  assert.equal(text(one(onlyCard.root, '.review-recap .empty')), STR.review.recapNone);
  assert.equal(all(onlyCard.root, '.pred-item').length, 4);
});

// ------------------------------------------------------------------------ the opening card

test('the card of predictions for the first lot holds the sentences and no result', () => {
  const doc = new FakeDocument();
  const parent = doc.createElement('div');
  const card = mountOpeningCard(doc, parent, mock);
  assert.equal(card.localName, 'section');
  assert.equal(card.className, 'opening-card');
  assert.equal(text(one(card, '.section-title')), STR.opening.title);
  const items = all(card, '.pred-item');
  assert.deepEqual(items.map((el) => text(one(el, '.pred-text'))), mock.predictions.items.map((item) => item.text));
  assert.deepEqual(items.map((el) => text(one(el, '.chip-format'))), ['密封一价', '密封二价', '盲盒', '英式加价']);
  assert.ok(items.every((el) => el.className === 'pred-item' && all(el, '.pred-result').length === 0));
  assert.equal(text(one(card, '.page-note')), '主持人开场押了 4 条，整夜结束后对账，结果在「主持人复盘」里。');
  assert.equal(parent.children.length, 1);
  const shown = text(card);
  for (const word of ['命中', '未中', '无法对账']) assert.ok(!shown.includes(word), word);
  const empty = doc.createElement('div');
  assert.equal(mountOpeningCard(doc, empty, clocks), null, 'a night without predictions has no card');
  assert.equal(empty.children.length, 0);
});

test('the stage opens the first lot with the card, and only when it is asked to', () => {
  const build = (model) => {
    const doc = new FakeDocument();
    const root = doc.createElement('main');
    return { root, stage: createStage(doc, root, model, {}) };
  };
  const classes = (root) => root.children.map((child) => child.className.split(' ')[0]);
  const { root, stage } = build(mock);
  stage.render(1, true, { opening: true });
  assert.deepEqual(classes(root), ['lot-card', 'lens-note', 'opening-card', 'bids', 'overlay', 'seats', 'chat-box', 'host']);
  assert.equal(all(root, '.pred-item').length, 4);
  stage.render(1, false, { opening: true });
  assert.equal(all(root, '.opening-card').length, 1, 'the predictions are public: with the lens off too');
  assert.equal(all(root, '.pred-result').length, 0);
  stage.render(1, true);
  assert.equal(all(root, '.opening-card').length, 0, 'not in the segments of the three-minute version');
  stage.render(2, true, { opening: true });
  assert.equal(all(root, '.opening-card').length, 0, 'and not on the second lot');
  const quiet = build(clocks);
  quiet.stage.render(1, true, { opening: true });
  assert.equal(all(quiet.root, '.opening-card').length, 0, 'a night without predictions');
});

test('the card goes to the first lot that was played', () => {
  const raw = fixture(SEALED);
  raw.timeline = raw.timeline.slice(1);
  const model = indexReplay(raw);
  assert.equal(model.lotNos[0], 2);
  const doc = new FakeDocument();
  const root = doc.createElement('main');
  const stage = createStage(doc, root, model, {});
  stage.render(2, true, { opening: true });
  assert.equal(all(root, '.opening-card').length, 1);
  stage.render(3, true, { opening: true });
  assert.equal(all(root, '.opening-card').length, 0);
});

// -------------------------------------------------------------------------- the whole page

const NIGHT = 'fixture-sealed-0001';
const okResponse = (body) => async (url) => ({ ok: true, status: 200, url, text: async () => body });
const loaded = (options = {}) => ({
  search: '?replay=../tests/fixtures/replays/sealed_four_lots.json',
  fetchImpl: okResponse(JSON.stringify(loadFixture(SEALED))),
  ...options,
});
const reviewHash = `#/n/${NIGHT}/review`;
const lotHash = (n) => `#/n/${NIGHT}/lot/${n}`;

test('the page opens the first lot with the predictions, and the other lots without them', () =>
  withPage(loaded(), async (page) => {
    const card = () => page.el('lot-stage').querySelectorAll('.opening-card');
    assert.equal(card().length, 1);
    assert.equal(page.el('lot-stage').querySelectorAll('.pred-item').length, 2);
    page.click('btn-next');
    assert.equal(card().length, 0);
    page.click('btn-prev');
    assert.equal(card().length, 1, 'back to the first lot: the opening again');
    page.click('lens');
    assert.equal(card().length, 1, 'the lens does not touch it');
    page.navigate(`#/n/${NIGHT}/recap`);
    assert.equal(card().length, 0, 'the three-minute version is for the best moments');
  }));

test('the review is a part of the page with its own address, behind the gate until the night is over', () =>
  withPage(loaded(), async (page) => {
    page.navigate(reviewHash);
    assert.equal(page.el('page').hidden, false);
    assert.equal(page.el('lot-stage').hidden, true);
    assert.equal(page.el('recap-bar').hidden, true);
    assert.equal(page.el('transport').hidden, true);
    assert.equal(page.hash, reviewHash);
    assert.deepEqual(page.el('views').children.map((a) => a.getAttribute('aria-current')), [null, null, null, null, 'page']);
    assert.equal(page.pendingFrames, 0, 'nothing runs behind it');
    assert.equal(page.el('page').querySelectorAll('.gate').length, 1);
    assert.equal(page.el('page').querySelectorAll('.review-para').length, 0);
    page.el('page').querySelector('button.gate-open').dispatch('click');
    assert.equal(page.el('page').querySelectorAll('.gate').length, 0);
    assert.equal(page.el('page').querySelectorAll('.review-para').length, 3);
    assert.equal(page.el('page').querySelectorAll('.pred-item.is-hit').length, 2);
    assert.equal(page.text('page').includes('命中 2 条，未中 0 条，无法对账 0 条'), true);
    page.navigate(`#/n/${NIGHT}/highlights`);
    assert.equal(page.el('page').querySelectorAll('.light').length, 7, 'one gate for both: opened here, opened there');
  }));

test('the gate of the highlights opens the review, and watching the night to its end opens both', () =>
  withPage(loaded(), async (page) => {
    page.navigate(`#/n/${NIGHT}/highlights`);
    page.el('page').querySelector('button.gate-open').dispatch('click');
    page.navigate(reviewHash);
    assert.equal(page.el('page').querySelectorAll('.gate').length, 0);
    assert.equal(page.el('page').querySelectorAll('.review-para').length, 3);
  }));

test('watching the last lot to its end unfolds the review by itself', () =>
  withPage(loaded(), async (page) => {
    page.clickSpeed(4);
    page.run(120);
    assert.equal(page.currentLot(), '4');
    page.navigate(reviewHash);
    assert.equal(page.el('page').querySelectorAll('.gate').length, 0);
    assert.equal(page.el('page').querySelectorAll('.pred-result').length, 2);
  }));

test('the lens switch moves the review between the whole recap and what it needs', () =>
  withPage(loaded({ hash: reviewHash }), async (page) => {
    assert.equal(page.hash, reviewHash);
    page.el('page').querySelector('button.gate-open').dispatch('click');
    assert.equal(page.el('page').querySelectorAll('.review-para').length, 3);
    page.click('lens');
    assert.equal(page.el('page').querySelectorAll('.review-para').length, 0);
    assert.equal(page.el('page').querySelectorAll('.review-held').length, 2);
    assert.equal(page.el('page').querySelectorAll('.pred-item').length, 2);
    assert.equal(page.el('page').querySelectorAll('.pred-result').length, 0);
    page.click('lens');
    assert.equal(page.el('page').querySelectorAll('.review-para').length, 3);
  }));

test('a night with nothing to review opens the page with a sentence, and the link stays', async () => {
  const raw = loadFixture(SEALED);
  delete raw.host.recap;
  delete raw.host.prediction_card;
  await withPage(loaded({ fetchImpl: okResponse(JSON.stringify(raw)) }), async (page) => {
    assert.equal(page.el('views').children.find((a) => a.getAttribute('data-view') === 'review').hasAttribute('aria-disabled'), false);
    page.navigate(reviewHash);
    assert.equal(page.el('page').querySelector('.empty').textContent, STR.review.empty);
    assert.equal(page.el('lot-stage').querySelectorAll('.opening-card').length, 0);
  });
});

test('a new replay folds the review again', async () => {
  const page = await launch(loaded());
  try {
    page.navigate(reviewHash);
    page.el('page').querySelector('button.gate-open').dispatch('click');
    assert.equal(page.el('page').querySelectorAll('.gate').length, 0);
    const text = JSON.stringify(loadFixture(MOCK));
    await page.chooseFile({ size: text.length, text: async () => text });
    page.navigate('#/n/mock-0001/review');
    assert.equal(page.el('page').querySelectorAll('.gate').length, 1);
    assert.equal(page.el('page').querySelectorAll('.review-para').length, 0);
  } finally {
    page.close();
  }
});

// ------------------------------------------------------------------------- hostile text

const ALLOWED_TAGS = new Set(['section', 'header', 'div', 'p', 'h2', 'h3', 'span', 'strong', 'ul', 'li', 'button']);
const ALLOWED_ATTRIBUTES = new Set(['class', 'aria-label', 'role', 'type']);

for (const [name, payload] of Object.entries(PAYLOADS)) {
  test(`hostile text "${name}" in the recap or a prediction stays text on the review and on the opening card`, () => {
    for (const file of [MOCK, SEALED]) {
      const model = indexReplay(poison(fixture(file), payload));
      const doc = new FakeDocument();
      const parent = doc.createElement('div');
      for (const lens of [true, false]) {
        for (const revealed of [true, false]) mountReview(doc, parent, model, { lens, revealed });
      }
      mountOpeningCard(doc, parent, model);
      for (const tag of tagNames(parent)) assert.ok(ALLOWED_TAGS.has(tag), `<${tag}>`);
      for (const { name: attr, value } of attributeValues(parent)) {
        assert.ok(ALLOWED_ATTRIBUTES.has(attr), attr);
        assert.ok(!/[<>"]/.test(value) && !/javascript:/i.test(value), `${attr}=${value}`);
      }
      const markup = serialize(parent);
      for (const needle of ['<img', '<script', '<svg', '<iframe', '<a ', '<b ', '<style']) assert.ok(!markup.includes(needle), needle);
      assert.ok(parent.querySelectorAll('.pred-text').every((el) => el.childNodes.length === 1 && el.childNodes[0].nodeType === 3));
    }
  });
}

test('text that the review draws holds no control or invisible character, and the recap keeps its paragraphs', () => {
  for (const key of ['ansi', 'osc', 'bidi', 'invisible']) {
    const model = indexReplay(poison(fixture(MOCK), PAYLOADS[key]));
    assert.ok(!hasHidden(text(mount(model).root)), key);
    for (const string of strings(reviewFor(model, { lens: true }))) assert.ok(!hasHidden(string), key);
  }
  const raw = fixture(MOCK);
  raw.host.recap = `one${String.fromCharCode(0x202e)}\r\ntwo\n\n\n\nthree\u0007`;
  assert.deepEqual(paragraphsOf(indexReplay(raw).recap), ['one', 'two', 'three']);
  raw.host.recap = `${'long line '.repeat(1000)}\nsecond`;
  const cut = indexReplay(raw).recap;
  assert.ok([...cut].length <= 4000 && cut.endsWith('…'), 'a recap is cut at the limit of a block');
});

test('the review adds nothing up and keeps the banned words out', () => {
  for (const model of [mock, sealed, clocks]) {
    for (const lens of [true, false]) {
      const shown = text(mount(model, { lens }).root);
      assert.ok(!/效率|福利|PoA|welfare|efficiency/i.test(shown));
    }
  }
  assert.ok(!/效率|福利|PoA|welfare|efficiency/i.test(JSON.stringify(STR.opening)));
});
