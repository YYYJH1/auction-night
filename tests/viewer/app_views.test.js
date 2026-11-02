// The five parts of the page, driven through the real app.js: the links of the top bar, the
// awards, the list of highlights, the review of the host, a jump to a moment, and the
// three-minute version.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { STR } from '../../viewer/lib/strings.js';
import { launch, STAGE_PARTS, withPage } from './app_harness.js';
import { attributeValues, serialize, tagNames } from './fake_dom.js';
import { CLOCKS, fixture, loadFixture, PAYLOADS, poison, SEALED, twelveLotReplay, VIEWER } from './helpers.js';

const NIGHT = 'fixture-sealed-0001';
const lotHash = (n) => `#/n/${NIGHT}/lot/${n}`;
const momentHash = (id) => `#/n/${NIGHT}/moment/${id}`;
const pageHash = (name, night = NIGHT) => `#/n/${night}/${name}`;
const okResponse = (text) => async (url) => ({ ok: true, status: 200, url, text: async () => text });
const sealedText = JSON.stringify(loadFixture(SEALED));
const loaded = (options = {}) => ({ search: '?replay=../tests/fixtures/replays/sealed_four_lots.json', fetchImpl: okResponse(sealedText), ...options });
const file = (text) => ({ size: text.length, text: async () => text });
const timeOf = (page) => page.text('time').split(' / ')[0];
const lengthOf = (page) => page.text('time').split(' / ')[1];
const seconds = (label) => {
  const [m, s] = label.split(' / ')[0].split(':').map(Number);
  return m * 60 + s;
};
const awards = (page) => page.el('page').querySelectorAll('.award');
const lights = (page) => page.el('page').querySelectorAll('.light');
const kicker = (page) => page.el('recap-bar').querySelector('.recap-kicker')?.textContent ?? null;
const links = (page) => Object.fromEntries(page.el('views').children.map((a) => [a.getAttribute('data-view'), a]));

/** Play the recap through at the top speed and note the lots in the order they were on show. */
function playRecap(page, limit = 200) {
  const seen = [];
  for (let i = 0; i < limit * 60 && page.pendingFrames > 0; i += 1) {
    page.frame(16);
    const lot = page.currentLot();
    if (seen.at(-1) !== lot) seen.push(lot);
  }
  return seen;
}

// ------------------------------------------------------------------------ the markup

test('the markup holds the caption of the recap, the lot and the page in that order inside the stage', () => {
  const html = readFileSync(new URL('index.html', `file://${VIEWER}`), 'utf8');
  const order = [...html.match(/<main id="stage"[^>]*>([\s\S]*?)<\/main>/)[1].matchAll(/<div id="([\w-]+)"/g)].map((m) => m[1]);
  assert.deepEqual(order, STAGE_PARTS);
  assert.match(html, /<div id="page"[^>]*tabindex="-1"[^>]*hidden>/, 'the page can take focus, and is hidden until it is needed');
  assert.match(html, /<nav id="views"[^>]*hidden>/);
  assert.match(html, /<div id="transport"[^>]*>[\s\S]*id="btn-prev"[\s\S]*id="scrub"[\s\S]*id="time"[\s\S]*<\/div>\s*<button[^>]*id="lens"/, 'the lens is outside the transport');
});

test('the page measures its top bar, so that the column of lots sticks below it however many rows the bar has', () =>
  withPage(loaded({ barHeight: 87 }), async (page) => {
    assert.equal(page.rootStyle('--topbar-h'), '87px');
    page.resizeBar(126);
    assert.equal(page.rootStyle('--topbar-h'), '126px', 'the bar wrapped onto a third row');
    page.resizeBar(58);
    assert.equal(page.rootStyle('--topbar-h'), '58px');
  }));

test('in a browser that cannot watch the size of an element the style sheet keeps its own height for the bar', () =>
  withPage(loaded(), async (page) => {
    assert.equal(page.rootStyle('--topbar-h'), '');
    assert.equal(page.el('app').hidden, false, 'and the page works');
  }));

// ------------------------------------------------------------------ the links of the top bar

