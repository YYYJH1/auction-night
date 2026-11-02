// Which animation plays which format. Each entry offers the same four things:
//   duration(lot)            length in seconds at normal speed
//   hammerAt(lot)            seconds into the animation at which the hammer falls
//   frameAt(lot, t, ctx)     the picture at time t, a plain object (pure)
//   mount(doc, parent, lot, ctx)   build the DOM once, then view.update(frame)
// The sealed formats share the envelopes, English has its price bar and Dutch its clock. A
// format this viewer does not know gets the plain list of what happened.

import { isSealedFormat } from '../lib/format.js';
import * as dutch from './dutch.js';
import { mountDutch } from './dutch_view.js';
import * as english from './english.js';
import { mountEnglish } from './english_view.js';
import * as envelopes from './envelopes.js';
import { mountEnvelopes } from './envelopes_view.js';
import * as summary from './summary.js';
import { mountSummary } from './summary_view.js';

const ENVELOPES = Object.freeze({
  id: 'envelopes',
  duration: envelopes.duration,
  hammerAt: envelopes.hammerAt,
  frameAt: envelopes.frameAt,
  mount: mountEnvelopes,
});

const ENGLISH = Object.freeze({
  id: 'english',
  duration: english.duration,
  hammerAt: english.hammerAt,
  frameAt: english.frameAt,
  mount: mountEnglish,
});

const DUTCH = Object.freeze({
  id: 'dutch',
  duration: dutch.duration,
  hammerAt: dutch.hammerAt,
  frameAt: dutch.frameAt,
  mount: mountDutch,
});

const SUMMARY = Object.freeze({
  id: 'summary',
  duration: summary.duration,
  hammerAt: summary.hammerAt,
  frameAt: summary.frameAt,
  mount: mountSummary,
});

export function animationFor(format) {
  if (isSealedFormat(format)) return ENVELOPES;
  if (format === 'english') return ENGLISH;
  if (format === 'dutch') return DUTCH;
  return SUMMARY;
}
