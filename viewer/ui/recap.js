// The caption of the three-minute version: which segment is playing, which lot it is, what the
// highlights of the lot say. It sits above the stage. While the card of a segment is up, it is
// shown large (data-phase="card"); once the lot plays on, it shrinks to a caption. When the
// whole version is over, it offers the next steps as links.
//
// The text of a highlight that rests on something private is left out while the lens is off
// (lib/highlights.js decides), so the caption never says more than the lens allows.

import { clear, h, setAttr } from '../lib/dom.js';
import { highlightsFor } from '../lib/highlights.js';
import { formatPageHash } from '../lib/route.js';
import { STR } from '../lib/strings.js';

/**
 * Mount the caption under `parent`. show(segment, lens) writes the caption of a segment,
 * update(at) puts it into the phase of the version's clock (recapAt in lib/recap.js).
 */
export function mountRecapBar(doc, parent, model, plan) {
  const root = h(doc, 'div', { class: 'recap-inner', 'data-phase': 'card' });
  parent.appendChild(root);
  let ends = null;

  function show(segment, lens) {
    clear(root);
    ends = null;
    const shown = highlightsFor(model, { lens });
    const lights = segment.ids.map((id) => shown.items.find((item) => item.id === id)).filter(Boolean);
    const name = model.catalog(segment.lot)?.name ?? '';
    root.appendChild(
      h(
        doc,
        'p',
        { class: 'recap-kicker' },
        STR.recap.title,
        ' · ',
        STR.recap.segment(segment.index, plan.segments.length),
      ),
    );
    root.appendChild(h(doc, 'h2', { class: 'recap-lot' }, STR.recap.lot(segment.lot, name).trim()));
    root.appendChild(
      h(
        doc,
        'ul',
        { class: 'recap-lights' },
        lights.map((item) =>
          h(
            doc,
            'li',
            { class: 'recap-light', 'data-category': item.known ? item.category : 'unknown' },
            h(doc, 'span', { class: 'chip chip-cat' }, item.label),
            h(doc, 'span', { class: 'chip' }, STR.recap.score(item.score)),
            h(doc, 'span', { class: 'recap-text' }, item.held ? STR.recap.held : (item.text ?? '')),
          ),
        ),
      ),
    );
    ends = h(
      doc,
      'p',
      { class: 'recap-end', hidden: true },
      STR.recap.done,
      ' ',
      h(doc, 'a', { class: 'recap-go', href: formatPageHash(model.night.id, 'highlights') }, STR.views.highlights),
      ' ',
      h(doc, 'a', { class: 'recap-go', href: formatPageHash(model.night.id, 'awards') }, STR.views.awards),
    );
    root.appendChild(ends);
  }

  function update(at) {
    setAttr(root, 'data-phase', at.phase);
    if (ends !== null) setAttr(ends, 'hidden', !at.done);
  }

  return { root, show, update, destroy: () => root.remove() };
}
