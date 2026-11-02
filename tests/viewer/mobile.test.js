// The layout for phones (320 to 430 px wide, upright or on the side) is style.css, which no
// test here can lay out. These tests read it as text and hold the rules that make the page
// usable on a small screen: the page may not be wider than the screen, every control is at
// least 44 px by 44 px, and every control that the page builds is one of those. A browser run
// measures the real sizes; these tests keep the rules from being edited away.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { withPage } from './app_harness.js';
import { CLOCKS, loadFixture, SEALED, VIEWER } from './helpers.js';
import { FakeDocument } from './fake_dom.js';
import { mountAwards } from '../../viewer/ui/awards.js';
import { mountHighlights } from '../../viewer/ui/highlights.js';
import { mountOpeningCard, mountReview } from '../../viewer/ui/review.js';
import { modelOf } from './helpers.js';

const css = readFileSync(new URL('style.css', `file://${VIEWER}`), 'utf8');
const html = readFileSync(new URL('index.html', `file://${VIEWER}`), 'utf8');

/** The text between the braces of the block that opens at `start` (the index of its "{"). */
function block(source, start) {
  let depth = 0;
  for (let i = start; i < source.length; i += 1) {
    if (source[i] === '{') depth += 1;
    if (source[i] === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(start + 1, i);
    }
  }
  throw new Error('unbalanced braces');
}

/** The body of an @media block, found by its condition. */
function media(condition) {
  const at = css.indexOf(`@media ${condition} {`);
  assert.ok(at >= 0, `no @media ${condition}`);
  return block(css, css.indexOf('{', at));
}

/** [{selectors, body}] of the rules of a block that holds no further blocks. */
function rules(source) {
  const out = [];
  for (const match of source.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selectors = match[1].replace(/\/\*[\s\S]*?\*\//g, '').split(',').map((s) => s.trim()).filter(Boolean);
    out.push({ selectors, body: match[2] });
  }
  return out;
}

const TOUCH = '(max-width: 640px), (max-height: 520px), (pointer: coarse)';

// ------------------------------------------------------------------------- the viewport

test('the page takes the width of the screen and lets the reader zoom', () => {
  const meta = html.match(/<meta name="viewport" content="([^"]*)">/);
  assert.ok(meta);
  assert.match(meta[1], /width=device-width/);
  assert.match(meta[1], /initial-scale=1/);
  assert.ok(!/user-scalable\s*=\s*(?:no|0)|maximum-scale/.test(meta[1]), 'zooming is not switched off');
});

