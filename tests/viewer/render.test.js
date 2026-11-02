import assert from 'node:assert/strict';
import test from 'node:test';

import { frameAt, TIMING } from '../../viewer/anim/envelopes.js';
import { lensFor } from '../../viewer/lib/lens.js';
import { indexReplay } from '../../viewer/lib/replay.js';
import { formatHash } from '../../viewer/lib/route.js';
import { BEATS, buildScript } from '../../viewer/lib/script.js';
import { STR } from '../../viewer/lib/strings.js';
import { mountLotList } from '../../viewer/ui/lotlist.js';
import { createStage } from '../../viewer/ui/stage.js';
import { attributeValues, FakeDocument, serialize, tagNames, textNodes } from './fake_dom.js';
import { CLOCKS, fixture, modelOf, PAYLOADS, poison, SEALED } from './helpers.js';

function build(model, options) {
  const doc = new FakeDocument();
  const root = doc.createElement('main');
  const stage = createStage(doc, root, model, options);
  return { doc, root, stage, model };
}

const sealed = modelOf(SEALED);
const ids = ['sample-large-1', 'sample-small-2', 'sample-mid-3', 'sample-mid-4'];
const all = (root, selector) => root.querySelectorAll(selector);
const one = (root, selector) => root.querySelector(selector);
const text = (el) => el.textContent;

function lotEnd(stage, lotNo) {
  const end = stage.durationOf(lotNo);
  stage.update(end);
  return end;
}

// ------------------------------------------------------------------------------ the page

test('the stage shows the lot card, the bids, the overlay, the seats, the chat and the host', () => {
  const { root, stage } = build(sealed);
  stage.render(2, true);
  assert.deepEqual(
    [...root.children].map((c) => c.className.split(' ')[0]),
    ['lot-card', 'lens-note', 'bids', 'overlay', 'seats', 'chat-box', 'host'],
  );
  const card = one(root, '.lot-card');
  for (const part of ['永远指向昨天的指南针', '机械', '档位 200', '密封二价', '第 2 / 4 件', '🧭']) assert.ok(text(card).includes(part), part);
});

test('a lot that was not played says so', () => {
  const raw = fixture(SEALED);
  raw.timeline = raw.timeline.slice(0, 1);
  const { root, stage } = build(indexReplay(raw));
  stage.render(3, true);
  assert.equal(text(root), STR.stage.noLot);
  assert.equal(stage.update(5), null);
  assert.equal(stage.durationOf(3), 0);
});

// ------------------------------------------------------------------- code names, model ids

test('chat bubbles carry code names, and model ids appear only in the info row', () => {
  const { root, stage } = build(sealed);
  for (const lotNo of sealed.lotNos) {
    for (const lens of [true, false]) {
      stage.render(lotNo, lens);
      lotEnd(stage, lotNo);
      const bubbles = all(root, '.bubble');
      assert.equal(bubbles.length, sealed.lot(lotNo).talk.length);
      bubbles.forEach((bubble, i) => {
        const seat = sealed.lot(lotNo).talk[i].seat;
        assert.equal(text(one(bubble, '.bubble-name')), sealed.codename(seat));
        assert.equal(text(one(bubble, '.seat-no')), String(seat));
      });
      for (const node of textNodes(root)) {
        for (const id of ids) {
          if (!node.data.includes(id)) continue;
          let el = node.parentNode;
          let inInfo = false;
          while (el) {
            if (el.classList?.contains('seat-info')) inInfo = true;
            el = el.parentNode;
          }
          assert.ok(inInfo, `model id ${id} outside an info row in lot ${lotNo}: ${node.data}`);
        }
      }
    }
  }
});

test('every seat has an info row with its model, or its policy for a bot', () => {
  const { root, stage } = build(sealed);
  stage.render(1, true);
  const infos = all(root, '.seat-info').map(text);
  assert.equal(infos.length, 6);
  assert.ok(infos[0].includes('sample-large-1-0926') && infos[0].includes('sample-large-1'), 'returned id, and the configured one when it differs');
  assert.ok(infos[0].includes('adaptive/high'));
  assert.ok(infos[1].includes('sample-small-2'));
  assert.ok(!infos[1].includes('配置'), 'no second id when both are the same');
  assert.equal(infos[4], 'truthful');
  assert.equal(infos[5], 'economist');
});

// ---------------------------------------------------------------------------- the lens

