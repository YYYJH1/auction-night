// Reading a replay.json v1 file: a shape check, and an index with defaults and clean text.
//
// The viewer reads replay.json and nothing else. The field names below are the ones of the
// replay contract (snake_case), so a path such as lot.hammer.true_value means the same here
// and in the file. Everything the viewer shows passes through the cleaners of lib/safe.js on
// the way in, and numbers must be integers or they are treated as missing.
//
// The show-layer parts (awards, bots_reference, highlights, pledges, verdicts, judges_absent and
// the host's recap and prediction card) are optional and read leniently: a replay from before
// they existed opens with empty lists, an award, a highlight or a finding that this viewer does
// not know is kept as 'unknown', and an entry that cannot be read is dropped.

import { isAwardKey } from './awards.js';
import { isFormat } from './format.js';
import { isCategory } from './highlights.js';
import {
  asCoins,
  asInt,
  asLotNo,
  asSeat,
  cleanBlock,
  cleanLine,
  clamp,
  keyInt,
  LIMITS,
  MAX_SEATS,
} from './safe.js';
import { STR } from './strings.js';

export const SCHEMA_ID = 'auction-night/replay@1';
// A text longer than this is refused before it is parsed.
export const MAX_FILE_CHARS = 12_000_000;

const KINDS = new Set(['llm', 'mock', 'bot', 'human']);
const PHASES = new Set(['plan', 'talk', 'english_init', 'english_revise', 'dutch', 'sealed']);
const STATUSES = new Set(['ok', 'clamped', 'abstain', 'kept_previous']);
const PLEDGES = new Set(['no_bid', 'max_bid', 'let_win']);
const UNITS = new Set(['coins', 'count', 'milli', 'none']);
const VERDICT_KINDS = new Set(['pledge_violation', 'false_claim', 'collusion_red', 'collusion_suspect']);
const TOPICS = new Set(['budget', 'valuation', 'mission_kind', 'mission_tag', 'plan']);
const OUTCOMES = new Set(['kept', 'violated', 'void']);
const ABSENCE_REASONS = new Set(['unreadable', 'timeout', 'refusal', 'budget_aborted', 'other']);
// More awards, highlights, findings or predictions than this are not a night, and the rest is
// not read.
const MAX_AWARDS = 40;
const MAX_HIGHLIGHTS = 200;
// A seat can pledge once a lot, so eight seats and 36 lots are 288 pledges at the very most.
const MAX_VERDICTS = 400;
const MAX_PLEDGES = 400;
const MAX_QUOTES = 6;
const MAX_JUDGES = 4;
const MAX_PREDICTIONS = 12;

const isObj = (v) => typeof v === 'object' && v !== null && !Array.isArray(v);
const list = (v) => (Array.isArray(v) ? v : []);
const get = (o, k) => (isObj(o) && Object.hasOwn(o, k) ? o[k] : undefined);

// ----------------------------------------------------------------------------- validation

