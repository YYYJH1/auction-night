// The stage: the lot that is playing, assembled from the pieces and kept up to date.
//
// render(lot, lens, {opening}) builds the DOM of one lot. update(t) puts it into the state of time t:
// it shows the chat lines the script has reached, advances the bid animation, marks the
// speaker, shows the overlay once the hammer has fallen. Both are cheap enough to run on every
// frame. The stage owns the script of each lot, so the page asks it how long a lot lasts (and,
// for the highlights and the three-minute version, where in the lot a moment is), and the small
// state that outlives a lot (whether the glossary is open).

import { animationFor } from '../anim/index.js';
import { clear, h, toggle } from '../lib/dom.js';
import { formatClass, formatTitle } from '../lib/format.js';
import { buildScript, stateAt } from '../lib/script.js';
import { STR } from '../lib/strings.js';
import { mountChat } from './chat.js';
import { mountOverlay } from './overlay.js';
import { mountOpeningCard } from './review.js';
import { mountSeats } from './seats.js';

const BID_PHASES = new Set(['english_init', 'english_revise', 'dutch', 'sealed']);

function chip(doc, text, className = 'chip') {
  return h(doc, 'span', { class: className }, text);
}

function lotCard(doc, lot, model) {
  const item = lot.item;
  const klass = `fmt-${formatClass(lot.format)}`;
  const chips = [
    item?.tag ? chip(doc, item.tag, 'chip chip-tag') : null,
    item?.tier != null ? chip(doc, STR.stage.tier(item.tier)) : null,
    chip(doc, formatTitle(lot.format), `chip chip-format ${klass}`),
    item && item.reserve > 0 ? chip(doc, STR.stage.reserve(item.reserve)) : null,
  ];
  return h(
    doc,
    'header',
    { class: `lot-card ${klass}` },
    h(doc, 'div', { class: 'lot-emoji', 'aria-hidden': true }, item?.emoji ?? ''),
    h(
      doc,
      'div',
      { class: 'lot-main' },
      h(doc, 'p', { class: 'lot-count' }, STR.stage.lotOf(lot.lot, model.night.n_lots)),
      h(doc, 'h2', { class: 'lot-title' }, item?.name ?? STR.lotList.lot(lot.lot)),
      h(doc, 'p', { class: 'chips' }, chips),
      item?.blurb ? h(doc, 'p', { class: 'lot-blurb' }, item.blurb) : null,
    ),
  );
}

function section(doc, className, label, ...children) {
  return h(doc, 'section', { class: className, 'aria-label': label }, children);
}

/**
 * Create the stage inside `root`.
 * options.reduceMotion: jump envelopes open instead of swinging them.
 */
export function createStage(doc, root, model, options = {}) {
  const reduceMotion = Boolean(options.reduceMotion);
  const scripts = new Map();
  const ui = { glossary: { open: false, focus: null } };
  let current = null;

  function scriptFor(lotNo) {
    if (!scripts.has(lotNo)) {
      const lot = model.lot(lotNo);
      const anim = lot === null ? null : animationFor(lot.format);
      scripts.set(lotNo, lot === null ? null : buildScript(lot, anim.duration(lot), anim.hammerAt(lot)));
    }
    return scripts.get(lotNo);
  }

  /**
   * Build the DOM of a lot. `opening` says that this is the lots in their order (and not a
   * segment of the three-minute version), so that the first lot opens with the predictions of
   * the host.
   */
  function render(lotNo, lens, { opening = false } = {}) {
    clear(root);
    current = null;
    const lot = model.lot(lotNo);
    if (lot === null) {
      root.appendChild(h(doc, 'p', { class: 'empty' }, STR.stage.noLot));
      return;
    }
    const anim = animationFor(lot.format);
    const bidThoughts = lens
      ? model.reveal.thoughts.filter((t) => t.lot === lotNo && BID_PHASES.has(t.phase))
      : null;
    const ctx = { model, lens, reduceMotion, bidThoughts, ui };

    root.appendChild(lotCard(doc, lot, model));
    // The cap of the night is no secret of any seat, so this line is shown with the lens off too.
    if (lot.muted.length > 0) {
      const text = STR.stage.capNote(model.mutedFrom ?? lot.lot, lot.muted);
      root.appendChild(h(doc, 'p', { class: 'cap-note', role: 'status' }, text));
    }
    const note = h(
      doc,
      'p',
      { class: lens ? 'lens-note is-on' : 'lens-note is-off', role: 'status' },
      lens ? STR.lens.on : STR.lens.off,
    );
    root.appendChild(note);
    if (opening === true && lotNo === model.lotNos[0]) mountOpeningCard(doc, root, model);
    const bids = section(doc, 'bids', STR.bids.title);
    const view = anim.mount(doc, bids, lot, ctx);
    root.appendChild(bids);
    const overlay = mountOverlay(doc, root, lot, ctx);
    const seatsBox = section(doc, 'seats', STR.stage.seats);
    const seats = mountSeats(doc, seatsBox, lot, ctx);
    root.appendChild(seatsBox);
    const chatBox = section(doc, 'chat-box', STR.stage.chat, h(doc, 'h3', { class: 'section-title' }, STR.stage.chat));
    chatBox.appendChild(h(doc, 'p', { class: 'chat-note' }, STR.stage.chatNote));
    const chat = mountChat(doc, chatBox, lot, ctx);
    root.appendChild(chatBox);
    const hostEl =
      lens && lot.host
        ? h(
            doc,
            'section',
            { class: 'host is-pending', 'aria-label': STR.stage.host },
            h(doc, 'h3', { class: 'section-title' }, STR.stage.host),
            h(doc, 'p', { class: 'host-line' }, lot.host),
            h(doc, 'p', { class: 'host-note' }, STR.lens.hostNote),
          )
        : null;
    if (hostEl !== null) root.appendChild(hostEl);
    current = { lotNo, lens, lot, script: scriptFor(lotNo), anim, view, overlay, seats, chat, hostEl };
  }

  /** Put the stage into the state of time t (seconds into the lot). Returns the script state. */
  function update(t) {
    if (current === null) return null;
    const state = stateAt(current.script, t);
    const frame = current.anim.frameAt(current.lot, state.bidT ?? 0, {
      trueValue: model.reveal.true_values[current.lotNo] ?? null,
      lens: current.lens,
    });
    current.chat.update(state);
    current.seats.update(state, frame);
    current.view.update(frame);
    current.overlay?.update(frame);
    if (current.hostEl !== null) toggle(current.hostEl, 'is-pending', !state.hostShown);
    return state;
  }

  return {
    render,
    update,
    /** Seconds the lot lasts at normal speed; 0 for a lot that was not played. */
    durationOf: (lotNo) => scriptFor(lotNo)?.duration ?? 0,
    /** The script of a lot (lib/script.js), or null for a lot that was not played. */
    scriptOf: (lotNo) => scriptFor(lotNo),
  };
}
