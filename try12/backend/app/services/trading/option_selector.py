"""
Option Selector Engine — Hard Filters + Multi-Factor Scoring Model
===================================================================

Architecture:
  Signal arrives
       ↓
  HARD FILTERS  (binary — any fail = NO TRADE, reason logged)
  ─────────────────────────────────────────────────────────────
  1. Spread > 3.0% of mid        → REJECT: spread too wide
  2. OI < 50,000                 → REJECT: liquidity too low
  3. Volume < 1,000              → REJECT: volume insufficient
  4. LTP < ₹10 or > ₹700        → REJECT: contract unsuitable
  5. Expected slippage > 2.0%   → REJECT: slippage too high
  6. R:R < 1.5 (after stretch)   → REJECT: risk/reward unacceptable
  ─────────────────────────────────────────────────────────────
       ↓
  SOFT SCORING  (0–100 composite, 8 factors)
       ↓
  SELECT best scoring candidate (if score ≥ MIN_SCORE=40)
       ↓
  NO TRADE if all candidates filtered/scored-out

Principle:
  Great NIFTY signal + Bad option → NO TRADE
  Mediocre NIFTY signal + Great option → STILL NO TRADE (signal gate upstream)

Scoring factors (weights sum to 1.0):
  Factor           Weight   Measures
  ──────────────────────────────────────────────────────────────────
  Liquidity         0.20    OI + Volume — clean entry/exit
  Spread            0.18    Bid-ask % — execution cost
  Delta             0.18    0.35–0.50 ideal for scalping
  Moneyness         0.15    1–2 strikes OTM preferred
  OI change         0.12    Rising OI = fresh positioning = conviction
  Volume trend      0.08    vs recent average
  Expiry            0.06    2–7 DTE sweet spot
  Price behavior    0.03    LTP momentum direction
  ──────────────────────────────────────────────────────────────────
"""
import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.models.instrument import Instrument, InstrumentType, OptionType
from app.services.cache.redis_client import redis_cache
from app.strategies.base import Direction

logger = logging.getLogger(__name__)

# ── Scoring weights ────────────────────────────────────────────────────────────
W_LIQUIDITY    = 0.20
W_SPREAD       = 0.18
W_DELTA        = 0.18
W_MONEYNESS    = 0.15
W_OI_CHANGE    = 0.12
W_VOLUME_TREND = 0.08
W_EXPIRY       = 0.06
W_PRICE_BEHAV  = 0.03

assert abs(W_LIQUIDITY + W_SPREAD + W_DELTA + W_MONEYNESS +
           W_OI_CHANGE + W_VOLUME_TREND + W_EXPIRY + W_PRICE_BEHAV - 1.0) < 1e-9

# ── Thresholds — Soft scoring ─────────────────────────────────────────────────
MIN_LTP          = 10.0       # ₹10 minimum premium (matches hard filter)
MAX_LTP          = 600.0      # ₹600 maximum premium
MIN_SCORE        = 40.0       # Minimum composite score to be eligible
OI_GOOD          = 5_00_000   # 5 lakh OI = excellent liquidity
OI_OK            = 1_00_000   # 1 lakh OI = acceptable
VOL_GOOD         = 30_000     # 30k contracts = good volume
NIFTY_STEP       = 50         # Nifty strike gap
BANKNIFTY_STEP   = 100        # BankNifty strike gap

# ── Hard filter thresholds (binary pass/fail) ─────────────────────────────────
HF_MAX_SPREAD_PCT    = 3.0     # Reject if (ask-bid)/mid > 3%  (was 2% — too tight pre-10am)
HF_MIN_OI            = 50_000  # Reject if OI < 50k  (was 1L — too strict early morning)
HF_MIN_VOLUME        = 1_000   # Reject if volume < 1,000  (was 5k — unreachable early-day)
HF_MIN_LTP           = 10.0   # Reject if LTP < ₹10  (was ₹15 — excludes valid deep OTM)
HF_MAX_LTP           = 700.0  # Reject if LTP > ₹700 for NIFTY; BankNifty uses 1500 (see below)
HF_MAX_LTP_BANKNIFTY = 1500.0  # BankNifty monthly expiry: near-ATM options routinely > ₹700
HF_MAX_SLIPPAGE_PCT  = 2.0    # Reject if expected slippage > 2% of LTP  (was 1.5%)
HF_MIN_RR            = 1.5    # Reject if R:R < 1.5 (was 2.0 — PaperEngine enforces its own)


