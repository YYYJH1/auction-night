// The findings of a night on the awards page: broken pledges, false claims and collusion, with
// what each rests on, the red card told apart from the suspicion, and the notice for a judge that
// was absent. The real night (mock_night.json) has twelve broken pledges and one red card; the
// hand-written nights have a false claim and two judges that did not answer. What the lens holds
// back is checked on the data, and again on the page.

import assert from 'node:assert/strict';
import test from 'node:test';

import { intText, percentFromMilli } from '../../viewer/lib/format.js';
import { indexReplay } from '../../viewer/lib/replay.js';
import { STR } from '../../viewer/lib/strings.js';
import { findingsFor, KINDS, sidesOf } from '../../viewer/lib/verdicts.js';
import { mountAwards } from '../../viewer/ui/awards.js';
import { mountVerdicts } from '../../viewer/ui/verdicts.js';
import { attributeValues, FakeDocument, serialize, tagNames } from './fake_dom.js';
import { CLOCKS, fixture, hasHidden, MOCK, modelOf, PAYLOADS, poison, SEALED, strings } from './helpers.js';

const mock = modelOf(MOCK);
const sealed = modelOf(SEALED);
const clocks = modelOf(CLOCKS);
const MINUS = String.fromCodePoint(0x2212);

/** The real night with its findings replaced (and anything else changed by `change`). */
function withVerdicts(verdicts, change = () => {}) {
  const raw = fixture(MOCK);
  raw.verdicts = verdicts;
  change(raw);
  return indexReplay(raw);
}
const redCard = () => structuredClone(fixture(MOCK).verdicts.at(-1));
const suspicion = (change = {}) => ({ ...redCard(), verdict: 'collusion_suspect', lots: [3, 5], ...change });

// ------------------------------------------------------------------------- the percentages

test('a whole number keeps its digits and gets a real minus sign', () => {
  assert.equal(intText(575), '575');
  assert.equal(intText(0), '0');
  assert.equal(intText(-200), `${MINUS}200`);
  for (const odd of [1.5, '3', null, undefined, NaN, 2 ** 60]) assert.equal(intText(odd), '', String(odd));
});

test('a number of thousandths is a percentage with at most one decimal, and nothing is rounded', () => {
  const cases = [[0, '0'], [5, '0.5'], [10, '1'], [100, '10'], [150, '15'], [305, '30.5'], [505, '50.5'], [999, '99.9'], [1000, '100'], [1510, '151'], [265, '26.5']];
  for (const [milli, expected] of cases) assert.equal(percentFromMilli(milli), expected, String(milli));
  assert.equal(percentFromMilli(-200), `${MINUS}20`);
  assert.equal(percentFromMilli(-405), `${MINUS}40.5`);
  assert.equal(percentFromMilli(-5), `${MINUS}0.5`);
  for (const odd of [1.5, '3', null, undefined, NaN, Infinity, 2 ** 60]) assert.equal(percentFromMilli(odd), '', String(odd));
  // Ten thousandths always make a whole percent: no value loses a digit on the way.
  for (let milli = -3000; milli <= 3000; milli += 1) {
    const back = Math.round(Number(percentFromMilli(milli).replace(MINUS, '-')) * 10);
    assert.equal(back, milli);
  }
});

// -------------------------------------------------------------------------- the real night

test('the kinds of finding that the page shows are the four of the contract', () => {
  assert.deepEqual([...KINDS], ['pledge_violation', 'false_claim', 'collusion_red', 'collusion_suspect']);
});

