import assert from 'node:assert/strict';
import test from 'node:test';

import { STR } from '../../viewer/lib/strings.js';
import { harnessIds, launch, pageIds, withPage } from './app_harness.js';
import { CLOCKS, fixture, loadFixture, PAYLOADS, poison, SEALED } from './helpers.js';
import { attributeValues, tagNames } from './fake_dom.js';

const NIGHT = 'fixture-sealed-0001';
const lotHash = (n, night = NIGHT) => `#/n/${night}/lot/${n}`;
const okResponse = (text) => async (url) => ({ ok: true, status: 200, url, text: async () => text });
const sealedText = JSON.stringify(loadFixture(SEALED));
const loaded = (options = {}) => ({ search: '?replay=../tests/fixtures/replays/sealed_four_lots.json', fetchImpl: okResponse(sealedText), ...options });
const file = (text) => ({ size: text.length, text: async () => text });
const labelOf = (page) => page.text('time');
const seconds = (label) => {
  const [m, s] = label.split(' / ')[0].split(':').map(Number);
  return m * 60 + s;
};

test('the harness offers exactly the elements that index.html declares', () => {
  assert.deepEqual([...harnessIds].sort(), [...pageIds()].sort());
});

// ------------------------------------------------------------------------- starting up

test('without a replay the page asks for one and shows nothing else', () =>
  withPage({}, async (page) => {
    assert.ok(page.text('panel').includes(STR.welcome.title));
    assert.ok(page.text('panel').includes(STR.welcome.pick));
    assert.equal(page.el('app').hidden, true);
    assert.equal(page.el('controls').hidden, true);
    assert.equal(page.requests.length, 0, 'no request is made without ?replay=');
    assert.equal(page.pendingFrames, 0);
  }));

test('?replay= fetches one file from the same site and starts the night at lot 1', () =>
  withPage(loaded(), async (page) => {
    assert.deepEqual(page.requests.map(([url]) => url), ['http://example.test/tests/fixtures/replays/sealed_four_lots.json']);
    assert.deepEqual(page.requests[0][1], { credentials: 'omit', cache: 'no-cache', referrerPolicy: 'no-referrer' });
    assert.equal(page.el('app').hidden, false);
    assert.equal(page.el('controls').hidden, false);
    assert.equal(page.el('panel').hidden, true);
    assert.equal(page.doc.title, STR.documentTitle(NIGHT));
    assert.ok(page.text('night-label').startsWith(NIGHT));
    assert.equal(page.el('lot-list').querySelectorAll('a.lot-link').length, 4);
    assert.equal(page.currentLot(), '1');
    assert.equal(page.hash, lotHash(1), 'the address is made canonical');
    assert.deepEqual(page.calls.push, [], 'no history entry of its own');
    assert.ok(page.el('stage').textContent.includes('只在雨天变甜的蜂蜜'));
  }));

test('a deep link opens that lot, and a link for another night still opens its lot with a notice', async () => {
  await withPage(loaded({ hash: lotHash(3) }), async (page) => {
    assert.equal(page.currentLot(), '3');
    assert.equal(page.hash, lotHash(3));
    assert.equal(page.el('panel').hidden, true);
  });
  await withPage(loaded({ hash: lotHash(2, 'some-other-night') }), async (page) => {
    assert.equal(page.currentLot(), '2');
    assert.ok(page.text('panel').includes('some-other-night'));
    assert.equal(page.hash, lotHash(2), 'and the address now names this night');
  });
});

test('a link to a lot that was not played falls back to the first lot', () =>
  withPage(loaded({ hash: lotHash(9) }), async (page) => {
    assert.equal(page.currentLot(), '1');
  }));

test('an address that leaves the site is refused before any request', async () => {
  for (const param of ['https://evil.example/x.json', '//evil.example/x.json', 'javascript:alert(1)', '../README.md']) {
    await withPage({ search: `?replay=${encodeURIComponent(param)}` }, async (page) => {
      assert.equal(page.requests.length, 0, param);
      assert.ok(page.text('panel').includes(STR.loadFailed));
      assert.ok(page.text('panel').includes(STR.err.badPath));
      assert.equal(page.el('app').hidden, true);
    });
  }
});