test('the top bar offers the five parts as links, the lots first, and marks the one on show', () =>
  withPage(loaded(), async (page) => {
    const nav = page.el('views');
    assert.equal(nav.hidden, false);
    assert.equal(nav.getAttribute('aria-label'), STR.views.label);
    assert.deepEqual(nav.children.map((a) => a.getAttribute('data-view')), ['lots', 'recap', 'highlights', 'awards', 'review']);
    assert.deepEqual(nav.children.map((a) => a.textContent), ['逐件观看', '三分钟看完一夜', '高光时刻', '颁奖', '主持人复盘']);
    assert.deepEqual(nav.children.map((a) => a.getAttribute('href')), [lotHash(1), pageHash('recap'), pageHash('highlights'), pageHash('awards'), pageHash('review')]);
    assert.deepEqual(nav.children.map((a) => a.getAttribute('aria-current')), ['page', null, null, null, null]);
    assert.ok(nav.children.every((a) => !a.hasAttribute('aria-disabled')));
    assert.ok(links(page).lots.classList.contains('is-current'));
  }));

test('the link to the lots follows the lot that is playing', () =>
  withPage(loaded(), async (page) => {
    page.click('btn-next');
    page.click('btn-next');
    assert.equal(links(page).lots.getAttribute('href'), lotHash(3));
    page.navigate(pageHash('awards'));
    assert.equal(links(page).lots.getAttribute('href'), lotHash(3), 'and keeps it while another part is on show');
  }));

// -------------------------------------------------------------------------- the awards

test('the awards part shows ten cards where the lot was, and nothing runs behind it', () =>
  withPage(loaded(), async (page) => {
    page.run(1);
    page.navigate(pageHash('awards'));
    assert.equal(page.el('page').hidden, false);
    assert.equal(page.el('lot-stage').hidden, true);
    assert.equal(page.el('recap-bar').hidden, true);
    assert.equal(awards(page).length, 10);
    assert.equal(page.el('page').querySelector('.page-title').textContent, STR.awards.title);
    assert.deepEqual(page.el('views').children.map((a) => a.getAttribute('aria-current')), [null, null, null, 'page', null]);
    assert.equal(page.el('transport').hidden, true, 'no play, no speed, no progress bar');
    assert.equal(page.el('controls').hidden, false);
    assert.equal(page.el('lens').getAttribute('aria-checked'), 'true', 'the lens stays within reach');
    assert.equal(page.pendingFrames, 0, 'the clock has stopped');
    assert.equal(page.hash, pageHash('awards'));
    assert.equal(page.el('lot-list').querySelectorAll('.lot-item.is-current').length, 0, 'no lot is the current one');
  }));

test('back to the lots: the stage returns, and play goes on where it was', () =>
  withPage(loaded(), async (page) => {
    page.run(2);
    const before = seconds(page.text('time'));
    page.navigate(pageHash('awards'));
    assert.equal(seconds(page.text('time')), before, 'the label of the lot is not touched');
    page.navigate(lotHash(1));
    assert.equal(page.el('lot-stage').hidden, false);
    assert.equal(page.el('page').hidden, true);
    assert.equal(page.el('transport').hidden, false);
    assert.equal(page.currentLot(), '1');
    assert.equal(page.pendingFrames, 1, 'it was playing, so it plays');
    assert.ok(seconds(page.text('time')) >= before, 'and did not start over');
    page.run(2);
    assert.ok(seconds(page.text('time')) > before);
  }));

test('a paused night stays paused after a visit to another part', () =>
  withPage(loaded(), async (page) => {
    page.click('btn-play');
    page.navigate(pageHash('highlights'));
    page.navigate(lotHash(1));
    assert.equal(page.pendingFrames, 0);
    assert.equal(page.el('btn-play').getAttribute('aria-pressed'), 'false');
  }));

test('back and forward move between the parts, and the address is the one the part has', () =>
  withPage(loaded(), async (page) => {
    page.navigate(pageHash('awards'));
    page.navigate(pageHash('highlights'));
    assert.equal(page.el('page').querySelector('.highlights') !== null, true);
    page.back();
    assert.equal(awards(page).length, 10);
    page.back();
    assert.equal(page.el('lot-stage').hidden, false);
    page.forward();
    assert.equal(awards(page).length, 10);
    assert.deepEqual(page.calls.push, [], 'following the address writes nothing');
  }));

