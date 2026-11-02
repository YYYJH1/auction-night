import assert from 'node:assert/strict';
import test from 'node:test';

import { createClock } from '../../viewer/lib/clock.js';

/** A stand-in for requestAnimationFrame: frames wait in a list until the test runs them. */
function harness(onTick) {
  const queue = new Map();
  let next = 1;
  const clock = createClock({
    request: (callback) => {
      queue.set(next, callback);
      return next++;
    },
    cancel: (id) => queue.delete(id),
    onTick: (seconds) => onTick(seconds, clock),
  });
  const run = (stamp) => {
    const [[id, callback]] = [...queue];
    queue.delete(id);
    callback(stamp);
  };
  return { clock, queue, run };
}

test('the clock asks for one frame and ticks with the time between frames', () => {
  const seen = [];
  const { clock, queue, run } = harness((s) => (seen.push(s), true));
  clock.start();
  assert.equal(queue.size, 1);
  run(1000);
  run(1016);
  run(1050);
  assert.deepEqual(seen.map((s) => Math.round(s * 1000)), [0, 16, 34]);
  assert.equal(queue.size, 1, 'always exactly one frame pending');
});

test('starting again, or starting from inside a tick, never queues a second frame', () => {
  const { clock, queue, run } = harness((s, c) => {
    c.start();
    c.start();
    return true;
  });
  clock.start();
  clock.start();
  assert.equal(queue.size, 1);
  for (let i = 0; i < 50; i += 1) {
    run(1000 + i * 16);
    assert.equal(queue.size, 1, `after frame ${i}`);
  }
});

test('a tick that returns false ends the loop', () => {
  let alive = true;
  const { clock, queue, run } = harness(() => alive);
  clock.start();
  run(100);
  assert.equal(queue.size, 1);
  alive = false;
  run(116);
  assert.equal(queue.size, 0);
  assert.equal(clock.running, false);
});

test('stop cancels the pending frame, and a restart measures from scratch', () => {
  const seen = [];
  const { clock, queue, run } = harness((s) => (seen.push(s), true));
  clock.start();
  run(100);
  clock.stop();
  assert.equal(queue.size, 0);
  assert.equal(clock.running, false);
  clock.start();
  run(5000);
  assert.equal(seen.at(-1), 0, 'no jump for the time spent stopped');
});

test('a tick that throws leaves the clock stopped, not stuck', () => {
  const { clock, queue, run } = harness(() => {
    throw new Error('boom');
  });
  clock.start();
  assert.throws(() => run(10), /boom/);
  assert.equal(queue.size, 0);
  assert.equal(clock.running, false);
  clock.start();
  assert.equal(queue.size, 1);
});
