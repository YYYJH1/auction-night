import assert from 'node:assert/strict';
import test from 'node:test';

import { actualRevenue, overlayFor } from '../../viewer/lib/overlay.js';
import { duration as sealedDuration, frameAt as sealedFrame, layout as sealedLayout } from '../../viewer/anim/envelopes.js';
import { STR } from '../../viewer/lib/strings.js';
import { mountOverlay } from '../../viewer/ui/overlay.js';
import { FakeDocument, textNodes, walk } from './fake_dom.js';
import { CLOCKS, fixture, indexModel, modelOf, SEALED } from './helpers.js';

const sealed = modelOf(SEALED);
const clocks = modelOf(CLOCKS);
const MINUS = String.fromCharCode(0x2212);

const deepFreeze = (x) => {
  if (x && typeof x === 'object') for (const v of Object.values(x)) deepFreeze(v);
  return Object.freeze(x);
};

/** An indexed lot with a hand-made overlay, for the cases that the fixtures do not hold. */
function lotWith(overlay, { format = 'sealed_second', hammer = {} } = {}) {
  return {
    lot: 1,
    format,
    overlay: {
      ref_bids: {},
      alpha_milli: {},
      regret: {},
      to_highest_value: null,
      excess: {},
      expected_revenue_milli: null,
      curse_gap: null,
      ...overlay,
    },
    hammer: { winner: 2, price: 50, payments: { 2: 50 }, unsold: false, ...hammer },
  };
}

// ----------------------------------------------------------------------- what may be shown

test('with the lens on, a lot shows its per-seat numbers, the flag and the public lines', () => {
  const shown = overlayFor(sealed.lot(1), { lens: true });
  assert.equal(shown.lens, true);
  assert.equal(shown.held, false);
  assert.deepEqual(shown.seats.map((r) => r.seat), [1, 2, 3, 4, 5, 6]);
  assert.deepEqual(shown.seats[3], { seat: 4, ref: 75, alpha_milli: null, regret: 7, excess: 1 });
  assert.deepEqual(shown.seats[5], { seat: 6, ref: 40, alpha_milli: 1000, regret: 0, excess: 0 });
  assert.equal(shown.highest, true);
  assert.deepEqual(shown.revenue, { expected_milli: 71295, actual: 74, all_pay: false });
  assert.equal(shown.curse_gap, null);
});

test('with the lens off, only what the table could see gets through, and the view is told that more is held back', () => {
  const shown = overlayFor(sealed.lot(1), { lens: false });
  assert.deepEqual(shown, {
    lens: false,
    held: true,
    seats: [],
    highest: null,
    revenue: { expected_milli: 71295, actual: 74, all_pay: false },
    curse_gap: null,
  });
  for (const lens of [undefined, null, 0, 'yes', 1]) {
    const odd = overlayFor(sealed.lot(1), { lens });
    assert.equal(odd.lens, false, `lens ${String(lens)} is not the lens`);
    assert.deepEqual(odd.seats, []);
    assert.equal(odd.highest, null);
  }
  assert.deepEqual(overlayFor(sealed.lot(1)).seats, [], 'no option at all means no lens');
});

test('no private figure of any lot of either night is in the result without the lens', () => {
  for (const model of [sealed, clocks]) {
    for (const lot of model.lots) {
      const shown = overlayFor(lot, { lens: false });
      if (shown === null) continue;
      assert.deepEqual(Object.keys(shown).sort(), ['curse_gap', 'held', 'highest', 'lens', 'revenue', 'seats']);
      assert.deepEqual(shown.seats, []);
      assert.equal(shown.highest, null);
      const text = JSON.stringify(shown);
      for (const table of [lot.overlay.ref_bids, lot.overlay.alpha_milli, lot.overlay.regret, lot.overlay.excess]) {
        for (const key of Object.keys(table)) assert.ok(!text.includes(`"seat":${key}`), `seat ${key}`);
      }
    }
  }
});

test('a blind box: the price minus the true value is public, the rest of its numbers are not', () => {
  const lot = sealed.lot(4);
  const open = overlayFor(lot, { lens: false });
  assert.equal(open.curse_gap, 14);
  assert.deepEqual(open.revenue, { expected_milli: 190980, actual: 190, all_pay: false });
  assert.deepEqual(open.seats, []);
  const lens = overlayFor(lot, { lens: true });
  assert.equal(lens.highest, null, 'the values of a blind box are common, so there is no highest');
  assert.equal(lens.seats.find((r) => r.seat === 3).excess, -14);
  assert.equal(lens.seats.find((r) => r.seat === 6).alpha_milli, 920);
});

