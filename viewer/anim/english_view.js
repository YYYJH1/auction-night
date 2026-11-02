// Draws the frames of anim/english.js: the price bar, the seats and the hammer.
//
// The DOM is built once per lot. update(frame) only changes classes, text and one style
// property (--fill, how far the bar is filled), so CSS does the painting. The positions of the
// exit marks and of the ticks are fixed when the lot is mounted, because the scale of the bar
// does not change during a lot. Under the hole-card lens the bar also carries the exit price of
// the winner, a number the table never saw, and the inner monologues.

import { h, setAttr, setText, toggle } from '../lib/dom.js';
import { STR } from '../lib/strings.js';
import { monologueList } from '../ui/monologue.js';
import { renderVerdict } from '../ui/verdict.js';
import { layout } from './english.js';
import { verdictOf } from './verdict.js';

const ROWS = 3; // exit marks take turns on three rows so that close prices do not hide each other
const STATE_CLASSES = ['is-in', 'is-left', 'is-skip', 'is-won'];

const round3 = (x) => Math.round(x * 1000) / 1000;

function place(el, fraction, row = null) {
  el.style.setProperty('--at', String(round3(fraction)));
  if (row !== null) el.style.setProperty('--row', String(row));
}

/**
 * Mount the price bar of a lot under `parent`.
 * ctx: {model, lens, reduceMotion, bidThoughts}  (bidThoughts is null when the lens is off)
 */
