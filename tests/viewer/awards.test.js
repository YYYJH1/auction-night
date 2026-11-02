import assert from 'node:assert/strict';
import test from 'node:test';

import { awardBasis, awardsFor, AWARD_KEYS, evidenceText, figureText, isAwardKey } from '../../viewer/lib/awards.js';
import { readFileSync } from 'node:fs';

import { indexReplay } from '../../viewer/lib/replay.js';
import { STR } from '../../viewer/lib/strings.js';
import { mountAwards } from '../../viewer/ui/awards.js';
import { attributeValues, FakeDocument, serialize, tagNames } from './fake_dom.js';
import { CLOCKS, fixture, hasHidden, MOCK, modelOf, PAYLOADS, poison, SEALED, strings, VIEWER } from './helpers.js';

const sealed = modelOf(SEALED);
const clocks = modelOf(CLOCKS);
const mock = modelOf(MOCK);
const PRIVATE = ['most_textbook', 'bargain', 'best_actor', 'mind_reader', 'consistent'];
const byKey = (items) => Object.fromEntries(items.map((item) => [item.key, item]));

/** The sealed night with the awards replaced. */
function withAwards(awards, bots = []) {
  const raw = fixture(SEALED);
  raw.awards = awards;
  raw.bots_reference = bots;
  return indexReplay(raw);
}

// ------------------------------------------------------------------------------ the keys

test('the ten awards are listed in the order of the award table, and each knows what it rests on', () => {
  assert.deepEqual([...AWARD_KEYS], [
    'most_textbook', 'winners_curse', 'tilt', 'bargain', 'backstabber', 'best_actor', 'keeper', 'mission_master', 'mind_reader', 'consistent',
  ]);
  assert.deepEqual(AWARD_KEYS.filter((key) => awardBasis(key) === 'private'), PRIVATE);
  assert.equal(awardBasis('a_future_award'), 'private', 'an award that is not known counts as private');
  assert.ok(isAwardKey('tilt') && !isAwardKey('Tilt') && !isAwardKey(null) && !isAwardKey('toString'));
  assert.deepEqual(Object.keys(STR.awards.items), [...AWARD_KEYS], 'one text for each award, in the same order');
});

test('every award has a name, a rule and a way to say its figure', () => {
  for (const key of AWARD_KEYS) {
    const info = STR.awards.items[key];
    assert.ok(info.name && info.rule, key);
    assert.ok(info.floor === null || typeof info.floor === 'string', key);
    if (info.floor === null) assert.ok(info.vacant, `${key} has no floor, so it says in words why it can be vacant`);
    if (info.metric !== null) assert.ok(['coins', 'count', 'milli'].includes(info.unit), key);
  }
  assert.equal(new Set(AWARD_KEYS.map((key) => STR.awards.items[key].name)).size, 10, 'the names differ');
});

test('the texts of the awards stay where the tests of the award table read them', () => {
  // The Python tests find the awards in lib/strings.js by their indentation: an object that opens
  // at six spaces is an award. Any other text of the page must not be written that way.
  const source = readFileSync(`${VIEWER}lib/strings.js`, 'utf8');
  const found = [...source.matchAll(/^ {6}(\w+): Object\.freeze\(\{\n(.*?)\n {6}\}\),$/gms)].map((match) => match[1]);
  assert.deepEqual(found, [...AWARD_KEYS]);
});

// ----------------------------------------------------------------------------- figures

test('a figure is written by the unit that the replay gave it, with whole numbers only', () => {
  assert.equal(figureText(35, 'coins'), '35 金币');
  assert.equal(figureText(32, 'milli'), '0.032');
  assert.equal(figureText(750, 'milli'), '0.750');
  assert.equal(figureText(0, 'milli'), '0.000');
  assert.equal(figureText(2, 'count'), '2');
  assert.equal(figureText(1.5, 'count'), null);
  assert.equal(figureText(null, 'coins'), null);
  assert.equal(figureText('3', 'coins'), null);
});

