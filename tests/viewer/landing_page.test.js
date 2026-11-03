// The home page at work: the real site/main.js runs against the real index.html in the stand-in
// DOM (tests/viewer/landing_harness.js). These tests press the language buttons, break the storage
// and the network, and read back what the reader would see: the texts, the leaderboard, the cards
// of the six nights and the line that stands in for the leaderboard when it cannot be read.
// tests/viewer/landing.test.js holds the tests of the files themselves.

import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import test from 'node:test';

import { parseReplay } from '../../viewer/lib/load.js';
import { replayParam, resolveReplayUrl } from '../../viewer/lib/route.js';
import { h } from '../../site/dom.js';
import { HTML_LANG, LANGS, STR } from '../../site/strings.js';
import { attributeValues, FakeDocument, serialize, tagNames, walk } from './fake_dom.js';
import { PAYLOADS, REPO } from './helpers.js';
import { answer, KEY, read, seasonTable, withHome } from './landing_harness.js';

const MINUS = '−';
const RAW = JSON.parse(read('seasons/s1/leaderboard.json'));
const MODELS = ['deepseek-v4-flash-0731', 'glm-5.1', 'deepseek-v4.1-flash', 'glm-5', 'qwen3.7-plus', 'qwen-flash'];
const BOT_NAMES = { zh: ['老实人', '经济学家'], en: ['Truthful', 'Economist'] };
const opened = (options = {}) => ({ fetchImpl: answer.json(RAW), ...options });

/** What the reader sees in a language, apart from the leaderboard: every text, link and button. */
function expectLanguage(page, lang) {
  const t = STR[lang];
  assert.equal(page.doc.documentElement.getAttribute('lang'), HTML_LANG[lang]);
  assert.equal(page.doc.title, t['page.title']);
  const named = page.all('[data-i18n]');
  assert.ok(named.length >= 20);
  for (const el of named) assert.equal(el.textContent, t[el.getAttribute('data-i18n')], el.getAttribute('data-i18n'));
  for (const el of page.all('[data-i18n-alt]')) assert.equal(el.getAttribute('alt'), t[el.getAttribute('data-i18n-alt')]);
  for (const el of page.all('[data-href]')) assert.equal(el.getAttribute('href'), t[el.getAttribute('data-href')]);
  for (const el of page.all('[data-lang]')) {
    assert.equal(el.getAttribute('aria-pressed'), String(el.getAttribute('data-lang') === lang), el.getAttribute('data-lang'));
  }
}

/** What the reader sees of the leaderboard and the cards in a language. */
function expectSeason(page, lang) {
  const t = STR[lang];
  const board = page.el('board');
  assert.deepEqual(
    board.querySelectorAll('thead th').map((th) => th.textContent),
    ['board.rank', 'board.entry', 'board.nights', 'board.surplus', 'board.abstain'].map((key) => t[key]),
  );
  const rows = board.querySelectorAll('tbody tr');
  assert.equal(rows.length, 8);
  const names = rows.map((tr) => tr.querySelector('.name').textContent);
  assert.deepEqual(names, [...MODELS, ...BOT_NAMES[lang]]);
  rows.forEach((tr, i) => {
    const bot = i >= 6;
    assert.equal(tr.classList.contains('is-bot'), bot, `row ${i + 1}`);
    assert.equal(tr.querySelectorAll('.tag').map((tag) => tag.textContent).join(), bot ? t['board.bot'] : '', `row ${i + 1}`);
    assert.equal(tr.querySelector('th').getAttribute('scope'), 'row');
  });
  const cards = page.el('nights').querySelectorAll('a.night');
  assert.deepEqual(cards.map((a) => a.querySelector('.card-title').textContent), [1, 2, 3, 4, 5, 6].map((n) => t['night.title'](n)));
  assert.deepEqual(cards.map((a) => a.querySelector('.go').textContent), Array(6).fill(t['night.go']));
  assert.deepEqual(cards.map((a) => a.querySelector('.night-id').textContent), RAW.nights.map((night) => night.night));
}

