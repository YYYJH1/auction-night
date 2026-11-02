import assert from 'node:assert/strict';
import test from 'node:test';

import { formatHash, formatMomentHash, formatPageHash, PAGES, parseHash, replayParam, resolveReplayUrl } from '../../viewer/lib/route.js';

test('a lot link round-trips', () => {
  const hash = formatHash('fixture-sealed-0001', 7);
  assert.equal(hash, '#/n/fixture-sealed-0001/lot/7');
  assert.deepEqual(parseHash(hash), { night: 'fixture-sealed-0001', view: 'lot', lot: 7, moment: null });
});

test('night ids with awkward characters round-trip and stay inside the link alphabet', () => {
  for (const id of ['s1 d03/r2', '夜 1', 'a(b)*c!\'d', '100%', '#hash', 'a?b=c&d', '..', 'x'.repeat(64)]) {
    const hash = formatHash(id, 3);
    assert.match(hash, /^#\/n\/[\w%.~-]+\/lot\/3$/, id);
    assert.deepEqual(parseHash(hash), { night: id, view: 'lot', lot: 3, moment: null }, id);
  }
});

test('a night link without a lot, and a moment link', () => {
  assert.deepEqual(parseHash('#/n/abc'), { night: 'abc', view: null, lot: null, moment: null });
  assert.deepEqual(parseHash('#/n/abc/moment/12'), { night: 'abc', view: 'moment', lot: null, moment: 12 });
});

test('the pages of a night have links of their own', () => {
  assert.deepEqual([...PAGES], ['recap', 'highlights', 'awards', 'review']);
  for (const page of PAGES) {
    const hash = formatPageHash('fixture-sealed-0001', page);
    assert.equal(hash, `#/n/fixture-sealed-0001/${page}`);
    assert.deepEqual(parseHash(hash), { night: 'fixture-sealed-0001', view: page, lot: null, moment: null });
  }
  assert.throws(() => formatPageHash('abc', 'lot'), RangeError);
  assert.throws(() => formatPageHash('abc', '../x'), RangeError);
});

test('a highlight link round-trips, and awkward night ids stay inside the link alphabet', () => {
  assert.equal(formatMomentHash('abc', 7), '#/n/abc/moment/7');
  for (const id of ['s1 d03/r2', '夜 1', "a(b)*c!'d", '100%', '#hash', '..']) {
    for (const hash of [formatMomentHash(id, 12), formatPageHash(id, 'awards')]) {
      assert.match(hash, /^#\/n\/[\w%.~-]+\/(?:moment\/12|awards)$/, id);
      assert.equal(parseHash(hash).night, id, id);
    }
  }
});

test('fragments that are not ours are refused', () => {
  for (const bad of [
    '',
    '#',
    '#/',
    '#/n',
    '#/n/',
    '#/x/y',
    '#/n/abc/lot',
    '#/n/abc/lot/',
    '#/n/abc/lot/0',
    '#/n/abc/lot/01',
    '#/n/abc/lot/-1',
    '#/n/abc/lot/1.5',
    '#/n/abc/lot/abc',
    '#/n/abc/lot/1/extra',
    '#/n/abc/lot/1234',
    '#/n/abc/page/1',
    '#/n/abc/recap/1',
    '#/n/abc/awards/',
    '#/n/abc/Awards',
    '#/n/abc/review/',
    '#/n/abc/Review',
    '#/n/abc/review/1',
    '#/n/abc/highlights/3',
    '#/n/abc/moment',
    '#/n/abc/moment/0',
    '#/n/abc/moment/1234',
    '#/n/abc/recap/lot/1',
    '#/n/%E0%A4%A/lot/1',
    '#/n//lot/1',
    'n/abc/lot/1',
    `#/n/${'x'.repeat(500)}/lot/1`,
    null,
    undefined,
    12,
  ]) {
    assert.equal(parseHash(bad), null, String(bad).slice(0, 40));
  }
});

test('the replay parameter', () => {
  assert.equal(replayParam('?replay=a/b.json'), 'a/b.json');
  assert.equal(replayParam('?x=1&replay=a.json'), 'a.json');
  assert.equal(replayParam('?replay='), null);
  assert.equal(replayParam(''), null);
  assert.equal(replayParam('?other=1'), null);
});

const PAGE = 'https://example.org/auction/viewer/index.html?replay=x#/n/a/lot/1';

test('relative paths on the same site are accepted', () => {
  for (const [param, expected] of [
    ['night.json', 'https://example.org/auction/viewer/night.json'],
    ['./night.json', 'https://example.org/auction/viewer/night.json'],
    ['../replays/n1.json', 'https://example.org/auction/replays/n1.json'],
    ['/replays/n1.json', 'https://example.org/replays/n1.json'],
    ['a/b/c.JSON', 'https://example.org/auction/viewer/a/b/c.JSON'],
    ['../replays/n 1.json', 'https://example.org/auction/replays/n%201.json'],
    ['night.json?v=2', 'https://example.org/auction/viewer/night.json?v=2'],
  ]) {
    assert.deepEqual(resolveReplayUrl(param, PAGE), { ok: true, url: expected }, param);
  }
});

test('anything that leaves the site or is not a json file is refused', () => {
  for (const [param, reason] of [
    ['', 'empty'],
    [null, 'empty'],
    [undefined, 'empty'],
    [42, 'empty'],
    ['https://evil.example/x.json', 'scheme'],
    ['http://example.org/x.json', 'scheme'],
    ['//evil.example/x.json', 'network-path'],
    ['javascript:alert(1)', 'scheme'],
    ['JavaScript:alert(1)', 'scheme'],
    ['data:application/json,{}', 'scheme'],
    ['blob:https://example.org/uuid', 'scheme'],
    ['file:///etc/passwd', 'scheme'],
    ['ftp://example.org/x.json', 'scheme'],
    ['https:evil.example/x.json', 'scheme'],
    ['a:b.json', 'scheme'],
    ['\\\\evil.example\\x.json', 'characters'],
    ['/\\evil.example/x.json', 'characters'],
    ['a\nb.json', 'characters'],
    ['a\tb.json', 'characters'],
    ['x\x00.json', 'characters'],
    ['../README.md', 'extension'],
    ['night.json.exe', 'extension'],
    ['night', 'extension'],
    ['x'.repeat(400) + '.json', 'long'],
  ]) {
    const result = resolveReplayUrl(param, PAGE);
    assert.equal(result.ok, false, String(param));
    assert.equal(result.reason, reason, String(param));
  }
});

test('a leading space cannot smuggle in a scheme', () => {
  const result = resolveReplayUrl(' javascript:alert(1)', PAGE);
  assert.equal(result.ok, false);
});

test('a page that is not served over http or https reads no replay by address', () => {
  const result = resolveReplayUrl('night.json', 'file:///srv/site/viewer/index.html');
  assert.equal(result.ok, false);
  assert.equal(result.reason, 'protocol');
});

test('a base address that cannot be parsed is refused', () => {
  assert.equal(resolveReplayUrl('night.json', 'not a url').ok, false);
});