test('the lens note says that it shows what the players did not see', () => {
  const { root, stage } = build(sealed);
  stage.render(1, true);
  assert.equal(text(one(root, '.lens-note')), STR.lens.on);
  assert.ok(STR.lens.on.includes('选手当时没看到'));
  assert.ok(one(root, '.lens-note').classList.contains('is-on'));
  stage.render(1, false);
  assert.equal(text(one(root, '.lens-note')), STR.lens.off);
  assert.ok(STR.lens.off.includes('选手当时看得到的公开信息'));
  assert.ok(one(root, '.lens-note').classList.contains('is-off'));
});

test('lens on: every seat has a thought bubble with value, budget, mission and plan', () => {
  const { root, stage } = build(sealed);
  stage.render(1, true);
  const holes = all(root, '.thought.hole');
  assert.equal(holes.length, 6);
  const first = text(holes[0]);
  for (const part of ['估值 37', '预算 342，此时还剩 342', '任务（收藏家）：拍到 1 件「海洋」标签的拍品', '计划 ☆☆☆，最多花 0', '打算表现成：低调的海洋爱好者', '开场独白：海洋货在第四件']) {
    assert.ok(first.includes(part), part);
  }
  const bot = text(holes[4]);
  assert.ok(bot.includes('估值 62') && bot.includes('预算 220'));
  assert.ok(!bot.includes('任务') && !bot.includes('计划'), 'a bot has neither');
});

test('lens on: a blind box shows the appraisal', () => {
  const { root, stage } = build(sealed);
  stage.render(4, true);
  assert.ok(text(all(root, '.thought.hole')[2]).includes('鉴定价 204'));
});

test('lens on: monologues hang under the lines and the bids they belong to', () => {
  const { root, stage } = build(sealed);
  stage.render(1, true);
  assert.equal(all(root, '.bubble .thought.mono').length, 4, 'four players spoke with a monologue');
  assert.equal(all(root, '.bubble .thought.guess').length, 4, 'and four mind-reading guesses');
  assert.equal(all(root, '.env .thought.mono').length, 3, 'three bids with a monologue; the abstaining seat has none');
  assert.ok(text(all(root, '.bubble .thought.guess')[0]).includes('读心：猜 2 号「阿铁」还剩 180，实际 260'));
});

test('lens off: no thought bubble, no private number, no monologue, no host line anywhere', () => {
  const { root, stage } = build(sealed);
  for (const lotNo of sealed.lotNos) {
    stage.render(lotNo, false);
    lotEnd(stage, lotNo);
    assert.equal(all(root, '.thought').length, 0);
    assert.equal(all(root, '.host').length, 0);
    const shown = text(root);
    for (const thought of sealed.reveal.thoughts) assert.ok(!shown.includes(thought.text), thought.text);
    for (const plan of sealed.reveal.plans) assert.ok(!shown.includes(plan.persona), plan.persona);
    for (const word of ['独白', '读心', '此时还剩', '最多花', '想出', '弃权', '超时']) assert.ok(!shown.includes(word), word);
    for (const seat of sealed.seatNos) {
      const card = lensFor(sealed, seat, lotNo);
      const private_ = [
        card.value === null ? null : card.valueKind === 'appraisal' ? STR.lens.appraisal(card.value) : STR.lens.value(card.value),
        card.budget === null ? null : STR.lens.budget(card.budget, card.remaining),
        card.mission?.text,
        card.plan ? STR.lens.plan(card.plan.stars, card.plan.budget) : null,
        card.plan?.persona ? STR.lens.persona(card.plan.persona) : null,
      ].filter(Boolean);
      const spoken = sealed.lot(lotNo).talk.map((t) => t.text).join('\n');
      for (const line of private_) {
        if (spoken.includes(line)) continue; // the seat said it out loud, so it is public
        assert.ok(!shown.includes(line), `lot ${lotNo} seat ${seat}: ${line}`);
      }
    }
    for (const lot of sealed.lots) if (lot.host) assert.ok(!shown.includes(lot.host));
  }
});

test('lens on: the host line appears at its beat', () => {
  const { root, stage } = build(sealed);
  stage.render(2, true);
  const script = buildScript(sealed.lot(2), 0);
  stage.update(0);
  assert.ok(one(root, '.host').classList.contains('is-pending'));
  lotEnd(stage, 2);
  assert.ok(!one(root, '.host').classList.contains('is-pending'));
  assert.ok(text(one(root, '.host-line')).includes('165'));
  assert.ok(script.duration > 0);
});