export function mountEnglish(doc, parent, lot, ctx) {
  const { model } = ctx;
  const lens = ctx.lens === true;
  const plan = layout(lot, { lens });
  const quoted = (seat) => `「${model.codename(seat)}」`;
  const verdict = verdictOf(lot);

  const exits = plan.stops.filter((stop) => stop.kind === 'exit');
  const marks = exits.map((stop, i) => {
    const el = h(
      doc,
      'li',
      { class: `eng-mark seat-c${stop.seat} is-pending` },
      h(doc, 'span', { class: 'seat-no' }, String(stop.seat)),
      h(doc, 'span', { class: 'eng-mark-price' }, String(stop.price)),
    );
    place(el, stop.price / plan.top, i % ROWS);
    return el;
  });
  const ticks = plan.ticks.map((value) => {
    const el = h(doc, 'li', { class: 'eng-tick' }, h(doc, 'span', {}, String(value)));
    place(el, value / plan.top);
    return el;
  });
  const ghost =
    lens && plan.winnerExit !== null
      ? h(doc, 'li', { class: 'eng-ghost is-pending' }, h(doc, 'span', { class: 'eng-ghost-label' }, STR.english.ghostMark(plan.winnerExit)))
      : null;
  if (ghost !== null) place(ghost, plan.winnerExit / plan.top);

  const fill = h(doc, 'div', { class: 'eng-fill' });
  const plot = h(
    doc,
    'div',
    { class: 'eng-plot' },
    h(doc, 'ul', { class: 'eng-marks', 'aria-hidden': true }, marks),
    h(doc, 'div', { class: 'eng-track' }, fill),
    h(doc, 'ul', { class: 'eng-ticks', 'aria-hidden': true }, ticks),
    ghost === null ? null : h(doc, 'ul', { class: 'eng-ghosts', 'aria-hidden': true }, ghost),
  );
  // Room above the track for the rows of marks that this lot needs, and no more.
  plot.style.setProperty('--rows', String(Math.min(ROWS, exits.length)));

  const priceNum = h(doc, 'strong', { class: 'eng-price-num' }, String(plan.start));
  const priceLine = h(
    doc,
    'p',
    { class: 'eng-price' },
    h(doc, 'span', { class: 'eng-price-label' }, STR.english.price),
    priceNum,
    plan.start > 0 ? h(doc, 'span', { class: 'eng-start' }, STR.english.startNote(plan.start)) : null,
  );

  const chips = new Map(
    plan.seats.map((seat) => {
      const state = h(doc, 'span', { class: 'eng-chip-state' });
      const el = h(
        doc,
        'li',
        { class: `eng-chip seat-c${seat} is-in`, 'data-seat': seat },
        h(doc, 'span', { class: 'seat-no' }, String(seat)),
        h(doc, 'strong', { class: 'eng-chip-name' }, model.codename(seat)),
        state,
      );
      return [seat, { el, state }];
    }),
  );
  const chipRow = h(doc, 'ul', { class: 'eng-seats' }, [...chips.values()].map((chip) => chip.el));

  const rows = plan.rows.map((row) =>
    h(
      doc,
      'li',
      { class: 'sum-row eng-log-row is-pending' },
      row.kind === 'skip' ? STR.english.notIn(row.seat, quoted(row.seat)) : STR.english.left(row.seat, quoted(row.seat), row.price, row.remaining),
    ),
  );
  const log = rows.length > 0 ? h(doc, 'ol', { class: 'sum-list eng-log' }, rows) : null;

  const winnerNote =
    lens && plan.winnerExit !== null && verdict.winner !== null
      ? h(doc, 'p', { class: 'eng-ghost-note is-pending' }, STR.english.ghost(verdict.winner, quoted(verdict.winner), plan.winnerExit, verdict.price))
      : null;
  const { root: monologueRoot, items: monologues } = monologueList(doc, ctx.bidThoughts, model);
  // The log shows what happened for the eye. This line says the same to a screen reader.
  const live = h(doc, 'p', { class: 'eng-live visually-hidden', 'aria-live': 'polite' });
  const verdictBox = h(doc, 'div', { class: 'verdict' });
  const root = h(
    doc,
    'div',
    { class: 'bid-stage eng-stage', 'data-phase': 'intro' },
    h(doc, 'p', { class: 'env-rule' }, STR.english.rule),
    plot,
    priceLine,
    chipRow,
    live,
    log,
    winnerNote,
    verdictBox,
    monologueRoot,
  );
  parent.appendChild(root);
  let verdictKey = -1;
  const nobodyIn = plan.seats.every((seat) => plan.skips.includes(seat)); // also true for an empty lot

  function update(frame) {
    setAttr(root, 'data-phase', frame.phase);
    const still = ctx.reduceMotion === true; // reduced motion: the bar jumps from exit to exit
    fill.style.setProperty('--fill', String(round3(still ? frame.settledFill : frame.fill)));
    setText(priceNum, String(still ? frame.settled : frame.price));
    for (const s of frame.seats) {
      const chip = chips.get(s.seat);
      if (chip === undefined) continue;
      const wanted = `is-${s.state}`;
      for (const name of STATE_CLASSES) toggle(chip.el, name, name === wanted);
      let text = STR.english.chip.in;
      if (s.state === 'skip') text = STR.english.chip.skip;
      else if (s.state === 'left') text = STR.english.chip.left(s.left_at);
      else if (s.state === 'won') text = STR.english.chip.won;
      setText(chip.state, text);
    }
    marks.forEach((mark, i) => toggle(mark, 'is-pending', i >= frame.shown));
    rows.forEach((row, i) => toggle(row, 'is-pending', i >= frame.rowsShown));
    if (ghost !== null) toggle(ghost, 'is-pending', frame.winnerExit === null);
    if (winnerNote !== null) toggle(winnerNote, 'is-pending', frame.winnerExit === null);
    monologues.forEach((item) => toggle(item, 'is-pending', frame.phase === 'intro'));

    let text = '';
    if (frame.verdict !== null) {
      if (nobodyIn) text = STR.english.nobody;
      else if (frame.verdict.unsold) text = STR.bids.unsold;
      else text = STR.bids.sold(frame.verdict.winner, quoted(frame.verdict.winner), frame.verdict.price);
    } else if (frame.phase === 'intro') text = nobodyIn ? '' : STR.english.intro(plan.start);
    else if (frame.last !== null) {
      text = STR.english.left(frame.last.seat, quoted(frame.last.seat), frame.last.price, frame.last.remaining);
    }
    setText(live, text);

    const key = frame.verdict === null ? 0 : 1;
    if (key !== verdictKey) {
      verdictKey = key;
      renderVerdict(doc, verdictBox, frame.verdict, null, model);
    }
  }

  return { root, update, destroy: () => root.remove() };
}