test('a link to another night still opens the part of this night', () =>
  withPage(loaded(), async (page) => {
    page.navigate(pageHash('awards', 'some-other-night'));
    assert.equal(awards(page).length, 10);
  }));

test('the lens switches what the awards show, and the choice is the same everywhere', () =>
  withPage(loaded(), async (page) => {
    page.navigate(pageHash('awards'));
    assert.equal(page.el('page').querySelectorAll('.award.is-held').length, 0);
    page.click('lens');
    assert.equal(page.el('lens').getAttribute('aria-checked'), 'false');
    assert.equal(page.el('page').querySelectorAll('.award.is-held').length, 5);
    assert.ok(!page.text('page').includes('阿铁'), 'the winner of a private award is not named');
    assert.equal(awards(page).length, 10);
    page.navigate(lotHash(1));
    assert.equal(page.el('lot-stage').querySelectorAll('.thought').length, 0, 'the lots follow the same switch');
    page.navigate(pageHash('awards'));
    assert.equal(page.el('page').querySelectorAll('.award.is-held').length, 5);
    page.click('lens');
    assert.equal(page.el('page').querySelectorAll('.award.is-held').length, 0);
  }));

// ------------------------------------------------------------------------ the highlights

test('the highlights are folded until the night has been watched, or the reader unfolds them', () =>
  withPage(loaded(), async (page) => {
    page.navigate(pageHash('highlights'));
    assert.equal(page.el('page').querySelectorAll('.gate').length, 1);
    assert.equal(lights(page).length, 0);
    assert.ok(!page.text('page').includes('托儿把指南针'), 'no sentence of the night is in the page');
    page.el('page').querySelector('button.gate-open').dispatch('click');
    assert.equal(lights(page).length, 7);
    assert.equal(page.el('page').querySelectorAll('.gate').length, 0);
    assert.deepEqual(lights(page).map((el) => el.getAttribute('data-id')), ['3', '7', '1', '4', '5', '6', '2']);
    page.navigate(lotHash(2));
    page.navigate(pageHash('highlights'));
    assert.equal(lights(page).length, 7, 'unfolded stays unfolded');
  }));

test('watching the night to its last lot unfolds the highlights by itself', () =>
  withPage(loaded(), async (page) => {
    page.clickSpeed(4);
    page.run(120);
    assert.equal(page.currentLot(), '4');
    assert.equal(page.pendingFrames, 0);
    page.navigate(pageHash('highlights'));
    assert.equal(lights(page).length, 7);
  }));

test('an unfinished night does not unfold them: a lot that is only visited is not watched', () =>
  withPage(loaded(), async (page) => {
    page.click('btn-next');
    page.click('btn-next');
    page.click('btn-next');
    assert.equal(page.currentLot(), '4');
    page.navigate(pageHash('highlights'));
    assert.equal(lights(page).length, 0, 'lot 4 is open but its end has not been reached');
  }));

test('a new replay folds the highlights again', () =>
  withPage(loaded(), async (page) => {
    page.navigate(pageHash('highlights'));
    page.el('page').querySelector('button.gate-open').dispatch('click');
    await page.chooseFile(file(JSON.stringify(loadFixture(CLOCKS))));
    page.navigate(pageHash('highlights', 'fixture-english-dutch-0001'));
    assert.equal(lights(page).length, 0);
    assert.equal(page.el('page').querySelectorAll('.gate').length, 1);
  }));

test('the lens decides which highlights keep their sentence, and the filter keeps its choice through it', () =>
  withPage(loaded(), async (page) => {
    page.navigate(pageHash('highlights'));
    page.el('page').querySelector('button.gate-open').dispatch('click');
    assert.equal(page.el('page').querySelectorAll('.light-held').length, 0);
    page.el('page').querySelectorAll('.filter button').find((b) => b.getAttribute('data-category') === 'bargain').dispatch('click');
    page.click('lens');
    assert.equal(page.el('page').querySelectorAll('.light-held').length, 4);
    const shown = lights(page).filter((el) => !el.classList.contains('is-filtered'));
    assert.deepEqual(shown.map((el) => el.getAttribute('data-category')), ['bargain'], 'the filter survived');
    assert.ok(!page.text('page').includes('小满花 55 拿下估值 88'));
  }));

