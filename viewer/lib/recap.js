// The three-minute version of a night: which highlights to play, in what order, for how long.
//
// The plan is a pure function of the replay, so the same replay always gives the same segments
// and the same running time. The rules, in order:
//
//   1. Candidates are the highlights whose lot was played. A highlight of the whole night, or of
//      a lot that was not played, has nothing to show and is left out.
//   2. Highlights of one lot are played as one segment, so a lot is never shown twice.
//   3. Segments are ranked by the best score of their highlights, higher first. Equal scores go
//      to the earlier lot. The ranking only decides who gets into the budget.
//   4. Going down the ranking, a segment is kept when it still fits in the budget of
//      RECAP.budget seconds (cards included, at normal speed). One that does not fit is cut
//      from the front to the room that is left, as long as that leaves RECAP.minSegment seconds
//      of its lot; if it cannot even be cut to that, it is skipped and the next one is tried.
//      So the version fills its budget to within one short segment when there is enough to show.
//   5. The kept segments play in the order of the night, so the story keeps its thread.
//
// A segment is a card of RECAP.card seconds with the category, the lot and the text of the
// highlight, followed by a window of the lot, on the clock of the lot (lib/script.js):
//   from  where the earliest highlight of the lot begins (momentStart in lib/highlights.js)
//   to    the end of the lot, so the hammer, the host line and the rest are always in
// A window shorter than RECAP.minSegment starts earlier (at the start of the lot at the
// soonest). A window longer than RECAP.maxSegment starts later, counted back from the end of the
// lot. The transition from one segment to the next is the card of the next one: the lot that
// follows appears frozen at its first moment under the card and starts to play when the card
// is over.
//
// The whole version is one clock from 0 to plan.length. recapAt(plan, t) says where it is.

import { momentStart, rankHighlights } from './highlights.js';
import { clamp } from './safe.js';

export const RECAP = Object.freeze({
  budget: 180, // seconds in all, about three minutes at normal speed
  card: 2, // the card that opens a segment
  minSegment: 14, // seconds of a lot shown at least (the whole lot when it is shorter)
  maxSegment: 32, // seconds of a lot shown at most
});

/** The highlights that can be played: their lot was played and has a script. */
function playable(model, scriptOf) {
  return model.highlights.filter(
    (light) => light.lot !== null && model.lot(light.lot) !== null && scriptOf(light.lot) !== null,
  );
}

function segmentOf(model, scriptOf, lotNo, lights) {
  const lot = model.lot(lotNo);
  const script = scriptOf(lotNo);
  const ranked = rankHighlights(lights);
  const end = script.duration;
  let from = Math.min(...ranked.map((light) => momentStart(lot, script, light)));
  if (end - from < RECAP.minSegment) from = Math.max(0, end - RECAP.minSegment);
  if (end - from > RECAP.maxSegment) from = end - RECAP.maxSegment;
  return {
    lot: lotNo,
    from,
    to: end,
    play: end - from,
    card: RECAP.card,
    length: RECAP.card + (end - from),
    ids: ranked.map((light) => light.id),
    score: ranked[0].score,
  };
}

/**
 * The plan: {segments, length}. Each segment is {index (from 1), lot, from, to, play, card,
 * length, ids (its highlights, best first), score (the best of them), start, end} with start
 * and end on the clock of the whole version. `scriptOf(lot)` gives the script of a lot, or null.
 * The budget is RECAP.budget seconds; a test may ask for another.
 */
export function planRecap(model, scriptOf, { budget = RECAP.budget } = {}) {
  const byLot = new Map();
  for (const light of playable(model, scriptOf)) {
    if (!byLot.has(light.lot)) byLot.set(light.lot, []);
    byLot.get(light.lot).push(light);
  }
  const ranked = [...byLot.entries()]
    .map(([lot, lights]) => segmentOf(model, scriptOf, lot, lights))
    .sort((a, b) => b.score - a.score || a.lot - b.lot);
  let used = 0;
  const kept = [];
  for (const segment of ranked) {
    const room = budget - used;
    if (segment.length <= room) {
      kept.push(segment);
      used += segment.length;
    } else if (room - RECAP.card >= RECAP.minSegment) {
      // Cut from the front: the hammer and what follows it are what the segment is for.
      const play = room - RECAP.card;
      kept.push({ ...segment, from: segment.to - play, play, length: room });
      used += room;
    }
  }
  kept.sort((a, b) => a.lot - b.lot);
  let at = 0;
  const segments = kept.map((segment, i) => {
    const placed = { ...segment, index: i + 1, start: at, end: at + segment.length };
    at = placed.end;
    return placed;
  });
  return { segments, length: at };
}

/**
 * Where the version is at time t: {t, segment, index, local, phase, lotT, done}.
 *   phase  'card' while the card of a segment is up, then 'play'
 *   lotT   the time on the clock of that segment's lot: its `from` during the card, then moving
 *   local  seconds since the segment began
 * A plan with no segments gives segment null.
 */
export function recapAt(plan, t) {
  const at = clamp(Number.isFinite(t) ? t : 0, 0, plan.length);
  if (plan.segments.length === 0) {
    return { t: at, segment: null, index: 0, local: 0, phase: 'done', lotT: 0, done: true };
  }
  const segment = plan.segments.find((s) => at < s.end) ?? plan.segments.at(-1);
  const local = at - segment.start;
  const card = local < segment.card;
  return {
    t: at,
    segment,
    index: segment.index,
    local,
    phase: card ? 'card' : 'play',
    lotT: segment.from + Math.max(0, local - segment.card),
    done: at >= plan.length,
  };
}

/** The start of the segment before or after the one at time t, or null at the ends. */
export function segmentStart(plan, t, direction) {
  const here = recapAt(plan, t);
  if (here.segment === null) return null;
  if (direction > 0) return plan.segments[here.index]?.start ?? null;
  // Going back: a few seconds into a segment, go to its own start first.
  if (here.local > 1) return here.segment.start;
  return plan.segments[here.index - 2]?.start ?? null;
}