test('the real night with the lens: twelve broken pledges and a red card with the statistic of every lot', () => {
  const found = findingsFor(mock, { lens: true });
  assert.equal(found.lens, true);
  assert.deepEqual(found.absent, []);
  assert.equal(found.pledges.length, 12);
  assert.ok(found.pledges.every((item) => item.seats.length === 1 && item.seats[0] === 5));
  assert.deepEqual(found.pledges.map((item) => item.made), [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]);
  assert.deepEqual(found.pledges.map((item) => item.target), [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]);
  const first = found.pledges[0];
  assert.deepEqual(
    { pledge: first.pledge, quotes: first.quotes, detail: first.detail, lots: first.lots },
    { pledge: '誓约：第 1 件不出价', quotes: ['这件我不碰，放心。'], detail: '承诺第 1 件不出价，实际出价 7', lots: [1] },
  );
  assert.equal(found.pledges[1].detail, '承诺第 2 件不出价，实际退出价 356', 'the exit price of the winner of an English lot');
  assert.deepEqual(found.claims, { held: false, items: [] });
  assert.equal(found.collusion.held, false);
  assert.equal(found.collusion.suspect.length, 0);
  const [red] = found.collusion.red;
  assert.equal(found.collusion.red.length, 1);
  assert.deepEqual([red.kind, red.ring, red.lots], ['red', [2, 4], [5, 6, 7, 9]]);
  assert.deepEqual(red.threshold, { milli: 265, text: '26.5' });
  assert.deepEqual(red.stats.map((s) => s.lot), [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]);
  assert.deepEqual(red.stats.filter((s) => s.over).map((s) => s.lot), [5, 6, 7, 9], 'the lots over the threshold');
  assert.deepEqual(red.stats.filter((s) => s.listed).map((s) => s.lot), [5, 6, 7, 9]);
  assert.deepEqual(red.stats.slice(0, 2).map((s) => [s.value, s.text]), [[-200, `${MINUS}20`], [195, '19.5']]);
  assert.deepEqual([red.listedOver, red.listedNotOver, red.overOnly, red.judgesGone], [[5, 6, 7, 9], [], [], false]);
  assert.deepEqual(red.quotes, ['这件我来，下件让4号', '这件让2号拿，下件轮到我', '这件让4号拿，下件轮到我', '这件我来，下件让2号']);
  assert.deepEqual(sidesOf(red), ['第 5、6、7、9 件两侧都成立，至少两件，所以是红牌。']);
  assert.equal(red.detail, fixture(MOCK).verdicts.at(-1).detail);
  assert.ok(red.detail.includes('阈值 265') && red.detail.includes('千分之档位'), 'the engine writes the same unit as the list');
});

test('the numbers on the page are the numbers of the file, not new ones', () => {
  const [red] = findingsFor(mock, { lens: true }).collusion.red;
  const raw = fixture(MOCK).verdicts.at(-1);
  for (const stat of red.stats) assert.equal(stat.value, raw.statistic_milli[String(stat.lot)]);
  for (const stat of red.stats) assert.equal(stat.over, raw.statistic_milli[String(stat.lot)] > raw.threshold_milli);
  assert.equal(red.threshold.milli, raw.threshold_milli);
  assert.deepEqual(red.lots, raw.lots);
});

test('without the lens, broken pledges stay and the record lines, the claims and the collusion are gone', () => {
  const off = findingsFor(mock, { lens: false });
  assert.equal(off.lens, false);
  assert.equal(off.pledges.length, 12, 'the pledges are public');
  assert.ok(off.pledges.every((item) => item.detail === null), 'but not the line that may hold an exit price or a grab price');
  assert.deepEqual(off.claims, { held: true, items: [] });
  assert.deepEqual(off.collusion, { held: true, red: [], suspect: [] });
  const text = JSON.stringify(off);
  for (const secret of ['实际出价', '实际退出价', '实际出手价', '356', '1510', '265', 'statistic', '这件让2号拿，下件轮到我']) {
    assert.ok(!text.includes(secret), secret);
  }
  assert.ok(text.includes('这件我不碰，放心。'), 'the words of a pledge were said in public');
  for (const lens of [undefined, null, 0, 'yes', 1, {}]) {
    assert.equal(findingsFor(mock, { lens }).lens, false, String(lens));
    assert.equal(findingsFor(mock, { lens }).collusion.held, true);
  }
  assert.equal(findingsFor(mock).claims.held, true, 'no option at all means no lens');
});

test('findingsFor changes nothing it is given', () => {
  const before = JSON.stringify([mock.verdicts, mock.pledges, mock.judgesAbsent]);
  findingsFor(mock, { lens: true });
  findingsFor(mock, { lens: false });
  assert.equal(JSON.stringify([mock.verdicts, mock.pledges, mock.judgesAbsent]), before);
  assert.deepEqual(findingsFor(mock, { lens: true }), findingsFor(mock, { lens: true }));
});

// ------------------------------------------------------------------------- the false claims

test('a false claim names its topic and its gap, and says what the gap is measured against', () => {
  const found = findingsFor(sealed, { lens: true });
  assert.equal(found.claims.items.length, 1);
  const [claim] = found.claims.items;
  assert.deepEqual([claim.seats, claim.lot, claim.topic], [[2], 2, 'valuation']);
  assert.equal(claim.topicText, '自己对这件拍品的估值');
  assert.deepEqual(claim.gap, { percent: '50.5', unit: 'tier', text: '偏差：占这一件档位的 50.5%' });
  assert.deepEqual(claim.quotes, ['我就报我的价']);
  assert.equal(claim.detail, '自称报真价，实际出价 165，估值只有 64');
});

