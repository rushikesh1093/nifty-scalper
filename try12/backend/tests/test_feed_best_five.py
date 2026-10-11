"""
SnapQuote (mode 3) best-five parsing.

The 200-byte block at 147:347 is 10 packets x 20 bytes, each little-endian
``<H flag, q qty, q price (paise), H orders``. The old parser read packet 0 as the
bid and packet 1 as the ask without looking at the flag; packets 0 and 1 are the
same side, so the "ask" was the second-best bid and every NIFTY option tick since
2026-10-05 had best_bid > best_ask (median gap ₹0.05). The SDK's flag meaning is
ambiguous (it swaps buy/sell after parsing), so sides are resolved by price: the
lower-priced group is the bid side, the higher-priced group the ask side.

Every test here builds a REAL binary SnapQuote frame (379 bytes) and runs it
through ``_parse_binary_tick``.
"""
import logging
import struct

import pytest

from app.core.trading_params import TradingParams
from app.services.market_data.feed import TickData, _parse_binary_tick

HEADER_FMT = "<BB25s" + "qqqqqq" + "dd" + "qqqq" + "qqq"     # 147 bytes
PACKET_FMT = "<HqqH"                                          # 20 bytes
TAIL_FMT = "<qqqq"                                            # 32 bytes

FIELDS = dict(
    token="43650", exch=2, seq=7, exch_ts=1_791_000_000_000, ltp=81.25, ltq=65, atp=80.9,
    volume=1_234_500, total_buy=410_000.0, total_sell=395_000.0,
    open=79.10, high=83.40, low=78.55, close=80.05, ltt=1_791_000_000, oi=2_345_600,
    oi_change=12_345,
)

BIDS = [(81.20, 650), (81.15, 1300), (81.10, 1950), (81.05, 2600), (81.00, 3250)]
ASKS = [(81.25, 715), (81.30, 1365), (81.35, 2015), (81.40, 2665), (81.45, 3315)]


def _p(rupees: float) -> int:
    return int(round(rupees * 100))


def _packets(levels):
    """levels: [(flag, qty, price_rupees), ...] -> 10 packets of 20 bytes (zero-padded)."""
    out = b"".join(struct.pack(PACKET_FMT, flag, qty, _p(price), 3) for flag, qty, price in levels)
    out += b"\x00" * 20 * (10 - len(levels))
    assert len(out) == 200
    return out


def _snap(levels, *, mode=3, **over) -> bytes:
    f = dict(FIELDS, **over)
    head = struct.pack(
        HEADER_FMT, mode, f["exch"], f["token"].encode("ascii"), f["seq"], f["exch_ts"],
        _p(f["ltp"]), f["ltq"], _p(f["atp"]), f["volume"], f["total_buy"], f["total_sell"],
        _p(f["open"]), _p(f["high"]), _p(f["low"]), _p(f["close"]), f["ltt"], f["oi"],
        f["oi_change"])
    assert len(head) == 147
    frame = head + _packets(levels) + struct.pack(TAIL_FMT, _p(90.0), _p(70.0), _p(400.0), _p(5.0))
    assert len(frame) == 379
    return frame


def _book(buy_flag=0, sell_flag=1, bids=BIDS, asks=ASKS):
    """Bids first (best first, as AngelOne sends them), then asks."""
    return [(buy_flag, q, px) for px, q in bids] + [(sell_flag, q, px) for px, q in asks]


def _legacy_bid_ask(frame: bytes):
    """The pre-fix parse, verbatim offsets: packet 0 → bid, packet 1 → ask."""
    bid = struct.unpack_from("<q", frame, 147 + 10)[0] / 100.0
    ask = struct.unpack_from("<q", frame, 167 + 10)[0] / 100.0
    return bid, ask


def _ba(t: TickData):
    return t.best_bid, t.best_bid_qty, t.best_ask, t.best_ask_qty


# ── normal books ─────────────────────────────────────────────────────────────

def test_normal_book_flag_zero_is_buy():
    t = _parse_binary_tick(_snap(_book(buy_flag=0, sell_flag=1)))
    assert _ba(t) == (81.20, 650, 81.25, 715)


def test_normal_book_with_flags_inverted():
    # the SDK swaps buy/sell after parsing, so flag 1 may be the buy side: same answer
    t = _parse_binary_tick(_snap(_book(buy_flag=1, sell_flag=0)))
    assert _ba(t) == (81.20, 650, 81.25, 715)


def test_interleaved_packets_resolve_by_price():
    levels = []
    for (bp, bq), (ap, aq) in zip(BIDS, ASKS):
        levels += [(0, bq, bp), (1, aq, ap)]
    t = _parse_binary_tick(_snap(levels))
    assert _ba(t) == (81.20, 650, 81.25, 715)


def test_best_level_is_not_assumed_to_be_first_on_the_wire():
    t = _parse_binary_tick(_snap(_book(bids=list(reversed(BIDS)), asks=list(reversed(ASKS)))))
    assert _ba(t) == (81.20, 650, 81.25, 715)


# ── empty levels / empty side / unresolvable books ───────────────────────────

def test_empty_levels_are_ignored():
    t = _parse_binary_tick(_snap(_book(bids=BIDS[:2], asks=ASKS[:3])))
    assert _ba(t) == (81.20, 650, 81.25, 715)