/** Check the parts the viewer cannot do without. Unknown extra fields are fine. */
export function validateReplay(raw) {
  const errors = [];
  const bad = (message) => {
    if (errors.length < 12) errors.push(message);
  };
  if (!isObj(raw)) return { ok: false, errors: [STR.err.notObject] };
  const schema = get(raw, 'schema');
  if (schema !== SCHEMA_ID) {
    const known = typeof schema === 'string' && schema.startsWith('auction-night/replay@');
    return { ok: false, errors: [known ? STR.err.version(cleanLine(schema, 40)) : STR.err.noSchema] };
  }

  const night = get(raw, 'night');
  const nLots = asLotNo(get(night, 'n_lots'));
  if (!isObj(night)) bad(STR.err.missing('night'));
  else {
    if (typeof get(night, 'id') !== 'string' || get(night, 'id') === '') bad(STR.err.missing('night.id'));
    if (nLots === null) bad(STR.err.invalid('night.n_lots'));
  }

  const seats = get(raw, 'seats');
  if (!Array.isArray(seats) || seats.length < 1 || seats.length > MAX_SEATS) bad(STR.err.invalid('seats'));
  else {
    const seen = new Set();
    seats.forEach((s, i) => {
      const seat = asSeat(get(s, 'seat'));
      if (seat === null || typeof get(s, 'codename') !== 'string') bad(STR.err.invalid(`seats[${i}]`));
      else if (seen.has(seat)) bad(STR.err.invalid(`seats[${i}].seat`));
      seen.add(seat);
    });
  }

  const catalog = get(raw, 'catalog');
  const catalogLots = new Set();
  if (!Array.isArray(catalog) || (nLots !== null && catalog.length !== nLots)) bad(STR.err.invalid('catalog'));
  else {
    catalog.forEach((c, i) => {
      const lot = asLotNo(get(c, 'lot'));
      if (lot === null || typeof get(c, 'name') !== 'string') bad(STR.err.invalid(`catalog[${i}]`));
      else catalogLots.add(lot);
    });
  }

  const timeline = get(raw, 'timeline');
  if (!Array.isArray(timeline) || timeline.length === 0) bad(STR.err.invalid('timeline'));
  else {
    let previous = 0;
    timeline.forEach((t, i) => {
      const lot = asLotNo(get(t, 'lot'));
      const fine = lot !== null && lot > previous && (nLots === null || lot <= nLots) && isObj(get(t, 'hammer'));
      if (!fine) bad(STR.err.invalid(`timeline[${i}]`));
      else if (catalogLots.size > 0 && !catalogLots.has(lot)) bad(STR.err.invalid(`timeline[${i}].lot`));
      if (lot !== null) previous = lot;
    });
  }

  const reveal = get(raw, 'reveal');
  if (!isObj(reveal)) bad(STR.err.missing('reveal'));
  return { ok: errors.length === 0, errors };
}

// ------------------------------------------------------------------------- small readers

const label = (v, max = LIMITS.label) => cleanLine(v, max);
const orNull = (s) => (s === '' ? null : s);

function seatList(v) {
  return list(v)
    .map(asSeat)
    .filter((s) => s !== null);
}

/** {"3": 120} becomes {3: 120}. Entries with a bad key or amount are dropped. */
function seatCoins(v) {
  const out = {};
  if (!isObj(v)) return out;
  for (const [key, value] of Object.entries(v)) {
    const seat = asSeat(keyInt(key));
    const amount = asCoins(value);
    if (seat !== null && amount !== null) out[seat] = amount;
  }
  return out;
}

/** Like seatCoins, but the amounts may be negative: a difference between two earnings. */
function seatInts(v) {
  const out = {};
  if (!isObj(v)) return out;
  for (const [key, value] of Object.entries(v)) {
    const seat = asSeat(keyInt(key));
    const amount = asInt(value);
    if (seat !== null && amount !== null) out[seat] = amount;
  }
  return out;
}

function lotCoins(v) {
  const out = {};
  if (!isObj(v)) return out;
  for (const [key, value] of Object.entries(v)) {
    const lot = asLotNo(keyInt(key));
    const amount = asCoins(value);
    if (lot !== null && amount !== null) out[lot] = amount;
  }
  return out;
}

function seatLotCoins(v) {
  const out = {};
  if (!isObj(v)) return out;
  for (const [key, value] of Object.entries(v)) {
    const seat = asSeat(keyInt(key));
    if (seat !== null) out[seat] = lotCoins(value);
  }
  return out;
}

/** {"3": -200} becomes {3: -200}: a figure of each lot, which can be negative. */
function lotInts(v) {
  const out = {};
  if (!isObj(v)) return out;
  for (const [key, value] of Object.entries(v)) {
    const lot = asLotNo(keyInt(key));
    const amount = asInt(value);
    if (lot !== null && amount !== null) out[lot] = amount;
  }
  return out;
}

/** An event number: a whole number from 1 on, else null. */
function asSeq(v) {
  const n = asInt(v);
  return n !== null && n >= 1 ? n : null;
}

/** {"3": 120} becomes {3: 120}: the event number of the line of each seat. Bad entries are dropped. */
function seatSeqs(v) {
  const out = {};
  if (!isObj(v)) return out;
  for (const [key, value] of Object.entries(v)) {
    const seat = asSeat(keyInt(key));
    const seq = asSeq(value);
    if (seat !== null && seq !== null) out[seat] = seq;
  }
  return out;
}

/**
 * What the file says an award or a highlight rests on: 'public' or 'private'. A text that is
 * neither is a basis this viewer does not know, and it counts as private. Anything that is not a
 * text says nothing (null), and then the viewer's own table decides.
 */
