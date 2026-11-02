// The findings of a night, on the awards page: broken pledges, false claims and collusion, each
// with what it rests on, and a notice when a judge was absent.
//
// What it draws comes from lib/verdicts.js, which has already left out what the lens keeps back,
// so the page cannot show a private number whatever it does. Every word that comes from the
// replay (code names, quotes, the engine's own record lines, vendors) is a text node made by the
// DOM helper like every other text of the page.

import { h } from '../lib/dom.js';
import { intText } from '../lib/format.js';
import { findingsFor, sidesOf } from '../lib/verdicts.js';
import { STR } from '../lib/strings.js';

function seatChip(doc, model, seat) {
  const badge = h(doc, 'span', { class: 'seat-no' }, String(seat));
  return h(doc, 'span', { class: `chip seat-chip seat-c${seat}` }, badge, model.codename(seat));
}

const quoteLines = (doc, quotes) => quotes.map((text) => h(doc, 'p', { class: 'verdict-quote' }, STR.verdicts.quote(text)));

const labelled = (doc, className, label, text) => h(doc, 'p', { class: className }, h(doc, 'strong', {}, label), '：', text);

function groupHead(doc, title, count) {
  return h(
    doc,
    'header',
    { class: 'verdict-group-head' },
    h(doc, 'h3', { class: 'verdict-group-title' }, title),
    count === null ? null : h(doc, 'span', { class: 'verdict-count' }, STR.verdicts.count(count)),
  );
}

// -------------------------------------------------------------------------------- the judges

function absentNotice(doc, data) {
  if (data.absent.length === 0) return null;
  return h(
    doc,
    'div',
    { class: 'verdict-absent' },
    h(doc, 'strong', { class: 'verdict-absent-title' }, STR.verdicts.absentTitle),
    h(doc, 'ul', { class: 'verdict-absent-list' }, data.absent.map((judge) => h(doc, 'li', {}, judge.text))),
    h(doc, 'p', { class: 'verdict-absent-only' }, STR.verdicts.absentOnly),
  );
}

// ------------------------------------------------------------------------------- the pledges

function pledgeItem(doc, model, item) {
  const where =
    item.made !== null
      ? STR.verdicts.pledge.made(item.made, item.target)
      : item.lots.length > 0
        ? STR.verdicts.pledge.about(STR.verdicts.lots(item.lots))
        : null;
  return h(
    doc,
    'li',
    { class: 'verdict-item' },
    h(
      doc,
      'div',
      { class: 'verdict-head' },
      item.seats.map((seat) => seatChip(doc, model, seat)),
      where === null ? null : h(doc, 'span', { class: 'verdict-where' }, where),
      h(doc, 'span', { class: 'verdict-badge' }, STR.verdicts.pledge.broken),
    ),
    item.pledge === null ? null : h(doc, 'p', { class: 'verdict-pledge' }, item.pledge),
    quoteLines(doc, item.quotes),
    item.detail === null ? null : labelled(doc, 'verdict-detail', STR.verdicts.pledge.record, item.detail),
  );
}

function pledgeGroup(doc, model, data) {
  const copy = STR.verdicts.pledge;
  const items = data.pledges;
  return h(
    doc,
    'article',
    { class: 'verdict-group is-pledge' },
    groupHead(doc, copy.title, items.length === 0 ? null : items.length),
    h(doc, 'p', { class: 'verdict-basis' }, copy.basis),
    items.length > 0 && !data.lens ? h(doc, 'p', { class: 'verdict-note' }, copy.detailHeld) : null,
    items.length === 0
      ? h(doc, 'p', { class: 'verdict-none' }, copy.none)
      : h(doc, 'ul', { class: 'verdict-list' }, items.map((item) => pledgeItem(doc, model, item))),
  );
}

// -------------------------------------------------------------------------------- the claims

function claimItem(doc, model, item) {
  const copy = STR.verdicts.claim;
  return h(
    doc,
    'li',
    { class: 'verdict-item' },
    h(
      doc,
      'div',
      { class: 'verdict-head' },
      item.seats.map((seat) => seatChip(doc, model, seat)),
      item.lot === null ? null : h(doc, 'span', { class: 'verdict-where' }, STR.verdicts.lot(item.lot)),
      h(doc, 'span', { class: 'verdict-badge' }, copy.badge),
    ),
    labelled(doc, 'verdict-topic', copy.topicLabel, item.topicText),
    quoteLines(doc, item.quotes),
    h(doc, 'p', { class: 'verdict-gap' }, item.gap.text),
    item.detail === null ? null : labelled(doc, 'verdict-detail', copy.record, item.detail),
  );
}

function claimGroup(doc, model, data) {
  const copy = STR.verdicts.claim;
  const { held, items } = data.claims;
  return h(
    doc,
    'article',
    { class: 'verdict-group is-claim' },
    groupHead(doc, copy.title, held || items.length === 0 ? null : items.length),
    h(doc, 'p', { class: 'verdict-basis' }, copy.basis),
    held
      ? h(doc, 'p', { class: 'verdict-held' }, copy.held)
      : items.length === 0
        ? h(doc, 'p', { class: 'verdict-none' }, copy.none)
        : [
            h(doc, 'ul', { class: 'verdict-list' }, items.map((item) => claimItem(doc, model, item))),
            h(doc, 'p', { class: 'verdict-note' }, copy.rounding),
          ],
  );
}

