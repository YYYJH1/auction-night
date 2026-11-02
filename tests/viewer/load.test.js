import assert from 'node:assert/strict';
import test from 'node:test';

import { fetchReplayText, parseReplay, readFileText, ReplayError } from '../../viewer/lib/load.js';
import { MAX_FILE_CHARS } from '../../viewer/lib/replay.js';
import { STR } from '../../viewer/lib/strings.js';
import { fixture, SEALED } from './helpers.js';

const PAGE = 'https://example.org/auction/viewer/index.html';

function failure(run) {
  return assert.rejects(run, (error) => error instanceof ReplayError);
}

test('a good replay text becomes a model', () => {
  const model = parseReplay(JSON.stringify(fixture(SEALED)));
  assert.equal(model.night.id, 'fixture-sealed-0001');
  assert.deepEqual(model.lotNos, [1, 2, 3, 4]);
});

test('text that is not JSON is refused in words', () => {
  for (const text of ['', '{', 'not json', '{"a":1,}', '<html>']) {
    assert.throws(() => parseReplay(text), (e) => e instanceof ReplayError && e.messages[0] === STR.err.notJson, text);
  }
});

test('JSON that is not a replay says why', () => {
  assert.throws(() => parseReplay('[]'), (e) => e.messages[0] === STR.err.notObject);
  assert.throws(() => parseReplay('{}'), (e) => e.messages[0] === STR.err.noSchema);
  const broken = fixture(SEALED);
  broken.night.n_lots = 0;
  assert.throws(() => parseReplay(JSON.stringify(broken)), (e) => e.messages.some((m) => m.includes('night.n_lots')));
});

test('a text that is too long is refused before it is parsed', () => {
  assert.throws(() => parseReplay('x'.repeat(MAX_FILE_CHARS + 1)), (e) => e.messages[0] === STR.err.tooLarge);
  assert.throws(() => parseReplay(null), (e) => e.messages[0] === STR.err.tooLarge);
});

test('a replay file is fetched from the same site, without credentials or a referrer', async () => {
  const calls = [];
  const stub = async (url, options) => {
    calls.push([url, options]);
    return { ok: true, status: 200, url, text: async () => 'body' };
  };
  assert.equal(await fetchReplayText('../replays/n1.json', PAGE, stub), 'body');
  assert.deepEqual(calls, [['https://example.org/auction/replays/n1.json', { credentials: 'omit', cache: 'no-cache', referrerPolicy: 'no-referrer' }]]);
});

test('an address that leaves the site is refused without any request', async () => {
  let called = 0;
  const stub = async () => {
    called += 1;
    return { ok: true, status: 200, text: async () => '' };
  };
  for (const param of ['https://evil.example/x.json', '//evil.example/x.json', 'javascript:alert(1)', 'data:application/json,{}', '../README.md', '']) {
    await failure(() => fetchReplayText(param, PAGE, stub));
  }
  assert.equal(called, 0);
});

test('a failed request, a bad status and a redirect to another site are reported', async () => {
  await assert.rejects(
    () => fetchReplayText('n.json', PAGE, async () => { throw new TypeError('network down'); }),
    (e) => e.messages[0] === STR.err.network,
  );
  await assert.rejects(
    () => fetchReplayText('n.json', PAGE, async () => ({ ok: false, status: 404, text: async () => '' })),
    (e) => e.messages[0] === STR.err.fetchFailed(404),
  );
  await assert.rejects(
    () => fetchReplayText('n.json', PAGE, async () => ({ ok: true, status: 200, url: 'https://evil.example/n.json', text: async () => 'x' })),
    (e) => e.messages[0] === STR.err.cross,
  );
  await assert.rejects(
    () => fetchReplayText('n.json', PAGE, async () => ({ ok: true, status: 200, url: PAGE, text: async () => { throw new Error('reset'); } })),
    (e) => e.messages[0] === STR.err.network,
  );
});

test('a chosen file is read as text, unless it is absurdly large', async () => {
  assert.equal(await readFileText({ size: 10, text: async () => 'hello' }), 'hello');
  await failure(() => readFileText({ size: MAX_FILE_CHARS * 4 + 1, text: async () => 'x' }));
  await failure(() => readFileText({ size: 1, text: async () => { throw new Error('gone'); } }));
});

test('a ReplayError carries its lines', () => {
  const error = new ReplayError(['a', 'b']);
  assert.deepEqual(error.messages, ['a', 'b']);
  assert.equal(error.message, 'a b');
  assert.equal(error.name, 'ReplayError');
});
