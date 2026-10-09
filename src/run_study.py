"""
Replay ITCH, reconstruct the book order by order, post hypothetical passive
quotes at the touch, and measure what happens to them.

Three questions:
  1. how well do the L2 queue estimators track the truth
  2. how does fill probability depend on where you land in the queue
  3. what a passive fill is worth 1s and 10s later, marked against the mid

Real data:       python src/run_study.py --file data/itch_20200130_prefix.gz
Simulated data:  python src/run_study.py --synthetic
"""

import argparse
import csv
import io
import json
import math
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import itch
from book import Book, BUY
from queue_sim import Watch, FILLED, SWEPT, EMPTIED, OUTBID, ALONE, TIMEOUT

ROOT = Path(__file__).resolve().parent.parent
TICK = 100          # ITCH prices carry 4 implied decimals, so 1 cent is 100
NS = 1_000_000_000
OPEN_NS = 9 * 3600 * NS + 30 * 60 * NS
SNAPSHOT_NS = 10 * 3600 * NS + 30 * 60 * NS     # book ladder captured at 10:30
# Judgement calls, left untuned. 300 messages between placement attempts
# usually keeps one order per side live; an order still waiting after a minute
# is a timeout; fills are marked at 1s and 10s.
PLACE_EVERY = 300
HOLD_NS = 60 * NS
HORIZONS_NS = [1 * NS, 10 * NS]


class SymbolState:
    def __init__(self, sym, locate):
        self.sym = sym
        self.locate = locate
        self.book = Book()
        self.watches = []
        self.done = []
        self.events = 0
        self.last_ts = 0
        self.pending_as = []      # (watch, fill time) waiting for post-fill mids
        self.pending_sweep = []   # watches whose level emptied on an execution
        self.trades = 0
        self.touch = []           # (bid, ask, bid size, ask size) at each placement
        self.snapshot = None
        self.match = None         # (orders matching, generator's count, rebuilt count)


def run(path, symbols, outdir, place_every=PLACE_EVERY, hold_ns=HOLD_NS,
        horizons_ns=HORIZONS_NS, truth=None):
    t_start = time.time()
    print("reading stock directory ...", flush=True)
    locates = itch.stock_directory(path)
    print(f"  {len(locates)} symbols in directory")

    states = {}
    for s in symbols:
        if s not in locates:
            print(f"  ! {s} not found in directory, skipping")
            continue
        states[locates[s]] = SymbolState(s, locates[s])
    want = set(states)
    if not want:
        raise SystemExit("no target symbols resolved")

    print(f"replaying for {[st.sym for st in states.values()]} ...", flush=True)
    n = 0
    first_ts = None
    last_ts = 0
    scan = {}
    for m in itch.stream(path, locates=want, stats=scan):
        st = states[m.locate]
        n += 1

        ts = m.ts
        if first_ts is None and ts > 0:
            first_ts = ts
        last_ts = ts
        st.last_ts = ts
        st.events += 1

        if type(m) is itch.Trade:
            st.trades += 1
            _through(st, m.side, m.price, m.shares, ts, any_side=True)
            continue

        bk = st.book
        # level size before the removal, per watch: the bid and ask watches
        # sit on different levels and each needs its own
        pre = None
        mid_pre = None
        if st.watches:
            pre = {(w.side, w.price): bk.depth_at(w.side, w.price)
                   for w in st.watches}
            if type(m) is itch.Exec:
                mid_pre = bk.mid()      # only executions are marked against it

        rem = bk.apply(m)

        if rem is not None and rem.reason == "exec" and (st.watches or st.pending_sweep):
            _through(st, rem.side, rem.price, rem.shares, ts)
        if type(m) is itch.Add and st.pending_sweep:
            _posted(st, m, ts)
        if rem is not None and st.watches:
            _feed(st, rem, pre, mid_pre, ts)

        _settle_sweeps(st, ts)
        _expire(st, ts, hold_ns)
        _resolve_adverse(st, ts, horizons_ns)

        bk.sanity()

        if st.snapshot is None and ts >= SNAPSHOT_NS:
            bids, asks = bk.ladder(10)
            st.snapshot = {"clock": _clock(ts), "bids": bids, "asks": asks}

        if st.events % place_every == 0:
            _maybe_place(st, ts)

    for st in states.values():
        _settle_sweeps(st, None)
        for w in st.watches:
            w.close(TIMEOUT, st.last_ts)
            st.done.append(w)
        st.watches = []

    elapsed = time.time() - t_start
    scanned = scan.get("scanned", 0)

    # On the simulated session the generator's own book is the answer key.
    if truth:
        for st in states.values():
            want, got = truth[st.sym], st.book.resting()
            st.match = (sum(1 for ref, o in want.items() if got.get(ref) == o),
                        len(want), len(got))

    os.makedirs(outdir, exist_ok=True)
    report = _report(states, horizons_ns, n, scanned, elapsed, first_ts, last_ts)
    with open(os.path.join(outdir, "queue_results.json"), "w") as f:
        json.dump(report, f, indent=2)
    _write_watches(states, horizons_ns, os.path.join(outdir, "watches.csv"))

    text = _format_report(report)
    print(text)
    with open(os.path.join(outdir, "results.txt"), "w") as f:
        f.write(text)
    print(f"written to {outdir}")
    return report


