"""
Queue position for a hypothetical order, true and estimated.

Executions come off the front of the queue, so every share executed ahead of
you moves you up and nobody has to guess. Cancels are the hard part: a feed
that only shows total size at each price can't tell you whether a cancel was in
front of you or behind you. The three estimators are the usual guesses
(pessimistic: always behind, optimistic: always in front, proportional: spread
evenly). ITCH gives every order a reference, so the true position is known and
the guesses can be scored.

The order never rests in the book, so a fill is a counterfactual: an execution
that reaches an order which joined after us would have hit us first.
"""

FILLED = "filled"
SWEPT = "swept"            # one aggressor took our whole level and kept going
OUTBID = "outbid"          # someone improved the price, we are no longer at the touch
ALONE = "alone"            # everyone else at our price left; we'd be alone at the front
EMPTIED = "emptied"        # our level emptied on an execution, under our size traded beyond
TIMEOUT = "timeout"


class Watch:
    __slots__ = (
        "side", "price", "size", "entry_seq", "t0",
        "ahead_true", "ahead_pess", "ahead_opt", "ahead_prop",
        "depth0", "exec_vol", "cancel_vol",
        "priority_violations", "outcome", "t_end", "marks",
        "mid_pre", "last_exec_ts", "through_shares", "fill_shares",
    )

    def __init__(self, side, price, size, entry_seq, t0, depth):
        self.side = side
        self.price = price
        self.size = size
        self.entry_seq = entry_seq
        self.t0 = t0
        # when we join, everything resting at this price is ahead of us, and a
        # price-level feed shows the same number
        self.ahead_true = depth
        self.ahead_pess = depth
        self.ahead_opt = depth
        self.ahead_prop = float(depth)
        self.depth0 = depth
        self.exec_vol = 0
        self.cancel_vol = 0
        self.priority_violations = 0
        self.outcome = None
        self.t_end = None
        self.marks = {}      # horizon_ns -> mid observed that far after the fill
        # Set from the replay loop. mid_pre is the mid just before the first
        # execution at our price in the current nanosecond, so a fill is marked
        # against the book the aggressor saw. through_shares counts shares the
        # same aggressor took at worse prices, which is what confirms a sweep.
        self.mid_pre = None
        self.last_exec_ts = None
        self.through_shares = 0
        self.fill_shares = 0

    def on_removal(self, r, level_shares_before):
        """Apply one removal at our price level. Returns True if we filled."""
        ahead_of_us = r.seq < self.entry_seq

        if r.reason == "exec":
            self.exec_vol += r.shares
            if not ahead_of_us:
                # An order that joined after us traded at our price, so in the
                # counterfactual the trade is ours.
                if self.ahead_true > 0:
                    # ...but displayed size from before us was still resting,
                    # which strict priority doesn't allow. Kept as a check; it
                    # reads zero on the real data once Book.execute splits off
                    # executions at other prices.
                    self.priority_violations += 1
                self.ahead_true = 0
                self.fill_shares = min(r.shares, self.size)
                self.outcome = FILLED
                return True
            self.ahead_true -= r.shares
            self.ahead_pess -= r.shares
            # a price-level observer can't be less than zero shares back
            self.ahead_opt = max(0, self.ahead_opt - r.shares)
            self.ahead_prop = max(0.0, self.ahead_prop - r.shares)
        else:
            # cancels, deletes, replaces, and executions at another price,
            # which a price-level feed also sees only as size leaving our level
            self.cancel_vol += r.shares
            if ahead_of_us:
                self.ahead_true -= r.shares
            self.ahead_opt = max(0, self.ahead_opt - r.shares)
            self.ahead_prop -= r.shares * self.ahead_prop / level_shares_before
            # pessimistic does nothing on a cancel, by definition
        return False

    def close(self, outcome, ts):
        if self.outcome is None:
            self.outcome = outcome
        self.t_end = ts
