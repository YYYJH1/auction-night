// A frame loop that never has more than one frame pending.
//
// `request(callback)` and `cancel(id)` stand in for the browser's requestAnimationFrame and
// cancelAnimationFrame, so the loop can be tested without a browser. After each frame
// `onTick(seconds)` says whether to go on. Asking the clock to start while it is already
// running, or from inside a tick, changes nothing: the frame that is running schedules the
// next one itself.

export function createClock({ request, cancel, onTick }) {
  let pending = 0;
  let last = 0;
  let inFrame = false;

  function frame(stamp) {
    pending = 0;
    const seconds = last === 0 ? 0 : (stamp - last) / 1000;
    last = stamp;
    inFrame = true;
    let again = false;
    try {
      again = onTick(seconds) === true;
    } finally {
      inFrame = false;
    }
    if (again) pending = request(frame);
    else last = 0;
  }

  return {
    start() {
      if (pending !== 0 || inFrame) return;
      last = 0;
      pending = request(frame);
    },
    stop() {
      if (pending !== 0) cancel(pending);
      pending = 0;
      last = 0;
    },
    get running() {
      return pending !== 0 || inFrame;
    },
  };
}
