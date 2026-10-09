"""
Streaming reader for Nasdaq TotalView-ITCH 5.0.

The feed is a flat sequence of length-prefixed binary messages, big-endian,
no framing beyond the 2-byte length. Files are gzipped and a full day is
about 5.6 GB compressed, so everything here works off a stream and never
holds the file in memory.

Spec: Nasdaq TotalView-ITCH 5.0, section 4.
Prices are 4-byte unsigned with 4 implied decimals, so 1234500 means 123.45.
Timestamps are 6-byte nanoseconds since midnight Eastern.
"""

import struct
import zlib
from collections import namedtuple

# Unpackers built once. struct.Struct is noticeably faster than the module
# level functions when you call them tens of millions of times.
_u_add = struct.Struct(">HH6sQcI8sI").unpack_from      # locate, track, ts, ref, side, shares, stock, price
_u_exec = struct.Struct(">HH6sQIQ").unpack_from        # locate, track, ts, ref, shares, match
_u_exec_px = struct.Struct(">HH6sQIQcI").unpack_from   # + printable, price
_u_cancel = struct.Struct(">HH6sQI").unpack_from       # locate, track, ts, ref, shares
_u_delete = struct.Struct(">HH6sQ").unpack_from        # locate, track, ts, ref
_u_replace = struct.Struct(">HH6sQQII").unpack_from    # locate, track, ts, oldref, newref, shares, price
_u_trade = struct.Struct(">HH6sQcI8sIQ").unpack_from   # non-cross trade
_u_dir = struct.Struct(">HH6s8s").unpack_from          # stock directory head

Add = namedtuple("Add", "ts locate ref side shares price")
Exec = namedtuple("Exec", "ts locate ref shares price")
Cancel = namedtuple("Cancel", "ts locate ref shares")
Delete = namedtuple("Delete", "ts locate ref")
Replace = namedtuple("Replace", "ts locate old_ref new_ref shares price")
Trade = namedtuple("Trade", "ts locate side shares price")


def _ts(b):
    return int.from_bytes(b, "big")


def _chunks(path, raw_chunk=1 << 22):
    """Decompressed bytes from a gzip file, chunk by chunk, tolerant of a
    truncated tail.

    The study runs on a byte-range prefix of a 5.6 GB file, so the gzip stream
    stops with no end-of-stream marker. gzip.read(n) raises at that point and
    drops everything it had decoded in the same call; zlib hands back every
    byte that can be decoded.
    """
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    with open(path, "rb") as f:
        while True:
            raw = f.read(raw_chunk)
            if not raw:
                break
            out = d.decompress(raw)
            while d.eof and d.unused_data:          # a multi-member gzip file
                rest = d.unused_data
                d = zlib.decompressobj(16 + zlib.MAX_WBITS)
                out += d.decompress(rest)
            if out:
                yield out


def stock_directory(path, limit_bytes=8 * 1024 * 1024):
    """Read the Stock Directory messages at the head of the file.

    Every later message identifies its instrument by a 2-byte stock_locate
    integer and never by ticker, so you cannot filter by symbol until you have
    read this mapping. Nasdaq emits all of them before the open, which is why a
    few MB is enough.
    """
    locates = {}
    buf = b""
    for block in _chunks(path, raw_chunk=1 << 20):
        buf += block
        if len(buf) >= limit_bytes:
            break
    i = 0
    n = len(buf)
    while i + 2 <= n:
        ln = (buf[i] << 8) | buf[i + 1]
        if i + 2 + ln > n:
            break
        if buf[i + 2:i + 3] == b"R":
            locate, _track, _t, stock = _u_dir(buf, i + 3)
            locates[stock.decode().strip()] = locate
        i += 2 + ln
    return locates


def stream(path, locates=None, stats=None):
    """Yield parsed book messages for the stock_locate ints in `locates`
    (None for every stock).

    Reading the length, type byte and locate straight off the buffer and
    rejecting before any unpacking keeps this fast in pure Python; unpacking
    every message first measured about 2.4 times slower end to end.
    stats["scanned"], if given, counts every message that went past.
    """
    want = set(b"AFECXDUP")
    buf = b""
    scanned = 0
    try:
        for block in _chunks(path):
            buf = buf + block if buf else block
            i = 0
            n = len(buf)
            while True:
                if i + 2 > n:
                    break
                ln = (buf[i] << 8) | buf[i + 1]
                end = i + 2 + ln
                if end > n:
                    break
                scanned += 1
                t = buf[i + 2]
                if t in want:
                    p = i + 3
                    locate = (buf[p] << 8) | buf[p + 1]
                    if locates is None or locate in locates:
                        m = _parse(t, buf, p)
                        if m is not None:
                            yield m
                i = end
            buf = buf[i:]
            if stats is not None:
                stats["scanned"] = scanned
    finally:
        if stats is not None:
            stats["scanned"] = scanned


def _parse(t, buf, p):
    if t == 0x41:  # A, add order, no attribution
        locate, _tr, ts, ref, side, shares, _stk, price = _u_add(buf, p)
        return Add(_ts(ts), locate, ref, side, shares, price)
    if t == 0x46:  # F, add order with MPID. Same layout plus 4 trailing bytes.
        locate, _tr, ts, ref, side, shares, _stk, price = _u_add(buf, p)
        return Add(_ts(ts), locate, ref, side, shares, price)
    if t == 0x45:  # E, execution at the order's display price
        locate, _tr, ts, ref, shares, _match = _u_exec(buf, p)
        return Exec(_ts(ts), locate, ref, shares, None)
    if t == 0x43:  # C, execution that can carry a price other than the display price
        locate, _tr, ts, ref, shares, _match, _pr, price = _u_exec_px(buf, p)
        return Exec(_ts(ts), locate, ref, shares, price)
    if t == 0x58:  # X, partial cancel
        locate, _tr, ts, ref, shares = _u_cancel(buf, p)
        return Cancel(_ts(ts), locate, ref, shares)
    if t == 0x44:  # D, full delete
        locate, _tr, ts, ref = _u_delete(buf, p)
        return Delete(_ts(ts), locate, ref)
    if t == 0x55:  # U, replace. Cancels old ref and adds new ref, loses priority.
        locate, _tr, ts, old, new, shares, price = _u_replace(buf, p)
        return Replace(_ts(ts), locate, old, new, shares, price)
    if t == 0x50:  # P, non-cross trade, hidden liquidity. Never touches the book.
        locate, _tr, ts, _ref, side, shares, _stk, price, _m = _u_trade(buf, p)
        return Trade(_ts(ts), locate, side, shares, price)
    return None
