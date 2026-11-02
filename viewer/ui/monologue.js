// The inner monologues of the bid phases of one lot, as a list under a bid animation. They
// belong to the hole-card lens: ctx.bidThoughts is null when the lens is off, and then there is
// no list at all. Used by the animations that show a lot as a clock or a list.

import { h } from '../lib/dom.js';
import { STR } from '../lib/strings.js';

/**
 * Build the list for `thoughts` (may be null). Returns {root, items}; root is null when there
 * is nothing to show. Every item starts out pending: the caller shows them when it is time.
 */
export function monologueList(doc, thoughts, model) {
  const items = (thoughts ?? []).map((t) =>
    h(
      doc,
      'li',
      { class: 'thought mono is-pending' },
      '\u{1F4AD} ',
      h(doc, 'strong', {}, `${t.seat} 号${model.codename(t.seat)}`),
      `（${STR.lens.phases[t.phase] ?? STR.lens.phases.unknown}）`,
      t.text,
    ),
  );
  return { root: items.length > 0 ? h(doc, 'ul', { class: 'sum-mono' }, items) : null, items };
}
