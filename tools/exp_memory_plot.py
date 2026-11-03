"""Draw the figure of the memory experiment from the summary that tools/exp_memory.py writes.

Two panels, one axis each: the share of won lots paid above the seat's own planned budget, and
the output tokens of a night. Every deck is a gray line from its night with memory off to its
night with memory on, and the means are the heavy marks. It needs matplotlib, which the project
does not install: run it with any Python that has it.

Usage: python3 tools/exp_memory_plot.py SUMMARY.json OUT.png [--lang en|zh]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager, ticker  # noqa: E402

SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
TEXT_2 = "#52514e"
GRID = "#e4e3df"
PAIR = "#b9b8b3"
OFF = "#2a78d6"
ON = "#eb6834"
CJK_FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"

TEXTS = {
    "en": {
        "off": "Memory off",
        "on": "Memory on",
        "a_title": "Won lots paid above the seat's plan",
        "a_unit": "share of won lots",
        "b_title": "Output tokens of a night",
        "b_unit": "tokens",
        "diff": "mean difference {d}, 95% interval {lo} to {hi}",
    },
    "zh": {
        "off": "无记忆",
        "on": "有记忆",
        "a_title": "拍下的件里，花钱超出自己计划的比例",
        "a_unit": "占拍下件数",
        "b_title": "一夜的输出 token",
        "b_unit": "token",
        "diff": "平均差值 {d}，95% 区间 {lo} 到 {hi}",
    },
}


def pairs(summary: dict, measure: str) -> list[tuple[float, float]]:
    by_deck: dict[str, dict[str, float]] = {}
    for night in summary["nights"]:
        m = night["measures"]
        if m is not None and m[measure] is not None:
            by_deck.setdefault(night["deck"], {})[night["condition"]] = float(m[measure])
    return [(v["off"], v["memory"]) for v in by_deck.values() if {"off", "memory"} <= set(v)]


def panel(ax, data, title, unit, note, labels, percent):
    scale = 100.0 if percent else 1.0
    for off, on in data:
        ax.plot([0, 1], [off * scale, on * scale], color=PAIR, linewidth=1, zorder=1)
    offs = [p[0] * scale for p in data]
    ons = [p[1] * scale for p in data]
    ax.scatter([0] * len(offs), offs, s=40, color=OFF, edgecolor=SURFACE, linewidth=1.5, zorder=2)
    ax.scatter([1] * len(ons), ons, s=40, color=ON, edgecolor=SURFACE, linewidth=1.5, zorder=2)
    mean_off, mean_on = sum(offs) / len(offs), sum(ons) / len(ons)
    ax.plot([0, 1], [mean_off, mean_on], color=TEXT, linewidth=2.5, zorder=3)
    ax.scatter(
        [0, 1], [mean_off, mean_on], s=110, color=[OFF, ON], edgecolor=TEXT, linewidth=1.5, zorder=4
    )
    ax.set_xticks([0, 1], labels)
    ax.set_xlim(-0.35, 1.35)
    ax.set_title(title, loc="left", fontsize=12.5, color=TEXT, pad=22)
    ax.text(0, 1.02, note, transform=ax.transAxes, fontsize=9.5, color=TEXT_2)
    ax.set_ylabel(unit, color=TEXT_2, fontsize=10)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=TEXT_2, length=0)
    if percent:
        ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{v:.0f}%"))
    else:
        ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("summary")
    parser.add_argument("out")
    parser.add_argument("--lang", choices=sorted(TEXTS), default="en")
    args = parser.parse_args(argv)
    summary = json.loads(Path(args.summary).read_text(encoding="utf-8"))
    t = TEXTS[args.lang]
    if args.lang == "zh":
        font_manager.fontManager.addfont(CJK_FONT)
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=CJK_FONT).get_name()
    diff = summary["differences"]

    def note(measure: str, percent: bool) -> str:
        d = diff[measure]
        if percent:
            unit = " pp" if args.lang == "en" else " 个百分点"
            low, high = (f"{v * 100:+.1f}" for v in d["interval_95"])
            return t["diff"].format(d=f"{d['mean_difference'] * 100:+.1f}{unit}", lo=low, hi=high)
        low, high = (f"{v:+,.0f}" for v in d["interval_95"])
        return t["diff"].format(d=f"{d['mean_difference']:+,.0f}", lo=low, hi=high)

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.6), dpi=160, facecolor=SURFACE)
    for ax in axes:
        ax.set_facecolor(SURFACE)
    labels = [t["off"], t["on"]]
    panel(
        axes[0],
        pairs(summary, "overspend_rate"),
        t["a_title"],
        t["a_unit"],
        note("overspend_rate", True),
        labels,
        True,
    )
    panel(
        axes[1],
        pairs(summary, "tokens_out"),
        t["b_title"],
        t["b_unit"],
        note("tokens_out", False),
        labels,
        False,
    )
    fig.tight_layout(w_pad=3)
    fig.savefig(args.out, facecolor=SURFACE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
