// A lot in which the cost gate skipped the models says so on the page.
//
// When a night reaches its cost cap the model seats stop acting, and the replay records each
// skipped bid as an abstention with the reason budget_aborted. The page reads that pair, gives the
// lot one line (the same with the hole-card lens on and off, since the cap is no secret of any
// seat) and does not call the silence an abstention. An abstention for any other reason is still
// shown as one. The replay below is the mock night of the exporter with the models muted from
// lot 7, which is the shape the exporter gives a night that the cap cut there
// (tests/e2e/test_e2e_muted.py checks the real thing through the page).

import assert from 'node:assert/strict';
import test from 'node:test';

import { indexReplay } from '../../viewer/lib/replay.js';
import { STR } from '../../viewer/lib/strings.js';
import { createStage } from '../../viewer/ui/stage.js';
import { FakeDocument } from './fake_dom.js';
import { fixture, MOCK, modelOf } from './helpers.js';

const MODELS = [1, 2, 3, 4, 5, 6];
const FROM = 7;
const BID_PHASES = new Set(['sealed', 'dutch', 'english_init', 'english_revise']);

/** The mock night with the bids of the model seats skipped by the gate from lot 7 on. */
function cutReplay(reason = 'budget_aborted', from = FROM) {
  const raw = fixture(MOCK);
  raw.night.status = 'budget_aborted';
  raw.night.ranked = false;
  for (const lot of raw.timeline) {
    if (lot.lot < from) continue;
    for (const action of lot.actions) {
      if (!MODELS.includes(action.seat) || !BID_PHASES.has(action.phase)) continue;
      Object.assign(action, { status: 'abstain', reason, value: null, raw_value: null });
    }
  }
  return raw;
}

function build(model) {
  const doc = new FakeDocument();
  const root = doc.createElement('main');
  return { root, stage: createStage(doc, root, model) };
}

const one = (root, selector) => root.querySelector(selector);
const mutedSeats = (model, lot) => model.lot(lot).muted;
const bidders = (model, lot) => model.lot(lot).actions.filter((a) => MODELS.includes(a.seat)).map((a) => a.seat);

// ------------------------------------------------------------------------------ the model

test('the model lists the seats whose bid the gate skipped, lot by lot, and the first such lot', () => {
  const model = indexReplay(cutReplay());
  assert.equal(model.mutedFrom, FROM);
  for (const lot of model.lotNos) {
    if (lot < FROM) assert.deepEqual(mutedSeats(model, lot), [], `lot ${lot}`);
    else assert.deepEqual(mutedSeats(model, lot), [...new Set(bidders(model, lot))].sort((a, b) => a - b), `lot ${lot}`);
  }
  assert.deepEqual(mutedSeats(model, 7), [1, 2, 3, 4, 5], 'seat 6 had no money left and was not asked');
});

test('a night the gate never cut has no muted lot', () => {
  const model = modelOf(MOCK);
  assert.equal(model.mutedFrom, null);
  assert.ok(model.lots.every((lot) => lot.muted.length === 0));
});

test('an abstention for another reason is not muted', () => {
  for (const reason of ['timeout', 'missing', 'parse_failed', 'refusal', 'not_asked']) {
    const model = indexReplay(cutReplay(reason));
    assert.equal(model.mutedFrom, null, reason);
    assert.ok(model.lots.every((lot) => lot.muted.length === 0), reason);
  }
});

test('a bid that was clamped or kept is not muted either, even from a seat that is named in a cut', () => {
  const raw = cutReplay();
  const lot = raw.timeline.find((entry) => entry.lot === 10);
  const revise = lot.actions.find((a) => a.phase === 'english_revise');
  Object.assign(revise, { status: 'kept_previous', reason: null, value: 40 });
  const model = indexReplay(raw);
  assert.ok(!model.lot(10).actions.find((a) => a.status === 'kept_previous').reason);
  assert.equal(model.mutedFrom, FROM);
});

// -------------------------------------------------------------------------------- the stage

test('every lot of the cut has the line of the cap, with the lens on and off, and no other lot has', () => {
  const model = indexReplay(cutReplay());
  for (const lens of [true, false]) {
    const { root, stage } = build(model);
    for (const lot of model.lotNos) {
      stage.render(lot, lens);
      const note = one(root, '.cap-note');
      if (lot < FROM) {
        assert.equal(note, null, `lot ${lot}`);
        continue;
      }
      assert.ok(note !== null, `lot ${lot}, lens ${lens}`);
      assert.equal(note.textContent, STR.stage.capNote(FROM, mutedSeats(model, lot)));
      assert.equal(note.getAttribute('role'), 'status');
    }
  }
});

test('the line says from which lot and which seats, and does not call the silence an abstention', () => {
  const model = indexReplay(cutReplay());
  const text = STR.stage.capNote(model.mutedFrom, mutedSeats(model, 9));
  assert.ok(text.includes('从第 7 件起') && text.includes('1、2、3、4、5 号') && text.includes('因为费用上限不再行动'));
  assert.ok(!text.includes('弃权'));
});

test('the line comes right after the lot card, before the note of the lens', () => {
  const model = indexReplay(cutReplay());
  const { root, stage } = build(model);
  stage.render(9, true);
  assert.deepEqual(
    [...root.children].map((c) => c.className.split(' ')[0]),
    ['lot-card', 'cap-note', 'lens-note', 'bids', 'overlay', 'seats', 'chat-box', 'host'],
  );
});

test('a lot the gate did not touch is drawn as before', () => {
  const { root, stage } = build(indexReplay(cutReplay()));
  stage.render(3, true);
  assert.deepEqual(
    [...root.children].map((c) => c.className.split(' ')[0]),
    ['lot-card', 'lens-note', 'bids', 'overlay', 'seats', 'chat-box', 'host'],
  );
});

// ------------------------------------------------------------------------- the lens details

function envelopeTexts(model, lot) {
  const { root, stage } = build(model);
  stage.render(lot, true);
  stage.update(stage.durationOf(lot));
  return [...root.querySelectorAll('.env.is-abstain')].map((env) => ({
    bid: one(env, '.env-bid').textContent,
    note: one(env, '.env-note').textContent,
  }));
}

test('under the lens a skipped bid in an envelope reads as not having acted, with the cap as the reason', () => {
  const found = envelopeTexts(indexReplay(cutReplay()), 9);
  assert.equal(found.length, 5);
  for (const env of found) assert.deepEqual(env, { bid: STR.bids.muted, note: STR.bids.reasons.budget_aborted });
});

test('under the lens an abstention for another reason is still an abstention', () => {
  const found = envelopeTexts(indexReplay(cutReplay('timeout')), 9);
  assert.equal(found.length, 5);
  for (const env of found) assert.deepEqual(env, { bid: STR.bids.abstain, note: STR.bids.reasons.timeout });
});

test('under the lens the list of grab prices of a Dutch lot says not acted for a skipped bid', () => {
  const draw = (reason) => {
    const { root, stage } = build(indexReplay(cutReplay(reason)));
    stage.render(8, true);
    return [...root.querySelectorAll('.dut-grab-list li')].map((li) => li.textContent);
  };
  const skipped = draw('budget_aborted');
  assert.equal(skipped.length, 6, 'the five model seats and the bot, which still named a price');
  assert.equal(skipped.filter((line) => line.includes('未行动（成本闸中止）')).length, 5);
  assert.ok(skipped.every((line) => !line.includes('弃权')));
  assert.equal(draw('timeout').filter((line) => line.includes('弃权（超时）')).length, 5);
});
