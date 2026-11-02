// Playback and navigation as a pure reducer.
//
// The page keeps one state object: which lot, how far into it, playing or not, the speed and
// the lens. `reduce(state, action, ctx)` returns the next state and a list of effects for
// the caller to carry out (changing the address). Nothing here touches the page, so the
// rules of playback, deep links, back and forward are all tested without a browser.
//
// ctx: {nightId, lotNos, durationOf(lot), momentOf(id), reducedMotion}
//   lotNos     the lots that were played, ascending
//   momentOf   optional: {lot, t} where the highlight with that id begins, or null. A highlight is
//              a moment of a lot, and the address #/n/<night>/moment/<id> goes to it. A click on a
//              highlight is a link, so it reaches the reducer as a change of address ('hash').
//              Going to a moment plays from there, unless reducedMotion is set

import { formatHash, parseHash } from './route.js';
import { clamp } from './safe.js';

export const SPEEDS = Object.freeze([0.5, 1, 2, 4]);
const MAX_STEP = 0.25; // seconds of playback per frame at most, so a sleeping tab does not skip

/**
 * First state. A paused start (reduced motion) shows the lot fully revealed, a playing start
 * begins the lot from its first beat, or from `at` seconds when a moment is asked for.
 */
export function initialState(ctx, { lot = null, at = 0, reducedMotion = false, lens = true } = {}) {
  const first = ctx.lotNos.includes(lot) ? lot : ctx.lotNos[0];
  const playing = !reducedMotion;
  const length = ctx.durationOf(first);
  return { lot: first, t: playing ? clamp(Number.isFinite(at) ? at : 0, 0, length) : length, playing, speed: 1, lens };
}

/** Go to a lot at a time. Paused, a lot is shown complete, so a shared link shows everything. */
function enter(state, lot, ctx, at = 0) {
  const length = ctx.durationOf(lot);
  return { ...state, lot, t: state.playing ? clamp(at, 0, length) : length };
}

function hashFor(ctx, lot) {
  return formatHash(ctx.nightId, lot);
}

/** The lot and time of a highlight, or null when the page does not know it. */
function momentTarget(ctx, id) {
  const target = ctx.momentOf?.(id) ?? null;
  if (target === null || !ctx.lotNos.includes(target.lot)) return null;
  return { lot: target.lot, at: Number.isFinite(target.t) ? target.t : 0 };
}

function step(ctx, lot, direction) {
  const at = ctx.lotNos.indexOf(lot);
  return ctx.lotNos[at + direction] ?? null;
}

/** Apply one action. Returns {state, effects}; effects are {kind: 'push'|'replace', hash}. */
export function reduce(state, action, ctx) {
  const none = (next) => ({ state: next, effects: [] });
  switch (action.type) {
    case 'goto': {
      if (!ctx.lotNos.includes(action.lot) || action.lot === state.lot) return none(state);
      return {
        state: enter(state, action.lot, ctx),
        effects: [{ kind: 'push', hash: hashFor(ctx, action.lot) }],
      };
    }
    case 'next':
    case 'prev': {
      const lot = step(ctx, state.lot, action.type === 'next' ? 1 : -1);
      return lot === null ? none(state) : reduce(state, { type: 'goto', lot }, ctx);
    }
    case 'hash': {
      // The address changed on its own (back, forward, a pasted link): follow it, change nothing.
      const parsed = parseHash(action.hash);
      if (parsed === null) return none(state);
      if (parsed.moment !== null) {
        // A moment is for watching: it plays from there, unless motion is reduced.
        const target = momentTarget(ctx, parsed.moment);
        if (target === null) return none(state);
        return none(enter({ ...state, playing: !ctx.reducedMotion }, target.lot, ctx, target.at));
      }
      if (parsed.lot === null) return none(state);
      if (!ctx.lotNos.includes(parsed.lot) || parsed.lot === state.lot) return none(state);
      return none(enter(state, parsed.lot, ctx));
    }
    case 'play': {
      if (state.playing) return none(state);
      const length = ctx.durationOf(state.lot);
      if (state.t < length) return none({ ...state, playing: true });
      const following = step(ctx, state.lot, 1);
      if (following === null) return none({ ...state, playing: true, t: 0 });
      return {
        state: { ...state, playing: true, lot: following, t: 0 },
        effects: [{ kind: 'push', hash: hashFor(ctx, following) }],
      };
    }
    case 'pause':
      return none(state.playing ? { ...state, playing: false } : state);
    case 'toggle':
      return reduce(state, { type: state.playing ? 'pause' : 'play' }, ctx);
    case 'speed':
      return none(SPEEDS.includes(action.value) ? { ...state, speed: action.value } : state);
    case 'lens':
      return none({ ...state, lens: Boolean(action.value) });
    case 'seek': {
      const length = ctx.durationOf(state.lot);
      return none({ ...state, t: clamp(Number.isFinite(action.t) ? action.t : 0, 0, length) });
    }
    case 'tick': {
      if (!state.playing || !(action.dt > 0)) return none(state);
      const t = state.t + Math.min(action.dt, MAX_STEP) * state.speed;
      const length = ctx.durationOf(state.lot);
      if (t < length) return none({ ...state, t });
      const following = step(ctx, state.lot, 1);
      if (following === null) return none({ ...state, t: length, playing: false });
      // Moving on by itself must not fill the history with one entry per lot.
      return {
        state: { ...state, lot: following, t: 0 },
        effects: [{ kind: 'replace', hash: hashFor(ctx, following) }],
      };
    }
    default:
      return none(state);
  }
}
