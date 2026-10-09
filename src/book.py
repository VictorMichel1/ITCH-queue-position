"""
Order-by-order book built from ITCH messages.

Every resting order is kept with a sequence number assigned on arrival. The
ITCH stream is in sequence, so inside a price level that number stands in for
time priority and the queue in front of any order is known. Aggregating to
price levels would throw away the one thing this project needs.
"""

from itch import Add, Exec, Cancel, Delete, Replace

BUY = b"B"
SELL = b"S"


class Level:
    __slots__ = ("shares", "orders")

    def __init__(self):
        self.shares = 0
        self.orders = {}  # ref -> [shares, seq]


class Removal:
    """What came off the book, and why. The queue tracker needs the seq to
    decide whether the removed order was ahead of or behind a watched order."""
    __slots__ = ("side", "price", "seq", "shares", "reason")

    def __init__(self, side, price, seq, shares, reason):
        self.side = side
        self.price = price
        self.seq = seq
        self.shares = shares
        self.reason = reason  # exec, exec_elsewhere, cancel, delete or replace


class Book:
    def __init__(self):
        self.orders = {}  # ref -> [side, price, shares, seq]
        self.bids = {}    # price -> Level
        self.asks = {}
        self._seq = 0
        self._bb = None   # cached best bid price
        self._ba = None
        self.crossed_events = 0
        self.checks = 0
        self.orphan_events = 0  # messages referencing orders we never saw

    def best_bid(self):
        if self._bb is None and self.bids:
            self._bb = max(self.bids)
        return self._bb

    def best_ask(self):
        if self._ba is None and self.asks:
            self._ba = min(self.asks)
        return self._ba

    def bbo(self):
        b, a = self.best_bid(), self.best_ask()
        if b is None or a is None:
            return None, None, 0, 0
        return b, a, self.bids[b].shares, self.asks[a].shares

    def depth_at(self, side, price):
        book = self.bids if side == BUY else self.asks
        lv = book.get(price)
        return lv.shares if lv else 0

    def mid(self):
        b, a = self.best_bid(), self.best_ask()
        if b is None or a is None:
            return None
        return (b + a) / 2.0

    def ladder(self, levels=10):
        """Top of book on each side as (price, shares) lists, best first."""
        bids = sorted(self.bids, reverse=True)[:levels]
        asks = sorted(self.asks)[:levels]
        return ([(p, self.bids[p].shares) for p in bids],
                [(p, self.asks[p].shares) for p in asks])

    def resting(self):
        """Every resting order as ref -> (side, price, shares), for comparing
        against the generator's own book after a synthetic session."""
        return {ref: (o[0], o[1], o[2]) for ref, o in self.orders.items()}

    def add(self, m):
        self._seq += 1
        book = self.bids if m.side == BUY else self.asks
        lv = book.get(m.price)
        if lv is None:
            lv = Level()
            book[m.price] = lv
            if m.side == BUY:
                if self._bb is not None and m.price > self._bb:
                    self._bb = m.price
            else:
                if self._ba is not None and m.price < self._ba:
                    self._ba = m.price
        lv.shares += m.shares
        lv.orders[m.ref] = [m.shares, self._seq]
        self.orders[m.ref] = [m.side, m.price, m.shares, self._seq]
        return None

    def _reduce(self, ref, shares, reason):
        o = self.orders.get(ref)
        if o is None:
            self.orphan_events += 1
            return None
        side, price, resting, seq = o
        taken = shares if shares < resting else resting
        book = self.bids if side == BUY else self.asks
        lv = book[price]
        lv.shares -= taken
        o[2] -= taken
        if o[2] <= 0:
            del self.orders[ref]
            del lv.orders[ref]
            if not lv.orders:
                del book[price]
                if side == BUY and price == self._bb:
                    self._bb = None
                elif side == SELL and price == self._ba:
                    self._ba = None
        else:
            lv.orders[ref][0] = o[2]
        return Removal(side, price, seq, taken, reason)

    def execute(self, m):
        # A C message can carry a price other than the one the order was
        # displayed at. On 30 January 2020 every such price after the open was
        # better for the incoming order (results/checks.txt): the resting order
        # was ranked at a price it didn't show. It leaves our queue without a
        # trade at our price, so it can't fill anyone behind it.
        o = self.orders.get(m.ref)
        if m.price is not None and o is not None and m.price != o[1]:
            return self._reduce(m.ref, m.shares, "exec_elsewhere")
        return self._reduce(m.ref, m.shares, "exec")

    def cancel(self, m):
        return self._reduce(m.ref, m.shares, "cancel")

    def delete(self, m):
        o = self.orders.get(m.ref)
        if o is None:
            self.orphan_events += 1
            return None
        return self._reduce(m.ref, o[2], "delete")

    def replace(self, m):
        """A replace is a cancel plus an add: new reference, back of the queue."""
        o = self.orders.get(m.old_ref)
        if o is None:
            self.orphan_events += 1
            return None
        side = o[0]
        rem = self._reduce(m.old_ref, o[2], "replace")
        self.add(Add(m.ts, m.locate, m.new_ref, side, m.shares, m.price))
        return rem

    def apply(self, m):
        """Dispatch. Returns a Removal or None so callers can watch the queue."""
        k = type(m)
        if k is Add:
            return self.add(m)
        if k is Exec:
            return self.execute(m)
        if k is Cancel:
            return self.cancel(m)
        if k is Delete:
            return self.delete(m)
        if k is Replace:
            return self.replace(m)
        return None  # Trade messages are hidden liquidity, book is untouched

    def sanity(self):
        """A crossed book means the rebuild is wrong. Called on every book
        event; the count of checks is reported next to the failures."""
        self.checks += 1
        b, a = self.best_bid(), self.best_ask()
        if b is not None and a is not None and b >= a:
            self.crossed_events += 1
            return False
        return True
