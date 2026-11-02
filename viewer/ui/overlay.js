// The overlay of one lot: the reference numbers next to the result, and the glossary.
//
// It appears with the hammer. What it shows comes from lib/overlay.js, which has already left
// out whatever the reader may not see: without the hole-card lens only the expected revenue
// (and, once the box is open, the price minus the true value of a blind box) get through. The
// per-seat table and the highest-value flag need the lens, because they depend on private
// values and budgets. Each term in the overlay is a button that opens the glossary at that
// term, and shows the meaning of the term as a hover hint (title). The glossary stays open from lot
// to lot: its state lives in ctx.ui, which the stage owns.

import { h, setAttr, toggle } from '../lib/dom.js';
import { milliText, signed } from '../lib/format.js';
import { overlayFor } from '../lib/overlay.js';
import { STR } from '../lib/strings.js';

const GLOSSARY_ID = 'glossary';

function signClass(n) {
  if (n === null || n === 0) return 'is-zero';
  return n > 0 ? 'is-plus' : 'is-minus';
}

function curseNote(gap) {
  if (gap > 0) return STR.overlay.curseOver;
  return gap < 0 ? STR.overlay.curseUnder : STR.overlay.curseSame;
}

/**
 * Mount the overlay of a lot as a section under `parent`, or return null when the replay has
 * nothing to show for the lot. ctx: {model, lens, ui}; ui.glossary is {open, focus}.
 * update(frame) shows the overlay from the hammer on, and the blind box line once it is open.
 */
