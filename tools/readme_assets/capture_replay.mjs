#!/usr/bin/env node
// Capture the replay page: an animated GIF of one lot, or a still of a lot or of a page.
//
//   CHROME_BIN=<headless chromium> node tools/readme_assets/capture_replay.mjs \
//       --replay <replay.json> --lot <k> --lang zh --out <file.gif|file.png> [options]
//
// What it does
//   1. Starts a file server on 127.0.0.1, on a port the system picks, that serves the folder
//      viewer/ and, under /replay.json, the replay file. Nothing else is served.
//   2. Starts the headless Chromium (CHROME_BIN) and drives the replay page through the Chrome
//      DevTools Protocol, with Node's own WebSocket. The page is paused and moved by its own
//      progress bar, so the pictures do not depend on how fast the machine is.
//   3. For a GIF, takes one screenshot per frame along a storyboard, then lets ffmpeg make the
//      GIF with a palette made from the frames (palettegen and paletteuse).
//
// Required
//   --replay <file>      a replay.json (schema auction-night/replay@1)
//   --out <file>         the picture to write; .gif makes an animation, .png a still
//   --lot <k>            the lot to show (not needed for a page of the night, see --view)
//   --lang zh|en         the locale of the browser, see the note below
//
// Options
//   --theme light|dark   the colour scheme of the page (default: dark)
//   --view <name>        lot (default), highlights, awards or review. A GIF is always a lot.
//   --lens on|off        the hole-card lens (default: on)
//   --scene <name>       for a still of a lot: card, chat, bids, seats or host (default: bids), the
//                        part of the lot that the picture starts at (seats: the hole cards of the seats)
//   --at <t>             for a still of a lot: seconds into the lot, or end (default: end)
//   --lines <n>          GIF: how many chat lines to show (default: 4)
//   --fps <n>            GIF: frames per second (default: 10)
//   --width <px>         width of the picture (default: 960)
//   --height <px>        height of the picture (default: 600)
//   --max-mb <n>         GIF: the size it must stay under (default: 4)
//   --chrome <file>      the browser (default: the environment variable CHROME_BIN)
//   --tmp <dir>          directory for the temporary files (default: the system's)
//   --keep               keep the temporary files
//
// Note on --lang. The replay page has a Chinese interface only, so --lang only sets the locale
// of the browser. Both values give the same picture, and the script says so for en.

