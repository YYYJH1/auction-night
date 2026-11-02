// The script of one lot: what is on screen at each moment.
//
// A lot plays as a row of beats on one clock, in seconds at normal speed: the lot card, one
// beat per chat line, the bid animation, the host line, a short rest. `stateAt(script, t)`
// is a pure function of the script and the time, so scrubbing, pausing and deep links need
// no stored progress. The length of the bid animation comes from the animation itself.

import { clamp } from './safe.js';

export const BEATS = Object.freeze({
  card: 1.2, // the lot card on its own
  talkBase: 1.1, // reading time of a chat line: base plus a share per character
  talkPerChar: 0.045,
  talkMin: 1.5,
  talkMax: 4.2,
  thoughtDelay: 0.35, // an inner monologue appears this long after the line it belongs to
  host: 2.6,
  tail: 0.9,
});

/** Seconds a chat line stays the centre of attention. */
export function talkSeconds(text) {
  const length = Array.from(String(text)).length;
  return clamp(BEATS.talkBase + BEATS.talkPerChar * length, BEATS.talkMin, BEATS.talkMax);
}

/**
 * Lay out the beats of a lot. `bidSeconds` is the length of its bid animation, and `hammerOffset`
 * the second of that animation at which the hammer falls (animationFor(format).hammerAt(lot));
 * without it the script does not know when the hammer falls and `hammerAt` is null.
 * Returns {beats, duration, bidStart, bidSeconds, hostStart, hasHost, hammerAt}, where hammerAt
 * is the hammer on the clock of the lot.
 */
export function buildScript(lot, bidSeconds, hammerOffset = null) {
  const beats = [];
  let at = 0;
  const push = (kind, length, extra = {}) => {
    beats.push({ kind, start: at, end: at + length, ...extra });
    at += length;
  };
  push('card', BEATS.card);
  lot.talk.forEach((line, index) => push('talk', talkSeconds(line.text), { index, seat: line.seat }));
  const bidStart = at;
  push('bid', bidSeconds);
  const hostStart = at;
  push('host', lot.host ? BEATS.host : 0);
  push('tail', BEATS.tail);
  const hammerAt = Number.isFinite(hammerOffset) ? bidStart + clamp(hammerOffset, 0, bidSeconds) : null;
  return { beats, duration: at, bidStart, bidSeconds, hostStart, hasHost: Boolean(lot.host), hammerAt };
}

/** What is on screen at time t. */
export function stateAt(script, t) {
  const at = clamp(Number.isFinite(t) ? t : 0, 0, script.duration);
  const talks = script.beats
    .filter((beat) => beat.kind === 'talk')
    .map((beat) => ({
      index: beat.index,
      seat: beat.seat,
      shown: at >= beat.start,
      thought: at >= beat.start + BEATS.thoughtDelay,
      speaking: at >= beat.start && at < beat.end,
    }));
  const bidding = at >= script.bidStart;
  return {
    t: at,
    duration: script.duration,
    talks,
    speaker: talks.find((x) => x.speaking)?.seat ?? null,
    bidT: bidding ? Math.min(at - script.bidStart, script.bidSeconds) : null,
    hostShown: script.hasHost && at >= script.hostStart,
    done: at >= script.duration,
  };
}
