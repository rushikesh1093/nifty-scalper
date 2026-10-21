"""
TREND_ENGINE — one unified intraday trend-following decision engine for
NIFTY and BANKNIFTY.

Per underlying and per completed 1-minute candle it produces exactly one
decision:

    BUY CE    — confirmed UPTREND + a valid entry trigger
    BUY PE    — confirmed DOWNTREND + a valid entry trigger
    NO TRADE  — sideways / weak / conflicting / unconfirmed / unsafe, with an
                explicit reason code

The 12 legacy strategies are used as EVIDENCE, not as 12 independent trade
triggers: their trend conditions (EMA/VWAP alignment, Supertrend direction,
ORB and previous-day breakouts, VWAP pullbacks, volume participation) are
re-evaluated here from the shared indicator values without their side
effects. Reversal evidence (RSI extremes) may only BLOCK an entry as an
over-extended chase; it can never flip the direction.

Units: every price level, stop and target in a decision is in UNDERLYING
INDEX POINTS (``units = "INDEX_POINTS"``). The paper engine converts the stop
and target to OPTION-PREMIUM points with the selected contract's delta (see
``paper_engine._engine_levels``); the two are never mixed.

No guarantee: a target is an objective only when volatility, structure and
room to the next level support it; otherwise the decision is NO TRADE.
"""
from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Optional

import numpy as np

from app.runtime.context import now_utc, params, trading_day
from app.services.market_data.timestamps import try_normalize_candle_timestamp
from app.strategies.base import BaseStrategy, Direction, SignalEvent
from app.strategies.indicators import ema, rsi, supertrend, vwap as vwap_series
from app.strategies.names import StrategyName

logger = logging.getLogger(__name__)

ENGINE_NAME = StrategyName.TREND_ENGINE.value

UPTREND, DOWNTREND, SIDEWAYS = "UPTREND", "DOWNTREND", "SIDEWAYS"
STRONG, MODERATE, WEAK = "STRONG", "MODERATE", "WEAK"
BUY_CE, BUY_PE, NO_TRADE = "BUY CE", "BUY PE", "NO TRADE"
_STRENGTH_RANK = {WEAK: 0, MODERATE: 1, STRONG: 2}

# NO TRADE reason codes (explicit, stable strings)
SIDEWAYS_MARKET = "SIDEWAYS_MARKET"
CONFLICTING_SIGNALS = "CONFLICTING_SIGNALS"
WEAK_TREND = "WEAK_TREND"
NO_ENTRY_CONFIRMATION = "NO_ENTRY_CONFIRMATION"
EXTENDED_MOVE = "EXTENDED_MOVE"
LOW_VOLATILITY = "LOW_VOLATILITY"
NO_ROOM_TO_TARGET = "NO_ROOM_TO_TARGET"
STOP_TOO_WIDE = "STOP_TOO_WIDE"
STALE_DATA = "STALE_DATA"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
MALFORMED_DATA = "MALFORMED_DATA"