def _feed(st, rem, pre, mid_pre, ts):
    keep = []
    for w in st.watches:
        if rem.side == w.side and rem.price == w.price:
            if rem.reason == "exec":
                if w.last_exec_ts != ts:
                    w.mid_pre = mid_pre
                w.last_exec_ts = ts
            if w.on_removal(rem, pre[(w.side, w.price)]):
                w.close(FILLED, ts)
                st.done.append(w)
                st.pending_as.append((w, ts))
                continue
        keep.append(w)
    st.watches = keep


def _posted(st, m, ts):
    """An add on the other side at our price, in the nanosecond that emptied
    our level, is the aggressor resting what it had left."""
    for w in st.pending_sweep:
        if m.side != w.side and m.price == w.price and w.last_exec_ts == ts:
            w.through_shares += m.shares


def _through(st, side, price, shares, ts, any_side=False):
    """Count shares executed beyond our price in the nanosecond that hit it.

    Nasdaq stamps every execution from one incoming order with the same
    nanosecond (results/checks.txt), so a trade at a worse price at the same
    timestamp as the last trade at our price is that aggressor carrying on.
    Hidden trades (P) count too whatever their side flag, since a worse price
    at that nanosecond only fits one direction.
    """
    for w in st.watches + st.pending_sweep:
        if (side != w.side and not any_side) or w.last_exec_ts != ts:
            continue
        worse = price < w.price if w.side == BUY else price > w.price
        if worse:
            w.through_shares += shares


def _settle_sweeps(st, ts):
    """Score a level that emptied on an execution once its nanosecond is over:
    a sweep if the aggressor visibly had at least our size left (traded beyond
    our price, or rested at it), otherwise emptied, which only counts toward
    the upper bound."""
    if not st.pending_sweep:
        return
    keep = []
    for w in st.pending_sweep:
        if ts is None or ts > w.last_exec_ts:
            w.close(SWEPT if w.through_shares >= w.size else EMPTIED, w.last_exec_ts)
            st.pending_as.append((w, w.last_exec_ts))
            st.done.append(w)
        else:
            keep.append(w)
    st.pending_sweep = keep


def _expire(st, ts, hold_ns):
    if not st.watches:
        return
    bb, ba = st.book.best_bid(), st.book.best_ask()
    keep = []
    for w in st.watches:
        out = None
        if w.side == BUY:
            if bb is None or bb < w.price:
                out = ALONE           # everyone else at our price is gone
            elif bb > w.price:
                out = OUTBID          # someone bid above us
        else:
            if ba is None or ba > w.price:
                out = ALONE
            elif ba < w.price:
                out = OUTBID

        # Our level emptied on an execution in this nanosecond and nobody
        # behind us exists for the fill rule to catch. Wait for the rest of the
        # nanosecond to see whether the aggressor traded through (_through).
        if out == ALONE and w.last_exec_ts == ts:
            st.pending_sweep.append(w)
            continue

        if out is None and ts - w.t0 > hold_ns:
            out = TIMEOUT
        if out is not None:
            w.close(out, ts)
            st.done.append(w)
        else:
            keep.append(w)
    st.watches = keep