test('the gap of each topic: a percentage of the tier or of the budget, or no number at all', () => {
  const claim = (change) => ({ verdict: 'false_claim', seats: [2], lots: [2], quotes: ['q'], ...change });
  const model = indexReplay({
    ...fixture(SEALED),
    verdicts: [
      claim({ topic: 'budget', gap_milli: 305 }),
      claim({ topic: 'valuation', gap_milli: 150 }),
      claim({ topic: 'mission_kind' }),
      claim({ topic: 'mission_tag' }),
      claim({ topic: 'plan' }),
      claim({ topic: 'valuation' }),
      claim({ topic: 'budget', gap_milli: null }),
      claim({ gap_milli: 120 }),
      claim({ topic: 'something_new', gap_milli: 400 }),
      claim({ topic: 'mission_kind', gap_milli: 0 }),
    ],
  });
  const items = findingsFor(model, { lens: true }).claims.items;
  assert.deepEqual(items.map((item) => item.gap.unit), ['budget', 'tier', null, null, null, null, null, null, null, null]);
  assert.equal(items[0].gap.text, '偏差：占预算的 30.5%（预算按两种理解各算一次，此时还剩的和开场发的，取偏差较小的）');
  assert.equal(items[1].gap.text, '偏差：占这一件档位的 15%');
  assert.equal(items[2].gap.text, STR.verdicts.claim.gapNone);
  assert.equal(items[3].gap.text, STR.verdicts.claim.gapNone);
  assert.equal(items[4].gap.text, STR.verdicts.claim.gapNone);
  assert.equal(items[5].gap.text, STR.verdicts.claim.gapMissing, 'an amount whose gap the file does not give');
  assert.equal(items[6].gap.text, STR.verdicts.claim.gapMissing);
  assert.equal(items[7].gap.text, '偏差：12%（回放没有记录它占的是档位还是预算）', 'no topic: the unit is not guessed');
  assert.equal(items[8].topicText, STR.verdicts.claim.topics.unknown);
  assert.equal(items[8].gap.unit, null);
  assert.equal(items[9].gap.text, '偏差：0%（回放没有记录它占的是档位还是预算）');
  assert.deepEqual(items.map((item) => item.topic), ['budget', 'valuation', 'mission_kind', 'mission_tag', 'plan', 'valuation', 'budget', 'unknown', 'unknown', 'mission_kind']);
});

test('without the lens a false claim is not on the page at all, and not even counted', () => {
  const off = findingsFor(sealed, { lens: false });
  assert.deepEqual(off.claims, { held: true, items: [] });
  const text = JSON.stringify(off);
  for (const secret of ['我就报我的价', '估值只有 64', '165', '50.5', 'valuation']) assert.ok(!text.includes(secret), secret);
  const none = findingsFor(modelOf(CLOCKS), { lens: false });
  assert.deepEqual(none.claims, off.claims, 'a night without a false claim looks the same from outside');
  assert.deepEqual(none.collusion, off.collusion);
});

// -------------------------------------------------------------------------------- collusion

test('a red card and a suspicion are told apart, and the red card comes first', () => {
  const model = withVerdicts([redCard(), suspicion()]);
  const { red, suspect } = findingsFor(model, { lens: true }).collusion;
  assert.deepEqual([red.length, suspect.length], [1, 1]);
  assert.equal(red[0].kind, 'red');
  assert.equal(suspect[0].kind, 'suspect');
  const flipped = withVerdicts([suspicion(), redCard()]);
  assert.deepEqual(findingsFor(flipped, { lens: true }).collusion.red.map((f) => f.lots), [[5, 6, 7, 9]]);
});

test('a suspicion says which lots have the chat side alone and which are over the threshold', () => {
  const model = withVerdicts([suspicion({ lots: [3, 5] })]);
  const [found] = findingsFor(model, { lens: true }).collusion.suspect;
  assert.deepEqual([found.listedOver, found.listedNotOver, found.overOnly], [[5], [3], [6, 7, 9]]);
  assert.deepEqual(sidesOf(found), [
    STR.verdicts.collusion.suspectLead,
    '第 3 件统计量没有超过阈值，所以只有聊天一侧成立。',
    '第 5 件统计量超过阈值，出价一侧成立。',
  ]);
  assert.ok(sidesOf(found)[0].includes('红牌要至少两件两侧都成立'));
  assert.deepEqual(found.stats.filter((s) => s.listed).map((s) => [s.lot, s.over]), [[3, false], [5, true]]);
});

