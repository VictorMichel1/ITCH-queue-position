"""
A simulated trading session written out as real TotalView-ITCH 5.0 binary.

It lets `run_study.py --synthetic` run without the 2.7 GB download, and it is
an answer key: the generator keeps its own book while it writes, so the
rebuilt book has to match it order for order, and since it enforces strict
price-time priority the study's violation counter has to read zero. Both are
in results/synthetic/results.txt.

The flow is simple on purpose. A fair value V nobody sees follows a random
walk. Limit orders arrive a geometric number of ticks from V. Cancels lean
toward recent arrivals. Market orders take liquidity in price-time order, and
a share of them trade toward V, which is what picks off resting quotes.
Replaces move an order by up to a tick and send it to the back. Hidden trades
print without touching the book. The cancel rate scales with book size so the
book doesn't drift to empty.

SIMA is a $20 large-tick name with deep queues and a pinned spread; SIMB is a
$300 small-tick name with thin queues. Whatever comes out describes these
parameters only.
"""

import gzip
import math
import struct
from collections import deque

import numpy as np

SYMBOLS = ("SIMA", "SIMB")

PROFILES = {
    "SIMA": dict(price=20.00, vol_ticks=0.035, add_k_p=0.65, lot_mean=4.0,
                 mkt_lot_mean=3.0, informed=0.35, target_orders=260),
    "SIMB": dict(price=300.00, vol_ticks=0.30, add_k_p=0.35, lot_mean=1.6,
                 mkt_lot_mean=1.5, informed=0.35, target_orders=140),
}

# event mix before the cancel rate is scaled by book size
W_ADD, W_CANCEL, W_MKT, W_REPLACE, W_HIDDEN = 0.50, 0.36, 0.09, 0.03, 0.02

NS = 1_000_000_000
OPEN_NS = 9 * 3600 * NS + 30 * 60 * NS

_S = struct.Struct(">cHH6sc")
_R = struct.Struct(">cHH6s8sccIcc2scccccIc")
_A = struct.Struct(">cHH6sQcI8sI")
_F = struct.Struct(">cHH6sQcI8sI4s")
_E = struct.Struct(">cHH6sQIQ")
_C = struct.Struct(">cHH6sQIQcI")
_X = struct.Struct(">cHH6sQI")
_D = struct.Struct(">cHH6sQ")
_U = struct.Struct(">cHH6sQQII")
_P = struct.Struct(">cHH6sQcI8sIQ")


def _t6(ns):
    return ns.to_bytes(6, "big")


def _frame(body):
    return len(body).to_bytes(2, "big") + body


def enc_system(ts, code):
    return _frame(_S.pack(b"S", 0, 0, _t6(ts), code))


def enc_directory(ts, locate, stock):
    return _frame(_R.pack(b"R", locate, 0, _t6(ts), stock.ljust(8).encode(),
                          b"Q", b"N", 100, b"N", b"C", b"  ", b"P", b"N",
                          b" ", b"1", b"N", 0, b"N"))


def enc_add(ts, locate, ref, side, shares, stock, price, mpid=None):
    if mpid:
        return _frame(_F.pack(b"F", locate, 0, _t6(ts), ref, side, shares,
                              stock.ljust(8).encode(), price, mpid))
    return _frame(_A.pack(b"A", locate, 0, _t6(ts), ref, side, shares,
                          stock.ljust(8).encode(), price))


def enc_exec(ts, locate, ref, shares, match, price=None):
    if price is not None:
        return _frame(_C.pack(b"C", locate, 0, _t6(ts), ref, shares, match, b"Y", price))
    return _frame(_E.pack(b"E", locate, 0, _t6(ts), ref, shares, match))


def enc_cancel(ts, locate, ref, shares):
    return _frame(_X.pack(b"X", locate, 0, _t6(ts), ref, shares))


def enc_delete(ts, locate, ref):
    return _frame(_D.pack(b"D", locate, 0, _t6(ts), ref))


def enc_replace(ts, locate, old, new, shares, price):
    return _frame(_U.pack(b"U", locate, 0, _t6(ts), old, new, shares, price))


def enc_trade(ts, locate, side, shares, stock, price, match):
    return _frame(_P.pack(b"P", locate, 0, _t6(ts), 0, side, shares,
                          stock.ljust(8).encode(), price, match))


