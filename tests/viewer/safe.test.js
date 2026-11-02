import assert from 'node:assert/strict';
import test from 'node:test';

import {
  asCoins,
  asInt,
  asLotNo,
  asSeat,
  cleanBlock,
  cleanLine,
  clamp,
  ELLIPSIS,
  keyInt,
  toText,
  truncate,
} from '../../viewer/lib/safe.js';
import { hasHidden, PAYLOADS } from './helpers.js';

const RLO = String.fromCharCode(0x202e);
const LRI = String.fromCharCode(0x2066);
const LINE_SEP = String.fromCharCode(0x2028);
const PARA_SEP = String.fromCharCode(0x2029);
const ZWJ = String.fromCharCode(0x200d);
const char = (cp) => String.fromCodePoint(cp);
const points = (s) => [...s].length;

// The code points of category Cf in Unicode 15.0, which is what the Python sanitiser of the
// engine (src/auction_night/llm/sanitize.py) drops together with the controls and surrogates.
// Generated once from unicodedata; each entry is [first, last]. U+200D is in the table and is
// the one character that the viewer keeps on purpose.
const ENGINE_FORMAT_RANGES = [
  [0x00ad, 0x00ad], [0x0600, 0x0605], [0x061c, 0x061c], [0x06dd, 0x06dd], [0x070f, 0x070f],
  [0x0890, 0x0891], [0x08e2, 0x08e2], [0x180e, 0x180e], [0x200b, 0x200f], [0x202a, 0x202e],
  [0x2060, 0x2064], [0x2066, 0x206f], [0xfeff, 0xfeff], [0xfff9, 0xfffb], [0x110bd, 0x110bd],
  [0x110cd, 0x110cd], [0x13430, 0x1343f], [0x1bca0, 0x1bca3], [0x1d173, 0x1d17a],
  [0xe0001, 0xe0001], [0xe0020, 0xe007f],
];

test('truncate leaves short text alone', () => {
  assert.equal(truncate('abc', 5), 'abc');
  assert.equal(truncate('abcde', 5), 'abcde');
  assert.equal(truncate('', 5), '');
});

test('truncate cuts long text to the limit, ellipsis included', () => {
  assert.equal(truncate('abcdef', 5), `abcd${ELLIPSIS}`);
  assert.equal(points(truncate('x'.repeat(1000), 240)), 240);
  assert.ok(truncate('x'.repeat(1000), 240).endsWith(ELLIPSIS));
});

test('truncate counts code points, never splits a surrogate pair', () => {
  const faces = '\u{1F600}'.repeat(10);
  const cut = truncate(faces, 4);
  assert.equal(cut, `\u{1F600}\u{1F600}\u{1F600}${ELLIPSIS}`);
  assert.ok(cut.isWellFormed());
  assert.equal(truncate('\u{1F600}'.repeat(3), 3), '\u{1F600}'.repeat(3), 'six UTF-16 units, three code points');
});

test('truncate marks content that the caller already dropped', () => {
  assert.equal(truncate('ab', 5, true), `ab${ELLIPSIS}`);
  assert.equal(truncate('abcde', 5, true), `abcd${ELLIPSIS}`);
});

test('truncate rejects a bad limit', () => {
  for (const bad of [0, -1, 1.5, Number.NaN, '3']) assert.throws(() => truncate('abc', bad), RangeError);
});

test('toText accepts strings and finite numbers only', () => {
  assert.equal(toText('x'), 'x');
  assert.equal(toText(12), '12');
  for (const other of [null, undefined, {}, [], true, Number.NaN, Number.POSITIVE_INFINITY, () => 1]) {
    assert.equal(toText(other), '');
  }
});

test('cleanLine removes control characters', () => {
  assert.equal(cleanLine('a\x00b\x07c\x7fd\x9be'), 'abcde');
  assert.equal(cleanLine(`a${LINE_SEP}b${PARA_SEP}c`), 'abc');
});

test('cleanLine turns line breaks and tabs into one space', () => {
  assert.equal(cleanLine('one\r\n\ttwo\nthree'), 'one two three');
  assert.equal(cleanLine('  padded  '), 'padded');
});

