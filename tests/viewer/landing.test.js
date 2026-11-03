// The home page of the site (index.html and the folder site/). These tests read its files and
// hold what the page promises: that it is a static page with no reach beyond its own site, that
// every link on it leads to a file that exists and that the replay page accepts, that the two
// languages say the same things, that the numbers of the leaderboard are those of the season, and
// that the style sheet keeps the colours of the replay page and a layout that fits a phone.
// tests/viewer/landing_page.test.js runs the page itself.

import assert from 'node:assert/strict';
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import test from 'node:test';

import { parseReplay } from '../../viewer/lib/load.js';
import { parseHash, replayParam, resolveReplayUrl } from '../../viewer/lib/route.js';
import { BOARD_URL, loadBoard, parseBoard, percentText, replayHref, tenthsText } from '../../site/board.js';
import { HREF_PATTERN } from '../../site/dom.js';
import { LANGS, nameOf, pickLanguage, STR, textOf } from '../../site/strings.js';
import { answer, INDEX, read, seasonTable } from './landing_harness.js';
import { REPO } from './helpers.js';

const SITE = join(REPO, 'site');
const MINUS = '\u2212';
// Control and format characters, and the two line separators, which no source file may hold.
const INVISIBLE = new RegExp(`[\\p{Cc}\\p{Cf}\\p{Cs}${String.fromCodePoint(0x2028)}${String.fromCodePoint(0x2029)}]`, 'u');
const REPO_URL = 'https://github.com/YYYJH1/auction-night';
const CSP =
  "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'";

const source = new Map(readdirSync(SITE).sort().map((name) => [name, readFileSync(join(SITE, name), 'utf8')]));
const scripts = [...source].filter(([name]) => name.endsWith('.js'));
const css = source.get('home.css');
const viewerCss = read('viewer/style.css');
const bare = INDEX.replace(/<!--[\s\S]*?-->/g, '');
const leaderboard = JSON.parse(read('seasons/s1/leaderboard.json'));

// -------------------------------------------------------------------------- the markup

test('index.html is the home page and not a redirect', () => {
  assert.ok(!/http-equiv="refresh"/i.test(bare), 'it no longer jumps to a replay');
  assert.match(bare, /<html lang="zh-CN">/);
  assert.match(bare, /<title>AI 拍卖之夜 · AI Auction Night<\/title>/);
  assert.match(bare, /<meta name="color-scheme" content="dark light">/);
  assert.match(bare, /<h1 id="hero-title" data-i18n="hero\.title"><\/h1>/);
});

test('index.html declares the content security policy of the replay page, before any script', () => {
  const metas = [...bare.matchAll(/<meta\s+http-equiv="Content-Security-Policy"\s+content="([^"]*)"/g)];
  assert.equal(metas.length, 1);
  assert.equal(metas[0][1], CSP);
  assert.equal(metas[0][1], /http-equiv="Content-Security-Policy"\s+content="([^"]*)"/.exec(read('viewer/index.html'))[1]);
  assert.ok(INDEX.indexOf('Content-Security-Policy') < INDEX.indexOf('<script'), 'declared before any script');
});

