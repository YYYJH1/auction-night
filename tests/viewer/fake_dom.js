// A small stand-in for the parts of the DOM that the viewer uses, so that the real rendering
// code runs under `node --test` with no browser and no package. It is deliberately strict
// where the browser is lenient: unknown tags are fine, but everything the viewer does with an
// element goes through the methods below, and the helpers at the end read the result back.

class TextNode {
  constructor(data) {
    this.nodeType = 3;
    this.data = String(data);
    this.parentNode = null;
  }

  get textContent() {
    return this.data;
  }

  set textContent(value) {
    this.data = String(value);
  }
}

class ClassList {
  constructor(el) {
    this.el = el;
  }

  #names() {
    return new Set((this.el.attrs.get('class') ?? '').split(/\s+/).filter(Boolean));
  }

  #write(names) {
    this.el.attrs.set('class', [...names].join(' '));
  }

  contains(name) {
    return this.#names().has(name);
  }

  add(...names) {
    const set = this.#names();
    for (const name of names) set.add(name);
    this.#write(set);
  }

  remove(...names) {
    const set = this.#names();
    for (const name of names) set.delete(name);
    this.#write(set);
  }

  toggle(name, force) {
    const on = force === undefined ? !this.contains(name) : Boolean(force);
    if (on) this.add(name);
    else this.remove(name);
    return on;
  }
}

class Style {
  constructor() {
    this.props = new Map();
  }

  setProperty(name, value) {
    this.props.set(name, String(value));
  }

  getPropertyValue(name) {
    return this.props.get(name) ?? '';
  }
}

export class FakeElement {
  constructor(doc, tag) {
    this.nodeType = 1;
    this.ownerDocument = doc;
    this.localName = String(tag).toLowerCase();
    this.tagName = this.localName.toUpperCase();
    this.attrs = new Map();
    this.childNodes = [];
    this.parentNode = null;
    this.listeners = new Map();
    this.classList = new ClassList(this);
    this.style = new Style();
  }

  get children() {
    return this.childNodes.filter((n) => n.nodeType === 1);
  }

  get firstChild() {
    return this.childNodes[0] ?? null;
  }

  get className() {
    return this.attrs.get('class') ?? '';
  }

  get textContent() {
    return this.childNodes.map((n) => n.textContent).join('');
  }

  set textContent(value) {
    this.replaceChildren();
    if (String(value) !== '') this.appendChild(new TextNode(value));
  }

  appendChild(node) {
    if (node.parentNode) node.parentNode.removeChild(node);
    node.parentNode = this;
    this.childNodes.push(node);
    return node;
  }

  append(...nodes) {
    for (const node of nodes) this.appendChild(typeof node === 'string' ? new TextNode(node) : node);
  }

  removeChild(node) {
    const at = this.childNodes.indexOf(node);
    if (at < 0) throw new Error('not a child');
    this.childNodes.splice(at, 1);
    node.parentNode = null;
    return node;
  }

  replaceChildren(...nodes) {
    for (const child of this.childNodes) child.parentNode = null;
    this.childNodes = [];
    this.append(...nodes);
  }

  remove() {
    if (this.parentNode) this.parentNode.removeChild(this);
  }

  setAttribute(name, value) {
    this.attrs.set(String(name), String(value));
  }

  getAttribute(name) {
    return this.attrs.has(name) ? this.attrs.get(name) : null;
  }

  hasAttribute(name) {
    return this.attrs.has(name);
  }

  removeAttribute(name) {
    this.attrs.delete(name);
  }

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }

  dispatch(type, extra = {}) {
    const event = { type, target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...extra };
    for (const handler of this.listeners.get(type) ?? []) handler(event);
    return event;
  }

  querySelectorAll(selector) {
    return [...walk(this)].filter((el) => el !== this && matches(el, selector));
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] ?? null;
  }
}

export class FakeDocument {
  constructor() {
    this.body = new FakeElement(this, 'body');
  }

  createElement(tag) {
    return new FakeElement(this, tag);
  }

  createTextNode(data) {
    return new TextNode(data);
  }
}

// ----------------------------------------------------------------- reading the result back

/** Every element under (and including) a node, depth first. */
export function* walk(node) {
  if (node.nodeType === 1) yield node;
  for (const child of node.childNodes ?? []) yield* walk(child);
}

/** Every text node under a node. */
export function* textNodes(node) {
  if (node.nodeType === 3) yield node;
  for (const child of node.childNodes ?? []) yield* textNodes(child);
}

function tokens(compound) {
  return compound.match(/[#.]?[\w-]+|\[[^\]]+\]/g) ?? [];
}

function matchesCompound(el, compound) {
  for (const token of tokens(compound)) {
    if (token.startsWith('.')) {
      if (!el.classList.contains(token.slice(1))) return false;
    } else if (token.startsWith('#')) {
      if (el.getAttribute('id') !== token.slice(1)) return false;
    } else if (token.startsWith('[')) {
      const [, name, value] = /^\[([\w-]+)(?:=["']?([^"'\]]*)["']?)?\]$/.exec(token);
      if (!el.hasAttribute(name)) return false;
      if (value !== undefined && el.getAttribute(name) !== value) return false;
    } else if (el.localName !== token) return false;
  }
  return true;
}

function matches(el, selector) {
  const parts = selector.trim().split(/\s+/);
  if (!matchesCompound(el, parts.at(-1))) return false;
  let node = el.parentNode;
  for (let i = parts.length - 2; i >= 0; i -= 1) {
    while (node && !matchesCompound(node, parts[i])) node = node.parentNode;
    if (!node) return false;
    node = node.parentNode;
  }
  return true;
}

const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' };
const escapeText = (s) => s.replace(/[&<>"]/g, (c) => ESCAPES[c]);

/**
 * HTML text of a node, as a browser would serialise it. Text is escaped. With `canonical`, the
 * class names of an element are sorted and its style properties are written out, so that two
 * trees that look the same compare equal whatever order the changes were made in.
 */
export function serialize(node, { canonical = false } = {}) {
  if (node.nodeType === 3) return escapeText(node.data);
  const pairs = [...node.attrs].map(([k, v]) => [k, canonical && k === 'class' ? v.split(/\s+/).sort().join(' ') : v]);
  if (canonical && node.style.props.size > 0) {
    pairs.push(['style', [...node.style.props].sort().map(([k, v]) => `${k}:${v}`).join(';')]);
  }
  const attrs = pairs.map(([k, v]) => ` ${k}="${escapeText(v)}"`).join('');
  return `<${node.localName}${attrs}>${node.childNodes.map((child) => serialize(child, { canonical })).join('')}</${node.localName}>`;
}

/** Every element name under a node, as a sorted set. */
export function tagNames(node) {
  return new Set([...walk(node)].map((el) => el.localName));
}

/** Every attribute value under a node. */
export function* attributeValues(node) {
  for (const el of walk(node)) for (const [name, value] of el.attrs) yield { el, name, value };
}
