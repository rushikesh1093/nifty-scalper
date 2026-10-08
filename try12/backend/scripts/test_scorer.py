"""
Test: Hard filters + scoring model.
Demonstrates that a great NIFTY signal + bad option = NO TRADE.
"""
import sys
sys.path.insert(0, ".")

from app.services.trading.option_selector import (
    _hard_filter, _score_liquidity, _score_spread, _score_delta,
    _score_moneyness, _score_oi_change, _score_volume_trend, _score_expiry,
    HF_MAX_SPREAD_PCT, HF_MIN_OI, HF_MIN_VOLUME, HF_MIN_LTP, HF_MAX_LTP,
    HF_MAX_SLIPPAGE_PCT, HF_MIN_RR, MIN_SCORE,
    W_LIQUIDITY, W_SPREAD, W_DELTA, W_MONEYNESS,
    W_OI_CHANGE, W_VOLUME_TREND, W_EXPIRY, W_PRICE_BEHAV,
)
from datetime import date, timedelta

from app.runtime.context import bind
from app.runtime.live import script_context

bind(script_context())     # the scorer reads the ctx trading day (design §14.2)

SL_PCT     = 0.35   # ORB_VWAP strategy defaults
TARGET_PCT = 0.60

EXPIRY_7DTE = date.today() + timedelta(days=7)

print("=" * 70)
print("  HARD FILTER THRESHOLDS")
print("=" * 70)
print(f"  Spread max    : {HF_MAX_SPREAD_PCT}% of mid")
print(f"  OI min        : {HF_MIN_OI:,}")
print(f"  Volume min    : {HF_MIN_VOLUME:,}")
print(f"  LTP range     : ₹{HF_MIN_LTP} – ₹{HF_MAX_LTP}")
print(f"  Slippage max  : {HF_MAX_SLIPPAGE_PCT}%")
print(f"  Min R:R       : {HF_MIN_RR}:1")
print(f"  Min score     : {MIN_SCORE}/100")
print()

# ── Test cases: (description, ltp, bid, ask, oi, volume) ─────────────────────
cases = [
    # Good candidates
    ("✅ Good candidate",           103.0, 102.5, 103.5, 3_000_000, 28_000),
    ("✅ Good candidate (ATM)",     132.5, 132.0, 133.0, 4_200_000, 41_000),

    # Hard filter failures
    ("❌ Spread too wide",           80.0,  78.0,  84.0,   800_000,  8_000),   # spread=7.3%
    ("❌ OI too low",                55.0,  54.5,  55.5,    60_000,  3_500),   # OI < 1L
    ("❌ Volume insufficient",       90.0,  89.5,  90.5,   500_000,  2_000),   # vol < 5k
    ("❌ LTP too cheap",              8.0,   7.5,   8.5,   300_000,  6_000),   # LTP < ₹15
    ("❌ LTP too expensive",        650.0, 649.0, 651.0, 1_000_000, 12_000),  # LTP > ₹600
    ("❌ Slippage too high",         50.0,  47.0,  54.0,   400_000,  7_000),   # spread=13.6%
]

print("=" * 70)
print(f"  {'Description':<35}  {'Result':<10}  {'Filter'}")
print("=" * 70)
for desc, ltp, bid, ask, oi, vol in cases:
    result = _hard_filter(ltp=ltp, bid=bid, ask=ask, oi=oi, volume=vol,
                          sl_pct=SL_PCT, target_pct=TARGET_PCT)
    if result.passed:
        print(f"  {desc:<35}  PASSED")
    else:
        print(f"  {desc:<35}  REJECTED  [{result.filter}]")
        print(f"  {'':35}  → {result.reason[:60]}")

print()
print("=" * 70)
print("  SCORING TABLE — NIFTY BUY signal, spot=24500")
print("=" * 70)

SPOT = 24500.0
STEP = 50

