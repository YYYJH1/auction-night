// The awards: what the replay says about them, and what a reader may see of it.
//
// The show layer decides the awards and writes them into the replay: for each award the seats
// that won (all of them on a tie), a figure and its unit, or "vacant" when nobody reached the
// lower limit, and what the award rests on (its `basis`). This module decides nothing. It puts
// the replay's answer next to the rule that the award is judged by, in words, and it applies the
// hole-card lens: an award whose basis is something the players did not make public (their
// values, their plans, their guesses, their claims about their own cards) is held back while the
// lens is off, as the numbers of the overlay are. awardsFor returns only what the reader may
// see, so a view that draws what it is given cannot leak a private figure.
//
// The basis is read from the replay. The table below is only for a replay that does not carry
// one (a hand-written file from before the field existed), and it must say what the exporter says.
//
// Each award shows its own figure, as recorded.

import { milliText } from './format.js';
import { STR } from './strings.js';

/** The awards in the order of the award table. */
export const AWARD_KEYS = Object.freeze([
  'most_textbook',
  'winners_curse',
  'tilt',
  'bargain',
  'backstabber',
  'best_actor',
  'keeper',
  'mission_master',
  'mind_reader',
  'consistent',
]);

// What each award rests on, for a replay that does not say. 'public' awards follow from things
// the whole table saw (the hammer, the revealed bids, the pledges, the mission results that are
// published after the last lot). 'private' awards need a private value, plan, guess or claim.
const BASIS = Object.freeze({
  most_textbook: 'private',
  winners_curse: 'public',
  tilt: 'public',
  bargain: 'private',
  backstabber: 'public',
  best_actor: 'private',
  keeper: 'public',
  mission_master: 'public',
  mind_reader: 'private',
  consistent: 'private',
});

export function isAwardKey(key) {
  return typeof key === 'string' && AWARD_KEYS.includes(key);
}

/**
 * 'public' or 'private'. The basis that the replay gives wins. Without one, the table decides,
 * and an award this viewer does not know counts as private.
 */
export function awardBasis(key, basis = null) {
  if (basis === 'public' || basis === 'private') return basis;
  return isAwardKey(key) ? BASIS[key] : 'private';
}

/** A figure as the reader sees it, by the unit the replay gave it. */
export function figureText(metric, unit) {
  if (!Number.isSafeInteger(metric)) return null;
  if (unit === 'coins') return STR.awards.coins(metric);
  if (unit === 'milli') return milliText(metric, 3) || String(metric);
  return String(metric);
}

/**
 * The sentence that says what the figure of an award is, or null when there is none to show.
 * An award shows its figure in its own words when the unit is the one the award is judged in,
 * and as a plain recorded number when the replay used another unit.
 */
export function evidenceText(key, metric, unit) {
  const number = figureText(metric, unit);
  if (number === null || unit === 'none') return null;
  const info = STR.awards.items[key];
  if (info && info.metric && info.unit === unit) return info.metric(number);
  return STR.awards.metricPlain(number);
}

/** What the page says of a mission award: the mission is done, with its bonus when it has one. */
function missionEvidence(model, winners) {
  const bonuses = winners.map((seat) => model.mission(seat)?.bonus ?? 0);
  const same = bonuses.length > 0 && bonuses.every((b) => b === bonuses[0] && b > 0);
  return same ? STR.awards.missionBonus(bonuses[0]) : STR.awards.missionDone;
}

function describe(model, entry, lens) {
  const info = STR.awards.items[entry.award] ?? null;
  const basis = awardBasis(entry.award, entry.basis);
  const base = {
    key: entry.award,
    known: info !== null,
    name: info?.name ?? STR.awards.unknownName,
    rule: info?.rule ?? STR.awards.unknownRule,
    floor: info === null ? null : info.floor,
    basis,
  };
  if (basis === 'private' && lens !== true) {
    return { ...base, status: 'held', winners: [], evidence: null, vacantText: null };
  }
  if (entry.winners.length === 0) {
    // The award for false claims needs two judges to find the same words. When one was absent no
    // claim could be found, and "nobody reached the limit" would not be the reason.
    const noJudges = entry.award === 'best_actor' && model.judgesAbsent.length > 0;
    const why = noJudges
      ? STR.awards.vacantNoJudges
      : info === null
        ? STR.awards.vacantPlain
        : info.floor !== null
          ? STR.awards.vacantWhy(info.floor)
          : (info.vacant ?? STR.awards.vacantPlain);
    return { ...base, status: 'vacant', winners: [], evidence: null, vacantText: why };
  }
  const evidence =
    entry.award === 'mission_master'
      ? missionEvidence(model, entry.winners)
      : evidenceText(entry.award, entry.metric, entry.metric_unit);
  return {
    ...base,
    status: entry.winners.length > 1 ? 'tied' : 'won',
    winners: [...entry.winners],
    evidence,
    vacantText: null,
  };
}

/** The awards in table order; an award that this viewer does not know follows, in file order. */
function inTableOrder(entries) {
  const known = AWARD_KEYS.flatMap((key) => entries.filter((e) => e.award === key).slice(0, 1));
  const rest = entries.filter((e) => !isAwardKey(e.award));
  return [...known, ...rest];
}

/**
 * Everything the awards page may show.
 *   lens    the switch it was made for
 *   items   the awards for the model seats: {key, known, name, rule, floor, basis, status,
 *           winners, evidence, vacantText}. status is 'won', 'tied', 'vacant' or 'held' (the
 *           lens is off and the award rests on something private: no winner, no figure)
 *   bots    the same for the reference list of the bots
 *   held    how many awards the lens holds back, in both lists
 */
export function awardsFor(model, { lens = false } = {}) {
  const on = lens === true;
  const items = inTableOrder(model.awards).map((entry) => describe(model, entry, on));
  const bots = inTableOrder(model.botsReference).map((entry) => describe(model, entry, on));
  const held = [...items, ...bots].filter((item) => item.status === 'held').length;
  return { lens: on, items, bots, held };
}