test('a missing file, a bad status and a text that is not a replay are reported in words', async () => {
  await withPage(loaded({ fetchImpl: async () => ({ ok: false, status: 404, text: async () => '' }) }), async (page) => {
    assert.ok(page.text('panel').includes(STR.err.fetchFailed(404)));
  });
  await withPage(loaded({ fetchImpl: async () => { throw new TypeError('offline'); } }), async (page) => {
    assert.ok(page.text('panel').includes(STR.err.network));
  });
  await withPage(loaded({ fetchImpl: okResponse('<html>not json') }), async (page) => {
    assert.ok(page.text('panel').includes(STR.err.notJson));
  });
  await withPage(loaded({ fetchImpl: okResponse('{"schema":"auction-night/replay@2"}') }), async (page) => {
    assert.ok(page.text('panel').includes('auction-night/replay@1'));
    assert.equal(page.el('app').hidden, true);
  });
});

test('reduced motion starts a night paused, with the lot complete, and asks for no frames', () =>
  withPage(loaded({ reducedMotion: true }), async (page) => {
    assert.equal(page.pendingFrames, 0);
    assert.equal(page.el('btn-play').getAttribute('aria-pressed'), 'false');
    const [now, total] = labelOf(page).split(' / ');
    assert.equal(now, total);
    assert.ok(page.el('stage').querySelectorAll('.bubble').every((b) => !b.classList.contains('is-pending')));
  }));

// ---------------------------------------------------------------------------- the clock

test('playing keeps exactly one frame pending, however long it runs', () =>
  withPage(loaded(), async (page) => {
    assert.equal(page.pendingFrames, 1);
    for (let i = 0; i < 400; i += 1) {
      page.frame(16);
      assert.ok(page.pendingFrames <= 1, `frame ${i}`);
    }
    assert.equal(page.maxPending, 1, 'never two frames in flight');
    assert.notEqual(labelOf(page).split(' / ')[0], '0:00', 'time has moved');
  }));

test('a lot plays through into the next, replacing the history entry rather than adding one', () =>
  withPage(loaded(), async (page) => {
    page.clickSpeed(4);
    const entries = page.entries.length;
    page.run(40);
    assert.ok(Number(page.currentLot()) >= 2);
    assert.equal(page.entries.length, entries, 'no new entries');
    assert.deepEqual(page.calls.push, []);
    assert.ok(page.calls.replace.length >= 2, 'the start, then each automatic move');
    assert.equal(page.hash, lotHash(Number(page.currentLot())));
  }));

test('the night ends on the last lot, stopped and complete', () =>
  withPage(loaded(), async (page) => {
    page.clickSpeed(4);
    page.run(120);
    assert.equal(page.currentLot(), '4');
    assert.equal(page.pendingFrames, 0, 'the loop stops by itself');
    assert.equal(page.el('btn-play').getAttribute('aria-pressed'), 'false');
    const [now, total] = labelOf(page).split(' / ');
    assert.equal(now, total);
  }));

test('pause stops the frames, play starts them again, and a long pause does not skip time', () =>
  withPage(loaded(), async (page) => {
    page.run(1);
    page.click('btn-play');
    assert.equal(page.el('btn-play').getAttribute('aria-pressed'), 'false');
    page.frame(16);
    assert.equal(page.pendingFrames, 0);
    const frozen = seconds(labelOf(page));
    page.click('btn-play');
    assert.equal(page.pendingFrames, 1);
    assert.equal(page.el('btn-play').getAttribute('aria-pressed'), 'true');
    page.frame(60_000);
    page.frame(16);
    assert.ok(seconds(labelOf(page)) - frozen <= 1, 'the minute that passed while the tab slept is not played');
  }));

test('the speed buttons choose the speed and show it', () =>
  withPage(loaded(), async (page) => {
    const pressed = () => page.el('speeds').children.filter((b) => b.getAttribute('aria-pressed') === 'true').map((b) => b.getAttribute('data-speed'));
    assert.deepEqual(pressed(), ['1']);
    for (const speed of [0.5, 2, 4, 1]) {
      page.clickSpeed(speed);
      assert.deepEqual(pressed(), [String(speed)]);
    }
    assert.deepEqual(page.el('speeds').children.map((b) => b.getAttribute('data-speed')), ['0.5', '1', '2', '4']);
  }));

test('a faster speed gets through the lot sooner', async () => {
  const seconds = [];
  for (const speed of [1, 4]) {
    await withPage(loaded(), async (page) => {
      page.clickSpeed(speed);
      let frames = 0;
      while (page.currentLot() === '1' && frames < 5000) {
        page.frame(16);
        frames += 1;
      }
      seconds.push(frames);
    });
  }
  assert.ok(seconds[0] > seconds[1] * 3, `${seconds}`);
});

// ------------------------------------------------------------------------- navigation

