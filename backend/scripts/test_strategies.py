"""
Offline strategy test — no DB, no Redis required.
Tests all 12 strategies with crafted candles designed to trigger each one.

Checks:
  1. compute_all() returns correct indicator keys (cache check)
  2. Each strategy fires a signal on purpose-built candles
  3. SignalBus delivers the signal to a captured handler
  4. Signal fields (direction, strategy_name, sl_pct, target_pct, min_rr) are correct
  5. Per-strategy cooldown blocks a second immediate signal
  6. SignalAggregator blocks same-direction duplicate within 3 minutes

Run:
    cd backend
    python scripts/test_strategies.py
"""
import asyncio
import sys
import os
from datetime import datetime, timezone, timedelta, date as dt_date
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.strategies.strategies import (
    ORBVWAPStrategy, VWAPPullbackStrategy, EMAVWAPStrategy,
    SupertrendVWAPStrategy, PDHPDLStrategy, VWAPReversalStrategy,
    BollingerBreakoutStrategy, RSIVWAPStrategy, OISpikeStrategy,
    IVCrushStrategy, CPRVWAPStrategy, GapVWAPFillStrategy,
)
from app.strategies.base import SignalBus, SignalEvent, Direction, signal_aggregator
from app.strategies.indicators import compute_all

# ── Helpers ───────────────────────────────────────────────────────────────────

PASS = "✅"
FAIL = "❌"
SKIP = "⚠️ "

captured_signals: list[SignalEvent] = []

async def _capture(signal: SignalEvent) -> bool:
    captured_signals.append(signal)
    return True   # tell SignalBus a position was "opened" → record_acted() is kept

def sep(title: str = ""):
    w = 68
    if title:
        pad = (w - len(title) - 2) // 2
        print(f"\n{'─'*pad} {title} {'─'*pad}")
    else:
        print("─" * w)

def ok(label: str, detail: str = ""):
    print(f"  {PASS}  {label}" + (f"  ({detail})" if detail else ""))

def fail(label: str, detail: str = ""):
    print(f"  {FAIL}  {label}" + (f"  ({detail})" if detail else ""))

def warn(label: str, detail: str = ""):
    print(f"  {SKIP}  {label}" + (f"  ({detail})" if detail else ""))


# ── Candle factory ────────────────────────────────────────────────────────────

def make_candles(
    n: int = 50,
    base: float = 24500.0,
    trend: float = 0.0,          # points added per candle
    volume: float = 80_000.0,
    start_time: Optional[datetime] = None,
    vol_spike_idx: Optional[int] = None,   # candle index to spike volume
    high_offset: float = 5.0,
    low_offset: float = 5.0,
) -> list[dict]:
    """Build a list of 1-min OHLCV candle dicts."""
    if start_time is None:
        # 9:15 IST today
        start_time = datetime.now(timezone(timedelta(hours=5, minutes=30))).replace(
            hour=9, minute=15, second=0, microsecond=0
        )
    candles = []
    price = base
    for i in range(n):
        ts = start_time + timedelta(minutes=i)
        o = price
        c = price + trend
        h = max(o, c) + high_offset
        l = min(o, c) - low_offset
        v = volume * 3.0 if vol_spike_idx is not None and i == vol_spike_idx else volume
        candles.append({
            "timestamp": ts.isoformat(),
            "open": round(o, 2),
            "high": round(h, 2),
            "low":  round(l, 2),
            "close": round(c, 2),
            "volume": v,
        })
        price = c
    return candles


def make_candles_at(hour: int, minute: int, **kwargs) -> list[dict]:
    """Candles whose last bar falls at the given IST hour:minute."""
    IST = timezone(timedelta(hours=5, minutes=30))
    today = datetime.now(IST).replace(hour=hour, minute=minute, second=0, microsecond=0)
    n = kwargs.pop("n", 50)
    start = today - timedelta(minutes=n - 1)
    return make_candles(n=n, start_time=start, **kwargs)


# ── Indicator cache check ─────────────────────────────────────────────────────

