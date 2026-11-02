// The hammer and, for a blind box, the opened box. Shared by every bid animation.
// All numbers are copied from the frame, which copied them from the replay.

import { clear, h } from '../lib/dom.js';
import { STR } from '../lib/strings.js';

/** Fill `box` with the verdict (and the reveal) of a frame. Both may be null. */
export function renderVerdict(doc, box, verdict, reveal, model) {
  clear(box);
  if (verdict === null) return;
  const quoted = (seat) => `「${model.codename(seat)}」`;
  const lines = [];
  if (verdict.unsold) {
    lines.push(h(doc, 'p', { class: 'verdict-main is-unsold' }, '\u{1F528} ', STR.bids.unsold));
  } else {
    lines.push(
      h(
        doc,
        'p',
        { class: 'verdict-main' },
        '\u{1F528} ',
        STR.bids.sold(verdict.winner, quoted(verdict.winner), verdict.price),
      ),
    );
  }
  if (verdict.tie_broken && verdict.tied.length > 0) {
    lines.push(h(doc, 'p', { class: 'verdict-note' }, STR.bids.tie(STR.bids.names(verdict.tied, quoted))));
  }
  if (verdict.rule === 'all_pay' && verdict.payments.length > 0) {
    lines.push(h(doc, 'p', { class: 'verdict-note' }, STR.bids.allPay(verdict.payments.length)));
  }
  if (verdict.newly_out.length > 0) {
    lines.push(h(doc, 'p', { class: 'verdict-note' }, STR.bids.out(STR.bids.names(verdict.newly_out, quoted))));
  }
  if (reveal !== null) {
    const opened = [h(doc, 'p', { class: 'verdict-main reveal-main' }, '\u{1F4E6} ', reveal.true_value === null ? '—' : STR.bids.openBox(reveal.true_value))];
    if (reveal.delta !== null) {
      const text = reveal.delta > 0 ? STR.bids.over(reveal.delta) : reveal.delta < 0 ? STR.bids.under(-reveal.delta) : STR.bids.same;
      opened.push(h(doc, 'p', { class: 'verdict-note' }, text));
    }
    lines.push(h(doc, 'div', { class: 'reveal' }, opened));
  }
  box.appendChild(h(doc, 'div', { class: 'verdict-body' }, lines));
}