test('an award says its figure in its own words, and a figure in another unit as the plain number that was recorded', () => {
  assert.equal(evidenceText('winners_curse', 14, 'coins'), '成交价比真值高 14 金币');
  assert.equal(evidenceText('tilt', 35, 'coins'), '没拍到却付了 35 金币');
  assert.equal(evidenceText('bargain', 33, 'coins'), '估值比成交价高 33 金币');
  assert.equal(evidenceText('backstabber', 1, 'count'), '违背了 1 条誓约');
  assert.equal(evidenceText('best_actor', 2, 'count'), '2 条说法被判为不实');
  assert.equal(evidenceText('keeper', 3, 'count'), '3 条誓约全部遵守');
  assert.equal(evidenceText('most_textbook', 32, 'milli'), '平均差距 0.032 个档位');
  assert.equal(evidenceText('mind_reader', 205, 'milli'), '平均误差 0.205（预算范围的倍数）');
  assert.equal(evidenceText('consistent', 750, 'milli'), '一致率 0.750');
  assert.equal(evidenceText('backstabber', 3, 'coins'), '回放记录的数字：3 金币');
  assert.equal(evidenceText('some_future_award', 7, 'count'), '回放记录的数字：7');
  assert.equal(evidenceText('tilt', null, 'coins'), null);
  assert.equal(evidenceText('tilt', 5, 'none'), null);
});

// ----------------------------------------------------------------------- the sealed night

test('with the lens: ten awards in table order, as the replay recorded them', () => {
  const shown = awardsFor(sealed, { lens: true });
  assert.equal(shown.lens, true);
  assert.deepEqual(shown.items.map((item) => item.key), [...AWARD_KEYS]);
  assert.deepEqual(shown.items.map((item) => item.status), ['vacant', 'won', 'won', 'won', 'tied', 'won', 'vacant', 'won', 'vacant', 'vacant']);
  const items = byKey(shown.items);
  assert.deepEqual(items.winners_curse.winners, [3]);
  assert.equal(items.winners_curse.evidence, '成交价比真值高 14 金币');
  assert.deepEqual(items.tilt.winners, [1]);
  assert.equal(items.tilt.evidence, '没拍到却付了 35 金币');
  assert.deepEqual(items.bargain.winners, [3]);
  assert.equal(items.bargain.evidence, '估值比成交价高 33 金币');
  assert.deepEqual(items.backstabber.winners, [1, 4], 'a tie names every winner');
  assert.equal(items.backstabber.evidence, '违背了 1 条誓约');
  assert.deepEqual(items.best_actor.winners, [2]);
  assert.deepEqual(items.mission_master.winners, [4]);
  assert.equal(items.mission_master.evidence, '完成了秘密任务，奖金 150', 'the bonus comes from the mission record');
  assert.equal(shown.held, 0);
});

test('an award nobody got is vacant, and says which limit was not reached', () => {
  const items = byKey(awardsFor(sealed, { lens: true }).items);
  assert.equal(items.most_textbook.vacantText, '没有座位达到下限（至少 6 次有效出价）。');
  assert.equal(items.mind_reader.vacantText, '没有座位达到下限（至少 8 次有效猜测）。');
  assert.equal(items.consistent.vacantText, '没有座位达到下限（至少 10 件拍品可判）。');
  assert.equal(items.keeper.vacantText, '没有座位立过至少 2 条誓约并且全部遵守。', 'an award without a floor names its condition');
  for (const key of ['most_textbook', 'keeper', 'mind_reader', 'consistent']) {
    assert.deepEqual(items[key].winners, [], key);
    assert.equal(items[key].evidence, null, key);
  }
  assert.equal(items.keeper.floor, null);
  assert.equal(items.most_textbook.floor, '至少 6 次有效出价');
});

test('every award carries its rule, and the rule is the same whoever won', () => {
  for (const item of awardsFor(sealed, { lens: true }).items) {
    assert.equal(item.rule, STR.awards.items[item.key].rule);
    assert.equal(item.name, STR.awards.items[item.key].name);
    assert.ok(item.rule.length > 10);
  }
});

test('the bots are a reference of their own and never in the list of the models', () => {
  const shown = awardsFor(sealed, { lens: true });
  assert.deepEqual(shown.bots.map((item) => [item.key, item.status]), [
    ['most_textbook', 'vacant'], ['winners_curse', 'vacant'], ['tilt', 'won'], ['bargain', 'vacant'],
  ]);
  const tilt = shown.bots.find((item) => item.key === 'tilt');
  assert.deepEqual(tilt.winners, [5]);
  assert.equal(tilt.evidence, '没拍到却付了 52 金币');
  const bots = new Set(sealed.seats.filter((seat) => seat.kind === 'bot').map((seat) => seat.seat));
  for (const item of shown.items) for (const seat of item.winners) assert.ok(!bots.has(seat), `${item.key} names a bot`);
});