@dataclass(frozen=True)
class TrendEngineConfig:
    """Every threshold the engine uses. ATR = the 14-period 1-minute ATR of
    the underlying; "bars" are completed 1-minute candles."""
    # data validity
    min_candles: int = 30
    stale_candle_minutes: float = 3.0
    # The last `contiguous_candles` candles must have no hole longer than
    # `max_candle_gap_minutes` (a feed outage or a restart that could not refill
    # history): EMAs, slopes and swing structure computed across a hole are not
    # real, so such a candle is NO TRADE (STALE_DATA) until the window is clean.
    contiguous_candles: int = 30
    max_candle_gap_minutes: float = 3.0
    # trend classification
    vwap_persistence_bars: int = 15        # share of recent closes on the trend side of VWAP
    vwap_persistence_min: float = 0.70
    vwap_cross_lookback: int = 20
    max_vwap_crosses: int = 3              # more = price chopping around VWAP → sideways
    ema_cross_lookback: int = 20
    max_ema_crosses: int = 2               # more = EMA9/21 tangled → sideways
    min_ema_sep_atr: float = 0.15          # |EMA9−EMA21| ≥ this × ATR, or …
    min_ema_slope_atr: float = 0.02        # … EMA21 slope ≥ this × ATR per bar
    slope_bars: int = 10                   # long enough that a pullback does not flip it
    swing_strength: int = 2                # pivot = extreme with this many bars each side
    structure_lookback: int = 45
    min_strength: str = MODERATE           # weaker trends are NO TRADE
    # entry triggers
    pullback_lookback: int = 5
    pullback_touch_atr: float = 0.35       # low reached within this of VWAP/EMA21
    pullback_max_break_atr: float = 0.5    # … without closing deeper than this below it
    breakout_lookback: int = 12
    retest_tolerance_atr: float = 0.25
    breakout_volume_mult: float = 1.2      # breakout bar volume ≥ this × average (futures vol)
    consolidation_bars: int = 5
    consolidation_max_atr: float = 1.2
    min_body_frac: float = 0.5             # trigger candle body ≥ this share of its range
    # no chasing
    max_trigger_range_atr: float = 1.8
    max_extension_atr: float = 1.2         # close − EMA9 (CE) / EMA9 − close (PE)
    rsi_overbought: float = 78.0           # CE blocked above (PE mirrored: below 100 − this)
    # stop / target, INDEX POINTS
    stop_buffer_atr: float = 0.2
    min_stop_atr: float = 0.6
    max_stop_atr: float = 2.5
    target_points: dict = field(default_factory=lambda: {
        "NIFTY": (20.0, 30.0), "BANKNIFTY": (50.0, 75.0)})
    max_target_mult: float = 1.5           # T1 above target_max × this → not realistic
    hold_minutes: int = 60
    expected_move_factor: float = 0.8      # ATR × √hold × factor must cover T1
    room_factor: float = 1.0               # next resistance/support ≥ T1 × factor away
    target2_mult: float = 1.5              # T2 = T1 × this, STRONG trends only
    # evidence scoring (each satisfied item adds 1 to the 0–6 score)
    strong_ema_sep_atr: float = 0.4        # "wide EMA9/21 separation" item
    steep_slope_mult: float = 2.0          # "steep EMA21 slope" = this × min_ema_slope_atr
    strong_score: int = 4                  # score ≥ this → STRONG
    moderate_score: int = 2                # score ≥ this → MODERATE (else WEAK)
    # stop placement / entry zone / retest detail
    stop_swing_lookback: int = 20          # bars searched for the protective swing
    stop_recent_bars: int = 3              # the stop is also beyond these bars' extreme
    entry_zone_atr: tuple = (0.3, 0.15)    # zone = close − a×ATR … close + b×ATR (CE)
    breakout_ref_bars: int = 20            # bars before the lookback that define SWING_HIGH
    retest_close_frac: float = 0.5         # retest close may undercut by this × tolerance
    volume_bars: int = 3                   # recent bars whose mean volume must beat the average
    cooldown_minutes: int = 10             # after an OPENED position
    # trailing stop, applied by the paper engine to the open position
    trail_activation_reward: float = 0.6   # structure trail starts with break-even (60% of reward)
    trail_swing_buffer_atr: float = 0.2


DEFAULT_CONFIG = TrendEngineConfig()


@dataclass
class TrendDecision:
    underlying: str
    candle_time: str
    evaluated_at: str
    trend: str
    strength: str
    decision: str
    reason_code: Optional[str]
    reason: str
    spot: Optional[float] = None
    vwap: Optional[float] = None
    atr: Optional[float] = None
    entry_trigger: Optional[str] = None
    entry_zone: Optional[tuple] = None
    stop_level: Optional[float] = None
    stop_points: Optional[float] = None
    target1_level: Optional[float] = None
    target1_points: Optional[float] = None
    target2_level: Optional[float] = None
    target2_points: Optional[float] = None
    risk_reward: Optional[float] = None
    trailing_rule: Optional[str] = None
    confirmation: Optional[str] = None
    invalidation: Optional[str] = None
    evidence: list = field(default_factory=list)
    units: str = "INDEX_POINTS"
    # Research telemetry. `score` is the 0-6 trend-evidence count (a classification
    # score, not a calibrated probability); `features` are the raw classification
    # inputs. Neither changes any decision.
    score: Optional[int] = None
    features: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = asdict(self)
        if d.get("entry_zone") is not None:
            d["entry_zone"] = list(d["entry_zone"])
        return d

    def as_text(self) -> str:
        def pts(level, p):
            return "—" if level is None else f"{level:.2f} ({p:+.1f} index pts)"
        lines = [
            f"Underlying: {self.underlying}",
            f"Market Trend: {self.trend}",
            f"Trend Strength: {self.strength}",
            f"Trade Signal: {self.decision}",
        ]
        if self.decision == NO_TRADE:
            lines.append(f"Reason: [{self.reason_code}] {self.reason}")
            return "\n".join(lines)
        sgn = 1 if self.decision == BUY_CE else -1
        lines += [
            f"Entry Trigger: {self.entry_trigger}",
            f"Spot Price: {self.spot:.2f}",
            f"Entry Zone (index): {self.entry_zone[0]:.2f} – {self.entry_zone[1]:.2f}",
            f"Stop-Loss (index): {pts(self.stop_level, -sgn * self.stop_points)}",
            f"Target 1 (index): {pts(self.target1_level, sgn * self.target1_points)}",
            f"Target 2 (index): " + (pts(self.target2_level, sgn * self.target2_points)
                                     if self.target2_level is not None else "— (not justified)"),
            f"Trailing SL: {self.trailing_rule}",
            f"Risk-to-Reward: {self.risk_reward:.2f}",
            f"Confirmation: {self.confirmation}",
            f"Invalidation: {self.invalidation}",
            f"Reason: {self.reason}",
            "Option contract / premium / premium stop: set by the option selector at entry "
            "(see the position).",
        ]
        return "\n".join(lines)