def test_compute_all():
    sep("1. compute_all() indicator cache")
    candles = make_candles(60)
    ind = compute_all(candles)

    required = [
        "ema9", "ema21", "ema50", "rsi", "atr",
        "bb_upper", "bb_mid", "bb_lower", "bb_width",
        "vwap", "supertrend", "supertrend_dir",
        "orb_high", "orb_low", "orb_broken",
        "close", "high", "low", "volume",
    ]
    all_ok = True
    for key in required:
        if key in ind and ind[key] is not None:
            ok(f"indicators['{key}'] = {ind[key]:.4f}" if isinstance(ind[key], float) else f"indicators['{key}'] = {ind[key]}")
        else:
            fail(f"indicators['{key}'] missing or None")
            all_ok = False

    # Sanity: EMA9 should track close
    if ind.get("ema9") and ind.get("close"):
        diff_pct = abs(ind["ema9"] - ind["close"]) / ind["close"] * 100
        if diff_pct < 5:
            ok(f"EMA9 close to close price (diff={diff_pct:.2f}%)")
        else:
            warn(f"EMA9 far from close ({diff_pct:.2f}%) — might be a flat candle series")

    return all_ok, ind


# ── Individual strategy tests ─────────────────────────────────────────────────

async def test_strategy(
    label: str,
    strategy,
    candles: list[dict],
    indicators: dict,
    expect_direction: Optional[Direction] = None,
    extra_setup=None,
) -> bool:
    """Run one strategy, return True if it fired a signal."""
    # Reset cooldown so we always get a fresh result
    strategy._last_signal_time = None

    if extra_setup:
        extra_setup(strategy)

    captured_signals.clear()
    signal_aggregator.reset()

    signal = await strategy.evaluate(candles, indicators)

    if signal is None:
        fail(f"{label}: no signal fired")
        return False

    if expect_direction and signal.direction != expect_direction:
        fail(f"{label}: expected {expect_direction}, got {signal.direction}")
        return False

    # Publish through SignalBus to verify delivery
    await SignalBus.publish(signal)

    if not captured_signals:
        fail(f"{label}: signal not delivered via SignalBus")
        return False

    s = captured_signals[0]
    ok(
        f"{label}: {s.direction.value} @ spot={s.spot_price:.1f} "
        f"sl={s.sl_pct:.0%} tgt={s.target_pct:.0%} min_rr={s.min_rr}",
        f"notes: {s.notes[:60]}"
    )
    return True


