import assert from 'node:assert/strict';
import test from 'node:test';

import { clock, formatClass, formatTitle, isFormat, milliText, signed, stars } from '../../viewer/lib/format.js';

const MINUS = String.fromCharCode(0x2212);

test('signed shows both signs and a plain zero', () => {
  assert.equal(signed(14), '+14');
  assert.equal(signed(-5), `${MINUS}5`);
  assert.equal(signed(0), '0');
  assert.equal(signed(-0), '0');
  assert.equal(signed(1_000_000), '+1000000');
  assert.equal(signed(-1_000_000), `${MINUS}1000000`);
});

test('signed uses a real minus sign, not a hyphen, and a negative number never shows a plus', () => {
  assert.ok(!signed(-3).includes('-'));
  assert.ok(!signed(-3).includes('+'));
  assert.equal(signed(-3).codePointAt(0), 0x2212);
});

test('signed gives an empty text for anything but an integer', () => {
  for (const bad of [1.5, Number.NaN, Number.POSITIVE_INFINITY, '3', null, undefined, {}, 2 ** 53]) assert.equal(signed(bad), '');
});

test('milliText writes thousandths as a decimal, with at least the asked decimals', () => {
  assert.equal(milliText(920, 2), '0.92');
  assert.equal(milliText(1000, 2), '1.00');
  assert.equal(milliText(905, 2), '0.905');
  assert.equal(milliText(1, 2), '0.001');
  assert.equal(milliText(0, 2), '0.00');
  assert.equal(milliText(71_295), '71.295');
  assert.equal(milliText(75_000), '75');
  assert.equal(milliText(75_500), '75.5');
  assert.equal(milliText(0), '0');
  assert.equal(milliText(123_456_789, 3), '123456.789');
});

test('milliText is exact: it never rounds and never goes through a float', () => {
  for (let n = 0; n < 3000; n += 7) {
    const text = milliText(n, 3);
    assert.match(text, /^\d+\.\d{3}$/);
    assert.equal(Number(text.replace('.', '')), n);
  }
  assert.equal(milliText(Number.MAX_SAFE_INTEGER, 3), '9007199254740.991');
});

test('milliText gives an empty text for a negative number or anything but an integer', () => {
  for (const bad of [-1, 1.5, Number.NaN, '920', null, undefined, 2 ** 53]) assert.equal(milliText(bad, 2), '');
});

test('the older helpers are unchanged', () => {
  assert.equal(stars(2), '★★☆');
  assert.equal(clock(75), '1:15');
  assert.equal(formatTitle('english'), '英式加价');
  assert.equal(formatClass('blind_box'), 'blind-box');
  assert.equal(isFormat('dutch'), true);
});
