// The public channel of one lot: speech bubbles with code names only.
//
// With the hole-card lens on, a thought bubble hangs under the line it belongs to: the inner
// monologue of the call that produced the line, and the mind-reading guess with its truth.

import { h, toggle } from '../lib/dom.js';
import { STR } from '../lib/strings.js';

/**
 * Mount the bubbles of a lot under `parent`. All bubbles exist from the start and start out
 * pending; update(state) shows them as the script reaches them.
 * ctx: {model, lens}
 */
export function mountChat(doc, parent, lot, ctx) {
  const { model, lens } = ctx;
  const quoted = (seat) => `「${model.codename(seat)}」`;
  const items = lot.talk.map((talk) => {
    const thought = lens ? (model.thoughts(lot.lot, talk.seat).find((t) => t.phase === 'talk') ?? null) : null;
    const guess = lens ? model.guess(lot.lot, talk.seat) : null;
    const mono = thought === null ? null : h(doc, 'p', { class: 'thought mono is-pending' }, '\u{1F4AD} ', thought.text);
    const read =
      guess === null
        ? null
        : h(
            doc,
            'p',
            { class: 'thought guess is-pending' },
            '\u{1F52E} ',
            STR.lens.guess(guess.target, quoted(guess.target), guess.guess, guess.truth),
          );
    const pledge =
      talk.pledge === null ? null : h(doc, 'p', { class: 'pledge' }, STR.stage.pledge(talk.pledge, quoted));
    const silent = talk.text === '';
    const el = h(
      doc,
      'li',
      { class: `bubble seat-c${talk.seat} is-pending`, 'data-seat': talk.seat },
      h(
        doc,
        'div',
        { class: 'bubble-head' },
        h(doc, 'span', { class: 'seat-no' }, String(talk.seat)),
        h(doc, 'strong', { class: 'bubble-name' }, model.codename(talk.seat)),
      ),
      h(doc, 'p', { class: silent ? 'bubble-text is-silent' : 'bubble-text' }, silent ? STR.stage.silent : talk.text),
      pledge,
      mono,
      read,
    );
    return { el, mono, read };
  });
  const empty = items.length === 0 ? h(doc, 'p', { class: 'chat-empty' }, STR.stage.chatEmpty) : null;
  const root = h(doc, 'div', { class: 'chat' }, empty, h(doc, 'ol', { class: 'chat-log' }, items.map((i) => i.el)));
  parent.appendChild(root);

  function update(state) {
    items.forEach((item, i) => {
      const beat = state.talks[i];
      toggle(item.el, 'is-pending', !beat.shown);
      toggle(item.el, 'is-speaking', beat.speaking);
      if (item.mono !== null) toggle(item.mono, 'is-pending', !beat.thought);
      if (item.read !== null) toggle(item.read, 'is-pending', !beat.thought);
    });
  }

  return { root, update, destroy: () => root.remove() };
}
