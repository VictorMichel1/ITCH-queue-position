"""
Three checks on the real file that the study's rules lean on.

1. One timestamp per aggressor. The sweep rule treats a trade at a worse price
   in the same nanosecond as the last trade at our price as the same incoming
   order. If Nasdaq stamped each level a few nanoseconds apart, that would miss
   real sweeps.
2. Queue order. How often an execution reaches an order that isn't at the
   front of its queue in my rebuild, split by whether it traded at the price
   the order displayed (E, or C at the same price) or somewhere else (C).
3. Trading state. Halts and crosses for these four stocks.

Run:  python src/checks.py      (real data, about two minutes)
Writes results/checks.txt.
"""

import sys
from collections import Counter
from pathlib import Path

import itch
from book import Book, BUY

ROOT = Path(__file__).resolve().parent.parent
NS = 1_000_000_000
START_NS = 9 * 3600 * NS + 32 * 60 * NS      # same start as the study's orders
GAP_BINS = [(1, "same nanosecond"), (1_000, "1 to 999 ns"), (1_000_000, "1 to 999 us"),
            (None, "1 ms or more")]


def _gap_bin(gap):
    for hi, name in GAP_BINS:
        if hi is None or gap < hi:
            return name


def _clock(ns):
    s = ns // NS
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def main(path, symbols):
    loc = itch.stock_directory(path)
    want = {loc[s]: s for s in symbols if s in loc}
    books = {l: Book() for l in want}
    last = {}
    gaps = Counter()
    q = {s: Counter() for s in want.values()}
    states, crosses = [], Counter()

    # One raw pass, because the halt (H) and cross (Q) messages aren't part
    # of what itch.stream yields.
    buf = b""
    for block in itch._chunks(path):
        buf = buf + block if buf else block
        i, n = 0, len(buf)
        while i + 2 <= n:
            end = i + 2 + ((buf[i] << 8) | buf[i + 1])
            if end > n:
                break
            t, p = buf[i + 2], i + 3
            locate = (buf[p] << 8) | buf[p + 1]
            i = end
            if locate not in want:
                continue
            sym = want[locate]
            if t == 0x48:                                   # H, trading action
                ts = int.from_bytes(buf[p + 4:p + 10], "big")
                states.append((sym, _clock(ts), chr(buf[p + 18])))
                continue
            if t == 0x51:                                   # Q, cross trade
                crosses[sym] += 1
                continue
            m = itch._parse(t, buf, p)
            if m is None:
                continue
            bk = books[locate]
            if type(m) is itch.Exec and m.ts >= START_NS:
                o = bk.orders.get(m.ref)
                if o is not None:
                    side, disp = o[0], o[1]
                    lv = (bk.bids if side == BUY else bk.asks)[disp]
                    skipped = next(iter(lv.orders)) != m.ref
                    if m.price is None or m.price == disp:
                        q[sym]["at display"] += 1
                        q[sym]["at display, skipped the front"] += skipped
                    else:
                        better = (m.price > disp) if side == BUY else (m.price < disp)
                        touch = disp == (bk.best_bid() if side == BUY else bk.best_ask())
                        q[sym]["at another price"] += 1
                        q[sym]["  better for the incoming order"] += better
                        q[sym]["  order displayed at the best price"] += touch
            rem = bk.apply(m)
            if rem is not None and rem.reason == "exec":
                key = (locate, rem.side)
                if key in last:
                    t0, p0 = last[key]
                    worse = rem.price < p0 if rem.side == BUY else rem.price > p0
                    if worse:
                        gaps[_gap_bin(m.ts - t0)] += 1
                last[key] = (m.ts, rem.price)
        buf = buf[i:]

    syms = list(want.values())
    out = ["1. executions at a worse level than the previous execution on the same",
           "   side, all four stocks, by time since that previous execution:"]
    out += [f"   {name:18s} {gaps[name]:7,d}" for _, name in GAP_BINS]
    out += ["", "2. executions from 09:32", f"   {'':36s}" + "".join(f"{s:>8s}" for s in syms)]
    for row in ("at display", "at display, skipped the front", "at another price",
                "  better for the incoming order", "  order displayed at the best price"):
        out.append(f"   {row:36s}" + "".join(f"{q[s][row]:8,d}" for s in syms))
    out.append(f"   {'share at display that skipped':36s}" + "".join(
        f"{q[s]['at display, skipped the front'] / max(1, q[s]['at display']):8.2%}"
        for s in syms))
    out += ["", "3. trading state messages (T = trading, H = halted, P/Q = paused/quoting)"]
    out += [f"   {s} {c} {st}" for s, c, st in states]
    out.append("   cross trades: " + ", ".join(f"{s} {crosses[s]}" for s in syms))
    text = "\n".join(out) + "\n"
    print(text)
    (ROOT / "results" / "checks.txt").write_text(text)


if __name__ == "__main__":
    path = ROOT / "data" / "itch_20200130_prefix.gz"
    if not path.exists():
        sys.exit(f"{path} not found. See data/README.md for the download.")
    main(str(path), ["INTC", "CSCO", "MSFT", "AAPL"])