// ---------------------------------------------------------------------------- moments

test('the address of a highlight opens its lot at the moment where it begins, and plays from there', () =>
  withPage(loaded(), async (page) => {
    page.navigate(momentHash(7));
    assert.equal(page.currentLot(), '4');
    assert.equal(timeOf(page), '0:14', 'the bid animation of lot 4 begins 14.45 seconds into the lot');
    assert.equal(lengthOf(page), '0:32');
    assert.equal(page.el('btn-play').getAttribute('aria-pressed'), 'true');
    assert.equal(page.pendingFrames, 1);
    assert.equal(page.hash, momentHash(7), 'the address stays as it was asked for');
    assert.equal(page.el('lot-stage').hidden, false);
    page.run(1);
    assert.ok(seconds(page.text('time')) >= 14);
  }));

test('a moment that opens in the talk begins at a line, one that opens with the lot at the start', () =>
  withPage(loaded(), async (page) => {
    page.navigate(momentHash(2));
    assert.equal(page.currentLot(), '1');
    assert.equal(timeOf(page), '0:03', 'the second line of lot 1: seat 2 speaks after seat 1');
    page.navigate(momentHash(3));
    assert.equal(page.currentLot(), '2');
    assert.equal(timeOf(page), '0:00', 'a highlight of the whole lot begins at its start');
  }));

test('a moment in the lot on show seeks inside it, and an unknown moment does nothing', () =>
  withPage(loaded(), async (page) => {
    page.navigate(lotHash(4));
    page.navigate(momentHash(7));
    assert.equal(page.currentLot(), '4');
    assert.equal(timeOf(page), '0:14');
    page.navigate(momentHash(99));
    assert.equal(page.currentLot(), '4');
    assert.equal(timeOf(page), '0:14', 'nothing moved');
    assert.equal(page.hash, momentHash(99), 'the address is what the browser says; the page does not rewrite it');
  }));

test('a moment link opened from the highlights page plays even after a pause', () =>
  withPage(loaded(), async (page) => {
    page.click('btn-play');
    page.navigate(pageHash('highlights'));
    page.el('page').querySelector('button.gate-open').dispatch('click');
    const link = page.el('page').querySelector('a[data-moment="7"]');
    assert.equal(link.getAttribute('href'), momentHash(7));
    page.navigate(link.getAttribute('href'));
    assert.equal(page.currentLot(), '4');
    assert.equal(page.el('btn-play').getAttribute('aria-pressed'), 'true');
    assert.equal(page.pendingFrames, 1);
    assert.equal(timeOf(page), '0:14');
  }));

test('playing on from a moment into the next lot writes the address of that lot, not a new entry', () =>
  withPage(loaded(), async (page) => {
    page.navigate(momentHash(1));
    const entries = page.entries.length;
    page.clickSpeed(4);
    page.run(7);
    assert.equal(page.currentLot(), '2');
    assert.equal(page.hash, lotHash(2));
    assert.equal(page.entries.length, entries);
  }));

test('a link to a moment opens the night there, and a moment that does not exist falls back to lot 1', async () => {
  await withPage(loaded({ hash: momentHash(7) }), async (page) => {
    assert.equal(page.currentLot(), '4');
    assert.equal(timeOf(page), '0:14');
    assert.equal(page.hash, momentHash(7));
    assert.deepEqual(page.calls.push, []);
  });
  await withPage(loaded({ hash: momentHash(99) }), async (page) => {
    assert.equal(page.currentLot(), '1');
    assert.equal(timeOf(page), '0:00');
    assert.equal(page.hash, lotHash(1));
  });
});

test('with reduced motion a moment is shown complete and paused, like every lot', () =>
  withPage(loaded({ reducedMotion: true }), async (page) => {
    page.navigate(momentHash(7));
    assert.equal(page.currentLot(), '4');
    assert.equal(timeOf(page), lengthOf(page));
    assert.equal(page.el('btn-play').getAttribute('aria-pressed'), 'false');
    assert.equal(page.pendingFrames, 0);
  }));