const shown = (page) => ['board-note', 'board-foot', 'nights-block'].map((id) => !page.el(id).hidden);

// ------------------------------------------------------------------- the language

test('with no stored choice the page opens in the first language of the browser that it has', async () => {
  for (const [languages, lang] of [[['zh-CN', 'en'], 'zh'], [['en-US'], 'en'], [['en-GB', 'zh-CN'], 'en'], [['de-DE', 'zh-TW'], 'zh'], [['ja-JP'], 'en']]) {
    await withHome(opened({ languages }), async (page) => {
      expectLanguage(page, lang);
      expectSeason(page, lang);
    });
  }
});

test('the choice of an earlier visit wins over the browser, and a stored value that is no language is ignored', async () => {
  await withHome(opened({ languages: ['zh-CN'], stored: 'en' }), async (page) => expectLanguage(page, 'en'));
  await withHome(opened({ languages: ['en-US'], stored: 'zh' }), async (page) => expectLanguage(page, 'zh'));
  await withHome(opened({ languages: ['zh-CN'], stored: 'fr' }), async (page) => expectLanguage(page, 'zh'));
  await withHome(opened({ languages: ['en-US'], stored: '' }), async (page) => expectLanguage(page, 'en'));
  await withHome(opened({ languages: ['en-US'], stored: 'zh' }), async (page) => assert.deepEqual(page.writes, [], 'opening writes nothing'));
});

test('a language button changes every text and the leaderboard, keeps the choice, and loads nothing again', async () => {
  await withHome(opened({ languages: ['en-US'] }), async (page) => {
    assert.equal(page.fetches.length, 1);
    page.click('zh');
    expectLanguage(page, 'zh');
    expectSeason(page, 'zh');
    assert.deepEqual(page.writes, [[KEY, 'zh']]);
    page.click('zh');
    assert.deepEqual(page.writes, [[KEY, 'zh']], 'pressing the language that is on show changes nothing');
    page.click('en');
    expectLanguage(page, 'en');
    expectSeason(page, 'en');
    assert.deepEqual(page.writes, [[KEY, 'zh'], [KEY, 'en']]);
    assert.equal(page.kept.get(KEY), 'en');
    assert.equal(page.fetches.length, 1, 'the leaderboard is not asked for again');
    await page.flush();
    expectLanguage(page, 'en');
  });
});

test('the page that is drawn depends on the language and the data only, however the reader got there', async () => {
  const tree = async (options, presses = []) =>
    withHome(opened(options), async (page) => {
      for (const lang of presses) page.click(lang);
      return [serialize(page.el('board'), { canonical: true }), serialize(page.el('nights'), { canonical: true })];
    });
  const fresh = await tree({ languages: ['zh-CN'] });
  assert.deepEqual(await tree({ languages: ['en-US'] }, ['zh']), fresh);
  assert.deepEqual(await tree({ languages: ['en-US'] }, ['zh', 'en', 'zh', 'zh', 'en', 'zh']), fresh);
  assert.deepEqual(await tree({ languages: ['zh-CN'], stored: 'zh' }), fresh);
  assert.notDeepEqual(await tree({ languages: ['en-US'] }), fresh);
});

test('a second visit opens in the language that the first one ended in', async () => {
  const first = await withHome(opened({ languages: ['en-US'] }), async (page) => {
    page.click('zh');
    return page.kept.get(KEY);
  });
  await withHome(opened({ languages: ['en-US'], stored: first }), async (page) => expectLanguage(page, 'zh'));
});

test('a storage that fails to read, to write, or to be touched at all does not stop the page', async () => {
  for (const storage of ['throws', 'blocked', 'full']) {
    await withHome(opened({ languages: ['zh-CN'], storage }), async (page) => {
      expectLanguage(page, 'zh');
      expectSeason(page, 'zh');
      page.click('en');
      expectLanguage(page, 'en');
      expectSeason(page, 'en');
      page.click('zh');
      expectLanguage(page, 'zh');
      assert.equal(page.fetches.length, 1, storage);
    });
  }
});

// ---------------------------------------------------------------- the leaderboard

