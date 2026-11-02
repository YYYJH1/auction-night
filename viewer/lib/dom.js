// Safe DOM construction.
//
// Everything on the page is built with these helpers. Text always goes in as a text node.
// Attribute names and values are checked against a short allow-list, so text from a replay
// can neither become markup nor reach an attribute that the browser would act on (links,
// sources, event handlers, inline styles). The helpers take the document as an argument so
// that tests can run them against a stand-in.

import { cleanLine } from './safe.js';

const TAGS = new Set([
  'a', 'article', 'aside', 'button', 'caption', 'dd', 'div', 'dl', 'dt', 'em', 'footer', 'h1', 'h2',
  'h3', 'header', 'input', 'label', 'li', 'main', 'nav', 'ol', 'p', 'section', 'small', 'span',
  'strong', 'table', 'tbody', 'td', 'th', 'thead', 'tr', 'ul',
]);

const CLASSES = /^[A-Za-z_][\w-]*( [A-Za-z_][\w-]*)*$/;
const TOKEN = /^[\w.:-]{0,64}$/;
const NUMBER = /^-?\d{1,16}(\.\d{1,4})?$/;
const ID = /^[A-Za-z][\w-]{0,63}$/;
const HASH_LINK = /^#\/[\w%.~/-]{0,300}$/; // our own deep links, never a scheme
const ARIA = new Set([
  'aria-atomic', 'aria-busy', 'aria-checked', 'aria-controls', 'aria-current',
  'aria-describedby', 'aria-disabled', 'aria-expanded', 'aria-hidden', 'aria-labelledby', 'aria-live',
  'aria-pressed', 'aria-selected', 'aria-valuemax', 'aria-valuemin', 'aria-valuenow',
]);
const ARIA_TEXT = new Set(['aria-label', 'aria-valuetext', 'title']);
const BOOLEAN_ATTRS = new Set(['hidden', 'disabled']);

function attributeValue(name, raw) {
  if (BOOLEAN_ATTRS.has(name)) return raw ? '' : null;
  if (typeof raw === 'boolean' && ARIA.has(name)) return raw ? 'true' : 'false';
  if (raw === null || raw === undefined || raw === false) return null;
  const value = typeof raw === 'number' ? String(raw) : raw === true ? 'true' : raw;
  if (typeof value !== 'string') throw new TypeError(`attribute ${name}: unsupported value`);
  const ok = (pattern) => {
    if (!pattern.test(value)) throw new TypeError(`attribute ${name}: value not allowed`);
    return value;
  };
  if (name === 'class') return ok(CLASSES);
  if (name === 'id' || name === 'for') return ok(ID);
  if (name === 'role') return ok(/^[a-z]{3,20}$/);
  if (name === 'scope') return ok(/^(col|row)$/);
  if (name === 'href') return ok(HASH_LINK);
  if (name === 'type') return ok(/^(button|range|file|checkbox)$/);
  if (name === 'accept') return ok(/^\.json,application\/json$/);
  if (name === 'tabindex') return ok(/^-?\d$/);
  if (name === 'min' || name === 'max' || name === 'step' || name === 'value') return ok(NUMBER);
  if (ARIA_TEXT.has(name)) return cleanLine(value, 160);
  if (ARIA.has(name)) return ok(TOKEN);
  if (/^data-[a-z][a-z0-9-]*$/.test(name)) return ok(TOKEN);
  throw new TypeError(`attribute not allowed: ${name}`);
}

function isNode(x) {
  return typeof x === 'object' && x !== null && typeof x.nodeType === 'number';
}

/** Append text, elements and arrays of them. Strings and numbers always become text nodes. */
export function append(doc, parent, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false || child === true) continue;
    if (Array.isArray(child)) append(doc, parent, child);
    else if (isNode(child)) parent.appendChild(child);
    else parent.appendChild(doc.createTextNode(String(child)));
  }
  return parent;
}

/** Build an element: h(doc, 'div', {class: 'x'}, 'text', childElement, ...). */
export function h(doc, tag, attrs, ...children) {
  if (!TAGS.has(tag)) throw new TypeError(`tag not allowed: ${tag}`);
  const el = doc.createElement(tag);
  for (const [name, raw] of Object.entries(attrs ?? {})) setAttr(el, name, raw);
  return append(doc, el, children);
}

/** Set (or remove, for null and false) one checked attribute. A value that is already there is left alone. */
export function setAttr(el, name, raw) {
  const value = attributeValue(name, raw);
  if (value === null) {
    if (el.hasAttribute(name)) el.removeAttribute(name);
  } else if (el.getAttribute(name) !== value) {
    el.setAttribute(name, value);
  }
}

/** Replace the content of an element with a single text node. */
export function setText(el, text) {
  const value = String(text);
  if (el.textContent !== value) el.textContent = value;
}

/** Remove all children. */
export function clear(el) {
  el.replaceChildren();
}

export function toggle(el, className, on) {
  el.classList.toggle(className, Boolean(on));
}
