// The leaderboard of the season: reading seasons/s1/leaderboard.json, checking its shape and
// turning its numbers into the text of the table. No DOM here, and nothing throws: a file that
// cannot be read or does not look like a leaderboard gives {ok: false, reason}.

export const BOARD_URL = 'seasons/s1/leaderboard.json';

const SCHEMA = 'auction-night/leaderboard@1';
const ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const MINUS = '\u2212'; // a real minus sign lines up with the digits of a column of figures
const MAX_ENTRIES = 64;
const MAX_NIGHTS = 64;

/**
 * Thousandths as a decimal with one place: 23667 is "23.7", and a negative number gets a minus
 * sign. Halves round away from zero, and a value that rounds to zero has no sign.
 */
export function tenthsText(milli) {
  const tenths = Math.round(Math.abs(milli) / 100);
  const text = `${Math.floor(tenths / 10)}.${tenths % 10}`;
  return milli < 0 && tenths > 0 ? `${MINUS}${text}` : text;
}

/** A rate in thousandths as a percentage with one place: 55 is "5.5%". */
export function percentText(permille) {
  return `${Math.floor(permille / 10)}.${permille % 10}%`;
}

/** The replay page opened on the replay of a night, as a link from the home page. */
export function replayHref(season, night) {
  return `viewer/index.html?replay=../seasons/${season}/${night}/replay.json`;
}

const isInteger = (x) => Number.isSafeInteger(x);
const isName = (x) => typeof x === 'string' && x.length > 0 && x.length <= 80;

function parseEntry(raw) {
  if (typeof raw !== 'object' || raw === null) return null;
  const { entry, label, kind, rank, nights, mean_milli: mean, abstain_rate_milli: abstain } = raw;
  if (!isName(entry) || !isName(label) || (kind !== 'model' && kind !== 'bot')) return null;
  if (!isInteger(rank) || rank < 1 || !isInteger(nights) || nights < 0) return null;
  if (!isInteger(mean) || !isInteger(abstain) || abstain < 0 || abstain > 1000) return null;
  return {
    id: entry,
    label,
    kind,
    rank,
    nights,
    mean: tenthsText(mean),
    sign: Math.sign(mean),
    abstain: percentText(abstain),
  };
}

/**
 * Check a parsed leaderboard and take what the page shows from it: the id of the season, the
 * rows in the order of the file, and the ids of the nights.
 */
export function parseBoard(raw) {
  const no = (reason) => ({ ok: false, reason });
  if (typeof raw !== 'object' || raw === null || raw.schema !== SCHEMA) return no('shape');
  const seasonId = raw.season?.id;
  if (typeof seasonId !== 'string' || !ID.test(seasonId)) return no('shape');
  if (!Array.isArray(raw.entries) || raw.entries.length === 0 || raw.entries.length > MAX_ENTRIES) return no('shape');
  if (!Array.isArray(raw.nights) || raw.nights.length === 0 || raw.nights.length > MAX_NIGHTS) return no('shape');
  const rows = raw.entries.map(parseEntry);
  if (rows.includes(null)) return no('shape');
  const nights = raw.nights.map((night) => night?.night);
  if (!nights.every((id) => typeof id === 'string' && ID.test(id))) return no('shape');
  return { ok: true, seasonId, rows, nights };
}

/**
 * Fetch and check the leaderboard. `fetchFn` is the browser's fetch, passed in so that tests can
 * stand in for it. The reason of a failure is network, status, json or shape.
 */
export async function loadBoard(fetchFn, url = BOARD_URL) {
  let response;
  try {
    response = await fetchFn(url, { credentials: 'omit', cache: 'no-cache', referrerPolicy: 'no-referrer' });
  } catch {
    return { ok: false, reason: 'network' };
  }
  if (!response.ok) return { ok: false, reason: 'status' };
  let raw;
  try {
    raw = await response.json();
  } catch {
    return { ok: false, reason: 'json' };
  }
  return parseBoard(raw);
}