def _resolve_adverse(st, ts, horizons_ns):
    """Stamp the mid at each horizon after a fill: the first mid at or past
    it, no interpolation. These books update many times a second."""
    if not st.pending_as:
        return
    mid = st.book.mid()
    if mid is None:
        return
    still = []
    for item in st.pending_as:
        w, t_fill = item
        age = ts - t_fill
        for h in horizons_ns:
            if age >= h and h not in w.marks:
                w.marks[h] = mid
        if age < horizons_ns[-1]:
            still.append(item)
    st.pending_as = still


def _maybe_place(st, ts):
    """Post one hypothetical order on each side at the touch, from 09:32 (two
    minutes clear of the opening cross) and only while the book is two-sided."""
    if len(st.watches) >= 2:
        return
    if ts < OPEN_NS + 120 * NS:
        return
    bk = st.book
    bb, ba, qb, qa = bk.bbo()
    if bb is None or qb == 0 or qa == 0 or bb >= ba:
        return
    if ba - bb > 5 * TICK:      # a very wide book is a different regime
        return
    st.touch.append((bb, ba, qb, qa))
    have = set(w.side for w in st.watches)
    for side, px, depth in ((BUY, bb, qb), (b"S", ba, qa)):
        if side in have:
            continue
        st.watches.append(Watch(side, px, 100, bk._seq + 1, ts, depth))


def _clock(ns):
    s = ns // NS
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


BUCKETS = [(200, "0-200"), (1000, "200-1k"), (5000, "1k-5k"), (20000, "5k-20k")]


def _bucket(depth):
    for hi, name in BUCKETS:
        if depth < hi:
            return name
    return "20k+"


def _sign(w):
    return 1.0 if w.side == BUY else -1.0


def _markout(w, h):
    """Mid at h after the fill against the fill price, in ticks.

    Positive means the fill made money before fees. It splits exactly into the
    half spread captured against the pre-trade mid and the move of the mid
    from there, which are _edge and _move.
    """
    mk = w.marks.get(h)
    if mk is None:
        return None
    return _sign(w) * (mk - w.price) / TICK


def _edge(w):
    if w.mid_pre is None:
        return None
    return _sign(w) * (w.mid_pre - w.price) / TICK


def _move(w, h):
    mk = w.marks.get(h)
    if mk is None or w.mid_pre is None:
        return None
    return _sign(w) * (mk - w.mid_pre) / TICK