// ------------------------------------------------------------------ the three minutes

test('the three-minute version opens on its first segment, with the caption, and plays', () =>
  withPage(loaded(), async (page) => {
    page.navigate(pageHash('recap'));
    assert.equal(page.el('recap-bar').hidden, false);
    assert.equal(page.el('lot-stage').hidden, false);
    assert.equal(page.el('page').hidden, true);
    assert.equal(kicker(page), '三分钟看完一夜 · 第 1 / 4 段');
    assert.equal(page.el('recap-bar').querySelector('.recap-lot').textContent, '第 1 件 只在雨天变甜的蜂蜜');
    assert.equal(page.el('recap-bar').querySelector('.recap-inner').getAttribute('data-phase'), 'card');
    assert.equal(timeOf(page), '0:00');
    assert.equal(lengthOf(page), '1:50');
    assert.equal(page.el('btn-play').getAttribute('aria-pressed'), 'true');
    assert.equal(page.pendingFrames, 1);
    assert.equal(page.currentLot(), '1');
    assert.equal(page.hash, pageHash('recap'));
    assert.deepEqual(page.el('views').children.map((a) => a.getAttribute('aria-current')), [null, 'page', null, null, null]);
    assert.equal(page.text('btn-prev'), '‹ 上一段');
    assert.equal(page.text('btn-next'), '下一段 ›');
  }));

test('the card gives way to the caption after two seconds, and the lot plays from its window', () =>
  withPage(loaded(), async (page) => {
    page.navigate(pageHash('recap'));
    const phase = () => page.el('recap-bar').querySelector('.recap-inner').getAttribute('data-phase');
    page.run(1.5);
    assert.equal(phase(), 'card');
    page.run(1);
    assert.equal(phase(), 'play');
    assert.ok(page.el('lot-stage').querySelectorAll('.bubble').some((b) => !b.classList.contains('is-pending')), 'lines of the lot appear');
  }));

test('the whole version plays through the four lots in order and stops at the end with the next steps', () =>
  withPage(loaded(), async (page) => {
    page.clickSpeed(4);
    page.navigate(pageHash('recap'));
    const seen = playRecap(page);
    assert.deepEqual(seen, ['1', '2', '3', '4']);
    assert.equal(page.pendingFrames, 0, 'the clock stops by itself');
    assert.equal(page.maxPending, 1, 'one frame in flight, always');
    assert.equal(timeOf(page), lengthOf(page));
    assert.equal(page.el('btn-play').getAttribute('aria-pressed'), 'false');
    assert.ok(!page.el('recap-bar').querySelector('.recap-end').hasAttribute('hidden'));
    assert.equal(kicker(page), '三分钟看完一夜 · 第 4 / 4 段');
    assert.equal(page.hash, pageHash('recap'), 'one address for the whole version');
    assert.deepEqual(page.calls.push, []);
  }));

test('after the three minutes the highlights are unfolded, and play starts the version over', () =>
  withPage(loaded(), async (page) => {
    page.clickSpeed(4);
    page.navigate(pageHash('recap'));
    playRecap(page);
    page.navigate(pageHash('highlights'));
    assert.equal(lights(page).length, 7);
    page.navigate(pageHash('recap'));
    assert.equal(timeOf(page), '0:00', 'a visit to the version always begins at the first segment');
    assert.equal(kicker(page), '三分钟看完一夜 · 第 1 / 4 段');
    assert.equal(page.pendingFrames, 1);
  }));

test('play at the end of the version starts it over', () =>
  withPage(loaded(), async (page) => {
    page.clickSpeed(4);
    page.navigate(pageHash('recap'));
    playRecap(page);
    page.click('btn-play');
    assert.equal(timeOf(page), '0:00');
    assert.equal(page.pendingFrames, 1);
    assert.equal(kicker(page), '三分钟看完一夜 · 第 1 / 4 段');
  }));

