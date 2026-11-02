// The seats around the table: code name, a small info row, and, with the hole-card lens on,
// a thought bubble with what the seat knew and nobody else did.
//
// Model ids appear in the info row of a seat card and nowhere else on the page.

import { seatInfo } from '../lib/format.js';
import { h, setText, toggle } from '../lib/dom.js';
import { lensFor } from '../lib/lens.js';
import { STR } from '../lib/strings.js';

function line(doc, className, text) {
  return text === '' ? null : h(doc, 'p', { class: `th-line ${className}` }, text);
}

function buildHole(doc, card) {
  const lines = [];
  if (card.value !== null) {
    lines.push(line(doc, 'th-value', card.valueKind === 'appraisal' ? STR.lens.appraisal(card.value) : STR.lens.value(card.value)));
  }
  if (card.budget !== null) lines.push(line(doc, 'th-budget', STR.lens.budget(card.budget, card.remaining)));
  if (card.mission !== null) lines.push(line(doc, 'th-mission', card.mission.text));
  if (card.plan !== null) {
    lines.push(line(doc, 'th-plan', STR.lens.plan(card.plan.stars, card.plan.budget)));
    if (card.plan.persona) lines.push(line(doc, 'th-persona', STR.lens.persona(card.plan.persona)));
  }
  for (const thought of card.thoughts.filter((t) => t.phase === 'plan')) {
    lines.push(line(doc, 'th-mono', `\u{1F4AD} ${STR.lens.phases.plan}：${thought.text}`));
  }
  const shown = lines.filter(Boolean);
  return h(doc, 'div', { class: 'thought hole' }, shown.length > 0 ? shown : h(doc, 'p', { class: 'th-line' }, STR.lens.noCard));
}

/**
 * Mount one card per seat under `parent`.
 * ctx: {model, lens}. update(state, frame) marks the speaker and the winner.
 */
export function mountSeats(doc, parent, lot, ctx) {
  const { model, lens } = ctx;
  const cards = new Map();
  for (const seat of model.seats) {
    const out = lot.sitting_out.includes(seat.seat);
    const state = h(doc, 'p', { class: 'seat-state' }, out ? STR.stage.sittingOut : '');
    const head = h(
      doc,
      'header',
      { class: 'seat-head' },
      h(doc, 'span', { class: 'seat-no' }, String(seat.seat)),
      h(doc, 'strong', { class: 'seat-name' }, seat.codename),
      h(doc, 'span', { class: 'seat-kind' }, STR.stage.kinds[seat.kind] ?? STR.stage.kinds.unknown),
    );
    const info = h(doc, 'p', { class: 'seat-info' }, seatInfo(seat, STR.stage.configured));
    const hole = lens ? buildHole(doc, lensFor(model, seat.seat, lot.lot)) : null;
    const el = h(
      doc,
      'article',
      { class: `seat seat-c${seat.seat} kind-${seat.kind}${out ? ' is-out' : ''}`, 'data-seat': seat.seat },
      head,
      info,
      state,
      hole,
    );
    cards.set(seat.seat, { el, state, out });
  }
  const root = h(doc, 'div', { class: 'seat-grid' }, [...cards.values()].map((c) => c.el));
  parent.appendChild(root);

  function update(state, frame) {
    const winner = frame?.verdict?.winner ?? null;
    for (const [seat, card] of cards) {
      const speaking = state.speaker === seat;
      const won = winner === seat;
      toggle(card.el, 'is-speaking', speaking);
      toggle(card.el, 'is-winner', won);
      let text = '';
      if (card.out) text = STR.stage.sittingOut;
      else if (won) text = STR.stage.won;
      else if (speaking) text = STR.stage.spoke;
      setText(card.state, text);
    }
  }

  return { root, update, destroy: () => root.remove() };
}