test('the leaderboard shows the rows of the file, with the numbers of the season documents', async () => {
  for (const [lang, file] of [['en', 'docs/seasons.md'], ['zh', 'docs/seasons_zh.md']]) {
    await withHome(opened({ languages: [lang === 'zh' ? 'zh-CN' : 'en-US'] }), async (page) => {
      const documented = seasonTable(file);
      const rows = page.rows();
      const table = page.el('board').children[0];
      assert.equal(page.el(table.getAttribute('aria-labelledby')).textContent, STR[lang]['season.boardTitle'], 'the table is named by its heading');
      assert.equal(rows.length, documented.length);
      rows.forEach((cells, i) => {
        assert.equal(cells.length, 5);
        const [rank, , nights, mean, abstentions] = documented[i];
        assert.equal(cells[0], rank, `row ${i + 1}`);
        assert.equal(cells[2], nights, `row ${i + 1}`);
        assert.equal(cells[3].replace(MINUS, '-'), mean, `row ${i + 1}`);
        assert.equal(cells[4], abstentions, `row ${i + 1}`);
      });
      assert.equal(rows.at(-1)[1], lang === 'zh' ? '经济学家机器人' : 'Economistbot', 'the name, then the mark of a bot');
      assert.deepEqual(shown(page), [true, true, true]);
    });
  }
});

test('a gain is green, a loss is red and written with a minus sign, and the bots are marked', async () => {
  await withHome(opened(), async (page) => {
    const cells = page.el('board').querySelectorAll('tbody tr').map((tr) => tr.children[3]);
    assert.deepEqual(cells.map((td) => td.classList.contains('is-gain')), [true, true, true, true, true, false, false, false]);
    assert.deepEqual(cells.map((td) => td.classList.contains('is-loss')), [false, false, false, false, false, true, true, true]);
    for (const td of cells.slice(5)) assert.ok(td.textContent.startsWith(MINUS), td.textContent);
    for (const td of cells.slice(0, 5)) assert.ok(!td.textContent.startsWith(MINUS) && !td.textContent.startsWith('-'));
    assert.equal(page.all('.tag').length, 2);
  });
});

test('the six cards open the six replays, and each replay is a file that exists and is the night of its card', async () => {
  await withHome(opened({ languages: ['zh-CN'] }), async (page) => {
    const cards = page.el('nights').querySelectorAll('a.night');
    assert.equal(cards.length, 6);
    cards.forEach((card, i) => {
      const id = RAW.nights[i].night;
      const href = card.getAttribute('href');
      assert.equal(href, `viewer/index.html?replay=../seasons/s1/${id}/replay.json`);
      const target = resolveReplayUrl(replayParam(href.slice(href.indexOf('?'))), 'http://example.test/viewer/index.html');
      assert.ok(target.ok, `${href}: ${target.reason}`);
      const file = new URL(target.url).pathname.slice(1);
      assert.ok(existsSync(join(REPO, file)), `${file} does not exist`);
      const model = parseReplay(read(file));
      assert.equal(model.night.id, id);
      assert.equal(model.night.season_id, RAW.season.id);
      assert.equal(model.night.rotation, i, 'the cards are in the order of the rotations');
    });
    assert.equal(new Set(cards.map((card) => card.getAttribute('href'))).size, 6);
  });
});

test('the leaderboard is asked for once, from this site, with no credentials and no referrer', async () => {
  await withHome(opened(), async (page) => {
    assert.deepEqual(page.fetches, [['seasons/s1/leaderboard.json', { credentials: 'omit', cache: 'no-cache', referrerPolicy: 'no-referrer' }]]);
  });
});

// ------------------------------------------------------------ waiting and failing

