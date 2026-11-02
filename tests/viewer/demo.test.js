// The replays in docs/demo are the ones that the READMEs show. The replay page must keep reading
// them. The same files are checked against the contract in tests/test_demo_replays.py.

import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import test from 'node:test';

import { parseReplay } from '../../viewer/lib/load.js';
import { REPO } from './helpers.js';

const DEMO = `${REPO}docs/demo/`;
const files = readdirSync(DEMO).filter((name) => name.endsWith('.json')).sort();

test('the demo folder holds a replay', () => {
  assert.ok(files.length > 0);
});

for (const name of files) {
  test(`the replay page reads docs/demo/${name}`, () => {
    const model = parseReplay(readFileSync(`${DEMO}${name}`, 'utf8'));
    assert.equal(model.night.id, name.replace(/\.json$/, ''));
    assert.ok(model.lotNos.length > 0);
  });
}
