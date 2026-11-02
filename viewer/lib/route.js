// Addresses: deep links into a night, and the replay file named in the query string.
//
// A deep link lives in the fragment, so a static host serves the same page for every link:
//   #/n/<night>/lot/<k>        a lot
//   #/n/<night>/moment/<id>    the lot of a highlight, at the moment where it begins
//   #/n/<night>/recap          the three-minute version of the night
//   #/n/<night>/highlights     the list of highlights
//   #/n/<night>/awards         the awards and the findings of the judges
//   #/n/<night>/review         the review of the host: its recap and its predictions
// The replay itself comes from  ?replay=<path>  or from the file picker. A path must stay on
// the same site: relative, no scheme, ending in .json.

const PATTERN = /^#\/n\/([^/]+)(?:\/(?:(lot|moment)\/([1-9][0-9]{0,2})|(recap|highlights|awards|review)))?$/;

/** The pages of a night that are not a lot. */
export const PAGES = Object.freeze(['recap', 'highlights', 'awards', 'review']);

// encodeURIComponent leaves ! ' ( ) * alone. Encode them too, so a link stays inside the
// character set that the DOM helper accepts for href.
function encodeSegment(text) {
  return encodeURIComponent(text).replace(
    /[!'()*]/g,
    (c) => `%${c.charCodeAt(0).toString(16).toUpperCase()}`,
  );
}

/** The fragment of a lot: #/n/<night>/lot/<k>. */
export function formatHash(nightId, lot) {
  return `#/n/${encodeSegment(String(nightId))}/lot/${lot}`;
}

/** The fragment of a highlight: #/n/<night>/moment/<id>. */
export function formatMomentHash(nightId, moment) {
  return `#/n/${encodeSegment(String(nightId))}/moment/${moment}`;
}

/** The fragment of a page of the night: #/n/<night>/recap, highlights, awards or review. */
export function formatPageHash(nightId, page) {
  if (!PAGES.includes(page)) throw new RangeError(`not a page: ${page}`);
  return `#/n/${encodeSegment(String(nightId))}/${page}`;
}

/**
 * Read a fragment. Returns {night, view, lot, moment} or null when the fragment is not one of
 * ours. `view` is 'lot', 'moment', 'recap', 'highlights', 'awards' or 'review', and null for a
 * link to the night alone. `lot` and `moment` are numbers when the view names one, else null.
 * The numbers are only checked for shape: the caller knows which lots and highlights exist.
 */
export function parseHash(hash) {
  if (typeof hash !== 'string' || hash.length > 400) return null;
  const match = PATTERN.exec(hash);
  if (match === null) return null;
  let night;
  try {
    night = decodeURIComponent(match[1]);
  } catch {
    return null;
  }
  const number = match[3] === undefined ? null : Number(match[3]);
  const view = match[2] ?? match[4] ?? null;
  return {
    night,
    view,
    lot: view === 'lot' ? number : null,
    moment: view === 'moment' ? number : null,
  };
}

/** The value of ?replay=..., or null. */
export function replayParam(search) {
  const value = new URLSearchParams(search).get('replay');
  return value === null || value === '' ? null : value;
}

const SCHEME = /^[A-Za-z][A-Za-z0-9+.-]*:/;
const FORBIDDEN = /[\u0000-\u001F\u007F\\]/;

/**
 * Resolve a ?replay= value against the page address. Returns {ok: true, url} for a relative
 * path that stays on the same site and names a .json file, else {ok: false, reason}.
 */
export function resolveReplayUrl(param, baseHref) {
  const no = (reason) => ({ ok: false, reason });
  if (typeof param !== 'string' || param === '') return no('empty');
  if (param.length > 300) return no('long');
  if (FORBIDDEN.test(param)) return no('characters');
  if (SCHEME.test(param)) return no('scheme');
  if (param.startsWith('//')) return no('network-path');
  let base;
  let url;
  try {
    base = new URL(baseHref);
    url = new URL(param, base);
  } catch {
    return no('unparsable');
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return no('protocol');
  if (url.origin !== base.origin) return no('origin');
  if (url.username !== '' || url.password !== '') return no('credentials');
  if (!/\.json$/i.test(url.pathname)) return no('extension');
  return { ok: true, url: url.href };
}