// --------------------------------------------------------------------------- the clock

test('at the start of a lot no line is shown and every envelope is closed and empty', () => {
  const { root, stage } = build(sealed);
  stage.render(2, true);
  stage.update(0);
  assert.ok(all(root, '.bubble').every((b) => b.classList.contains('is-pending')));
  assert.ok(all(root, '.env').every((e) => e.classList.contains('is-sealed')));
  assert.ok(all(root, '.env-bid').every((e) => text(e) === ''), 'closed envelopes hold no number');
  assert.ok(all(root, '.env .thought').every((e) => e.classList.contains('is-pending')), 'nor a monologue');
  assert.equal(all(root, '.verdict-body').length, 0);
});

test('lines appear as the script reaches them, and the speaker is marked', () => {
  const { root, stage } = build(sealed);
  stage.render(1, true);
  const lot = sealed.lot(1);
  const script = buildScript(lot, 20);
  const talks = script.beats.filter((b) => b.kind === 'talk');
  talks.forEach((beat, i) => {
    stage.update(beat.start + 0.01);
    const visible = all(root, '.bubble').filter((b) => !b.classList.contains('is-pending'));
    assert.equal(visible.length, i + 1);
    assert.ok(visible.at(-1).classList.contains('is-speaking'));
    const card = one(root, `.seat[data-seat="${beat.seat}"]`);
    assert.ok(card.classList.contains('is-speaking'));
    assert.equal(all(root, '.seat.is-speaking').length, 1);
    assert.equal(text(one(card, '.seat-state')), STR.stage.spoke);
    const mono = one(visible.at(-1), '.thought.mono');
    if (mono) assert.ok(mono.classList.contains('is-pending'), 'the monologue follows a moment later');
    stage.update(beat.start + BEATS.thoughtDelay + 0.01);
    if (mono) assert.ok(!mono.classList.contains('is-pending'));
  });
});

test('the envelopes sit in seat order and open in bid order', () => {
  const { root, stage } = build(sealed);
  stage.render(1, true);
  assert.deepEqual(all(root, '.env').map((e) => e.getAttribute('data-seat')), ['1', '2', '3', '4', '5', '6']);
  const script = buildScript(sealed.lot(1), frameAt(sealed.lot(1), 0).duration);
  const opened = [];
  for (let t = script.bidStart; t < script.duration; t += 0.05) {
    stage.update(t);
    for (const env of all(root, '.env')) {
      const seat = Number(env.getAttribute('data-seat'));
      if (!env.classList.contains('is-sealed') && !opened.includes(seat)) opened.push(seat);
    }
  }
  assert.deepEqual(opened, [3, 1, 6, 5, 2, 4], 'by bids 0, 30, 40, 62, 66, 74');
});

test('at the end the winner is marked, the bids are shown and the hammer text is in place', () => {
  const { root, stage } = build(sealed);
  stage.render(1, true);
  lotEnd(stage, 1);
  assert.deepEqual(all(root, '.env.is-winner').map((e) => e.getAttribute('data-seat')), ['4']);
  assert.deepEqual(all(root, '.seat.is-winner').map((e) => e.getAttribute('data-seat')), ['4']);
  assert.equal(text(one(all(root, '.seat.is-winner')[0], '.seat-state')), STR.stage.won);
  assert.equal(text(one(root, '.verdict-main')), '🔨 4 号「拾光」拍得，成交价 74');
  const shown = Object.fromEntries(all(root, '.env').map((e) => [e.getAttribute('data-seat'), text(one(e, '.env-bid'))]));
  assert.deepEqual(shown, { 1: '30', 2: '66', 3: '弃权', 4: '74', 5: '62', 6: '40' });
  assert.equal(text(one(one(root, '.env[data-seat="3"]'), '.env-note')), '超时');
  assert.equal(text(one(one(root, '.env[data-seat="4"]'), '.env-pay')), '付 74');
  assert.ok(all(root, '.bubble').every((b) => !b.classList.contains('is-pending')));
});