def _mean_se(pairs):
    """Mean and a standard error clustered by minute of fill.

    The 10 second windows of fills close together overlap, so the moves are
    not independent and the plain sd/sqrt(n) would be too small. Summing
    residuals within each minute before squaring allows for any correlation
    inside a minute. pairs is a list of (fill time ns, value).
    """
    xs = [v for _, v in pairs]
    n = len(xs)
    if n == 0:
        return None, None
    mu = math.fsum(xs) / n
    clusters = defaultdict(float)
    for t, v in pairs:
        clusters[t // (60 * NS)] += v - mu
    g = len(clusters)
    if g < 2:
        return mu, None
    var = math.fsum(c * c for c in clusters.values()) / (n * n) * g / (g - 1)
    return mu, math.sqrt(var)


def _fill_stats(ws, horizons_ns):
    out = {"n": len(ws)}
    e = [(w.t_end, _edge(w)) for w in ws if _edge(w) is not None]
    out["edge"] = _mean_se(e)
    for h, name in zip(horizons_ns, ("1s", "10s")):
        mv = [(w.t_end, _move(w, h)) for w in ws if _move(w, h) is not None]
        mo = [(w.t_end, _markout(w, h)) for w in ws if _markout(w, h) is not None]
        out["move_" + name] = _mean_se(mv)
        out["markout_" + name] = _mean_se(mo)
        out["n_" + name] = len(mo)
    return {k: ([round(x, 4) if x is not None else None for x in v]
                if isinstance(v, tuple) else v) for k, v in out.items()}


def _report(states, horizons_ns, n_msgs, scanned, elapsed, first_ts, last_ts):
    out = {
        "messages_for_targets": n_msgs,
        "messages_scanned": scanned,
        "seconds": round(elapsed, 1),
        "scan_rate_msgs_per_sec": int(scanned / elapsed) if elapsed else 0,
        "clock_from": _clock(first_ts) if first_ts else None,
        "clock_to": _clock(last_ts) if last_ts else None,
        "symbols": {},
    }
    for st in states.values():
        ws = st.done
        if not ws:
            continue
        by_outcome = defaultdict(int)
        for w in ws:
            by_outcome[w.outcome] += 1

        # estimator error, measured only on watches that saw at least one cancel
        err = {"pessimistic": [], "optimistic": [], "proportional": []}
        for w in ws:
            if w.cancel_vol == 0:
                continue
            err["pessimistic"].append(w.ahead_pess - w.ahead_true)
            err["optimistic"].append(w.ahead_opt - w.ahead_true)
            err["proportional"].append(w.ahead_prop - w.ahead_true)
        est = {}
        for k, v in err.items():
            if v:
                est[k] = {
                    "n": len(v),
                    "mean_bias_shares": round(sum(v) / len(v), 1),
                    "mean_abs_error_shares": round(sum(abs(x) for x in v) / len(v), 1),
                }

        buckets = defaultdict(lambda: {"n": 0, "filled": 0, "upper": 0, "mo_1s": []})
        for w in ws:
            b = buckets[_bucket(w.depth0)]
            b["n"] += 1
            if w.outcome in (FILLED, SWEPT, EMPTIED):
                b["upper"] += 1
            if w.outcome == FILLED:
                b["filled"] += 1
                m = _markout(w, horizons_ns[0])
                if m is not None:
                    b["mo_1s"].append(m)

        bk_out = {}
        order = [name for _, name in BUCKETS] + ["20k+"]
        for name in order:
            v = buckets.get(name)
            if not v or v["n"] < 20:
                continue
            row = {
                "orders": v["n"],
                "fill_rate": round(v["filled"] / v["n"], 4),
                "fill_rate_upper": round(v["upper"] / v["n"], 4),
            }
            if len(v["mo_1s"]) >= 20:
                row["markout_1s_ticks"] = round(math.fsum(v["mo_1s"]) / len(v["mo_1s"]), 4)
                row["markout_1s_n"] = len(v["mo_1s"])
            bk_out[name] = row

        touch = {}
        if st.touch:
            k = len(st.touch)
            px = sum((b + a) / 2.0 for b, a, _, _ in st.touch) / k / 10000.0
            touch = {
                "samples": k,
                "mean_price": round(px, 2),
                "tick_bp": round(1e4 * 0.01 / px, 2),
                "mean_spread_ticks": round(sum((a - b) for b, a, _, _ in st.touch) / k / TICK, 2),
                "mean_touch_depth": round(sum((qb + qa) / 2.0 for _, _, qb, qa in st.touch) / k),
            }

        viol = sum(w.priority_violations for w in ws)
        out["symbols"][st.sym] = {
            "events": st.events,
            "hidden_trades": st.trades,
            "orders_posted": len(ws),
            "outcomes": dict(by_outcome),
            "fill_rate_strict": round(by_outcome[FILLED] / len(ws), 4),
            "fill_rate_with_sweeps": round((by_outcome[FILLED] + by_outcome[SWEPT]) / len(ws), 4),
            "fill_rate_upper": round((by_outcome[FILLED] + by_outcome[SWEPT]
                                      + by_outcome[EMPTIED]) / len(ws), 4),
            "price_time_violations": viol,
            "violation_rate": round(viol / max(1, len(ws)), 5),
            "violations_share_of_strict_fills": round(viol / max(1, by_outcome[FILLED]), 4),
            "strict_fills_under_size": sum(1 for w in ws if w.outcome == FILLED and w.fill_shares < w.size),
            "strict_fills_maybe_partial": round(sum(1 for w in ws if w.outcome == FILLED
                                                    and w.fill_shares < w.size)
                                                / max(1, by_outcome[FILLED]), 4),
            "crossed_book_events": st.book.crossed_events,
            "integrity_checks": st.book.checks,
            "matches_generator": st.match,
            "orphan_messages": st.book.orphan_events,
            "touch": touch,
            "markouts_ticks": {
                "filled": _fill_stats([w for w in ws if w.outcome == FILLED], horizons_ns),
                "swept": _fill_stats([w for w in ws if w.outcome == SWEPT], horizons_ns),
                "emptied": _fill_stats([w for w in ws if w.outcome == EMPTIED], horizons_ns),
            },
            "estimator_error": est,
            "by_initial_queue": bk_out,
            "book_at_1030": st.snapshot,
        }
    return out


def _write_watches(states, horizons_ns, path):
    with open(path, "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["symbol", "side", "t0_ns", "depth_at_join", "outcome",
                     "exec_volume", "cancel_volume", "err_pessimistic",
                     "err_optimistic", "err_proportional", "fill_price",
                     "mid_before_fill", "markout_1s_ticks", "markout_10s_ticks"])
        r4 = lambda x: "" if x is None else round(x, 4)
        for st in states.values():
            for w in st.done:
                filled = w.outcome in (FILLED, SWEPT, EMPTIED)
                wr.writerow([st.sym, w.side.decode(), w.t0, w.depth0, w.outcome,
                             w.exec_vol, w.cancel_vol,
                             w.ahead_pess - w.ahead_true,
                             w.ahead_opt - w.ahead_true,
                             round(w.ahead_prop - w.ahead_true, 1),
                             w.price / 10000 if filled else "",
                             w.mid_pre / 10000 if filled and w.mid_pre else "",
                             r4(_markout(w, horizons_ns[0])) if filled else "",
                             r4(_markout(w, horizons_ns[1])) if filled else ""])