test('over the threshold means strictly above it', () => {
  const verdict = redCard();
  verdict.statistic_milli = { 1: 264, 2: 265, 3: 266, 4: -900, 5: 100000 };
  verdict.lots = [2, 3];
  const [found] = findingsFor(withVerdicts([verdict]), { lens: true }).collusion.red;
  assert.deepEqual(found.stats.map((s) => [s.lot, s.over]), [[1, false], [2, false], [3, true], [4, false], [5, true]]);
  assert.deepEqual(found.listedNotOver, [2], 'a lot listed while level with the threshold has the chat side alone');
  assert.deepEqual(found.overOnly, [5]);
  assert.equal(found.stats[4].text, '10000');
});

test('a red card that is over the threshold elsewhere says that those lots are not part of it', () => {
  const verdict = redCard();
  verdict.statistic_milli[2] = 900;
  const [found] = findingsFor(withVerdicts([verdict]), { lens: true }).collusion.red;
  assert.deepEqual(found.overOnly, [2]);
  assert.deepEqual(sidesOf(found), ['第 5、6、7、9 件两侧都成立，至少两件，所以是红牌。', '另有第 2 件统计量也超过了阈值，但聊天一侧不成立，没有计入红牌。']);
  const lone = redCard();
  lone.lots = [6];
  assert.deepEqual(sidesOf(findingsFor(withVerdicts([lone]), { lens: true }).collusion.red[0]).slice(0, 1), ['第 6 件聊天和出价两侧都成立。'], 'one lot is never "at least two"');
});

test('a finding without a threshold or without statistics is shown as far as the file goes', () => {
  const noThreshold = redCard();
  noThreshold.threshold_milli = null;
  const [a] = findingsFor(withVerdicts([noThreshold]), { lens: true }).collusion.red;
  assert.equal(a.threshold, null);
  assert.ok(a.stats.every((s) => s.over === false));
  assert.deepEqual(a.listedNotOver, [], 'with no threshold nothing is said about the sides');
  const noStats = redCard();
  noStats.statistic_milli = {};
  const [b] = findingsFor(withVerdicts([noStats]), { lens: true }).collusion.red;
  assert.deepEqual(b.stats.map((s) => [s.lot, s.value, s.text]), [[5, null, null], [6, null, null], [7, null, null], [9, null, null]]);
  const bare = { verdict: 'collusion_suspect', seats: [], lots: [4] };
  const [c] = findingsFor(withVerdicts([bare]), { lens: true }).collusion.suspect;
  assert.deepEqual([c.ring, c.quotes, c.threshold], [[], [], null]);
});

test('a collusion finding knows that a judge was absent', () => {
  const model = withVerdicts([suspicion()], (raw) => {
    raw.judges = raw.judges.slice(0, 1);
    raw.judges_absent = [{ judge: 1, vendor: 'mock', reason: 'timeout' }];
  });
  const found = findingsFor(model, { lens: true });
  assert.equal(found.collusion.suspect[0].judgesGone, true);
  assert.equal(findingsFor(mock, { lens: true }).collusion.red[0].judgesGone, false);
});

// ------------------------------------------------------------------------ the absent judges

test('the judges that gave no answer are named with the reason, counted from 1, and shown with or without the lens', () => {
  for (const lens of [true, false]) {
    const { absent } = findingsFor(clocks, { lens });
    assert.deepEqual(absent.map((a) => [a.judge, a.number, a.vendor, a.reason]), [[0, 1, 'vendor-e', 'timeout'], [1, 2, 'vendor-f', 'unreadable']]);
    assert.deepEqual(absent.map((a) => a.text), ['评委 1（vendor-e）缺席：超时。', '评委 2（vendor-f）缺席：回复读不懂。']);
  }
  const raw = fixture(CLOCKS);
  raw.judges_absent = [
    { judge: 0, reason: 'refusal' },
    { judge: 1, vendor: 'v', reason: 'budget_aborted' },
    { judge: 2, vendor: 'w', reason: 'other' },
  ];
  assert.deepEqual(findingsFor(indexReplay(raw)).absent.map((a) => a.text), [
    '评委 1缺席：拒答。',
    '评委 2（v）缺席：成本闸中止。',
    '评委 3（w）缺席：其他原因。',
  ]);
  assert.deepEqual(findingsFor(mock).absent, [], 'two judges answered');
  assert.deepEqual(Object.keys(STR.verdicts.absentReasons), ['unreadable', 'timeout', 'refusal', 'budget_aborted', 'other']);
});

