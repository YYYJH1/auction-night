// Shared by the viewer tests: the replay fixtures, and a payload for every kind of attack.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { indexReplay } from '../../viewer/lib/replay.js';

export const REPO = fileURLToPath(new URL('../../', import.meta.url));
export const VIEWER = fileURLToPath(new URL('../../viewer/', import.meta.url));

export function loadFixture(name) {
  const url = new URL(`../fixtures/replays/${name}`, import.meta.url);
  return JSON.parse(readFileSync(url, 'utf8'));
}

export const SEALED = 'sealed_four_lots.json';
export const CLOCKS = 'english_dutch.json';
// Real output of the exporter: the mock night of twelve lots (see real_night.test.js).
export const MOCK = 'mock_night.json';

/** A fresh deep copy of a fixture, free to be changed by the test. */
export function fixture(name) {
  return structuredClone(loadFixture(name));
}

export function modelOf(name) {
  return indexReplay(loadFixture(name));
}

/** The model of a replay that the test has changed. */
export function indexModel(raw) {
  return indexReplay(raw);
}

/**
 * A night of twelve lots made of the four lots of the sealed night, one after the other, with one
 * highlight on each lot whose score is the matching entry of `scores`. A highlight spans the
 * whole lot, so every segment of the three-minute version is as long as a segment can be.
 */
export function twelveLotReplay(scores = [9, 8, 7, 7, 6, 6, 5, 5, 4, 3, 2, 1]) {
  const raw = fixture(SEALED);
  const { timeline, catalog } = raw;
  raw.night.n_lots = 12;
  raw.timeline = [];
  raw.catalog = [];
  for (let k = 1; k <= 12; k += 1) {
    const source = structuredClone(timeline[(k - 1) % 4]);
    const span = source.last_seq - source.first_seq;
    const base = 1000 * k;
    source.actions = source.actions.map((a) => ({ ...a, seq: base + (a.seq - source.first_seq) }));
    raw.timeline.push({ ...source, lot: k, first_seq: base, last_seq: base + span });
    raw.catalog.push({ ...structuredClone(catalog[(k - 1) % 4]), lot: k });
  }
  raw.highlights = scores.map((score, i) => ({
    id: i + 1,
    category: 'backstab',
    score,
    lot: i + 1,
    seats: [1],
    first_seq: 1000 * (i + 1),
    last_seq: 1000 * (i + 1) + 10,
    text: `moment ${i + 1}`,
  }));
  return raw;
}

/** Text that must stay text wherever it lands on the page. */
export const PAYLOADS = Object.freeze({
  image: '<img src=x onerror=alert(1)>',
  script: '<script>alert(document.cookie)</script>',
  handler: '<b onmouseover=alert(1)>x</b>',
  jsLink: 'javascript:alert(1)',
  anchor: '<a href="javascript:alert(1)">click</a>',
  markdown: '[click](javascript:alert(1))',
  richLink: '[link=https://evil.example/x]claim[/link]',
  richStyle: '[bold red]alarm[/bold red]',
  ansi: '\u001b[31mred\u001b[0m',
  osc: '\u001b]8;;https://evil.example\u0007link\u001b]8;;\u0007',
  bidi: `abc${String.fromCharCode(0x202e)}def`,
  invisible: ['a', 0x200b, 'b', 0x200e, 'c', 0xfeff, 'd', 0x061c, 'e', 0x2060, 'f'].map((x) => (typeof x === 'number' ? String.fromCharCode(x) : x)).join(''),
  quote: '"><svg onload=alert(1)>',
  entity: '&lt;b&gt;&amp;',
});

/** Replace every string that the viewer shows from the replay with `text`. */
export function poison(replay, text) {
  const set = (obj, keys) => {
    for (const key of keys) if (key in obj) obj[key] = text;
  };
  set(replay.night, ['id']);
  for (const seat of replay.seats) {
    set(seat, ['codename', 'vendor', 'endpoint', 'model', 'model_returned', 'persona', 'policy', 'thinking', 'effort']);
  }
  for (const entry of replay.catalog) set(entry, ['item', 'name', 'emoji', 'tag', 'blurb']);
  for (const lot of replay.timeline) {
    set(lot, ['host']);
    for (const talk of lot.talk) talk.text = text;
  }
  for (const thought of replay.reveal.thoughts) thought.text = text;
  for (const plan of replay.reveal.plans) plan.persona = text;
  for (const mission of replay.reveal.missions) set(mission, ['tag']);
  for (const line of replay.host.per_lot) line.text = text;
  if (replay.host.recap) replay.host.recap = text;
  for (const item of replay.host.prediction_card?.items ?? []) set(item, ['claim', 'text']);
  for (const light of replay.highlights ?? []) light.text = text;
  for (const verdict of replay.verdicts ?? []) {
    verdict.quotes = (verdict.quotes ?? []).map(() => text);
    set(verdict, ['detail']);
  }
  for (const record of replay.pledges ?? []) set(record, ['detail']);
  for (const absence of replay.judges_absent ?? []) set(absence, ['vendor']);
  return replay;
}

/**
 * True if a string holds a control character, a line separator, a lone surrogate or a format
 * character (zero-width and direction marks, bidirectional overrides, the byte order mark and
 * the like). The zero-width joiner of emoji sequences is allowed.
 */
export function hasHidden(text) {
  for (const ch of text) {
    const cp = ch.codePointAt(0);
    const control = cp <= 0x08 || cp === 0x0b || cp === 0x0c || (cp >= 0x0e && cp <= 0x1f);
    const delAndC1 = cp >= 0x7f && cp <= 0x9f;
    const separators = cp === 0x2028 || cp === 0x2029;
    const lone = cp >= 0xd800 && cp <= 0xdfff;
    const format = cp !== 0x200d && /^\p{Cf}$/u.test(ch);
    if (control || delAndC1 || separators || lone || format) return true;
  }
  return false;
}

/** Every string inside a value of plain objects and arrays. */
export function* strings(value) {
  if (typeof value === 'string') yield value;
  else if (Array.isArray(value)) for (const item of value) yield* strings(item);
  else if (value !== null && typeof value === 'object') for (const item of Object.values(value)) yield* strings(item);
}