test('all-pay: what the seller received is every payment of the lot, set beside the expected revenue', () => {
  const lot = sealed.lot(3);
  assert.equal(actualRevenue(lot), 35 + 20 + 55 + 52 + 3);
  assert.deepEqual(overlayFor(lot, { lens: false }).revenue, { expected_milli: 71287, actual: 165, all_pay: true });
});

test('a lot with a reserve price has no expected revenue, so the table shows nothing public for it', () => {
  const lot = clocks.lot(1);
  assert.equal(lot.overlay.expected_revenue_milli, null);
  const off = overlayFor(lot, { lens: false });
  assert.equal(off.revenue, null);
  assert.equal(off.held, true);
  assert.deepEqual(off.seats, []);
  const on = overlayFor(lot, { lens: true });
  assert.equal(on.highest, false);
  assert.equal(on.seats.find((r) => r.seat === 3).regret, 55);
});

test('excess and the curse gap keep their sign, and zero is a number, not a missing one', () => {
  const lot = lotWith({ excess: { 1: 71, 2: -94, 3: 0 }, regret: { 3: 0 } }, { format: 'all_pay' });
  const rows = overlayFor(lot, { lens: true }).seats;
  assert.deepEqual(rows.map((r) => [r.seat, r.excess]), [[1, 71], [2, -94], [3, 0]]);
  assert.equal(rows[2].regret, 0);
  assert.equal(rows[0].regret, null, 'a part that the replay lacks is null, never invented');
  const blind = lotWith({ curse_gap: -12 }, { format: 'blind_box' });
  assert.equal(overlayFor(blind, { lens: false }).curse_gap, -12);
});

test('the curse gap belongs to the blind box and is ignored on any other lot', () => {
  assert.equal(overlayFor(lotWith({ curse_gap: 5, excess: { 1: 0 } }, { format: 'sealed_first' }), { lens: true }).curse_gap, null);
});

test('a lot the replay says nothing about has no overlay at all', () => {
  assert.equal(overlayFor(lotWith({}), { lens: true }), null);
  assert.equal(overlayFor({ lot: 1, format: 'dutch', hammer: {} }, { lens: true }), null);
  assert.equal(overlayFor(null, { lens: true }), null);
  assert.equal(overlayFor(undefined), null);
});

test('an unsold lot has an expected revenue but nothing was received', () => {
  const lot = lotWith({ expected_revenue_milli: 5500 }, { hammer: { winner: null, price: 0, payments: {}, unsold: true } });
  assert.equal(actualRevenue(lot), null);
  assert.deepEqual(overlayFor(lot, { lens: false }).revenue, { expected_milli: 5500, actual: null, all_pay: false });
});

test('overlayFor changes nothing it is given and returns fresh objects', () => {
  const frozen = deepFreeze(structuredClone(sealed.lot(2)));
  const a = overlayFor(frozen, { lens: true });
  const b = overlayFor(frozen, { lens: true });
  assert.deepEqual(a, b);
  assert.notEqual(a.seats, b.seats);
  assert.notEqual(a.seats[0], b.seats[0]);
});

// ------------------------------------------------------------------------------- the view

function mount(model, lotNo, { lens = true, ui } = {}) {
  const doc = new FakeDocument();
  const parent = doc.createElement('main');
  const lot = model.lot(lotNo);
  const view = mountOverlay(doc, parent, lot, { model, lens, ui: ui ?? { glossary: { open: false, focus: null } } });
  return { doc, parent, view, lot };
}

const text = (el) => el.textContent;
const all = (el, selector) => el.querySelectorAll(selector);
const one = (el, selector) => el.querySelector(selector);

/** A frame of the sealed animation, which is all the overlay reads (verdict and reveal). */
const frame = (lot, t) => sealedFrame(lot, t);

test('the overlay is a section that stays hidden until the hammer falls', () => {
  const { view, lot } = mount(sealed, 1);
  assert.equal(view.root.localName, 'section');
  assert.equal(view.root.getAttribute('aria-label'), STR.overlay.title);
  view.update(frame(lot, 0));
  assert.ok(view.root.classList.contains('is-pending'));
  view.update(frame(lot, sealedLayout(lot).verdictAt - 0.01));
  assert.ok(view.root.classList.contains('is-pending'), 'still bidding');
  view.update(frame(lot, sealedLayout(lot).verdictAt));
  assert.ok(!view.root.classList.contains('is-pending'), 'the hammer has fallen');
  view.update(frame(lot, sealedDuration(lot)));
  assert.ok(!view.root.classList.contains('is-pending'));
  view.update(frame(lot, 0));
  assert.ok(view.root.classList.contains('is-pending'), 'and hidden again when the time goes back');
});

