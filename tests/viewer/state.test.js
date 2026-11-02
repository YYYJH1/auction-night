import assert from 'node:assert/strict';
import test from 'node:test';

import { formatHash, formatMomentHash } from '../../viewer/lib/route.js';
import { initialState, reduce, SPEEDS } from '../../viewer/lib/state.js';

const ctx = { nightId: 'night-1', lotNos: [1, 2, 3, 4], durationOf: (lot) => 20 + lot };
const hash = (lot) => formatHash('night-1', lot);
const apply = (state, action, c = ctx) => reduce(state, action, c);
const start = (options) => initialState(ctx, options);

test('a night starts at lot 1, playing at normal speed, with the lens on', () => {
  assert.deepEqual(start(), { lot: 1, t: 0, playing: true, speed: 1, lens: true });
});

test('a deep link chooses the first lot, an unknown lot falls back to the first played lot', () => {
  assert.equal(start({ lot: 3 }).lot, 3);
  assert.equal(start({ lot: 9 }).lot, 1);
  assert.equal(start({ lot: null }).lot, 1);
  assert.equal(initialState({ ...ctx, lotNos: [2, 5] }, { lot: 4 }).lot, 2);
});

test('with reduced motion the night starts paused with the lot fully shown', () => {
  const state = start({ reducedMotion: true });
  assert.equal(state.playing, false);
  assert.equal(state.t, 21);
});

test('going to a lot changes the address with one new history entry', () => {
  const { state, effects } = apply(start(), { type: 'goto', lot: 3 });
  assert.equal(state.lot, 3);
  assert.equal(state.t, 0, 'a playing lot starts from its first beat');
  assert.deepEqual(effects, [{ kind: 'push', hash: hash(3) }]);
});

test('going to the same lot, an unplayed lot or a nonsense lot does nothing', () => {
  const s = start();
  for (const lot of [1, 9, 0, -1, 2.5, '2', null, undefined]) {
    const result = apply(s, { type: 'goto', lot });
    assert.equal(result.state, s, String(lot));
    assert.deepEqual(result.effects, []);
  }
});

test('while paused a lot is shown complete, so a shared link shows everything', () => {
  const paused = { ...start(), playing: false, t: 3 };
  const { state } = apply(paused, { type: 'goto', lot: 2 });
  assert.equal(state.t, 22);
  assert.equal(state.playing, false);
});

test('next and previous stop at the ends', () => {
  const first = start();
  assert.equal(apply(first, { type: 'prev' }).state, first);
  const last = { ...start(), lot: 4 };
  assert.equal(apply(last, { type: 'next' }).state, last);
  assert.equal(apply(first, { type: 'next' }).state.lot, 2);
  assert.equal(apply(last, { type: 'prev' }).state.lot, 3);
  assert.deepEqual(apply(first, { type: 'next' }).effects, [{ kind: 'push', hash: hash(2) }]);
});

test('lots that were not played are skipped', () => {
  const gappy = { ...ctx, lotNos: [1, 3, 4] };
  const s = initialState(gappy);
  assert.equal(reduce(s, { type: 'next' }, gappy).state.lot, 3);
  assert.equal(reduce(s, { type: 'goto', lot: 2 }, gappy).state, s);
});

test('a change of address follows it and writes nothing back', () => {
  const { state, effects } = apply(start(), { type: 'hash', hash: hash(3) });
  assert.equal(state.lot, 3);
  assert.deepEqual(effects, []);
});

test('addresses that are not ours, or name no played lot or no known moment, are ignored', () => {
  const s = { ...start(), lot: 2 };
  for (const bad of ['', '#', '#/x', '#/n/night-1', '#/n/night-1/lot/9', '#/n/night-1/lot/0', '#/n/night-1/lot/2', '#/n/night-1/moment/3', null]) {
    const result = apply(s, { type: 'hash', hash: bad });
    assert.equal(result.state, s, String(bad));
    assert.deepEqual(result.effects, []);
  }
});

// A moment is a lot at a time: the page knows where each highlight begins.
const moments = { 3: { lot: 2, t: 7.5 }, 5: { lot: 4, t: 0 }, 6: { lot: 3, t: 1e9 }, 7: { lot: 9, t: 1 }, 8: { lot: 1, t: Number.NaN } };
const withMoments = { ...ctx, momentOf: (id) => moments[id] ?? null, reducedMotion: false };

test('an address of a moment goes to its lot at the time where it begins, and plays from there', () => {
  const result = apply(start(), { type: 'hash', hash: formatMomentHash('night-1', 3) }, withMoments);
  assert.equal(result.state.lot, 2);
  assert.equal(result.state.t, 7.5);
  assert.equal(result.state.playing, true);
  assert.deepEqual(result.effects, [], 'the address already changed, nothing is written back');
});

