"""
Regime-label stabiliser (23 Oct): five switchable filters between the
classifier and the detector's transition step. The per-minute label flipped
~117 times a day (median regime: 2 bars; 44% lasted one bar), mostly on
thresholds: ADX near 18/22, price at the 0.2% VWAP band edge, a jumpy ATR
percentile and breakouts that vanish when one bar dips back.

All off by default (TradingParams); with every knob at its default ``step``
returns exactly what ``classify`` returns.

  regime_eval_minutes          classify only when an N-minute bar closes (clock
                               minute % N == 0); hold the label in between
  regime_hysteresis            stay in a trend down to ADX_RANGE_THRESHOLD, in a
                               range (RANGE_NORMAL_VOL or RANGE_BOUND) up to
                               ADX_TREND_THRESHOLD, in compression
                               out to regime_vwap_exit_pct from VWAP
  regime_atr_smooth            EMA of the ATR percentile over N evaluations
  regime_breakout_hold_minutes keep a breakout label this long unless price
                               falls back regime_breakout_fail_pts through the
                               broken level
  regime_confirm_evals         a new label must be seen in N consecutive
                               evaluations; the adopted label holds until then

State is per (underlying, trading day) and in memory: a restart starts the day's
filters afresh. EXPIRY_DAY and DETECTOR_ERROR always pass straight through.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Callable, Optional

TREND = frozenset({"BULL_TREND_NORMAL_VOL", "BEAR_TREND_NORMAL_VOL", "TREND_HIGH_VOL"})
RANGE = frozenset({"RANGE_NORMAL_VOL", "RANGE_BOUND"})
BULL_BO, BEAR_BO = "BREAKOUT_EVENT_BULLISH", "BREAKOUT_EVENT_BEARISH"
IMMEDIATE = frozenset({"EXPIRY_DAY", "DETECTOR_ERROR"})
# a latched breakout yields to these (and to the opposite breakout)
OVERRIDE_LATCH = IMMEDIATE | {"HIGH_VOL_CHOP"}


def is_off(p) -> bool:
    return (p.regime_eval_minutes <= 1 and not p.regime_hysteresis
            and p.regime_atr_smooth <= 0 and p.regime_breakout_hold_minutes <= 0
            and p.regime_confirm_evals <= 1)


@dataclass
class _Latch:
    label: str
    level: float
    until: datetime


@dataclass
class _DayState:
    adopted: Optional[str] = None
    pending: Optional[str] = None
    pending_n: int = 0
    atr_ema: Optional[float] = None
    latch: Optional[_Latch] = None


class RegimeStabilizer:
    def __init__(self) -> None:
        self._state: dict[tuple[str, date], _DayState] = {}

    def reset(self) -> None:
        self._state.clear()

    def step(self, *, underlying: str, day: date, now_ist: datetime, indicators, config,
             classify: Callable, p) -> tuple[str, object]:
        """The label for this candle and the indicators it was judged on.
        *classify(indicators, config)* is the plain classifier."""
        if is_off(p):
            return classify(indicators, config), indicators

        key = (underlying, day)
        if key not in self._state:                       # a new day: drop old days
            self._state = {k: v for k, v in self._state.items() if k[1] >= day}
            self._state[key] = _DayState()
        st = self._state[key]

        # 1. cadence: between N-minute closes the adopted label holds
        n = max(1, int(p.regime_eval_minutes))
        if n > 1 and st.adopted is not None and now_ist.minute % n != 0:
            return st.adopted, indicators

        # 2. smoothed ATR percentile
        ind = indicators
        if p.regime_atr_smooth > 0:
            alpha = 2.0 / (p.regime_atr_smooth + 1.0)
            raw = float(indicators.atr_percentile)
            st.atr_ema = raw if st.atr_ema is None else st.atr_ema + alpha * (raw - st.atr_ema)
            ind = replace(indicators, atr_percentile=round(st.atr_ema, 4))

        # 3. hysteresis from the adopted label
        cfg = config
        if p.regime_hysteresis and st.adopted is not None:
            if st.adopted in TREND:
                cfg = replace(config, ADX_TREND_THRESHOLD=config.ADX_RANGE_THRESHOLD)
            elif st.adopted in RANGE:
                cfg = replace(config, ADX_RANGE_THRESHOLD=config.ADX_TREND_THRESHOLD)
            elif st.adopted == "LOW_VOL_COMPRESSION":
                cfg = replace(config, VWAP_PROXIMITY_PERCENT=max(
                    config.VWAP_PROXIMITY_PERCENT, p.regime_vwap_exit_pct))

        label = classify(ind, cfg)

        # 4. breakout latch
        if p.regime_breakout_hold_minutes > 0:
            label = self._latch(st, label, ind, now_ist, p)

        # 5. confirmation
        if label in IMMEDIATE or st.adopted is None or p.regime_confirm_evals <= 1:
            st.adopted, st.pending, st.pending_n = label, None, 0
        elif label == st.adopted:
            st.pending, st.pending_n = None, 0
        else:
            st.pending_n = st.pending_n + 1 if label == st.pending else 1
            st.pending = label
            if st.pending_n >= p.regime_confirm_evals:
                st.adopted, st.pending, st.pending_n = label, None, 0
        return st.adopted, ind

    @staticmethod
    def _latch(st: _DayState, label: str, ind, now: datetime, p) -> str:
        price = float(ind.price)
        if label in (BULL_BO, BEAR_BO):
            if label == BULL_BO:
                broken = [lv for lv in (ind.orb_high, ind.pdh) if lv is not None and price > lv]
                level = max(broken) if broken else price
            else:
                broken = [lv for lv in (ind.orb_low, ind.pdl) if lv is not None and price < lv]
                level = min(broken) if broken else price
            st.latch = _Latch(label, level,
                              now + timedelta(minutes=p.regime_breakout_hold_minutes))
            return label
        lt = st.latch
        if lt is None:
            return label
        failed = (price < lt.level - p.regime_breakout_fail_pts if lt.label == BULL_BO
                  else price > lt.level + p.regime_breakout_fail_pts)
        if label in OVERRIDE_LATCH or failed or now >= lt.until:
            st.latch = None
            return label
        return lt.label


regime_stabilizer = RegimeStabilizer()