test('with the lens: the facts, the table of seats, and a term button for every term it uses', () => {
  const { view } = mount(sealed, 1, { lens: true });
  const root = view.root;
  assert.ok(root.classList.contains('is-lens'));
  assert.equal(text(one(root, '.ov-revenue')), `${STR.overlay.revenue} ${STR.overlay.revenueLine('71.295', 74, false)}`);
  assert.equal(text(one(root, '.ov-highest')), `${STR.overlay.highest}：${STR.overlay.highestYes}`);
  const rows = all(root, '.ov-row');
  assert.equal(rows.length, 6);
  assert.deepEqual(rows.map((r) => r.getAttribute('data-seat')), ['1', '2', '3', '4', '5', '6']);
  assert.deepEqual(all(rows[3], 'td').map(text), ['75', STR.overlay.missing, '7', '+1']);
  assert.deepEqual(all(rows[5], 'td').map(text), ['40', '1.00', '0', '0']);
  assert.equal(text(one(rows[3], 'th')), '4拾光');
  assert.deepEqual(all(root, 'thead th').map((th) => th.getAttribute('scope')), ['col', 'col', 'col', 'col', 'col']);
  assert.deepEqual(all(root, 'tbody th').map((th) => th.getAttribute('scope')), Array(6).fill('row'));
  assert.deepEqual(all(root, 'thead button.term').map(text), ['参考出价', 'α', '后悔值', '相对教科书']);
  assert.deepEqual(all(root, 'button.term').map((b) => b.getAttribute('data-term')), ['revenue', 'highest', 'ref', 'alpha', 'regret', 'excess']);
  assert.equal(all(root, '.ov-hint').length, 0, 'nothing is held back with the lens on');
});

test('excess shows its sign either way, with a real minus sign, and a colour class that follows it', () => {
  const { view } = mount(sealed, 3);
  const cells = Object.fromEntries(all(view.root, '.ov-row').map((r) => [r.getAttribute('data-seat'), all(r, 'td').at(-1)]));
  assert.equal(text(cells[3]), '+71');
  assert.ok(cells[3].classList.contains('is-plus'));
  assert.equal(text(cells[1]), `${MINUS}25`);
  assert.ok(cells[1].classList.contains('is-minus'));
  assert.equal(text(cells[4]), '0');
  assert.ok(cells[4].classList.contains('is-zero'));
  for (const cell of Object.values(cells)) assert.ok(!text(cell).includes('-'), 'never a hyphen');
});

test('alpha is shown as a decimal, for the seats that have one', () => {
  const { view } = mount(sealed, 4);
  const alphas = Object.fromEntries(all(view.root, '.ov-row').map((r) => [r.getAttribute('data-seat'), text(all(r, 'td')[1])]));
  assert.deepEqual(alphas, { 1: '—', 2: '—', 3: '—', 4: '—', 5: '—', 6: '0.92' });
});

test('without the lens: the public lines and a hint, no table, no flag, and no private figure outside the glossary', () => {
  const { view } = mount(sealed, 1, { lens: false });
  const root = view.root;
  assert.ok(!root.classList.contains('is-lens'));
  assert.equal(all(root, 'table').length, 0);
  assert.equal(all(root, '.ov-highest').length, 0);
  assert.equal(all(root, '.ov-row').length, 0);
  assert.equal(text(one(root, '.ov-hint')), STR.overlay.hidden);
  assert.ok(text(one(root, '.ov-revenue')).includes('71.295'));
  const outside = root.children.filter((el) => el.getAttribute('id') !== 'glossary').map(text).join('\n');
  assert.deepEqual(outside.match(/\d+(?:\.\d+)?/g), ['71.295', '74'], 'the only figures are the expected revenue and the price');
  assert.deepEqual(all(root, 'button.term').map((b) => b.getAttribute('data-term')), ['revenue']);
});

test('the blind box line waits for the box to open, and the other lines wait for the hammer only', () => {
  const { view, lot } = mount(sealed, 4);
  const curse = one(view.root, '.ov-curse');
  assert.ok(curse.classList.contains('is-pending'));
  assert.equal(text(curse), `${STR.overlay.curse} +14（${STR.overlay.curseOver}）`);
  const plan = sealedLayout(lot);
  const atHammer = sealedFrame(lot, plan.verdictAt);
  assert.equal(atHammer.reveal, null, 'the hammer has fallen and the box is still shut');
  view.update(atHammer);
  assert.ok(!view.root.classList.contains('is-pending'));
  assert.ok(curse.classList.contains('is-pending'));
  view.update(sealedFrame(lot, plan.revealAt));
  assert.ok(!curse.classList.contains('is-pending'), 'the box is open');
  view.update(sealedFrame(lot, plan.verdictAt - 0.01));
  assert.ok(curse.classList.contains('is-pending'), 'and shut again when the time goes back');
});