@dataclass
class FilterResult:
    """Result of hard filter check — passed or rejected with reason."""
    passed:  bool
    reason:  str = ""          # populated when passed=False
    filter:  str = ""          # which filter triggered the rejection


def _hard_filter(
    ltp:     float,
    bid:     float,
    ask:     float,
    oi:      int,
    volume:  int,
    sl_pct:  float,
    target_pct: float,
    underlying: str = "NIFTY",   # needed to apply the correct LTP ceiling
) -> FilterResult:
    """
    Run all 6 hard filters in order.
    Returns FilterResult(passed=True) if all pass.
    Returns FilterResult(passed=False, reason=..., filter=...) on first failure.

    These are BINARY gates — if any fails the option is rejected
    regardless of how well it scores on other factors.
    """

    # ── Filter 1: Contract suitability (LTP range) ────────────────────────────
    max_ltp = HF_MAX_LTP_BANKNIFTY if underlying.upper() == "BANKNIFTY" else HF_MAX_LTP
    if ltp < HF_MIN_LTP:
        return FilterResult(
            passed=False,
            filter="CONTRACT_UNSUITABLE",
            reason=f"LTP ₹{ltp:.2f} < minimum ₹{HF_MIN_LTP} — too cheap, "
                   f"gamma risk too high near expiry",
        )
    if ltp > max_ltp:
        return FilterResult(
            passed=False,
            filter="CONTRACT_UNSUITABLE",
            reason=f"LTP ₹{ltp:.2f} > maximum ₹{max_ltp} — too expensive, "
                   f"can't achieve 2× target within reasonable move",
        )

    # ── Filter 2: Bid-ask spread ──────────────────────────────────────────────
    if bid > 0 and ask > bid:
        mid = (bid + ask) / 2.0
        spread_pct = (ask - bid) / mid * 100.0
        if spread_pct > HF_MAX_SPREAD_PCT:
            return FilterResult(
                passed=False,
                filter="SPREAD_TOO_WIDE",
                reason=f"Spread {spread_pct:.2f}% > limit {HF_MAX_SPREAD_PCT}% — "
                       f"bid={bid:.2f} ask={ask:.2f} mid={mid:.2f}. "
                       f"Entry cost too high.",
            )
    # If bid/ask are both zero (paper mode / feed gap) we skip the spread filter
    # rather than rejecting — the option may still be valid on LTP alone.

    # ── Filter 3: OI (liquidity) ──────────────────────────────────────────────
    # oi == -1 is a sentinel meaning "not yet received from feed" — skip filter.
    if oi >= 0 and oi < HF_MIN_OI:
        return FilterResult(
            passed=False,
            filter="LIQUIDITY_TOO_LOW",
            reason=f"OI {oi:,} < minimum {HF_MIN_OI:,} — insufficient "
                   f"open interest, exit may be difficult",
        )

    # ── Filter 4: Volume ──────────────────────────────────────────────────────
    # volume == -1 is a sentinel meaning "not yet received from feed" — skip filter.
    if volume >= 0 and volume < HF_MIN_VOLUME:
        return FilterResult(
            passed=False,
            filter="VOLUME_INSUFFICIENT",
            reason=f"Volume {volume:,} < minimum {HF_MIN_VOLUME:,} — "
                   f"low participation, wide fills expected",
        )

    # ── Filter 5: Expected slippage ───────────────────────────────────────────
    # Slippage approximation: half the spread as market impact
    if bid > 0 and ask > bid:
        mid = (bid + ask) / 2.0
        expected_slippage_pct = ((ask - mid) / mid) * 100.0   # one-way
        if expected_slippage_pct > HF_MAX_SLIPPAGE_PCT:
            return FilterResult(
                passed=False,
                filter="SLIPPAGE_TOO_HIGH",
                reason=f"Expected slippage {expected_slippage_pct:.2f}% > "
                       f"limit {HF_MAX_SLIPPAGE_PCT}% — "
                       f"market impact will erode edge",
            )

    # ── Filter 6: Risk/reward ─────────────────────────────────────────────────
    sl     = ltp * (1.0 - sl_pct)
    target = ltp * (1.0 + target_pct)
    rr     = (target - ltp) / max(ltp - sl, 0.01)
    if rr < HF_MIN_RR:
        # Try stretching target
        target_stretched = ltp + (ltp - sl) * HF_MIN_RR
        rr_stretched = (target_stretched - ltp) / max(ltp - sl, 0.01)
        if rr_stretched < HF_MIN_RR:
            return FilterResult(
                passed=False,
                filter="RR_INSUFFICIENT",
                reason=f"R:R {rr:.2f} < minimum {HF_MIN_RR} even after target "
                       f"adjustment. ltp={ltp:.2f} sl={sl:.2f} target={target:.2f}",
            )

    return FilterResult(passed=True)


