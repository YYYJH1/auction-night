// Draws the frames of anim/summary.js: a note that this format has no animation here, the
// monologues of the lens, and the hammer. This is the picture of a format that the viewer does
// not know.

import { h, setAttr, toggle } from '../lib/dom.js';
import { STR } from '../lib/strings.js';
import { monologueList } from '../ui/monologue.js';
import { renderVerdict } from '../ui/verdict.js';

/**
 * Mount the picture of a lot under `parent`.
 * ctx: {model, bidThoughts}  (bidThoughts is null when the lens is off)
 */
export function mountSummary(doc, parent, lot, ctx) {
  const { model } = ctx;
  const { root: monologueRoot, items: monologues } = monologueList(doc, ctx.bidThoughts, model);
  const verdictBox = h(doc, 'div', { class: 'verdict' });
  const root = h(
    doc,
    'div',
    { class: 'bid-stage sum-stage', 'data-phase': 'intro' },
    h(doc, 'p', { class: 'sum-title' }, STR.summary.unknown),
    monologueRoot,
    verdictBox,
  );
  parent.appendChild(root);
  let verdictKey = -1;

  function update(frame) {
    setAttr(root, 'data-phase', frame.phase);
    monologues.forEach((item) => toggle(item, 'is-pending', frame.phase === 'intro'));
    const key = frame.verdict === null ? 0 : 1;
    if (key !== verdictKey) {
      verdictKey = key;
      renderVerdict(doc, verdictBox, frame.verdict, null, model);
    }
  }

  return { root, update, destroy: () => root.remove() };
}