test('the next and previous buttons add history entries and stop at the ends', () =>
  withPage(loaded(), async (page) => {
    page.click('btn-next');
    page.click('btn-next');
    assert.equal(page.currentLot(), '3');
    assert.deepEqual(page.calls.push, [lotHash(2), lotHash(3)]);
    assert.equal(page.hash, lotHash(3));
    page.click('btn-prev');
    assert.equal(page.currentLot(), '2');
    page.click('btn-prev');
    page.click('btn-prev');
    assert.equal(page.currentLot(), '1');
    assert.equal(page.calls.push.length, 4, 'the last previous did nothing');
  }));

test('back and forward follow the address without adding entries', () =>
  withPage(loaded(), async (page) => {
    page.navigate(lotHash(3));
    assert.equal(page.currentLot(), '3');
    page.click('btn-next');
    assert.equal(page.currentLot(), '4');
    const pushes = page.calls.push.length;
    page.back();
    assert.equal(page.currentLot(), '3');
    page.back();
    assert.equal(page.currentLot(), '1');
    page.forward();
    assert.equal(page.currentLot(), '3');
    assert.equal(page.calls.push.length, pushes, 'traversing history writes nothing');
  }));

test('a lot that is shown after a jump starts over when playing, and is complete when paused', async () => {
  await withPage(loaded(), async (page) => {
    page.run(2);
    page.navigate(lotHash(2));
    assert.equal(labelOf(page).split(' / ')[0], '0:00');
  });
  await withPage(loaded(), async (page) => {
    page.click('btn-play');
    page.navigate(lotHash(2));
    const [now, total] = labelOf(page).split(' / ');
    assert.equal(now, total);
  });
});

test('the lot list marks the current lot and the lots already shown', () =>
  withPage(loaded(), async (page) => {
    page.click('btn-next');
    page.click('btn-next');
    const items = page.el('lot-list').querySelectorAll('.lot-item');
    assert.deepEqual(items.map((i) => i.classList.contains('is-current')), [false, false, true, false]);
    assert.deepEqual(items.map((i) => i.classList.contains('is-visited')), [true, true, true, false]);
  }));

test('the keyboard: space plays and pauses, arrows change lot, and typing in a field is left alone', () =>
  withPage(loaded(), async (page) => {
    const space = page.press(' ');
    assert.equal(space.defaultPrevented, true);
    assert.equal(page.el('btn-play').getAttribute('aria-pressed'), 'false');
    page.press(' ');
    assert.equal(page.el('btn-play').getAttribute('aria-pressed'), 'true');
    page.press('ArrowRight');
    assert.equal(page.currentLot(), '2');
    page.press('ArrowLeft');
    assert.equal(page.currentLot(), '1');
    page.press('ArrowRight', { tagName: 'INPUT' });
    assert.equal(page.currentLot(), '1', 'the range input keeps its own arrow keys');
    assert.equal(page.press(' ', { tagName: 'BUTTON' }).defaultPrevented, false, 'a focused button handles the space itself');
    page.press('ArrowRight', { ctrlKey: true });
    assert.equal(page.currentLot(), '1');
  }));

test('the progress bar seeks inside the lot', () =>
  withPage(loaded(), async (page) => {
    page.click('btn-play');
    page.el('scrub').value = '500';
    page.el('scrub').dispatch('input');
    const [, total] = labelOf(page).split(' / ');
    assert.ok(Math.abs(seconds(labelOf(page)) - seconds(`${total} / ${total}`) / 2) <= 1, labelOf(page));
    page.el('scrub').value = '1000';
    page.el('scrub').dispatch('input');
    assert.equal(labelOf(page).split(' / ')[0], labelOf(page).split(' / ')[1]);
    assert.ok(page.el('stage').querySelector('.verdict-main'), 'the hammer has fallen');
  }));

// -------------------------------------------------------------------------------- the lens

test('the lens switch rebuilds the stage without and with the hole cards', () =>
  withPage(loaded(), async (page) => {
    page.click('btn-play');
    const count = () => page.el('stage').querySelectorAll('.thought').length;
    assert.ok(count() > 0);
    assert.equal(page.el('lens').getAttribute('aria-checked'), 'true');
    assert.ok(page.text('stage').includes(STR.lens.on));
    page.click('lens');
    assert.equal(count(), 0);
    assert.equal(page.el('lens').getAttribute('aria-checked'), 'false');
    assert.ok(page.text('stage').includes(STR.lens.off));
    assert.ok(page.text('lens').includes(STR.lens.label));
    page.click('lens');
    assert.ok(count() > 0);
  }));

test('the lens is on when a night opens', () =>
  withPage(loaded(), async (page) => {
    assert.equal(page.el('lens').getAttribute('aria-checked'), 'true');
    assert.ok(page.text('stage').includes('选手当时没看到'));
  }));