function normalizeBasis(v) {
  if (typeof v !== 'string') return null;
  return v === 'public' ? 'public' : 'private';
}

// ---------------------------------------------------------------------------- the pieces

function normalizeNight(n) {
  return {
    id: label(get(n, 'id')) || '?',
    mode: label(get(n, 'mode'), LIMITS.name),
    status: label(get(n, 'status'), LIMITS.name),
    ranked: get(n, 'ranked') === true,
    n_lots: asLotNo(get(n, 'n_lots')) ?? 0,
    budget_lo: asCoins(get(n, 'budget_lo')),
    budget_hi: asCoins(get(n, 'budget_hi')),
    collectors: asCoins(get(n, 'collectors')) ?? 0,
    shills: asCoins(get(n, 'shills')) ?? 0,
    mission_bonus: asCoins(get(n, 'mission_bonus')),
    season_id: orNull(label(get(n, 'season_id'))),
    deck_id: orNull(label(get(n, 'deck_id'))),
    rotation: asInt(get(n, 'rotation')),
  };
}

function normalizeSeat(s) {
  const seat = asSeat(get(s, 'seat'));
  if (seat === null) return null;
  const kind = get(s, 'kind');
  return {
    seat,
    codename: label(get(s, 'codename'), LIMITS.name) || `${seat} 号`,
    kind: KINDS.has(kind) ? kind : 'unknown',
    vendor: orNull(label(get(s, 'vendor'))),
    endpoint: orNull(label(get(s, 'endpoint'))),
    model: orNull(label(get(s, 'model'))),
    model_returned: orNull(label(get(s, 'model_returned'))),
    persona: orNull(label(get(s, 'persona'))),
    policy: orNull(label(get(s, 'policy'))),
    thinking: orNull(label(get(s, 'thinking'), LIMITS.name)),
    effort: orNull(label(get(s, 'effort'), LIMITS.name)),
  };
}

function normalizeCatalog(c) {
  const lot = asLotNo(get(c, 'lot'));
  if (lot === null) return null;
  const format = get(c, 'format');
  return {
    lot,
    item: label(get(c, 'item')),
    name: label(get(c, 'name')) || `第 ${lot} 件`,
    emoji: cleanLine(get(c, 'emoji'), 8),
    tag: label(get(c, 'tag'), LIMITS.name),
    tier: asCoins(get(c, 'tier')),
    format: isFormat(format) ? format : 'unknown',
    reserve: asCoins(get(c, 'reserve')) ?? 0,
    blurb: cleanLine(get(c, 'blurb'), LIMITS.note),
  };
}

function normalizePledge(p) {
  if (!isObj(p)) return null;
  const kind = get(p, 'kind');
  return {
    kind: PLEDGES.has(kind) ? kind : 'unknown',
    lot: asLotNo(get(p, 'lot')),
    amount: asCoins(get(p, 'amount')),
    to: asSeat(get(p, 'to')),
  };
}

function normalizeAction(a) {
  const seat = asSeat(get(a, 'seat'));
  if (seat === null) return null;
  const phase = get(a, 'phase');
  const status = get(a, 'status');
  return {
    seq: asInt(get(a, 'seq')) ?? 0,
    seat,
    phase: PHASES.has(phase) ? phase : 'unknown',
    status: STATUSES.has(status) ? status : 'unknown',
    reason: orNull(label(get(a, 'reason'), LIMITS.name)),
    value: asCoins(get(a, 'value')),
    raw_value: asCoins(get(a, 'raw_value')),
  };
}

// The reason that the replay gives to an action the cost gate skipped (see docs/contracts.md): the
// seat did not abstain, the cap of the night took the action.
export const MUTED_REASON = 'budget_aborted';

/**
 * The seats whose bid the cost gate skipped in a lot: they did not choose to stay out, the cap
 * of the night did. They are read from the recorded actions, in seat order.
 */
function mutedSeats(actions) {
  const seats = actions.filter((a) => a.status === 'abstain' && a.reason === MUTED_REASON).map((a) => a.seat);
  return [...new Set(seats)].sort((a, b) => a - b);
}

