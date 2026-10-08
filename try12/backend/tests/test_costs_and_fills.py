"""Fix 11: statutory charges and fills at bid/ask with fallbacks."""
import pytest

from app.services.trading.paper_engine import (
    _entry_fill, _exit_fill, _quote_valid, _statutory_charges, _tick_age_seconds,
    paper_engine,
)
from tests.helpers import add_position, get_position, run, seed_config


def test_statutory_charges_hand_calc():
    # buy ₹6,500, sell ₹8,450, brokerage ₹40 (both legs)
    # STT 12.675 + exch 5.3117 + SEBI 0.01495 + stamp 0.195 + GST 8.1588 = 26.355
    assert _statutory_charges(6500, 8450, 40) == pytest.approx(26.36, abs=0.02)


def test_quote_validity():
    assert _quote_valid(70.0, 70.5, 1)
    assert not _quote_valid(70.30, 70.25, 1)      # crossed (B6) → invalid
    assert not _quote_valid(0, 70.5, 1)           # missing bid
    assert not _quote_valid(70.0, 70.5, 120)      # stale
    assert not _quote_valid(70.0, 70.5, None)     # age unknown
    assert not _quote_valid(60.0, 70.0, 1)        # spread > 5% of mid


def test_entry_fill():
    assert _entry_fill(70.2, 70.0, 70.5, 1, 0.2) == 70.5            # valid → ask
    assert _entry_fill(70.0, 70.30, 70.25, 1, 0.2) == 70.14         # crossed → LTP + 0.2%
    assert _entry_fill(70.0, 0, 0, None, 0.2) == 70.14              # missing → fallback
    assert _entry_fill(70.0, 70.0, 70.5, 120, 0.2) == 70.14         # stale → fallback


def test_exit_fill():
    assert _exit_fill(70.2, 70.0, 70.5, 1, "SL") == 70.0                 # valid → bid
    assert _exit_fill(70.2, 70.0, 70.5, 1, "EOD_STALE") == 70.0
    assert _exit_fill(130.0, 129.0, 129.5, 1, "TARGET") == 129.87        # limit, ignores bid
    assert _exit_fill(70.0, 70.30, 70.25, 1, "SL") == 69.93              # crossed → −0.1%
    assert _exit_fill(70.0, 0, 0, None, "KILL_SWITCH") == 69.93


def test_tick_age():
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 10, 6, 5, 0, 0, tzinfo=timezone.utc)
    tick = {"ts": (now - timedelta(seconds=90)).replace(tzinfo=None).isoformat()}
    assert _tick_age_seconds(tick, now) == pytest.approx(90)
    assert _tick_age_seconds({}, now) is None
    assert _tick_age_seconds(None, now) is None
    assert _tick_age_seconds({"ts": "garbage"}, now) is None


def test_close_position_books_charges(sqlite_db, fake_redis):
    run(seed_config(sqlite_db, used_capital=6500.0))
    pid = run(add_position(sqlite_db))   # 1 lot × 65 @ 100, entry brokerage ₹20
    fake_redis.put_tick("OPT1", 130.0, bid=129.5, ask=130.5, age_s=1)
    run(paper_engine._close_position(pid, 130.0, "TARGET"))
    pos = run(get_position(sqlite_db, pid))

    exit_fill = round(130.0 * 0.999, 2)                       # 129.87 (TARGET limit)
    gross = round((exit_fill - 100.0) * 65, 2)
    charges = _statutory_charges(6500.0, exit_fill * 65, 40.0)
    assert pos.actual_exit == exit_fill
    assert pos.gross_pnl == gross
    assert pos.brokerage == 40.0
    assert pos.statutory_charges == charges
    assert pos.net_pnl == round(gross - 40.0 - charges, 2)
    assert pos.exit_reason == "TARGET"


def test_close_position_sl_fills_at_valid_bid(sqlite_db, fake_redis):
    run(seed_config(sqlite_db, used_capital=6500.0))
    pid = run(add_position(sqlite_db))
    fake_redis.put_tick("OPT1", 84.5, bid=84.3, ask=84.7, age_s=1)
    run(paper_engine._close_position(pid, 85.0, "SL"))
    pos = run(get_position(sqlite_db, pid))
    assert pos.actual_exit == 84.3
    assert pos.exit_slippage == round(85.0 - 84.3, 2)
