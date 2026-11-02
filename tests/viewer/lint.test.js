// The viewer shows text written by language models. These tests read the source of the viewer
// and fail when it could turn that text into markup or reach beyond its own site.

import assert from 'node:assert/strict';
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import test from 'node:test';

import { VIEWER } from './helpers.js';

const EXPECTED_CSP =
  "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'";

function* files(dir) {
  for (const name of readdirSync(dir).sort()) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) yield* files(path);
    else yield path;
  }
}

const all = [...files(VIEWER)];
const source = new Map(all.map((path) => [relative(VIEWER, path), readFileSync(path, 'utf8')]));
const scripts = [...source].filter(([name]) => name.endsWith('.js'));

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
  ['cookies and storage', /\b(?:document\.cookie|localStorage|sessionStorage|indexedDB)\b/, 'localStorage.setItem(a, b)'],
  ['other network APIs', /\b(?:XMLHttpRequest|WebSocket|EventSource|sendBeacon|importScripts|serviceWorker)\b/, 'new WebSocket(u)'],
  ['direct fetch call', /\bfetch\s*\(/, 'fetch(url)'],
  ['dynamic import', /\bimport\s*\(/, 'import(x)'],
  ['absolute web address', /https?:\/\//i, 'https://example.org/x'],
];

test('the lint rules catch what they are meant to catch', () => {
  for (const [name, pattern, sample] of RULES) assert.match(sample, pattern, name);
});

test('the viewer folder holds the files we expect and nothing else', () => {
  const names = [...source.keys()];
  assert.ok(names.includes('index.html') && names.includes('app.js') && names.includes('style.css'));
  for (const name of names) assert.match(name, /\.(?:html|js|css|json)$/, `unexpected file ${name}`);
  assert.ok(scripts.length >= 15, 'the scripts are found');
});

for (const [name, pattern] of RULES) {
  test(`no file of the viewer matches: ${name}`, () => {
    for (const [file, text] of source) {
      assert.ok(!pattern.test(text), `${file} matches ${pattern}`);
    }
  });
}

test('the page markup is never built from strings: the four DOM calls that parse markup are absent', () => {
  // The same four names as the lint rules above, spelled out as the task states them.
  const names = ['inner' + 'HTML', 'outer' + 'HTML', 'insertAdjacent' + 'HTML', 'document.' + 'write'];
  for (const [file, text] of source) for (const name of names) assert.ok(!text.includes(name), `${file} contains ${name}`);
});

test('scripts import only files of the viewer, and every import resolves', () => {
  for (const [file, text] of scripts) {
    for (const match of text.matchAll(/^\s*(?:import|export)\b[^'"]*?\bfrom\s*['"]([^'"]+)['"]/gm)) {
      const specifier = match[1];
      assert.match(specifier, /^\.\.?\//, `${file} imports ${specifier}`);
      assert.ok(existsSync(resolve(dirname(join(VIEWER, file)), specifier)), `${file}: ${specifier} does not exist`);
    }
    assert.ok(!/\brequire\s*\(/.test(text), `${file} uses require`);
  }
});

test('fetch is named in the loader and the page only', () => {
  const mentions = scripts.filter(([, text]) => /\bfetch\b/.test(text)).map(([file]) => file).sort();
  assert.deepEqual(mentions, ['app.js', 'lib/load.js']);
});

test('index.html declares exactly the specified content security policy', () => {
  const html = source.get('index.html');
  const metas = [...html.matchAll(/<meta\s+http-equiv="Content-Security-Policy"\s+content="([^"]*)"/g)];
  assert.equal(metas.length, 1);
  assert.equal(metas[0][1], EXPECTED_CSP);
  assert.ok(html.indexOf('Content-Security-Policy') < html.indexOf('<script'), 'declared before any script');
});

test('index.html has no inline script or style and no event handler attributes', () => {
  const html = source.get('index.html').replace(/<!--[\s\S]*?-->/g, '');
  const scriptTags = [...html.matchAll(/<script\b[^>]*>/g)].map((m) => m[0]);
  assert.deepEqual(scriptTags, ['<script type="module" src="app.js">']);
  assert.ok(!/<script\b[^>]*>[^<\s]/.test(html), 'a script tag with a body');
  assert.ok(!/<style\b/i.test(html), 'a style element');
  assert.ok(!/\sstyle\s*=/i.test(html), 'a style attribute');
  assert.ok(!/\son[a-z]+\s*=/i.test(html), 'an event handler attribute');
  assert.ok(!/<(?:iframe|object|embed|base|form|img|svg|link\s+rel="(?!stylesheet))/i.test(html));
  assert.deepEqual([...html.matchAll(/<link\b[^>]*>/g)].map((m) => m[0]), ['<link rel="stylesheet" href="style.css">']);
  assert.match(html, /<html lang="zh-CN">/);
});

test('index.html has a header comment that says what the page is and how to open it', () => {
  const html = source.get('index.html');
  assert.match(html, /^<!doctype html>\s*<!--/i);
  const comment = html.match(/<!--([\s\S]*?)-->/)[1];
  for (const part of ['replay viewer', '?replay=', '#/n/', 'file://', 'text nodes']) assert.ok(comment.includes(part), part);
});

test('style.css loads nothing and runs no script', () => {
  const css = source.get('style.css');
  assert.ok(!/@import/i.test(css));
  assert.ok(!/url\s*\(/i.test(css));
  assert.ok(!/expression\s*\(|behavior\s*:|-moz-binding/i.test(css));
});

test('the package files only say that the folder holds modules', () => {
  for (const name of ['package.json']) {
    assert.deepEqual(JSON.parse(source.get(name)), { private: true, type: 'module' });
  }
});

test('the viewer names no local path', () => {
  for (const [file, text] of source) {
    assert.ok(!/\/(?:data|home)\//.test(text), `${file} names a local path`);
  }
});

test('no file of the viewer, its tests or its fixtures holds a local path or an address', () => {
  const tests = join(VIEWER, '..', 'tests');
  const named = [...source].map(([name, text]) => [`viewer/${name}`, text]);
  for (const folder of [join(tests, 'viewer'), join(tests, 'fixtures')]) {
    for (const path of files(folder)) named.push([relative(join(VIEWER, '..'), path), readFileSync(path, 'utf8')]);
  }
  named.push(['tests/test_replay_fixtures.py', readFileSync(join(tests, 'test_replay_fixtures.py'), 'utf8')]);
  assert.ok(named.length > 40);
  for (const [name, text] of named) {
    assert.ok(!/(?:^|[^\w./~-])\/(?:data|home)\//.test(text), `${name} holds a local path`);
    assert.ok(!/[\w.+-]+@[\w-]+\.[\w.]+/.test(text.replace(/noreply@[\w.-]+/g, '')), `${name} holds an e-mail address`);
  }
});

test('elements and text nodes are made in lib/dom.js only, so every piece of text goes through it', () => {
  for (const [file, text] of scripts) {
    if (file === 'lib/dom.js') continue;
    assert.ok(!/\bcreateElement\w*\s*\(/.test(text), `${file} creates an element itself`);
    assert.ok(!/\bcreateTextNode\s*\(/.test(text), `${file} creates a text node itself`);
    assert.ok(!/\.textContent\s*=[^=]/.test(text), `${file} assigns textContent itself`);
    assert.ok(!/\.setAttribute\s*\(/.test(text), `${file} sets an attribute itself`);
  }
  const dom = source.get('lib/dom.js');
  assert.ok(/createTextNode/.test(dom) && /createElement/.test(dom));
});

test('no source file of the viewer or of its tests holds a control or an invisible character', () => {
  // Zero-width and direction marks in a source file hide what the code does, and the Write and
  // Edit tools have turned escapes into raw characters before. Escapes and String.fromCodePoint
  // are the way to name such a character. Line feed and tab are the only controls allowed.
  const tests = join(VIEWER, '..', 'tests', 'viewer');
  const named = [...source].map(([name, text]) => [`viewer/${name}`, text]);
  for (const path of files(tests)) named.push([`tests/viewer/${relative(tests, path)}`, readFileSync(path, 'utf8')]);
  assert.ok(named.length > 40);
  for (const [name, text] of named) {
    const found = [...text.replace(/[\n\t]/g, '')].find((ch) => /[\p{Cc}\p{Cf}\p{Cs}\u2028\u2029]/u.test(ch));
    assert.equal(found, undefined, `${name} holds U+${found?.codePointAt(0).toString(16).toUpperCase().padStart(4, '0')}`);
  }
});

test('the overlay and the clocks read only their arguments: no storage, no address, no document', () => {
  for (const [file, text] of scripts) {
    if (!/^(?:anim\/(?:english|dutch)(?:_view)?|lib\/overlay|ui\/overlay)\.js$/.test(file)) continue;
    assert.ok(!/\blocalStorage\b|\bnavigator\b|\blocation\b|\bdocument\./.test(text), `${file} reaches outside its arguments`);
  }
});