# All these pass hard filters — show scoring
good_candidates = [
    # (strike, ltp, bid, ask, oi, oi_change, volume, iv)
    (24400, 210.5, 209.0, 211.5, 2_100_000,  80_000, 28_000, 14.2),
    (24450, 168.0, 167.0, 169.0, 3_400_000, 120_000, 32_000, 13.9),
    (24500, 132.5, 132.0, 133.0, 4_200_000, 150_000, 41_000, 13.5),
    (24550, 103.0, 102.5, 103.5, 2_800_000,  95_000, 29_000, 13.8),
    (24600,  80.5,  79.5,  81.5, 1_200_000,  40_000, 18_000, 14.1),
    (24650,  60.0,  59.0,  61.0,   500_000,  15_000,  8_000, 14.5),
    # Bad ones mixed in — these will be filtered
    (24700,  12.0,  10.0,  15.0,    50_000,   1_000,  1_000, 18.0),  # cheap+illiquid
    (24350, 260.0, 258.0, 263.0,   900_000,  30_000, 15_000, 14.8),  # spread ok, but deep ITM
]

print(f"\n  {'Strike':>8}  {'LTP':>7}  {'Delta':>6}  {'OI':>9}  "
      f"{'Score':>9}  {'Status'}")
print("  " + "-" * 72)

scored = []
for strike, ltp, bid, ask, oi, oi_chg, vol, iv in good_candidates:
    hf = _hard_filter(ltp=ltp, bid=bid, ask=ask, oi=oi, volume=vol,
                      sl_pct=SL_PCT, target_pct=TARGET_PCT)
    if not hf.passed:
        print(f"  {strike:>8.0f}  {ltp:>7.2f}  {'n/a':>6}  {oi:>9,}  "
              f"  {'---':>9}  HARD FILTER: {hf.filter}")
        continue

    s_liq    = _score_liquidity(oi, vol)
    s_spread = _score_spread(bid, ask, ltp)
    s_delta, dval = _score_delta(SPOT, strike, "CE", 7, iv / 100)
    s_money  = _score_moneyness(strike, SPOT, STEP, "CE")
    s_oi_chg = _score_oi_change(oi, oi_chg)
    s_voltrd = _score_volume_trend(vol, vol * 0.7)
    s_expiry = _score_expiry(EXPIRY_7DTE)
    s_pbehav = 60.0

    score = (
        W_LIQUIDITY    * s_liq    +
        W_SPREAD       * s_spread +
        W_DELTA        * s_delta  +
        W_MONEYNESS    * s_money  +
        W_OI_CHANGE    * s_oi_chg +
        W_VOLUME_TREND * s_voltrd +
        W_EXPIRY       * s_expiry +
        W_PRICE_BEHAV  * s_pbehav
    )

    status = "SOFT FILTERED (<40)" if score < MIN_SCORE else "OK"
    scored.append((strike, ltp, dval, oi, score, status))

# Sort by score
scored.sort(key=lambda x: x[4], reverse=True)
for i, (strike, ltp, dval, oi, score, status) in enumerate(scored):
    marker = "  ← SELECTED" if i == 0 and status == "OK" else ""
    disp_status = status + marker
    print(f"  {strike:>8.0f}  {ltp:>7.2f}  {dval:>6.3f}  {oi:>9,}  "
          f"{score:>7.1f}/100  {disp_status}")

print()
if scored and scored[0][5] == "OK":
    best_strike, best_ltp = scored[0][0], scored[0][1]
    sl  = round(best_ltp * (1 - SL_PCT), 1)
    tgt = round(best_ltp + (best_ltp - sl) * HF_MIN_RR, 1)
    print(f"  TRADE: {best_strike:.0f} CE @ ₹{best_ltp}")
    print(f"  SL     = ₹{sl}  ({SL_PCT*100:.0f}% below LTP)")
    print(f"  Target = ₹{tgt}  (R:R = {HF_MIN_RR}:1)")
    print(f"  Max loss / lot   = ₹{round((best_ltp - sl) * 50):.0f}")
    print(f"  Max profit / lot = ₹{round((tgt - best_ltp) * 50):.0f}")
else:
    print("  NO TRADE — no candidates passed all filters")