test('ANSI escapes lose their escape byte and remain as plain text', () => {
  const out = cleanLine(PAYLOADS.ansi);
  assert.equal(out, '[31mred[0m');
  assert.ok(!hasHidden(out));
  const link = cleanLine(PAYLOADS.osc);
  assert.ok(!hasHidden(link));
  assert.ok(!link.includes('\x1b') && !link.includes('\x07'));
});

test('bidirectional overrides are removed', () => {
  assert.equal(cleanLine(`abc${RLO}def`), 'abcdef');
  assert.equal(cleanLine(`${LRI}x`), 'x');
});

test('invisible format characters are removed: zero-width spaces, direction marks, the byte order mark', () => {
  const hidden = {
    'zero width space': 0x200b,
    'zero width non-joiner': 0x200c,
    'left-to-right mark': 0x200e,
    'right-to-left mark': 0x200f,
    'Arabic letter mark': 0x061c,
    'byte order mark': 0xfeff,
    'word joiner': 0x2060,
    'soft hyphen': 0x00ad,
    'Mongolian vowel separator': 0x180e,
    'invisible times': 0x2062,
    'tag latin small letter a': 0xe0061,
    'cancel tag': 0xe007f,
  };
  for (const [name, cp] of Object.entries(hidden)) {
    assert.equal(cleanLine(`ab${char(cp)}cd`), 'abcd', name);
    assert.equal(cleanBlock(`ab${char(cp)}\ncd`), 'ab\ncd', name);
  }
  assert.equal(cleanLine(char(0x200b).repeat(50)), '', 'text that is only hidden characters becomes empty');
  assert.equal(cleanLine(`${char(0xfeff)}  hello  ${char(0x200f)}`), 'hello', 'and trimming sees the real ends');
});

test('the zero-width joiner stays, so emoji sequences survive', () => {
  const family = `\u{1F468}${ZWJ}\u{1F469}${ZWJ}\u{1F467}`;
  const couple = `\u{1F469}${ZWJ}\u2764\uFE0F${ZWJ}\u{1F468}`;
  const technologist = `\u{1F9D1}\u{1F3FD}${ZWJ}\u{1F4BB}`;
  for (const sequence of [family, couple, technologist]) {
    assert.equal(cleanLine(sequence), sequence);
    assert.equal(cleanLine(`x${sequence}y`), `x${sequence}y`);
    assert.equal(cleanBlock(sequence), sequence);
    assert.equal(points(cleanLine(sequence)), points(sequence));
  }
  assert.equal(cleanLine(ZWJ), ZWJ, 'the joiner alone is not dropped either');
});

test('visible look-alikes are not mistaken for hidden characters', () => {
  // Variation selectors and combining marks are not format characters. Spaces stay inside a text.
  const kept = ['\u2764\uFE0F', '\u00e9', 'e\u0301', '\u4e2d\u6587\uff0c\u3002', 'a\u3000b', 'a\u00a0b', '\u2022', '\u2212'];
  for (const text of kept) assert.equal(cleanLine(text), text, JSON.stringify(text));
  assert.equal(cleanLine('\u00a0x\u3000'), 'x', 'ordinary spaces at the ends are trimmed');
});

test('lone surrogates are removed, pairs are kept', () => {
  assert.equal(cleanLine(`a${String.fromCharCode(0xd800)}b`), 'ab');
  assert.equal(cleanLine(`a${String.fromCharCode(0xdc00)}b`), 'ab');
  assert.equal(cleanLine(`a${String.fromCharCode(0xdbff)}${String.fromCharCode(0x0041)}`), 'aA');
  assert.equal(cleanLine('a\u{1F600}b'), 'a\u{1F600}b');
  assert.ok(cleanLine(`${'x'.repeat(5)}${String.fromCharCode(0xd83d)}`).isWellFormed(), 'a pair cut in half by the pre-cut is cleaned too');
});