test('next and previous move between segments, and a previous a little into a segment goes to its own start', () =>
  withPage(loaded(), async (page) => {
    page.navigate(pageHash('recap'));
    page.click('btn-next');
    assert.equal(kicker(page), '三分钟看完一夜 · 第 2 / 4 段');
    assert.equal(timeOf(page), '0:29', 'the second segment begins 28.57 seconds in');
    assert.equal(page.currentLot(), '2');
    page.run(3);
    page.click('btn-prev');
    assert.equal(timeOf(page), '0:29', 'back to the start of this segment');
    page.click('btn-prev');
    assert.equal(kicker(page), '三分钟看完一夜 · 第 1 / 4 段');
    assert.equal(timeOf(page), '0:00');
    page.click('btn-prev');
    assert.equal(timeOf(page), '0:00', 'nothing before the first');
    for (let i = 0; i < 6; i += 1) page.click('btn-next');
    assert.equal(kicker(page), '三分钟看完一夜 · 第 4 / 4 段', 'nothing after the last');
    assert.deepEqual(page.calls.push, [], 'moving between segments writes no address');
  }));

test('the arrow keys move between segments in the version, and space plays and pauses', () =>
  withPage(loaded(), async (page) => {
    page.navigate(pageHash('recap'));
    page.press('ArrowRight');
    assert.equal(kicker(page), '三分钟看完一夜 · 第 2 / 4 段');
    page.press('ArrowRight');
    page.press('ArrowLeft');
    page.press('ArrowLeft');
    assert.equal(kicker(page), '三分钟看完一夜 · 第 1 / 4 段');
    assert.equal(page.press(' ').defaultPrevented, true);
    assert.equal(page.el('btn-play').getAttribute('aria-pressed'), 'false');
    page.frame(16);
    assert.equal(page.pendingFrames, 0);
    page.press(' ');
    assert.equal(page.pendingFrames, 1);
  }));

test('the progress bar seeks across the whole version, and the speed buttons speed it up', () =>
  withPage(loaded(), async (page) => {
    page.navigate(pageHash('recap'));
    page.click('btn-play');
    page.el('scrub').value = '500';
    page.el('scrub').dispatch('input');
    assert.equal(timeOf(page), '0:55');
    assert.equal(kicker(page), '三分钟看完一夜 · 第 2 / 4 段', '54.9 seconds in is the second segment');
    page.el('scrub').value = '1000';
    page.el('scrub').dispatch('input');
    assert.equal(timeOf(page), lengthOf(page));
    assert.equal(kicker(page), '三分钟看完一夜 · 第 4 / 4 段');
    assert.ok(!page.el('recap-bar').querySelector('.recap-end').hasAttribute('hidden'));
    page.el('scrub').value = '0';
    page.el('scrub').dispatch('input');
    assert.ok(page.el('recap-bar').querySelector('.recap-end').hasAttribute('hidden'));
    assert.equal(page.el('scrub').getAttribute('aria-label'), STR.recap.seek);
    const pressed = () => page.el('speeds').children.filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => b.getAttribute('data-speed'));
    page.clickSpeed(4);
    assert.deepEqual(pressed(), ['4']);
    page.navigate(lotHash(1));
    assert.deepEqual(pressed(), ['4'], 'the speed is the same in the lots');
  }));

test('the lot list follows the segment, and the lens changes what the caption says', () =>
  withPage(loaded(), async (page) => {
    page.navigate(pageHash('recap'));
    page.click('btn-next');
    page.click('btn-next');
    assert.equal(page.currentLot(), '3');
    assert.deepEqual(page.el('lot-list').querySelectorAll('.lot-item').map((i) => i.classList.contains('is-visited')), [true, true, true, false]);
    const caption = () => page.el('recap-bar').querySelectorAll('.recap-text').map((e) => e.textContent);
    assert.deepEqual(caption(), [sealedHighlight(5).text, sealedHighlight(6).text]);
    page.click('lens');
    assert.deepEqual(caption(), [sealedHighlight(5).text, STR.recap.held], 'the bargain rests on a private value');
    assert.equal(page.el('lot-stage').querySelectorAll('.thought').length, 0);
    assert.equal(kicker(page), '三分钟看完一夜 · 第 3 / 4 段', 'the lens does not move the version');
    page.click('lens');
    assert.deepEqual(caption(), [sealedHighlight(5).text, sealedHighlight(6).text]);
  }));
