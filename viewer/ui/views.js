// The parts of a night, as links in the top bar: the lots one by one, the three-minute version,
// the list of highlights, the awards, the review of the host. They are real links to addresses
// of the page (lib/route.js), so the browser's back and forward buttons and "copy link address"
// work, and the page follows the address like it follows a lot link. The link to the lots goes
// to the lot that is playing. The three-minute version is switched off, with its reason as a
// hover hint, when the replay has no highlight that can be played.

import { h, setAttr, toggle } from '../lib/dom.js';
import { formatHash, formatPageHash } from '../lib/route.js';
import { STR } from '../lib/strings.js';

export const VIEWS = Object.freeze(['lots', 'recap', 'highlights', 'awards', 'review']);

/**
 * Fill `nav` with one link per view. update(current, lotNo) marks the current view and points
 * the link to the lots at lot `lotNo`.
 */
export function mountViews(doc, nav, model, { recapAvailable = true } = {}) {
  const links = new Map();
  for (const view of VIEWS) {
    const link = h(doc, 'a', { class: `view-link view-${view}`, 'data-view': view }, STR.views[view]);
    nav.appendChild(link);
    links.set(view, link);
  }

  function update(current, lotNo) {
    for (const [view, link] of links) {
      const off = view === 'recap' && !recapAvailable;
      const href = view === 'lots' ? formatHash(model.night.id, lotNo) : formatPageHash(model.night.id, view);
      setAttr(link, 'href', off ? null : href);
      setAttr(link, 'aria-disabled', off ? true : null);
      setAttr(link, 'aria-current', view === current ? 'page' : null);
      setAttr(link, 'title', off ? STR.views.recapOff : null);
      toggle(link, 'is-current', view === current);
      toggle(link, 'is-off', off);
    }
  }

  return { update, links };
}