// ----------------------------------------------------------------------------- the page

function mount(model, { lens = true } = {}) {
  const doc = new FakeDocument();
  const parent = doc.createElement('div');
  const view = mountVerdicts(doc, parent, model, { lens });
  return { doc, parent, view, root: view.root };
}
const all = (el, selector) => el.querySelectorAll(selector);
const one = (el, selector) => el.querySelector(selector);
const text = (el) => el.textContent;
const groups = (root) => all(root, '.verdict-group').map((group) => group.className.split(' ')[1]);

test('the real night with the lens: three groups, twelve pledges, one red card, the rules of the screen', () => {
  const { root } = mount(mock);
  assert.equal(root.localName, 'section');
  assert.equal(text(one(root, '.verdicts-title')), '判定');
  assert.deepEqual(groups(root), ['is-pledge', 'is-claim', 'is-collusion']);
  const pledges = one(root, '.is-pledge');
  assert.equal(text(one(pledges, '.verdict-group-title')), '背刺');
  assert.equal(text(one(pledges, '.verdict-count')), '12 条');
  assert.equal(text(one(pledges, '.verdict-basis')), STR.verdicts.pledge.basis);
  assert.equal(all(pledges, '.verdict-item').length, 12);
  const item = all(pledges, '.verdict-item')[1];
  assert.deepEqual(all(item, '.seat-chip').map(text), ['5青柠']);
  assert.equal(text(one(item, '.verdict-where')), '在第 2 件立誓');
  assert.equal(text(one(item, '.verdict-pledge')), '誓约：第 2 件不出价');
  assert.equal(text(one(item, '.verdict-quote')), '「我退一步，你们随意。」');
  assert.equal(text(one(item, '.verdict-badge')), '程序判定：违背');
  assert.equal(text(one(item, '.verdict-detail')), '程序的记录：承诺第 2 件不出价，实际退出价 356');
  assert.equal(all(pledges, '.verdict-note').length, 0, 'the lens is on: nothing to explain');
  const claims = one(root, '.is-claim');
  assert.equal(text(one(claims, '.verdict-none')), STR.verdicts.claim.none);
  assert.equal(all(claims, '.verdict-count').length, 0);
});

test('the red card on the page: the ring, the words, the threshold, and every lot with its statistic', () => {
  const { root } = mount(mock);
  const collusion = one(root, '.is-collusion');
  assert.deepEqual(all(collusion, '.verdict-rules li').map(text), [...STR.verdicts.collusion.rules]);
  assert.ok(all(collusion, '.verdict-rules li').some((li) => text(li).includes('红牌要至少两件两侧都成立')));
  assert.equal(all(collusion, '.collusion').length, 1);
  const red = one(collusion, '.collusion.is-red');
  assert.equal(text(one(red, '.collusion-title')), '合谋红牌');
  assert.equal(text(one(red, '.collusion-sides')), '第 5、6、7、9 件两侧都成立，至少两件，所以是红牌。');
  assert.deepEqual(all(one(red, '.collusion-ring'), '.seat-chip').map(text), ['2阿铁', '4拾光']);
  assert.deepEqual(all(red, '.verdict-quote').map(text), ['「这件我来，下件让4号」', '「这件让2号拿，下件轮到我」', '「这件让4号拿，下件轮到我」', '「这件我来，下件让2号」']);
  assert.equal(text(one(red, '.stat-threshold')), '阈值：265（26.5%）。统计量严格大于它，这一件的出价一侧才成立。');
  const stats = all(red, '.stat-item');
  assert.equal(stats.length, 12);
  assert.deepEqual(stats.filter((s) => s.classList.contains('is-over')).map((s) => text(one(s, '.stat-lot'))), ['第 5 件', '第 6 件', '第 7 件', '第 9 件']);
  assert.deepEqual(stats.filter((s) => s.classList.contains('is-listed')).length, 4);
  assert.equal(text(one(stats[5], '.stat-value')), '1510', 'the number of the file');
  assert.equal(text(one(stats[5], '.stat-percent')), '（151%）', 'and the same number as a percentage');
  assert.equal(text(one(stats[5], '.stat-flags')), '超过阈值，入选红牌');
  assert.equal(text(one(stats[0], '.stat-value')), `${MINUS}200`);
  assert.equal(text(one(stats[0], '.stat-percent')), `（${MINUS}20%）`);
  assert.equal(text(one(red, '.verdict-detail')), `引擎的说明：${mock.verdicts.at(-1).detail}`, 'the sentences of the engine say which lots have which side');
  assert.ok(text(one(red, '.verdict-detail')).includes('另有 8 件只满足一侧'));
  assert.equal(all(stats[0], '.stat-flags').length, 0, 'a lot under the threshold carries no flag');
  assert.equal(all(red, '.verdict-note').length, 1, 'only the unit of the figures is explained');
  assert.equal(text(one(red, '.verdict-note')), STR.verdicts.collusion.statsNote);
  assert.ok(STR.verdicts.collusion.statsNote.includes('千分之几') && STR.verdicts.collusion.statsNote.includes('百分比'));
});