const sealedHighlight = (id) => loadFixture(SEALED).highlights.find((h) => h.id === id);

test('leaving the version stops it, and the clock never runs for two parts at once', () =>
  withPage(loaded(), async (page) => {
    page.navigate(pageHash('recap'));
    page.run(3);
    page.navigate(pageHash('awards'));
    assert.equal(page.pendingFrames, 0);
    page.navigate(lotHash(2));
    page.navigate(pageHash('recap'));
    page.navigate(lotHash(1));
    page.navigate(pageHash('recap'));
    assert.equal(page.maxPending, 1);
    assert.equal(page.pendingFrames, 1);
    assert.equal(timeOf(page), '0:00');
  }));

test('a link to a lot while the version plays leaves it for that lot', () =>
  withPage(loaded(), async (page) => {
    page.navigate(pageHash('recap'));
    page.run(5);
    page.navigate(lotHash(3));
    assert.equal(page.currentLot(), '3');
    assert.equal(page.el('recap-bar').hidden, true);
    assert.equal(page.text('btn-next'), '下一件 ›');
    assert.equal(page.hash, lotHash(3));
  }));

test('a night opened on the address of a part opens that part, and keeps the address', async () => {
  await withPage(loaded({ hash: pageHash('awards') }), async (page) => {
    assert.equal(awards(page).length, 10);
    assert.equal(page.hash, pageHash('awards'));
    assert.equal(page.pendingFrames, 0);
    assert.equal(page.el('lot-stage').hidden, true);
  });
  await withPage(loaded({ hash: pageHash('highlights') }), async (page) => {
    assert.equal(page.el('page').querySelectorAll('.gate').length, 1);
    assert.equal(page.hash, pageHash('highlights'));
  });
  await withPage(loaded({ hash: pageHash('recap') }), async (page) => {
    assert.equal(kicker(page), '三分钟看完一夜 · 第 1 / 4 段');
    assert.equal(page.hash, pageHash('recap'));
    assert.equal(page.pendingFrames, 1);
    assert.equal(page.maxPending, 1);
    assert.deepEqual(page.calls.push, []);
  });
});

test('with reduced motion the version starts paused at its first segment, and play runs it', () =>
  withPage(loaded({ reducedMotion: true }), async (page) => {
    page.navigate(pageHash('recap'));
    assert.equal(timeOf(page), '0:00');
    assert.equal(page.el('btn-play').getAttribute('aria-pressed'), 'false');
    assert.equal(page.pendingFrames, 0);
    page.click('btn-play');
    assert.equal(page.pendingFrames, 1);
  }));

// --------------------------------------------------------------- a night with no highlights

const quietText = (() => {
  const raw = fixture(SEALED);
  delete raw.highlights;
  return JSON.stringify(raw);
})();

test('a night with no highlights has no three-minute version: the link is off and says why', () =>
  withPage(loaded({ fetchImpl: okResponse(quietText) }), async (page) => {
    const recap = links(page).recap;
    assert.equal(recap.hasAttribute('href'), false);
    assert.equal(recap.getAttribute('aria-disabled'), 'true');
    assert.equal(recap.getAttribute('title'), STR.views.recapOff);
    assert.ok(recap.classList.contains('is-off'));
    page.navigate(pageHash('recap'));
    assert.equal(page.el('recap-bar').hidden, true, 'it falls back to the lots');
    assert.equal(page.el('lot-stage').hidden, false);
    assert.equal(page.hash, lotHash(1), 'and the address says so');
    page.navigate(pageHash('highlights'));
    page.el('page').querySelector('button.gate-open').dispatch('click');
    assert.equal(page.el('page').querySelector('.empty').textContent, STR.highlights.empty);
  }));

test('opened on the address of the version, a night with no highlights opens on its lots', () =>
  withPage(loaded({ fetchImpl: okResponse(quietText), hash: pageHash('recap') }), async (page) => {
    assert.equal(page.hash, lotHash(1));
    assert.equal(page.currentLot(), '1');
    assert.equal(page.el('recap-bar').hidden, true);
  }));