function normalizeDutch(d) {
  if (!isObj(d)) return null;
  return {
    ladder: list(get(d, 'ladder'))
      .map(asCoins)
      .filter((x) => x !== null),
    level: asInt(get(d, 'level')),
    price: asCoins(get(d, 'price')),
    buyer: asSeat(get(d, 'buyer')),
    unsold: get(d, 'unsold') === true,
  };
}

function normalizeHammer(h, trueValues, lot) {
  const winner = asSeat(get(h, 'winner'));
  const format = get(h, 'format');
  return {
    format: isFormat(format) ? format : 'unknown',
    winner,
    price: asCoins(get(h, 'price')) ?? 0,
    payments: seatCoins(get(h, 'payments')),
    bids: isObj(get(h, 'bids')) ? seatCoins(get(h, 'bids')) : null,
    true_value: asCoins(get(h, 'true_value')) ?? trueValues[lot] ?? null,
    unsold: get(h, 'unsold') === true || winner === null,
    tie_broken: get(h, 'tie_broken') === true,
    tied: seatList(get(h, 'tied')),
    newly_out: seatList(get(h, 'newly_out')),
  };
}

// The reference numbers of one lot.
// excess and curse_gap can be negative; expected_revenue_milli is thousandths of a coin.
function normalizeOverlay(o) {
  const flag = get(o, 'to_highest_value');
  return {
    ref_bids: seatCoins(get(o, 'ref_bids')),
    alpha_milli: seatCoins(get(o, 'alpha_milli')),
    regret: seatCoins(get(o, 'regret')),
    to_highest_value: typeof flag === 'boolean' ? flag : null,
    excess: seatInts(get(o, 'excess')),
    expected_revenue_milli: asCoins(get(o, 'expected_revenue_milli')),
    curse_gap: asInt(get(o, 'curse_gap')),
  };
}

function normalizeLot(t, catalog, trueValues, hostLines) {
  const lot = asLotNo(get(t, 'lot'));
  if (lot === null || !isObj(get(t, 'hammer'))) return null;
  const entry = catalog.get(lot) ?? null;
  const format = get(t, 'format');
  const own = cleanLine(get(t, 'host'), LIMITS.note);
  const actions = list(get(t, 'actions')).map(normalizeAction).filter(Boolean);
  return {
    lot,
    format: isFormat(format) ? format : (entry?.format ?? 'unknown'),
    mechanism: label(get(t, 'mechanism'), LIMITS.name),
    first_seq: asInt(get(t, 'first_seq')) ?? 0,
    last_seq: asInt(get(t, 'last_seq')) ?? 0,
    item: entry,
    active: seatList(get(t, 'active')),
    sitting_out: seatList(get(t, 'sitting_out')),
    talk_order: seatList(get(t, 'talk_order')),
    talk: list(get(t, 'talk'))
      .map((x) => {
        const seat = asSeat(get(x, 'seat'));
        if (seat === null) return null;
        return {
          seat,
          text: cleanLine(get(x, 'text'), LIMITS.line),
          pledge: normalizePledge(get(x, 'pledge')),
        };
      })
      .filter(Boolean),
    actions,
    muted: mutedSeats(actions),
    dropouts: list(get(t, 'dropouts'))
      .map((d) => {
        const seat = asSeat(get(d, 'seat'));
        if (seat === null) return null;
        return {
          seat,
          price: asCoins(get(d, 'price')) ?? 0,
          remaining: seatList(get(d, 'remaining')),
          participated: get(d, 'participated') !== false,
        };
      })
      .filter(Boolean),
    dutch: normalizeDutch(get(t, 'dutch')),
    hammer: normalizeHammer(get(t, 'hammer'), trueValues, lot),
    overlay: normalizeOverlay(get(t, 'overlay')),
    host: orNull(own) ?? hostLines.get(lot) ?? null,
    // The event numbers that let a jump land on the exact beat: the line of each seat, and the
    // hammer. A replay from before they existed has an empty table and null.
    talk_seqs: seatSeqs(get(t, 'talk_seqs')),
    hammer_seq: asSeq(get(t, 'hammer_seq')),
  };
}