import { spawn } from 'node:child_process';
import { mkdir, readFile, rm, stat, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';
import { Browser, chromeBin, makeWorkDir } from './lib/chrome.mjs';
import { serve } from './lib/serve.mjs';
import { animationFor } from '../../viewer/anim/index.js';
import { parseReplay } from '../../viewer/lib/load.js';
import { buildScript } from '../../viewer/lib/script.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const VIEWER = path.resolve(HERE, '..', '..', 'viewer');
const VIEWS = ['lot', 'highlights', 'awards', 'review'];
const SCENES = ['card', 'chat', 'bids', 'seats', 'host'];

function fail(message) {
  console.error(`capture_replay: ${message}`);
  process.exit(2);
}

const { values: opt } = parseArgs({
  options: {
    replay: { type: 'string' },
    out: { type: 'string' },
    lot: { type: 'string' },
    lang: { type: 'string' },
    theme: { type: 'string', default: 'dark' },
    view: { type: 'string', default: 'lot' },
    lens: { type: 'string', default: 'on' },
    scene: { type: 'string', default: 'bids' },
    at: { type: 'string', default: 'end' },
    lines: { type: 'string', default: '4' },
    fps: { type: 'string', default: '10' },
    width: { type: 'string', default: '960' },
    height: { type: 'string', default: '600' },
    'max-mb': { type: 'string', default: '4' },
    chrome: { type: 'string' },
    tmp: { type: 'string' },
    keep: { type: 'boolean', default: false },
    help: { type: 'boolean', short: 'h', default: false },
  },
});

if (opt.help) {
  console.log(
    'usage: CHROME_BIN=<headless chromium> node tools/readme_assets/capture_replay.mjs --replay FILE --lot K ' +
      '--lang zh|en --out FILE.gif|FILE.png [--theme light|dark] [--view lot|highlights|awards|review] ' +
      '[--lens on|off] [--scene card|chat|bids|seats|host] [--at SECONDS|end] [--lines N] [--fps N] [--width PX] ' +
      '[--height PX] [--max-mb N] [--chrome FILE] [--tmp DIR] [--keep]\n(see the comment at the top of the file)',
  );
  process.exit(0);
}

for (const name of ['replay', 'out', 'lang']) if (!opt[name]) fail(`--${name} is required`);
if (!['zh', 'en'].includes(opt.lang)) fail('--lang must be zh or en');
if (!['light', 'dark'].includes(opt.theme)) fail('--theme must be light or dark');
if (!VIEWS.includes(opt.view)) fail(`--view must be one of ${VIEWS.join(', ')}`);
if (!['on', 'off'].includes(opt.lens)) fail('--lens must be on or off');
if (!SCENES.includes(opt.scene)) fail(`--scene must be one of ${SCENES.join(', ')}`);
const extension = path.extname(opt.out).toLowerCase();
if (extension !== '.gif' && extension !== '.png') fail('--out must end in .gif or .png');
const isGif = extension === '.gif';
if (isGif && opt.view !== 'lot') fail('a GIF shows a lot: leave --view out');
if (opt.view === 'lot' && !opt.lot) fail('--lot is required for a lot');
const num = (name, min, max) => {
  const value = Number(opt[name]);
  if (!Number.isFinite(value) || value < min || value > max) fail(`--${name} must be a number from ${min} to ${max}`);
  return value;
};
const LINES = Math.round(num('lines', 0, 8));
const FPS = Math.round(num('fps', 4, 25));
const WIDTH = Math.round(num('width', 240, 2400));
const HEIGHT = Math.round(num('height', 135, 1600));
const MAX_BYTES = num('max-mb', 0.1, 64) * 1024 * 1024;
const GIF_SECONDS_MAX = 15;
// A picture of the page gets a one-pixel outline, so that it stands out on a page of nearly its own colour.
const OUTLINE = { light: 'cfc5ab', dark: '363e5e' };
const AT = opt.at === 'end' ? null : Number(opt.at);
if (AT !== null && (!Number.isFinite(AT) || AT < 0)) fail('--at must be a number of seconds, or end');

if (opt.lang === 'en') {
  console.log('capture_replay: the replay page has a Chinese interface only, so --lang only sets the locale of the browser');
}

try {
  chromeBin(opt.chrome);
} catch (error) {
  fail(error.message);
}

const replayFile = path.resolve(opt.replay);
const replayText = await readFile(replayFile, 'utf8').catch(() => fail(`cannot read ${opt.replay}`));
let model;
try {
  model = parseReplay(replayText);
} catch (error) {
  fail(`${opt.replay} is not a replay the viewer accepts: ${error.messages?.join(' ') ?? error.message}`);
}
const nightId = model.night.id;
const lotNo = opt.view === 'lot' ? Number(opt.lot) : null;
if (opt.view === 'lot' && !model.lotNos.includes(lotNo)) {
  fail(`lot ${opt.lot} was not played (played: ${model.lotNos.join(', ')})`);
}

// ------------------------------------------------------------------- the script of a lot

/** The beats of a lot, as the page lays them out (viewer/lib/script.js). */
function scriptOf(number) {
  const lot = model.lot(number);
  const animation = animationFor(lot.format);
  const script = buildScript(lot, animation.duration(lot), animation.hammerAt(lot));
  return { lot, script, bidEnd: script.bidStart + script.bidSeconds };
}

/**
 * The storyboard of a GIF: shots, each with the scene the camera looks at, the stretch of the
 * lot it plays (seconds on the clock of the lot) and how long it lasts in the GIF. A chat line is
 * shown for a short time that grows a little with its length; the envelopes, the clock or the
 * price bar run faster than in the page; the hammer and the host are held long enough to read.
 */
function storyboard(number, lines) {
  const { script, bidEnd } = scriptOf(number);
  const clamp = (x, lo, hi) => Math.min(hi, Math.max(lo, x));
  const talks = script.beats.filter((beat) => beat.kind === 'talk');
  const shots = [];
  const card = script.beats.find((beat) => beat.kind === 'card');
  shots.push({ scene: 'card', from: 0, to: card.end, secs: 1.2 });
  for (const beat of talks.slice(0, lines)) {
    shots.push({ scene: 'chat', from: beat.start, to: beat.end, secs: clamp((beat.end - beat.start) * 0.55, 0.9, 1.5) });
  }
  const hammer = script.hammerAt ?? bidEnd;
  shots.push({ scene: 'bids', from: script.bidStart, to: hammer, secs: clamp((hammer - script.bidStart) / 2.6, 2.4, 5) });
  if (bidEnd > hammer) shots.push({ scene: 'bids', from: hammer, to: bidEnd, secs: clamp((bidEnd - hammer) * 0.7, 1.2, 2.2) });
  shots.push({ scene: 'bids', from: bidEnd, to: bidEnd, secs: 0.6 });
  if (script.hasHost) {
    const host = script.beats.find((beat) => beat.kind === 'host');
    shots.push({ scene: 'host', from: host.start, to: host.end, secs: 1.6 });
  }
  return { shots, script };
}

const total = (shots) => shots.reduce((sum, shot) => sum + shot.secs, 0);

// -------------------------------------------------------------------------- the camera

/**
 * The code that runs in the page and says where the camera is for a scene: the left edge and the
 * top of the picture, in the coordinates of the whole page. It looks at the page as it is at that
 * moment, because the page grows as the lines and the bids appear.
 */
const CAMERA = `((scene, height, compact) => {
  const stage = document.getElementById('lot-stage');
  const box = stage.getBoundingClientRect();
  const top = (el) => el.getBoundingClientRect().top + window.scrollY;
  const bottom = (el) => el.getBoundingClientRect().bottom + window.scrollY;
  const shown = (el) => el !== null && el.getClientRects().length > 0;
  const card = stage.querySelector('.lot-card');
  const bids = stage.querySelector('.bids');
  const chat = stage.querySelector('.chat-box');
  const seats = stage.querySelector('.seats');
  const host = stage.querySelector('.host');
  const verdict = bids.querySelector('.verdict-main');
  const verdictBox = bids.querySelector('.verdict');
  const start = top(card) - 10;
  let y = start;
  if (scene === 'chat') {
    const lines = chat.querySelectorAll('.bubble:not(.is-pending)');
    const floor = top(chat) - 12;
    y = lines.length === 0 ? floor : Math.max(floor, bottom(lines[lines.length - 1]) + 14 - height);
  } else if (scene === 'bids' && !compact) {
    // the bids from their top, and the hammer at the foot of the picture once it has fallen
    y = shown(verdict) ? Math.max(top(bids) - 12, bottom(verdictBox) + 16 - height) : top(bids) - 12;
  } else if (scene === 'seats') {
    y = top(seats) - 10;
  } else if (scene === 'host') {
    y = shown(host) ? bottom(host) + 14 - height : start;
  }
  return { x: box.left + window.scrollX - 10, y: Math.max(0, y), width: box.width + 20 };
})`;

/**
 * Whether the lot above the fold fits in the picture: its card, the lens note, the bids and the
 * hammer. It is measured once, at the end of the lot, so that the camera does not jump while the
 * bids grow.
 */
const FITS = `((height) => {
  const stage = document.getElementById('lot-stage');
  const card = stage.querySelector('.lot-card');
  const verdict = stage.querySelector('.bids .verdict');
  if (verdict === null || verdict.getClientRects().length === 0) return false;
  const top = card.getBoundingClientRect().top + window.scrollY - 10;
  return verdict.getBoundingClientRect().bottom + window.scrollY + 16 - top <= height;
})`;

/** The camera for a page of the night (awards, highlights, review): the top of the page. */
const PAGE_CAMERA = `(() => {
  const page = document.getElementById('page');
  const box = page.getBoundingClientRect();
  return { x: box.left + window.scrollX - 10, y: box.top + window.scrollY - 10, width: box.width + 20,
           full: box.height + 20 };
})()`;

// ------------------------------------------------------------------------ the browser

const work = await makeWorkDir(opt.tmp);
const server = await serve({ root: VIEWER, mounts: { '/replay.json': replayFile } });
const browser = await Browser.launch({ bin: opt.chrome, workDir: work });
let exitCode = 0;
try {
  const page = await browser.newPage({ width: 1280, height: 900, scale: 1, colorScheme: opt.theme });
  await page.send('Emulation.setLocaleOverride', { locale: opt.lang === 'zh' ? 'zh-CN' : 'en-US' }).catch(() => {});
  const startHash =
    opt.view === 'lot' ? `#/n/${encodeURIComponent(nightId)}/lot/${lotNo}` : `#/n/${encodeURIComponent(nightId)}/lot/${model.lotNos[0]}`;
  await page.goto(`${server.origin}/index.html?replay=replay.json${startHash}`);
  await page.waitFor(
    "document.querySelector('#lot-stage .lot-card') !== null || document.querySelector('#panel [role=alert]') !== null",
    { what: 'the replay page to show a lot' },
  );
  const refused = await page.evaluate("document.querySelector('#panel [role=alert]')?.textContent ?? null");
  if (refused !== null) throw new Error(`the replay page did not accept the replay: ${refused}`);

  // Pause the page. From here on the page only moves when this script moves it.
  await page.evaluate(`(() => {
    const play = document.getElementById('btn-play');
    if (play.getAttribute('aria-pressed') === 'true') play.click();
  })()`);
  // The page reads "reduced motion" once, when it starts, and the picture of every moment should
  // not depend on a transition that is half done. Asking for reduced motion now freezes the style
  // sheet's animations and leaves the page's own animation alone.
  const media = [
    { name: 'prefers-color-scheme', value: opt.theme },
    { name: 'prefers-reduced-motion', value: 'reduce' },
  ];
  await page.setMedia(media);
  if (opt.lens === 'off') {
    await page.evaluate(`(() => { const lens = document.getElementById('lens'); if (lens.getAttribute('aria-checked') === 'true') lens.click(); })()`);
  }

  /** Move the page to second t of the lot on show, by its own progress bar. */
  const seek = (t, length) =>
    page.evaluate(`(() => {
      const scrub = document.getElementById('scrub');
      scrub.step = 'any'; // the bar then takes any value, not only whole thousandths
      scrub.value = String(Math.min(1, Math.max(0, ${t} / ${length})) * 1000);
      scrub.dispatchEvent(new Event('input'));
    })()`);

  /** Whether the lot fits above the fold (see FITS): measured with the lot played to its end. */
  const fitsAbove = async (length) => {
    await seek(length, length);
    return page.evaluate(`${FITS}(${HEIGHT})`);
  };

  /** Take the picture at the camera: a Buffer of PNG, WIDTH x HEIGHT before scaling. */
  const shoot = async (camera, height = HEIGHT) => {
    const clip = { x: camera.x, y: camera.y, width: camera.width, height };
    return page.screenshot({ clip });
  };

  if (opt.view !== 'lot') {
    // The highlights and the review stay behind a gate until the night has been watched to its
    // end, so go to the end of the last lot first, and then to the page.
    const last = model.lotNos.at(-1);
    const go = (hash) => page.evaluate(`(() => { location.hash = ${JSON.stringify(hash)}; })()`);
    await go(`#/n/${encodeURIComponent(nightId)}/lot/${last}`);
    await page.waitFor(`document.querySelector('.lot-item.is-current .lot-link')?.dataset.lot === '${last}'`, {
      what: 'the last lot',
    });
    await seek(1, 1);
    await go(`#/n/${encodeURIComponent(nightId)}/${opt.view}`);
    await page.waitFor("document.getElementById('page').hidden === false", { what: `the ${opt.view} page` });
    await new Promise((resolve) => setTimeout(resolve, 150));
    const camera = await page.evaluate(PAGE_CAMERA);
    await writePicture(await shoot(camera, Math.min(HEIGHT, Math.ceil(camera.full))));
  } else if (!isGif) {
    const { script } = scriptOf(lotNo);
    const at = AT === null ? script.duration : AT;
    const compact = await fitsAbove(script.duration);
    await seek(at, script.duration);
    const camera = await page.evaluate(`${CAMERA}(${JSON.stringify(opt.scene)}, ${HEIGHT}, ${compact})`);
    await writePicture(await shoot(camera));
  } else {
    await captureGif();
  }

  async function writePicture(png) {
    await mkdir(path.dirname(path.resolve(opt.out)), { recursive: true });
    const tmp = path.join(work, 'still.png');
    await writeFile(tmp, png);
    await ffmpeg(['-y', '-v', 'error', '-i', tmp, '-vf', `scale=${WIDTH}:-1:flags=lanczos,${outline()}`, '-frames:v', '1', path.resolve(opt.out)]);
    const { size } = await stat(opt.out);
    console.log(`${opt.out}  ${(size / 1024).toFixed(0)} KiB`);
  }

  async function captureGif() {
    let lines = LINES;
    let board = storyboard(lotNo, lines);
    while (total(board.shots) > GIF_SECONDS_MAX && lines > 1) board = storyboard(lotNo, --lines);
    const seconds = total(board.shots);
    if (seconds > GIF_SECONDS_MAX) throw new Error(`the storyboard lasts ${seconds.toFixed(1)} s, more than ${GIF_SECONDS_MAX} s`);
    const frames = Math.round(seconds * FPS);
    const dir = path.join(work, 'frames');
    await mkdir(dir, { recursive: true });
    const length = board.script.duration;
    const compact = await fitsAbove(length);
    console.log(`lot ${lotNo}: ${board.shots.length} shots, ${seconds.toFixed(1)} s, ${frames} frames, ${lines} chat lines, lot ${compact ? 'fits' : 'does not fit'} above the fold`);
    const starts = [];
    let clock = 0;
    for (const shot of board.shots) {
      starts.push(clock);
      clock += shot.secs;
    }
    let clipWidth = 960;
    for (let i = 0; i < frames; i += 1) {
      const g = (i + 0.5) / FPS; // the time of this frame in the GIF
      let k = starts.findLastIndex((s) => s <= g);
      if (k < 0) k = 0;
      const shot = board.shots[k];
      const share = Math.min(1, Math.max(0, (g - starts[k]) / shot.secs));
      await seek(shot.from + (shot.to - shot.from) * share, length);
      const camera = await page.evaluate(`${CAMERA}(${JSON.stringify(shot.scene)}, ${HEIGHT}, ${compact})`);
      clipWidth = camera.width;
      const png = await shoot(camera);
      await writeFile(path.join(dir, `${String(i + 1).padStart(4, '0')}.png`), png);
    }
    await mkdir(path.dirname(path.resolve(opt.out)), { recursive: true });
    const palette = path.join(dir, 'palette.png');
    const out = path.resolve(opt.out);
    let size = Infinity;
    for (const colors of [256, 128, 96, 64, 48, 32]) {
      await ffmpeg([
        '-y', '-v', 'error', '-framerate', String(FPS), '-i', path.join(dir, '%04d.png'),
        '-vf', `scale=${WIDTH}:-1:flags=lanczos,${outline()},palettegen=max_colors=${colors}:stats_mode=full`, palette,
      ]);
      await ffmpeg([
        '-y', '-v', 'error', '-framerate', String(FPS), '-i', path.join(dir, '%04d.png'), '-i', palette,
        '-lavfi', `scale=${WIDTH}:-1:flags=lanczos,${outline()} [x]; [x][1:v] paletteuse=dither=none:diff_mode=rectangle`,
        '-loop', '0', out,
      ]);
      size = (await stat(out)).size;
      console.log(`${opt.out}  ${colors} colours  ${(size / 1024).toFixed(0)} KiB`);
      if (size <= MAX_BYTES) break;
    }
    if (size <= MAX_BYTES) console.log(`${opt.out}: ${WIDTH}x${Math.round((HEIGHT * WIDTH) / clipWidth)}, ${seconds.toFixed(1)} s at ${FPS} frames per second`);
    if (size > MAX_BYTES) {
      console.error(`capture_replay: the GIF is larger than ${(MAX_BYTES / 1048576).toFixed(1)} MB even with 32 colours; show fewer lines or lower --fps`);
      exitCode = 1;
    }
  }
} catch (error) {
  console.error(`capture_replay: ${error.message ?? error}`);
  exitCode = 1;
} finally {
  await browser.close();
  await server.close();
  if (!opt.keep) await rm(work, { recursive: true, force: true });
}
process.exit(exitCode);

/** The ffmpeg filter that draws the outline of a picture. */
function outline() {
  return `drawbox=x=0:y=0:w=iw:h=ih:color=0x${OUTLINE[opt.theme]}:t=1`;
}

function ffmpeg(args) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.env.FFMPEG ?? 'ffmpeg', args, { stdio: ['ignore', 'inherit', 'inherit'] });
    child.once('error', (error) => reject(new Error(`cannot run ffmpeg: ${error.message}`)));
    child.once('exit', (code) => (code === 0 ? resolve() : reject(new Error(`ffmpeg exited with ${code}`))));
  });
}