test('the other night: one tie, one award with a figure of two pledges, and a reference that is all vacant', () => {
  const shown = awardsFor(clocks, { lens: true });
  const items = byKey(shown.items);
  assert.deepEqual(items.backstabber.winners, [1, 3]);
  assert.equal(items.backstabber.status, 'tied');
  assert.deepEqual(items.keeper.winners, [2]);
  assert.equal(items.keeper.evidence, '2 条誓约全部遵守');
  assert.equal(shown.items.filter((item) => item.status === 'vacant').length, 8);
  assert.ok(shown.bots.every((item) => item.status === 'vacant'));
});

// -------------------------------------------------------------------------------- the lens

test('without the lens an award that rests on something private shows no winner, no figure and no vacancy', () => {
  const shown = awardsFor(sealed, { lens: false });
  assert.equal(shown.lens, false);
  const held = shown.items.filter((item) => item.status === 'held').map((item) => item.key);
  assert.deepEqual(held, PRIVATE);
  for (const item of shown.items.filter((entry) => entry.status === 'held')) {
    assert.deepEqual(item.winners, [], item.key);
    assert.equal(item.evidence, null, item.key);
    assert.equal(item.vacantText, null, item.key);
    assert.ok(item.name && item.rule, 'the rule of the award stays readable');
  }
  const items = byKey(shown.items);
  assert.deepEqual(items.winners_curse.winners, [3], 'the public awards are unchanged');
  assert.deepEqual(items.backstabber.winners, [1, 4]);
  assert.equal(items.mission_master.evidence, '完成了秘密任务，奖金 150');
  assert.equal(shown.held, 5 + 2, 'five of the models and two of the bots');
});

test('without the lens no private figure of any award is anywhere in the result', () => {
  for (const model of [sealed, clocks]) {
    const shown = awardsFor(model, { lens: false });
    const text = JSON.stringify(shown);
    assert.ok(!text.includes('估值比成交价高'), 'the bargain figure is a private value in disguise');
    assert.ok(!text.includes('说法被判为不实'));
    for (const raw of [...model.awards, ...model.botsReference]) {
      if (awardBasis(raw.award) === 'public') continue;
      const item = [...shown.items, ...shown.bots].find((entry) => entry.key === raw.award);
      assert.equal(item.status, 'held');
    }
  }
  for (const lens of [undefined, null, 0, 'yes', 1]) {
    assert.equal(awardsFor(sealed, { lens }).lens, false, `lens ${String(lens)} is not the lens`);
    assert.equal(awardsFor(sealed, { lens }).held, 7);
  }
  assert.equal(awardsFor(sealed).held, 7, 'no option at all means no lens');
});

// ------------------------------------------------------------------------------- the basis

test('the basis that the replay gives wins over the table, in both directions', () => {
  const model = withAwards(
    [
      { award: 'most_textbook', winners: [1], metric: 12, metric_unit: 'milli', basis: 'public' },
      { award: 'winners_curse', winners: [3], metric: 14, metric_unit: 'coins', basis: 'private' },
      { award: 'tilt', winners: [2], metric: 40, metric_unit: 'coins' },
      { award: 'bargain', winners: [4], metric: 9, metric_unit: 'coins' },
      { award: 'a_future_award', winners: [5], metric: 1, metric_unit: 'count', basis: 'public' },
      { award: 'another_future_award', winners: [6], metric: 2, metric_unit: 'count' },
    ],
    [
      { award: 'most_textbook', winners: [7], metric: 0, metric_unit: 'milli', basis: 'public' },
      { award: 'tilt', winners: [8], metric: 28, metric_unit: 'coins', basis: 'private' },
    ],
  );
  const off = awardsFor(model, { lens: false });
  const items = byKey(off.items.filter((item) => item.known));
  assert.equal(items.most_textbook.status, 'won', 'the table says private, the file says public: shown without the lens');
  assert.deepEqual(items.most_textbook.winners, [1]);
  assert.equal(items.winners_curse.status, 'held', 'the table says public, the file says private: held');
  assert.deepEqual(items.winners_curse.winners, []);
  assert.equal(items.tilt.status, 'won', 'no basis in the file: the table (public)');
  assert.equal(items.bargain.status, 'held', 'no basis in the file: the table (private)');
  assert.deepEqual(off.items.filter((item) => !item.known).map((item) => [item.status, item.basis]), [['won', 'public'], ['held', 'private']]);
  assert.deepEqual(off.bots.map((item) => [item.key, item.status, item.basis]), [['most_textbook', 'won', 'public'], ['tilt', 'held', 'private']]);
  assert.equal(off.held, 4);
  const on = awardsFor(model, { lens: true });
  assert.ok([...on.items, ...on.bots].every((item) => item.status === 'won'), 'with the lens everything is shown');
  assert.deepEqual(on.items.map((item) => item.basis), ['public', 'private', 'public', 'private', 'public', 'private']);
});