function normalizeMission(m) {
  const seat = asSeat(get(m, 'seat'));
  if (seat === null) return null;
  const kind = get(m, 'kind');
  return {
    seat,
    kind: kind === 'collector' || kind === 'shill' ? kind : 'unknown',
    completed: get(m, 'completed') === true,
    bonus: asCoins(get(m, 'bonus')) ?? 0,
    tag: orNull(label(get(m, 'tag'), LIMITS.name)),
    need: asInt(get(m, 'need')),
    won_lots: list(get(m, 'won_lots'))
      .map(asLotNo)
      .filter((x) => x !== null),
    target_lot: asLotNo(get(m, 'target_lot')),
    threshold_price: asCoins(get(m, 'threshold_price')),
    final_price: asCoins(get(m, 'final_price')),
    buyer: asSeat(get(m, 'buyer')),
  };
}

function normalizePlan(p) {
  const seat = asSeat(get(p, 'seat'));
  if (seat === null) return null;
  const level = (x) => {
    const n = asInt(x);
    return n !== null && n >= 0 && n <= 3 ? n : null;
  };
  return {
    seat,
    priorities: list(get(p, 'priorities')).map(level),
    budget_plan: list(get(p, 'budget_plan')).map(asCoins),
    persona: orNull(label(get(p, 'persona'))),
  };
}

function normalizeGuess(g) {
  const lot = asLotNo(get(g, 'lot'));
  const seat = asSeat(get(g, 'seat'));
  const target = asSeat(get(g, 'target'));
  if (lot === null || seat === null || target === null) return null;
  return {
    lot,
    seat,
    target,
    guess: asCoins(get(g, 'guess')),
    truth: asCoins(get(g, 'truth')),
    abs_error: asCoins(get(g, 'abs_error')),
  };
}

function normalizeThought(t) {
  const seat = asSeat(get(t, 'seat'));
  const text = cleanLine(get(t, 'text'), LIMITS.thought);
  if (seat === null || text === '') return null;
  const phase = get(t, 'phase');
  return {
    seq: asInt(get(t, 'seq')) ?? 0,
    lot: asLotNo(get(t, 'lot')),
    seat,
    phase: PHASES.has(phase) ? phase : 'unknown',
    text,
  };
}

function normalizeReveal(r) {
  return {
    budgets: seatCoins(get(r, 'budgets')),
    values: seatLotCoins(get(r, 'values')),
    signals: seatLotCoins(get(r, 'signals')),
    true_values: lotCoins(get(r, 'true_values')),
    missions: list(get(r, 'missions')).map(normalizeMission).filter(Boolean),
    plans: list(get(r, 'plans')).map(normalizePlan).filter(Boolean),
    guesses: list(get(r, 'guesses')).map(normalizeGuess).filter(Boolean),
    thoughts: list(get(r, 'thoughts'))
      .map(normalizeThought)
      .filter(Boolean)
      .sort((a, b) => a.seq - b.seq),
  };
}

/** One award of the replay. It is vacant exactly when no valid seat won it. */
function normalizeAward(a) {
  const key = get(a, 'award');
  if (typeof key !== 'string') return null;
  const winners = [...new Set(seatList(get(a, 'winners')))].sort((x, y) => x - y);
  const unit = get(a, 'metric_unit');
  return {
    award: isAwardKey(key) ? key : 'unknown',
    winners,
    metric: asInt(get(a, 'metric')),
    metric_unit: UNITS.has(unit) ? unit : 'none',
    vacant: winners.length === 0,
    basis: normalizeBasis(get(a, 'basis')),
  };
}

/** One highlight. A score outside 0 to 10 is brought back into it; a category we do not know is 'unknown'. */
function normalizeHighlight(h) {
  const id = asInt(get(h, 'id'));
  if (id === null || id < 1) return null;
  const category = get(h, 'category');
  const score = asInt(get(h, 'score'));
  return {
    id,
    category: isCategory(category) ? category : 'unknown',
    score: score === null ? 0 : clamp(score, 0, 10),
    lot: asLotNo(get(h, 'lot')),
    seats: [...new Set(seatList(get(h, 'seats')))],
    first_seq: asInt(get(h, 'first_seq')) ?? 0,
    last_seq: asInt(get(h, 'last_seq')) ?? 0,
    text: cleanLine(get(h, 'text'), LIMITS.note),
    basis: normalizeBasis(get(h, 'basis')),
  };
}