test('a moment plays even when the night was paused, unless motion is reduced', () => {
  const paused = { ...start(), playing: false, t: 12 };
  const plays = apply(paused, { type: 'hash', hash: formatMomentHash('night-1', 3) }, withMoments).state;
  assert.equal(plays.playing, true);
  assert.equal(plays.t, 7.5);
  const calm = apply(paused, { type: 'hash', hash: formatMomentHash('night-1', 3) }, { ...withMoments, reducedMotion: true }).state;
  assert.equal(calm.playing, false);
  assert.equal(calm.t, 22, 'shown complete, like every lot while paused');
});

test('a moment in the lot that is already on show still seeks to it, and the time stays inside the lot', () => {
  const inside = { ...start(), lot: 2, t: 15 };
  assert.equal(apply(inside, { type: 'hash', hash: formatMomentHash('night-1', 3) }, withMoments).state.t, 7.5);
  assert.equal(apply(start(), { type: 'hash', hash: formatMomentHash('night-1', 6) }, withMoments).state.t, 23, 'clamped to the end of lot 3');
  assert.equal(apply(start(), { type: 'hash', hash: formatMomentHash('night-1', 8) }, withMoments).state.t, 0, 'a time that is not a number is the start');
});

test('a moment that is unknown, or sits in a lot that was not played, changes nothing', () => {
  const s = { ...start(), lot: 2, t: 4 };
  for (const id of [4, 7, 99]) {
    const result = apply(s, { type: 'hash', hash: formatMomentHash('night-1', id) }, withMoments);
    assert.equal(result.state, s, String(id));
    assert.deepEqual(result.effects, []);
  }
  assert.equal(apply(s, { type: 'hash', hash: formatMomentHash('night-1', 3) }).state, s, 'a page with no moments ignores them');
});

test('a moment of another night still moves to the lot it names, and a moment that opens the night starts there', () => {
  assert.equal(apply(start(), { type: 'hash', hash: formatMomentHash('someone-else', 5) }, withMoments).state.lot, 4);
  const first = initialState(withMoments, { lot: 2, at: 7.5 });
  assert.equal(first.lot, 2);
  assert.equal(first.t, 7.5);
  assert.equal(initialState(withMoments, { lot: 2, at: 1e9 }).t, 22, 'clamped');
  assert.equal(initialState(withMoments, { lot: 2, at: Number.NaN }).t, 0);
  assert.equal(initialState(withMoments, { lot: 2, at: 7.5, reducedMotion: true }).t, 22, 'paused: complete');
});

test('an address of another night still moves to the lot it names', () => {
  assert.equal(apply(start(), { type: 'hash', hash: formatHash('someone-else', 4) }).state.lot, 4);
});

test('back and forward through a simulated history', () => {
  // A tiny browser: entries, a cursor, pushState, replaceState and traversal.
  const history = { entries: [hash(1)], at: 0 };
  let state = start();
  const run = (action) => {
    const result = apply(state, action);
    state = result.state;
    for (const effect of result.effects) {
      if (effect.kind === 'push') {
        history.entries.splice(history.at + 1, Infinity, effect.hash);
        history.at += 1;
      } else history.entries[history.at] = effect.hash;
    }
  };
  const go = (step) => {
    history.at += step;
    run({ type: 'hash', hash: history.entries[history.at] });
  };
  run({ type: 'goto', lot: 3 });
  run({ type: 'next' });
  assert.equal(state.lot, 4);
  go(-1);
  assert.equal(state.lot, 3);
  go(-1);
  assert.equal(state.lot, 1);
  go(1);
  assert.equal(state.lot, 3);
  run({ type: 'goto', lot: 2 });
  assert.equal(history.entries.length, 3, 'a new entry cut off the forward entries');
  go(1 - 1);
  assert.equal(state.lot, 2);
});

test('play, pause and toggle', () => {
  const playing = start();
  const paused = apply(playing, { type: 'pause' }).state;
  assert.equal(paused.playing, false);
  assert.equal(apply(paused, { type: 'pause' }).state, paused);
  assert.equal(apply(paused, { type: 'play' }).state.playing, true);
  assert.equal(apply(playing, { type: 'play' }).state, playing);
  assert.equal(apply(playing, { type: 'toggle' }).state.playing, false);
  assert.equal(apply(paused, { type: 'toggle' }).state.playing, true);
});