test('all-pay shows every seat that paid, with its own bid', () => {
  const { root, stage } = build(sealed);
  stage.render(3, true);
  lotEnd(stage, 3);
  const pays = Object.fromEntries(all(root, '.env').map((e) => [e.getAttribute('data-seat'), text(one(e, '.env-pay'))]));
  assert.deepEqual(pays, { 1: '付 35', 2: '付 20', 3: '付 55', 4: '', 5: '付 52', 6: '付 3' });
  const notes = all(root, '.verdict-note').map(text);
  assert.ok(notes.includes(STR.bids.allPay(5)));
  assert.equal(text(one(root, '.verdict-main')), '🔨 3 号「小满」拍得，成交价 55');
});

test('a clamped bid says what the seat wanted and what it could afford, under the lens', () => {
  const { root, stage } = build(sealed);
  stage.render(4, true);
  lotEnd(stage, 4);
  assert.equal(text(one(one(root, '.env[data-seat="2"]'), '.env-note')), STR.bids.clamped(140, 82));
  assert.ok(one(root, '.env[data-seat="2"]').classList.contains('is-clamped'));
});

test('without the lens an abstention and a clamp look like the plain bids that everyone saw', () => {
  const { root, stage } = build(sealed);
  stage.render(1, false);
  lotEnd(stage, 1);
  const abstainer = one(root, '.env[data-seat="3"]');
  assert.equal(text(one(abstainer, '.env-bid')), '0');
  assert.equal(text(one(abstainer, '.env-note')), '');
  assert.ok(!abstainer.classList.contains('is-abstain'));
  stage.render(4, false);
  lotEnd(stage, 4);
  const clamped = one(root, '.env[data-seat="2"]');
  assert.equal(text(one(clamped, '.env-bid')), '82');
  assert.equal(text(one(clamped, '.env-note')), '');
  assert.ok(!clamped.classList.contains('is-clamped'));
});

test('the blind box opens only after the hammer', () => {
  const { root, stage } = build(sealed);
  stage.render(4, true);
  const lot = sealed.lot(4);
  const script = buildScript(lot, frameAt(lot, 0).duration);
  const { verdictAt, revealAt } = { verdictAt: script.bidStart + TIMING.intro + 6 * (TIMING.open + TIMING.gap), revealAt: script.bidStart + TIMING.intro + 6 * (TIMING.open + TIMING.gap) + TIMING.verdict };
  stage.update(verdictAt + 0.1);
  assert.equal(all(root, '.reveal').length, 0);
  assert.equal(all(root, '.verdict-main').length, 1);
  stage.update(revealAt + 0.1);
  assert.equal(all(root, '.reveal').length, 1);
  assert.equal(text(one(root, '.reveal-main')), '📦 开箱：真值是 176');
  assert.ok(text(one(root, '.reveal')).includes(STR.bids.over(14)));
});

test('reduced motion: an envelope is either shut or open, never half', () => {
  const { root, stage } = build(sealed, { reduceMotion: true });
  stage.render(1, true);
  const lot = sealed.lot(1);
  const script = buildScript(lot, frameAt(lot, 0).duration);
  const seen = new Set();
  for (let t = script.bidStart; t < script.duration; t += 0.05) {
    stage.update(t);
    for (const env of all(root, '.env')) seen.add(env.style.getPropertyValue('--open'));
  }
  assert.deepEqual([...seen].sort(), ['0', '1']);
});

test('without reduced motion the flap swings through the values in between', () => {
  const { root, stage } = build(sealed);
  stage.render(1, true);
  const script = buildScript(sealed.lot(1), frameAt(sealed.lot(1), 0).duration);
  stage.update(script.bidStart + TIMING.intro + TIMING.open / 2);
  const values = all(root, '.env').map((e) => e.style.getPropertyValue('--open'));
  assert.ok(values.includes('0.5'), values.join());
});

test('seeking back and forth gives the same picture as playing straight to that time', () => {
  const a = build(sealed);
  const b = build(sealed);
  a.stage.render(3, true);
  b.stage.render(3, true);
  const end = a.stage.durationOf(3);
  for (const t of [end, 1, end * 0.7, 0, end * 0.4]) a.stage.update(t);
  b.stage.update(end * 0.4);
  assert.equal(serialize(a.root, { canonical: true }), serialize(b.root, { canonical: true }));
});

// ------------------------------------------------------------------- English and Dutch