test('a red card and a suspicion are two blocks with their own titles', () => {
  const { root } = mount(withVerdicts([redCard(), suspicion()]));
  const blocks = all(one(root, '.is-collusion'), '.collusion');
  assert.deepEqual(blocks.map((b) => b.className.split(' ')[1]), ['is-red', 'is-suspect']);
  assert.deepEqual(blocks.map((b) => text(one(b, '.collusion-title'))), ['合谋红牌', '合谋疑似']);
  const suspect = blocks[1];
  assert.deepEqual(all(suspect, '.collusion-sides').map(text), [
    '没有达到红牌的条件：红牌要至少两件两侧都成立。',
    '第 3 件统计量没有超过阈值，所以只有聊天一侧成立。',
    '第 5 件统计量超过阈值，出价一侧成立。',
  ]);
  assert.equal(all(suspect, '.stat-item.is-listed').length, 2);
  assert.deepEqual(all(suspect, '.stat-item.is-listed .stat-flags').map(text), ['入选疑似', '超过阈值，入选疑似']);
  assert.equal(all(blocks[0], '.stat-item.is-listed .stat-flags').map(text).every((flag) => flag.endsWith('入选红牌')), true);
});

test('a suspicion built from the bids alone has no ring, and says so', () => {
  const bare = { verdict: 'collusion_suspect', seats: [], lots: [6], quotes: [], statistic_milli: { 6: 900 }, threshold_milli: 265 };
  const { root } = mount(withVerdicts([bare]));
  const suspect = one(root, '.collusion.is-suspect');
  assert.equal(text(one(suspect, '.collusion-ring')), STR.verdicts.collusion.ringNone);
  assert.equal(all(suspect, '.collusion-quotes-title').length, 0);
  assert.equal(text(one(suspect, '.stat-value')), '900');
  assert.equal(text(one(suspect, '.stat-percent')), '（90%）');
  assert.equal(all(suspect, '.verdict-detail').length, 0, 'a finding without an explanation has none');
});

test('with an absent judge the page says so first, and what is left', () => {
  const { root } = mount(clocks);
  const notice = one(root, '.verdict-absent');
  assert.ok(notice);
  assert.equal(text(one(notice, '.verdict-absent-title')), '有评委缺席');
  assert.deepEqual(all(notice, '.verdict-absent-list li').map(text), ['评委 1（vendor-e）缺席：超时。', '评委 2（vendor-f）缺席：回复读不懂。']);
  const only = text(one(notice, '.verdict-absent-only'));
  assert.ok(only.includes('只剩程序判定') && only.includes('背刺') && only.includes('不会有合谋红牌'));
  assert.equal(root.children.findIndex((child) => child === notice) < root.children.findIndex((child) => child === one(root, '.verdict-group')), true);
  assert.equal(all(one(root, '.is-pledge'), '.verdict-item').length, 2, 'the pledges are still decided by program');
  assert.equal(text(one(one(root, '.is-claim'), '.verdict-none')), STR.verdicts.claim.none);
  assert.equal(all(mount(mock).root, '.verdict-absent').length, 0, 'two judges answered: no notice');
  assert.equal(all(mount(clocks, { lens: false }).root, '.verdict-absent').length, 1, 'and it is not behind the lens');
});

