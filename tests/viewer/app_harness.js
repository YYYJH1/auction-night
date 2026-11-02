// Runs the real viewer/app.js under Node, against the stand-in DOM and a small model of the
// browser: an address bar with a history, an animation frame queue, a fetch that the test
// controls, and the keyboard. Nothing here is a second implementation of the page. The page
// code runs unchanged, so what it does with the address, the clock and the buttons is tested.

import { readFileSync } from 'node:fs';

import { VIEWER } from './helpers.js';
import { FakeDocument } from './fake_dom.js';

const ELEMENTS = [
  ['header', 'topbar'], ['p', 'night-label'], ['nav', 'views'], ['div', 'controls'], ['div', 'transport'], ['button', 'btn-prev'],
  ['button', 'btn-play'], ['button', 'btn-next'], ['div', 'speeds'], ['input', 'scrub'], ['span', 'time'],
  ['button', 'lens'], ['input', 'file'], ['span', 'file-label'], ['div', 'panel'], ['div', 'app'],
  ['h2', 'lots-title'], ['ol', 'lot-list'], ['main', 'stage'], ['div', 'recap-bar'], ['div', 'lot-stage'],
  ['div', 'page'],
];

/** The ids that index.html declares. The harness must offer exactly these. */
export function pageIds() {
  const html = readFileSync(new URL('index.html', `file://${VIEWER}`), 'utf8');
  return [...html.matchAll(/\sid="([^"]+)"/g)].map((m) => m[1]);
}

export const harnessIds = ELEMENTS.map(([, id]) => id);
export const STAGE_PARTS = ['recap-bar', 'lot-stage', 'page'];

let counter = 0;

