#!/usr/bin/env node
// Build the figures of the README: one PNG for every figure, theme and language.
//
//   CHROME_BIN=<headless chromium> node tools/readme_assets/build.mjs [options]
//
//   --out <dir>       where the PNGs go (default: docs/assets)
//   --only <list>     comma-separated figures to build (default: all of them)
//   --theme <name>    light or dark (default: both)
//   --lang <name>     en or zh (default: both)
//   --scale <n>       pixel ratio of the PNGs (default: 2)
//   --tmp <dir>       directory for the browser's temporary files (default: the system's)
//   --keep            keep the browser's temporary directory
//
// The figures are the HTML files in tools/readme_assets/figures/. Each one is a single card
// whose colours follow data-theme and whose text follows data-lang, so one source gives
// <name>-<lang>-<theme>.png in all four combinations. The browser is the headless shell of
// Chromium, started through lib/chrome.mjs. Nothing is loaded from the network.

import { mkdir, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { parseArgs } from 'node:util';
import { Browser, chromeBin, makeWorkDir } from './lib/chrome.mjs';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '..', '..');
const FIGURES = ['banner', 'night', 'formats', 'architecture'];
const THEMES = ['light', 'dark'];
const LANGS = ['en', 'zh'];
const LIMIT = 400 * 1024; // the size one PNG may reach

function listOf(value, allowed, name) {
  if (value === undefined) return allowed;
  const chosen = value.split(',').map((s) => s.trim()).filter(Boolean);
  const unknown = chosen.filter((c) => !allowed.includes(c));
  if (unknown.length > 0) throw new Error(`unknown ${name}: ${unknown.join(', ')} (known: ${allowed.join(', ')})`);
  return chosen;
}

const { values } = parseArgs({
  options: {
    out: { type: 'string', default: path.join(REPO, 'docs', 'assets') },
    only: { type: 'string' },
    theme: { type: 'string' },
    lang: { type: 'string' },
    scale: { type: 'string', default: '2' },
    tmp: { type: 'string' },
    keep: { type: 'boolean', default: false },
    help: { type: 'boolean', short: 'h', default: false },
  },
});

if (values.help) {
  console.log(
    'usage: CHROME_BIN=<headless chromium> node tools/readme_assets/build.mjs ' +
      '[--out DIR] [--only a,b] [--theme light|dark] [--lang en|zh] [--scale N] [--tmp DIR] [--keep]',
  );
  process.exit(0);
}

const outDir = path.resolve(values.out);
const figures = listOf(values.only, FIGURES, 'figure');
const themes = listOf(values.theme, THEMES, 'theme');
const langs = listOf(values.lang, LANGS, 'language');
const scale = Number(values.scale);
if (!(scale >= 1 && scale <= 4)) throw new Error('--scale must be between 1 and 4');

try {
  chromeBin();
} catch (error) {
  console.error(`build: ${error.message}`);
  process.exit(2);
}
await mkdir(outDir, { recursive: true });
const work = await makeWorkDir(values.tmp);
const browser = await Browser.launch({ workDir: work });
let failed = false;
try {
  const page = await browser.newPage({ width: 1000, height: 1400, scale, transparent: true });
  for (const figure of figures) {
    for (const lang of langs) {
      for (const theme of themes) {
        const url = `${pathToFileURL(path.join(HERE, 'figures', `${figure}.html`)).href}?theme=${theme}&lang=${lang}`;
        await page.goto(url);
        await page.waitFor('window.__ready === true', { what: `${figure} to be ready` });
        const box = await page.evaluate(`(() => {
          const r = document.getElementById('card').getBoundingClientRect();
          return { x: r.x, y: r.y, width: r.width, height: r.height };
        })()`);
        const png = await page.screenshot({ clip: { ...box, scale: 1 } });
        const file = path.join(outDir, `${figure}-${lang}-${theme}.png`);
        await writeFile(file, png);
        const kib = (png.length / 1024).toFixed(0);
        const over = png.length > LIMIT;
        if (over) failed = true;
        console.log(
          `${path.relative(REPO, file)}  ${Math.round(box.width * scale)}x${Math.round(box.height * scale)}  ${kib} KiB${over ? '  OVER THE LIMIT' : ''}`,
        );
      }
    }
  }
} finally {
  await browser.close();
  if (!values.keep) await rm(work, { recursive: true, force: true });
}
if (failed) {
  console.error('a PNG is larger than 400 KiB: simplify the figure or shrink the PNG');
  process.exit(1);
}
