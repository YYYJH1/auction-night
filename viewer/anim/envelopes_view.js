// Draws the frames of anim/envelopes.js.
//
// The DOM is built once per lot. Envelopes sit in seat order, so their places on the table
// say nothing about the bids; they open in bid order. update(frame) only changes classes,
// text and one style property (--open, the angle of the flap), so CSS does the painting.
// A closed envelope holds no bid text, and an opened one shows it.

import { h, setAttr, setText, toggle } from '../lib/dom.js';
import { MUTED_REASON } from '../lib/replay.js';
import { STR } from '../lib/strings.js';
import { renderVerdict } from '../ui/verdict.js';
import { envelopeOrder } from './envelopes.js';

const STATE_CLASSES = ['is-sealed', 'is-opening', 'is-open'];

function ruleText(lot) {
  if (lot.format === 'blind_box') return STR.bids.rule.blind;
  if (lot.format === 'sealed_first') return STR.bids.rule.first;
  if (lot.format === 'all_pay') return STR.bids.rule.all_pay;
  return STR.bids.rule.second;
}

function buildEnvelope(doc, seat, model, thought) {
  const bid = h(doc, 'span', { class: 'env-bid' });
  const slip = h(doc, 'div', { class: 'env-slip' }, bid);
  const body = h(
    doc,
    'div',
    { class: 'env-body' },
    h(doc, 'span', { class: 'env-badge' }, String(seat)),
    h(doc, 'span', { class: 'env-name' }, model.codename(seat)),
  );
  const flap = h(doc, 'div', { class: 'env-flap' });
  const paper = h(doc, 'div', { class: 'env-paper' }, slip, body, flap);
  const note = h(doc, 'p', { class: 'env-note' });
  const pay = h(doc, 'p', { class: 'env-pay' });
  const mono = thought === null ? null : h(doc, 'p', { class: 'thought mono is-pending' }, '\u{1F4AD} ', thought.text);
  const el = h(doc, 'li', { class: `env is-sealed seat-c${seat}`, 'data-seat': seat }, paper, note, pay, mono);
  return { el, bid, note, pay, mono };
}

/**
 * Mount the envelopes of a lot under `parent`.
 * ctx: {model, reduceMotion, bidThoughts}  (bidThoughts is null when the lens is off)
 */
export function mountEnvelopes(doc, parent, lot, ctx) {
  const { model } = ctx;
  const seats = envelopeOrder(lot)
    .map((row) => row.seat)
    .sort((a, b) => a - b);
  const items = new Map(
    seats.map((seat) => {
      const thought = ctx.bidThoughts?.find((t) => t.seat === seat) ?? null;
      return [seat, buildEnvelope(doc, seat, model, thought)];
    }),
  );
  const rule = h(doc, 'p', { class: 'env-rule' }, ruleText(lot));
  const row = h(doc, 'ul', { class: 'env-row' }, [...items.values()].map((item) => item.el));
  const caption = h(doc, 'p', { class: 'env-caption', 'aria-live': 'polite' });
  const verdictBox = h(doc, 'div', { class: 'verdict' });
  const root = h(doc, 'div', { class: 'bid-stage env-stage', 'data-phase': 'intro' }, rule, row, caption, verdictBox);
  parent.appendChild(root);
  let verdictKey = -1;

  function update(frame) {
    setAttr(root, 'data-phase', frame.phase);
    for (const e of frame.envelopes) {
      const item = items.get(e.seat);
      if (item === undefined) continue;
      const wanted = `is-${e.state}`;
      for (const name of STATE_CLASSES) toggle(item.el, name, name === wanted);
      toggle(item.el, 'is-winner', e.winner);
      toggle(item.el, 'is-leader', frame.verdict === null && frame.leader === e.seat);
      // Why a bid was zero or smaller than wanted is known to the seat and the engine, not to the
      // table. The notes belong to the hole-card lens; without it a bid is just a number.
      const notes = ctx.lens === true;
      const abstained = notes && e.status === 'abstain';
      const clamped = notes && e.status === 'clamped' && e.raw_value !== null;
      toggle(item.el, 'is-abstain', abstained);
      toggle(item.el, 'is-clamped', clamped);
      const open = ctx.reduceMotion ? (e.progress >= 1 ? 1 : 0) : Math.round(e.progress * 1000) / 1000;
      item.el.style.setProperty('--open', String(open));
      const muted = abstained && e.reason === MUTED_REASON;
      setText(item.bid, e.bid === null ? '' : muted ? STR.bids.muted : abstained ? STR.bids.abstain : String(e.bid));
      let note = '';
      if (abstained) note = e.reason ? (STR.bids.reasons[e.reason] ?? '') : '';
      else if (clamped) note = STR.bids.clamped(e.raw_value, e.bid);
      setText(item.note, note);
      setText(item.pay, e.pays !== null && e.pays > 0 ? STR.bids.pays(e.pays) : '');
      if (item.mono !== null) toggle(item.mono, 'is-pending', e.state === 'sealed');
    }
    let text = '';
    if (seats.length === 0 && frame.phase !== 'intro') text = STR.bids.nobody;
    else if (frame.phase === 'intro') text = STR.bids.intro;
    else if (frame.focus !== null) text = STR.bids.opening(frame.focus, `「${model.codename(frame.focus)}」`);
    setText(caption, text);
    const key = (frame.verdict === null ? 0 : 1) + (frame.reveal === null ? 0 : 2);
    if (key !== verdictKey) {
      verdictKey = key;
      renderVerdict(doc, verdictBox, frame.verdict, frame.reveal, model);
    }
  }

  return { root, update, destroy: () => root.remove() };
}
