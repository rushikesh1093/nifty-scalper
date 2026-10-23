"""Fill and cost model (design §15, R14): app/services/trading/fills.py is the one
implementation; one case per exit_fill decision-table row, gap-through, quote
slippage, charges and the net identity."""
import random

import pytest

from app.core.trading_params import TradingParams
from app.services.trading import fills
from app.services.trading.paper_engine import _tick_age_seconds, paper_engine
from tests.helpers import add_position, get_position, run, seed_config

P = TradingParams()          # quote_slippage_pct 0.05, slippage_pct 0.2, spread cap 5%


def test_statutory_charges_hand_calc():
    # buy ₹6,500, sell ₹8,450, brokerage ₹40 (both legs)
    # STT 12.675 + exch 5.3117 + SEBI 0.01495 + stamp 0.195 + GST 8.1588 = 26.355
    assert fills.statutory_charges(6500, 8450, 40, P) == pytest.approx(26.36, abs=0.02)


def test_brokerage_per_lot_per_leg():
    # per lot when the flat per-order model is off; otherwise one flat amount per order
    from dataclasses import replace
    assert fills.brokerage(3, replace(P, brokerage_flat_per_order=False)) == 60.0
    assert fills.brokerage(3, replace(P, brokerage_flat_per_order=True)) == 20.0


def test_quote_validity():
    assert fills.quote_valid(70.0, 70.5, 1, P)
    assert not fills.quote_valid(70.30, 70.25, 1, P)      # crossed → invalid
    assert not fills.quote_valid(0, 70.5, 1, P)           # missing bid
    assert not fills.quote_valid(70.0, 70.5, 120, P)      # stale
    assert not fills.quote_valid(70.0, 70.5, None, P)     # age unknown
    assert not fills.quote_valid(60.0, 70.0, 1, P)        # spread > 5% of mid
    assert fills.quote_valid(195.0, 205.0, 1, P)          # exactly 5% of mid


def test_entry_fill_at_ask_plus_quote_slippage_rounded_up():
    price, src = fills.entry_fill(70.2, 70.0, 70.5, 1, P)
    assert src == "ASK"
    assert price == 70.55                                  # ceil_tick(70.5 × 1.0005 = 70.535)
    assert price == fills.ceil_tick(70.5 * 1.0005)


@pytest.mark.parametrize("bid, ask, age", [(70.30, 70.25, 1), (0, 0, None), (70.0, 70.5, 120)])
def test_entry_fill_without_a_valid_quote_is_the_ltp_fallback(bid, ask, age):
    assert fills.entry_fill(70.0, bid, ask, age, P) == (70.14, "LTP_FALLBACK")   # LTP + 0.2%


# ── exit_fill decision table, one case per row (§15) ─────────────────────────

def test_row1_target_is_a_resting_limit():
    # ignores even a valid bid: trigger × (1 − 0.1%)
    assert fills.exit_fill(130.0, 130.5, 129.0, 129.5, 1, "TARGET", P) == (129.87, "TRIGGER_SLIPPED")


def test_row2_valid_quote_sells_at_bid_less_quote_slippage_rounded_down():
    price, src = fills.exit_fill(70.2, 70.2, 70.0, 70.5, 1, "MANUAL", P)
    assert src == "BID" and price == 69.95                 # floor_tick(70.0 × 0.9995 = 69.965)


def test_row3_stale_reason_with_no_tick_is_last_known():
    assert fills.exit_fill(70.0, None, 0, 0, None, "EOD_STALE", P) == (69.93, "LAST_KNOWN")


def test_row4_no_quote_is_trigger_slipped():
    assert fills.exit_fill(70.0, 70.0, 70.30, 70.25, 1, "KILL_SWITCH", P) == (69.93, "TRIGGER_SLIPPED")
    assert fills.exit_fill(70.0, 71.0, 0, 0, None, "EOD_STALE", P) == (69.93, "TRIGGER_SLIPPED")


def test_sl_gap_through_fills_at_the_observed_price():
    trigger, ltp = 85.0, 76.5                                # 10% below the stop, no quote
    assert fills.exit_fill(trigger, ltp, 0, 0, None, "SL", P) == (76.5, "LTP_GAP")
    assert fills.exit_fill(trigger, ltp, 0, 0, None, "SL_STALE", P) == (76.5, "LTP_GAP")


def test_sl_with_a_valid_bid_below_ltp_stays_bid():
    price, src = fills.exit_fill(85.0, 84.8, 84.3, 84.7, 1, "SL", P)
    assert (price, src) == (84.25, "BID")                    # floor_tick(84.3 × 0.9995)