@dataclass
class ScoredCandidate:
    """One scored option candidate with all factor breakdown."""
    token:        str
    symbol:       str
    strike:       float
    option_type:  str
    expiry:       date
    ltp:          float
    bid:          float
    ask:          float
    spread_pct:   float
    oi:           int
    oi_change:    int
    volume:       int
    delta_est:    float
    iv:           float
    lot_size:     int

    # Score breakdown
    score:           float
    score_liquidity: float = 0.0
    score_spread:    float = 0.0
    score_delta:     float = 0.0
    score_moneyness: float = 0.0
    score_oi_change: float = 0.0
    score_vol_trend: float = 0.0
    score_expiry:    float = 0.0
    score_price_beh: float = 0.0

    # Hard filter result (always passed=True for candidates that reach scoring)
    filter_result: FilterResult = None

    @property
    def selected(self) -> bool:
        return self.score >= MIN_SCORE


# Keep SelectedOption as alias for backward compatibility
SelectedOption = ScoredCandidate


# ── Black-Scholes delta approximation ─────────────────────────────────────────

def _estimate_delta(spot: float, strike: float, option_type: str,
                    dte: int, iv: float = 0.20) -> float:
    """
    Approximate B-S delta using normal CDF.
    iv is already in decimal (0.20 = 20%).
    """
    if dte <= 0 or iv <= 0:
        if option_type == "CE":
            return 1.0 if spot > strike else 0.05
        else:
            return -1.0 if spot < strike else -0.05

    T   = max(dte / 365.0, 1 / 365.0)
    sqT = math.sqrt(T)
    try:
        d1 = (math.log(spot / strike) + (0.5 * iv ** 2) * T) / (iv * sqT)
    except (ValueError, ZeroDivisionError):
        return 0.5

    def _ncdf(x: float) -> float:
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    delta = _ncdf(d1) if option_type == "CE" else _ncdf(d1) - 1.0
    return round(delta, 4)


# ── Individual factor scorers (each returns 0–100) ────────────────────────────

def _score_liquidity(oi: int, volume: int) -> float:
    """
    Nifty options need real liquidity to enter and exit cleanly.
    Scale:
      OI ≥ 5L  → 100     (very liquid)
      OI ≥ 1L  → 60-100
      OI < 1L  → 0-60    (illiquid, penalised)
    Volume adds a smaller boost.
    """
    oi_score  = min(oi  / OI_GOOD,  1.0) * 100
    vol_score = min(volume / VOL_GOOD, 1.0) * 100
    return round(oi_score * 0.65 + vol_score * 0.35, 2)


def _score_spread(bid: float, ask: float, ltp: float) -> float:
    """
    Tight spread = lower execution cost = better score.
    Scale:
      spread < 0.3%  → 100
      spread = 1%    → 72
      spread = 2%    → 44
      spread ≥ 5%    → 0
    Formula: score = 100 - (spread_pct / 0.05) capped at 100
    """
    if not bid or not ask or ask <= bid:
        return 40.0   # unknown spread — moderate penalty
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return 40.0
    spread_pct = (ask - bid) / mid * 100.0
    score = max(0.0, 100.0 - spread_pct * 20.0)   # 5% spread → 0
    return round(score, 2)


