import assert from 'node:assert/strict';
import test from 'node:test';

import { append, clear, h, setAttr, setText, toggle } from '../../viewer/lib/dom.js';
import { attributeValues, FakeDocument, serialize, tagNames, textNodes, walk } from './fake_dom.js';
import { PAYLOADS } from './helpers.js';

const doc = new FakeDocument();

test('h builds elements and text', () => {
  const el = h(doc, 'div', { class: 'a b', id: 'x', 'data-seat': 3 }, 'one ', h(doc, 'span', {}, 'two'), 3);
  assert.equal(serialize(el), '<div class="a b" id="x" data-seat="3">one <span>two</span>3</div>');
});

test('children that are null, false or true are skipped, arrays are flattened', () => {
  const el = h(doc, 'p', {}, null, undefined, false, true, ['a', ['b', null, 'c']]);
  assert.equal(el.textContent, 'abc');
});

for (const [name, payload] of Object.entries(PAYLOADS)) {
  test(`text child "${name}" stays one inert text node`, () => {
    const el = h(doc, 'p', { class: 'line' }, payload);
    assert.equal(el.childNodes.length, 1);
    assert.equal(el.childNodes[0].nodeType, 3);
    assert.equal(el.childNodes[0].data, payload);
    assert.deepEqual([...tagNames(el)], ['p']);
    assert.ok(!serialize(el).includes('<img') && !serialize(el).includes('<script') && !serialize(el).includes('<svg'));
  });
}

test('setText replaces the content with text, whatever it looks like', () => {
  const el = h(doc, 'p', {}, h(doc, 'span', {}, 'old'));
  setText(el, PAYLOADS.image);
  assert.equal(el.childNodes.length, 1);
  assert.equal(el.textContent, PAYLOADS.image);
  assert.deepEqual([...tagNames(el)], ['p']);
  setText(el, 42);
  assert.equal(el.textContent, '42');
});

test('append puts a node, not its text, into the parent', () => {
  const parent = h(doc, 'div', {});
  append(doc, parent, [h(doc, 'span', {}, 'x'), 'y']);
  assert.equal(serialize(parent), '<div><span>x</span>y</div>');
});

test('clear and toggle', () => {
  const el = h(doc, 'div', { class: 'a' }, 'x', 'y');
  clear(el);
  assert.equal(el.childNodes.length, 0);
  toggle(el, 'on', true);
  assert.ok(el.classList.contains('on'));
  toggle(el, 'on', false);
  assert.ok(!el.classList.contains('on'));
});

test('only known tags can be created', () => {
  for (const tag of ['img', 'script', 'iframe', 'svg', 'style', 'link', 'object', 'embed', 'form', 'textarea', 'base', 'meta', 'video']) {
    assert.throws(() => h(doc, tag, {}), TypeError, tag);
  }
  for (const tag of ['div', 'span', 'p', 'ul', 'ol', 'li', 'button', 'header', 'section', 'article', 'strong', 'small', 'h2', 'h3', 'table', 'caption', 'thead', 'tbody', 'tr', 'th', 'td', 'dl', 'dt', 'dd']) {
    assert.doesNotThrow(() => h(doc, tag, {}), tag);
  }
});

test('tables and definition lists are built from the same checked helper', () => {
  const table = h(
    doc,
    'table',
    { class: 'ov-table' },
    h(doc, 'caption', {}, 'c'),
    h(doc, 'thead', {}, h(doc, 'tr', {}, h(doc, 'th', { scope: 'col' }, PAYLOADS.image))),
    h(doc, 'tbody', {}, h(doc, 'tr', {}, h(doc, 'th', { scope: 'row' }, 'a'), h(doc, 'td', {}, PAYLOADS.script))),
  );
  assert.deepEqual([...tagNames(table)].sort(), ['caption', 'table', 'tbody', 'td', 'th', 'thead', 'tr']);
  assert.ok(!serialize(table).includes('<img') && !serialize(table).includes('<script'));
  const list = h(doc, 'dl', {}, h(doc, 'div', { tabindex: -1, 'data-term': 'ref' }, h(doc, 'dt', {}, 'x'), h(doc, 'dd', {}, 'y')));
  assert.equal(list.querySelector('div').getAttribute('tabindex'), '-1');
  assert.doesNotThrow(() => h(doc, 'button', { type: 'button', 'aria-expanded': false, 'aria-controls': 'glossary' }));
  assert.doesNotThrow(() => h(doc, 'div', { hidden: true, id: 'glossary' }));
});

