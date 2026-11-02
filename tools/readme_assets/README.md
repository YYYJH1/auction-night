<p align="center"><b>English</b> · <a href="README_zh.md">简体中文</a></p>

# README pictures

The pictures in `docs/assets/` that the two READMEs and [docs/how-it-works.md](../../docs/how-it-works.md) show are made by the scripts in this folder. They need Node 22, ffmpeg and a headless Chromium (the "headless shell" build), and add no package to the project.

```
export CHROME_BIN=/path/to/chrome-headless-shell
```

The browser is started with `--incognito` and with its profile and temporary files in the directory given by `--tmp` (default: the system's temporary directory). Chinese text uses Noto Sans CJK SC, so that font has to be installed. ffmpeg is taken from `PATH`, or from the variable `FFMPEG`.

## Figures

`figures/` holds one HTML file per figure: `banner`, `night`, `formats` and `architecture`. Each is a single rounded card. `figures.css` defines the colours as variables for the light and the dark theme, and every text exists twice, as `<span class="en">` and `<span class="zh">`, so one source gives four pictures. `connect.js` draws the arrows between the boxes.

```
node tools/readme_assets/build.mjs
```

writes `docs/assets/<figure>-<en|zh>-<light|dark>.png` at twice the pixel ratio. `--only formats`, `--lang zh`, `--theme dark`, `--out DIR` and `--tmp DIR` narrow or redirect it, and a PNG over 400 KiB is an error. To look at a figure in a browser, open `figures/night.html?theme=dark&lang=zh`.

## Pictures of the replay page

`capture_replay.mjs` starts a file server on 127.0.0.1 (a port the system picks) that serves `viewer/` and the replay file, drives the replay page in the browser through the Chrome DevTools Protocol, and takes screenshots. For a GIF it follows a storyboard (the lot card, a few chat lines, the bids and the hammer, the host) and lets ffmpeg make the GIF from a palette of its frames. The page is paused and moved by its own progress bar, so the same replay gives the same frames.

The pictures of the replay page are the lot GIF in the READMEs and the awards page and the review page in [docs/how-it-works.md](../../docs/how-it-works.md), each in the light and in the dark theme:

```
R=docs/demo/trial-d01-r1.json    # the night of the pictures; any replay.json from auction-night export works
for t in light dark; do
  node tools/readme_assets/capture_replay.mjs --replay $R --lot 5 --lang zh --theme $t --out docs/assets/lot-$t.gif
  node tools/readme_assets/capture_replay.mjs --replay $R --view awards --lang zh --theme $t --height 582 --out docs/assets/awards-$t.png
  node tools/readme_assets/capture_replay.mjs --replay $R --view review --lang zh --theme $t --height 641 --out docs/assets/review-$t.png
done
```

For another night, point `R` at its `replay.json` and choose the lot with `--lot`.

- `--lines N` sets how many chat lines the GIF shows (default 4), `--fps` and `--width` the frame rate (10) and the width (960), and `--height` the height of every picture (600). A GIF has to stay under `--max-mb` (4), and the script lowers the number of colours until it does.
- A still of a lot takes `--scene card|chat|bids|seats|host` and `--at <seconds>|end`. `--lens off` hides the hole cards.
- The replay page has a Chinese interface only, so `--lang` only sets the locale of the browser.
- `--help` lists every option.