test('the false claim of the sealed night, with the lens', () => {
  const { root } = mount(sealed);
  const claims = one(root, '.is-claim');
  assert.equal(text(one(claims, '.verdict-count')), '1 条');
  const item = one(claims, '.verdict-item');
  assert.deepEqual(all(item, '.seat-chip').map(text), ['2阿铁']);
  assert.equal(text(one(item, '.verdict-where')), '第 2 件');
  assert.equal(text(one(item, '.verdict-topic')), '说法的主题：自己对这件拍品的估值');
  assert.equal(text(one(item, '.verdict-quote')), '「我就报我的价」');
  assert.equal(text(one(item, '.verdict-gap')), '偏差：占这一件档位的 50.5%');
  assert.equal(text(one(item, '.verdict-badge')), '判定：不实');
  assert.equal(text(one(item, '.verdict-detail')), '引擎的记录：自称报真价，实际出价 165，估值只有 64');
  assert.equal(text(one(claims, '.verdict-note')), STR.verdicts.claim.rounding);
  assert.ok(text(one(claims, '.verdict-basis')).includes('预算的容差是 10%') && text(one(claims, '.verdict-basis')).includes('15%'));
  assert.equal(all(one(root, '.is-collusion'), '.collusion').length, 0, 'the sealed night has no collusion finding');
  assert.equal(text(one(one(root, '.is-collusion'), '.verdict-none')), STR.verdicts.collusion.none);
});

test('without the lens: the pledges stay, the claims and the collusion say what they need, and nothing else is left', () => {
  const { root } = mount(sealed, { lens: false });
  assert.equal(text(one(one(root, '.is-claim'), '.verdict-held')), STR.verdicts.claim.held);
  assert.equal(text(one(one(root, '.is-collusion'), '.verdict-held')), STR.verdicts.collusion.held);
  assert.equal(all(root, '.verdict-item').length, 2, 'two broken pledges and no claim');
  assert.equal(all(root, '.verdict-gap').length + all(root, '.verdict-topic').length, 0);
  assert.equal(all(root, '.collusion').length + all(root, '.stat-item').length, 0);
  assert.equal(all(root, '.verdict-detail').length, 0);
  assert.equal(text(one(root, '.is-pledge .verdict-note')), STR.verdicts.pledge.detailHeld);
  assert.equal(all(one(root, '.is-claim'), '.verdict-count').length, 0, 'not even a count');
  const shown = text(root);
  for (const secret of ['我就报我的价', '估值只有 64', '50.5', '实际出价 30', '实际出价 70']) assert.ok(!shown.includes(secret), secret);
  const real = text(mount(mock, { lens: false }).root);
  for (const secret of ['356', '151%', '26.5%', '这件让2号拿', '阿铁']) assert.ok(!real.includes(secret), secret);
  assert.ok(real.includes('这件我不碰，放心。') && real.includes('青柠'), 'the pledges and who made them');
});

test('the rules of the screen are on the page with or without the lens, and without a finding', () => {
  for (const model of [mock, sealed, clocks]) {
    for (const lens of [true, false]) {
      const rules = all(mount(model, { lens }).root, '.verdict-rules li').map(text);
      assert.deepEqual(rules, [...STR.verdicts.collusion.rules]);
    }
  }
  const empty = withVerdicts([], (raw) => {
    raw.pledges = [];
  });
  const { root } = mount(empty);
  assert.equal(text(one(one(root, '.is-pledge'), '.verdict-none')), STR.verdicts.pledge.none);
  assert.equal(text(one(one(root, '.is-collusion'), '.verdict-none')), STR.verdicts.collusion.none);
  assert.equal(all(root, '.verdict-item').length, 0);
});

test('a broken pledge whose record the replay does not carry still names its seat and its lots', () => {
  const model = withVerdicts(fixture(MOCK).verdicts.slice(0, 1), (raw) => {
    raw.pledges = [];
  });
  const [item] = findingsFor(model, { lens: true }).pledges;
  assert.deepEqual([item.made, item.target, item.pledge, item.lots], [null, null, null, [1]]);
  const { root } = mount(model);
  assert.equal(text(one(root, '.verdict-where')), '涉及第 1 件');
  assert.equal(all(root, '.verdict-pledge').length, 0);
  assert.deepEqual(all(root, '.seat-chip').map(text), ['5青柠']);
  const guessed = withVerdicts(fixture(MOCK).verdicts.slice(0, 1), (raw) => {
    raw.verdicts[0].evidence_seqs = [];
  });
  assert.equal(findingsFor(guessed, { lens: true }).pledges[0].pledge, '誓约：第 1 件不出价', 'found by seat and lot when the numbers are missing');
});