class DecisionLog:
    """Last N decisions per underlying, for the API / dashboard."""

    def __init__(self, maxlen: int = 400) -> None:   # a full session of 1-min decisions
        self._log: dict[str, deque] = {}
        self._maxlen = maxlen

    def record(self, d: TrendDecision) -> None:
        self._log.setdefault(d.underlying, deque(maxlen=self._maxlen)).append(d)

    def latest(self, underlying: str) -> Optional[TrendDecision]:
        q = self._log.get(underlying)
        return q[-1] if q else None

    def recent(self, underlying: Optional[str] = None, limit: int = 50) -> list[TrendDecision]:
        keys = [underlying] if underlying else sorted(self._log)
        out: list[TrendDecision] = []
        for k in keys:
            out.extend(self._log.get(k, []))
        out.sort(key=lambda d: d.evaluated_at)
        return out[-limit:]

    def reset(self) -> None:
        self._log.clear()


decision_log = DecisionLog()


# ── pure helpers ─────────────────────────────────────────────────────────────

def _largest_gap(candles: list[dict]) -> Optional[tuple[float, str]]:
    """(largest minutes between consecutive candle starts, HH:MM of the candle
    after it), or None when fewer than two timestamps parse."""
    ts = [try_normalize_candle_timestamp(c.get("timestamp")) for c in candles]
    ts = [t for t in ts if t is not None]
    best: Optional[tuple[float, str]] = None
    for a, b in zip(ts, ts[1:]):
        g = (b - a).total_seconds() / 60.0
        if best is None or g > best[0]:
            best = (g, b.strftime("%H:%M"))
    return best


def _sign_changes(x: np.ndarray) -> int:
    s = np.sign(x[~np.isnan(x)])
    s = s[s != 0]
    return int(np.sum(s[1:] != s[:-1])) if len(s) > 1 else 0


def _pivots(h: np.ndarray, l: np.ndarray, k: int, start: int) -> tuple[list[int], list[int]]:
    """Indices of CONFIRMED swing highs/lows (k bars on each side) from *start*."""
    highs, lows = [], []
    n = len(h)
    for i in range(max(start, k), n - k):
        win_h, win_l = h[i - k:i + k + 1], l[i - k:i + k + 1]
        if h[i] == win_h.max() and np.sum(win_h == h[i]) == 1:
            highs.append(i)
        if l[i] == win_l.min() and np.sum(win_l == l[i]) == 1:
            lows.append(i)
    return highs, lows


def last_swing_low(h: np.ndarray, l: np.ndarray, k: int, start: int = 0) -> Optional[float]:
    _, lows = _pivots(h, l, k, start)
    return float(l[lows[-1]]) if lows else None


def last_swing_high(h: np.ndarray, l: np.ndarray, k: int, start: int = 0) -> Optional[float]:
    highs, _ = _pivots(h, l, k, start)
    return float(h[highs[-1]]) if highs else None


@dataclass
class _Series:
    """Price series in the 'long' frame: for a DOWN evaluation every price is
    negated, so one set of bullish rules serves both directions."""
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    vw: np.ndarray
    e9: np.ndarray
    e21: np.ndarray
    rsi: float
    levels: dict           # name → level (already in this frame)

    @classmethod
    def mirrored(cls, s: "_Series") -> "_Series":
        return cls(o=-s.o, h=-s.l, l=-s.h, c=-s.c, v=s.v, vw=-s.vw, e9=-s.e9, e21=-s.e21,
                   rsi=100.0 - s.rsi, levels={k: -v for k, v in s.levels.items()})


