// The plain picture of a lot whose format has no animation of its own.
//
// English lots have the price bar (english.js), Dutch lots the clock of ten cells (dutch.js), and
// the sealed formats share the envelopes (envelopes.js). A format that this viewer does not know
// yet, for instance one that a newer engine added, still plays: a short introduction, then the
// hammer exactly as the replay recorded it, then a rest. Like every animation here it is a pure
// function of the lot and a time, and it reads everything from the replay.

import { clamp } from '../lib/safe.js';
import { verdictOf } from './verdict.js';

export const TIMING = Object.freeze({ intro: 0.8, verdict: 2.2, tail: 0.6 });

/** Length of the picture in seconds at normal speed. */
export function duration() {
  return TIMING.intro + TIMING.verdict + TIMING.tail;
}

/** Seconds into the picture at which the hammer is shown. */
export function hammerAt() {
  return TIMING.intro;
}

/** {t, duration, phase, kind, verdict}. The phase is 'intro', 'verdict' or 'done'. */
export function frameAt(lot, t) {
  const total = duration();
  const at = clamp(Number.isFinite(t) ? t : 0, 0, total);
  let phase = 'done';
  if (at < TIMING.intro) phase = 'intro';
  else if (at < TIMING.intro + TIMING.verdict) phase = 'verdict';
  return {
    t: at,
    duration: total,
    phase,
    kind: lot.format,
    verdict: at >= TIMING.intro ? verdictOf(lot) : null,
  };
}