test('switching the lens keeps the lot and the time', () =>
  withPage(loaded(), async (page) => {
    page.click('btn-next');
    page.click('btn-play');
    page.el('scrub').value = '300';
    page.el('scrub').dispatch('input');
    const before = labelOf(page);
    page.click('lens');
    assert.equal(page.currentLot(), '2');
    assert.equal(labelOf(page), before);
  }));

// ----------------------------------------------------------------------------- the picker

test('a chosen file opens the night, and a deep link opened before it is honoured', () =>
  withPage({ hash: lotHash(3) }, async (page) => {
    assert.ok(page.text('panel').includes(STR.welcome.title));
    await page.chooseFile(file(sealedText));
    assert.equal(page.currentLot(), '3');
    assert.equal(page.el('app').hidden, false);
    assert.equal(page.requests.length, 0, 'a chosen file is never fetched');
    assert.equal(page.el('file').value, '', 'the same file can be chosen again');
  }));

test('a second file starts at its first lot whatever the address said', () =>
  withPage({ hash: lotHash(3) }, async (page) => {
    await page.chooseFile(file(sealedText));
    assert.equal(page.currentLot(), '3');
    await page.chooseFile(file(JSON.stringify(loadFixture(CLOCKS))));
    assert.equal(page.currentLot(), '1');
    assert.ok(page.text('night-label').startsWith('fixture-english-dutch-0001'));
    assert.equal(page.el('lot-list').querySelectorAll('a.lot-link').length, 2);
    assert.equal(page.hash, lotHash(1, 'fixture-english-dutch-0001'));
    assert.equal(page.pendingFrames, 1, 'one clock, not two');
  }));

test('a file that is not a replay is reported, and the page can try again', () =>
  withPage({}, async (page) => {
    await page.chooseFile(file('{"hello":1}'));
    assert.ok(page.text('panel').includes(STR.loadFailed));
    assert.ok(page.text('panel').includes(STR.err.noSchema));
    await page.chooseFile(file(sealedText));
    assert.equal(page.el('app').hidden, false);
    assert.equal(page.el('panel').hidden, true);
  }));

test('a file that is too large is refused without being read', () =>
  withPage({}, async (page) => {
    let read = false;
    await page.chooseFile({ size: 1e9, text: async () => ((read = true), '') });
    assert.equal(read, false);
    assert.ok(page.text('panel').includes(STR.err.tooLarge));
  }));

// ------------------------------------------------------------------ the overlay and the clocks

const clocksText = JSON.stringify(loadFixture(CLOCKS));

test('the overlay appears with the hammer, and the glossary stays open from lot to lot and through the lens', () =>
  withPage({}, async (page) => {
    await page.chooseFile(file(clocksText));
    const stage = page.el('stage');
    const overlay = () => stage.querySelector('.overlay');
    page.click('btn-play');
    assert.ok(overlay().classList.contains('is-pending'), 'not before the hammer');
    page.el('scrub').value = '1000';
    page.el('scrub').dispatch('input');
    assert.ok(!overlay().classList.contains('is-pending'));
    assert.ok(stage.querySelector('.eng-chip.is-won'), 'the English lot ends on its winner');
    stage.querySelector('button.term[data-term="regret"]').dispatch('click');
    assert.ok(!stage.querySelector('#glossary').hasAttribute('hidden'));
    page.click('btn-next');
    assert.equal(page.currentLot(), '2');
    assert.ok(stage.querySelector('.dut-cells'), 'the Dutch lot has its clock');
    assert.ok(!stage.querySelector('#glossary').hasAttribute('hidden'), 'still open on the next lot');
    assert.deepEqual(stage.querySelectorAll('.gloss-item.is-focus').map((e) => e.getAttribute('data-term')), ['regret']);
    page.click('lens');
    assert.ok(!stage.querySelector('#glossary').hasAttribute('hidden'), 'and when the lens is switched');
    stage.querySelector('.gloss-toggle').dispatch('click');
    assert.ok(stage.querySelector('#glossary').hasAttribute('hidden'));
    page.click('btn-prev');
    assert.ok(stage.querySelector('#glossary').hasAttribute('hidden'), 'closed stays closed');
  }));

test('both clocks play from the first beat to the last, one lot into the next, with a single frame in flight', () =>
  withPage({}, async (page) => {
    await page.chooseFile(file(clocksText));
    page.clickSpeed(4);
    page.run(120);
    assert.equal(page.currentLot(), '2');
    assert.equal(page.pendingFrames, 0, 'the clock stops by itself at the end of the night');
    assert.equal(page.maxPending, 1);
    assert.ok(page.el('stage').querySelector('.dut-cell.is-stop'), 'the Dutch clock rests on its sale');
    assert.equal(page.el('stage').querySelectorAll('.verdict-body').length, 1);
  }));