/**
 * One finding of the judges or of the program. Only what the page shows is read: the intent of a
 * finding is left out on purpose, because the page does not say why a seat did what it did.
 * A kind this viewer does not know is kept as 'unknown' (and not shown).
 */
function normalizeVerdict(v) {
  const kind = get(v, 'verdict');
  if (typeof kind !== 'string') return null;
  const topic = get(v, 'topic');
  return {
    kind: VERDICT_KINDS.has(kind) ? kind : 'unknown',
    seats: [...new Set(seatList(get(v, 'seats')))],
    lots: [...new Set(list(get(v, 'lots')).map(asLotNo).filter((n) => n !== null))],
    evidence_seqs: list(get(v, 'evidence_seqs'))
      .map(asSeq)
      .filter((n) => n !== null),
    quotes: list(get(v, 'quotes'))
      .slice(0, MAX_QUOTES)
      .map((q) => cleanLine(q, LIMITS.line))
      .filter((q) => q !== ''),
    detail: cleanLine(get(v, 'detail'), LIMITS.note),
    topic: TOPICS.has(topic) ? topic : typeof topic === 'string' && topic !== '' ? 'unknown' : null,
    gap_milli: asCoins(get(v, 'gap_milli')),
    statistic_milli: lotInts(get(v, 'statistic_milli')),
    threshold_milli: asCoins(get(v, 'threshold_milli')),
  };
}

/** A pledge and how it ended, as the program decided it. Its words are left out: the line is in the talk. */
function normalizePledgeRecord(r) {
  const seq = asSeq(get(r, 'seq'));
  const lot = asLotNo(get(r, 'lot'));
  const seat = asSeat(get(r, 'seat'));
  if (seq === null || lot === null || seat === null) return null;
  const outcome = get(r, 'outcome');
  return { seq, lot, seat, pledge: normalizePledge(get(r, 'pledge')), outcome: OUTCOMES.has(outcome) ? outcome : 'unknown' };
}

/** A judge that gave no usable answer. The number of the judge counts from 0, as in the file. */
function normalizeAbsence(a) {
  const judge = asInt(get(a, 'judge'));
  if (judge === null || judge < 0 || judge >= MAX_JUDGES) return null;
  const reason = get(a, 'reason');
  return {
    judge,
    vendor: orNull(label(get(a, 'vendor'))),
    reason: ABSENCE_REASONS.has(reason) ? reason : 'other',
  };
}

/** The pre-show predictions of the host and what the night made of them, or null when there are none. */
function normalizePredictions(card) {
  if (!isObj(card)) return null;
  const items = list(get(card, 'items'))
    .slice(0, MAX_PREDICTIONS)
    .map((item) => {
      const text = cleanLine(get(item, 'text'), LIMITS.line);
      if (text === '') return null;
      const format = get(item, 'format');
      const hit = get(item, 'hit');
      return {
        format: isFormat(format) ? format : 'unknown',
        claim: orNull(label(get(item, 'claim'), LIMITS.name)),
        text,
        // true, false, or null when the night had nothing to compare
        hit: typeof hit === 'boolean' ? hit : null,
      };
    })
    .filter(Boolean);
  return items.length === 0 ? null : { items, hits: asCoins(get(card, 'hits')) };
}

// ------------------------------------------------------------------------------- the index

/** Group items by "lot:seat". */
function groupBy(items, key) {
  const out = new Map();
  for (const item of items) {
    const k = key(item);
    if (!out.has(k)) out.set(k, []);
    out.get(k).push(item);
  }
  return out;
}

/**
 * Turn a validated replay into the model that the viewer reads. The result is read-only.
 * `lots` holds the lots that were played, `catalog` all announced lots.
 */
