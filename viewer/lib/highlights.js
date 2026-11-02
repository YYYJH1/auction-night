// The highlights: moments of a night that are worth a jump.
//
// The show layer picks them and gives each a fun score from 0 to 10, a category, the lot it
// happened in, the seats involved and the range of events it covers. The score only says how
// good the moment is to watch.
//
// Like the awards, a highlight can rest on something the players kept private: a secret
// mission, a plan, a private value, a guess, a claim about their own cards. While the hole-card
// lens is off, such a highlight keeps its category, its score and its lot, and loses its text
// and its seats. highlightsFor returns only what the reader may see. What a highlight rests on is
// read from the replay (its `basis`); the table below is for a replay that does not say.

import { STR } from './strings.js';

/** The categories of a highlight. */
export const CATEGORY_KEYS = Object.freeze([
  'winners_curse',
  'shill_stuck',
  'backstab',
  'claim_vs_hand',
  'plan_contradiction',
  'all_pay_tilt',
  'bargain',
  'mindread_miss',
]);

// For a replay that does not carry a basis; it must say what the exporter says.
const BASIS = Object.freeze({
  winners_curse: 'public', // the price against the true value that the opened box shows
  shill_stuck: 'private', // a secret mission
  backstab: 'public', // a public pledge against a revealed bid
  claim_vs_hand: 'private', // a claim against the claimant's own cards
  plan_contradiction: 'private', // a private plan
  all_pay_tilt: 'public', // revealed bids and payments
  bargain: 'private', // a private value against the price
  mindread_miss: 'private', // a private guess against a private budget
});

export function isCategory(key) {
  return typeof key === 'string' && CATEGORY_KEYS.includes(key);
}

/**
 * 'public' or 'private'. The basis that the replay gives wins. Without one, the table decides,
 * and a category this viewer does not know counts as private.
 */
export function categoryBasis(key, basis = null) {
  if (basis === 'public' || basis === 'private') return basis;
  return isCategory(key) ? BASIS[key] : 'private';
}

export function categoryLabel(key) {
  return isCategory(key) ? STR.highlights.categories[key] : STR.highlights.unknownCategory;
}

const lotKey = (item) => item.lot ?? Number.MAX_SAFE_INTEGER; // night-level highlights go last

/** The order of the list: the higher score first, then the earlier lot, then the lower id. */
export function rankHighlights(list) {
  return [...list].sort((a, b) => b.score - a.score || lotKey(a) - lotKey(b) || a.id - b.id);
}

/**
 * Everything the list of highlights may show, in list order.
 *   lens    the switch it was made for
 *   items   [{id, category, basis, known, label, score, lot, lotName, jumpable, held, seats, text}]
 *           basis is what the highlight rests on, 'public' or 'private' (the replay's word first);
 *           lotName is the catalog name of the lot or null, jumpable says that the lot was
 *           played (so there is something to jump to), held says that the lens keeps the
 *           details back: then seats is empty and text is null
 *   held    how many items the lens holds back
 *   categories  the categories that occur, in the order of CATEGORY_KEYS (others last)
 */
export function highlightsFor(model, { lens = false } = {}) {
  const on = lens === true;
  const items = rankHighlights(model.highlights).map((light) => {
    const basis = categoryBasis(light.category, light.basis);
    const held = basis === 'private' && !on;
    return {
      id: light.id,
      category: light.category,
      basis,
      known: isCategory(light.category),
      label: categoryLabel(light.category),
      score: light.score,
      lot: light.lot,
      lotName: light.lot === null ? null : (model.catalog(light.lot)?.name ?? null),
      jumpable: light.lot !== null && model.lot(light.lot) !== null,
      held,
      seats: held ? [] : [...light.seats],
      text: held || light.text === '' ? null : light.text,
    };
  });
  const present = new Set(items.map((item) => item.category));
  const categories = [
    ...CATEGORY_KEYS.filter((key) => present.has(key)),
    ...[...present].filter((key) => !isCategory(key)),
  ];
  return { lens: on, items, held: items.filter((item) => item.held).length, categories };
}

const BIDDING = new Set(['english_init', 'english_revise', 'dutch', 'sealed']);

/**
 * The talk beat that a seq belongs to, found with the table of event numbers of the lines
 * (lot.talk_seqs): the beat whose line has the greatest number that is not above `seq`. An event
 * that follows a line, such as the reading that the same seat made in the same turn, belongs to
 * that line. Null when the table says nothing that fits.
 */
function talkAt(lot, talks, seq) {
  let best = null;
  for (const beat of talks) {
    const number = lot.talk_seqs?.[beat.seat];
    if (!Number.isInteger(number) || number > seq) continue;
    if (best === null || number > best.number) best = { number, beat };
  }
  return best === null ? null : best.beat;
}

/**
 * Where in its lot a highlight begins, in seconds on the clock of the lot (lib/script.js).
 * The range of events of the highlight decides, and the lot's own event numbers make it exact
 * when the replay carries them (`talk_seqs`, the event number of each seat's line, and
 * `hammer_seq`, the event number of the hammer):
 *   it opens with the lot (no later than the lot's first event)       ->  the start of the lot;
 *   it opens at the hammer or after it (hammer_seq known)             ->  the moment the hammer
 *     falls in the bid animation (script.hammerAt; the start of the animation when the script
 *     does not know it);
 *   it opens before the first bid of the lot, so in the talk          ->  the line that its first
 *     event belongs to (talk_seqs known), else the first line of a seat it names, else the first
 *     line of the lot;
 *   it opens later, in the bidding                                    ->  the start of the bid animation.
 * Without talk_seqs and hammer_seq the last two rules are the approximation that older replays
 * get. `lot` is an indexed lot of the model and `script` its script (buildScript).
 */
export function momentStart(lot, script, light) {
  if (!(light.first_seq > lot.first_seq)) return 0;
  if (Number.isInteger(lot.hammer_seq) && light.first_seq >= lot.hammer_seq) {
    return Number.isFinite(script.hammerAt) ? script.hammerAt : script.bidStart;
  }
  const firstBid = Math.min(...lot.actions.filter((a) => BIDDING.has(a.phase)).map((a) => a.seq));
  const talks = script.beats.filter((beat) => beat.kind === 'talk');
  if (light.first_seq < firstBid && talks.length > 0) {
    const exact = talkAt(lot, talks, light.first_seq);
    if (exact !== null) return exact.start;
    return (talks.find((beat) => light.seats.includes(beat.seat)) ?? talks[0]).start;
  }
  return script.bidStart;
}