class TrendFollowingEngine(BaseStrategy):
    """The unified decision engine. Registered as strategy TREND_ENGINE so the
    existing regime map, user flags, aggregator, risk gate, option selector and
    paper engine all apply to it unchanged."""

    name = ENGINE_NAME
    # Placeholders that satisfy the 1:2 R:R gate before selection; the paper
    # engine replaces them with the delta-converted index-point stop/target.
    sl_pct = 0.15
    target_pct = 0.30

    def __init__(self, underlying: str = "NIFTY", config: TrendEngineConfig = DEFAULT_CONFIG):
        self.config = config
        self._cooldown_minutes = config.cooldown_minutes
        super().__init__(underlying)
        self.pdh: Optional[float] = None
        self.pdl: Optional[float] = None
        self._last_candle: Optional[str] = None

    def set_levels(self, pdh: Optional[float], pdl: Optional[float]) -> None:
        self.pdh, self.pdl = pdh, pdl

    # ── BaseStrategy hook ────────────────────────────────────────────────────

    async def evaluate(self, candles: list[dict], indicators: dict) -> Optional[SignalEvent]:
        ts = str(candles[-1].get("timestamp")) if candles else None
        if ts is not None and ts == self._last_candle:
            return None                          # same candle evaluated twice: no duplicate
        self._last_candle = ts
        d = self.decide(candles, indicators)
        decision_log.record(d)
        log = logger.info if d.decision != NO_TRADE else logger.debug
        log("[TREND_ENGINE] %s %s %s %s — %s", d.underlying, d.trend, d.strength, d.decision,
            d.reason_code or d.entry_trigger)
        if d.decision == NO_TRADE:
            return None
        direction = Direction.BUY if d.decision == BUY_CE else Direction.SELL
        ind = dict(indicators)
        ind.update({
            "engine": d.as_dict(),
            "engine_units": "INDEX_POINTS",
            "engine_stop_pts": d.stop_points,
            "engine_target_pts": d.target1_points,
            "engine_trail": {
                "activation_reward": self.config.trail_activation_reward,
                "swing_strength": self.config.swing_strength,
                "buffer_pts": round(self.config.trail_swing_buffer_atr * (d.atr or 0.0), 2),
            },
        })
        return self._make_signal(direction, d.spot, ind, notes=d.as_text())

    # ── the decision ─────────────────────────────────────────────────────────

    def decide(self, candles: list[dict], indicators: dict) -> TrendDecision:
        cfg = self.config
        ts = str(candles[-1].get("timestamp")) if candles else ""

        def no(trend, strength, code, reason, **kw):
            return TrendDecision(self.underlying, ts, now_utc().isoformat(), trend, strength,
                                 NO_TRADE, code, reason, **kw)

        # 1. data validity — anything missing or odd is NO TRADE, never a guess
        if len(candles) < cfg.min_candles:
            return no(SIDEWAYS, WEAK, INSUFFICIENT_DATA,
                      f"{len(candles)} candles; need {cfg.min_candles}")
        try:
            o = np.array([float(x["open"]) for x in candles])
            h = np.array([float(x["high"]) for x in candles])
            l = np.array([float(x["low"]) for x in candles])
            c = np.array([float(x["close"]) for x in candles])
            v = np.array([float(x.get("volume") or 0.0) for x in candles])
        except (KeyError, TypeError, ValueError) as exc:
            return no(SIDEWAYS, WEAK, MALFORMED_DATA, f"unreadable candle: {exc}")
        if not (np.all(np.isfinite(o)) and np.all(np.isfinite(h)) and np.all(np.isfinite(l))
                and np.all(np.isfinite(c))) or np.any(h < l) or np.any(c <= 0):
            return no(SIDEWAYS, WEAK, MALFORMED_DATA, "non-finite or inverted OHLC")
        atr = indicators.get("atr")
        if atr is None or not math.isfinite(float(atr)) or atr <= 0:
            return no(SIDEWAYS, WEAK, MALFORMED_DATA, "ATR missing")
        atr = float(atr)
        last_dt = try_normalize_candle_timestamp(candles[-1].get("timestamp"))
        if last_dt is None:
            return no(SIDEWAYS, WEAK, MALFORMED_DATA, "last candle has no timestamp")
        age_min = (now_utc() - (last_dt + timedelta(minutes=1))).total_seconds() / 60.0
        if age_min > cfg.stale_candle_minutes:
            return no(SIDEWAYS, WEAK, STALE_DATA,
                      f"last candle closed {age_min:.1f} min ago (> {cfg.stale_candle_minutes})")
        gap = _largest_gap(candles[-cfg.contiguous_candles:])
        if gap is not None and gap[0] > cfg.max_candle_gap_minutes:
            return no(SIDEWAYS, WEAK, STALE_DATA,
                      f"{gap[0]:.0f}-minute hole in the candles before {gap[1]}; need "
                      f"{cfg.contiguous_candles} contiguous candles")

        vw = vwap_series(h, l, c, v)
        e9, e21 = ema(c, 9), ema(c, 21)
        if np.isnan(e21[-1]) or np.isnan(e21[-1 - cfg.slope_bars]):
            return no(SIDEWAYS, WEAK, INSUFFICIENT_DATA, "EMA21 not warmed up")
        r = rsi(c, 14)
        rsi_now = float(r[-1]) if np.isfinite(r[-1]) else 50.0
        close, vwap_now = float(c[-1]), float(vw[-1])
        base = dict(spot=round(close, 2), vwap=round(vwap_now, 2), atr=round(atr, 2))

        # 2. trend classification
        n = cfg.vwap_persistence_bars
        above = float(np.mean(c[-n:] > vw[-n:]))
        below = float(np.mean(c[-n:] < vw[-n:]))
        vwap_crosses = _sign_changes((c - vw)[-(cfg.vwap_cross_lookback + 1):])
        ema_crosses = _sign_changes((e9 - e21)[-(cfg.ema_cross_lookback + 1):])
        sep = (e9[-1] - e21[-1]) / atr
        slope = (e21[-1] - e21[-1 - cfg.slope_bars]) / (cfg.slope_bars * atr)
        start = max(0, len(c) - cfg.structure_lookback)
        ph, pl = _pivots(h, l, cfg.swing_strength, start)
        hh = len(ph) >= 2 and h[ph[-1]] > h[ph[-2]]
        hl = len(pl) >= 2 and l[pl[-1]] > l[pl[-2]]
        lh = len(ph) >= 2 and h[ph[-1]] < h[ph[-2]]
        ll = len(pl) >= 2 and l[pl[-1]] < l[pl[-2]]
        st_dir = int(supertrend(h, l, c, 10, 3.0).direction[-1])
        orb_h, orb_l = indicators.get("orb_high"), indicators.get("orb_low")
        orb_ready = len(c) >= 15 and orb_h is not None and orb_l is not None
        base["features"] = {
            "ema_sep_atr": round(float(sep), 4), "ema21_slope_atr": round(float(slope), 5),
            "vwap_above_frac": round(above, 3), "vwap_below_frac": round(below, 3),
            "vwap_crosses": int(vwap_crosses), "ema_crosses": int(ema_crosses),
            "rsi": round(rsi_now, 2), "supertrend_dir": st_dir,
            "hh": bool(hh), "hl": bool(hl), "lh": bool(lh), "ll": bool(ll),
        }

        if vwap_crosses > cfg.max_vwap_crosses or ema_crosses > cfg.max_ema_crosses:
            return no(SIDEWAYS, WEAK, SIDEWAYS_MARKET,
                      f"choppy: {vwap_crosses} VWAP crosses / {ema_crosses} EMA9-21 crosses in "
                      f"{cfg.vwap_cross_lookback} bars (max {cfg.max_vwap_crosses}/"
                      f"{cfg.max_ema_crosses})", **base)

        trending = abs(sep) >= cfg.min_ema_sep_atr or abs(slope) >= cfg.min_ema_slope_atr
        # EMA21 may flatten during a pullback; it must not be clearly AGAINST the trend
        up_core = (close > vwap_now and e9[-1] > e21[-1] and trending
                   and slope > -cfg.min_ema_slope_atr and above >= cfg.vwap_persistence_min)
        dn_core = (close < vwap_now and e9[-1] < e21[-1] and trending
                   and slope < cfg.min_ema_slope_atr and below >= cfg.vwap_persistence_min)

        if not (up_core or dn_core):
            price_side = close > vwap_now
            ema_side = e9[-1] > e21[-1]
            if price_side != ema_side:
                return no(SIDEWAYS, WEAK, CONFLICTING_SIGNALS,
                          f"price {'above' if price_side else 'below'} VWAP but EMA9 "
                          f"{'above' if ema_side else 'below'} EMA21", **base)
            return no(SIDEWAYS, WEAK, SIDEWAYS_MARKET,
                      f"no sustained direction: {above:.0%} of last {n} closes above VWAP, "
                      f"EMA sep {sep:+.2f} ATR, EMA21 slope {slope:+.3f} ATR/bar", **base)

        trend = UPTREND if up_core else DOWNTREND
        sgn = 1 if up_core else -1
        bull_struct, bear_struct = (hh and hl), (lh and ll)
        breakout = False
        evidence: list[str] = [
            f"price {'above' if up_core else 'below'} VWAP on {max(above, below):.0%} of the "
            f"last {n} closes (VWAP_PULLBACK/EMA_VWAP)",
            f"EMA9 {'>' if up_core else '<'} EMA21, sep {sep:+.2f} ATR, slope {slope:+.3f} "
            f"ATR/bar (EMA_VWAP)",
        ]
        score = 0
        if (bull_struct if up_core else bear_struct):
            score += 1
            evidence.append("market structure " + ("higher highs + higher lows" if up_core
                                                   else "lower highs + lower lows"))
        elif (bear_struct if up_core else bull_struct):
            return no(trend, WEAK, CONFLICTING_SIGNALS,
                      "EMA/VWAP say " + trend + " but swing structure is opposite", **base)
        lvl_hi = [x for x in (orb_h if orb_ready else None, self.pdh) if x]
        lvl_lo = [x for x in (orb_l if orb_ready else None, self.pdl) if x]
        if up_core and any(close > x for x in lvl_hi):
            breakout = True
        if dn_core and any(close < x for x in lvl_lo):
            breakout = True
        if breakout:
            score += 1
            evidence.append("beyond opening range / previous-day level (ORB_VWAP/PDH_PDL)")
        if st_dir == sgn:
            score += 1
            evidence.append("Supertrend agrees (SUPERTREND_VWAP)")
        if abs(sep) >= cfg.strong_ema_sep_atr:
            score += 1
            evidence.append("wide EMA9/21 separation")
        if abs(slope) >= cfg.steep_slope_mult * cfg.min_ema_slope_atr:
            score += 1
            evidence.append("steep EMA21 slope")
        avg_vol = indicators.get("avg_volume")
        vol_ok = bool(avg_vol) and float(np.mean(v[-cfg.volume_bars:])) > float(avg_vol)
        if vol_ok:
            score += 1
            evidence.append("futures volume above average")
        has_structure = (bull_struct if up_core else bear_struct) or breakout
        base["score"] = score
        strength = (STRONG if score >= cfg.strong_score
                    else MODERATE if score >= cfg.moderate_score else WEAK)
        if not has_structure:
            strength = WEAK
        base["evidence"] = evidence
        if _STRENGTH_RANK[strength] < _STRENGTH_RANK[cfg.min_strength]:
            return no(trend, strength, WEAK_TREND,
                      f"trend evidence {score}/6" + ("" if has_structure else
                                                     ", no confirming structure or breakout"),
                      **base)

        # 3. tradeable volatility (shared ATR floor)
        from app.strategies.strategies import atr_floor
        if atr < atr_floor(close):
            return no(trend, strength, LOW_VOLATILITY,
                      f"ATR {atr:.1f} below floor {atr_floor(close):.1f}", **base)

        # 4. entry trigger, in the long frame
        s = _Series(o=o, h=h, l=l, c=c, v=v, vw=vw, e9=e9, e21=e21, rsi=rsi_now,
                    levels={"ORB": orb_h if orb_ready else None, "PDH": self.pdh}
                    if up_core else {"ORB": orb_l if orb_ready else None, "PDL": self.pdl})
        s.levels = {k: x for k, x in s.levels.items() if x}
        if dn_core:
            s = _Series.mirrored(s)
        chase = self._chase(s, atr)
        if chase:
            return no(trend, strength, EXTENDED_MOVE, chase, **base)
        trigger = (self._breakout_retest(s, atr, avg_vol) or self._pullback(s, atr)
                   or self._continuation(s, atr))
        if trigger is None:
            return no(trend, strength, NO_ENTRY_CONFIRMATION,
                      "trend present but no confirmed pullback rejection, breakout-retest "
                      "or continuation on the last completed candle", **base)

        # 5. stop (index points) from structure, bounded by ATR
        lo_k = max(0, len(s.c) - cfg.stop_swing_lookback)
        swing = last_swing_low(s.h, s.l, cfg.swing_strength, lo_k)
        struct_low = min(x for x in (swing, float(s.l[-1]), float(np.min(s.l[-cfg.stop_recent_bars:])))
                         if x is not None)
        stop_lvl = struct_low - cfg.stop_buffer_atr * atr
        stop_pts = float(s.c[-1]) - stop_lvl
        if stop_pts > cfg.max_stop_atr * atr:
            return no(trend, strength, STOP_TOO_WIDE,
                      f"structural stop {stop_pts:.1f} pts > {cfg.max_stop_atr} × ATR "
                      f"({cfg.max_stop_atr * atr:.1f})", **base)
        stop_pts = max(stop_pts, cfg.min_stop_atr * atr)

        # 6. target realism (index points)
        tmin, tmax = cfg.target_points.get(self.underlying, cfg.target_points["NIFTY"])
        min_rr = params().min_rr
        t1 = max(min_rr * stop_pts, tmin)
        if t1 > tmax * cfg.max_target_mult:
            return no(trend, strength, STOP_TOO_WIDE,
                      f"needs a {t1:.1f}-pt target for {min_rr}:1 on a {stop_pts:.1f}-pt stop "
                      f"(> {tmax * cfg.max_target_mult:.0f})", **base)
        expected = atr * math.sqrt(cfg.hold_minutes) * cfg.expected_move_factor
        if t1 > expected:
            return no(trend, strength, LOW_VOLATILITY,
                      f"ATR {atr:.1f} supports ≈{expected:.0f} pts in {cfg.hold_minutes} min; "
                      f"target needs {t1:.1f}", **base)
        # Room is checked against MAJOR levels only (previous-day high/low, opening
        # range). The intraday swing high is not a barrier for a trend entry: a
        # pullback buy's thesis is precisely that the trend takes it out.
        room_levels = [x for x in s.levels.values() if x > s.c[-1]]
        if room_levels:
            nearest = min(room_levels) - float(s.c[-1])
            if nearest < t1 * cfg.room_factor:
                return no(trend, strength, NO_ROOM_TO_TARGET,
                          f"{'resistance' if up_core else 'support'} {nearest:.1f} pts away; "
                          f"target needs {t1:.1f}", **base)
        t2 = t1 * cfg.target2_mult if strength == STRONG else None

        # 7. decision (back to real prices)
        stop_level = close - sgn * stop_pts
        za, zb = cfg.entry_zone_atr
        zone = sorted((close - sgn * za * atr, close + sgn * zb * atr))
        side = "higher lows" if up_core else "lower highs"
        return TrendDecision(
            self.underlying, ts, now_utc().isoformat(), trend, strength,
            BUY_CE if up_core else BUY_PE, None,
            f"{trend} ({strength}, evidence {score}/6) + {trigger}",
            entry_trigger=trigger, entry_zone=(round(zone[0], 2), round(zone[1], 2)),
            stop_level=round(stop_level, 2), stop_points=round(stop_pts, 2),
            target1_level=round(close + sgn * t1, 2), target1_points=round(t1, 2),
            target2_level=round(close + sgn * t2, 2) if t2 else None,
            target2_points=round(t2, 2) if t2 else None,
            risk_reward=round(t1 / stop_pts, 2),
            trailing_rule=(f"break-even (+costs) after {params().trail_tiers[-1].trigger_reward_frac:.0%}"
                           f" of the reward; from {cfg.trail_activation_reward:.0%} trail beyond "
                           f"confirmed {side} − {cfg.trail_swing_buffer_atr} ATR; never widened"),
            confirmation=f"completed candle at {ts[11:16] or ts}: {trigger}",
            invalidation=(f"spot {'below' if up_core else 'above'} {stop_level:.2f}, or a close "
                          f"{'below' if up_core else 'above'} VWAP with EMA9 "
                          f"{'<' if up_core else '>'} EMA21"),
            **base,
        )

    # ── entry rules (long frame; DOWN uses mirrored series) ──────────────────

    def _confirm(self, s: _Series) -> bool:
        body, rng = s.c[-1] - s.o[-1], s.h[-1] - s.l[-1]
        return (body > 0 and rng > 0 and body >= self.config.min_body_frac * rng
                and s.c[-1] > s.c[-2] and s.c[-1] > s.e9[-1])

    def _chase(self, s: _Series, atr: float) -> Optional[str]:
        cfg = self.config
        rng = s.h[-1] - s.l[-1]
        ext = s.c[-1] - s.e9[-1]
        if rng > cfg.max_trigger_range_atr * atr:
            return f"trigger candle range {rng:.1f} > {cfg.max_trigger_range_atr} × ATR — not chasing"
        if ext > cfg.max_extension_atr * atr:
            return f"price {ext:.1f} pts beyond EMA9 (> {cfg.max_extension_atr} × ATR) — not chasing"
        if s.rsi > cfg.rsi_overbought:
            return f"RSI {s.rsi:.0f} stretched (RSI_VWAP/VWAP_REVERSAL evidence) — not chasing"
        return None

    def _pullback(self, s: _Series, atr: float) -> Optional[str]:
        cfg = self.config
        k = cfg.pullback_lookback
        support = max(float(s.vw[-1]), float(s.e21[-1]))
        floor = min(float(s.vw[-1]), float(s.e21[-1]))
        low_k = float(np.min(s.l[-k:]))
        touched = low_k <= support + cfg.pullback_touch_atr * atr
        held = float(np.min(s.c[-k:])) >= floor - cfg.pullback_max_break_atr * atr
        if touched and held and s.c[-1] > support and self._confirm(s):
            return "pullback rejection at VWAP/EMA21 (VWAP_PULLBACK)"
        return None

    def _breakout_retest(self, s: _Series, atr: float, avg_vol) -> Optional[str]:
        cfg = self.config
        n = len(s.c)
        levels = dict(s.levels)
        ref = s.h[max(0, n - cfg.breakout_lookback - cfg.breakout_ref_bars):
                  max(1, n - cfg.breakout_lookback)]
        if len(ref):
            levels["SWING_HIGH"] = float(np.max(ref))
        for name, lvl in levels.items():
            for j in range(max(1, n - cfg.breakout_lookback), n - 1):
                if not (s.c[j] > lvl >= s.c[j - 1]):
                    continue
                if not avg_vol or s.v[j] < cfg.breakout_volume_mult * float(avg_vol):
                    continue                     # unconfirmed breakout (no volume)
                retest = any(s.l[m] <= lvl + cfg.retest_tolerance_atr * atr
                             and s.c[m] >= lvl - cfg.retest_close_frac * cfg.retest_tolerance_atr * atr
                             for m in range(j + 1, n))
                if retest and s.c[-1] > lvl and self._confirm(s):
                    return f"{name} breakout + retest held ({'ORB_VWAP' if name == 'ORB' else 'PDH_PDL' if name in ('PDH', 'PDL') else 'structure'})"
        return None

    def _continuation(self, s: _Series, atr: float) -> Optional[str]:
        cfg = self.config
        m = cfg.consolidation_bars
        box_h, box_l = s.h[-1 - m:-1], s.l[-1 - m:-1]
        if len(box_h) < m:
            return None
        tight = float(np.max(box_h) - np.min(box_l)) <= cfg.consolidation_max_atr * atr
        intact = bool(np.all(s.c[-1 - m:-1] > s.e21[-1 - m:-1]))
        if tight and intact and s.c[-1] > float(np.max(box_h)) and self._confirm(s):
            return "continuation out of a tight consolidation"
        return None

    # ── open-position invalidation ───────────────────────────────────────────

    def invalidation(self, option_type: str, candles: list[dict]) -> Optional[str]:
        """Reason to exit an open engine position (CE in a now-broken uptrend,
        PE in a now-broken downtrend), or None."""
        if len(candles) < 22:
            return None
        h = np.array([float(x["high"]) for x in candles])
        l = np.array([float(x["low"]) for x in candles])
        c = np.array([float(x["close"]) for x in candles])
        v = np.array([float(x.get("volume") or 0.0) for x in candles])
        vw = vwap_series(h, l, c, v)
        e9, e21 = ema(c, 9), ema(c, 21)
        if np.isnan(e21[-1]):
            return None
        if option_type == "CE" and c[-1] < vw[-1] and e9[-1] < e21[-1]:
            return "uptrend invalidated: close below VWAP and EMA9 < EMA21"
        if option_type == "PE" and c[-1] > vw[-1] and e9[-1] > e21[-1]:
            return "downtrend invalidated: close above VWAP and EMA9 > EMA21"
        return None
