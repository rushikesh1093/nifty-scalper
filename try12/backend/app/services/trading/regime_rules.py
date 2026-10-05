"""
Regime rules and classification logic.

Provides:
  RegimeRule       — NamedTuple defining strategy/direction/sizing per regime.
  REGIME_RULES     — Dict mapping all 11 regime names to their RegimeRule.
  get_regime_rule  — Retrieve a rule with dynamic ATR-based size adjustment.
  classify_regime  — Classify the current regime using a strict 9-priority order.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, NamedTuple, Optional

if TYPE_CHECKING:
    from app.services.trading.regime_indicators import RegimeIndicators
    from app.services.trading.regime_config import RegimeConfig

logger = logging.getLogger(__name__)


# ── RegimeRule ────────────────────────────────────────────────────────────────

class RegimeRule(NamedTuple):
    """
    Defines the trading behaviour that applies for a given regime.

    Fields
    ------
    enabled_strategies:
        frozenset of strategy names that may generate signals in this regime.
    allowed_directions:
        frozenset of directions (subset of {"BUY", "SELL"}) permitted by the
        regime.  An empty frozenset means no trades are allowed.
    position_multiplier:
        Scaling factor applied to the base lot size.  One of 0.0 (no trade),
        0.5 (half size), or 1.0 (full size).
    notes:
        Human-readable explanation of why this rule applies.
    """

    enabled_strategies: frozenset
    allowed_directions: frozenset
    position_multiplier: float
    notes: str


# ── REGIME_RULES ──────────────────────────────────────────────────────────────

REGIME_RULES: dict[str, RegimeRule] = {
    # ── Trend regimes ────────────────────────────────────────────────────────
    "BULL_TREND_NORMAL_VOL": RegimeRule(
        enabled_strategies=frozenset({"VWAP_PULLBACK"}),
        allowed_directions=frozenset({"BUY"}),
        position_multiplier=1.0,
        notes="Confirmed uptrend with normal volatility — CE trades only",
    ),
    "BEAR_TREND_NORMAL_VOL": RegimeRule(
        enabled_strategies=frozenset({"VWAP_PULLBACK"}),
        allowed_directions=frozenset({"SELL"}),
        position_multiplier=1.0,
        notes="Confirmed downtrend with normal volatility — PE trades only",
    ),
    "TREND_HIGH_VOL": RegimeRule(
        enabled_strategies=frozenset({"VWAP_PULLBACK"}),
        # Both directions allowed; filtered at signal time by price vs VWAP
        allowed_directions=frozenset({"BUY", "SELL"}),
        position_multiplier=0.5,
        notes="Trending but elevated volatility — reduced position sizing",
    ),
    # ── Breakout regimes ─────────────────────────────────────────────────────
    "BREAKOUT_EVENT_BULLISH": RegimeRule(
        enabled_strategies=frozenset({"ORB_VWAP"}),
        allowed_directions=frozenset({"BUY"}),
        # Adjusted to 0.5 dynamically when ATR_percentile > 70
        position_multiplier=1.0,
        notes="Volume-confirmed bullish breakout",
    ),
    "BREAKOUT_EVENT_BEARISH": RegimeRule(
        enabled_strategies=frozenset({"ORB_VWAP"}),
        allowed_directions=frozenset({"SELL"}),
        # Adjusted to 0.5 dynamically when ATR_percentile > 70
        position_multiplier=1.0,
        notes="Volume-confirmed bearish breakout",
    ),
    # ── Compression / range regimes ──────────────────────────────────────────
    "LOW_VOL_COMPRESSION": RegimeRule(
        enabled_strategies=frozenset({"BOLLINGER_BREAKOUT"}),
        allowed_directions=frozenset({"BUY", "SELL"}),
        position_multiplier=1.0,
        notes="Low volatility compression — await expansion breakout",
    ),
    "RANGE_NORMAL_VOL": RegimeRule(
        enabled_strategies=frozenset({"VWAP_REVERSAL"}),
        allowed_directions=frozenset({"BUY", "SELL"}),
        position_multiplier=1.0,
        notes="Range-bound with normal volatility — mean-reversion trades",
    ),
    # ── No-trade regimes ─────────────────────────────────────────────────────
    "HIGH_VOL_CHOP": RegimeRule(
        enabled_strategies=frozenset(),
        allowed_directions=frozenset(),
        position_multiplier=0.0,
        notes="High volatility chop — no trading to avoid whipsaws",
    ),
    "REGIME_TRANSITION": RegimeRule(
        enabled_strategies=frozenset(),
        allowed_directions=frozenset(),
        position_multiplier=0.0,
        notes="Regime transition detected — pause trading during instability",
    ),
    "EXPIRY_DAY": RegimeRule(
        enabled_strategies=frozenset(),
        allowed_directions=frozenset(),
        position_multiplier=0.0,
        notes="Option expiry day — no regime-based trades (let expiry guards handle)",
    ),
    "UNKNOWN": RegimeRule(
        enabled_strategies=frozenset(),
        allowed_directions=frozenset(),
        position_multiplier=0.0,
        notes="Regime classification failed or insufficient data — no trading",
    ),
}


# ── get_regime_rule ───────────────────────────────────────────────────────────

def get_regime_rule(regime: str, atr_percentile: float = 50.0) -> RegimeRule:
    """
    Return the ``RegimeRule`` for *regime* with dynamic sizing adjustments.

    Falls back to the UNKNOWN rule when *regime* is not found in
    ``REGIME_RULES``.

    Dynamic adjustment
    ------------------
    For ``BREAKOUT_EVENT_BULLISH`` and ``BREAKOUT_EVENT_BEARISH``, the base
    position_multiplier is 1.0 but is reduced to 0.5 whenever
    ``atr_percentile > 70`` (per Requirements 5.4 and 5.5).

    Parameters
    ----------
    regime:
        One of the 11 recognised regime name strings.
    atr_percentile:
        Current ATR percentile (0–100).  Defaults to 50.0 (neutral).

    Returns
    -------
    RegimeRule
        Possibly with an adjusted ``position_multiplier``.
    """
    rule = REGIME_RULES.get(regime, REGIME_RULES["UNKNOWN"])

    # Dynamic breakout size reduction under high volatility
    if regime in {"BREAKOUT_EVENT_BULLISH", "BREAKOUT_EVENT_BEARISH"}:
        if atr_percentile > 70:
            rule = rule._replace(position_multiplier=0.5)

    return rule


# ── classify_regime ───────────────────────────────────────────────────────────

def classify_regime(
    indicators: "RegimeIndicators",
    underlying: str,
    is_expiry_day: bool,
    previous_regime: Optional[str],  # noqa: F841  — transition handled externally
    config: "RegimeConfig",
) -> str:
    """
    Classify the current market regime using a strict 9-priority order.

    Priority (highest to lowest)
    ----------------------------
    1.  EXPIRY_DAY            — ``is_expiry_day`` is True
    2.  *(REGIME_TRANSITION handled externally — skipped here)*
    3.  HIGH_VOL_CHOP         — ATR%ile > HIGH AND vwap_crosses >= threshold
    4.  BREAKOUT_EVENT_BULLISH — ORB/PDH break up with volume AND price > VWAP
    5.  BREAKOUT_EVENT_BEARISH — ORB/PDL break down with volume AND price < VWAP
    6.  BULL_TREND_NORMAL_VOL — ADX > trend_thr, price > VWAP, normal ATR%ile
    7.  BEAR_TREND_NORMAL_VOL — ADX > trend_thr, price < VWAP, normal ATR%ile
    8.  TREND_HIGH_VOL        — ADX > trend_thr AND high ATR%ile
    9.  LOW_VOL_COMPRESSION   — low ATR%ile AND price hugs VWAP
    10. RANGE_NORMAL_VOL      — ADX < range_thr AND normal ATR%ile
    11. UNKNOWN               — no conditions matched

    Parameters
    ----------
    indicators:
        ``RegimeIndicators`` instance from ``regime_indicators.py``.
    underlying:
        Instrument name used only for logging context.
    is_expiry_day:
        True when today is the weekly options expiry for *underlying*.
    previous_regime:
        Not used directly; regime transition is detected externally.
    config:
        ``RegimeConfig`` holding all detection thresholds.

    Returns
    -------
    str
        One of the 11 regime name strings.
    """
    logger.debug(
        "[classify_regime] underlying=%s adx=%s atr_pct=%.1f vwap=%.2f "
        "price=%.2f vwap_crosses=%d volume=%.0f avg_volume=%.0f "
        "orb_high=%s orb_low=%s pdh=%s pdl=%s is_expiry=%s",
        underlying,
        f"{indicators.adx:.2f}" if indicators.adx is not None else "None",
        indicators.atr_percentile,
        indicators.vwap,
        indicators.price,
        indicators.vwap_cross_count,
        indicators.volume,
        indicators.avg_volume,
        indicators.orb_high,
        indicators.orb_low,
        indicators.pdh,
        indicators.pdl,
        is_expiry_day,
    )

    # ── Shorthand aliases ────────────────────────────────────────────────────
    adx = indicators.adx
    atr_pct = indicators.atr_percentile
    price = indicators.price
    vwap = indicators.vwap
    volume = indicators.volume
    avg_volume = indicators.avg_volume
    orb_high = indicators.orb_high
    orb_low = indicators.orb_low
    pdh = indicators.pdh
    pdl = indicators.pdl

    # Track which conditions are satisfied for priority-resolution logging
    matched: list[str] = []

    # ── Priority 1: EXPIRY_DAY ────────────────────────────────────────────────
    if is_expiry_day:
        logger.debug(
            "[classify_regime] underlying=%s → EXPIRY_DAY (priority 1)",
            underlying,
        )
        return "EXPIRY_DAY"

    # ── Priority 3: HIGH_VOL_CHOP ─────────────────────────────────────────────
    high_vol_chop = (
        atr_pct > config.ATR_HIGH_PERCENTILE
        and indicators.vwap_cross_count >= config.VWAP_CROSS_CHOP_THRESHOLD
    )
    if high_vol_chop:
        matched.append("HIGH_VOL_CHOP")

    # ── Priority 4 & 5: BREAKOUT_EVENT ───────────────────────────────────────
    # Volume-surge condition (requires avg_volume > 0)
    volume_surge = (
        avg_volume > 0
        and volume >= avg_volume * config.VOLUME_SURGE_MULTIPLIER
    )

    breakout_bull = (
        (
            (orb_high is not None and price > orb_high)
            or (pdh is not None and price > pdh)
        )
        and volume_surge
        and price > vwap
    )
    breakout_bear = (
        (
            (orb_low is not None and price < orb_low)
            or (pdl is not None and price < pdl)
        )
        and volume_surge
        and price < vwap
    )
    if breakout_bull:
        matched.append("BREAKOUT_EVENT_BULLISH")
    if breakout_bear:
        matched.append("BREAKOUT_EVENT_BEARISH")

    # ── Priority 6 & 7: BULL/BEAR_TREND_NORMAL_VOL ───────────────────────────
    normal_vol_band = (
        config.ATR_LOW_PERCENTILE <= atr_pct <= config.ATR_HIGH_PERCENTILE
    )
    trend_adx = adx is not None and adx > config.ADX_TREND_THRESHOLD

    bull_trend_normal = trend_adx and price > vwap and normal_vol_band
    bear_trend_normal = trend_adx and price < vwap and normal_vol_band
    if bull_trend_normal:
        matched.append("BULL_TREND_NORMAL_VOL")
    if bear_trend_normal:
        matched.append("BEAR_TREND_NORMAL_VOL")

    # ── Priority 8: TREND_HIGH_VOL ────────────────────────────────────────────
    trend_high_vol = trend_adx and atr_pct > config.ATR_HIGH_PERCENTILE
    if trend_high_vol:
        matched.append("TREND_HIGH_VOL")

    # ── Priority 9: LOW_VOL_COMPRESSION ──────────────────────────────────────
    vwap_proximity_pct = (abs(price - vwap) / vwap * 100) if vwap > 0 else float("inf")
    low_vol_compression = (
        atr_pct < config.ATR_LOW_PERCENTILE
        and vwap_proximity_pct < config.VWAP_PROXIMITY_PERCENT
    )
    if low_vol_compression:
        matched.append("LOW_VOL_COMPRESSION")

    # ── Priority 10: RANGE_NORMAL_VOL ─────────────────────────────────────────
    range_adx = adx is not None and adx < config.ADX_RANGE_THRESHOLD
    range_normal_vol = range_adx and normal_vol_band
    if range_normal_vol:
        matched.append("RANGE_NORMAL_VOL")

    # ── Log when priority resolution is applied ───────────────────────────────
    if len(matched) > 1:
        logger.debug(
            "[classify_regime] underlying=%s multiple conditions satisfied=%s "
            "— applying priority order",
            underlying,
            matched,
        )

    # ── Apply strict priority and return ─────────────────────────────────────
    if high_vol_chop:
        regime = "HIGH_VOL_CHOP"
    elif breakout_bull:
        regime = "BREAKOUT_EVENT_BULLISH"
    elif breakout_bear:
        regime = "BREAKOUT_EVENT_BEARISH"
    elif bull_trend_normal:
        regime = "BULL_TREND_NORMAL_VOL"
    elif bear_trend_normal:
        regime = "BEAR_TREND_NORMAL_VOL"
    elif trend_high_vol:
        regime = "TREND_HIGH_VOL"
    elif low_vol_compression:
        regime = "LOW_VOL_COMPRESSION"
    elif range_normal_vol:
        regime = "RANGE_NORMAL_VOL"
    else:
        regime = "UNKNOWN"

    logger.debug(
        "[classify_regime] underlying=%s → %s",
        underlying,
        regime,
    )
    return regime