async def run_all_strategy_tests():
    sep("2–13. Strategy Signal Tests")

    results: dict[str, bool] = {}

    # ── 1. ORB_VWAP ──────────────────────────────────────────────────────────
    # Need: close > orb_high, close > vwap, volume surge, time 9:30-11:30
    # Build: 15 base candles (ORB) then price breaks up hard with vol spike
    base_orb = 24500.0
    orb_candles = make_candles_at(10, 0, n=40, base=base_orb, trend=0.0)
    # Force last candle above ORB high + VWAP with a volume spike
    orb_high = max(c["high"] for c in orb_candles[:15])
    vwap_approx = base_orb  # VWAP ≈ base on flat candles
    for i in range(-3, 0):
        orb_candles[i]["close"] = orb_high + 30
        orb_candles[i]["open"]  = orb_high + 10
        orb_candles[i]["high"]  = orb_high + 40
        orb_candles[i]["low"]   = orb_high + 5
        orb_candles[i]["volume"] = 250_000   # surge
    ind_orb = compute_all(orb_candles)
    # Manually push indicators so strategy sees correct orb_high + vwap
    ind_orb["orb_high"] = orb_high
    ind_orb["orb_low"]  = base_orb - 50
    ind_orb["vwap"]     = base_orb - 10    # price now above vwap
    ind_orb["avg_volume"] = 80_000
    results["ORB_VWAP"] = await test_strategy(
        "ORB_VWAP", ORBVWAPStrategy("NIFTY"),
        orb_candles, ind_orb, Direction.BUY
    )

    # ── 2. VWAP_PULLBACK ─────────────────────────────────────────────────────
    # Need: 4+ of last 6 closes above vwap, last candle touches vwap and bounces
    base_vp = 24500.0
    vp_candles = make_candles_at(11, 0, n=30, base=base_vp + 50, trend=0.5)
    # Last candle: low touches vwap, close above vwap, bullish
    vwap_vp = base_vp + 45
    for i in range(-6, 0):
        vp_candles[i]["close"] = vwap_vp + 10
    vp_candles[-1]["low"]   = vwap_vp - 2      # touched vwap
    vp_candles[-1]["close"] = vwap_vp + 15     # bounced above
    vp_candles[-1]["open"]  = vwap_vp + 3      # bullish candle
    ind_vp = compute_all(vp_candles)
    ind_vp["vwap"] = vwap_vp
    results["VWAP_PULLBACK"] = await test_strategy(
        "VWAP_PULLBACK", VWAPPullbackStrategy("NIFTY"),
        vp_candles, ind_vp, Direction.BUY
    )

    # ── 3. EMA_VWAP ──────────────────────────────────────────────────────────
    # Need: EMA9 crosses ABOVE EMA21 on the last bar ([-2] had EMA9 <= EMA21).
    # Strategy also needs: close > vwap, vol_surge (1.3×), ATR gate (≥3 pts).
    #
    # Approach: build 35 bars that trend DOWN so EMA9 < EMA21 at bar[-2],
    # then add a massive UP bar at the end to force the cross.
    # Volume is constant so vol_surge returns True (avg=0 → bypass).
    base_ema = 24500.0
    IST = timezone(timedelta(hours=5, minutes=30))
    ema_start = datetime.now(IST).replace(hour=9, minute=55, second=0, microsecond=0)
    ema_start = ema_start - timedelta(minutes=36)
    ema_candles = []
    # Phase 1 (36 bars): gentle downtrend so EMA9 < EMA21 before the final bar
    price_ema = base_ema
    for i in range(36):
        ts = ema_start + timedelta(minutes=i)
        price_ema -= 1.5   # slow drift down keeps EMA9 below EMA21
        ema_candles.append({"timestamp": ts.isoformat(),
            "open":   round(price_ema + 0.5, 2),
            "high":   round(price_ema + 3,   2),
            "low":    round(price_ema - 3,   2),
            "close":  round(price_ema,       2),
            "volume": 80_000})
    # Phase 2 (1 bar): explosive up — EMA9 crosses above EMA21 on this bar
    ts_last = ema_start + timedelta(minutes=36)
    jump_close = price_ema + 300   # ≈300-pt jump overwhelms EMA spread
    ema_candles.append({"timestamp": ts_last.isoformat(),
        "open":   round(price_ema + 10, 2),
        "high":   round(jump_close + 5, 2),
        "low":    round(price_ema,      2),
        "close":  round(jump_close,     2),
        "volume": 200_000})   # volume spike — 2.5× baseline → vol_surge passes
    ind_ema = compute_all(ema_candles)
    ind_ema["vwap"]       = jump_close - 50   # close > vwap ✓
    ind_ema["avg_volume"] = 80_000             # explicit baseline for vol_surge check
    results["EMA_VWAP"] = await test_strategy(
        "EMA_VWAP", EMAVWAPStrategy("NIFTY"),
        ema_candles, ind_ema
    )

    # ── 4. SUPERTREND_VWAP ───────────────────────────────────────────────────
    # Need: supertrend direction[-2]==-1 and direction[-1]==1 (flip UP on last bar).
    # Build: 15 flat bars (ATR warm-up), 15 hard-down bars (forces direction=-1),
    # then add UP bars one at a time until the flip occurs on the LAST bar.
    base_st = 24500.0
    IST = timezone(timedelta(hours=5, minutes=30))
    st_start = datetime.now(IST).replace(
        hour=9, minute=30, second=0, microsecond=0
    )
    import numpy as _np
    from app.strategies.indicators import supertrend as _supertrend

    st_raw: list[dict] = []
    price_st = base_st
    # Phase 1: flat (ATR warm-up)
    for i in range(15):
        ts = st_start + timedelta(minutes=i)
        st_raw.append({"timestamp": ts.isoformat(),
            "open": round(price_st, 2), "high": round(price_st + 5, 2),
            "low":  round(price_st - 5, 2), "close": round(price_st, 2),
            "volume": 80_000})
    # Phase 2: aggressive down — push supertrend into -1
    for i in range(15):
        ts = st_start + timedelta(minutes=15 + i)
        st_raw.append({"timestamp": ts.isoformat(),
            "open":  round(price_st, 2),      "high": round(price_st + 2, 2),
            "low":   round(price_st - 20, 2), "close": round(price_st - 18, 2),
            "volume": 80_000})
        price_st -= 18
    # Phase 3: aggressive up — add bars until direction flip lands on the last bar
    flip_candles = None
    for extra in range(1, 30):
        candidate = list(st_raw)
        p = price_st
        for j in range(extra):
            ts = st_start + timedelta(minutes=30 + j)
            candidate.append({"timestamp": ts.isoformat(),
                "open":  round(p, 2),       "high": round(p + 25, 2),
                "low":   round(p - 2, 2),   "close": round(p + 22, 2),
                "volume": 80_000})
            p += 22
        h_ = _np.array([float(c["high"])  for c in candidate])
        l_ = _np.array([float(c["low"])   for c in candidate])
        c_ = _np.array([float(c["close"]) for c in candidate])
        st_res = _supertrend(h_, l_, c_, 10, 3.0)
        d = st_res.direction
        if d[-2] == -1 and d[-1] == 1:
            flip_candles = candidate
            break

    if flip_candles is None:
        warn("SUPERTREND_VWAP: could not construct flip candles — skipping")
        results["SUPERTREND_VWAP"] = False
    else:
        # Ensure the candle timestamp falls within 9:30–14:00 strategy window
        ind_st = compute_all(flip_candles)
        ind_st["vwap"]  = flip_candles[-1]["close"] - 50
        ind_st["ema9"]  = flip_candles[-1]["close"] + 10
        ind_st["ema21"] = flip_candles[-1]["close"] - 10
        results["SUPERTREND_VWAP"] = await test_strategy(
            "SUPERTREND_VWAP", SupertrendVWAPStrategy("NIFTY"),
            flip_candles, ind_st
        )

    # ── 5. PDH_PDL ───────────────────────────────────────────────────────────
    # Need: pdh set, close > pdh for 2 bars, volume ok, time 9:30-13:00
    pdh = 24600.0; pdl = 24300.0
    pdh_candles = make_candles_at(10, 0, n=20, base=pdh + 40, trend=1.0, vol_spike_idx=18)
    for i in [-2, -1]:
        pdh_candles[i]["close"] = pdh + 55
        pdh_candles[i]["open"]  = pdh + 30
        pdh_candles[i]["volume"] = 200_000
    ind_pdh = compute_all(pdh_candles)
    ind_pdh["avg_volume"] = 80_000
    results["PDH_PDL"] = await test_strategy(
        "PDH_PDL", PDHPDLStrategy("NIFTY"),
        pdh_candles, ind_pdh, Direction.BUY,
        extra_setup=lambda s: s.set_levels(pdh=pdh, pdl=pdl)
    )

    # ── 6. VWAP_REVERSAL ─────────────────────────────────────────────────────
    # Need: pct > 0.8% above vwap, bearish candle (close < open), body_r > 0.5,
    #       RSI > 70, time 10:00-14:30, ATR gate passes.
    #
    # With vwap=24500: need close > 24500 * 1.008 = 24696.
    # Use vwap=24000, close=24210 → pct = 210/24000*100 = 0.875% ✓
    # Candle: open=24230, close=24210, high=24240, low=24200
    #   body=20, range=40, body_r=0.50 → exactly 0.5, NOT > 0.5
    # Fix: open=24235, close=24205, high=24240, low=24200
    #   body=30, range=40, body_r=0.75 ✓
    base_rev = 24500.0
    vwap_rev = 24000.0
    rev_candles = make_candles_at(11, 0, n=20, base=base_rev + 150, trend=0.0)
    rev_candles[-1]["open"]  = vwap_rev + 235   # open=24235 → bearish (open > close)
    rev_candles[-1]["close"] = vwap_rev + 205   # close=24205, pct=205/24000=0.854% > 0.8% ✓
    rev_candles[-1]["high"]  = vwap_rev + 240   # range = 240-200 = 40
    rev_candles[-1]["low"]   = vwap_rev + 200   # body = 235-205 = 30, body_r = 30/40 = 0.75 ✓
    ind_rev = compute_all(rev_candles)
    ind_rev["vwap"] = vwap_rev
    ind_rev["rsi"]  = 75.0
    results["VWAP_REVERSAL"] = await test_strategy(
        "VWAP_REVERSAL", VWAPReversalStrategy("NIFTY"),
        rev_candles, ind_rev, Direction.SELL
    )

    # ── 7. BOLLINGER_BREAKOUT ─────────────────────────────────────────────────
    # Need: squeeze = indicators["bb_width"] < avg_w_from_candles * 0.80
    #       AND close (from candles[-1]) > indicators["bb_upper"]
    #       AND close > indicators["vwap"]
    #
    # Strategy recomputes avg_w internally from the candle array.
    # Build: 40 bars with moderate noise (avg_w gets a real value),
    # then 20 calm bars (bb_w drops → squeeze), last calm close acts as
    # the "breakout" bar by setting bb_upper just below close in indicators.
    IST = timezone(timedelta(hours=5, minutes=30))
    bb_start = datetime.now(IST).replace(hour=10, minute=0, second=0, microsecond=0) \
               - timedelta(minutes=59)
    import numpy as _np2
    _np2.random.seed(7)
    base_bb = 24500.0
    bb_candles = []
    # Phase 1: 40 bars moderate noise — builds non-zero avg_w
    prices_bb = base_bb + _np2.cumsum(_np2.random.randn(40) * 2)
    for i, p in enumerate(prices_bb):
        ts = bb_start + timedelta(minutes=i)
        bb_candles.append({"timestamp": ts.isoformat(),
            "open": round(p, 2), "high": round(p + 8, 2),
            "low":  round(p - 8, 2), "close": round(p, 2), "volume": 80_000})
    # Phase 2: 20 calm bars — bb_w contracts into a squeeze
    last_p_bb = float(prices_bb[-1])
    for i in range(20):
        ts = bb_start + timedelta(minutes=40 + i)
        bb_candles.append({"timestamp": ts.isoformat(),
            "open": round(last_p_bb, 2), "high": round(last_p_bb + 1, 2),
            "low":  round(last_p_bb - 1, 2), "close": round(last_p_bb, 2),
            "volume": 80_000})
    ind_bb = compute_all(bb_candles)
    # Set bb_upper just below last close so close > bb_upper (breakout condition)
    ind_bb["bb_upper"] = last_p_bb - 2.0
    ind_bb["bb_lower"] = last_p_bb - 20.0
    ind_bb["vwap"]     = last_p_bb - 10.0   # close > vwap ✓
    # bb_width stays as computed (real squeeze value)
    results["BOLLINGER_BREAKOUT"] = await test_strategy(
        "BOLLINGER_BREAKOUT", BollingerBreakoutStrategy("NIFTY"),
        bb_candles, ind_bb, Direction.BUY
    )

    # ── 8. RSI_VWAP ──────────────────────────────────────────────────────────
    # Reversal BUY: RSI < 35, near vwap, bullish candle
    base_rsi = 24500.0
    rsi_candles = make_candles_at(11, 0, n=30, base=base_rsi, trend=-1.0)
    vwap_rsi = base_rsi - 20
    rsi_candles[-1]["close"] = vwap_rsi + 3    # near vwap
    rsi_candles[-1]["open"]  = vwap_rsi - 2    # bullish candle
    ind_rsi = compute_all(rsi_candles)
    ind_rsi["rsi"]  = 32.0
    ind_rsi["vwap"] = vwap_rsi
    results["RSI_VWAP"] = await test_strategy(
        "RSI_VWAP", RSIVWAPStrategy("NIFTY"),
        rsi_candles, ind_rsi, Direction.BUY
    )

    # ── 9. OI_SPIKE ──────────────────────────────────────────────────────────
    # Need: pe_chg > 25%, price > vwap + 0.15%, bullish 3-bar trend,
    #       supertrend UP, is_expiry_day=False, _update_count >= 2.
    #
    # Call update_oi() twice (required guard: _update_count < 2 blocks firing).
    # pe_chg = (1_300_000 - 1_000_000) / 1_000_000 * 100 = 30% > 25% ✓
    base_oi = 24500.0
    oi_candles = make_candles_at(10, 30, n=20, base=base_oi + 20, trend=2.0)
    # Force last 3 candles to be strictly ascending (trend_up check)
    for i, offset in [(-3, 20), (-2, 30), (-1, 40)]:
        oi_candles[i]["close"] = base_oi + offset
        oi_candles[i]["open"]  = base_oi + offset - 8
        oi_candles[i]["high"]  = base_oi + offset + 5
        oi_candles[i]["low"]   = base_oi + offset - 10
    ind_oi = compute_all(oi_candles)
    ind_oi["vwap"]          = base_oi + 5    # close=24540 > vwap=24505, gap=35/24505=0.14%... 
    ind_oi["vwap"]          = base_oi - 10   # close=24540, vwap=24490 → gap_pct=50/24490=0.204% > 0.15% ✓
    ind_oi["supertrend_dir"] = 1              # supertrend UP

    def _setup_oi(s: OISpikeStrategy):
        s.is_expiry_day = False
        # First update: seed prev values
        s.update_oi(1_000_000, 1_000_000)
        # Second update: PE spike +30% → triggers BUY
        s.update_oi(1_050_000, 1_300_000)

    results["OI_SPIKE"] = await test_strategy(
        "OI_SPIKE", OISpikeStrategy("NIFTY"),
        oi_candles, ind_oi, Direction.BUY,
        extra_setup=_setup_oi
    )

    # ── 10. IV_CRUSH ─────────────────────────────────────────────────────────
    # Need: atm_iv > 20, iv_rising (> prev * 1.05), close > vwap,
    #       bullish candle with body_ratio > 0.35,
    #       _iv_updated_at set (within 5 min staleness window),
    #       is_expiry_day = False.
    #
    # body = close - open, range = high - low
    # candle: open=24510, close=24550, high=24560, low=24505
    #   body=40, range=55, body_ratio=0.727 > 0.35 ✓
    base_iv = 24500.0
    iv_candles = make_candles_at(10, 0, n=25, base=base_iv, trend=0.5)
    iv_candles[-1]["open"]  = base_iv + 10
    iv_candles[-1]["close"] = base_iv + 50   # bullish, body=40
    iv_candles[-1]["high"]  = base_iv + 60   # range=55
    iv_candles[-1]["low"]   = base_iv + 5
    ind_iv = compute_all(iv_candles)
    ind_iv["vwap"] = base_iv + 20            # close=24550 > vwap=24520 ✓

    def _setup_iv(s: IVCrushStrategy):
        s.is_expiry_day  = False
        s.prev_atm_iv    = 19.0
        s.atm_iv         = 21.5   # > 20 ✓ and > 19*1.05=19.95 → iv_rising ✓
        s._iv_updated_at = datetime.now(timezone.utc)   # fresh — passes staleness guard

    results["IV_CRUSH"] = await test_strategy(
        "IV_CRUSH", IVCrushStrategy("NIFTY"),
        iv_candles, ind_iv, Direction.BUY,
        extra_setup=_setup_iv
    )

    # ── 11. CPR_VWAP ─────────────────────────────────────────────────────────
    # Need: close > TC, vwap > pivot, close > open (bullish),
    #       vol_surge (vol > avg_volume * 1.2), ATR gate passes.
    #
    # vol_surge check uses indicators["avg_volume"] when present.
    # Set avg_volume=80_000 and last candle volume=120_000 → 1.5× ✓
    pivot = 24500.0; bc = 24480.0; tc = 24520.0
    base_cpr = tc + 40
    cpr_candles = make_candles_at(10, 0, n=25, base=base_cpr, trend=0.3)
    cpr_candles[-1]["close"]  = tc + 50     # 24570 > TC=24520 ✓
    cpr_candles[-1]["open"]   = tc + 30     # bullish ✓
    cpr_candles[-1]["volume"] = 120_000     # 1.5× avg → vol_surge passes ✓
    ind_cpr = compute_all(cpr_candles)
    ind_cpr["vwap"]       = pivot + 30     # 24530 > pivot=24500 ✓
    ind_cpr["avg_volume"] = 80_000          # explicit baseline for _vol_surge

    def _setup_cpr(s: CPRVWAPStrategy):
        s.set_cpr(pivot=pivot, bc=bc, tc=tc)

    results["CPR_VWAP"] = await test_strategy(
        "CPR_VWAP", CPRVWAPStrategy("NIFTY"),
        cpr_candles, ind_cpr, Direction.BUY,
        extra_setup=_setup_cpr
    )

    # ── 12. GAP_VWAP_FILL ────────────────────────────────────────────────────
    # Need: gap down (day_open < prev_close * 0.997), price > vwap, bullish candle
    # Time window: 9:15-10:45
    prev_close = 24500.0
    day_open   = prev_close * 0.994   # -0.6% gap down
    IST = timezone(timedelta(hours=5, minutes=30))
    today_ist = datetime.now(IST).replace(
        hour=9, minute=15, second=0, microsecond=0
    )
    gap_candles = []
    price = day_open
    for i in range(20):
        ts = today_ist + timedelta(minutes=i)
        gap_candles.append({
            "timestamp": ts.isoformat(),
            "open":   round(price, 2),
            "high":   round(price + 5, 2),
            "low":    round(price - 3, 2),
            "close":  round(price + 2, 2),
            "volume": 90_000,
        })
        price += 3   # recovering from gap
    # Last candle: above vwap, bullish
    vwap_gap = day_open + 25
    gap_candles[-1]["close"] = vwap_gap + 10
    gap_candles[-1]["open"]  = vwap_gap - 5
    ind_gap = compute_all(gap_candles)
    ind_gap["vwap"] = vwap_gap

    def _setup_gap(s: GapVWAPFillStrategy):
        s.set_prev_close(prev_close)

    results["GAP_VWAP_FILL"] = await test_strategy(
        "GAP_VWAP_FILL", GapVWAPFillStrategy("NIFTY"),
        gap_candles, ind_gap, Direction.BUY,
        extra_setup=_setup_gap
    )

    return results


