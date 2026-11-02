// Names and small formatting helpers shared by the viewer.
//
// FORMAT_TITLES must agree with the Chinese titles of the mechanism definitions. A test in
// the Python suite compares the two, so edit the table and the definitions together.

export const FORMAT_KEYS = Object.freeze([
  'english',
  'dutch',
  'sealed_first',
  'sealed_second',
  'all_pay',
  'blind_box',
]);

export const FORMAT_TITLES = Object.freeze({
  english: '英式加价',
  dutch: '荷式降价',
  sealed_first: '密封一价',
  sealed_second: '密封二价',
  all_pay: '全员付费',
  blind_box: '盲盒',
});

// How the price is set. English and Dutch lots follow their clocks; see the bid animations.
export const PRICING = Object.freeze({
  english: 'second',
  dutch: 'first',
  sealed_first: 'first',
  sealed_second: 'second',
  all_pay: 'all_pay',
  blind_box: 'second',
});

export const SEALED_FORMATS = Object.freeze(['sealed_first', 'sealed_second', 'all_pay', 'blind_box']);

export function isFormat(key) {
  return typeof key === 'string' && FORMAT_KEYS.includes(key);
}

export function isSealedFormat(key) {
  return typeof key === 'string' && SEALED_FORMATS.includes(key);
}

/** A value of the format enum that this viewer does not know still gets a neutral name. */
export function formatTitle(key) {
  return isFormat(key) ? FORMAT_TITLES[key] : '未知规则';
}

/** Class-name suffix for a format: only known keys, so replay text never reaches a class. */
export function formatClass(key) {
  return isFormat(key) ? key.replace('_', '-') : 'unknown';
}

/** Priority 0 to 3 as stars. */
export function stars(n) {
  if (!Number.isInteger(n) || n < 0 || n > 3) return '';
  return '★'.repeat(n) + '☆'.repeat(3 - n);
}

const MINUS = '\u2212'; // a real minus sign lines up with the plus sign in a column of figures

/** A difference with its sign: +14, a minus sign and 14, or 0. Anything but an integer gives an empty text. */
export function signed(n) {
  if (!Number.isSafeInteger(n)) return '';
  if (n > 0) return `+${n}`;
  if (n < 0) return `${MINUS}${-n}`;
  return '0';
}

/**
 * Thousandths as a decimal: 920 is "0.92" with two decimals at least, 71295 is "71.295", and
 * with no minimum 75000 is "75". Integer arithmetic only, so nothing is rounded.
 */
export function milliText(n, minDecimals = 0) {
  if (!Number.isSafeInteger(n) || n < 0) return '';
  const whole = Math.floor(n / 1000);
  let fraction = String(n % 1000).padStart(3, '0');
  while (fraction.length > minDecimals && fraction.endsWith('0')) fraction = fraction.slice(0, -1);
  return fraction === '' ? String(whole) : `${whole}.${fraction}`;
}

/** A whole number as it is, with a real minus sign when it is below zero. Anything else gives an empty text. */
export function intText(n) {
  if (!Number.isSafeInteger(n)) return '';
  return n < 0 ? `${MINUS}${-n}` : String(n);
}

/**
 * Thousandths as a percentage, written exactly: 305 is "30.5", 1510 is "151", 0 is "0", and a
 * negative number gets a real minus sign. A thousandth is a tenth of a percent, so one decimal
 * is always enough and nothing is rounded. Anything but a whole number gives an empty text.
 */
export function percentFromMilli(n) {
  if (!Number.isSafeInteger(n)) return '';
  const size = Math.abs(n);
  const whole = Math.floor(size / 10);
  const tenth = size % 10;
  const text = tenth === 0 ? String(whole) : `${whole}.${tenth}`;
  return n < 0 ? `${MINUS}${text}` : text;
}

/** "m:ss" for a time in seconds. */
export function clock(seconds) {
  const s = Math.max(0, Math.round(Number.isFinite(seconds) ? seconds : 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

/**
 * The small print under a seat's code name: the model id, how it was run, the bot policy.
 * Model ids appear here and nowhere else. Chat and bids show code names only.
 */
export function seatInfo(seat, configured = (model) => ` (${model})`) {
  const parts = [];
  if ((seat.kind === 'bot' || seat.kind === 'mock') && seat.policy) parts.push(seat.policy);
  const used = seat.model_returned ?? seat.model;
  if (used) {
    const differs = seat.model && seat.model_returned && seat.model !== seat.model_returned;
    parts.push(differs ? `${used}${configured(seat.model)}` : used);
  }
  const mode = [seat.thinking, seat.effort].filter(Boolean).join('/');
  if (mode) parts.push(mode);
  if (seat.persona) parts.push(seat.persona);
  return parts.join(' \u00B7 ');
}