test('the layout is one column below 860 px, with rules for 430 px and for a short window on its side', () => {
  assert.match(media('(max-width: 860px)'), /\.layout\s*\{\s*grid-template-columns:\s*minmax\(0,\s*1fr\);/);
  assert.match(media('(max-width: 860px)'), /\.topbar\s*\{\s*position:\s*static;/);
  assert.ok(media('(max-width: 430px)').includes('.topbar'));
  const short = media('(max-height: 520px)');
  assert.match(short, /\.topbar[\s\S]*position:\s*static/, 'the bar scrolls away on a phone held on its side');
  assert.match(short, /\.lots[\s\S]*max-height:\s*none/);
});

test('the column of lots sticks below the bar by the height that the page measures, and the bar never takes its own height as its least', () => {
  const topbar = rules(css).find((rule) => rule.selectors.includes('.topbar'));
  assert.match(topbar.body, /min-height:\s*var\(--topbar-min\)/, 'else a bar that once had two rows could never shrink again');
  const lots = rules(css).find((rule) => rule.selectors.includes('.lots'));
  assert.match(lots.body, /top:\s*var\(--topbar-h\)/);
  assert.match(lots.body, /max-height:\s*calc\(100vh - var\(--topbar-h\)\)/);
  assert.match(css, /--topbar-min:\s*58px/);
  assert.match(css, /--topbar-h:\s*58px/, 'a guess for a bar of one row, until the page measures it');
});

// -------------------------------------------------------------------------- nothing wider

test('no rule asks for a fixed width wider than a phone, and no grid column is wider than one', () => {
  const widths = [...css.matchAll(/(?<![\w-])(?:min-)?width:\s*(\d+(?:\.\d+)?)px/g)].map((m) => Number(m[1]));
  assert.ok(widths.length > 5);
  for (const value of widths) assert.ok(value <= 296, `a width of ${value}px does not fit a 320 px phone`);
  for (const match of css.matchAll(/minmax\(\s*(\d+)px/g)) assert.ok(Number(match[1]) <= 296, `a column of ${match[1]}px`);
  for (const match of css.matchAll(/flex:\s*\d+\s+\d+\s+(\d+)px/g)) assert.ok(Number(match[1]) <= 296, `a flex basis of ${match[1]}px`);
  assert.ok(css.includes('min(100%, 290px)'), 'the award cards shrink to the width they are given');
  assert.ok(css.includes('min(100%, 260px)'));
});

test('long text breaks instead of widening the page', () => {
  for (const selector of ['.recap-text', '.light-text', '.winner-name', '.award-basis', '.award-rule dd', '.verdict-quote', '.verdict-basis', '.review-para', '.pred-text']) {
    const found = rules(css).find((rule) => rule.selectors.some((s) => s === selector || s.endsWith(selector)) && /overflow-wrap:\s*anywhere/.test(rule.body));
    assert.ok(found, `${selector} breaks long words`);
  }
  assert.match(css, /\.layout\s*\{[^}]*grid-template-columns:\s*264px minmax\(0, 1fr\)/, 'the wide layout lets the stage shrink');
});

// ---------------------------------------------------------------------- touch targets

const touchRules = rules(media(TOUCH));
const covered = touchRules.filter((rule) => /min-height:\s*44px|(?<![\w-])height:\s*44px/.test(rule.body)).flatMap((rule) => rule.selectors);

test('on a phone, upright or on its side, and on any screen used with a finger, controls are at least 44 px high and wide', () => {
  assert.ok(covered.length >= 8);
  for (const selector of ['.btn', '.view-link', '.recap-go', '.gate-open', '.lot-link', '.light-link', 'button.term', '.scrub']) {
    assert.ok(covered.includes(selector), `${selector} has a height of 44 px`);
  }
  const wide = touchRules.filter((rule) => /min-width:\s*44px/.test(rule.body)).flatMap((rule) => rule.selectors);
  for (const selector of ['.btn', '.view-link', '.recap-go', 'button.term']) assert.ok(wide.includes(selector), `${selector} has a width of 44 px`);
});

test('every button, link and slider in the markup is covered by those rules', () => {
  for (const tag of html.match(/<(?:button|input|label|a)\b[^>]*>/g)) {
    if (/visually-hidden|type="file"/.test(tag)) continue;
    const classes = (tag.match(/class="([^"]*)"/)?.[1] ?? '').split(/\s+/);
    assert.ok(classes.some((name) => covered.includes(`.${name}`)), `${tag} is not covered`);
  }
});

/** Every control of the page in some state, as the selectors of the touch rules see them. */
function uncovered(root) {
  const odd = [];
  const controls = [...root.querySelectorAll('button'), ...root.querySelectorAll('a'), ...root.querySelectorAll('input')];
  for (const el of controls) {
    const hit = covered.some((selector) => root.querySelectorAll(selector).includes(el));
    if (!hit) odd.push(`${el.localName}.${el.className}`);
  }
  return odd;
}

test('every control that the page builds, in every part and in both lens states, is one of them', () =>
  withPage({}, async (page) => {
    for (const name of [SEALED, CLOCKS]) {
      const text = JSON.stringify(loadFixture(name));
      await page.chooseFile({ size: text.length, text: async () => text });
      const night = name === SEALED ? 'fixture-sealed-0001' : 'fixture-english-dutch-0001';
      for (const lens of [true, false]) {
        if ((page.el('lens').getAttribute('aria-checked') === 'true') !== lens) page.click('lens');
        for (const hash of [`#/n/${night}/lot/1`, `#/n/${night}/lot/2`, `#/n/${night}/recap`, `#/n/${night}/highlights`, `#/n/${night}/awards`, `#/n/${night}/review`]) {
          page.navigate(hash);
          page.el('page').querySelector('button.gate-open')?.dispatch('click');
          page.el('lot-stage').querySelector('.gloss-toggle')?.dispatch('click');
          page.el('scrub').value = '1000';
          page.el('scrub').dispatch('input');
          for (const id of ['stage', 'lot-list', 'views']) {
            const root = page.el(id);
            const controls = [...root.querySelectorAll('button'), ...root.querySelectorAll('a'), ...root.querySelectorAll('input')];
            assert.ok(controls.length > 0 || hash.endsWith('awards') || hash.endsWith('review'), `${hash} ${id}`);
            const members = new Set(covered.flatMap((selector) => root.querySelectorAll(selector)));
            for (const el of controls) assert.ok(members.has(el), `${hash} lens ${lens}: ${el.localName}.${el.className}`);
          }
        }
      }
    }
  }));

test('the same holds for the pieces mounted on their own', () => {
  for (const name of [SEALED, CLOCKS]) {
    const model = modelOf(name);
    const doc = new FakeDocument();
    const parent = doc.createElement('div');
    mountAwards(doc, parent, model, { lens: true });
    mountHighlights(doc, parent, model, { lens: true, revealed: false });
    mountHighlights(doc, parent, model, { lens: true, revealed: true });
    mountReview(doc, parent, model, { lens: true, revealed: false });
    mountReview(doc, parent, model, { lens: true, revealed: true });
    mountOpeningCard(doc, parent, model);
    assert.deepEqual(uncovered(parent), []);
  }
});