# ── Cooldown and aggregator tests ─────────────────────────────────────────────

async def test_cooldown():
    sep("14. Per-strategy cooldown blocks immediate re-fire")
    strat = ORBVWAPStrategy("NIFTY")
    strat._last_signal_time = datetime.now(timezone.utc)  # just fired
    strat._cooldown_minutes = 5

    candles = make_candles_at(10, 0, n=40)
    ind = compute_all(candles)
    ind["orb_high"] = candles[-1]["close"] - 100
    ind["orb_low"]  = candles[-1]["close"] - 200
    ind["vwap"]     = candles[-1]["close"] - 50
    ind["avg_volume"] = 10_000

    captured_signals.clear()
    await strat.on_candle(candles, ind)   # should be blocked by cooldown

    if not captured_signals:
        ok("Cooldown correctly blocked immediate re-signal")
        return True
    else:
        fail("Cooldown did NOT block — strategy fired twice immediately")
        return False


async def test_aggregator_dedup():
    sep("15. SignalAggregator deduplicates same-direction within 3 min")
    signal_aggregator.reset()
    captured_signals.clear()

    # Wire bus
    SignalBus._handlers = [_capture]

    strat = ORBVWAPStrategy("NIFTY")
    strat._last_signal_time = None

    candles = make_candles_at(10, 0, n=40, base=24500.0)
    orb_h = max(c["high"] for c in candles[:15])
    for i in range(-3, 0):
        candles[i]["close"]  = orb_h + 40
        candles[i]["open"]   = orb_h + 10
        candles[i]["volume"] = 300_000
    ind = compute_all(candles)
    ind["orb_high"]   = orb_h
    ind["orb_low"]    = 24300.0
    ind["vwap"]       = orb_h - 20
    ind["avg_volume"] = 80_000

    # First signal — should go through
    signal_aggregator.reset()
    await strat.on_candle(candles, ind)
    first = len(captured_signals)

    # Immediately try a second signal for same direction (reset strategy cooldown)
    strat._last_signal_time = None
    await strat.on_candle(candles, ind)
    second = len(captured_signals)

    if first == 1 and second == 1:  # second was blocked
        ok("Aggregator blocked duplicate BUY within 3-min window")
        return True
    elif first == 0:
        fail("First signal was blocked — aggregator too aggressive")
        return False
    else:
        fail(f"Aggregator let through {second} signals (expected 1)")
        return False


