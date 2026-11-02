// The review of the host: the recap it wrote after the night, and the predictions it made before
// it, with what the night made of them.
//
// There are two pieces. The page of the review tells how the night ended, so it stands behind the
// same gate as the list of highlights (it opens when the night has been watched to its end, or
// when the reader opens it). The card of predictions is shown on the first lot, before anything
// has happened: it holds the predictions and nothing about how they turned out. What both draw
// comes from lib/review.js, which has already left out what the lens keeps back.

import { h } from '../lib/dom.js';
import { predictionsFor, reviewFor } from '../lib/review.js';
import { STR } from '../lib/strings.js';
import { mountGate } from './gate.js';

function predictionItem(doc, item) {
  const done = item.result !== null;
  return h(
    doc,
    'li',
    { class: done ? `pred-item is-${item.result}` : 'pred-item' },
    h(doc, 'span', { class: 'chip chip-format' }, item.formatTitle),
    h(doc, 'span', { class: 'pred-text' }, item.text),
    done ? h(doc, 'strong', { class: 'pred-result' }, STR.review.results[item.result]) : null,
  );
}

function predictionBlock(doc, data) {
  const copy = STR.review;
  const body =
    data.predictions === null
      ? [h(doc, 'p', { class: 'empty' }, copy.predNone)]
      : [
          h(doc, 'ul', { class: 'pred-list' }, data.predictions.items.map((item) => predictionItem(doc, item))),
          data.predictions.held
            ? h(doc, 'p', { class: 'review-held' }, copy.predHeld)
            : h(doc, 'p', { class: 'pred-tally' }, copy.tally(data.predictions.tally.hit, data.predictions.tally.miss, data.predictions.tally.open)),
          !data.predictions.held && data.predictions.tally.open > 0 ? h(doc, 'p', { class: 'page-note' }, copy.openWhy) : null,
        ];
  return h(
    doc,
    'section',
    { class: 'review-pred', 'aria-label': copy.predTitle },
    h(doc, 'h3', { class: 'section-title' }, copy.predTitle),
    h(doc, 'p', { class: 'page-note' }, copy.predIntro),
    body,
  );
}

function recapBlock(doc, data) {
  const copy = STR.review;
  const { present, held, paragraphs } = data.recap;
  return h(
    doc,
    'section',
    { class: 'review-recap', 'aria-label': copy.recapTitle },
    h(doc, 'h3', { class: 'section-title' }, copy.recapTitle),
    !present
      ? h(doc, 'p', { class: 'empty' }, copy.recapNone)
      : held
        ? h(doc, 'p', { class: 'review-held' }, copy.recapHeld)
        : h(doc, 'div', { class: 'review-text' }, paragraphs.map((text) => h(doc, 'p', { class: 'review-para' }, text))),
  );
}

/**
 * Mount the page of the review under `parent`.
 * options: {lens, revealed, onReveal}
 *   revealed  the gate is open; until then the page holds the gate and nothing of the night
 *   onReveal  called when the reader opens the gate; the page then mounts the page again
 * Returns {root, destroy}.
 */
export function mountReview(doc, parent, model, options = {}) {
  const { lens = false, revealed = false, onReveal = () => {} } = options;
  const data = reviewFor(model, { lens });
  const head = h(
    doc,
    'header',
    { class: 'page-head' },
    h(doc, 'h2', { class: 'page-title' }, STR.review.title),
    h(doc, 'p', { class: 'page-note' }, STR.review.intro),
  );
  const root = h(doc, 'section', { class: 'review', 'aria-label': STR.review.title }, head);
  parent.appendChild(root);
  if (data.nothing) {
    root.appendChild(h(doc, 'p', { class: 'empty' }, STR.review.empty));
  } else if (!revealed) {
    mountGate(doc, root, onReveal);
  } else {
    root.appendChild(recapBlock(doc, data));
    root.appendChild(predictionBlock(doc, data));
  }
  return { root, destroy: () => root.remove() };
}

/**
 * The card of predictions for the first lot: the sentences and the rule each one is about, no
 * result. Returns the card, or null when the replay has no predictions.
 */
export function mountOpeningCard(doc, parent, model) {
  const data = predictionsFor(model, { lens: false });
  if (data === null) return null;
  const root = h(
    doc,
    'section',
    { class: 'opening-card', 'aria-label': STR.opening.title },
    h(doc, 'h3', { class: 'section-title' }, STR.opening.title),
    h(doc, 'ul', { class: 'pred-list' }, data.items.map((item) => predictionItem(doc, { ...item, result: null }))),
    h(doc, 'p', { class: 'page-note' }, STR.opening.note(data.items.length)),
  );
  parent.appendChild(root);
  return root;
}
