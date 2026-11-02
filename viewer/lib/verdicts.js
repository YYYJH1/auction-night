// The findings of a night: what the program and the judges decided, and what a reader may see.
//
// There are three kinds. A broken pledge (背刺) is decided by the program, from a structured
// pledge that was made in the public channel and from the bids that were recorded for the lot. A
// false claim (不实说法) needs two judges to find the same words and the deal to contradict them.
// A collusion finding (合谋) puts the chat and the bids side by side: a red card needs two lots
// with both, anything less is a suspicion. This module decides nothing and recomputes nothing. It
// reads what the replay recorded, and it applies the hole-card lens.
//
// What the lens holds back, and why (findings have no `basis` of their own, so the partition is
// made here, by the same test as for the awards: does the finding need something the players did
// not make public?):
//   broken pledges   public. The pledge was made in the public channel and the program decided it
//                    from the bids recorded for the lot. Only the engine's own record line stays
//                    behind the lens: it can name the exit price of the winner of an English lot
//                    or a grab price of a Dutch lot, and the table never learns those.
//   false claims     the whole finding. A claim about one's own cards is false only against the
//                    private cards, so the finding itself, its seat, its topic and its count
//                    would tell what the cards are not. (The award for it is held back the same
//                    way, and a list of findings would give it away.)
//   collusion        the whole finding, red or suspicion. The bid side is computed against the
//                    textbook bid of each seat, which needs its private value and budget, and a
//                    red card needs that side. The rules of the screen are public and stay.
// The judges that are absent are a fact about the show and not about the cards: always shown.
//
// Intent is not read at all. The page never says why a seat did what it did.

import { percentFromMilli } from './format.js';
import { STR } from './strings.js';

/** The verdict kinds of the replay that this page shows. */
export const KINDS = Object.freeze(['pledge_violation', 'false_claim', 'collusion_red', 'collusion_suspect']);

const lotsText = (lots) => STR.verdicts.lots(lots);

/** The record of the broken pledge that a verdict is about, or null. */
function pledgeOf(model, verdict) {
  const mine = model.pledges.filter((r) => r.outcome === 'violated' && verdict.seats.includes(r.seat));
  return (
    mine.find((r) => verdict.evidence_seqs.includes(r.seq)) ??
    mine.find((r) => verdict.lots.includes(r.lot)) ??
    null
  );
}

function describePledge(model, verdict, lens) {
  const record = pledgeOf(model, verdict);
  const nameOf = (seat) => `「${model.codename(seat)}」`;
  const pledge = record?.pledge ?? null;
  return {
    seats: [...verdict.seats],
    lots: [...verdict.lots],
    made: record?.lot ?? null,
    target: pledge?.lot ?? null,
    pledge: pledge === null ? null : STR.stage.pledge(pledge, nameOf),
    quotes: [...verdict.quotes],
    detail: lens && verdict.detail !== '' ? verdict.detail : null,
  };
}

/** The gap of a false claim in words, and the unit it is measured in. */
function gapOf(verdict) {
  const { topic, gap_milli: gap } = verdict;
  const copy = STR.verdicts.claim;
  if (gap === null) {
    const plain = topic === 'mission_kind' || topic === 'mission_tag' || topic === 'plan';
    return { percent: null, unit: null, text: plain ? copy.gapNone : copy.gapMissing };
  }
  const percent = percentFromMilli(gap);
  if (topic === 'valuation') return { percent, unit: 'tier', text: copy.gapTier(percent) };
  if (topic === 'budget') return { percent, unit: 'budget', text: copy.gapBudget(percent) };
  return { percent, unit: null, text: copy.gapPlain(percent) };
}

function describeClaim(verdict) {
  const topic = verdict.topic ?? 'unknown';
  return {
    seats: [...verdict.seats],
    lot: verdict.lots[0] ?? null,
    topic,
    topicText: STR.verdicts.claim.topics[topic] ?? STR.verdicts.claim.topics.unknown,
    gap: gapOf(verdict),
    quotes: [...verdict.quotes],
    detail: verdict.detail === '' ? null : verdict.detail,
  };
}

/**
 * One collusion finding with every screened lot marked. A lot is over the threshold when its
 * statistic is strictly above it. `listed` are the lots that the finding names: all of them have
 * both sides in a red card, and at least one side in a suspicion, so a listed lot that is not
 * over the threshold has the chat side alone.
 */
