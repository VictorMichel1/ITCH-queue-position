"""
Figures for the README, drawn from results/queue_results.json.

Run:  python src/plots.py                      real-data results
      python src/plots.py --dir results/synthetic
"""

import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent.parent

BLUE, ORANGE, GREEN, GREY = "tab:blue", "tab:orange", "tab:green", "tab:gray"
INK, INK2 = "black", "dimgray"

plt.rcParams.update({"axes.spines.top": False, "axes.spines.right": False,
                     "axes.grid": True, "grid.alpha": 0.3, "axes.axisbelow": True})


def estimator_bias(rep, out):
    syms = list(rep["symbols"])
    fig, axes = plt.subplots(1, len(syms), figsize=(3.0 * len(syms) + 0.6, 3.6))
    if len(syms) == 1:
        axes = [axes]
    names = [("pessimistic", ORANGE), ("optimistic", BLUE), ("proportional", GREEN)]
    for ax, sym in zip(axes, syms):
        e = rep["symbols"][sym]["estimator_error"]
        vals = [e[n]["mean_bias_shares"] for n, _ in names]
        bars = ax.bar(range(3), vals, color=[c for _, c in names], width=0.62)
        for b, v in zip(bars, vals):
            ax.annotate(f"{v:+.0f}", (b.get_x() + b.get_width() / 2, v),
                        xytext=(0, 3 if v >= 0 else -11), textcoords="offset points",
                        ha="center", fontsize=8.5, color=INK2)
        ax.axhline(0, color=INK2, linewidth=0.8)
        ax.set_xticks(range(3), ["pess.", "opt.", "prop."])
        ax.set_title(sym)
        lo, hi = min(vals + [0]), max(vals + [0])
        pad = 0.18 * (hi - lo or 1)
        ax.set_ylim(lo - pad, hi + pad)
    axes[0].set_ylabel("mean error vs true queue position, shares")
    fig.suptitle("Queue estimators against ground truth: + means it thinks you are further back",
                 fontsize=10.5, color=INK, x=0.02, ha="left")
    fig.tight_layout()
    fig.savefig(out / "estimator_bias.png", dpi=150)
    plt.close(fig)


def cross_section(rep, out):
    rows = []
    for sym, r in rep["symbols"].items():
        t = r["touch"]
        if t:
            rows.append((t["tick_bp"], r["fill_rate_strict"], r["fill_rate_upper"], sym))
    rows.sort()
    x = [r[0] for r in rows]
    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    for tb, lo, hi, _ in rows:
        ax.plot([tb, tb], [100 * lo, 100 * hi], color=GREY, linewidth=2, zorder=1)
    ax.plot(x, [100 * r[1] for r in rows], marker="o", markersize=7, color=BLUE,
            label="strict: an order that joined after us traded")
    ax.plot(x, [100 * r[2] for r in rows], marker="o", markersize=7, color=ORANGE,
            label="upper: also counting levels that emptied on a trade")
    for tb, lo, hi, sym in rows:
        ax.annotate(sym, (tb, 100 * hi), xytext=(0, 8), textcoords="offset points",
                    ha="center", fontsize=9, color=INK)
    ax.set_xlabel("tick size relative to price, basis points")
    ax.set_ylabel("share of hypothetical orders filled, %")
    ax.set_title("Fill rate against relative tick size, these four stocks", loc="left")
    ax.set_ylim(0, max(100 * r[2] for r in rows) * 1.3)
    ax.legend(loc="upper left")
    fig.tight_layout()
    fig.savefig(out / "fill_rate_vs_tick.png", dpi=150)
    plt.close(fig)


def adverse_selection(rep, out):
    """Per strict fill: half spread captured, mid move over the next second, and
    their sum, the 1 second markout. Error bars are 95% intervals from the
    standard errors clustered by minute."""
    rows = []
    for s, r in rep["symbols"].items():
        f = r["markouts_ticks"]["filled"]
        if f["n"] >= 20 and f["markout_1s"][0] is not None:
            rows.append((s, f))
    if not rows:
        return
    fig, ax = plt.subplots(figsize=(7.2, 4.4))
    w = 0.26
    series = [("edge", GREY, "half spread captured, against the pre-trade mid"),
              ("move_1s", ORANGE, "mid move over the next second"),
              ("markout_1s", BLUE, "markout after 1 second, the sum of the two")]
    for j, (key, col, lab) in enumerate(series):
        xs = [i + (j - 1) * (w + 0.02) for i in range(len(rows))]
        ys = [f[key][0] for _, f in rows]
        err = [1.96 * (f[key][1] or 0.0) for _, f in rows]
        ax.bar(xs, ys, width=w, color=col, label=lab)
        if key != "edge":
            ax.errorbar(xs, ys, yerr=err, fmt="none", ecolor=INK, elinewidth=1, capsize=3)
        for x, y, e in zip(xs, ys, err):
            off = (e if key != "edge" else 0) + 0.04
            ax.annotate(f"{y:+.2f}", (x, y + (off if y >= 0 else -off)),
                        ha="center", va="bottom" if y >= 0 else "top",
                        fontsize=8, color=INK2)
    ax.axhline(0, color=INK2, linewidth=0.8)
    ax.set_xticks(range(len(rows)), [f"{s}\n{f['n']} fills" for s, f in rows])
    ax.set_ylabel("ticks per fill, + in the quoter's favour")
    ax.set_title("Per passive fill: half spread earned, then the next second's mid move", loc="left")
    lo = min(f["move_1s"][0] - 1.96 * (f["move_1s"][1] or 0) for _, f in rows)
    hi = max(f["edge"][0] for _, f in rows)
    ax.set_ylim(lo * 1.35, hi * 1.9)
    ax.legend(loc="upper left", ncol=1)
    fig.tight_layout()
    fig.savefig(out / "adverse_selection.png", dpi=150)
    plt.close(fig)


def book_snapshot(rep, out, sym):
    snap = rep["symbols"][sym].get("book_at_1030")
    if not snap:
        return
    bids, asks = snap["bids"], snap["asks"]
    fig, ax = plt.subplots(figsize=(7.2, 4.6))
    prices = [p / 10000 for p, _ in asks[::-1]] + [p / 10000 for p, _ in bids]
    sizes = [q for _, q in asks[::-1]] + [q for _, q in bids]
    colours = [ORANGE] * len(asks) + [BLUE] * len(bids)
    ys = range(len(prices))
    ax.barh(list(ys), sizes, color=colours, height=0.72)
    ax.set_yticks(list(ys), [f"{p:.2f}" for p in prices])
    ax.invert_yaxis()
    ax.set_xlabel("resting shares")
    ax.set_title(f"{sym} order book reconstructed from ITCH at {snap['clock']}", loc="left")
    ax.grid(axis="y", visible=False)
    ax.text(0.98, 0.03, "asks above, bids below", transform=ax.transAxes,
            ha="right", color=INK2, fontsize=8.5)
    fig.tight_layout()
    fig.savefig(out / "book_snapshot.png", dpi=150)
    plt.close(fig)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(ROOT / "results"))
    a = ap.parse_args()
    out = Path(a.dir)
    rep = json.loads((out / "queue_results.json").read_text())
    estimator_bias(rep, out)
    cross_section(rep, out)
    adverse_selection(rep, out)
    book_snapshot(rep, out, list(rep["symbols"])[0])
    print("figures written to", out)
