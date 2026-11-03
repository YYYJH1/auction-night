// The home page: connects the texts, the leaderboard, the language buttons and the browser.
//
// The decisions live in the other files of this folder. This one reads the language that the
// reader chose before (the only thing the page remembers, in localStorage, and only when the
// browser lets it), asks the browser for the language list, fetches the leaderboard of the
// season from this site, and redraws the page when a language button is pressed.

import { loadBoard } from './board.js';
import { applyLanguage, renderSeason, seasonParts } from './page.js';
import { LANGS, pickLanguage } from './strings.js';

const doc = document;
const win = window;
const KEY = 'auction-night.lang';

// Storage can be missing, full or refused (a private window, blocked site data). Every use of it
// sits in a try block, and the page works without it.
function remembered() {
  try {
    return win.localStorage.getItem(KEY);
  } catch {
    return null;
  }
}

function remember(lang) {
  try {
    win.localStorage.setItem(KEY, lang);
  } catch {
    // the choice then lasts until the page is closed
  }
}

function browserLanguages() {
  const nav = win.navigator ?? {};
  return Array.isArray(nav.languages) && nav.languages.length > 0 ? nav.languages : [nav.language ?? ''];
}

const parts = seasonParts(doc);
let lang = pickLanguage({ stored: remembered(), languages: browserLanguages() });
let state = { status: 'loading' };

function draw() {
  applyLanguage(doc, lang);
  renderSeason(doc, parts, state, lang);
}

for (const button of doc.querySelectorAll('[data-lang]')) {
  button.addEventListener('click', () => {
    const chosen = button.getAttribute('data-lang');
    if (!LANGS.includes(chosen) || chosen === lang) return;
    lang = chosen;
    remember(lang);
    draw();
  });
}

async function boot() {
  draw();
  const board = await loadBoard(win.fetch.bind(win));
  state = board.ok ? { status: 'ready', ...board } : { status: 'failed' };
  draw();
}

boot();