export async function launch({
  search = '',
  hash = '',
  base = 'http://example.test/viewer/index.html',
  fetchImpl = async () => {
    throw new Error('no fetch expected');
  },
  reducedMotion = false,
  barHeight = null,
} = {}) {
  const doc = new FakeDocument();
  const byId = new Map();
  for (const [tag, id] of ELEMENTS) {
    const el = doc.createElement(tag);
    el.setAttribute('id', id);
    byId.set(id, el);
  }
  // As in index.html, the stage holds the caption of the recap, the lot, and the page of the
  // awards or the highlights. A test compares this order with the markup.
  for (const id of STAGE_PARTS) byId.get('stage').appendChild(byId.get(id));
  byId.get('scrub').value = '0';
  byId.get('file').files = [];
  byId.get('controls').hidden = true;
  byId.get('app').hidden = true;
  byId.get('panel').hidden = false;
  doc.getElementById = (id) => byId.get(id) ?? null;
  doc.title = '';
  doc.documentElement = doc.createElement('html');

  const listeners = { window: new Map(), document: new Map() };
  const addTo = (where) => (type, fn) => {
    if (!listeners[where].has(type)) listeners[where].set(type, []);
    listeners[where].get(type).push(fn);
  };
  doc.addEventListener = addTo('document');

  // The address bar: a list of entries and a cursor, as in a browser.
  const entries = [{ hash }];
  let at = 0;
  const calls = { push: [], replace: [] };
  const location = {
    get search() {
      return search;
    },
    get hash() {
      return entries[at].hash;
    },
    get href() {
      return `${base.split('?')[0]}${search}${entries[at].hash}`;
    },
  };
  const history = {
    pushState(_state, _title, url) {
      calls.push.push(url);
      entries.splice(at + 1, Infinity, { hash: url });
      at += 1;
    },
    replaceState(_state, _title, url) {
      calls.replace.push(url);
      entries[at] = { hash: url };
    },
  };
  const fire = (type) => {
    for (const fn of listeners.window.get(type) ?? []) fn({ type });
  };

  // Animation frames: callbacks wait until the test runs them.
  const pending = new Map();
  let nextFrame = 1;
  let maxPending = 0;
  let stamp = 1000;
  const requests = [];

  const win = {
    location,
    history,
    matchMedia: () => ({ matches: reducedMotion }),
    requestAnimationFrame(callback) {
      const id = nextFrame++;
      pending.set(id, callback);
      maxPending = Math.max(maxPending, pending.size);
      return id;
    },
    cancelAnimationFrame: (id) => pending.delete(id),
    addEventListener: addTo('window'),
    fetch: async (url, options) => {
      requests.push([url, options]);
      return fetchImpl(url, options);
    },
  };
  // A browser that can say when the top bar changes its size: the test sets the height and calls
  // every observer. Without barHeight there is no ResizeObserver, as in an old browser.
  const observers = [];
  if (barHeight !== null) {
    byId.get('topbar').offsetHeight = barHeight;
    win.ResizeObserver = class {
      constructor(callback) {
        observers.push(callback);
      }

      observe() {}
    };
  }

  const saved = new Map();
  const install = (name, value) => {
    saved.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });
  };
  install('document', doc);
  install('window', win);
  install('history', history);

  await import(`../../viewer/app.js?run=${(counter += 1)}`);

  const page = {
    doc,
    el: (id) => byId.get(id),
    calls,
    requests,
    entries,
    get maxPending() {
      return maxPending;
    },
    get pendingFrames() {
      return pending.size;
    },
    /** Let promises settle: fetches, file reads, the start of a night. */
    async flush() {
      for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setImmediate(resolve));
    },
    /** Run one animation frame, `ms` after the last one. */
    frame(ms = 16) {
      const [[id, callback]] = [...pending];
      pending.delete(id);
      stamp += ms;
      callback(stamp);
    },
    /** Play on for `seconds` of wall clock time, in frames of about 16 ms. */
    run(seconds) {
      const count = Math.round((seconds * 1000) / 16);
      for (let i = 0; i < count && pending.size > 0; i += 1) page.frame(16);
    },
    click: (id) => byId.get(id).dispatch('click'),
    /** The page element that holds the custom properties, and a way to resize the top bar. */
    rootStyle: (name) => doc.documentElement.style.getPropertyValue(name),
    resizeBar(height) {
      byId.get('topbar').offsetHeight = height;
      for (const callback of observers) callback([]);
    },
    clickSpeed(value) {
      const button = byId.get('speeds').children.find((b) => b.getAttribute('data-speed') === String(value));
      button.dispatch('click');
    },
    press(key, { tagName = 'BODY', ...modifiers } = {}) {
      const event = { key, target: { tagName }, defaultPrevented: false, ctrlKey: false, metaKey: false, altKey: false, ...modifiers, preventDefault() { this.defaultPrevented = true; } };
      for (const fn of listeners.document.get('keydown') ?? []) fn(event);
      return event;
    },
    /** The user follows a link or edits the address: a new entry, then the event. */
    navigate(newHash) {
      entries.splice(at + 1, Infinity, { hash: newHash });
      at += 1;
      fire('hashchange');
    },
    back() {
      at -= 1;
      fire('hashchange');
    },
    forward() {
      at += 1;
      fire('hashchange');
    },
    async chooseFile(file) {
      byId.get('file').files = [file];
      byId.get('file').dispatch('change');
      await page.flush();
    },
    get hash() {
      return location.hash;
    },
    text: (id) => byId.get(id).textContent,
    currentLot: () => byId.get('lot-list').querySelector('.lot-item.is-current .lot-no')?.textContent ?? null,
    close() {
      for (const [name, descriptor] of saved) {
        if (descriptor) Object.defineProperty(globalThis, name, descriptor);
        else delete globalThis[name];
      }
    },
  };
  await page.flush();
  return page;
}

/** Run a test body with a launched page and always put the globals back. */
export async function withPage(options, body) {
  const page = await launch(options);
  try {
    return await body(page);
  } finally {
    page.close();
  }
}