// ----------------------------------------------------------------------------- the collusion

function statItem(doc, stat, kind) {
  const copy = STR.verdicts.collusion;
  const flags = [
    stat.over ? copy.over : null,
    stat.listed ? (kind === 'red' ? copy.listedRed : copy.listedSuspect) : null,
  ].filter(Boolean);
  const classes = ['stat-item', stat.over ? 'is-over' : null, stat.listed ? 'is-listed' : null].filter(Boolean).join(' ');
  return h(
    doc,
    'li',
    { class: classes },
    h(doc, 'span', { class: 'stat-lot' }, STR.verdicts.lot(stat.lot)),
    h(doc, 'strong', { class: 'stat-value' }, stat.text === null ? STR.overlay.missing : intText(stat.value)),
    stat.text === null ? null : h(doc, 'small', { class: 'stat-percent' }, `（${stat.text}%）`),
    flags.length === 0 ? null : h(doc, 'span', { class: 'stat-flags' }, flags.join('，')),
  );
}

function statsBlock(doc, finding) {
  const copy = STR.verdicts.collusion;
  return [
    h(doc, 'p', { class: 'stat-title' }, h(doc, 'strong', {}, copy.statsTitle)),
    h(doc, 'p', { class: 'verdict-note' }, copy.statsNote),
    h(
      doc,
      'p',
      { class: 'stat-threshold' },
      finding.threshold === null ? copy.thresholdMissing : copy.thresholdLine(intText(finding.threshold.milli), finding.threshold.text),
    ),
    finding.stats.length === 0
      ? h(doc, 'p', { class: 'verdict-none' }, copy.statsMissing)
      : h(doc, 'ul', { class: 'stat-list', 'aria-label': copy.statsTitle }, finding.stats.map((stat) => statItem(doc, stat, finding.kind))),
  ];
}

function collusionItem(doc, model, finding) {
  const copy = STR.verdicts.collusion;
  const title = finding.kind === 'red' ? copy.redTitle : copy.suspectTitle;
  return h(
    doc,
    'div',
    { class: `collusion is-${finding.kind}` },
    h(doc, 'header', { class: 'collusion-head' }, h(doc, 'h3', { class: 'collusion-title' }, title)),
    sidesOf(finding).map((sentence) => h(doc, 'p', { class: 'collusion-sides' }, sentence)),
    finding.judgesGone ? h(doc, 'p', { class: 'verdict-note' }, copy.judgesGone) : null,
    finding.ring.length === 0
      ? h(doc, 'p', { class: 'collusion-ring' }, copy.ringNone)
      : h(doc, 'p', { class: 'collusion-ring' }, h(doc, 'strong', {}, copy.ring), '：', finding.ring.map((seat) => seatChip(doc, model, seat))),
    finding.quotes.length === 0 ? null : [h(doc, 'p', { class: 'collusion-quotes-title' }, h(doc, 'strong', {}, copy.quotes)), quoteLines(doc, finding.quotes)],
    statsBlock(doc, finding),
    finding.detail === null ? null : labelled(doc, 'verdict-detail', copy.record, finding.detail),
  );
}

function collusionGroup(doc, model, data) {
  const copy = STR.verdicts.collusion;
  const { held, red, suspect } = data.collusion;
  return h(
    doc,
    'article',
    { class: 'verdict-group is-collusion' },
    groupHead(doc, copy.title, null),
    h(doc, 'ul', { class: 'verdict-rules' }, copy.rules.map((rule) => h(doc, 'li', {}, rule))),
    held
      ? h(doc, 'p', { class: 'verdict-held' }, copy.held)
      : red.length + suspect.length === 0
        ? h(doc, 'p', { class: 'verdict-none' }, copy.none)
        : [...red, ...suspect].map((finding) => collusionItem(doc, model, finding)),
  );
}

/**
 * Mount the findings under `parent`. options.lens: the switch of the hole-card lens.
 * Returns {root, destroy}.
 */
export function mountVerdicts(doc, parent, model, options = {}) {
  const data = findingsFor(model, { lens: options.lens === true });
  const root = h(
    doc,
    'section',
    { class: 'verdicts', 'aria-label': STR.verdicts.title },
    h(doc, 'h3', { class: 'section-title verdicts-title' }, STR.verdicts.title),
    h(doc, 'p', { class: 'page-note' }, STR.verdicts.intro),
    h(doc, 'p', { class: 'page-note' }, STR.verdicts.lensRule),
    absentNotice(doc, data),
    pledgeGroup(doc, model, data),
    claimGroup(doc, model, data),
    collusionGroup(doc, model, data),
  );
  parent.appendChild(root);
  return { root, destroy: () => root.remove() };
}