test('the English lot is a price bar: the log lists who left, and who did not take part', () => {
  const clocks = modelOf(CLOCKS);
  const { root, stage } = build(clocks);
  stage.render(1, true);
  lotEnd(stage, 1);
  assert.deepEqual(all(root, '.eng-log-row').map(text), [
    '4 号「老实人」没有参加',
    '1 号「青柠」在 95 退出，场上还剩 3 人',
    '2 号「麦芽」在 140 退出，场上还剩 2 人',
    '5 号「经济学家」在 165 退出，场上还剩 1 人',
  ]);
  assert.equal(text(one(root, '.verdict-main')), '🔨 3 号「小满」拍得，成交价 165');
  assert.equal(all(root, '.sum-mono li').length, 5, 'five bid-phase monologues in the lens');
});

test('the Dutch lot is a clock of ten cells that stops where the lot was bought, and marks the seat that sat out', () => {
  const clocks = modelOf(CLOCKS);
  const { root, stage } = build(clocks);
  stage.render(2, true);
  lotEnd(stage, 2);
  assert.deepEqual(all(root, '.dut-price').map(text), ['400', '360', '320', '280', '240', '200', '160', '120', '80', '40']);
  assert.deepEqual(all(root, '.dut-cell').map((c) => ['is-lit', 'is-stop', 'is-dim'].find((k) => c.classList.contains(k))), [
    'is-lit', 'is-lit', 'is-lit', 'is-stop', 'is-dim', 'is-dim', 'is-dim', 'is-dim', 'is-dim', 'is-dim',
  ]);
  assert.equal(text(one(root, '.verdict-main')), '🔨 2 号「麦芽」拍得，成交价 280');
  const out = all(root, '.seat.is-out');
  assert.equal(out.length, 1);
  assert.equal(text(one(out[0], '.seat-state')), STR.stage.sittingOut);
});

test('a lot nobody could see through the lens: English with the lens off has no monologues', () => {
  const clocks = modelOf(CLOCKS);
  const { root, stage } = build(clocks);
  stage.render(1, false);
  lotEnd(stage, 1);
  assert.equal(all(root, '.sum-mono').length, 0);
  assert.equal(all(root, '.thought').length, 0);
});

// ------------------------------------------------------------------------- the lot list

test('the lot list holds one link per played lot, and marks the current and the seen', () => {
  const doc = new FakeDocument();
  const list = doc.createElement('ol');
  const view = mountLotList(doc, list, sealed);
  const links = all(list, 'a.lot-link');
  assert.equal(links.length, 4);
  links.forEach((a, i) => {
    assert.equal(a.getAttribute('href'), formatHash('fixture-sealed-0001', i + 1));
    assert.equal(a.getAttribute('data-lot'), String(i + 1));
  });
  view.update(2, new Set([1, 2]));
  const items = all(list, '.lot-item');
  assert.deepEqual(items.map((i) => i.classList.contains('is-current')), [false, true, false, false]);
  assert.deepEqual(items.map((i) => i.classList.contains('is-visited')), [true, true, false, false]);
  assert.equal(links[1].getAttribute('aria-current'), 'step');
  assert.equal(links[0].hasAttribute('aria-current'), false);
  assert.ok(text(items[0]).includes('只在雨天变甜的蜂蜜') && text(items[0]).includes('甜品 · 档位 100 · 密封一价'));
});

test('lots that were not played are listed but cannot be opened', () => {
  const raw = fixture(SEALED);
  raw.timeline = raw.timeline.slice(0, 2);
  const doc = new FakeDocument();
  const list = doc.createElement('ol');
  mountLotList(doc, list, indexReplay(raw));
  assert.equal(all(list, 'a.lot-link').length, 2);
  assert.equal(all(list, '.lot-link.is-unplayed').length, 2);
  assert.ok(text(all(list, '.lot-item')[3]).includes(STR.lotList.unplayed));
});

// ------------------------------------------------------------------- hostile replays

const ALLOWED_TAGS = new Set([
  'main', 'header', 'div', 'p', 'h2', 'h3', 'section', 'article', 'span', 'strong', 'ol', 'ul', 'li',
  'button', 'table', 'caption', 'thead', 'tbody', 'tr', 'th', 'td', 'dl', 'dt', 'dd',
]);
const ALLOWED_ATTRIBUTES = new Set([
  'class', 'data-seat', 'data-phase', 'data-term', 'role', 'aria-label', 'aria-live', 'aria-hidden',
  'aria-controls', 'aria-expanded', 'type', 'id', 'hidden', 'tabindex', 'scope', 'title',
]);
// The only hover hints on a lot are the meanings of the glossary terms, which are constants.
const HINTS = new Set(STR.glossary.map((item) => item.text));