test('play at the end of a lot moves on, and at the end of the night starts over', () => {
  const ended = { ...start(), playing: false, t: 21 };
  const next = apply(ended, { type: 'play' });
  assert.equal(next.state.lot, 2);
  assert.equal(next.state.t, 0);
  assert.equal(next.state.playing, true);
  assert.deepEqual(next.effects, [{ kind: 'push', hash: hash(2) }]);
  const over = { ...start(), lot: 4, playing: false, t: 24 };
  const again = apply(over, { type: 'play' });
  assert.equal(again.state.lot, 4);
  assert.equal(again.state.t, 0);
  assert.deepEqual(again.effects, []);
});

test('only the four speeds exist', () => {
  assert.deepEqual([...SPEEDS], [0.5, 1, 2, 4]);
  for (const value of SPEEDS) assert.equal(apply(start(), { type: 'speed', value }).state.speed, value);
  for (const value of [0, 3, -1, 8, '2', null, undefined, Number.NaN]) {
    const s = start();
    assert.equal(apply(s, { type: 'speed', value }).state, s, String(value));
  }
});

test('time advances by the tick times the speed', () => {
  let state = start();
  state = apply(state, { type: 'tick', dt: 0.1 }).state;
  assert.ok(Math.abs(state.t - 0.1) < 1e-9);
  state = apply({ ...state, speed: 4 }, { type: 'tick', dt: 0.1 }).state;
  assert.ok(Math.abs(state.t - 0.5) < 1e-9);
  state = apply({ ...state, speed: 0.5 }, { type: 'tick', dt: 0.1 }).state;
  assert.ok(Math.abs(state.t - 0.55) < 1e-9);
});

test('a long gap between frames cannot skip a lot', () => {
  const state = apply(start(), { type: 'tick', dt: 60 }).state;
  assert.equal(state.t, 0.25);
});

test('ticks while paused, or with no time, change nothing', () => {
  const paused = { ...start(), playing: false };
  assert.equal(apply(paused, { type: 'tick', dt: 1 }).state, paused);
  const s = start();
  for (const dt of [0, -1, Number.NaN, undefined, 'x']) assert.equal(apply(s, { type: 'tick', dt }).state, s, String(dt));
});

test('at the end of a lot playback moves on by itself and replaces the history entry', () => {
  const near = { ...start(), t: 20.9 };
  const { state, effects } = apply(near, { type: 'tick', dt: 0.2 });
  assert.equal(state.lot, 2);
  assert.equal(state.t, 0);
  assert.equal(state.playing, true);
  assert.deepEqual(effects, [{ kind: 'replace', hash: hash(2) }]);
});

test('a night that plays through adds no history entries of its own', () => {
  let state = start();
  const kinds = [];
  for (let i = 0; i < 2000 && state.playing; i += 1) {
    const result = apply({ ...state, speed: 4 }, { type: 'tick', dt: 0.25 });
    state = result.state;
    kinds.push(...result.effects.map((e) => e.kind));
  }
  assert.deepEqual(kinds, ['replace', 'replace', 'replace']);
  assert.equal(state.lot, 4);
});

test('at the end of the night playback stops with the last lot complete', () => {
  const end = { ...start(), lot: 4, t: 24.9 };
  const { state, effects } = apply(end, { type: 'tick', dt: 0.25 });
  assert.equal(state.playing, false);
  assert.equal(state.t, 24);
  assert.equal(state.lot, 4);
  assert.deepEqual(effects, []);
});

test('seeking stays inside the lot and keeps playing', () => {
  const s = start();
  assert.equal(apply(s, { type: 'seek', t: 7 }).state.t, 7);
  assert.equal(apply(s, { type: 'seek', t: -3 }).state.t, 0);
  assert.equal(apply(s, { type: 'seek', t: 1e9 }).state.t, 21);
  assert.equal(apply(s, { type: 'seek', t: Number.NaN }).state.t, 0);
  assert.equal(apply(s, { type: 'seek', t: 7 }).state.playing, true);
});

test('the lens is a switch of its own and does not move the clock', () => {
  const s = { ...start(), t: 5 };
  const off = apply(s, { type: 'lens', value: false }).state;
  assert.equal(off.lens, false);
  assert.equal(off.t, 5);
  assert.equal(apply(off, { type: 'lens', value: true }).state.lens, true);
});

test('unknown actions do nothing and states are never changed in place', () => {
  const s = Object.freeze(start());
  assert.equal(apply(s, { type: 'dance' }).state, s);
  assert.doesNotThrow(() => apply(s, { type: 'tick', dt: 0.1 }));
  assert.doesNotThrow(() => apply(s, { type: 'goto', lot: 2 }));
});
