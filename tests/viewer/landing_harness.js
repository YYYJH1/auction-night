// Runs the real site/main.js under Node, against the stand-in DOM built from the real index.html
// and a small model of the browser: the list of languages, the storage, and a fetch that the test
// controls. Nothing here is a second implementation of the page. The page code runs unchanged,
// so what it does with the language buttons, the storage and the leaderboard is what is tested.

import { readFileSync } from 'node:fs';

import { FakeDocument, walk } from './fake_dom.js';
import { REPO } from './helpers.js';

export const read = (path) => readFileSync(`${REPO}${path}`, 'utf8');
export const INDEX = read('index.html');
export const KEY = 'auction-night.lang';

const VOID = new Set(['meta', 'link', 'img', 'source', 'br', 'hr', 'input']);
const TOKEN = /<(\/?)([a-zA-Z][\w-]*)([^>]*?)>|([^<]+)/g;
const ATTRIBUTE = /([\w:-]+)(?:="([^"]*)")?/g;

/**
 * Build what is between <body> and </body> in the stand-in DOM. The markup of index.html is plain:
 * quoted attributes, no entities, no comment in the body. A boolean attribute that the page code
 * reads as a property (hidden) is set as that property too.
 */
export function buildBody(doc, html) {
  const from = html.indexOf('<body>');
  const to = html.indexOf('</body>');
  if (from < 0 || to < 0) throw new Error('no body');
  const stack = [doc.body];
  for (const match of html.slice(from + '<body>'.length, to).matchAll(TOKEN)) {
    const [, closing, tag, attributes, text] = match;
    const top = stack.at(-1);
    if (text !== undefined) {
      if (text.trim() !== '') top.append(text.replace(/\s+/g, ' '));
      continue;
    }
    const name = tag.toLowerCase();
    if (closing) {
      if (stack.length < 2 || top.localName !== name) throw new Error(`unbalanced </${name}>`);
      stack.pop();
      continue;
    }
    const el = doc.createElement(name);
    for (const attribute of attributes.matchAll(ATTRIBUTE)) {
      el.setAttribute(attribute[1], attribute[2] ?? '');
      if (attribute[1] === 'hidden') el.hidden = true;
    }
    top.appendChild(el);
    if (!VOID.has(name)) stack.push(el);
  }
  if (stack.length !== 1) throw new Error('an element is not closed');
}

/** A document with the body of index.html, as the page code uses it. */
export function makeDocument(html = INDEX) {
  const doc = new FakeDocument();
  doc.title = '';
  doc.documentElement = doc.createElement('html');
  buildBody(doc, html);
  const ids = [];
  doc.requestedIds = ids;
  doc.getElementById = (id) => {
    ids.push(id);
    return [...walk(doc.body)].find((el) => el.getAttribute('id') === id) ?? null;
  };
  doc.querySelectorAll = (selector) => doc.body.querySelectorAll(selector);
  doc.querySelector = (selector) => doc.body.querySelector(selector);
  return doc;
}

/** The rows of the leaderboard table in a document of the season: [rank, name, nights, mean, abstentions]. */
export function seasonTable(file) {
  return read(file)
    .split('\n')
    .map((line) => /^\| (\d+) \| (.+?) \| (\d+) \| (-?[\d.]+) \| ([\d.]+%) \|$/.exec(line))
    .filter(Boolean)
    .map((m) => m.slice(1));
}

/** The answers of a fetch: a leaderboard file, a bad status, a body that is not JSON. */
export const answer = {
  json: (raw) => async () => ({ ok: true, status: 200, json: async () => structuredClone(raw) }),
  status: (code) => async () => ({ ok: false, status: code, json: async () => ({}) }),
  garbage: () => async () => ({
    ok: true,
    status: 200,
    json: async () => {
      throw new SyntaxError('Unexpected token < in JSON at position 0');
    },
  }),
  network: () => async () => {
    throw new TypeError('Failed to fetch');
  },
};

let counter = 0;

/**
 * Open the page. `languages` is the list of the browser. `storage` is 'works' (with `stored` as
 * the value kept from an earlier visit), 'throws' (reading and writing fail), 'full' (only writing
 * fails) or 'blocked' (touching window.localStorage fails). `fetchImpl` gives the response.
 */
export async function launchHome({ languages = ['en-US'], storage = 'works', stored, fetchImpl } = {}) {
  const doc = makeDocument();
  const kept = new Map(stored === undefined ? [] : [[KEY, stored]]);
  const writes = [];
  const fetches = [];
  const area = {
    getItem: (key) => kept.get(key) ?? null,
    setItem: (key, value) => {
      writes.push([key, value]);
      kept.set(key, String(value));
    },
  };
  const win = {
    navigator: { languages, language: languages[0] },
    fetch: async (url, options) => {
      fetches.push([url, options]);
      return fetchImpl(url, options);
    },
  };
  if (storage === 'blocked') {
    Object.defineProperty(win, 'localStorage', {
      get() {
        throw new Error('SecurityError: the storage is blocked');
      },
    });
  } else if (storage === 'throws') {
    win.localStorage = {
      getItem() {
        throw new Error('SecurityError');
      },
      setItem() {
        throw new Error('SecurityError');
      },
    };
  } else if (storage === 'full') {
    win.localStorage = {
      getItem: () => null,
      setItem() {
        throw new Error('QuotaExceededError');
      },
    };
  } else {
    win.localStorage = area;
  }

  const saved = new Map();
  const install = (name, value) => {
    saved.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });
  };
  install('document', doc);
  install('window', win);
  await import(`../../site/main.js?run=${(counter += 1)}`);

  const page = {
    doc,
    writes,
    fetches,
    kept,
    el: (id) => doc.getElementById(id),
    all: (selector) => doc.querySelectorAll(selector),
    /** Let promises settle: the fetch, and the redraw after it. */
    async flush() {
      for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setImmediate(resolve));
    },
    /** Press a language button: 'zh' or 'en'. */
    click: (lang) => doc.querySelector(`[data-lang=${lang}]`).dispatch('click'),
    /** The text of the board: one array of cell texts per row of the body. */
    rows: () => doc.getElementById('board').querySelectorAll('tbody tr').map((tr) => tr.children.map((cell) => cell.textContent)),
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

/** Run a test body with an open page and always put the globals back. */
export async function withHome(options, body) {
  const page = await launchHome(options);
  try {
    return await body(page);
  } finally {
    page.close();
  }
}
