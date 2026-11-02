// The host's review of a night: the recap it wrote afterwards, and the predictions it made
// before the first lot, with what the night made of them.
//
// Both belong to the end of the night. The recap is written from the fact cards, which hold
// numbers that the players never saw, so it follows the same rule as the host's line under each
// lot: it needs the hole-card lens. The predictions are different at the two ends of the night.
// Before the show they are plain sentences, one for each kind of rule, and nobody has seen how
// they turned out. Settling them compares bids with the textbook bid and with the private values,
// so the result of each (hit, miss, or nothing to compare) is held back while the lens is off,
// and so is the tally. This module decides nothing: the results are the ones in the replay.

import { formatTitle } from './format.js';

/** The recap as paragraphs: one for each line of the text, blank lines only separate. */
export function paragraphsOf(text) {
  if (typeof text !== 'string') return [];
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line !== '');
}

const resultOf = (hit) => (hit === true ? 'hit' : hit === false ? 'miss' : 'open');

/**
 * The host's predictions, or null when the replay has none.
 *   lens    the switch it was made for
 *   held    true without the lens: the results are not given
 *   items   [{format, formatTitle, text, result}] result is 'hit', 'miss' or 'open' (nothing to
 *           compare), and null while the lens is off
 *   tally   {hit, miss, open} counted from the items, or null while the lens is off
 */
export function predictionsFor(model, { lens = false } = {}) {
  const card = model.predictions;
  if (card === null) return null;
  const on = lens === true;
  const items = card.items.map((item) => ({
    format: item.format,
    formatTitle: formatTitle(item.format),
    text: item.text,
    result: on ? resultOf(item.hit) : null,
  }));
  const count = (result) => items.filter((item) => item.result === result).length;
  return {
    lens: on,
    held: !on,
    items,
    tally: on ? { hit: count('hit'), miss: count('miss'), open: count('open') } : null,
  };
}

/**
 * Everything the page of the review may show.
 *   recap        {present, held, paragraphs}  present says the replay has a recap; held that the
 *                lens keeps it back (and then paragraphs is empty)
 *   predictions  as predictionsFor, or null
 *   nothing      true when the replay has neither
 */
export function reviewFor(model, { lens = false } = {}) {
  const on = lens === true;
  const paragraphs = paragraphsOf(model.recap);
  const predictions = predictionsFor(model, { lens: on });
  return {
    lens: on,
    recap: { present: paragraphs.length > 0, held: paragraphs.length > 0 && !on, paragraphs: on ? paragraphs : [] },
    predictions,
    nothing: paragraphs.length === 0 && predictions === null,
  };
}