test('the winner exit price shows on the bar with the lens and disappears when the lens is switched off', () =>
  withPage({}, async (page) => {
    await page.chooseFile(file(clocksText));
    page.el('scrub').value = '1000';
    page.el('scrub').dispatch('input');
    const ghosts = () => page.el('stage').querySelectorAll('.eng-ghost').filter((g) => !g.classList.contains('is-pending'));
    assert.equal(ghosts().length, 1);
    page.click('lens');
    assert.equal(page.el('stage').querySelectorAll('.eng-ghost').length, 0);
    assert.ok(!page.text('stage').includes('赢家的退出价'));
    page.click('lens');
    assert.equal(ghosts().length, 1, 'back with the lens, at the same time');
  }));

// ----------------------------------------------------------------------- hostile files

test('a replay full of hostile text makes a page with the same elements as any other', async () => {
  const reference = await withPage(loaded(), async (page) => {
    page.press('ArrowRight');
    return [...tagNames(page.el('stage'))].sort();
  });
  for (const payload of Object.values(PAYLOADS)) {
    await withPage({}, async (page) => {
      await page.chooseFile(file(JSON.stringify(poison(fixture(SEALED), payload))));
      assert.equal(page.el('app').hidden, false);
      page.press('ArrowRight');
      page.run(30);
      for (const root of [page.el('stage'), page.el('lot-list'), page.el('panel'), page.el('night-label')]) {
        for (const tag of tagNames(root)) assert.ok([...reference, 'a', 'small', 'li', 'ol'].includes(tag), `${tag} for ${payload}`);
        for (const { name, value } of attributeValues(root)) {
          assert.ok(!name.startsWith('on') && name !== 'style' && name !== 'src', name);
          assert.ok(!/[<>"]/.test(value), `${name}=${value}`);
        }
      }
      assert.match(page.hash, /^#\/n\/[\w%.~-]+\/lot\/\d+$/, 'the address stays inside the link alphabet');
    });
  }
});

test('a night id with odd characters still makes working links', () =>
  withPage({}, async (page) => {
    const raw = fixture(SEALED);
    raw.night.id = 'night (1)/a b?c#d';
    await page.chooseFile(file(JSON.stringify(raw)));
    assert.equal(page.el('app').hidden, false);
    const hrefs = page.el('lot-list').querySelectorAll('a').map((a) => a.getAttribute('href'));
    assert.equal(hrefs.length, 4);
    for (const href of hrefs) assert.match(href, /^#\/n\/[\w%.~-]+\/lot\/\d$/);
    page.click('btn-next');
    assert.equal(page.currentLot(), '2');
    page.navigate(hrefs[3]);
    assert.equal(page.currentLot(), '4');
  }));

test('launching twice gives two independent pages', async () => {
  const a = await launch(loaded());
  const lotA = a.currentLot();
  a.close();
  const b = await launch(loaded({ hash: lotHash(2) }));
  assert.equal(lotA, '1');
  assert.equal(b.currentLot(), '2');
  b.close();
});

test('a night of twelve lots lists twelve and plays them in order to the last', () =>
  withPage({}, async (page) => {
    const raw = fixture(SEALED);
    const timeline = raw.timeline;
    const catalog = raw.catalog;
    raw.night.n_lots = 12;
    raw.timeline = [];
    raw.catalog = [];
    for (let k = 1; k <= 12; k += 1) {
      raw.timeline.push({ ...structuredClone(timeline[(k - 1) % 4]), lot: k });
      raw.catalog.push({ ...structuredClone(catalog[(k - 1) % 4]), lot: k });
    }
    await page.chooseFile(file(JSON.stringify(raw)));
    assert.equal(page.el('lot-list').querySelectorAll('a.lot-link').length, 12);
    assert.ok(page.text('night-label').includes('12 件拍品'));
    assert.ok(page.text('stage').includes('第 1 / 12 件'));
    for (let k = 2; k <= 12; k += 1) page.press('ArrowRight');
    assert.equal(page.currentLot(), '12');
    page.press('ArrowRight');
    assert.equal(page.currentLot(), '12', 'the last lot is the end');
    assert.equal(page.hash, lotHash(12));
    page.click('lens');
    page.clickSpeed(4);
    page.navigate(lotHash(10));
    page.click('btn-play');
    page.click('btn-play');
    page.run(300);
    assert.equal(page.currentLot(), '12');
    assert.equal(page.pendingFrames, 0, 'the night is over and the clock has stopped');
  }));