def _score_delta(spot: float, strike: float, option_type: str,
                 dte: int, iv: float) -> tuple[float, float]:
    """
    Ideal delta for scalping: 0.35–0.50
      - Too low (<0.25): deeply OTM — slow price action, lottery ticket
      - Too high (>0.65): deep ITM — expensive, low leverage
    Returns (score, delta_value).
    """
    delta = abs(_estimate_delta(spot, strike, option_type, dte, iv))
    if 0.35 <= delta <= 0.50:
        score = 100.0
    elif 0.25 <= delta < 0.35:
        # Linear interpolation 0→100 as delta approaches 0.35
        score = ((delta - 0.25) / 0.10) * 100.0
    elif 0.50 < delta <= 0.65:
        # Linear decay 100→60 from 0.50 to 0.65
        score = 100.0 - ((delta - 0.50) / 0.15) * 40.0
    elif delta > 0.65:
        # Further decay for deep ITM
        score = max(0.0, 60.0 - ((delta - 0.65) / 0.35) * 60.0)
    else:
        # delta < 0.25: steep penalty
        score = max(0.0, (delta / 0.25) * 50.0)
    return round(score, 2), round(delta, 4)


def _score_moneyness(strike: float, spot: float, step: int,
                     option_type: str = "CE") -> float:
    """
    For scalping we want strikes that are:
      • 1 strike OTM:  100  (sweet spot — good delta + leverage)
      • ATM:           85   (too expensive, lower leverage)
      • 2 strikes OTM: 80
      • 3 strikes OTM: 55
      • 4+ strikes OTM: decaying rapidly

    OTM direction depends on option type:
      CE: strike > spot is OTM  (higher strike = more OTM)
      PE: strike < spot is OTM  (lower strike = more OTM)
    """
    if option_type == "CE":
        strikes_otm = (strike - spot) / step   # positive = OTM for CE
    else:
        strikes_otm = (spot - strike) / step   # positive = OTM for PE

    strikes_otm_abs = abs(strikes_otm)
    # ITM (strikes_otm < 0): penalise — expensive, low leverage
    if strikes_otm < 0:
        return max(0.0, 60.0 + strikes_otm * 15.0)   # deeper ITM → lower score

    if strikes_otm_abs == 0:         # ATM
        return 85.0
    elif strikes_otm_abs <= 1:
        return 100.0
    elif strikes_otm_abs <= 2:
        return 80.0
    elif strikes_otm_abs <= 3:
        return 55.0
    elif strikes_otm_abs <= 4:
        return 30.0
    else:
        return max(0.0, 30.0 - (strikes_otm_abs - 4) * 10)


def _score_oi_change(oi: int, oi_change: int) -> float:
    """
    Rising OI = fresh money entering = conviction.
    Falling OI = unwinding = weaker signal.

    Score:
      OI increasing >10%  → 100
      OI stable           → 60
      OI declining >10%   → 20
    """
    if oi <= 0:
        return 50.0   # no data
    pct_change = (oi_change / oi) * 100.0 if oi > 0 else 0.0

    if pct_change >= 10.0:
        return 100.0
    elif pct_change >= 2.0:
        return 60.0 + (pct_change - 2.0) / 8.0 * 40.0
    elif pct_change >= 0:
        return 60.0
    elif pct_change >= -5.0:
        return 60.0 + pct_change * 4.0   # 60 → 40
    else:
        return max(20.0, 40.0 + (pct_change + 5.0) * 4.0)


def _score_volume_trend(volume: int, avg_volume: float) -> float:
    """
    Elevated volume = increased participation = better fill quality.
    Score based on volume / average:
      2× average → 100
      1× average → 70
      0.5× average → 40
      0× → 0
    """
    if avg_volume <= 0:
        return 50.0
    ratio = volume / avg_volume
    if ratio >= 2.0:
        return 100.0
    elif ratio >= 1.0:
        return 70.0 + (ratio - 1.0) * 30.0
    else:
        return max(0.0, ratio * 70.0)


def _score_expiry(expiry: date) -> float:
    """
    Weekly expiry 2-7 DTE is ideal for scalping:
      - Max theta decay per day
      - Biggest intraday move per point of NIFTY

    DTE 1:     25  (expiry day AM only — not afternoon)
    DTE 2-3:   90
    DTE 4-7:   100 (sweet spot)
    DTE 8-14:  75
    DTE 15-30: 55
    DTE >30:   30  (monthly expiry — slow theta)
    """
    dte = max((expiry - datetime.now(timezone(timedelta(hours=5, minutes=30))).date()).days, 0)
    if dte == 0:
        return 10.0   # expiry day — avoid unless before noon
    elif dte == 1:
        return 25.0
    elif dte <= 3:
        return 90.0
    elif dte <= 7:
        return 100.0
    elif dte <= 14:
        return 75.0
    elif dte <= 30:
        return 55.0
    else:
        return 30.0


