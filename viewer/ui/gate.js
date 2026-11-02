// The gate in front of a page that tells how the night ended. It says so, and offers one button
// that opens the page now. The page decides when the gate is not needed any more (the night has
// been watched to its end), and mounts its content instead. Used by the list of highlights and by
// the review of the host.

import { h } from '../lib/dom.js';
import { STR } from '../lib/strings.js';

/** Append the gate to `root`; `onReveal` is called when the reader opens the page. Returns the gate. */
export function mountGate(doc, root, onReveal) {
  const button = h(doc, 'button', { type: 'button', class: 'btn btn-primary gate-open' }, STR.gate.open);
  button.addEventListener('click', () => onReveal());
  const title = h(doc, 'h3', { class: 'section-title' }, STR.gate.title);
  const gate = h(doc, 'div', { class: 'gate', role: 'group', 'aria-label': STR.gate.title }, title, h(doc, 'p', {}, STR.gate.body), button);
  root.appendChild(gate);
  return gate;
}