test('while the leaderboard loads, one line stands in its place, and it follows the language', async () => {
  let release;
  const gate = new Promise((resolve) => (release = resolve));
  await withHome({ languages: ['en-US'], fetchImpl: () => gate }, async (page) => {
    expectLanguage(page, 'en');
    assert.equal(page.el('board').textContent, STR.en['season.loading']);
    assert.equal(page.el('board').querySelector('p').getAttribute('role'), 'status');
    assert.deepEqual(shown(page), [false, false, false]);
    assert.equal(page.el('nights').children.length, 0);
    page.click('zh');
    assert.equal(page.el('board').textContent, STR.zh['season.loading']);
    release(await answer.json(RAW)());
    await page.flush();
    expectLanguage(page, 'zh');
    expectSeason(page, 'zh');
    assert.deepEqual(shown(page), [true, true, true]);
  });
});

const FAILURES = [
  ['the network is down', answer.network()],
  ['the file is not found', answer.status(404)],
  ['the answer is not JSON', answer.garbage()],
  ['the file is not a leaderboard', answer.json({ schema: 'auction-night/leaderboard@1', entries: [] })],
  ['the file is another kind of file', answer.json({ hello: 'world' })],
];

for (const [what, fetchImpl] of FAILURES) {
  test(`when ${what}, a line with a link to the season notes stands in the place of the leaderboard`, async () => {
    for (const lang of LANGS) {
      await withHome({ languages: [lang === 'zh' ? 'zh-CN' : 'en-US'], fetchImpl }, async (page) => {
        const board = page.el('board');
        const line = board.querySelector('p');
        assert.equal(board.children.length, 1);
        assert.equal(line.getAttribute('role'), 'status');
        const link = line.querySelector('a');
        assert.equal(link.getAttribute('href'), STR[lang]['link.seasons']);
        assert.equal(link.textContent, STR[lang]['season.failedLink']);
        assert.equal(line.textContent, `${STR[lang]['season.failed']} ${STR[lang]['season.failedLink']}`);
        assert.deepEqual(shown(page), [false, false, false], 'nothing that depends on the leaderboard is shown');
        assert.equal(page.el('nights').children.length, 0);
        assert.equal(page.rows().length, 0);
        // The rest of the page does not depend on the file, so it is all there.
        expectLanguage(page, lang);
        for (const el of page.all('[data-i18n]')) assert.ok(el.textContent.length > 0, el.getAttribute('data-i18n'));
        assert.equal(page.fetches.length, 1, 'it does not try again');
        // Pressing the other language redraws the line in that language, with the link of that language.
        const other = lang === 'zh' ? 'en' : 'zh';
        page.click(other);
        expectLanguage(page, other);
        assert.equal(board.querySelector('a').getAttribute('href'), STR[other]['link.seasons']);
        assert.equal(board.querySelector('p').textContent.startsWith(STR[other]['season.failed']), true);
      });
    }
  });
}

test('the link to the season notes follows the language of the page', () => {
  assert.ok(STR.zh['link.seasons'].endsWith('/docs/seasons_zh.md'));
  assert.ok(STR.en['link.seasons'].endsWith('/docs/seasons.md'));
});

// ---------------------------------------------------------------------- safety

/** A leaderboard whose names and nights are text that would be harmful if it were markup. */
function hostile(change) {
  const raw = structuredClone(RAW);
  change(raw);
  return raw;
}

test('a name that looks like markup, a script or a link is shown as the text that it is', async () => {
  for (const [kind, payload] of Object.entries(PAYLOADS)) {
    const raw = hostile((r) => {
      r.entries[0].label = payload.slice(0, 80);
      r.entries[0].entry = payload.slice(0, 80);
      r.entries[7].label = payload.slice(0, 80);
    });
    await withHome({ languages: ['en-US'], fetchImpl: answer.json(raw) }, async (page) => {
      const board = page.el('board');
      const table = board.children[0];
      const first = table.querySelector('tbody tr').querySelector('.name').textContent;
      assert.equal(first, payload.slice(0, 80), kind);
      assert.deepEqual([...tagNames(table)].sort(), ['span', 'table', 'tbody', 'td', 'th', 'thead', 'tr'], kind);
      for (const { name, value } of attributeValues(table)) {
        assert.ok(['class', 'scope', 'aria-labelledby'].includes(name), `${kind}: attribute ${name}`);
        assert.match(value, /^[\w -]+$/, `${kind}: ${name}=${value}`);
      }
      for (const tag of ['script', 'img', 'iframe', 'svg', 'style', 'a']) assert.equal(board.querySelectorAll(tag).length, 0, `${kind}: ${tag}`);
    });
  }
});