function describeCollusion(verdict, absent) {
  const red = verdict.kind === 'collusion_red';
  const threshold = verdict.threshold_milli;
  const listed = new Set(verdict.lots);
  const lots = [...new Set([...Object.keys(verdict.statistic_milli).map(Number), ...verdict.lots])].sort((a, b) => a - b);
  const stats = lots.map((lot) => {
    const value = verdict.statistic_milli[lot] ?? null;
    return {
      lot,
      value,
      text: value === null ? null : percentFromMilli(value),
      over: threshold !== null && value !== null && value > threshold,
      listed: listed.has(lot),
    };
  });
  const over = stats.filter((s) => s.over).map((s) => s.lot);
  return {
    kind: red ? 'red' : 'suspect',
    ring: [...verdict.seats],
    quotes: [...verdict.quotes],
    lots: [...verdict.lots],
    stats,
    threshold: threshold === null ? null : { milli: threshold, text: percentFromMilli(threshold) },
    detail: verdict.detail === '' ? null : verdict.detail,
    // The listed lots that the statistic puts over the threshold, and those it does not.
    listedOver: verdict.lots.filter((lot) => over.includes(lot)),
    listedNotOver: threshold === null ? [] : verdict.lots.filter((lot) => !over.includes(lot)),
    // Over the threshold but not named: the bid side alone, which a red card does not count.
    overOnly: over.filter((lot) => !listed.has(lot)),
    judgesGone: absent,
  };
}

/** The judges that gave no usable answer, with the reason in words. */
function absentJudges(model) {
  return model.judgesAbsent.map((a) => {
    const reasonText = STR.verdicts.absentReasons[a.reason] ?? STR.verdicts.absentReasons.other;
    return {
      judge: a.judge,
      number: a.judge + 1,
      vendor: a.vendor,
      reason: a.reason,
      reasonText,
      text: STR.verdicts.absentOne(a.judge + 1, a.vendor, reasonText),
    };
  });
}

/** The sentences that say which side each listed lot has, from what the replay recorded. */
export function sidesOf(finding) {
  const copy = STR.verdicts.collusion;
  const out = [];
  if (finding.kind === 'red') {
    if (finding.lots.length >= 2) out.push(copy.redLead(lotsText(finding.lots)));
    else if (finding.lots.length > 0) out.push(copy.sidesBoth(lotsText(finding.lots)));
    if (finding.overOnly.length > 0) out.push(copy.sidesBidOnly(lotsText(finding.overOnly)));
    return out;
  }
  out.push(copy.suspectLead);
  if (finding.listedNotOver.length > 0) out.push(copy.sidesChat(lotsText(finding.listedNotOver)));
  if (finding.listedOver.length > 0) out.push(copy.sidesBid(lotsText(finding.listedOver)));
  return out;
}

/**
 * Everything the findings may show.
 *   lens       the switch it was made for
 *   absent     [{judge, number, vendor, reason, reasonText, text}] the judges that gave no usable
 *              answer (number counts from 1); always shown, whatever the lens
 *   pledges    [{seats, lots, made, target, pledge, quotes, detail}] the broken pledges, in the
 *              order of the file. lots are the lots the finding names, made the lot where the
 *              pledge was made and target the lot it was about (null if the replay does not
 *              carry the pledge), pledge the pledge in words (null likewise), detail the record
 *              of the program (null without the lens)
 *   claims     {held, items}  held is true without the lens, and then items is empty. An item is
 *              {seats, lot, topic, topicText, gap: {percent, unit, text}, quotes, detail}
 *   collusion  {held, red, suspect}  likewise. An entry is {kind, ring, quotes, lots, stats:
 *              [{lot, value, text, over, listed}], threshold: {milli, text}, detail, listedOver,
 *              listedNotOver, overOnly, judgesGone}. value is the statistic of the file, in
 *              thousandths of the tier, and text the same number as a percentage
 */
export function findingsFor(model, { lens = false } = {}) {
  const on = lens === true;
  const absent = absentJudges(model);
  const of = (kind) => model.verdicts.filter((v) => v.kind === kind);
  // Without the lens the claims and the collusion are not even described: nothing of them is made.
  const collusion = (kind) => (on ? of(kind).map((v) => describeCollusion(v, absent.length > 0)) : []);
  return {
    lens: on,
    absent,
    pledges: of('pledge_violation').map((v) => describePledge(model, v, on)),
    claims: { held: !on, items: on ? of('false_claim').map(describeClaim) : [] },
    collusion: { held: !on, red: collusion('collusion_red'), suspect: collusion('collusion_suspect') },
  };
}