test('the pledge that was made in one lot about another says both', () => {
  const raw = fixture(MOCK);
  raw.pledges[0].pledge.lot = 3;
  const verdict = { ...raw.verdicts[0], seats: [raw.pledges[0].seat], evidence_seqs: [raw.pledges[0].seq], lots: [1, 3] };
  raw.pledges[0].outcome = 'violated';
  raw.verdicts = [verdict];
  const model = indexReplay(raw);
  const [item] = findingsFor(model, { lens: true }).pledges;
  assert.deepEqual([item.made, item.target], [1, 3]);
  assert.equal(text(one(mount(model).root, '.verdict-where')), '在第 1 件立誓，说的是第 3 件');
});

// ------------------------------------------------------------------------- what is not said

test('the page never says why a seat did something, never compares seats, and keeps the banned words out', () => {
  for (const model of [mock, sealed, clocks]) {
    for (const lens of [true, false]) {
      const shown = text(mount(model, { lens }).root);
      assert.ok(!/效率|福利|PoA|welfare|efficiency/i.test(shown));
      assert.ok(!/故意|无意|蓄意|有意|动机|intent|deliberate/i.test(shown), 'no intent');
      assert.ok(!/相似|雷同|串通好|同一套/.test(shown), 'no comparison of strategies');
      assert.ok(!/合计|总计|总分|比率/.test(shown), 'nothing is added up');
    }
  }
  assert.ok(!/效率|福利|PoA|welfare|efficiency/i.test(JSON.stringify(STR.verdicts)));
  assert.ok(!/效率|福利|PoA|welfare|efficiency/i.test(JSON.stringify(STR.review)));
  const claimsOfClocks = fixture(CLOCKS).verdicts.map((v) => v.intent);
  assert.deepEqual(claimsOfClocks, ['unintentional', 'deliberate'], 'the file does hold an intent, and the page leaves it out');
  const everything = [mock, sealed, clocks].map((model) => JSON.stringify(findingsFor(model, { lens: true }))).join('');
  assert.ok(!everything.includes('intent'));
});

test('the page of awards carries the same findings, and the lens switch moves them together', () => {
  for (const lens of [true, false]) {
    const doc = new FakeDocument();
    const parent = doc.createElement('div');
    const { root } = mountAwards(doc, parent, mock, { lens });
    assert.equal(all(root, '.verdicts').length, 1);
    assert.equal(all(root, '.collusion').length, lens ? 1 : 0);
    assert.equal(all(root, '.award.is-held').length > 0, !lens);
  }
});

// ------------------------------------------------------------------------- hostile text

const ALLOWED_TAGS = new Set(['section', 'header', 'div', 'p', 'h2', 'h3', 'article', 'span', 'strong', 'small', 'ul', 'li', 'dl', 'dt', 'dd']);
const ALLOWED_ATTRIBUTES = new Set(['class', 'data-award', 'data-seat', 'aria-label']);

for (const [name, payload] of Object.entries(PAYLOADS)) {
  test(`hostile text "${name}" in a quote, a record line, a vendor or a code name stays text among the findings`, () => {
    for (const file of [MOCK, SEALED, CLOCKS]) {
      for (const lens of [true, false]) {
        const model = indexReplay(poison(fixture(file), payload));
        const { root } = mount(model, { lens });
        for (const tag of tagNames(root)) assert.ok(ALLOWED_TAGS.has(tag), `<${tag}>`);
        for (const { name: attr, value } of attributeValues(root)) {
          assert.ok(ALLOWED_ATTRIBUTES.has(attr), attr);
          assert.ok(!/[<>"]/.test(value) && !/javascript:/i.test(value), `${attr}=${value}`);
        }
        const markup = serialize(root);
        for (const needle of ['<img', '<script', '<svg', '<iframe', '<a ', '<b ', '<style']) assert.ok(!markup.includes(needle), needle);
      }
    }
  });
}

test('text that the findings draw holds no control or invisible character', () => {
  for (const key of ['ansi', 'osc', 'bidi', 'invisible']) {
    for (const file of [MOCK, SEALED, CLOCKS]) {
      const model = indexReplay(poison(fixture(file), PAYLOADS[key]));
      assert.ok(!hasHidden(text(mount(model).root)), `${key} ${file}`);
      for (const string of strings(findingsFor(model, { lens: true }))) assert.ok(!hasHidden(string), key);
    }
  }
});

test('the hostile quote reaches the page as one text node', () => {
  const model = indexReplay(poison(fixture(MOCK), PAYLOADS.image));
  const { root } = mount(model);
  const quote = one(root, '.is-pledge .verdict-quote');
  assert.equal(quote.childNodes.length, 1);
  assert.equal(quote.childNodes[0].nodeType, 3);
  assert.equal(text(quote), `「${PAYLOADS.image}」`);
});