def test_sl_fill_is_never_better_than_the_stop():
    rnd = random.Random(11)
    for _ in range(500):
        trigger = round(rnd.uniform(20, 400), 2)
        ltp = round(trigger * rnd.uniform(0.85, 1.0), 2)       # SL triggers only at ltp <= stop
        bid = round(ltp * rnd.uniform(0.97, 1.0), 2) if rnd.random() < 0.7 else 0.0
        ask = round(bid * rnd.uniform(1.0, 1.04), 2) if bid else 0.0
        price, _ = fills.exit_fill(trigger, ltp, bid, ask, rnd.choice([1, 120, None]), "SL", P)
        assert price <= trigger


def test_quote_slippage_must_not_exceed_ltp_slippage():
    v = TradingParams(quote_slippage_pct=0.5, slippage_pct=0.2).validate()
    assert any("quote_slippage_pct" in m for m in v)


def test_tick_age(runtime_ctx):
    # the age is measured against the ctx clock (no `now` parameter, design §14.2)
    from datetime import timedelta
    now = runtime_ctx.clock.now_utc()
    tick = {"ts": (now - timedelta(seconds=90)).replace(tzinfo=None).isoformat()}
    assert _tick_age_seconds(tick) == pytest.approx(90)
    assert _tick_age_seconds({}) is None
    assert _tick_age_seconds(None) is None
    assert _tick_age_seconds({"ts": "garbage"}) is None


def test_net_identity_holds_for_every_leg_mix():
    rnd = random.Random(5)
    for _ in range(200):
        entry, qty_lots, lot = round(rnd.uniform(20, 300), 2), rnd.randint(2, 4), 65
        part_lots = qty_lots // 2
        part_px, final_px = round(entry * rnd.uniform(0.9, 1.3), 2), round(entry * rnd.uniform(0.7, 1.3), 2)
        legs = [(part_lots, part_px), (qty_lots - part_lots, final_px)]
        gross = sum((px - entry) * n * lot for n, px in legs)
        brok = fills.brokerage(qty_lots, P) + sum(fills.brokerage(n, P) for n, _ in legs)
        charges = sum(fills.statutory_charges(entry * n * lot, px * n * lot,
                                              2 * fills.brokerage(n, P), P) for n, px in legs)
        assert fills.net_pnl(gross, brok, charges) == pytest.approx(gross - brok - charges, abs=0.01)


def test_expected_round_trip_cost_is_brokerage_plus_charges():
    cost = fills.expected_round_trip_cost(100.0, 130, 2, P)
    brok = 2 * fills.brokerage(2, P)
    assert cost == pytest.approx(brok + fills.statutory_charges(13000.0, 13000.0, brok, P),
                                 abs=0.01)


# ── the engine uses fills.py ─────────────────────────────────────────────────

def test_close_position_books_charges(sqlite_db, fake_redis):
    run(seed_config(sqlite_db, used_capital=6500.0))
    pid = run(add_position(sqlite_db))   # 1 lot × 65 @ 100, entry brokerage ₹20
    fake_redis.put_tick("OPT1", 130.0, bid=129.5, ask=130.5, age_s=1)
    run(paper_engine._close_position(pid, 130.0, "TARGET"))
    pos = run(get_position(sqlite_db, pid))

    exit_fill = round(130.0 * 0.999, 2)                       # 129.87 (TARGET limit)
    gross = round((exit_fill - 100.0) * 65, 2)
    charges = fills.statutory_charges(6500.0, exit_fill * 65, 40.0, P)
    assert pos.actual_exit == exit_fill and pos.exit_fill_source == "TRIGGER_SLIPPED"
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
    assert pos.actual_exit == 84.25 and pos.exit_fill_source == "BID"
    assert pos.exit_slippage == round(85.0 - 84.25, 2)


def test_quoteless_open_position_still_closes_at_square_off(sqlite_db, fake_redis):
    """Entries may be refused; exits may not (§15)."""
    run(seed_config(sqlite_db, used_capital=6500.0))
    pid = run(add_position(sqlite_db))
    fake_redis.drop_price("OPT1")
    paper_engine._last_known[pid] = (101.0, None)
    run(paper_engine.square_off_all("EOD"))
    pos = run(get_position(sqlite_db, pid))
    assert not pos.is_open and pos.exit_reason == "EOD_STALE"
    assert (pos.actual_exit, pos.exit_fill_source) == (round(101.0 * 0.999, 2), "LAST_KNOWN")
