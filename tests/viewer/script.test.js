import assert from 'node:assert/strict';
import test from 'node:test';

import { animationFor } from '../../viewer/anim/index.js';
import { BEATS, buildScript, stateAt, talkSeconds } from '../../viewer/lib/script.js';
import { CLOCKS, modelOf, SEALED } from './helpers.js';

const sealed = modelOf(SEALED);
const scriptOf = (model, lotNo) => {
  const lot = model.lot(lotNo);
  return buildScript(lot, animationFor(lot.format).duration(lot));
};

test('reading time grows with the line and stays between the bounds', () => {
  assert.equal(talkSeconds(''), BEATS.talkMin);
  assert.ok(talkSeconds('短') >= BEATS.talkMin);
  assert.equal(talkSeconds('字'.repeat(5000)), BEATS.talkMax);
  assert.ok(talkSeconds('字'.repeat(30)) > talkSeconds('字'.repeat(10)));
  assert.equal(talkSeconds('\u{1F600}'.repeat(10)), talkSeconds('x'.repeat(10)), 'counted in characters');
});

test('beats follow each other without gaps: card, one per line, bid, host, rest', () => {
  const script = scriptOf(sealed, 1);
  assert.deepEqual(script.beats.map((b) => b.kind), ['card', ...Array(6).fill('talk'), 'bid', 'host', 'tail']);
  assert.equal(script.beats[0].start, 0);
  for (let i = 1; i < script.beats.length; i += 1) assert.equal(script.beats[i].start, script.beats[i - 1].end);
  assert.equal(script.duration, script.beats.at(-1).end);
  assert.equal(script.bidStart, script.beats.find((b) => b.kind === 'bid').start);
  assert.deepEqual(script.beats.filter((b) => b.kind === 'talk').map((b) => b.seat), sealed.lot(1).talk_order);
});

test('the bid beat is as long as the animation says', () => {
  for (const [model, lotNo] of [[sealed, 1], [sealed, 4], [modelOf(CLOCKS), 1], [modelOf(CLOCKS), 2]]) {
    const lot = model.lot(lotNo);
    const script = scriptOf(model, lotNo);
    const bid = script.beats.find((b) => b.kind === 'bid');
    assert.ok(Math.abs(bid.end - bid.start - animationFor(lot.format).duration(lot)) < 1e-9);
  }
});

test('the script knows the second at which the hammer falls, on the clock of the lot', () => {
  const lot = sealed.lot(1);
  const script = buildScript(lot, 10, 4);
  assert.equal(script.hammerAt, script.bidStart + 4);
  assert.ok(script.hammerAt > script.bidStart && script.hammerAt < script.hostStart);
  assert.equal(buildScript(lot, 10).hammerAt, null, 'without being told it does not know');
  assert.equal(buildScript(lot, 10, null).hammerAt, null);
  assert.equal(buildScript(lot, 10, 99).hammerAt, script.bidStart + 10, 'never after the bid animation');
  assert.equal(buildScript(lot, 10, -3).hammerAt, script.bidStart, 'never before it');
  for (const odd of [NaN, Infinity, '4', undefined, {}]) assert.equal(buildScript(lot, 10, odd).hammerAt, null, String(odd));
  for (const lotNo of [1, 2, 3, 4]) {
    const stage = sealed.lot(lotNo);
    const anim = animationFor(stage.format);
    const real = buildScript(stage, anim.duration(stage), anim.hammerAt(stage));
    assert.equal(real.hammerAt, real.bidStart + anim.hammerAt(stage));
  }
});

test('a lot without a host line has no host beat length', () => {
  const lot = { ...sealed.lot(1), host: null };
  const without = buildScript(lot, 10);
  const host = without.beats.find((b) => b.kind === 'host');
  assert.equal(host.end, host.start);
  assert.equal(without.hasHost, false);
  assert.equal(stateAt(without, without.duration).hostShown, false);
  assert.equal(scriptOf(sealed, 1).hasHost, true);
});

test('before the start nothing is shown but the card', () => {
  const script = scriptOf(sealed, 1);
  const state = stateAt(script, 0);
  assert.equal(state.t, 0);
  assert.deepEqual(state.talks.map((x) => x.shown), Array(6).fill(false));
  assert.equal(state.speaker, null);
  assert.equal(state.bidT, null);
  assert.equal(state.hostShown, false);
  assert.equal(state.done, false);
});

test('lines appear one at a time, in order, and the speaker is the current line', () => {
  const script = scriptOf(sealed, 1);
  const talks = script.beats.filter((b) => b.kind === 'talk');
  let shownBefore = 0;
  for (const beat of talks) {
    const state = stateAt(script, beat.start + 0.01);
    const shown = state.talks.filter((x) => x.shown).length;
    assert.equal(shown, shownBefore + 1);
    shownBefore = shown;
    assert.equal(state.speaker, beat.seat);
    assert.equal(state.talks[beat.index].speaking, true);
    assert.equal(state.talks[beat.index].thought, false, 'the monologue comes a moment later');
    const later = stateAt(script, beat.start + BEATS.thoughtDelay + 0.01);
    assert.equal(later.talks[beat.index].thought, true);
  }
});

test('the bid animation starts after the last line and its clock is the time inside the beat', () => {
  const script = scriptOf(sealed, 2);
  assert.equal(stateAt(script, script.bidStart - 0.001).bidT, null);
  assert.equal(stateAt(script, script.bidStart).bidT, 0);
  assert.equal(stateAt(script, script.bidStart + 1.5).bidT, 1.5);
  assert.equal(stateAt(script, script.duration).bidT, script.bidSeconds, 'capped at its length');
  assert.equal(stateAt(script, script.bidStart - 0.001).talks.filter((x) => x.shown).length, 6);
});

test('the host line comes after the bid and the lot ends done', () => {
  const script = scriptOf(sealed, 3);
  assert.equal(stateAt(script, script.hostStart - 0.001).hostShown, false);
  assert.equal(stateAt(script, script.hostStart).hostShown, true);
  const end = stateAt(script, script.duration);
  assert.equal(end.done, true);
  assert.equal(end.hostShown, true);
  assert.deepEqual(end.talks.map((x) => x.shown), Array(6).fill(true));
});

test('time outside the lot is clamped, and nonsense is zero', () => {
  const script = scriptOf(sealed, 1);
  assert.equal(stateAt(script, -5).t, 0);
  assert.equal(stateAt(script, 1e9).t, script.duration);
  for (const bad of [Number.NaN, undefined, 'x', null]) assert.equal(stateAt(script, bad).t, 0);
});

test('stateAt is a pure function of the script and the time', () => {
  const script = scriptOf(sealed, 1);
  const before = JSON.stringify(script);
  const a = stateAt(script, 7.3);
  const b = stateAt(script, 7.3);
  assert.deepEqual(a, b);
  assert.equal(JSON.stringify(script), before);
});

test('a lot nobody spoke in still plays', () => {
  const lot = { ...sealed.lot(1), talk: [] };
  const script = buildScript(lot, 10);
  assert.deepEqual(script.beats.map((b) => b.kind), ['card', 'bid', 'host', 'tail']);
  assert.deepEqual(stateAt(script, 3).talks, []);
});
