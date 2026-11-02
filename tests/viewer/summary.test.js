import assert from 'node:assert/strict';
import test from 'node:test';

import { animationFor } from '../../viewer/anim/index.js';
import { duration, frameAt, TIMING } from '../../viewer/anim/summary.js';
import { mountSummary } from '../../viewer/anim/summary_view.js';
import { STR } from '../../viewer/lib/strings.js';
import { FakeDocument } from './fake_dom.js';
import { modelOf, SEALED } from './helpers.js';

const model = modelOf(SEALED);
// A lot of a format that a newer engine might add: it keeps the real hammer of the sealed lot.
const future = { ...model.lot(1), format: 'a_future_format' };

test('each format picks its animation, and a format the viewer does not know gets the plain picture', () => {
  for (const format of ['sealed_first', 'sealed_second', 'all_pay', 'blind_box']) assert.equal(animationFor(format).id, 'envelopes', format);
  assert.equal(animationFor('english').id, 'english');
  assert.equal(animationFor('dutch').id, 'dutch');
  for (const format of ['unknown', 'a_future_format', '', undefined, null]) assert.equal(animationFor(format).id, 'summary', String(format));
});

test('the plain picture is an introduction, the hammer as recorded, and a rest', () => {
  assert.equal(frameAt(future, 0).phase, 'intro');
  assert.equal(frameAt(future, 0).verdict, null);
  const hammer = frameAt(future, TIMING.intro);
  assert.equal(hammer.phase, 'verdict');
  assert.equal(hammer.verdict.winner, 4);
  assert.equal(hammer.verdict.price, 74);
  assert.deepEqual(hammer.verdict.payments, [{ seat: 4, amount: 74 }]);
  assert.equal(frameAt(future, TIMING.intro + TIMING.verdict).phase, 'done');
  assert.equal(frameAt(future, 1e9).phase, 'done');
  assert.equal(frameAt(future, 1e9).verdict.winner, 4, 'the hammer stays on screen');
  assert.equal(frameAt(future, 0).kind, 'a_future_format');
});

test('a lot nobody bought is shown as unsold', () => {
  const unsold = { ...future, hammer: { ...future.hammer, winner: null, unsold: true, price: 0, payments: {} } };
  const frame = frameAt(unsold, TIMING.intro);
  assert.equal(frame.verdict.unsold, true);
  assert.equal(frame.verdict.winner, null);
});

test('duration and clamping', () => {
  assert.ok(Math.abs(duration() - (TIMING.intro + TIMING.verdict + TIMING.tail)) < 1e-9);
  assert.equal(animationFor('a_future_format').duration(future), duration());
  assert.equal(frameAt(future, -1).t, 0);
  assert.equal(frameAt(future, Number.NaN).t, 0);
  assert.equal(frameAt(future, 1e9).t, duration());
});

test('the plain picture is pure', () => {
  const before = JSON.stringify(future);
  assert.deepEqual(frameAt(future, 1.5), frameAt(future, 1.5));
  assert.equal(JSON.stringify(future), before);
});

test('the plain picture says why it has no animation, and shows the hammer once the time comes', () => {
  const doc = new FakeDocument();
  const parent = doc.createElement('div');
  const view = mountSummary(doc, parent, future, { model, bidThoughts: null });
  assert.equal(view.root.querySelector('.sum-title').textContent, STR.summary.unknown);
  view.update(frameAt(future, 0));
  assert.equal(view.root.querySelector('.verdict-main'), null);
  view.update(frameAt(future, duration()));
  assert.equal(view.root.querySelector('.verdict-main').textContent, '🔨 4 号「拾光」拍得，成交价 74');
  assert.equal(view.root.getAttribute('data-phase'), 'done');
  view.update(frameAt(future, 0));
  assert.equal(view.root.querySelector('.verdict-main'), null, 'and gone again when the time goes back');
});