async def test_signal_fields():
    sep("16. Signal field correctness check")
    signal_aggregator.reset()
    SignalBus._handlers = [_capture]
    captured_signals.clear()

    strat = VWAPReversalStrategy("BANKNIFTY")
    strat._last_signal_time = None

    # Need: pct > 0.8% above vwap (SELL direction), bearish candle, body_r > 0.5, RSI > 70.
    # Use vwap=52000, close=52000+450=52450 → pct=450/52000*100=0.865% > 0.8% ✓
    # Candle: open=52480, close=52450, high=52490, low=52430
    #   body=30, range=60, body_r=0.5 → exactly 0.5, NOT > 0.5
    # Fix: open=52490, close=52440 → body=50, range=60, body_r=0.833 ✓
    base = 52000.0; vwap_v = base
    IST = timezone(timedelta(hours=5, minutes=30))
    fld_start = datetime.now(IST).replace(hour=10, minute=40, second=0, microsecond=0) - timedelta(minutes=19)
    fld_candles = []
    for i in range(20):
        ts = fld_start + timedelta(minutes=i)
        fld_candles.append({"timestamp": ts.isoformat(),
            "open": base + 400, "high": base + 450,
            "low": base + 380, "close": base + 420, "volume": 80_000})
    # Last candle: well above vwap, big bearish body, body_r > 0.5
    fld_candles[-1]["open"]  = vwap_v + 490   # bearish (open > close)
    fld_candles[-1]["close"] = vwap_v + 440   # pct = 440/52000*100 = 0.846% > 0.8% ✓
    fld_candles[-1]["high"]  = vwap_v + 495   # range = 495-435 = 60
    fld_candles[-1]["low"]   = vwap_v + 435   # body = 490-440 = 50, body_r = 50/60 = 0.833 ✓
    ind_fld = compute_all(fld_candles)
    ind_fld["vwap"] = vwap_v
    ind_fld["rsi"]  = 76.0

    await strat.on_candle(fld_candles, ind_fld)

    if not captured_signals:
        fail("No signal captured for VWAP_REVERSAL field check")
        return False

    s = captured_signals[0]
    all_ok = True

    checks = [
        ("strategy_name", s.strategy_name == "VWAP_REVERSAL"),
        ("underlying",    s.underlying    == "BANKNIFTY"),
        ("direction",     s.direction     == Direction.SELL),
        ("sl_pct",        s.sl_pct        == 0.15),      # current strategy value
        ("target_pct",    s.target_pct    == 0.30),      # current strategy value
        ("min_rr",        s.min_rr        == 2.0),
        ("spot_price > 0",s.spot_price    > 0),
        ("vwap set",      s.vwap is not None),
        ("signal_time set", s.signal_time is not None),
        ("notes non-empty", len(s.notes)  > 0),
    ]
    for label, passed in checks:
        if passed:
            ok(label)
        else:
            fail(label, f"got {getattr(s, label.split()[0], '?')!r}")
            all_ok = False

    return all_ok


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    print("\n" + "=" * 68)
    print("  STRATEGY TEST SUITE — all 12 strategies + bus + cooldowns")
    print("=" * 68)

    # Wire SignalBus once
    SignalBus._handlers = [_capture]

    # ── Indicator cache
    cache_ok, _ = test_compute_all()

    # ── All 12 strategies
    signal_aggregator.reset()
    results = await run_all_strategy_tests()

    # ── Cooldown / aggregator / field checks
    cooldown_ok   = await test_cooldown()
    aggregator_ok = await test_aggregator_dedup()
    fields_ok     = await test_signal_fields()

    # ── Summary ───────────────────────────────────────────────────────────────
    sep("SUMMARY")
    passed = sum(1 for v in results.values() if v)
    total  = len(results)

    for name, result in results.items():
        icon = PASS if result else FAIL
        print(f"  {icon}  {name}")

    infra = [
        ("compute_all() indicator cache", cache_ok),
        ("per-strategy cooldown",         cooldown_ok),
        ("SignalAggregator dedup",         aggregator_ok),
        ("SignalEvent field correctness",  fields_ok),
    ]
    print()
    for label, result in infra:
        icon = PASS if result else FAIL
        print(f"  {icon}  {label}")

    print()
    infra_passed = sum(1 for _, v in infra if v)
    print(f"  Strategies : {passed}/{total} fired signals")
    print(f"  Infrastructure : {infra_passed}/{len(infra)} checks passed")
    print()

    all_ok = (passed == total) and all(v for _, v in infra)
    if all_ok:
        print("  ✅  ALL TESTS PASSED")
    else:
        print(f"  ❌  {total - passed} strategy / {len(infra) - infra_passed} infra check(s) FAILED")
        print()
        print("  Failed strategies may need their candle setup tuned")
        print("  (indicators like supertrend depend on full price history).")
        print("  That does NOT mean the strategy logic is broken —")
        print("  it means the synthetic candles didn't produce the right")
        print("  indicator state.  Check the FAIL lines above for details.")

    print("=" * 68)


if __name__ == "__main__":
    asyncio.run(main())