def _score_price_behavior(ltp: float, ltp_history: list[float]) -> float:
    """
    Is the option price trending in our favour?
    For CE: we want LTP rising (underlying going up).
    We compare last LTP to average of recent ticks.

    ltp_history: list of recent LTPs (last 3-5 ticks), newest last.
    """
    if not ltp_history or len(ltp_history) < 2:
        return 50.0   # no data

    recent_avg = sum(ltp_history[:-1]) / len(ltp_history[:-1])
    if recent_avg <= 0:
        return 50.0

    pct_change = (ltp - recent_avg) / recent_avg * 100.0
    # Positive = option rising = momentum
    if pct_change > 1.0:
        return 100.0
    elif pct_change > 0:
        return 50.0 + pct_change * 50.0
    elif pct_change >= -1.0:
        return 50.0 + pct_change * 30.0
    else:
        return max(0.0, 50.0 + pct_change * 20.0)


# ── Main scorer ────────────────────────────────────────────────────────────────

def score_candidate(
    inst:        Instrument,
    spot:        float,
    tick:        dict,
    expiry:      date,
    direction:   Direction,
    step:        int,
    ltp_history: list[float] = None,
    prev_oi:     int = 0,
    avg_volume:  float = 0.0,   # rolling average volume from Redis; 0 = unknown
    sl_pct:      float = 0.30,
    target_pct:  float = 0.55,
) -> Optional[ScoredCandidate]:
    """
    Score a single option candidate.
    Runs 6 hard filters first — returns None if any filter fails.
    Returns ScoredCandidate only if all filters pass.
    """
    ltp    = float(tick.get("ltp")       or 0)
    bid    = float(tick.get("best_bid")  or 0)
    ask    = float(tick.get("best_ask")  or 0)
    # -1 sentinel = "not received from feed yet" — pass through to _hard_filter
    # which will skip the OI/volume filters for that sentinel value.
    _oi_raw     = tick.get("oi")
    _vol_raw    = tick.get("volume")
    oi          = int(_oi_raw)    if _oi_raw    is not None else -1
    volume      = int(_vol_raw)   if _vol_raw   is not None else -1
    iv_raw = float(tick.get("iv")        or 0)
    # Normalise IV to percentage form (20.0 for 20%).
    # Tick data from WebSocket may be decimal (0.20); chain_iv is already percent.
    iv = iv_raw * 100 if (0 < iv_raw < 1.0) else iv_raw

    opt_type_str = inst.option_type.value if inst.option_type else "CE"

    # ── STEP 1: Hard filters — binary pass/fail ───────────────────────────────
    hf = _hard_filter(
        ltp=ltp, bid=bid, ask=ask, oi=oi, volume=volume,
        sl_pct=sl_pct, target_pct=target_pct,
        underlying=inst.name or "NIFTY",
    )
    if not hf.passed:
        logger.warning(
            "[HardFilter] REJECTED %s %.0f%s  ltp=%.2f bid=%.2f ask=%.2f "
            "oi=%s vol=%s | [%s] %s",
            inst.symbol, inst.strike, opt_type_str, ltp, bid, ask,
            f"{oi:,}", f"{volume:,}", hf.filter, hf.reason,
        )
        return None

    # ── STEP 2: Soft scoring ──────────────────────────────────────────────────
    iv_decimal = (iv or 20.0) / 100.0
    dte        = max((expiry - datetime.now(timezone(timedelta(hours=5, minutes=30))).date()).days, 0)
    # Normalise sentinel -1 → 0 for scoring (gives low but valid score)
    oi_score   = max(oi, 0)
    vol_score  = max(volume, 0)
    oi_change  = oi_score - prev_oi

    # ── Compute all 8 factor scores ─────────────────────────────────────────
    s_liq    = _score_liquidity(oi_score, vol_score)
    s_spread = _score_spread(bid, ask, ltp)
    s_delta, delta_val = _score_delta(spot, inst.strike, opt_type_str, dte, iv_decimal)
    s_money  = _score_moneyness(inst.strike, spot, step, opt_type_str)
    s_oi_chg = _score_oi_change(oi_score, oi_change)
    s_voltrd = _score_volume_trend(vol_score, avg_volume)
    s_expiry = _score_expiry(expiry)
    s_pbehav = _score_price_behavior(ltp, ltp_history or [])

    # ── Weighted composite ──────────────────────────────────────────────────
    composite = (
        W_LIQUIDITY    * s_liq    +
        W_SPREAD       * s_spread +
        W_DELTA        * s_delta  +
        W_MONEYNESS    * s_money  +
        W_OI_CHANGE    * s_oi_chg +
        W_VOLUME_TREND * s_voltrd +
        W_EXPIRY       * s_expiry +
        W_PRICE_BEHAV  * s_pbehav
    )

    mid = (bid + ask) / 2.0 if bid and ask else ltp
    spread_pct = round((ask - bid) / mid * 100, 2) if mid > 0 and ask > bid else 0.0

    return ScoredCandidate(
        token        = inst.token,
        symbol       = inst.symbol,
        strike       = inst.strike,
        option_type  = opt_type_str,
        expiry       = expiry,
        ltp          = round(ltp, 2),
        bid          = round(bid, 2),
        ask          = round(ask, 2),
        spread_pct   = spread_pct,
        oi           = oi_score,
        oi_change    = oi_change,
        volume       = vol_score,
        delta_est    = delta_val,
        iv           = round(iv or 20.0, 2),
        lot_size     = inst.lot_size,
        score        = round(composite, 2),
        score_liquidity = round(s_liq,    2),
        score_spread    = round(s_spread, 2),
        score_delta     = round(s_delta,  2),
        score_moneyness = round(s_money,  2),
        score_oi_change = round(s_oi_chg, 2),
        score_vol_trend = round(s_voltrd, 2),
        score_expiry    = round(s_expiry, 2),
        score_price_beh = round(s_pbehav, 2),
    )


