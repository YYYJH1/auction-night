// The home page: puts the texts of one language into index.html, and builds the leaderboard and
// the cards of the six nights from the checked leaderboard. Both take the document as an
// argument and know nothing of the address bar, the storage or the network (main.js does).

import { replayHref } from './board.js';
import { clear, h, setAttr } from './dom.js';
import { HTML_LANG, LANGS, nameOf, textOf } from './strings.js';

/**
 * Put one language into the page: the lang attribute and the title of the document, every
 * element that index.html marks with data-i18n, data-i18n-alt or data-href, and the state of the
 * language buttons.
 */
export function applyLanguage(doc, lang) {
  if (!LANGS.includes(lang)) throw new RangeError(`not a language: ${lang}`);
  setAttr(doc.documentElement, 'lang', HTML_LANG[lang]);
  doc.title = textOf(lang, 'page.title');
  for (const el of doc.querySelectorAll('[data-i18n]')) el.textContent = textOf(lang, el.getAttribute('data-i18n'));
  for (const el of doc.querySelectorAll('[data-i18n-alt]')) setAttr(el, 'alt', textOf(lang, el.getAttribute('data-i18n-alt')));
  for (const el of doc.querySelectorAll('[data-href]')) setAttr(el, 'href', textOf(lang, el.getAttribute('data-href')));
  for (const el of doc.querySelectorAll('[data-lang]')) {
    setAttr(el, 'aria-pressed', el.getAttribute('data-lang') === lang ? 'true' : 'false');
  }
}

/** The parts of the season section that the page fills in, found by id. */
export function seasonParts(doc) {
  const find = (id) => {
    const el = doc.getElementById(id);
    if (el === null) throw new Error(`index.html has no element with id ${id}`);
    return el;
  };
  return {
    board: find('board'),
    boardNote: find('board-note'),
    boardFoot: find('board-foot'),
    nightsBlock: find('nights-block'),
    nights: find('nights'),
  };
}

function boardTable(doc, rows, lang) {
  const t = (key, ...args) => textOf(lang, key, ...args);
  const head = h(
    doc,
    'tr',
    {},
    h(doc, 'th', { scope: 'col', class: 'rank' }, t('board.rank')),
    h(doc, 'th', { scope: 'col' }, t('board.entry')),
    h(doc, 'th', { scope: 'col', class: 'num' }, t('board.nights')),
    h(doc, 'th', { scope: 'col', class: 'num' }, t('board.surplus')),
    h(doc, 'th', { scope: 'col', class: 'num' }, t('board.abstain')),
  );
  const body = rows.map((row) =>
    h(
      doc,
      'tr',
      { class: row.kind === 'bot' ? 'is-bot' : null },
      h(doc, 'td', { class: 'rank' }, row.rank),
      h(
        doc,
        'th',
        { scope: 'row', class: 'who' },
        h(doc, 'span', { class: 'name' }, nameOf(lang, row.id, row.label)),
        row.kind === 'bot' ? h(doc, 'span', { class: 'tag' }, t('board.bot')) : null,
      ),
      h(doc, 'td', { class: 'num' }, row.nights),
      h(doc, 'td', { class: row.sign > 0 ? 'num is-gain' : row.sign < 0 ? 'num is-loss' : 'num' }, row.mean),
      h(doc, 'td', { class: 'num' }, row.abstain),
    ),
  );
  return h(doc, 'table', { class: 'board', 'aria-labelledby': 'board-title' }, h(doc, 'thead', {}, head), h(doc, 'tbody', {}, body));
}

function nightCard(doc, seasonId, id, number, lang) {
  return h(
    doc,
    'li',
    {},
    h(
      doc,
      'a',
      { class: 'card night', href: replayHref(seasonId, id) },
      h(doc, 'strong', { class: 'card-title' }, textOf(lang, 'night.title', number)),
      h(doc, 'span', { class: 'night-id' }, id),
      h(doc, 'span', { class: 'go' }, textOf(lang, 'night.go')),
    ),
  );
}

/**
 * Show the season in a language. `state` is {status: 'loading'}, {status: 'failed'} or the
 * result of parseBoard with the status 'ready' added. While the board is not there, one line
 * stands in its place, and after a failure the line holds a link to the notes of the season, so
 * the section is never empty.
 */
export function renderSeason(doc, parts, state, lang) {
  const ready = state.status === 'ready';
  parts.boardNote.hidden = !ready;
  parts.boardFoot.hidden = !ready;
  parts.nightsBlock.hidden = !ready;
  clear(parts.board);
  clear(parts.nights);
  if (!ready) {
    const failed = state.status === 'failed';
    parts.board.appendChild(
      h(
        doc,
        'p',
        { class: 'note', role: 'status' },
        textOf(lang, failed ? 'season.failed' : 'season.loading'),
        failed ? ' ' : null,
        failed ? h(doc, 'a', { href: textOf(lang, 'link.seasons') }, textOf(lang, 'season.failedLink')) : null,
      ),
    );
    return;
  }
  parts.board.appendChild(boardTable(doc, state.rows, lang));
  state.nights.forEach((id, i) => parts.nights.appendChild(nightCard(doc, state.seasonId, id, i + 1, lang)));
}