test('index.html has one module script and one style sheet, and no inline script, style or handler', () => {
  assert.deepEqual([...bare.matchAll(/<script\b[^>]*>/g)].map((m) => m[0]), ['<script type="module" src="site/main.js">']);
  assert.ok(!/<script\b[^>]*>[^<\s]/.test(bare), 'a script tag with a body');
  assert.ok(!/<style\b/i.test(bare), 'a style element');
  assert.ok(!/\sstyle\s*=/i.test(bare), 'a style attribute');
  assert.ok(!/\son[a-z]+\s*=/i.test(bare), 'an event handler attribute');
  assert.ok(!/<(?:iframe|object|embed|base|form|svg|link\s+rel="(?!stylesheet))/i.test(bare));
  assert.deepEqual([...bare.matchAll(/<link\b[^>]*>/g)].map((m) => m[0]), ['<link rel="stylesheet" href="site/home.css">']);
});

test('the page takes the width of the screen and lets the reader zoom', () => {
  const meta = bare.match(/<meta name="viewport" content="([^"]*)">/);
  assert.ok(meta);
  assert.match(meta[1], /width=device-width/);
  assert.match(meta[1], /initial-scale=1/);
  assert.ok(!/user-scalable\s*=\s*(?:no|0)|maximum-scale/.test(meta[1]), 'zooming is not switched off');
});

test('index.html has a header comment that says what the page is, how to open it and what it keeps', () => {
  assert.match(INDEX, /^<!doctype html>\s*<!--/i);
  const comment = INDEX.match(/<!--([\s\S]*?)-->/)[1];
  for (const part of ['home page', 'file://', 'localStorage', 'text nodes', 'data-i18n', 'site/']) assert.ok(comment.includes(part), part);
});

test('without scripts the page still says so and points to the repository', () => {
  const noscript = bare.match(/<noscript>([\s\S]*?)<\/noscript>/)[1];
  assert.match(noscript, /JavaScript/);
  assert.ok(noscript.includes(`href="${REPO_URL}"`));
});

test('every address in index.html is a file of this site that exists, or an address of the repository', () => {
  const found = [...bare.matchAll(/\s(?:href|src)="([^"]+)"/g)].map((m) => m[1]);
  for (const m of bare.matchAll(/\ssrcset="([^"]+)"/g)) found.push(...m[1].split(',').map((part) => part.trim().split(/\s+/)[0]));
  assert.ok(found.length >= 10, 'the addresses are found');
  for (const address of found) {
    if (address.startsWith('https://')) {
      assert.ok(address === REPO_URL || address.startsWith(`${REPO_URL}/`), `${address} leaves the repository`);
      continue;
    }
    assert.ok(!/^[A-Za-z][A-Za-z0-9+.-]*:|^\/\//.test(address), `${address} has a scheme`);
    const path = address.replace(/[?#].*$/, '');
    assert.ok(existsSync(resolve(REPO, path)), `${address} does not exist`);
  }
  // A link that takes its address from strings.js has none of its own.
  for (const tag of bare.match(/<a\b[^>]*data-href=[^>]*>/g)) assert.ok(!/\shref=/.test(tag), tag);
});

test('the GIF follows the colour scheme as in the README, and the width and height of the image are those of the files', () => {
  assert.match(bare, /<source media="\(prefers-color-scheme: dark\)" srcset="docs\/assets\/lot-dark\.gif">/);
  const img = bare.match(/<img\b[^>]*>/)[0];
  assert.match(img, /src="docs\/assets\/lot-light\.gif"/);
  for (const name of ['lot-dark.gif', 'lot-light.gif']) {
    const bytes = readFileSync(join(REPO, 'docs', 'assets', name));
    assert.equal(bytes.subarray(0, 6).toString('latin1').slice(0, 3), 'GIF');
    assert.ok(img.includes(`width="${bytes.readUInt16LE(6)}" height="${bytes.readUInt16LE(8)}"`), name);
  }
  assert.match(img, /alt=""\s+data-i18n-alt="hero\.alt"/, 'the alt text comes from strings.js');
});

// The links of "Start here": the replay page has to accept each one.
const startLinks = [...bare.matchAll(/<a class="card" href="([^"]+)">/g)].map((m) => m[1]);

test('the three links of "Start here" open the demo night, its lot 5, its three-minute version and its awards', () => {
  assert.equal(startLinks.length, 3);
  const BASE = 'http://example.test/viewer/index.html';
  const views = [];
  for (const href of startLinks) {
    const url = new URL(href, 'http://example.test/index.html');
    assert.equal(url.pathname, '/viewer/index.html');
    const target = resolveReplayUrl(replayParam(url.search), BASE);
    assert.ok(target.ok, `${href}: ${target.reason}`);
    const file = new URL(target.url).pathname.slice(1);
    assert.equal(file, 'docs/demo/trial-d01-r1.json');
    const model = parseReplay(read(file));
    const link = parseHash(url.hash);
    assert.equal(link.night, model.night.id, 'the address names the night that the file holds');
    views.push(link.view);
    if (link.view === 'lot') assert.ok(model.lotNos.includes(link.lot), `lot ${link.lot} was played`);
    if (link.view === 'lot') assert.equal(link.lot, 5);
    if (link.view === 'recap') assert.ok(model.highlights.length > 0, 'a three-minute version needs highlights');
  }
  assert.deepEqual(views, ['lot', 'recap', 'awards']);
});

// ------------------------------------------------------------------------------ the texts

/** The keys of an object, nested objects included, as paths. */
function paths(object, prefix = '') {
  return Object.entries(object).flatMap(([key, value]) =>
    typeof value === 'object' && value !== null ? paths(value, `${prefix}${key}.`) : [`${prefix}${key}`],
  );
}

test('the two languages have the same keys, of the same kind', () => {
  assert.deepEqual(LANGS, ['zh', 'en']);
  assert.deepEqual(paths(STR.zh).sort(), paths(STR.en).sort());
  for (const key of Object.keys(STR.zh)) {
    assert.equal(typeof STR.zh[key], typeof STR.en[key], key);
    if (typeof STR.zh[key] === 'function') assert.equal(STR.zh[key].length, STR.en[key].length, `${key} takes the same arguments`);
  }
});

test('no text is empty, and none starts or ends with a space', () => {
  for (const lang of LANGS) {
    for (const [key, value] of Object.entries(STR[lang])) {
      const shown = typeof value === 'function' ? value(3) : typeof value === 'object' ? Object.values(value).join('') : value;
      assert.ok(shown.length > 0, `${lang}/${key}`);
      assert.equal(shown, shown.trim(), `${lang}/${key}`);
    }
  }
  assert.equal(STR.zh['night.title'](3), '第 3 夜');
  assert.equal(STR.en['night.title'](3), 'Night 3');
});

test('every key that index.html names exists in both languages, and every key is used somewhere', () => {
  const named = new Set();
  for (const m of bare.matchAll(/\sdata-(?:i18n|i18n-alt|href)="([^"]+)"/g)) named.add(m[1]);
  assert.ok(named.size >= 25, 'the keys are found');
  for (const key of named) for (const lang of LANGS) assert.ok(Object.hasOwn(STR[lang], key), `${lang} has no ${key}`);
  // A key that a script uses is quoted there at least once besides its two definitions in strings.js.
  const code = scripts.map(([, text]) => text).join('\n');
  for (const key of Object.keys(STR.zh)) {
    const uses = code.split(`'${key}'`).length - 1;
    assert.ok(named.has(key) || uses >= 3, `${key} is not used`);
  }
});

test('an element with data-i18n holds no other element, so that its text can be replaced', () => {
  const leaves = [...bare.matchAll(/<(\w+)\b[^>]*\sdata-i18n="[^"]+"[^>]*>([\s\S]*?)<\/\1>/g)];
  assert.ok(leaves.length >= 20);
  for (const [whole, , inner] of leaves) assert.ok(!/</.test(inner), `${whole.slice(0, 60)} holds markup`);
});

test('textOf, pickLanguage and nameOf do what the page needs of them', () => {
  assert.equal(textOf('en', 'run.go'), 'Try it');
  assert.equal(textOf('zh', 'night.title', 2), '第 2 夜');
  assert.throws(() => textOf('en', 'no.such.key'), RangeError);
  assert.throws(() => textOf('fr', 'run.go'), RangeError);
  assert.equal(nameOf('en', 'bot:truthful', '老实人'), 'Truthful');
  assert.equal(nameOf('zh', 'bot:economist', 'x'), '经济学家');
  assert.equal(nameOf('en', 'glm-5', 'glm-5'), 'glm-5', 'a model keeps its name in both languages');
  assert.equal(nameOf('en', 'toString', 'label'), 'label', 'an id is never looked up on the prototype');
});

test('the language to open in: the stored choice, else the browser, else English', () => {
  const pick = (stored, languages) => pickLanguage({ stored, languages });
  assert.equal(pick('en', ['zh-CN']), 'en', 'what the reader chose wins');
  assert.equal(pick('zh', ['en-US']), 'zh');
  assert.equal(pick('fr', ['zh-CN']), 'zh', 'a stored value that is not a language is ignored');
  assert.equal(pick(null, ['zh-CN', 'en']), 'zh');
  assert.equal(pick(null, ['en-GB', 'zh-CN']), 'en', 'the first language of the list that the page has');
  assert.equal(pick(null, ['de-DE', 'zh-Hans-CN']), 'zh', 'languages the page lacks are skipped');
  assert.equal(pick(null, ['ZH_tw']), 'zh');
  assert.equal(pick(null, ['ja-JP']), 'en');
  assert.equal(pick(null, []), 'en');
  assert.equal(pick(undefined, undefined), 'en');
  assert.equal(pickLanguage(), 'en');
});

// The two links that depend on the language point into the repository.
const slug = (heading) => heading.trim().toLowerCase().replace(/[^\p{L}\p{N}\- ]/gu, '').replace(/ /g, '-');

function anchors(file) {
  const text = read(file).replace(/^ {0,3}(`{3,}|~{3,})[\s\S]*?^ {0,3}\1[ \t]*$/gm, '');
  return new Set([...text.matchAll(/^#{1,6}[ \t]+(.+?)[ \t]*#*$/gm)].map((m) => slug(m[1])));
}

test('the links that depend on the language are addresses of the repository that exist, anchors included', () => {
  const prefix = `${REPO_URL}/blob/main/`;
  for (const lang of LANGS) {
    for (const key of Object.keys(STR[lang]).filter((k) => k.startsWith('link.'))) {
      const address = STR[lang][key];
      assert.ok(address.startsWith(prefix), address);
      assert.ok(HREF_PATTERN.test(address), `${address} is refused by the link check of dom.js`);
      const [file, fragment] = address.slice(prefix.length).split('#');
      assert.ok(existsSync(join(REPO, file)), `${file} does not exist`);
      assert.equal(file.endsWith('_zh.md'), lang === 'zh', `${key} is in the language of the page`);
      if (fragment !== undefined) assert.ok(anchors(file).has(fragment), `${file} has no heading ${fragment}`);
    }
  }
  assert.ok(STR.en['link.try'].endsWith('README.md#try-it') && STR.zh['link.try'].endsWith('README_zh.md#试一试'));
});

test('the words of the cross-night summaries stay out of the home page', () => {
  const everything = [INDEX, css, ...scripts.map(([, text]) => text)].join('\n');
  assert.ok(!/效率|福利|PoA|welfare|efficiency/i.test(everything));
});

// -------------------------------------------------------------------- the numbers

test('a number of thousandths is written with one decimal, a real minus sign and no sign on zero', () => {
  assert.equal(tenthsText(23667), '23.7');
  assert.equal(tenthsText(23167), '23.2');
  assert.equal(tenthsText(22000), '22.0');
  assert.equal(tenthsText(0), '0.0');
  assert.equal(tenthsText(50), '0.1', 'a half rounds away from zero');
  assert.equal(tenthsText(-50), `${MINUS}0.1`);
  assert.equal(tenthsText(-49), '0.0', 'a value that rounds to zero has no sign');
  assert.equal(tenthsText(-19667), `${MINUS}19.7`);
  assert.equal(tenthsText(-158000), `${MINUS}158.0`);
  assert.equal(tenthsText(99950), '100.0');
  assert.equal(percentText(55), '5.5%');
  assert.equal(percentText(12), '1.2%');
  assert.equal(percentText(0), '0.0%');
  assert.equal(percentText(1000), '100.0%');
  assert.equal(replayHref('s1', 's1-d01-r0'), 'viewer/index.html?replay=../seasons/s1/s1-d01-r0/replay.json');
});

test('the leaderboard file gives eight rows in its order, and the six nights', () => {
  const board = parseBoard(leaderboard);
  assert.ok(board.ok);
  assert.equal(board.seasonId, 's1');
  assert.deepEqual(board.rows.map((row) => row.rank), [1, 2, 3, 4, 5, 6, 7, 8]);
  assert.deepEqual(board.rows.map((row) => row.kind), ['model', 'model', 'model', 'model', 'model', 'model', 'bot', 'bot']);
  assert.deepEqual(board.rows.slice(6).map((row) => row.id), ['bot:truthful', 'bot:economist']);
  assert.deepEqual(board.nights, ['s1-d01-r0', 's1-d01-r1', 's1-d01-r2', 's1-d01-r3', 's1-d01-r4', 's1-d01-r5']);
});

test('the table of the page says what the table in docs/seasons.md and in docs/seasons_zh.md says', () => {
  const { rows } = parseBoard(leaderboard);
  for (const [file, lang] of [['docs/seasons.md', 'en'], ['docs/seasons_zh.md', 'zh']]) {
    const table = seasonTable(file);
    assert.equal(table.length, rows.length, file);
    table.forEach(([rank, name, nights, mean, abstentions], i) => {
      const row = rows[i];
      assert.equal(Number(rank), row.rank, `${file} row ${i + 1}`);
      assert.equal(Number(nights), row.nights, `${file} row ${i + 1}`);
      assert.equal(mean, row.mean.replace(MINUS, '-'), `${file} row ${i + 1}`);
      assert.equal(abstentions, row.abstain, `${file} row ${i + 1}`);
      const shown = nameOf(lang, row.id, row.label);
      if (row.kind === 'model') assert.equal(name, shown);
      else assert.ok(name.toLowerCase().startsWith(shown.toLowerCase()), `${name} and ${shown}`);
    });
  }
});

test('the Chinese names of the bots are the labels that the leaderboard file gives them', () => {
  for (const entry of leaderboard.entries.filter((e) => e.kind === 'bot')) assert.equal(STR.zh['board.names'][entry.entry], entry.label);
  assert.deepEqual(Object.keys(STR.en['board.names']).sort(), leaderboard.entries.filter((e) => e.kind === 'bot').map((e) => e.entry).sort());
});

test('a file that is not the leaderboard of a season is refused, whatever is wrong with it', () => {
  const changed = (change) => {
    const raw = structuredClone(leaderboard);
    change(raw);
    return parseBoard(raw);
  };
  const refused = [
    ['another schema', (raw) => (raw.schema = 'auction-night/leaderboard@2')],
    ['no season', (raw) => delete raw.season],
    ['a season id with a slash', (raw) => (raw.season.id = '../x')],
    ['no entries', (raw) => (raw.entries = [])],
    ['entries that are not a list', (raw) => (raw.entries = {})],
    ['an entry without a name', (raw) => delete raw.entries[0].label],
    ['a kind that is neither model nor bot', (raw) => (raw.entries[0].kind = 'human')],
    ['a rank of zero', (raw) => (raw.entries[0].rank = 0)],
    ['a mean that is not a whole number', (raw) => (raw.entries[0].mean_milli = 32.5)],
    ['a mean that is not a number', (raw) => (raw.entries[0].mean_milli = '32500')],
    ['a rate above a thousand', (raw) => (raw.entries[0].abstain_rate_milli = 1001)],
    ['no nights', (raw) => (raw.nights = [])],
    ['a night with a path in its id', (raw) => (raw.nights[0].night = '../../x')],
    ['a night with a query in its id', (raw) => (raw.nights[0].night = 'a?b=c')],
    ['a night that is not text', (raw) => (raw.nights[0].night = 7)],
  ];
  for (const [what, change] of refused) {
    const result = changed(change);
    assert.deepEqual(result, { ok: false, reason: 'shape' }, what);
  }
  for (const raw of [null, undefined, 'text', 7, [], {}]) assert.equal(parseBoard(raw).ok, false);
});

test('loading asks for one file of the site and says why it failed when it cannot be read', async () => {
  const asked = [];
  const respond = answer.json(leaderboard);
  const ok = await loadBoard(async (url, options) => {
    asked.push([url, options]);
    return respond(url, options);
  });
  assert.equal(ok.ok, true);
  assert.deepEqual(asked, [[BOARD_URL, { credentials: 'omit', cache: 'no-cache', referrerPolicy: 'no-referrer' }]]);
  assert.equal(BOARD_URL, 'seasons/s1/leaderboard.json');
  assert.ok(existsSync(join(REPO, BOARD_URL)));
  assert.deepEqual(await loadBoard(answer.network()), { ok: false, reason: 'network' });
  assert.deepEqual(await loadBoard(answer.status(404)), { ok: false, reason: 'status' });
  assert.deepEqual(await loadBoard(answer.garbage()), { ok: false, reason: 'json' });
  assert.deepEqual(await loadBoard(answer.json({ schema: 'something else' })), { ok: false, reason: 'shape' });
});

// ------------------------------------------------------------------------ the style sheet

/** The text between the braces of the block that opens at `start` (the index of its "{"). */
function block(text, start) {
  let depth = 0;
  for (let i = start; i < text.length; i += 1) {
    if (text[i] === '{') depth += 1;
    if (text[i] === '}') {
      depth -= 1;
      if (depth === 0) return text.slice(start + 1, i);
    }
  }
  throw new Error('unbalanced braces');
}

/** The body of an @media block, found by its condition. */
function media(text, condition) {
  const at = text.indexOf(`@media ${condition} {`);
  assert.ok(at >= 0, `no @media ${condition}`);
  return block(text, text.indexOf('{', at));
}

/** [{selectors, body}] of the rules of a block that holds no further blocks. */
function rules(text) {
  return [...text.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map((m) => ({
    selectors: m[1].replace(/\/\*[\s\S]*?\*\//g, '').split(',').map((s) => s.trim()).filter(Boolean),
    body: m[2],
  }));
}

/** The colour tokens of a style sheet: the dark ones on :root, the light ones under prefers-color-scheme. */
function tokens(text) {
  const pairs = (body) => new Map([...body.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]));
  return {
    dark: pairs(block(text, text.indexOf('{', text.indexOf(':root {')))),
    light: pairs(media(text, '(prefers-color-scheme: light)')),
  };
}

test('the colours and the font are those of the replay page, in the dark and in the light theme', () => {
  const home = tokens(css);
  const viewer = tokens(viewerCss);
  const shared = ['--bg', '--panel', '--panel-2', '--ink', '--muted', '--line', '--accent', '--accent-ink', '--gain', '--loss'];
  for (const theme of ['dark', 'light']) {
    for (const name of shared) {
      assert.ok(viewer[theme].has(name), `the replay page has ${name}`);
      assert.equal(home[theme].get(name), viewer[theme].get(name), `${theme} ${name}`);
    }
    assert.ok(home[theme].has('--accent-text'), `${theme} --accent-text`);
  }
  const font = (text) => /(?:^|\n)body\s*\{[^}]*\bfont:\s*([^;]+);/.exec(text)[1];
  assert.equal(font(css), font(viewerCss));
});

test('the style sheet loads nothing and runs no script', () => {
  assert.ok(!/@import/i.test(css));
  assert.ok(!/url\s*\(/i.test(css));
  assert.ok(!/expression\s*\(|behavior\s*:|-moz-binding/i.test(css));
});

test('below 720 px every part is one column, and nothing asks for a width that a 320 px phone lacks', () => {
  assert.match(media(css, '(max-width: 720px)'), /\.cards\s*\{\s*grid-template-columns:\s*minmax\(0,\s*1fr\);/);
  const widths = [...css.matchAll(/(?<![\w-])(?:min-)?width:\s*(\d+(?:\.\d+)?)px/g)].map((m) => Number(m[1]));
  for (const value of widths) assert.ok(value <= 296, `a width of ${value}px does not fit a 320 px phone`);
  for (const match of css.matchAll(/minmax\(\s*(\d+)px/g)) assert.ok(Number(match[1]) <= 296, `a column of ${match[1]}px`);
  assert.match(css, /\.wrap\s*\{[^}]*max-width:\s*960px[^}]*padding:\s*0 16px/);
  assert.match(css, /\.stage img\s*\{[^}]*width:\s*100%;[^}]*height:\s*auto/);
});

test('on a phone, a short window and any screen used with a finger, controls are at least 44 px high and wide', () => {
  const touch = rules(media(css, '(max-width: 640px), (max-height: 520px), (pointer: coarse)'));
  const high = touch.filter((rule) => /min-height:\s*44px/.test(rule.body)).flatMap((rule) => rule.selectors);
  const wide = touch.filter((rule) => /min-width:\s*44px/.test(rule.body)).flatMap((rule) => rule.selectors);
  for (const selector of ['.lang-btn', '.btn', '.card', '.foot a']) assert.ok(high.includes(selector), `${selector} is 44 px high`);
  for (const selector of ['.lang-btn', '.btn']) assert.ok(wide.includes(selector), `${selector} is 44 px wide`);
  assert.match(css, /a:focus-visible,\s*button:focus-visible\s*\{\s*outline:\s*2px solid var\(--accent\)/);
});

test('long text breaks instead of widening the page, and a Chinese heading of the table stays whole', () => {
  assert.ok(rules(css).some((rule) => rule.selectors.includes('.board .who') && /overflow-wrap:\s*anywhere/.test(rule.body)));
  assert.ok(rules(css).some((rule) => rule.selectors.includes('.lead') && /word-break:\s*keep-all/.test(rule.body)));
  assert.ok(rules(css).some((rule) => rule.selectors.includes('.board thead th') && /word-break:\s*keep-all/.test(rule.body)));
});

// ------------------------------------------------------------------------ the scripts

// Every rule has a sample that it must catch, so that a typo cannot make a rule pass silently.
const RULES = [
  ['innerHTML', /innerHTML/, 'el.innerHTML = x'],
  ['outerHTML', /outerHTML/, 'el.outerHTML = x'],
  ['insertAdjacentHTML', /insertAdjacentHTML/, "el.insertAdjacentHTML('beforeend', x)"],
  ['document.write', /document\.write/, 'document.write(x)'],
  ['eval', /\beval\s*\(/, 'eval(x)'],
  ['new Function', /new\s+Function\b/, 'new Function(x)'],
  ['string timers', /\bset(?:Timeout|Interval)\s*\(\s*['"`]/, 'setTimeout("x()", 1)'],
  ['javascript: URL', /javascript:/i, 'href = "javascript:x"'],
  ['srcdoc', /\bsrcdoc\b/, 'frame.srcdoc = x'],
  ['cookies and other storage', /\b(?:document\.cookie|sessionStorage|indexedDB)\b/, 'sessionStorage.setItem(a, b)'],
  ['other network APIs', /\b(?:XMLHttpRequest|WebSocket|EventSource|sendBeacon|importScripts|serviceWorker)\b/, 'new WebSocket(u)'],
  ['direct fetch call', /\bfetch\s*\(/, 'fetch(url)'],
  ['dynamic import', /\bimport\s*\(/, 'import(x)'],
];

test('the lint rules catch what they are meant to catch', () => {
  for (const [name, pattern, sample] of RULES) assert.match(sample, pattern, name);
});

test('the site folder holds scripts and one style sheet, and the scripts that the page needs', () => {
  const names = [...source.keys()];
  for (const name of ['board.js', 'dom.js', 'home.css', 'main.js', 'page.js', 'strings.js']) assert.ok(names.includes(name), name);
  for (const name of names) assert.match(name, /\.(?:js|css)$/, `unexpected file ${name}`);
});

for (const [name, pattern] of RULES) {
  test(`no file of the home page matches: ${name}`, () => {
    for (const [file, text] of [...source, ['index.html', bare]]) assert.ok(!pattern.test(text), `${file} matches ${pattern}`);
  });
}

test('scripts import only files of the site folder, and every import resolves', () => {
  for (const [file, text] of scripts) {
    for (const match of text.matchAll(/^\s*(?:import|export)\b[^'"]*?\bfrom\s*['"]([^'"]+)['"]/gm)) {
      assert.match(match[1], /^\.\//, `${file} imports ${match[1]}`);
      assert.ok(existsSync(resolve(dirname(join(SITE, file)), match[1])), `${file}: ${match[1]} does not exist`);
    }
    assert.ok(!/\brequire\s*\(/.test(text), `${file} uses require`);
  }
});

test('fetch is named in the leaderboard loader and the wiring only', () => {
  assert.deepEqual(scripts.filter(([, text]) => /\bfetch\b/.test(text)).map(([file]) => file).sort(), ['board.js', 'main.js']);
});

test('the storage is touched in main.js only, and every touch sits in a try block', () => {
  for (const [file, text] of scripts) assert.equal(/\blocalStorage\b/.test(text), file === 'main.js', file);
  const main = source.get('main.js').replace(/\/\/.*$/gm, ''); // the code without its comments
  const touches = [...main.matchAll(/\blocalStorage\b/g)].map((m) => m.index);
  assert.ok(touches.length >= 2, 'it reads and writes');
  for (const at of touches) {
    const start = main.lastIndexOf('try {', at);
    assert.ok(start >= 0, 'a touch outside any try block');
    let depth = 1;
    for (const ch of main.slice(start + 'try {'.length, at)) depth += ch === '{' ? 1 : ch === '}' ? -1 : 0;
    assert.ok(depth > 0, 'a touch after the end of its try block');
  }
});

test('web addresses appear only in strings.js, and every one of them is an address of the repository', () => {
  for (const [file, text] of scripts) {
    const addresses = text.match(/https?:\/\/[^\s'"`)]*/g) ?? [];
    if (file !== 'strings.js') assert.deepEqual(addresses, [], file);
  }
  const literal = source.get('strings.js').match(/https?:\/\/[^\s'"`)]*/g) ?? [];
  assert.deepEqual(literal, [REPO_URL.replace('auction-night', 'auction-night/blob/main/')]);
});

test('the home page names no local path, and no file of it holds a control or an invisible character', () => {
  for (const [file, text] of [...source, ['index.html', INDEX]]) {
    assert.ok(!/(?:^|[^\w./~-])\/(?:data|home)\//.test(text), `${file} names a local path`);
    assert.ok(!/[\w.+-]+@[\w-]+\.[\w.]+/.test(text), `${file} holds an e-mail address`);
    const found = [...text.replace(/[\n\t]/g, '')].find((ch) => INVISIBLE.test(ch));
    assert.equal(found, undefined, `${file} holds U+${found?.codePointAt(0).toString(16).toUpperCase().padStart(4, '0')}`);
  }
});