# ── OptionSelector class ───────────────────────────────────────────────────────

class OptionSelector:
    """
    Evaluates all CE/PE candidates for a given signal and selects the best.
    Logs the full scoring table so you can audit every decision.
    """

    async def select(
        self,
        db:          AsyncSession,
        direction:   Direction,
        underlying:  str,
        spot_price:  float,
        expiry:      Optional[date] = None,
        min_ltp:     float = MIN_LTP,
        max_ltp:     float = MAX_LTP,
        sl_pct:      float = 0.30,
        target_pct:  float = 0.55,
    ) -> Optional[ScoredCandidate]:
        """
        Main entry point — returns the single best option.
        direction = BUY → CE,  SELL → PE.
        Returns None (NO TRADE) if no candidate passes all hard filters.
        """
        candidates, rejected, no_tick_count = await self._build_candidates(
            db, direction, underlying, spot_price, expiry,
            min_ltp, max_ltp, sl_pct, target_pct,
        )

        if not candidates:
            logger.warning(
                "[OptionSelector] ❌ NO TRADE — %s %s spot=%.1f | "
                "%d candidates all REJECTED by hard filters  "
                "(no_tick_fallbacks=%d — if high, option LTPs not in Redis yet)",
                underlying, direction, spot_price, rejected, no_tick_count,
            )
            return None

        candidates.sort(key=lambda c: c.score, reverse=True)
        self._log_table(candidates, underlying, rejected)

        best = candidates[0]
        logger.info(
            "[OptionSelector] ✅ SELECTED %s %.0f%s | "
            "score=%.1f/100  ltp=%.2f  δ=%.3f  iv=%.1f%%  "
            "spread=%.2f%%  oi=%s  (%d passed / %d rejected filters)",
            underlying, best.strike, best.option_type,
            best.score, best.ltp, best.delta_est, best.iv,
            best.spread_pct, f"{best.oi:,}",
            len(candidates), rejected,
        )
        return best

    async def select_top_n(
        self,
        db:         AsyncSession,
        direction:  Direction,
        underlying: str,
        spot_price: float,
        n:          int = 3,
        sl_pct:     float = 0.30,
        target_pct: float = 0.55,
    ) -> list[ScoredCandidate]:
        """Return top N scored candidates (for frontend option selector widget)."""
        candidates, _, _ntc = await self._build_candidates(
            db, direction, underlying, spot_price,
            sl_pct=sl_pct, target_pct=target_pct,
        )
        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates[:n]

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _build_candidates(
        self,
        db:         AsyncSession,
        direction:  Direction,
        underlying: str,
        spot_price: float,
        expiry:     Optional[date] = None,
        min_ltp:    float = MIN_LTP,
        max_ltp:    float = MAX_LTP,
        sl_pct:     float = 0.30,
        target_pct: float = 0.55,
    ) -> tuple[list[ScoredCandidate], int]:
        """
        Build and score all candidates. Returns (passed_list, rejected_count, no_tick_count).
        rejected_count  = number of candidates that failed hard filters or soft score.
        no_tick_count   = number of candidates that had no tick in Redis (LTP-only fallback).
        """

        option_type_enum = (
            OptionType.CE if direction == Direction.BUY else OptionType.PE
        )
        step = NIFTY_STEP if underlying.upper() == "NIFTY" else BANKNIFTY_STEP

        # Resolve expiry
        if not expiry:
            exp_result = await db.execute(
                select(Instrument.expiry)
                .where(
                    Instrument.name             == underlying.upper(),
                    Instrument.instrument_type  == InstrumentType.OPTIDX,
                    Instrument.option_type      == option_type_enum,
                    Instrument.is_active        == True,
                    Instrument.expiry           >= datetime.now(timezone(timedelta(hours=5, minutes=30))).date(),
                )
                .distinct()
                .order_by(Instrument.expiry)
                .limit(1)
            )
            expiry = exp_result.scalar()
            if not expiry:
                return [], 0

        # Fetch strikes: ATM ± 5 strikes
        atm  = round(spot_price / step) * step
        low  = atm - 5 * step
        high = atm + 5 * step

        inst_result = await db.execute(
            select(Instrument).where(
                Instrument.name             == underlying.upper(),
                Instrument.instrument_type  == InstrumentType.OPTIDX,
                Instrument.option_type      == option_type_enum,
                Instrument.expiry           == expiry,
                Instrument.is_active        == True,
                Instrument.strike           >= low,
                Instrument.strike           <= high,
            ).order_by(Instrument.strike)
        )
        instruments = inst_result.scalars().all()
        if not instruments:
            return [], 0

        # Fetch IV from option chain cache (best-effort)
        chain_iv: dict[str, float] = {}
        chain = await redis_cache.get_option_chain(underlying, str(expiry))
        if chain:
            for s in chain.get("strikes", []):
                if s.get("token") and s.get("iv"):
                    raw_iv = float(s["iv"])
                    # Normalise to percentage form (e.g. 20.0 for 20%).
                    # AngelOne may return either decimal (0.20) or percent (20.0).
                    # Values < 1 are treated as decimal and multiplied by 100.
                    chain_iv[s["token"]] = raw_iv * 100 if raw_iv < 1.0 else raw_iv

        # Fetch previous OI snapshot from Redis for OI-change scoring
        prev_oi_map: dict[str, int] = {}
        prev_snap_key = f"prev_oi:{underlying}:{expiry}"
        prev_snap = await redis_cache.get(prev_snap_key)
        if prev_snap and isinstance(prev_snap, dict):
            prev_oi_map = {k: int(v) for k, v in prev_snap.items()}

        # Fetch rolling average volume from Redis for volume-trend scoring
        avg_vol_key = f"avg_vol:{underlying}:{expiry}"
        avg_vol_snap = await redis_cache.get(avg_vol_key)
        avg_vol_map: dict[str, float] = {}
        if avg_vol_snap and isinstance(avg_vol_snap, dict):
            avg_vol_map = {k: float(v) for k, v in avg_vol_snap.items()}

        candidates: list[ScoredCandidate] = []
        rejected = 0
        no_tick_count = 0

        for inst in instruments:
            tick = await redis_cache.get_tick(inst.token)
            if not tick:
                ltp_val = await redis_cache.get_ltp(inst.token)
                if not ltp_val:
                    rejected += 1
                    continue
                # We have LTP but no full tick (bid/ask/OI/volume not yet received).
                # Use sentinel value -1 for OI and volume so _hard_filter knows
                # to SKIP those filters rather than reject on a zero value that
                # simply means "not yet received from feed".
                tick = {"ltp": ltp_val, "best_bid": 0, "best_ask": 0,
                        "oi": -1, "volume": -1, "iv": chain_iv.get(inst.token, 0)}
                no_tick_count += 1

            # Inject IV from chain snapshot if tick doesn't have it
            if not tick.get("iv") and inst.token in chain_iv:
                tick = dict(tick)
                tick["iv"] = chain_iv[inst.token]

            prev_oi = prev_oi_map.get(inst.token, 0)

            cand = score_candidate(
                inst        = inst,
                spot        = spot_price,
                tick        = tick,
                expiry      = expiry,
                direction   = direction,
                step        = step,
                ltp_history = [],
                prev_oi     = prev_oi,
                avg_volume  = avg_vol_map.get(inst.token, 0.0),
                sl_pct      = sl_pct,
                target_pct  = target_pct,
            )
            if cand:
                if cand.score >= MIN_SCORE:
                    candidates.append(cand)
                else:
                    rejected += 1
                    logger.debug(
                        "[SoftFilter] REJECTED %s %.0f%s score=%.1f < %.1f",
                        inst.symbol, inst.strike,
                        inst.option_type.value if inst.option_type else "",
                        cand.score, MIN_SCORE,
                    )
            else:
                rejected += 1

        # Save current OI snapshot for next cycle's OI-change scoring
        # Also update rolling average volume (EMA α=0.3) for volume-trend scoring
        if instruments:
            new_snap: dict[str, int] = {}
            new_avg_vol: dict[str, float] = {}
            for inst in instruments:
                tick = await redis_cache.get_tick(inst.token)
                if tick:
                    # OI snapshot — used for OI-change scoring next cycle
                    cur_oi = int(tick.get("oi") or 0)
                    new_snap[inst.token] = cur_oi
                    # Volume EMA — used for volume-trend scoring (was wrongly using OI)
                    prev_avg = avg_vol_map.get(inst.token, 0.0)
                    cur_volume = float(tick.get("volume") or 0)
                    if prev_avg <= 0:
                        new_avg_vol[inst.token] = cur_volume
                    else:
                        new_avg_vol[inst.token] = round(0.3 * cur_volume + 0.7 * prev_avg, 1)
            if new_snap:
                await redis_cache.set(prev_snap_key, new_snap, ttl=3600)
            if new_avg_vol:
                await redis_cache.set(avg_vol_key, new_avg_vol, ttl=28800)

        if no_tick_count > 0:
            logger.warning(
                "[OptionSelector] %d / %d instruments had NO tick in Redis "
                "(LTP-only fallback used). OI & volume hard filters were bypassed "
                "for those — if all still rejected, check LTP range or spread.",
                no_tick_count, len(instruments) if instruments else 0,
            )

        return candidates, rejected, no_tick_count

    def _log_table(self, candidates: list[ScoredCandidate], underlying: str, rejected: int = 0):
        """Log the full scoring table with pass/reject summary."""
        if not candidates:
            return
        logger.info(
            "[OptionSelector] %s — %d candidates passed, %d rejected by filters",
            underlying, len(candidates), rejected,
        )
        header = (
            f"  {'Strike':>8}  {'LTP':>7}  {'Delta':>6}  "
            f"{'IV':>6}  {'OI':>9}  {'Spread':>7}  "
            f"{'Score':>9}  | {'Liq':>5}  {'Sprd':>5}  "
            f"{'Δ':>5}  {'Mny':>5}  {'OIΔ':>5}"
        )
        logger.info(header)
        logger.info("  " + "─" * 112)
        for i, c in enumerate(candidates[:8]):
            marker = "  ← SELECTED" if i == 0 else ""
            logger.info(
                "  %8.0f  %7.2f  %6.3f  %5.1f%%  %9s  %6.2f%%  "
                "%7.1f/100  | %5.1f  %5.1f  %5.1f  %5.1f  %5.1f%s",
                c.strike, c.ltp, c.delta_est, c.iv, f"{c.oi:,}",
                c.spread_pct, c.score,
                c.score_liquidity, c.score_spread, c.score_delta,
                c.score_moneyness, c.score_oi_change,
                marker,
            )


# ── Singleton ──────────────────────────────────────────────────────────────────
option_selector = OptionSelector()