// ------------------------------------------------------------------ a night of twelve lots

test('a night of twelve lots comes to three minutes in the version, and plays to its end', () =>
  withPage({}, async (page) => {
    await page.chooseFile(file(JSON.stringify(twelveLotReplay())));
    page.navigate(pageHash('recap', 'fixture-sealed-0001'));
    assert.equal(lengthOf(page), '3:00');
    assert.match(kicker(page), /^三分钟看完一夜 · 第 1 \/ 6 段$/);
    page.clickSpeed(4);
    const seen = playRecap(page, 60);
    assert.equal(seen.length, 6);
    assert.deepEqual(seen, [...seen].sort((a, b) => Number(a) - Number(b)), 'in the order of the night');
    assert.equal(page.pendingFrames, 0);
  }));

// ----------------------------------------------------------------------- hostile files

const ALLOWED_TAGS = new Set([
  'main', 'header', 'div', 'p', 'h2', 'h3', 'section', 'article', 'span', 'strong', 'small', 'ol', 'ul', 'li', 'a', 'button',
  'table', 'caption', 'thead', 'tbody', 'tr', 'th', 'td', 'dl', 'dt', 'dd', 'nav',
]);
const SAFE_ATTRIBUTES = /^(?:class|id|role|type|hidden|tabindex|scope|title|href|data-[a-z-]+|aria-[a-z]+)$/;

for (const [name, payload] of Object.entries(PAYLOADS)) {
  test(`hostile text "${name}" is inert in every part of the page`, () =>
    withPage({}, async (page) => {
      await page.chooseFile(file(JSON.stringify(poison(fixture(SEALED), payload))));
      const roots = () => [page.el('stage'), page.el('views'), page.el('lot-list'), page.el('panel'), page.el('night-label')];
      const check = (label) => {
        for (const root of roots()) {
          for (const tag of tagNames(root)) assert.ok(ALLOWED_TAGS.has(tag), `${label}: <${tag}>`);
          for (const { name: attr, value } of attributeValues(root)) {
            assert.match(attr, SAFE_ATTRIBUTES, `${label}: ${attr}`);
            assert.ok(!/[<>"]/.test(value), `${label}: ${attr}=${value}`);
            if (attr === 'href') assert.match(value, /^#\/n\/[\w%.~-]+\/(?:lot\/\d+|moment\/\d+|recap|highlights|awards|review)$/, label);
          }
          const markup = serialize(root);
          for (const needle of ['<img', '<script', '<svg', '<iframe', '<b ', '<style']) assert.ok(!markup.includes(needle), `${label}: ${needle}`);
        }
        assert.match(page.hash, /^#\/n\/[\w%.~-]+\/(?:lot\/\d+|moment\/\d+|recap|highlights|awards|review)$/, label);
      };
      const night = encodeURIComponent(payload).replace(/[!'()*]/g, (c) => `%${c.charCodeAt(0).toString(16).toUpperCase()}`);
      const sealedNight = (view) => `#/n/${night}/${view}`;
      check('lots');
      for (const lens of [true, false]) {
        if ((page.el('lens').getAttribute('aria-checked') === 'true') !== lens) page.click('lens');
        page.navigate(sealedNight('awards'));
        check(`awards lens ${lens}`);
        page.navigate(sealedNight('highlights'));
        page.el('page').querySelector('button.gate-open')?.dispatch('click');
        check(`highlights lens ${lens}`);
        page.navigate(sealedNight('review'));
        check(`review lens ${lens}`);
        page.navigate(sealedNight('recap'));
        page.run(8);
        page.click('btn-next');
        check(`recap lens ${lens}`);
        page.navigate(sealedNight('moment/7'));
        page.run(3);
        check(`moment lens ${lens}`);
      }
    }));
}

test('launching the page twice, once on a part, gives two independent pages', async () => {
  const a = await launch(loaded({ hash: pageHash('awards') }));
  const onAwards = a.el('page').hidden;
  a.close();
  const b = await launch(loaded());
  const onLots = b.el('page').hidden;
  b.close();
  assert.equal(onAwards, false);
  assert.equal(onLots, true);
});
