// Building elements for the home page. Text always goes in as a text node, never as markup.
// The tags and the attributes are checked against a short allow-list, and a link may point only
// to the replay page of this site or to the repository, so that nothing read from a file can
// become a script, an address on another site or an inline style. The helpers take the document
// as an argument so that tests can run them against a stand-in.

const TAGS = new Set(['a', 'div', 'li', 'p', 'span', 'strong', 'table', 'tbody', 'td', 'th', 'thead', 'tr', 'ul']);

/** The addresses that a link of the page may have: the replay page with a replay of this site, or the repository. */
export const HREF_PATTERN =
  /^(?:viewer\/index\.html\?replay=\.\.\/(?:docs|seasons)\/(?:[\w-]+\/)*[\w-]+\.json(?:#\/[\w%./-]*)?|https:\/\/github\.com\/YYYJH1\/auction-night(?:\/[^\s"'<>]*)?)$/;

const PATTERNS = {
  class: /^[A-Za-z_][\w-]*( [A-Za-z_][\w-]*)*$/,
  href: HREF_PATTERN,
  scope: /^(?:col|row)$/,
  role: /^(?:status|list)$/,
  'aria-labelledby': /^[A-Za-z][\w-]{0,63}$/,
  lang: /^[a-z]{2,3}(?:-[A-Z]{2})?$/,
  alt: /^[^\u0000-\u001f<>]{0,400}$/,
  'aria-pressed': /^(?:true|false)$/,
};

/** Set one checked attribute. A name that is not on the list, or a value that does not fit it, throws. */
export function setAttr(el, name, value) {
  const pattern = PATTERNS[name];
  if (pattern === undefined) throw new TypeError(`attribute not allowed: ${name}`);
  if (typeof value !== 'string' || !pattern.test(value)) throw new TypeError(`attribute ${name}: value not allowed`);
  el.setAttribute(name, value);
}

/** Append text, elements and arrays of them. Strings and numbers become text nodes. */
function append(doc, parent, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    if (Array.isArray(child)) append(doc, parent, child);
    else if (typeof child === 'object' && typeof child.nodeType === 'number') parent.appendChild(child);
    else parent.appendChild(doc.createTextNode(String(child)));
  }
  return parent;
}

/** Build an element: h(doc, 'td', {class: 'num'}, '6'). An attribute that is null or false is left out. */
export function h(doc, tag, attrs, ...children) {
  if (!TAGS.has(tag)) throw new TypeError(`tag not allowed: ${tag}`);
  const el = doc.createElement(tag);
  for (const [name, value] of Object.entries(attrs ?? {})) {
    if (value !== null && value !== undefined && value !== false) setAttr(el, name, value);
  }
  return append(doc, el, children);
}

/** Remove all children. */
export function clear(el) {
  el.replaceChildren();
}
