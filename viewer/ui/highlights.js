// The list of highlights: score, category, lot and a sentence for each, and a link that jumps
// to the moment (#/n/<night>/moment/<id>, lib/route.js).
//
// A highlight tells how a lot ended, so the list spoils the night. It stays folded until the
// whole night has been watched (the page tells it with `revealed`) or the reader unfolds it
// with the button. What it draws comes from lib/highlights.js, which has already left out what
// the lens keeps back. The list can be narrowed to one category; the choice is kept in `ui`,
// which the page owns, so that it survives the lens being switched.

import { h, setAttr, setText, toggle } from '../lib/dom.js';
import { highlightsFor } from '../lib/highlights.js';
import { formatMomentHash } from '../lib/route.js';
import { STR } from '../lib/strings.js';
import { mountGate } from './gate.js';

function seatChip(doc, model, seat) {
  const badge = h(doc, 'span', { class: 'seat-no' }, String(seat));
  return h(doc, 'span', { class: `chip seat-chip seat-c${seat}` }, badge, model.codename(seat));
}

function itemBody(doc, model, item) {
  const where = item.lot === null ? STR.highlights.wholeNight : STR.highlights.lot(item.lot, item.lotName ?? '');
  return [
    h(
      doc,
      'span',
      { class: 'light-meta' },
      h(doc, 'span', { class: 'chip chip-cat' }, item.label),
      h(doc, 'span', { class: 'light-lot' }, where.trim()),
    ),
    item.held
      ? h(doc, 'span', { class: 'light-text light-held' }, STR.highlights.held)
      : item.text === null
        ? null
        : h(doc, 'span', { class: 'light-text' }, item.text),
    item.seats.length === 0
      ? null
      : h(doc, 'span', { class: 'light-seats' }, item.seats.map((seat) => seatChip(doc, model, seat))),
  ];
}

function entry(doc, model, item) {
  const score = h(
    doc,
    'span',
    { class: 'light-score' },
    h(doc, 'span', { class: 'visually-hidden' }, `${STR.highlights.scoreWord} `),
    h(doc, 'strong', {}, String(item.score)),
    h(doc, 'small', {}, '/10'),
  );
  const inner = [score, h(doc, 'span', { class: 'light-body' }, itemBody(doc, model, item))];
  const link = item.jumpable
    ? h(
        doc,
        'a',
        { class: 'light-link', href: formatMomentHash(model.night.id, item.id), 'data-moment': item.id },
        inner,
        h(doc, 'span', { class: 'light-go' }, `${STR.highlights.jump} ›`),
      )
    : h(doc, 'div', { class: 'light-link is-static', title: STR.highlights.noJump }, inner);
  const category = item.known ? item.category : 'unknown';
  return h(doc, 'li', { class: 'light', 'data-category': category, 'data-id': item.id }, link);
}

/**
 * Mount the highlights page under `parent`.
 * options: {lens, revealed, ui, onReveal}
 *   ui        {filter}: the category the list is narrowed to, or null for all. Kept by the page.
 *   onReveal  called when the reader unfolds the list; the page then mounts the page again
 */
export function mountHighlights(doc, parent, model, options = {}) {
  const { lens = false, revealed = false, onReveal = () => {} } = options;
  const ui = options.ui ?? { filter: null };
  const head = h(
    doc,
    'header',
    { class: 'page-head' },
    h(doc, 'h2', { class: 'page-title' }, STR.highlights.title),
    h(doc, 'p', { class: 'page-note' }, STR.highlights.intro),
  );
  const root = h(doc, 'section', { class: 'highlights', 'aria-label': STR.highlights.title }, head);
  parent.appendChild(root);

  if (!revealed) {
    mountGate(doc, root, onReveal);
    return { root, destroy: () => root.remove() };
  }

  const data = highlightsFor(model, { lens });
  if (data.items.length === 0) {
    root.appendChild(h(doc, 'p', { class: 'empty' }, STR.highlights.empty));
    return { root, destroy: () => root.remove() };
  }

  if (!data.categories.includes(ui.filter)) ui.filter = null;
  const buttons = [];
  const filterBar =
    data.categories.length < 2
      ? null
      : h(doc, 'div', { class: 'filter', role: 'group', 'aria-label': STR.highlights.filter });
  const count = h(doc, 'p', { class: 'page-note light-count', role: 'status' });
  const list = h(doc, 'ol', { class: 'light-list' });
  const rows = data.items.map((item) => ({ item, el: entry(doc, model, item) }));
  rows.forEach(({ el }) => list.appendChild(el));

  function apply() {
    const shown = rows.filter(({ item }) => ui.filter === null || item.category === ui.filter);
    for (const { item, el } of rows) toggle(el, 'is-filtered', !(ui.filter === null || item.category === ui.filter));
    for (const { key, button } of buttons) setAttr(button, 'aria-pressed', key === ui.filter);
    setText(count, STR.highlights.count(shown.length, rows.length));
  }

  if (filterBar !== null) {
    const named = data.categories.map((key) => ({ key, label: data.items.find((item) => item.category === key).label }));
    for (const { key, label } of [{ key: null, label: STR.highlights.all }, ...named]) {
      const button = h(doc, 'button', { type: 'button', class: 'btn chip-btn', 'data-category': key ?? 'all' }, label);
      button.addEventListener('click', () => {
        ui.filter = key;
        apply();
      });
      buttons.push({ key, button });
      filterBar.appendChild(button);
    }
    root.appendChild(filterBar);
  }
  root.appendChild(count);
  root.appendChild(h(doc, 'p', { class: 'page-note' }, STR.highlights.order));
  root.appendChild(list);
  if (data.held > 0) {
    root.appendChild(h(doc, 'p', { class: 'page-note light-held-note' }, STR.highlights.heldCount(data.held)));
  }
  apply();
  return { root, destroy: () => root.remove() };
}