test('a night whose id would reach the address, or any other odd id, makes the file unusable', async () => {
  const ids = [PAYLOADS.image, '../../x', 'a/b', 'a?b=c', 'a#b', '', 'x'.repeat(65), '-s1', 'a b', 'a.b'];
  for (const id of ids) {
    const raw = hostile((r) => (r.nights[2].night = id));
    await withHome({ languages: ['en-US'], fetchImpl: answer.json(raw) }, async (page) => {
      assert.equal(page.el('board').querySelector('p').getAttribute('role'), 'status', JSON.stringify(id));
      assert.equal(page.el('nights').children.length, 0, JSON.stringify(id));
      assert.deepEqual(page.all('a.night'), [], JSON.stringify(id));
    });
  }
  const raw = hostile((r) => (r.season.id = '../..'));
  await withHome({ languages: ['en-US'], fetchImpl: answer.json(raw) }, async (page) => assert.deepEqual(page.all('a.night'), []));
});

test('the builders refuse what the page never builds: other tags, handlers, scripts and addresses of other sites', () => {
  const doc = new FakeDocument();
  for (const tag of ['script', 'img', 'iframe', 'svg', 'style', 'button', 'input']) assert.throws(() => h(doc, tag, {}), /tag not allowed/, tag);
  for (const name of ['onclick', 'onerror', 'style', 'src', 'srcdoc', 'id']) assert.throws(() => h(doc, 'a', { [name]: 'x' }), /attribute not allowed/, name);
  const links = [
    'javascript:alert(1)',
    'https://evil.example/x',
    'http://github.com/YYYJH1/auction-night',
    'https://github.com.evil.example/YYYJH1/auction-night',
    'https://github.com/YYYJH1/auction-night-evil',
    '//evil.example/x',
    'viewer/index.html?replay=https://evil.example/x.json',
    'viewer/index.html?replay=../../x.json',
    'viewer/index.html?replay=../seasons/s1/../../x.json',
    'viewer/index.html?replay=../seasons/s1/x.txt',
    'viewer/index.html?replay=../docs/demo/a.json#javascript:x',
  ];
  for (const href of links) assert.throws(() => h(doc, 'a', { href }), /not allowed/, href);
  assert.throws(() => h(doc, 'a', { class: 'x" onclick="y' }), /not allowed/);
  assert.throws(() => h(doc, 'td', { scope: 'row', role: 'button' }), /not allowed/);
  for (const href of ['viewer/index.html?replay=../seasons/s1/s1-d01-r0/replay.json', 'viewer/index.html?replay=../docs/demo/trial-d01-r1.json#/n/trial-d01-r1/lot/5', 'https://github.com/YYYJH1/auction-night/blob/main/README.md#try-it']) {
    assert.equal(h(doc, 'a', { href }, 'x').getAttribute('href'), href);
  }
});

test('text given to a builder becomes a text node, and a number is written as digits', () => {
  const doc = new FakeDocument();
  const cell = h(doc, 'td', { class: 'num' }, 0, ' ', PAYLOADS.script, null, false, [h(doc, 'span', {}, 7)]);
  assert.equal(cell.textContent, `0 ${PAYLOADS.script}7`);
  assert.deepEqual([...walk(cell)].map((el) => el.localName), ['td', 'span']);
  assert.equal(serialize(cell), `<td class="num">0 &lt;script&gt;alert(document.cookie)&lt;/script&gt;<span>7</span></td>`);
});

test('every link of the page that the reader can press is one that the builders would accept', async () => {
  await withHome(opened(), async (page) => {
    const links = [...walk(page.doc.body)].filter((el) => el.localName === 'a').map((el) => el.getAttribute('href'));
    assert.ok(links.length >= 12);
    for (const href of links) assert.ok(typeof href === 'string' && h(new FakeDocument(), 'a', { href }), String(href));
  });
});
