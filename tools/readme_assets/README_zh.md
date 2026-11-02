<p align="center"><a href="README.md">English</a> · <b>简体中文</b></p>

# README 配图

`docs/assets/` 里两份 README 和 [docs/how-it-works_zh.md](../../docs/how-it-works_zh.md) 展示的配图，由本文件夹里的脚本生成。脚本需要 Node 22、ffmpeg 和一个无头 Chromium（“headless shell”版本），不给项目增加任何包。

```
export CHROME_BIN=/path/to/chrome-headless-shell
```

浏览器以 `--incognito` 启动，它的配置目录和临时文件放在 `--tmp` 指定的目录里（默认是系统的临时目录）。中文文字使用 Noto Sans CJK SC，所以要安装这个字体。ffmpeg 取自 `PATH`，或取自变量 `FFMPEG`。

## 插图

`figures/` 里每张图一个 HTML 文件：`banner`、`night`、`formats` 和 `architecture`。每张图是一张圆角卡片。`figures.css` 把颜色定义成浅色和深色主题的变量。每段文字都写了两遍，分别是 `<span class="en">` 和 `<span class="zh">`，所以一份源文件能得到四张图。`connect.js` 画出方框之间的箭头。

```
node tools/readme_assets/build.mjs
```

它按两倍的像素比写出 `docs/assets/<figure>-<en|zh>-<light|dark>.png`。`--only formats`、`--lang zh`、`--theme dark`、`--out DIR` 和 `--tmp DIR` 用来缩小范围或改变输出位置，超过 400 KiB 的 PNG 会报错。要在浏览器里看一张图，打开 `figures/night.html?theme=dark&lang=zh`。

## 回放页的配图

`capture_replay.mjs` 在 127.0.0.1 上启动一个文件服务器（端口由系统选定），服务器提供 `viewer/` 和回放文件。它通过 Chrome DevTools Protocol 驱动浏览器里的回放页，并截图。生成 GIF 时，它按分镜脚本走（拍品卡、几句聊天、出价和落槌、主持人），再让 ffmpeg 根据这些帧的调色板制作 GIF。页面处于暂停状态，由它自己的进度条推动，所以同一份回放得到同样的帧。

回放页的配图是 README 里的拍品 GIF，以及 [docs/how-it-works_zh.md](../../docs/how-it-works_zh.md) 里的颁奖页和复盘页，每张都有浅色和深色两个主题：

```
R=docs/demo/trial-d01-r1.json    # 配图用的那一夜，auction-night export 导出的任何 replay.json 都可以
for t in light dark; do
  node tools/readme_assets/capture_replay.mjs --replay $R --lot 5 --lang zh --theme $t --out docs/assets/lot-$t.gif
  node tools/readme_assets/capture_replay.mjs --replay $R --view awards --lang zh --theme $t --height 582 --out docs/assets/awards-$t.png
  node tools/readme_assets/capture_replay.mjs --replay $R --view review --lang zh --theme $t --height 560 --out docs/assets/review-$t.png
done
```

要用真实模型的一夜，把 `R` 指向它的 `replay.json`，并用 `--lot` 选择拍品。

- `--lines N` 设置 GIF 显示几句聊天（默认 4）。`--fps` 和 `--width` 设置帧率（10）和宽度（960）。`--height` 设置每张图的高度（600）。GIF 的大小必须在 `--max-mb`（4）以内。脚本会不断减少颜色数，直到满足。
- 拍品的静态截图使用 `--scene card|chat|bids|seats|host` 和 `--at <seconds>|end`。`--lens off` 会隐藏底牌。
- 回放页只有中文界面，所以 `--lang` 只设置浏览器的区域设置。
- `--help` 列出全部选项。