test('the curse gap says which way it went', () => {
  const over = mount(sealed, 4).view.root;
  assert.ok(text(one(over, '.ov-curse')).includes(STR.overlay.curseOver));
  const model = indexModel(structuredClone((() => {
    const raw = fixture(SEALED);
    raw.timeline[3].overlay.curse_gap = -12;
    return raw;
  })()));
  assert.equal(text(one(mount(model, 4).view.root, '.ov-curse')), `${STR.overlay.curse} ${MINUS}12（${STR.overlay.curseUnder}）`);
  const zero = fixture(SEALED);
  zero.timeline[3].overlay.curse_gap = 0;
  assert.ok(text(one(mount(indexModel(zero), 4).view.root, '.ov-curse')).includes(STR.overlay.curseSame));
});

test('all-pay says what was received, and an unsold lot says so', () => {
  assert.equal(text(one(mount(sealed, 3, { lens: false }).view.root, '.ov-revenue')), `${STR.overlay.revenue} ${STR.overlay.revenueLine('71.287', 165, true)}`);
  assert.ok(text(one(mount(sealed, 3, { lens: false }).view.root, '.ov-revenue')).includes('实际收到 165'));
  const raw = fixture(SEALED);
  raw.timeline[0].hammer = { format: 'sealed_first', winner: null, price: 0, payments: {}, bids: { 1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0 }, unsold: true };
  const model = indexModel(raw);
  assert.ok(text(one(mount(model, 1, { lens: false }).view.root, '.ov-revenue')).includes('流拍'));
});

test('a lot without any overlay numbers mounts nothing', () => {
  const raw = fixture(SEALED);
  raw.timeline[1].overlay = {};
  const model = indexModel(raw);
  assert.equal(mount(model, 2).view, null);
  assert.equal(mount(model, 2).parent.childNodes.length, 0);
});

// -------------------------------------------------------------------------- the glossary

test('the glossary lists every term, plainly, and starts closed', () => {
  const { view } = mount(sealed, 1);
  const panel = one(view.root, '#glossary');
  assert.ok(panel.hasAttribute('hidden'));
  assert.equal(all(panel, '.gloss-item').length, STR.glossary.length);
  assert.ok(STR.glossary.length >= 10);
  const toggle = one(view.root, '.gloss-toggle');
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
  assert.equal(toggle.getAttribute('aria-controls'), 'glossary');
  assert.equal(text(toggle), STR.overlay.glossary);
  for (const item of STR.glossary) {
    assert.ok(item.term && item.text, item.id);
    assert.ok([...item.text].length <= 120, `${item.id} is short enough to be read at a glance`);
    assert.ok(item.text.split(/[。！？]/).filter(Boolean).length <= 2, `${item.id} is one or two sentences`);
  }
  assert.equal(new Set(STR.glossary.map((g) => g.id)).size, STR.glossary.length, 'ids are unique');
});

test('every term that the overlay shows has an entry in the glossary', () => {
  const ids = new Set(STR.glossary.map((g) => g.id));
  for (const [model, lotNo] of [[sealed, 1], [sealed, 3], [sealed, 4], [clocks, 1], [clocks, 2]]) {
    for (const lens of [true, false]) {
      const { view } = mount(model, lotNo, { lens });
      for (const button of all(view.root, 'button.term')) assert.ok(ids.has(button.getAttribute('data-term')), button.getAttribute('data-term'));
    }
  }
});

test('the button opens and closes the glossary, and says so', () => {
  const ui = { glossary: { open: false, focus: null } };
  const { view } = mount(sealed, 1, { ui });
  const toggle = one(view.root, '.gloss-toggle');
  const panel = one(view.root, '#glossary');
  toggle.dispatch('click');
  assert.equal(ui.glossary.open, true);
  assert.equal(toggle.getAttribute('aria-expanded'), 'true');
  assert.ok(!panel.hasAttribute('hidden'));
  assert.equal(all(view.root, '.gloss-item.is-focus').length, 0);
  toggle.dispatch('click');
  assert.equal(ui.glossary.open, false);
  assert.ok(panel.hasAttribute('hidden'));
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
});

