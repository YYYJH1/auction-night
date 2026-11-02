// The awards page: one card per award, then the findings of the judges and the program (broken
// pledges, false claims, collusion; ui/verdicts.js), then the reference list of the bots.
//
// Every card says the rule the award is judged by and its lower limit, then what the replay
// recorded: the winners and the figure that decided it, or "vacant this night" with the limit
// that nobody reached. What it draws comes from lib/awards.js, which has already left out what
// the reader may not see. The names and model ids of the winners are text nodes made by the
// DOM helper like every other text of the page.

import { awardsFor } from '../lib/awards.js';
import { h } from '../lib/dom.js';
import { seatInfo } from '../lib/format.js';
import { STR } from '../lib/strings.js';
import { mountVerdicts } from './verdicts.js';

function winnerRow(doc, model, seat) {
  const entry = model.seat(seat);
  const info = entry === null ? '' : seatInfo(entry, STR.stage.configured);
  const name = h(doc, 'strong', { class: 'winner-name' }, model.codename(seat));
  const text = h(doc, 'span', { class: 'winner-text' }, name, info === '' ? null : h(doc, 'small', { class: 'winner-info' }, info));
  return h(doc, 'li', { class: `winner seat-c${seat}`, 'data-seat': seat }, h(doc, 'span', { class: 'seat-no' }, String(seat)), text);
}

const winnersList = (doc, model, item) =>
  h(doc, 'ul', { class: 'award-winners' }, item.winners.map((seat) => winnerRow(doc, model, seat)));

const basisLine = (doc, evidence) =>
  evidence === null ? null : h(doc, 'p', { class: 'award-basis' }, h(doc, 'strong', {}, STR.awards.basis), '：', evidence);

/** The body of a card: what happened to the award this night. */
function outcome(doc, model, item) {
  if (item.status === 'held') return [h(doc, 'p', { class: 'award-held' }, STR.awards.held)];
  if (item.status === 'vacant') {
    return [h(doc, 'p', { class: 'award-vacant' }, h(doc, 'strong', {}, STR.awards.states.vacant), '：', item.vacantText)];
  }
  return [winnersList(doc, model, item), basisLine(doc, item.evidence)];
}

function card(doc, model, item) {
  return h(
    doc,
    'article',
    { class: `award is-${item.status}`, 'data-award': item.key },
    h(
      doc,
      'header',
      { class: 'award-head' },
      h(doc, 'h3', { class: 'award-name' }, item.name),
      h(doc, 'span', { class: 'award-state' }, STR.awards.states[item.status]),
    ),
    outcome(doc, model, item),
    h(
      doc,
      'dl',
      { class: 'award-rule' },
      h(doc, 'dt', {}, STR.awards.rule),
      h(doc, 'dd', {}, item.rule),
      h(doc, 'dt', {}, STR.awards.floor),
      h(doc, 'dd', {}, item.floor ?? STR.awards.noFloor),
    ),
  );
}

/** One line of the reference list: the award, and who did it best among the bots. */
function referenceRow(doc, model, item) {
  const won = item.status === 'won' || item.status === 'tied';
  return h(
    doc,
    'li',
    { class: `ref-item is-${item.status}`, 'data-award': item.key },
    h(doc, 'strong', { class: 'ref-name' }, item.name),
    h(doc, 'span', { class: 'ref-state' }, STR.awards.states[item.status]),
    won ? winnersList(doc, model, item) : null,
    won && item.evidence !== null ? h(doc, 'p', { class: 'award-basis' }, item.evidence) : null,
  );
}

/**
 * Mount the awards page under `parent`. options.lens: the switch of the hole-card lens.
 * Returns {root, destroy}.
 */
export function mountAwards(doc, parent, model, options = {}) {
  const data = awardsFor(model, { lens: options.lens === true });
  const head = h(
    doc,
    'header',
    { class: 'page-head' },
    h(doc, 'h2', { class: 'page-title' }, STR.awards.title),
    h(doc, 'p', { class: 'page-note' }, STR.awards.intro),
    h(doc, 'p', { class: 'page-note' }, STR.awards.note),
  );
  const cards =
    data.items.length === 0
      ? h(doc, 'p', { class: 'empty' }, STR.awards.none)
      : h(doc, 'div', { class: 'award-grid' }, data.items.map((item) => card(doc, model, item)));
  const bots = h(
    doc,
    'section',
    { class: 'awards-bots', 'aria-label': STR.awards.botsTitle },
    h(doc, 'h3', { class: 'section-title' }, STR.awards.botsTitle),
    h(doc, 'p', { class: 'page-note' }, STR.awards.botsNote),
    data.bots.length === 0
      ? h(doc, 'p', { class: 'empty' }, STR.awards.botsNone)
      : h(doc, 'ul', { class: 'ref-list' }, data.bots.map((item) => referenceRow(doc, model, item))),
  );
  const root = h(doc, 'section', { class: 'awards', 'aria-label': STR.awards.title }, head, cards);
  mountVerdicts(doc, root, model, { lens: data.lens });
  root.appendChild(bots);
  parent.appendChild(root);
  return { root, destroy: () => root.remove() };
}