@pytest.mark.parametrize("bids, asks", [(BIDS, []), ([], ASKS)])
def test_one_sided_book_leaves_both_sides_at_default(bids, asks):
    # a lone group cannot be labelled by price, and the flag is ambiguous
    t = _parse_binary_tick(_snap(_book(bids=bids, asks=asks)))
    assert _ba(t) == (0.0, 0, 0.0, 0)
    assert t.ltp == FIELDS["ltp"] and t.oi == FIELDS["oi"]


def test_all_zero_book():
    t = _parse_binary_tick(_snap([]))
    assert _ba(t) == (0.0, 0, 0.0, 0)


def test_overlapping_sides_are_unresolvable_and_not_logged_at_warning(caplog):
    caplog.set_level(logging.DEBUG, logger="app.services.market_data.feed")
    t = _parse_binary_tick(_snap([(0, 650, 81.20), (0, 650, 81.40), (1, 715, 81.30)]))
    assert _ba(t) == (0.0, 0, 0.0, 0)
    assert not [r for r in caplog.records
                if r.name == "app.services.market_data.feed" and r.levelno >= logging.WARNING]


def test_negative_price_or_qty_packets_are_ignored():
    # either bad packet, if read, would change the answer; the rest is
    # bids 81.20..81.00 (flag 0) and asks 81.25..81.35 (flag 1)
    levels = [(1, 650, -81.50), (1, -715, 81.22)] + _book()[:8]
    t = _parse_binary_tick(_snap(levels))
    assert _ba(t) == (81.20, 650, 81.25, 715)


# ── the pure helper ──────────────────────────────────────────────────────────

def test_helper_is_pure_and_bounds_checked():
    from app.services.market_data.feed import (
        BEST_FIVE_END, BEST_FIVE_OFFSET, BEST_FIVE_PACKET_LEN, BEST_FIVE_PACKETS,
        parse_best_five,
    )
    assert (BEST_FIVE_OFFSET, BEST_FIVE_PACKETS, BEST_FIVE_PACKET_LEN, BEST_FIVE_END) == \
        (147, 10, 20, 347)
    block = _packets(_book())
    assert parse_best_five(block) == (81.20, 650, 81.25, 715)
    assert parse_best_five(block) == parse_best_five(block)
    assert parse_best_five(block[:199]) == (0.0, 0, 0.0, 0)
    assert parse_best_five(b"") == (0.0, 0, 0.0, 0)


def test_locked_book_is_resolved():
    from app.services.market_data.feed import parse_best_five
    assert parse_best_five(_packets([(0, 650, 81.20), (1, 715, 81.20)])) == \
        (81.20, 650, 81.20, 715)


# ── short frames ─────────────────────────────────────────────────────────────

def test_frame_shorter_than_ltp_is_dropped():
    assert _parse_binary_tick(_snap(_book())[:50]) is None


def test_ltp_only_frame():
    t = _parse_binary_tick(_snap(_book())[:51])
    assert t.ltp == FIELDS["ltp"] and t.token == FIELDS["token"]
    assert _ba(t) == (0.0, 0, 0.0, 0) and t.oi == 0


@pytest.mark.parametrize("n", [146, 200, 346])
def test_truncated_snap_quote_has_no_bid_ask(n):
    t = _parse_binary_tick(_snap(_book())[:n])
    assert _ba(t) == (0.0, 0, 0.0, 0)
    assert t.close == FIELDS["close"]
    assert t.oi == (FIELDS["oi"] if n >= 147 else 0)


def test_quote_mode_frame_parses_ohlc_without_bid_ask():
    t = _parse_binary_tick(_snap(_book(), mode=2)[:123])
    assert (t.open, t.high, t.low, t.close, t.volume) == (
        FIELDS["open"], FIELDS["high"], FIELDS["low"], FIELDS["close"], FIELDS["volume"])
    assert _ba(t) == (0.0, 0, 0.0, 0)


# ── everything else is unchanged ─────────────────────────────────────────────

def test_other_fields_types_and_units_unchanged():
    t = _parse_binary_tick(_snap(_book()))
    assert isinstance(t, TickData)
    assert (t.token, t.exchange, t.exchange_ts) == (FIELDS["token"], "NFO", FIELDS["exch_ts"])
    assert (t.ltp, t.open, t.high, t.low, t.close) == (
        FIELDS["ltp"], FIELDS["open"], FIELDS["high"], FIELDS["low"], FIELDS["close"])
    assert (t.volume, t.oi, t.oi_change) == (FIELDS["volume"], FIELDS["oi"], FIELDS["oi_change"])
    assert type(t.best_bid) is float and type(t.best_ask) is float
    assert type(t.best_bid_qty) is int and type(t.best_ask_qty) is int


# ── the consequence: the selector and the fill model can use the quote ───────

def test_parsed_quote_is_usable_where_the_old_parse_was_not():
    from app.services.trading import fills
    from app.services.trading.option_selector import _quote_usable

    frame = _snap(_book())
    t = _parse_binary_tick(frame)
    assert _quote_usable(t.best_bid, t.best_ask) is True
    assert fills.quote_valid(t.best_bid, t.best_ask, 0.0, TradingParams()) is True
    assert fills.entry_fill(t.ltp, t.best_bid, t.best_ask, 0.0, TradingParams())[1] == "ASK"

    # fail-before, against the old offsets: the live symptom (bid > ask by ₹0.05)
    old_bid, old_ask = _legacy_bid_ask(frame)
    assert (old_bid, old_ask) == (81.20, 81.15)
    assert round(old_bid - old_ask, 2) == 0.05
    assert _quote_usable(old_bid, old_ask) is False
    assert fills.quote_valid(old_bid, old_ask, 0.0, TradingParams()) is False