test('a basis that the page does not know counts as private, and a basis that is not a text says nothing', () => {
  const model = withAwards([
    { award: 'tilt', winners: [1], metric: 40, metric_unit: 'coins', basis: 'secret' },
    { award: 'bargain', winners: [4], metric: 9, metric_unit: 'coins', basis: 3 },
    { award: 'keeper', winners: [2], metric: 2, metric_unit: 'count', basis: null },
  ]);
  const items = byKey(awardsFor(model, { lens: false }).items);
  assert.equal(items.tilt.status, 'held', 'the table says public, but the file named some other basis');
  assert.equal(items.bargain.status, 'held', 'not a text: the table (private)');
  assert.equal(items.keeper.status, 'won', 'null: the table (public)');
});

test('awardBasis takes the basis of the replay first and the table only when there is none', () => {
  assert.equal(awardBasis('tilt'), 'public');
  assert.equal(awardBasis('tilt', null), 'public');
  assert.equal(awardBasis('tilt', 'private'), 'private');
  assert.equal(awardBasis('best_actor', 'public'), 'public');
  assert.equal(awardBasis('best_actor', 'private'), 'private');
  assert.equal(awardBasis('best_actor', undefined), 'private');
  assert.equal(awardBasis('a_future_award', 'public'), 'public');
  assert.equal(awardBasis('a_future_award', 'whatever'), 'private', 'only the two words are bases');
  assert.equal(awardBasis('a_future_award'), 'private');
});

test('the real night: what the exporter says each award rests on is what holds it back', () => {
  const shown = awardsFor(mock, { lens: false });
  const entries = [...mock.awards, ...mock.botsReference];
  assert.ok(entries.every((entry) => entry.basis === 'public' || entry.basis === 'private'));
  const heldKeys = [...shown.items, ...shown.bots].filter((item) => item.status === 'held').map((item) => item.key);
  assert.deepEqual(heldKeys, entries.filter((entry) => entry.basis === 'private').map((entry) => entry.award));
  assert.equal(shown.held, heldKeys.length);
  assert.deepEqual(heldKeys, ['most_textbook', 'bargain', 'best_actor', 'mind_reader', 'consistent', 'most_textbook', 'bargain']);
  const text = JSON.stringify(shown);
  assert.ok(!text.includes('平均误差') && !text.includes('估值比成交价高'), 'no private figure in the result');
});

// ---------------------------------------------------------------- the judges and best actor

test('an award for false claims that is vacant while a judge was absent does not blame the limit', () => {
  const items = byKey(awardsFor(clocks, { lens: true }).items);
  assert.equal(items.best_actor.status, 'vacant');
  assert.equal(items.best_actor.vacantText, STR.awards.vacantNoJudges);
  assert.ok(!items.best_actor.vacantText.includes('下限'));
  assert.equal(items.most_textbook.vacantText, '没有座位达到下限（至少 6 次有效出价）。', 'the other awards are not touched');
  const raw = fixture(CLOCKS);
  delete raw.judges_absent;
  assert.equal(byKey(awardsFor(indexReplay(raw), { lens: true }).items).best_actor.vacantText, '没有座位达到下限（至少 1 条）。');
  assert.equal(byKey(awardsFor(sealed, { lens: true }).items).best_actor.status, 'won', 'a winner is a winner');
});

// ----------------------------------------------------------------------- what the page keeps

