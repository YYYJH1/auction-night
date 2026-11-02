// Draws the frames of anim/dutch.js: the cells of the ladder, lit one after the other.
//
// The DOM is built once per lot; update(frame) only changes classes and text. Nothing on the
// cells says that a seat waits or hesitates: the clock is a way to show a result that was fixed
// before it started. Under the hole-card lens, each seat's grab price is drawn as a small badge
// on the cell where that seat would have bought, from the first frame on, together with a
// list in words. They are private until the end of the night, and they are already decided
// when the clock starts, which is what drawing them from the start shows.

import { h, setAttr, setText, toggle } from '../lib/dom.js';
import { MUTED_REASON } from '../lib/replay.js';
import { STR } from '../lib/strings.js';
import { monologueList } from '../ui/monologue.js';
import { renderVerdict } from '../ui/verdict.js';
import { grabsOf, layout } from './dutch.js';

const STATE_CLASSES = ['is-dim', 'is-lit', 'is-now', 'is-stop'];

function grabLine(g, model) {
  const name = `「${model.codename(g.seat)}」`;
  if (g.grab === null) {
    const reason = g.reason ? (STR.bids.reasons[g.reason] ?? '') : '';
    return g.reason === MUTED_REASON ? STR.dutch.muted(g.seat, name, reason) : STR.dutch.abstained(g.seat, name, reason);
  }
  const line = STR.dutch.grab(g.seat, name, g.grab, g.cell);
  return g.raw === null ? line : `${line}（${STR.bids.clamped(g.raw, g.grab)}）`;
}

/**
 * Mount the clock of a lot under `parent`.
 * ctx: {model, lens, bidThoughts}  (bidThoughts is null when the lens is off)
 */
export function mountDutch(doc, parent, lot, ctx) {
  const { model } = ctx;
  const plan = layout(lot);
  const grabs = ctx.lens === true ? grabsOf(lot) : [];
  const quoted = (seat) => `「${model.codename(seat)}」`;

  const cells = plan.ladder.map((price, i) => {
    const who = h(doc, 'span', { class: 'dut-who' });
    const badges = grabs
      .filter((g) => g.cell === i)
      .map((g) => h(doc, 'span', { class: `seat-no seat-c${g.seat} dut-grab` }, String(g.seat)));
    const el = h(
      doc,
      'li',
      { class: 'dut-cell is-dim' },
      h(doc, 'span', { class: 'dut-level' }, STR.dutch.level(i + 1)),
      h(doc, 'strong', { class: 'dut-price' }, String(price)),
      who,
      badges.length > 0 ? h(doc, 'span', { class: 'dut-grabs', 'aria-hidden': true }, badges) : null,
    );
    return { el, who };
  });
  const row = h(doc, 'ol', { class: 'dut-cells' }, cells.map((cell) => cell.el));

  const grabBox =
    grabs.length > 0
      ? h(
          doc,
          'div',
          { class: 'dut-grab-list' },
          h(doc, 'p', { class: 'dut-grab-title' }, STR.dutch.grabs),
          h(doc, 'ul', {}, grabs.map((g) => h(doc, 'li', {}, grabLine(g, model)))),
        )
      : null;

  const { root: monologueRoot, items: monologues } = monologueList(doc, ctx.bidThoughts, model);
  const caption = h(doc, 'p', { class: 'env-caption', 'aria-live': 'polite' });
  const verdictBox = h(doc, 'div', { class: 'verdict' });
  const root = h(
    doc,
    'div',
    { class: 'bid-stage dut-stage', 'data-phase': 'intro' },
    h(doc, 'p', { class: 'env-rule' }, STR.dutch.rule(plan.ladder.length > 0 ? plan.ladder.length : 10)),
    row,
    caption,
    grabBox,
    verdictBox,
    monologueRoot,
  );
  parent.appendChild(root);
  let verdictKey = -1;

  function update(frame) {
    setAttr(root, 'data-phase', frame.phase);
    frame.cells.forEach((c, i) => {
      const cell = cells[i];
      if (cell === undefined) return;
      const wanted = `is-${c.state}`;
      for (const name of STATE_CLASSES) toggle(cell.el, name, name === wanted);
      setText(cell.who, c.buyer === null ? '' : STR.dutch.buyer(c.buyer));
    });
    monologues.forEach((item) => toggle(item, 'is-pending', frame.phase === 'intro'));

    let text = '';
    if (plan.ladder.length === 0) text = frame.phase === 'intro' ? '' : STR.dutch.nobody;
    else if (frame.phase === 'intro') text = STR.dutch.intro;
    else if (frame.rested) {
      text =
        frame.buyer === null
          ? STR.dutch.none(plan.ladder.length)
          : STR.dutch.stop(frame.buyer.level, frame.buyer.price, frame.buyer.seat, quoted(frame.buyer.seat));
    } else if (frame.price !== null) text = STR.dutch.fall(frame.lit, frame.price);
    setText(caption, text);

    const key = frame.verdict === null ? 0 : 1;
    if (key !== verdictKey) {
      verdictKey = key;
      renderVerdict(doc, verdictBox, frame.verdict, null, model);
    }
  }

  return { root, update, destroy: () => root.remove() };
}