def _format_report(rep):
    o = io.StringIO()
    p = lambda *a: print(*a, file=o)
    p("=" * 74)
    p("QUEUE POSITION STUDY")
    p("=" * 74)
    p(f"{rep['messages_for_targets']:,} messages for the target symbols, "
      f"{rep['messages_scanned']:,} scanned in total, {rep['seconds']}s")
    p(f"exchange clock {rep['clock_from']} to {rep['clock_to']}")
    for sym, r in rep["symbols"].items():
        p(f"\n{sym}   {r['events']:,} events   {r['orders_posted']} hypothetical orders")
        t = r["touch"]
        if t:
            p(f"  price {t['mean_price']:.2f}   tick {t['tick_bp']:.2f} bp   "
              f"spread {t['mean_spread_ticks']:.2f} ticks   "
              f"touch depth {t['mean_touch_depth']:,} shares")
        p(f"  outcomes: {r['outcomes']}")
        p(f"  fill rate: {r['fill_rate_strict']:.1%} strict, "
          f"{r['fill_rate_with_sweeps']:.1%} with confirmed sweeps, "
          f"{r['fill_rate_upper']:.1%} if emptied levels count")
        p(f"  price-time violations: {r['price_time_violations']} "
          f"({r['violation_rate']:.3%} of orders, "
          f"{r['violations_share_of_strict_fills']:.1%} of strict fills)")
        p(f"  strict fills where the first trade behind us was under our size: "
          f"{r['strict_fills_under_size']}")
        p(f"  book integrity: crossed={r['crossed_book_events']} in "
          f"{r['integrity_checks']:,} checks, orphans={r['orphan_messages']}")
        if r["matches_generator"]:
            ok, n_want, n_got = r["matches_generator"]
            p(f"  rebuilt book against the generator's at the end: {ok} of {n_want} resting "
              f"orders identical, {n_got} in the rebuilt book")
        mk = r["markouts_ticks"]
        if mk["filled"]["n"] >= 2:
            p("  per fill, in ticks, + is in our favour (standard errors clustered by minute):")
            p(f"    {'':8s} {'n':>5s} {'half spread':>13s} {'mid move 1s':>13s} "
              f"{'markout 1s':>13s} {'markout 10s':>13s}")
            for kind in ("filled", "swept", "emptied"):
                f = mk[kind]
                if f["n"] < 2:
                    continue
                cell = lambda ms: ("-" if ms[0] is None else
                                   f"{ms[0]:+.3f} ({ms[1]:.3f})" if ms[1] is not None
                                   else f"{ms[0]:+.3f}")
                p(f"    {kind:8s} {f['n']:5d} {cell(f['edge']):>13s} {cell(f['move_1s']):>13s} "
                  f"{cell(f['markout_1s']):>13s} {cell(f['markout_10s']):>13s}")
        if r["estimator_error"]:
            p("  queue estimator error in shares (+ means it thinks you are further back):")
            for k, v in r["estimator_error"].items():
                p(f"    {k:14s} bias {v['mean_bias_shares']:+8.1f}   "
                  f"mae {v['mean_abs_error_shares']:8.1f}   n={v['n']}")
        if r["by_initial_queue"]:
            p("  by shares ahead at join:")
            p(f"    {'bucket':10s} {'orders':>7s} {'fill':>7s} {'upper':>8s} "
              f"{'markout 1s':>11s}")
            for b, v in r["by_initial_queue"].items():
                a1 = f"{v['markout_1s_ticks']:+.3f}" if "markout_1s_ticks" in v else "-"
                p(f"    {b:10s} {v['orders']:7d} {v['fill_rate']:6.1%} "
                  f"{v['fill_rate_upper']:7.1%} {a1:>11s}")

    syms = [s for s, r in rep["symbols"].items() if r["touch"]]
    mk1 = lambda r: r["markouts_ticks"]["filled"]["markout_1s"]
    bias = lambda r, k: r["estimator_error"].get(k, {}).get("mean_bias_shares")
    rows = [
        ("tick, bp of price", "{:.2f}", lambda r: r["touch"]["tick_bp"]),
        ("hypothetical orders", "{}", lambda r: r["orders_posted"]),
        ("filled, strict", "{:.1%}", lambda r: r["fill_rate_strict"]),
        ("filled, upper bound", "{:.1%}", lambda r: r["fill_rate_upper"]),
        ("strict fills maybe partial", "{:.0%}", lambda r: r["strict_fills_maybe_partial"]),
        ("markout after 1s, ticks", "{:+.3f}", lambda r: mk1(r)[0]),
        ("  standard error", "{:.3f}", lambda r: mk1(r)[1]),
        ("pessimistic bias, shares", "{:+.1f}", lambda r: bias(r, "pessimistic")),
        ("proportional bias, shares", "{:+.1f}", lambda r: bias(r, "proportional")),
    ]
    p("\n" + f"{'summary':26s}" + "".join(f"{s:>9s}" for s in syms))
    for label, spec, get in rows:
        vals = [get(rep["symbols"][s]) for s in syms]
        p(f"{label:26s}" + "".join(f"{'-' if v is None else spec.format(v):>9s}" for v in vals))
    return o.getvalue()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(ROOT / "data" / "itch_20200130_prefix.gz"))
    ap.add_argument("--symbols", nargs="+", default=["INTC", "CSCO", "MSFT", "AAPL"])
    ap.add_argument("--synthetic", action="store_true",
                    help="generate and replay a simulated session (no download needed)")
    a = ap.parse_args()

    if a.synthetic:
        import synth
        path = ROOT / "data" / "synthetic_itch.gz"
        truth = synth.generate(str(path)).truth()
        a.file = str(path)
        a.symbols = list(synth.SYMBOLS)
        out = str(ROOT / "results" / "synthetic")
    else:
        out = str(ROOT / "results")
        truth = None
        if not os.path.exists(a.file):
            sys.exit(f"{a.file} not found. See data/README.md for the download, "
                     f"or run with --synthetic.")

    run(a.file, a.symbols, out, truth=truth)