test('scope takes only col and row, and the table tags take no other new attribute', () => {
  for (const bad of ['colgroup', 'rowgroup', 'x', '', 'col row', 'javascript:1']) assert.throws(() => h(doc, 'th', { scope: bad }), TypeError, bad);
  for (const name of ['colspan', 'rowspan', 'headers', 'abbr', 'background', 'bgcolor']) assert.throws(() => h(doc, 'td', { [name]: '2' }), TypeError, name);
  for (const bad of ['12', 'a', '--1', '1 2', '']) assert.throws(() => h(doc, 'div', { tabindex: bad }), TypeError, bad);
});

test('attributes that the browser would act on are refused', () => {
  for (const name of ['onclick', 'onerror', 'onload', 'onmouseover', 'style', 'src', 'srcdoc', 'formaction', 'action', 'target', 'rel', 'xlink:href', 'ping']) {
    assert.throws(() => h(doc, 'div', { [name]: 'x' }), TypeError, name);
  }
});

test('links may only point at our own fragments', () => {
  assert.doesNotThrow(() => h(doc, 'a', { href: '#/n/fixture-0001/lot/3' }));
  assert.doesNotThrow(() => h(doc, 'a', { href: '#/n/%E5%A4%9C/lot/3' }));
  for (const bad of [
    'javascript:alert(1)',
    'JaVaScRiPt:alert(1)',
    ' javascript:alert(1)',
    'https://evil.example/',
    '//evil.example/',
    'data:text/html,x',
    '/n/1',
    '#',
    '#x',
    '#/n/x"onmouseover="alert(1)',
    '#/n/x y',
    '#/n/<x>',
  ]) {
    assert.throws(() => h(doc, 'a', { href: bad }), TypeError, bad);
  }
});

test('class, id and data values cannot carry markup or spaces where none belong', () => {
  assert.doesNotThrow(() => h(doc, 'div', { class: 'seat seat-c3 is-out' }));
  for (const bad of ['a"b', 'a b"', 'x onmouseover=y', '<x>', '', ' a', 'a  b', '1abc']) {
    assert.throws(() => h(doc, 'div', { class: bad }), TypeError, bad);
  }
  for (const bad of ['a b', 'a"b', '1x', '']) assert.throws(() => h(doc, 'div', { id: bad }), TypeError, bad);
  for (const bad of ['a"b', '<x>', 'a b', 'x'.repeat(100)]) {
    assert.throws(() => h(doc, 'div', { 'data-x': bad }), TypeError, bad);
  }
  assert.throws(() => h(doc, 'div', { 'data-Bad': 'x' }), TypeError);
});

test('aria and title text is cleaned and short', () => {
  const el = h(doc, 'div', { 'aria-label': `a\x07b${'x'.repeat(500)}`, title: PAYLOADS.image });
  const label = el.getAttribute('aria-label');
  assert.ok(label.startsWith('ab'));
  assert.ok([...label].length <= 160);
  assert.equal(el.getAttribute('title'), PAYLOADS.image, 'plain text in an attribute is inert');
});

test('boolean attributes', () => {
  const el = h(doc, 'button', { 'aria-pressed': false, hidden: true, disabled: false });
  assert.equal(el.getAttribute('aria-pressed'), 'false');
  assert.equal(el.getAttribute('hidden'), '');
  assert.equal(el.hasAttribute('disabled'), false);
  setAttr(el, 'aria-pressed', true);
  assert.equal(el.getAttribute('aria-pressed'), 'true');
  setAttr(el, 'aria-pressed', null);
  assert.equal(el.hasAttribute('aria-pressed'), false);
});

test('numeric attributes are checked', () => {
  assert.doesNotThrow(() => h(doc, 'input', { type: 'range', min: 0, max: 1000, step: 1, value: 0 }));
  assert.throws(() => h(doc, 'input', { type: 'text' }), TypeError);
  assert.throws(() => h(doc, 'input', { type: 'range', min: '0; x' }), TypeError);
});

test('a tree built from hostile text holds only the tags we asked for', () => {
  const tree = h(doc, 'section', {}, ...Object.values(PAYLOADS).map((p) => h(doc, 'p', { class: 'x' }, p)));
  assert.deepEqual([...tagNames(tree)].sort(), ['p', 'section']);
  for (const { name, value } of attributeValues(tree)) {
    assert.ok(['class'].includes(name), name);
    assert.equal(value, 'x');
  }
  const seen = [...textNodes(tree)].map((n) => n.data);
  assert.deepEqual(seen, Object.values(PAYLOADS));
  assert.equal([...walk(tree)].length, 1 + Object.keys(PAYLOADS).length);
});
