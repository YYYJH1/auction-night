// The hole-card lens: what the players did not see.
//
// Everything here comes from replay.reveal: private budgets, values and appraisals, secret
// missions, battle plans, mind-reading guesses with the truth, and inner monologues. The
// lens never changes the public record. The stage draws it, or leaves it out, as one switch.

import { stars } from './format.js';
import { STR } from './strings.js';

/** The mission of a seat in words. */
export function missionText(mission) {
  if (mission === null || mission === undefined) return '';
  if (mission.kind === 'collector') return STR.lens.collector(mission.need, mission.tag);
  if (mission.kind === 'shill') return STR.lens.shill(mission.target_lot, mission.threshold_price);
  return STR.lens.missionUnknown;
}

/**
 * Everything the lens shows for one seat at one lot. Missing pieces are null, never invented.
 * `thoughts` are in event order. The opening monologue joins the first lot that was played.
 */
export function lensFor(model, seat, lotNo) {
  const values = model.reveal.values[seat] ?? {};
  const signals = model.reveal.signals[seat] ?? {};
  const appraisal = signals[lotNo] ?? null;
  const value = values[lotNo] ?? null;
  const plan = model.plan(seat);
  const mission = model.mission(seat);
  const opening = lotNo === model.lotNos[0] ? model.planThought(seat) : null;
  const budget = model.reveal.budgets[seat] ?? null;
  const index = lotNo - 1;
  const level = plan?.priorities[index] ?? null;
  return {
    seat,
    budget,
    remaining: budget === null ? null : model.remainingBefore(seat, lotNo),
    valueKind: appraisal !== null ? 'appraisal' : value !== null ? 'value' : null,
    value: appraisal ?? value,
    mission: mission === null ? null : { kind: mission.kind, text: missionText(mission) },
    plan:
      plan === null
        ? null
        : {
            level,
            stars: level === null ? '' : stars(level),
            budget: plan.budget_plan[index] ?? null,
            persona: plan.persona,
          },
    thoughts: [...(opening === null ? [] : [opening]), ...model.thoughts(lotNo, seat)],
    guess: model.guess(lotNo, seat),
  };
}