export function indexReplay(raw) {
  const reveal = normalizeReveal(get(raw, 'reveal'));
  const night = normalizeNight(get(raw, 'night'));
  const seats = list(get(raw, 'seats'))
    .map(normalizeSeat)
    .filter(Boolean)
    .sort((a, b) => a.seat - b.seat);
  const catalog = new Map(
    list(get(raw, 'catalog'))
      .map(normalizeCatalog)
      .filter(Boolean)
      .map((c) => [c.lot, c]),
  );
  const hostLines = new Map(
    list(get(get(raw, 'host'), 'per_lot'))
      .map((x) => [asLotNo(get(x, 'lot')), orNull(cleanLine(get(x, 'text'), LIMITS.note))])
      .filter(([lot, text]) => lot !== null && text !== null),
  );
  const lots = list(get(raw, 'timeline'))
    .map((t) => normalizeLot(t, catalog, reveal.true_values, hostLines))
    .filter(Boolean)
    .sort((a, b) => a.lot - b.lot);

  const awards = list(get(raw, 'awards')).slice(0, MAX_AWARDS).map(normalizeAward).filter(Boolean);
  const botsReference = list(get(raw, 'bots_reference')).slice(0, MAX_AWARDS).map(normalizeAward).filter(Boolean);
  const highlightMap = new Map();
  for (const light of list(get(raw, 'highlights')).slice(0, MAX_HIGHLIGHTS).map(normalizeHighlight)) {
    if (light !== null && !highlightMap.has(light.id)) highlightMap.set(light.id, light);
  }
  const verdicts = list(get(raw, 'verdicts')).slice(0, MAX_VERDICTS).map(normalizeVerdict).filter(Boolean);
  const pledges = list(get(raw, 'pledges')).slice(0, MAX_PLEDGES).map(normalizePledgeRecord).filter(Boolean);
  const judgesAbsent = new Map();
  for (const absence of list(get(raw, 'judges_absent')).slice(0, MAX_JUDGES * 2).map(normalizeAbsence)) {
    if (absence && !judgesAbsent.has(absence.judge)) judgesAbsent.set(absence.judge, absence);
  }

  const seatMap = new Map(seats.map((s) => [s.seat, s]));
  const lotMap = new Map(lots.map((l) => [l.lot, l]));
  const missions = new Map(reveal.missions.map((m) => [m.seat, m]));
  const plans = new Map(reveal.plans.map((p) => [p.seat, p]));
  const guesses = new Map(reveal.guesses.map((g) => [`${g.lot}:${g.seat}`, g]));
  const thoughts = groupBy(
    reveal.thoughts.filter((t) => t.lot !== null),
    (t) => `${t.lot}:${t.seat}`,
  );
  const planThoughts = new Map(reveal.thoughts.filter((t) => t.phase === 'plan').map((t) => [t.seat, t]));

  return Object.freeze({
    night,
    seats,
    seatNos: seats.map((s) => s.seat),
    catalogLots: [...catalog.values()].sort((a, b) => a.lot - b.lot),
    lots,
    lotNos: lots.map((l) => l.lot),
    // The first lot in which the cost gate skipped a bid, or null for a night it never cut.
    mutedFrom: lots.find((l) => l.muted.length > 0)?.lot ?? null,
    reveal,
    awards,
    botsReference,
    highlights: [...highlightMap.values()],
    highlight: (id) => highlightMap.get(id) ?? null,
    verdicts,
    pledges,
    judgesAbsent: [...judgesAbsent.values()].sort((a, b) => a.judge - b.judge),
    // The host's recap keeps its line breaks, so that it can be set in paragraphs.
    recap: orNull(cleanBlock(get(get(raw, 'host'), 'recap'), LIMITS.block)),
    predictions: normalizePredictions(get(get(raw, 'host'), 'prediction_card')),
    seat: (no) => seatMap.get(no) ?? null,
    codename: (no) => seatMap.get(no)?.codename ?? `${no} 号`,
    lot: (no) => lotMap.get(no) ?? null,
    catalog: (no) => catalog.get(no) ?? null,
    mission: (seat) => missions.get(seat) ?? null,
    plan: (seat) => plans.get(seat) ?? null,
    planThought: (seat) => planThoughts.get(seat) ?? null,
    guess: (lot, seat) => guesses.get(`${lot}:${seat}`) ?? null,
    thoughts: (lot, seat) => thoughts.get(`${lot}:${seat}`) ?? [],
    /** Budget left before a lot opens: the opening budget minus every payment before it. */
    remainingBefore(seat, lotNo) {
      const budget = reveal.budgets[seat];
      if (budget === undefined) return null;
      let left = budget;
      for (const lot of lots) {
        if (lot.lot >= lotNo) break;
        left -= lot.hammer.payments[seat] ?? 0;
      }
      return left;
    },
  });
}