test('the viewer removes everything that the engine sanitiser removes, except the joiner', () => {
  let checked = 0;
  for (const [first, last] of ENGINE_FORMAT_RANGES) {
    for (let cp = first; cp <= last; cp += 1) {
      if (cp === 0x200d) continue;
      assert.equal(cleanLine(`a${char(cp)}b`), 'ab', `U+${cp.toString(16).toUpperCase().padStart(4, '0')}`);
      checked += 1;
    }
  }
  assert.equal(checked, 169, 'the table holds 170 code points, one of them the joiner');
  for (let cp = 0; cp <= 0x9f; cp += 1) {
    if (cp >= 0x20 && cp < 0x7f) continue;
    const out = cleanLine(`a${char(cp)}b`);
    const spaced = cp === 0x09 || cp === 0x0a || cp === 0x0d;
    assert.equal(out, spaced ? 'a b' : 'ab', `control U+${cp.toString(16).toUpperCase().padStart(4, '0')}`);
  }
  assert.equal(cleanLine(`a${LINE_SEP}b${PARA_SEP}c`), 'abc');
});

test('no code point of category Cf, Cc or Cs survives, whatever the Unicode version of the browser', () => {
  for (let cp = 0; cp <= 0x10ffff; cp += 1) {
    const surrogate = cp >= 0xd800 && cp <= 0xdfff;
    const probe = surrogate ? String.fromCharCode(cp) : char(cp);
    const category = /^\p{Cf}$/u.test(probe) || /^\p{Cc}$/u.test(probe) || /^\p{Cs}$/u.test(probe);
    if (!category || cp === 0x200d || cp === 0x09 || cp === 0x0a || cp === 0x0d) continue;
    assert.ok(!cleanLine(`a${probe}b`).includes(probe), `U+${cp.toString(16).toUpperCase().padStart(4, '0')}`);
  }
});

test('markup, scripts and links pass through as the same characters', () => {
  for (const key of ['image', 'script', 'handler', 'jsLink', 'anchor', 'markdown', 'richLink', 'richStyle', 'quote', 'entity']) {
    assert.equal(cleanLine(PAYLOADS[key]), PAYLOADS[key], key);
  }
});

test('cleanLine shortens very long text with an ellipsis', () => {
  const out = cleanLine('字'.repeat(10_000), 240);
  assert.equal(points(out), 240);
  assert.ok(out.endsWith(ELLIPSIS));
  assert.equal(cleanLine('x'.repeat(240), 240), 'x'.repeat(240));
});

test('cleanLine copes with a very large input', () => {
  const huge = 'ab'.repeat(2_500_000);
  const started = Date.now();
  const out = cleanLine(huge, 100);
  assert.equal(points(out), 100);
  assert.ok(Date.now() - started < 1000, 'cleaning a 5 MB string takes well under a second');
});

test('cleanLine of text that is only hidden characters still says it was cut', () => {
  assert.equal(cleanLine('\x01'.repeat(100_000), 50), ELLIPSIS);
});

test('cleanLine treats non-text as empty', () => {
  for (const other of [null, undefined, {}, [], true]) assert.equal(cleanLine(other), '');
  assert.equal(cleanLine(42), '42');
});

test('cleanBlock keeps line breaks and shrinks runs of blank lines', () => {
  assert.equal(cleanBlock('a\r\nb\rc'), 'a\nb\nc');
  assert.equal(cleanBlock('a\n\n\n\n\nb'), 'a\n\nb');
  assert.equal(cleanBlock('a\tb\x07c'), 'a bc');
  assert.equal(points(cleanBlock('x'.repeat(10_000), 100)), 100);
});

test('number readers accept integers only', () => {
  assert.equal(asInt(5), 5);
  assert.equal(asInt(-5), -5);
  for (const bad of [1.5, '12', Number.NaN, 2 ** 53, true, null, undefined]) assert.equal(asInt(bad), null);
  assert.equal(asCoins(0), 0);
  assert.equal(asCoins(-1), null);
  assert.equal(asSeat(1), 1);
  assert.equal(asSeat(8), 8);
  assert.equal(asSeat(0), null);
  assert.equal(asSeat(9), null);
  assert.equal(asLotNo(36), 36);
  assert.equal(asLotNo(37), null);
  assert.equal(asLotNo(0), null);
});

test('keyInt reads the integer in a JSON object key', () => {
  assert.equal(keyInt('3'), 3);
  assert.equal(keyInt('12'), 12);
  for (const bad of ['03', '0', '-1', 'x', '1.5', '', ' 1', 3, null]) assert.equal(keyInt(bad), null, String(bad));
});

test('clamp', () => {
  assert.equal(clamp(5, 0, 3), 3);
  assert.equal(clamp(-5, 0, 3), 0);
  assert.equal(clamp(2, 0, 3), 2);
});
