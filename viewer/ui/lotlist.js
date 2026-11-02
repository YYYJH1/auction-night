// The left column: every announced lot. Played lots are links to their deep link, so the
// browser's own back and forward buttons and "copy link address" just work.

import { h, setAttr, toggle } from '../lib/dom.js';
import { formatTitle } from '../lib/format.js';
import { formatHash } from '../lib/route.js';
import { STR } from '../lib/strings.js';

/** Fill the <ol> with one entry per announced lot. */
export function mountLotList(doc, list, model) {
  const entries = new Map();
  for (const entry of model.catalogLots) {
    const played = model.lot(entry.lot) !== null;
    const meta = [entry.tag, entry.tier === null ? null : STR.stage.tier(entry.tier), formatTitle(entry.format)]
      .filter(Boolean)
      .join(' · ');
    const inner = [
      h(doc, 'span', { class: 'lot-no' }, String(entry.lot)),
      h(doc, 'span', { class: 'lot-icon', 'aria-hidden': true }, entry.emoji),
      h(
        doc,
        'span',
        { class: 'lot-text' },
        h(doc, 'strong', { class: 'lot-name' }, entry.name),
        h(doc, 'small', { class: 'lot-meta' }, played ? meta : `${meta} · ${STR.lotList.unplayed}`),
      ),
    ];
    const link = played
      ? h(doc, 'a', { class: 'lot-link', href: formatHash(model.night.id, entry.lot), 'data-lot': entry.lot }, inner)
      : h(doc, 'span', { class: 'lot-link is-unplayed' }, inner);
    const item = h(doc, 'li', { class: 'lot-item' }, link);
    list.appendChild(item);
    entries.set(entry.lot, { item, link });
  }

  /** Mark the lot that is playing and the lots already seen. */
  function update(currentLot, visited) {
    for (const [lot, entry] of entries) {
      toggle(entry.item, 'is-current', lot === currentLot);
      toggle(entry.item, 'is-visited', visited.has(lot));
      setAttr(entry.link, 'aria-current', lot === currentLot ? 'step' : null);
    }
  }

  return { update };
}