test('the page shows the replay as it is: it recomputes nothing and adds nothing up', () => {
  const model = withAwards([
    { award: 'winners_curse', winners: [3], metric: 99, metric_unit: 'coins' },
    { award: 'tilt', winners: [2, 4], metric: 41, metric_unit: 'coins' },
  ]);
  const items = byKey(awardsFor(model, { lens: true }).items);
  assert.equal(items.winners_curse.evidence, '成交价比真值高 99 金币', 'the figure of the file, not the one of the lot');
  assert.deepEqual(items.tilt.winners, [2, 4]);
  assert.equal(items.tilt.status, 'tied');
  const text = JSON.stringify(awardsFor(model, { lens: true }));
  assert.ok(!/合计|总计|总分|比率|效果/.test(text));
});

test('a file in any order gives the table order, and an award named twice counts once', () => {
  const shuffled = withAwards([
    { award: 'consistent', vacant: true },
    { award: 'tilt', winners: [1], metric: 35, metric_unit: 'coins' },
    { award: 'winners_curse', winners: [3], metric: 14, metric_unit: 'coins' },
    { award: 'tilt', winners: [2], metric: 99, metric_unit: 'coins' },
  ]);
  const shown = awardsFor(shuffled, { lens: true });
  assert.deepEqual(shown.items.map((item) => item.key), ['winners_curse', 'tilt', 'consistent']);
  assert.deepEqual(shown.items[1].winners, [1], 'the first entry stands');
});

test('an award that this viewer does not know is listed last, in neutral words, and held while the lens is off', () => {
  const model = withAwards([
    { award: 'tilt', winners: [1], metric: 35, metric_unit: 'coins' },
    { award: 'a_future_award', winners: [2], metric: 7, metric_unit: 'count' },
  ]);
  const on = awardsFor(model, { lens: true }).items;
  assert.deepEqual(on.map((item) => item.key), ['tilt', 'unknown']);
  assert.equal(on[1].name, STR.awards.unknownName);
  assert.equal(on[1].rule, STR.awards.unknownRule);
  assert.equal(on[1].evidence, '回放记录的数字：7');
  assert.equal(on[1].known, false);
  assert.equal(awardsFor(model, { lens: false }).items[1].status, 'held');
});

test('an award with winners is never vacant, and an award without them always is', () => {
  const model = withAwards([
    { award: 'tilt', winners: [], vacant: false },
    { award: 'bargain', winners: [3], vacant: true, metric: 3, metric_unit: 'coins' },
    { award: 'keeper', winners: [9, 0, 'x'], metric: 2, metric_unit: 'count' },
  ]);
  const items = byKey(awardsFor(model, { lens: true }).items);
  assert.equal(items.tilt.status, 'vacant');
  assert.equal(items.bargain.status, 'won');
  assert.equal(items.keeper.status, 'vacant', 'seats that do not exist are not winners');
});

// ---------------------------------------------------------------------------- the page

function mount(model, { lens = true } = {}) {
  const doc = new FakeDocument();
  const parent = doc.createElement('div');
  const view = mountAwards(doc, parent, model, { lens });
  return { doc, parent, view, root: view.root };
}
const all = (el, selector) => el.querySelectorAll(selector);
const one = (el, selector) => el.querySelector(selector);
const text = (el) => el.textContent;

test('the page is a section with a title, the rules in a few sentences, ten cards and the reference of the bots', () => {
  const { root } = mount(sealed);
  assert.equal(root.localName, 'section');
  assert.equal(text(one(root, '.page-title')), '颁奖');
  const notes = all(root, '.page-head .page-note').map(text);
  assert.ok(notes[0].includes('只有模型座位参评') && notes[0].includes('并列获奖') && notes[0].includes('本场空缺') && notes[0].includes('机器人参考'));
  assert.ok(notes[1].includes('不重新计算'));
  assert.deepEqual(all(root, '.award').map((card) => card.getAttribute('data-award')), [...AWARD_KEYS]);
  assert.equal(all(root, '.awards-bots').length, 1);
  assert.equal(text(one(root, '.awards-bots .section-title')), '机器人参考');
});