test('a term in the overlay opens the glossary at that term, and only that one is marked', () => {
  const ui = { glossary: { open: false, focus: null } };
  const { view } = mount(sealed, 1, { ui });
  const focused = [];
  for (const entry of all(view.root, '.gloss-item')) entry.focus = () => focused.push(entry.getAttribute('data-term'));
  one(view.root, 'button.term[data-term="regret"]').dispatch('click');
  assert.equal(ui.glossary.open, true);
  assert.equal(ui.glossary.focus, 'regret');
  assert.deepEqual(all(view.root, '.gloss-item.is-focus').map((e) => e.getAttribute('data-term')), ['regret']);
  assert.deepEqual(focused, ['regret'], 'focus moves to the entry');
  one(view.root, 'button.term[data-term="excess"]').dispatch('click');
  assert.deepEqual(all(view.root, '.gloss-item.is-focus').map((e) => e.getAttribute('data-term')), ['excess']);
  assert.ok(!one(view.root, '#glossary').hasAttribute('hidden'));
  one(view.root, '.gloss-toggle').dispatch('click');
  assert.equal(all(view.root, '.gloss-item.is-focus').length, 0, 'closing clears the mark');
});

test('the glossary stays as the reader left it when the next lot, or the lens, rebuilds the overlay', () => {
  const ui = { glossary: { open: false, focus: null } };
  const first = mount(sealed, 1, { ui }).view;
  one(first.root, 'button.term[data-term="ref"]').dispatch('click');
  for (const [lotNo, lens] of [[2, true], [3, false], [4, true]]) {
    const next = mount(sealed, lotNo, { ui, lens }).view;
    assert.ok(!one(next.root, '#glossary').hasAttribute('hidden'), `lot ${lotNo}`);
    assert.equal(one(next.root, '.gloss-toggle').getAttribute('aria-expanded'), 'true');
    assert.deepEqual(all(next.root, '.gloss-item.is-focus').map((e) => e.getAttribute('data-term')), ['ref']);
  }
});

test('every term shows its meaning when the pointer rests on it', () => {
  const meaning = Object.fromEntries(STR.glossary.map((item) => [item.id, item.text]));
  let seen = 0;
  for (const [model, lotNo] of [[sealed, 1], [sealed, 3], [sealed, 4], [clocks, 1], [clocks, 2]]) {
    for (const lens of [true, false]) {
      const { view } = mount(model, lotNo, { lens });
      for (const button of all(view.root, 'button.term')) {
        const title = button.getAttribute('title');
        assert.equal(title, meaning[button.getAttribute('data-term')], `${button.getAttribute('data-term')} in lot ${lotNo}`);
        assert.ok([...title].length > 10 && [...title].length <= 160);
        seen += 1;
      }
    }
  }
  assert.ok(seen >= 30, `${seen} terms were checked`);
});

test('every entry of the glossary list carries its meaning as a hint on its term', () => {
  const { view } = mount(sealed, 1);
  const items = all(view.root, '.gloss-item');
  assert.equal(items.length, STR.glossary.length);
  for (const [i, item] of STR.glossary.entries()) {
    const term = one(items[i], 'dt');
    assert.equal(text(term), item.term);
    assert.equal(term.getAttribute('title'), item.text);
    assert.equal(text(one(items[i], 'dd')), item.text);
  }
});

test('the glossary text is plain text in plain elements', () => {
  const { view } = mount(sealed, 1);
  for (const el of walk(view.root)) assert.ok(['section', 'header', 'h3', 'button', 'p', 'div', 'strong', 'span', 'table', 'caption', 'thead', 'tbody', 'tr', 'th', 'td', 'dl', 'dt', 'dd'].includes(el.localName), el.localName);
  assert.equal(all(view.root, 'a').length, 0);
});

test('the overlay does not name a model id anywhere', () => {
  const { view } = mount(sealed, 1);
  for (const id of ['sample-large-1', 'sample-small-2', 'sample-mid-3', 'sample-mid-4']) assert.ok(!text(view.root).includes(id));
});

test('the overlay never adds the figures of a lot together, or divides them', () => {
  // The per-seat table shows each figure as the replay has it, one per cell, and nothing else.
  const { view } = mount(sealed, 3);
  const lot = sealed.lot(3);
  const cells = all(view.root, '.ov-row').map((r) => all(r, 'td').map(text));
  assert.deepEqual(cells.map((row) => row[0]), [1, 2, 3, 4, 5, 6].map((seat) => String(lot.overlay.ref_bids[seat])));
  assert.deepEqual(cells.map((row) => row[2]), [1, 2, 3, 4, 5, 6].map((seat) => String(lot.overlay.regret[seat])));
  assert.equal(all(view.root, 'tfoot').length, 0, 'no total row');
  assert.ok(!/%|÷|合计|总计|总分|比率|占比|效果/.test(text(view.root)), 'no total, no score, no ratio');
});