export function mountOverlay(doc, parent, lot, ctx) {
  const data = overlayFor(lot, { lens: ctx.lens === true });
  if (data === null) return null;
  const { model } = ctx;
  const ui = ctx.ui ?? { glossary: { open: false, focus: null } };
  const gloss = ui.glossary;

  // Every term carries its meaning as a hover hint, in the list and wherever the overlay uses it.
  const meaning = new Map(STR.glossary.map((item) => [item.id, item.text]));
  const entries = STR.glossary.map((item) => {
    const el = h(
      doc,
      'div',
      { class: 'gloss-item', 'data-term': item.id, tabindex: -1 },
      h(doc, 'dt', { title: item.text }, item.term),
      h(doc, 'dd', {}, item.text),
    );
    return { id: item.id, el };
  });
  const panel = h(
    doc,
    'div',
    { class: 'glossary', id: GLOSSARY_ID, hidden: true },
    h(doc, 'h3', { class: 'section-title' }, STR.overlay.glossaryTitle),
    h(doc, 'dl', { class: 'gloss-list' }, entries.map((entry) => entry.el)),
  );
  const toggleButton = h(
    doc,
    'button',
    { type: 'button', class: 'btn gloss-toggle', 'aria-controls': GLOSSARY_ID, 'aria-expanded': false },
    STR.overlay.glossary,
  );

  function sync(reveal) {
    setAttr(toggleButton, 'aria-expanded', gloss.open);
    setAttr(panel, 'hidden', !gloss.open);
    for (const entry of entries) toggle(entry.el, 'is-focus', gloss.open && gloss.focus === entry.id);
    if (reveal && gloss.open) {
      const target = entries.find((entry) => entry.id === gloss.focus);
      if (target) {
        target.el.scrollIntoView?.({ block: 'nearest' });
        target.el.focus?.();
      }
    }
  }
  toggleButton.addEventListener('click', () => {
    gloss.open = !gloss.open;
    gloss.focus = null;
    sync(false);
  });
  const term = (id, text) => {
    const button = h(
      doc,
      'button',
      { type: 'button', class: 'term', 'aria-controls': GLOSSARY_ID, 'data-term': id, title: meaning.get(id) },
      text,
    );
    button.addEventListener('click', () => {
      gloss.open = true;
      gloss.focus = id;
      sync(true);
    });
    return button;
  };

  const facts = [];
  if (data.revenue !== null) {
    const { expected_milli: expected, actual, all_pay: allPay } = data.revenue;
    facts.push(
      h(doc, 'p', { class: 'ov-fact ov-revenue' }, term('revenue', STR.overlay.revenue), ' ', h(doc, 'strong', {}, STR.overlay.revenueLine(milliText(expected), actual, allPay))),
    );
  }
  if (data.highest !== null) {
    facts.push(
      h(
        doc,
        'p',
        { class: `ov-fact ov-highest ${data.highest ? 'is-yes' : 'is-no'}` },
        term('highest', STR.overlay.highest),
        '：',
        h(doc, 'strong', {}, data.highest ? STR.overlay.highestYes : STR.overlay.highestNo),
      ),
    );
  }
  const curse =
    data.curse_gap === null
      ? null
      : h(
          doc,
          'p',
          { class: 'ov-fact ov-curse is-pending' },
          term('curse', STR.overlay.curse),
          ' ',
          h(doc, 'strong', { class: signClass(data.curse_gap) }, signed(data.curse_gap)),
          `（${curseNote(data.curse_gap)}）`,
        );
  if (curse !== null) facts.push(curse);

  let table = null;
  if (data.seats.length > 0) {
    const head = h(
      doc,
      'tr',
      {},
      h(doc, 'th', { scope: 'col' }, STR.overlay.seat),
      h(doc, 'th', { scope: 'col' }, term('ref', STR.overlay.columns.ref)),
      h(doc, 'th', { scope: 'col' }, term('alpha', STR.overlay.columns.alpha)),
      h(doc, 'th', { scope: 'col' }, term('regret', STR.overlay.columns.regret)),
      h(doc, 'th', { scope: 'col' }, term('excess', STR.overlay.columns.excess)),
    );
    const num = (text, className = '') => h(doc, 'td', { class: `ov-num ${className}`.trim() }, text);
    const body = data.seats.map((row) =>
      h(
        doc,
        'tr',
        { class: `ov-row seat-c${row.seat}`, 'data-seat': row.seat },
        h(
          doc,
          'th',
          { scope: 'row', class: 'ov-seat' },
          h(doc, 'span', { class: 'seat-no' }, String(row.seat)),
          h(doc, 'strong', { class: 'ov-name' }, model.codename(row.seat)),
        ),
        num(row.ref === null ? STR.overlay.missing : String(row.ref)),
        num(row.alpha_milli === null ? STR.overlay.missing : milliText(row.alpha_milli, 2)),
        num(row.regret === null ? STR.overlay.missing : String(row.regret)),
        num(row.excess === null ? STR.overlay.missing : signed(row.excess), row.excess === null ? '' : signClass(row.excess)),
      ),
    );
    table = h(
      doc,
      'table',
      { class: 'ov-table' },
      h(doc, 'caption', { class: 'visually-hidden' }, STR.overlay.tableCaption),
      h(doc, 'thead', {}, head),
      h(doc, 'tbody', {}, body),
    );
  }

  const root = h(
    doc,
    'section',
    { class: `overlay is-pending${data.lens ? ' is-lens' : ''}`, 'aria-label': STR.overlay.title },
    h(doc, 'header', { class: 'overlay-head' }, h(doc, 'h3', { class: 'section-title' }, STR.overlay.title), toggleButton),
    h(doc, 'p', { class: 'overlay-note' }, STR.overlay.note),
    facts.length > 0 ? h(doc, 'div', { class: 'ov-facts' }, facts) : null,
    table,
    data.held ? h(doc, 'p', { class: 'ov-hint' }, STR.overlay.hidden) : null,
    panel,
  );
  parent.appendChild(root);
  sync(false);

  function update(frame) {
    toggle(root, 'is-pending', frame.verdict === null);
    if (curse !== null) toggle(curse, 'is-pending', frame.reveal === null);
  }

  return { root, update, destroy: () => root.remove() };
}