test('a card has the name, the state, the winners with their figure, and the rule with its floor', () => {
  const { root } = mount(sealed);
  const curse = one(root, '.award[data-award="winners_curse"]');
  assert.equal(text(one(curse, '.award-name')), '赢家诅咒王');
  assert.equal(text(one(curse, '.award-state')), '获奖');
  assert.ok(curse.classList.contains('is-won'));
  const winner = one(curse, '.winner');
  assert.equal(winner.getAttribute('data-seat'), '3');
  assert.equal(text(one(winner, '.seat-no')), '3');
  assert.equal(text(one(winner, '.winner-name')), '小满');
  assert.ok(text(one(winner, '.winner-info')).includes('sample-mid-3-1003'), 'the model of the winner, in small print');
  assert.equal(text(one(curse, '.award-basis')), '依据：成交价比真值高 14 金币');
  assert.deepEqual(all(curse, '.award-rule dt').map(text), ['判定', '下限']);
  assert.equal(text(all(curse, '.award-rule dd')[0]), STR.awards.items.winners_curse.rule);
  assert.equal(text(all(curse, '.award-rule dd')[1]), '成交价至少比真值高出这个盲盒档位的 5%');
});

test('a tie lists every winner under one card that says so', () => {
  const { root } = mount(sealed);
  const card = one(root, '.award[data-award="backstabber"]');
  assert.equal(text(one(card, '.award-state')), '并列获奖');
  assert.ok(card.classList.contains('is-tied'));
  assert.deepEqual(all(card, '.winner').map((el) => el.getAttribute('data-seat')), ['1', '4']);
  assert.deepEqual(all(card, '.winner-name').map(text), ['鲸落', '拾光']);
  assert.equal(all(card, '.award-basis').length, 1, 'one figure for both');
});

test('a vacant card says so, with the floor that was not reached, and shows no winner', () => {
  const { root } = mount(sealed);
  const card = one(root, '.award[data-award="most_textbook"]');
  assert.equal(text(one(card, '.award-state')), '本场空缺');
  assert.ok(card.classList.contains('is-vacant'));
  assert.equal(text(one(card, '.award-vacant')), '本场空缺：没有座位达到下限（至少 6 次有效出价）。');
  assert.equal(all(card, '.winner').length, 0);
  assert.equal(all(card, '.award-basis').length, 0);
  const keeper = one(root, '.award[data-award="keeper"]');
  assert.equal(text(all(keeper, '.award-rule dd')[1]), STR.awards.noFloor, 'an award without a floor says that');
  assert.ok(text(one(keeper, '.award-vacant')).includes('立过至少 2 条誓约并且全部遵守'));
});

test('the reference of the bots lists each award with the bot that did best, or says that nobody did', () => {
  const { root } = mount(sealed);
  const rows = all(root, '.ref-item');
  assert.deepEqual(rows.map((row) => row.getAttribute('data-award')), ['most_textbook', 'winners_curse', 'tilt', 'bargain']);
  const tilt = rows[2];
  assert.equal(text(one(tilt, '.winner-name')), '老实人');
  assert.equal(text(one(tilt, '.winner-info')), 'truthful');
  assert.equal(text(one(tilt, '.award-basis')), '没拍到却付了 52 金币');
  assert.equal(text(one(rows[0], '.ref-state')), '本场空缺');
  assert.equal(all(rows[0], '.winner').length, 0);
  assert.equal(text(one(tilt, '.ref-state')), '获奖');
  assert.ok(text(one(root, '.awards-bots')).includes('机器人不参评'));
});

test('without the lens: the held cards say why, show no winner, and the public cards are as before', () => {
  const { root } = mount(sealed, { lens: false });
  const held = all(root, '.award.is-held');
  assert.deepEqual(held.map((card) => card.getAttribute('data-award')), PRIVATE);
  for (const card of held) {
    assert.equal(text(one(card, '.award-state')), '需要底牌镜头');
    assert.equal(text(one(card, '.award-held')), STR.awards.held);
    assert.equal(all(card, '.winner').length, 0);
    assert.equal(all(card, '.award-basis').length, 0);
    assert.equal(all(card, '.award-rule dd').length, 2, 'the rule is still readable');
  }
  assert.equal(all(root, '.award.is-won').length, 3);
  assert.equal(all(root, '.award.is-tied').length, 1);
  assert.ok(!text(root).includes('估值比成交价高'));
  assert.equal(all(root, '.ref-item.is-held').length, 2);
  assert.ok(!text(root).includes('阿铁'), 'the winner of a private award is not named');
});