function assertInert(root, label) {
  for (const tag of tagNames(root)) assert.ok(ALLOWED_TAGS.has(tag), `${label}: unexpected element <${tag}>`);
  for (const { el, name, value } of attributeValues(root)) {
    assert.ok(ALLOWED_ATTRIBUTES.has(name), `${label}: attribute ${name} on <${el.localName}>`);
    assert.ok(!/[<>"]/.test(value) && !/javascript:/i.test(value), `${label}: attribute ${name}=${value}`);
    if (name === 'class') assert.match(value, /^[\w -]+$/, label);
    if (name === 'data-seat') assert.match(value, /^[1-8]$/, label);
    if (name === 'data-term') assert.match(value, /^[a-z-]+$/, label);
    if (name === 'type') assert.equal(value, 'button', label);
    if (name === 'id') assert.equal(value, 'glossary', label);
    if (name === 'scope') assert.match(value, /^(?:col|row)$/, label);
    if (name === 'title') assert.ok(HINTS.has(value), `${label}: a hover hint that is not a glossary text: ${value}`);
    if (name === 'tabindex') assert.equal(value, '-1', label);
  }
  const markup = serialize(root);
  for (const needle of ['<img', '<script', '<svg', '<iframe', '<a ', '<b ', '<style']) assert.ok(!markup.includes(needle), `${label}: ${needle} in the markup`);
}

for (const [name, payload] of Object.entries(PAYLOADS)) {
  test(`hostile text "${name}" in every field stays text in every lot, with the lens on or off`, () => {
    for (const night of [SEALED, CLOCKS]) {
      const model = indexReplay(poison(fixture(night), payload));
      const { root, stage } = build(model);
      for (const lotNo of model.lotNos) {
        for (const lens of [true, false]) {
          stage.render(lotNo, lens);
          const end = stage.durationOf(lotNo);
          for (const t of [0, end * 0.3, end * 0.6, end]) {
            stage.update(t);
            assertInert(root, `${name}/${night}/lot ${lotNo}/lens ${lens}/t ${t.toFixed(1)}`);
          }
        }
      }
    }
  });
}

test('the lot list built from a hostile replay holds only safe links', () => {
  for (const payload of Object.values(PAYLOADS)) {
    const model = indexReplay(poison(fixture(SEALED), payload));
    const doc = new FakeDocument();
    const list = doc.createElement('ol');
    mountLotList(doc, list, model);
    for (const a of all(list, 'a')) assert.match(a.getAttribute('href'), /^#\/n\/[\w%.~-]+\/lot\/\d+$/);
    for (const { name } of attributeValues(list)) assert.ok(['class', 'href', 'data-lot', 'aria-hidden', 'aria-current'].includes(name), name);
    assert.deepEqual([...tagNames(list)].sort(), ['a', 'li', 'ol', 'small', 'span', 'strong']);
  }
});

test('the hostile chat lines of the fixture night show up as plain text', () => {
  const { root, stage } = build(sealed);
  const expected = new Map([
    [1, '<img src=x onerror=alert(1)> 谁抢蜂蜜我跟谁急！'],
    [2, '<script>alert("xss")</script> 指南针归我！'],
    [3, '[link=https://evil.example/claim]点我领取免费拍品[/link]'],
    [3, '[点我领奖](javascript:alert(1)) javascript:void(0)'],
    [4, '[31m红色警报[0m 我的鉴定价很高，这个箱子是我的。'],
  ]);
  for (const lotNo of sealed.lotNos) {
    stage.render(lotNo, true);
    lotEnd(stage, lotNo);
    assertInert(root, `lot ${lotNo}`);
    const lines = all(root, '.bubble-text').map(text);
    for (const [lot, line] of expected) if (lot === lotNo) assert.ok(lines.includes(line), `lot ${lot}: ${line}`);
    assert.equal(all(root, 'a').length, 0, 'no link is made from text');
  }
});

test('a very long line is cut with an ellipsis in the page', () => {
  const { root, stage } = build(sealed);
  stage.render(4, true);
  lotEnd(stage, 4);
  const lengths = all(root, '.bubble-text').map((el) => [...text(el)].length);
  assert.equal(Math.max(...lengths), 240);
  assert.ok(text(all(root, '.bubble-text')[0]).endsWith('\u2026'));
});
