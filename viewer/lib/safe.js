// Text hygiene for data that comes from a replay file.
//
// A replay is written by language models, and the viewer does not trust it. Text is cleaned
// and shortened here, once, when the replay is indexed (lib/replay.js). The DOM helpers in
// lib/dom.js then insert it only as text nodes, so even uncleaned text could not become
// markup. Cleaning is for readers: it keeps control characters, bidirectional overrides and
// walls of text from wrecking the page.

export const ELLIPSIS = '…';
export const MAX_SEATS = 8;
export const MAX_LOTS = 36;

// Display limits in characters (code points).
export const LIMITS = Object.freeze({
  name: 24, // code names and tags
  label: 48, // model ids, personas, item names
  line: 240, // one chat line
  thought: 400, // one inner monologue
  note: 600, // host line, item blurb
  block: 4000, // multi-line text
});

// Removed because a reader cannot see them or because they change how the text is drawn:
//   C0 controls except tab, line feed and carriage return, DEL, and the C1 controls;
//   the line and paragraph separators;
//   every format character (Unicode category Cf): zero-width space and non-joiner, the
//   left-to-right, right-to-left and Arabic letter marks, bidirectional embeddings, overrides
//   and isolates, the soft hyphen, the byte order mark, word joiners, tag characters and more;
//   lone surrogates (category Cs).
// The zero-width joiner (U+200D) stays: emoji sequences are glued together with it.
// The engine drops the categories Cc, Cf and Cs from model text before an event exists
// (src/auction_night/llm/sanitize.py). The viewer applies the same rule again to whatever a
// replay file holds, and it uses the Unicode data of the browser, so it removes at least as
// much as the engine does.
const HIDDEN = /[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F-\u009F\u2028\u2029\p{Cs}]|(?!\u200D)\p{Cf}/gu;
const BREAKS = /[\t\r\n]+/g;
const TABS = /\t/g;
const NEWLINES = /\r\n?/g;
const BLANK_LINES = /\n{3,}/g;

/** Strings and finite numbers become text. Everything else becomes the empty string. */
export function toText(value) {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  return '';
}

/**
 * Keep at most `max` code points. A longer text loses its end and gets an ellipsis, and the
 * result is `max` code points long including the ellipsis. `cut` says that the caller has
 * already dropped content after `text`, so an ellipsis is due even for a short remainder.
 */
export function truncate(text, max, cut = false) {
  if (!Number.isInteger(max) || max < 1) throw new RangeError('max must be a positive integer');
  const s = String(text);
  if (!cut && s.length <= max) return s;
  const kept = [];
  let more = cut;
  for (const ch of s) {
    if (kept.length === max) {
      more = true;
      break;
    }
    kept.push(ch);
  }
  if (!more) return s;
  return kept.slice(0, max - 1).join('') + ELLIPSIS;
}

/** One line of text: no control characters, line breaks become spaces, at most `max` characters. */
export function cleanLine(value, max = LIMITS.line) {
  const raw = toText(value);
  const head = raw.length > max * 8 ? raw.slice(0, max * 8) : raw;
  const text = head.replace(HIDDEN, '').replace(BREAKS, ' ').trim();
  return truncate(text, max, head.length < raw.length);
}

/** Several lines of text: line breaks are kept, runs of blank lines shrink to one. */
export function cleanBlock(value, max = LIMITS.block) {
  const raw = toText(value);
  const head = raw.length > max * 8 ? raw.slice(0, max * 8) : raw;
  const text = head
    .replace(HIDDEN, '')
    .replace(NEWLINES, '\n')
    .replace(TABS, ' ')
    .replace(BLANK_LINES, '\n\n')
    .trim();
  return truncate(text, max, head.length < raw.length);
}

/** A safe integer, or null. */
export function asInt(value) {
  return typeof value === 'number' && Number.isSafeInteger(value) ? value : null;
}

/** A non-negative safe integer, or null. */
export function asCoins(value) {
  const n = asInt(value);
  return n !== null && n >= 0 ? n : null;
}

/** A seat number from 1 to 8, or null. */
export function asSeat(value) {
  const n = asInt(value);
  return n !== null && n >= 1 && n <= MAX_SEATS ? n : null;
}

/** A lot number from 1 to 36, or null. */
export function asLotNo(value) {
  const n = asInt(value);
  return n !== null && n >= 1 && n <= MAX_LOTS ? n : null;
}

/** The integer in a JSON object key such as "3", or null. */
export function keyInt(key) {
  return typeof key === 'string' && /^[1-9][0-9]{0,15}$/.test(key) ? Number(key) : null;
}

export function clamp(x, lo, hi) {
  return Math.min(hi, Math.max(lo, x));
}