class _Sym:
    """One instrument's ground-truth book and the state needed to drive it."""

    def __init__(self, name, locate, prof):
        self.name = name
        self.locate = locate
        self.p = prof
        self.v = prof["price"] * 100.0          # fair value in ticks (cents)
        self.bids = {}                           # price in ticks -> deque of refs
        self.asks = {}
        self.orders = {}                         # ref -> [side, price_ticks, shares]
        self.recent = []                         # refs in arrival order, lazily pruned

    def best(self, side):
        book = self.bids if side == b"B" else self.asks
        if not book:
            return None
        return max(book) if side == b"B" else min(book)

    def mid(self):
        b, a = self.best(b"B"), self.best(b"S")
        if b is None or a is None:
            return self.v
        return 0.5 * (a + b)


class Generator:
    def __init__(self, seed=7, symbols=SYMBOLS):
        self.rng = np.random.default_rng(seed)
        self.syms = [_Sym(s, i + 1, PROFILES[s]) for i, s in enumerate(symbols)]
        self.ref = 1000
        self.match = 1
        self.out = []
        self.counts = {}

    def _emit(self, kind, msg):
        self.out.append(msg)
        self.counts[kind] = self.counts.get(kind, 0) + 1

    def _new_ref(self):
        self.ref += 1
        return self.ref

    def _lots(self, mean):
        """Order size in round lots, geometric with the given mean in lots."""
        if mean <= 1:
            return 100
        return 100 * int(self.rng.geometric(1.0 / mean))

    def _rest(self, s, ts, side, price, shares):
        ref = self._new_ref()
        book = s.bids if side == b"B" else s.asks
        book.setdefault(price, deque()).append(ref)
        s.orders[ref] = [side, price, shares]
        s.recent.append(ref)
        mpid = b"SIMM" if self.rng.random() < 0.1 else None
        self._emit("A", enc_add(ts, s.locate, ref, side, shares, s.name, price * 100, mpid))
        return ref

    def _remove(self, s, ref):
        side, price, _ = s.orders.pop(ref)
        book = s.bids if side == b"B" else s.asks
        q = book[price]
        q.remove(ref)
        if not q:
            del book[price]

    def _take(self, s, ts, aggressor, shares, limit=None):
        """Execute `shares` against the opposite side in strict price-time
        order, optionally only at prices no worse than `limit`. Returns what
        is left unfilled."""
        book = s.asks if aggressor == b"B" else s.bids
        while shares > 0 and book:
            px = min(book) if aggressor == b"B" else max(book)
            if limit is not None:
                if aggressor == b"B" and px > limit:
                    break
                if aggressor == b"S" and px < limit:
                    break
            q = book[px]
            ref = q[0]
            o = s.orders[ref]
            take = min(shares, o[2])
            self.match += 1
            if self.rng.random() < 0.05:
                self._emit("C", enc_exec(ts, s.locate, ref, take, self.match, px * 100))
            else:
                self._emit("E", enc_exec(ts, s.locate, ref, take, self.match))
            o[2] -= take
            shares -= take
            if o[2] == 0:
                q.popleft()
                del s.orders[ref]
                if not q:
                    del book[px]
        return shares

    def _pick(self, s, recent_bias):
        """A resting order to cancel or replace. Half the time weighted toward
        the most recent arrivals, half the time uniform over the book."""
        if not s.orders:
            return None
        if recent_bias and self.rng.random() < 0.5:
            while s.recent:
                back = int(self.rng.exponential(25.0))
                i = len(s.recent) - 1 - min(back, len(s.recent) - 1)
                ref = s.recent[i]
                if ref in s.orders:
                    return ref
                s.recent.pop(i)
            return None
        if len(s.recent) > 4 * len(s.orders) + 64:
            s.recent = [r for r in s.recent if r in s.orders]
        for _ in range(8):
            ref = s.recent[int(self.rng.integers(len(s.recent)))]
            if ref in s.orders:
                return ref
        return next(iter(s.orders))

    def _add(self, s, ts):
        side = b"B" if self.rng.random() < 0.5 else b"S"
        k = int(self.rng.geometric(s.p["add_k_p"])) - 1
        # bids at or below the tick under V, asks strictly above it, so a fresh
        # quote never sits on the wrong side of fair value
        if side == b"B":
            price = math.floor(s.v) - k
        else:
            price = math.floor(s.v) + 1 + k
        shares = self._lots(s.p["lot_mean"])
        opp = s.best(b"S" if side == b"B" else b"B")
        crosses = opp is not None and (price >= opp if side == b"B" else price <= opp)
        if crosses:
            # a marketable limit: executes first, and only the remainder rests
            shares = self._take(s, ts, side, shares, limit=price)
        if shares > 0:
            self._rest(s, ts, side, price, shares)

    def _cancel(self, s, ts):
        ref = self._pick(s, recent_bias=True)
        if ref is None:
            return
        o = s.orders[ref]
        if o[2] > 100 and self.rng.random() < 0.25:
            cut = 100 * int(self.rng.integers(1, o[2] // 100))
            o[2] -= cut
            self._emit("X", enc_cancel(ts, s.locate, ref, cut))
        else:
            self._remove(s, ref)
            self._emit("D", enc_delete(ts, s.locate, ref))

    def _market(self, s, ts):
        if self.rng.random() < s.p["informed"]:
            side = b"B" if s.v > s.mid() else b"S"
        else:
            side = b"B" if self.rng.random() < 0.5 else b"S"
        self._take(s, ts, side, self._lots(s.p["mkt_lot_mean"]))

    def _replace(self, s, ts):
        ref = self._pick(s, recent_bias=False)
        if ref is None:
            return
        side, price, _ = s.orders[ref]
        step = 1 if s.v > price else -1
        new_price = price + step * int(self.rng.integers(0, 2))
        opp = s.best(b"S" if side == b"B" else b"B")
        if opp is not None and (new_price >= opp if side == b"B" else new_price <= opp):
            return
        shares = self._lots(s.p["lot_mean"])
        self._remove(s, ref)
        new = self._new_ref()
        book = s.bids if side == b"B" else s.asks
        book.setdefault(new_price, deque()).append(new)
        s.orders[new] = [side, new_price, shares]
        s.recent.append(new)
        self._emit("U", enc_replace(ts, s.locate, ref, new, shares, new_price * 100))

    def _hidden(self, s, ts):
        self.match += 1
        side = b"B" if self.rng.random() < 0.5 else b"S"
        self._emit("P", enc_trade(ts, s.locate, side, 100, s.name,
                                  int(round(s.mid())) * 100, self.match))

    def run(self, duration_s=5400, rate=45.0):
        t = 3 * 3600 * NS
        self._emit("S", enc_system(t, b"O"))
        for s in self.syms:
            self._emit("R", enc_directory(t + s.locate, s.locate, s.name))
        self._emit("S", enc_system(OPEN_NS, b"Q"))

        ts = OPEN_NS
        for s in self.syms:
            for lvl in range(5):
                for _ in range(3):
                    self._rest(s, ts, b"B", math.floor(s.v) - lvl, self._lots(s.p["lot_mean"]))
                    self._rest(s, ts, b"S", math.floor(s.v) + 1 + lvl, self._lots(s.p["lot_mean"]))

        end = OPEN_NS + int(duration_s * NS)
        n_sym = len(self.syms)
        while True:
            ts += int(self.rng.exponential(1.0 / (rate * n_sym)) * NS) + 1
            if ts >= end:
                break
            s = self.syms[int(self.rng.integers(n_sym))]
            s.v += self.rng.normal(0.0, s.p["vol_ticks"])
            w_cancel = W_CANCEL * len(s.orders) / s.p["target_orders"]
            tot = W_ADD + w_cancel + W_MKT + W_REPLACE + W_HIDDEN
            u = self.rng.random() * tot
            if u < W_ADD:
                self._add(s, ts)
            elif u < W_ADD + w_cancel:
                self._cancel(s, ts)
            elif u < W_ADD + w_cancel + W_MKT:
                self._market(s, ts)
            elif u < W_ADD + w_cancel + W_MKT + W_REPLACE:
                self._replace(s, ts)
            else:
                self._hidden(s, ts)
        self._emit("S", enc_system(end, b"M"))
        return self

    def truth(self):
        """Every resting order at the end, keyed by symbol, in the same shape
        Book.resting() returns: ref -> (side, price in ITCH units, shares)."""
        return {s.name: {ref: (o[0], o[1] * 100, o[2]) for ref, o in s.orders.items()}
                for s in self.syms}

    def write(self, path):
        with gzip.open(path, "wb", compresslevel=6) as f:
            f.write(b"".join(self.out))


def generate(path, seed=7, duration_s=5400, rate=45.0, symbols=SYMBOLS, quiet=False):
    g = Generator(seed, symbols).run(duration_s, rate)
    g.write(path)
    if not quiet:
        total = sum(g.counts.values())
        print(f"simulated {duration_s / 60:.0f} minutes, {total:,} ITCH messages "
              f"-> {path}")
        print("  " + "  ".join(f"{k}:{v:,}" for k, v in sorted(g.counts.items())))
    return g


if __name__ == "__main__":
    import argparse
    from pathlib import Path
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent
                                         / "data" / "synthetic_itch.gz"))
    a = ap.parse_args()
    generate(a.out)