test('a night without awards says so, and so does a night without a reference', () => {
  const none = withAwards([], []);
  const { root } = mount(none);
  assert.equal(text(one(root, '.empty')), STR.awards.none);
  assert.equal(all(root, '.award').length, 0);
  assert.equal(text(all(root, '.empty')[1]), STR.awards.botsNone);
  const raw = fixture(CLOCKS);
  delete raw.awards;
  delete raw.bots_reference;
  const old = mount(indexReplay(raw)).root;
  assert.equal(all(old, '.award').length, 0, 'a replay from before the awards still opens');
});

test('the findings sit on the page between the cards and the reference of the bots', () => {
  const { root } = mount(sealed);
  const parts = root.children.map((child) => child.className.split(' ')[0]);
  assert.deepEqual(parts, ['page-head', 'award-grid', 'verdicts', 'awards-bots']);
  assert.equal(text(one(root, '.verdicts-title')), STR.verdicts.title);
  assert.equal(all(root, '.award').length, 10, 'the cards are the same');
  const absent = mount(clocks).root;
  assert.ok(text(one(absent, '.verdict-absent')).includes('评委 1（vendor-e）缺席：超时'));
});

test('model ids are in the small print of a winner and nowhere else on the page', () => {
  const { root } = mount(sealed);
  const ids = ['sample-large-1', 'sample-small-2', 'sample-mid-3', 'sample-mid-4'];
  for (const el of all(root, '*')) {
    const own = el.childNodes.filter((n) => n.nodeType === 3).map((n) => n.data).join('');
    for (const id of ids) {
      if (own.includes(id)) assert.ok(el.classList.contains('winner-info'), `${id} in ${el.className}`);
    }
  }
});

test('the cards never add figures up or divide them, and the banned words are nowhere on the page', () => {
  for (const model of [sealed, clocks]) {
    for (const lens of [true, false]) {
      const { root } = mount(model, { lens });
      // The page note says that nothing is added up, so only the cards and the reference are read.
      const cards = [...all(root, '.award'), ...all(root, '.ref-item')].map(text).join('\n');
      assert.ok(!/合计|总计|总分|比率|效果/.test(cards));
      assert.ok(!/效率|福利|PoA|welfare|efficiency/i.test(text(root)));
      assert.equal(all(root, 'tfoot').length, 0);
    }
  }
  assert.ok(!/效率|福利|PoA|welfare|efficiency/i.test(JSON.stringify(STR.awards)));
});

// ------------------------------------------------------------------------- hostile text

const ALLOWED_TAGS = new Set(['section', 'header', 'div', 'p', 'h2', 'h3', 'article', 'span', 'strong', 'small', 'ul', 'li', 'dl', 'dt', 'dd']);
const ALLOWED_ATTRIBUTES = new Set(['class', 'data-award', 'data-seat', 'aria-label']);

for (const [name, payload] of Object.entries(PAYLOADS)) {
  test(`hostile text "${name}" in a name, a model id or a persona stays text on the awards page`, () => {
    for (const lens of [true, false]) {
      const { root } = mount(indexReplay(poison(fixture(SEALED), payload)), { lens });
      for (const tag of tagNames(root)) assert.ok(ALLOWED_TAGS.has(tag), `<${tag}>`);
      for (const { name: attr, value } of attributeValues(root)) {
        assert.ok(ALLOWED_ATTRIBUTES.has(attr), attr);
        assert.ok(!/[<>"]/.test(value) && !/javascript:/i.test(value), `${attr}=${value}`);
      }
      const markup = serialize(root);
      for (const needle of ['<img', '<script', '<svg', '<iframe', '<a ', '<b ', '<style']) assert.ok(!markup.includes(needle), needle);
    }
  });
}

test('text that the page draws on the awards page holds no control or invisible character', () => {
  for (const key of ['ansi', 'osc', 'bidi', 'invisible']) {
    const model = indexReplay(poison(fixture(SEALED), PAYLOADS[key]));
    const { root } = mount(model);
    assert.ok(!hasHidden(text(root)), key);
    for (const string of strings(awardsFor(model, { lens: true }))) assert.ok(!hasHidden(string), key);
  }
});
