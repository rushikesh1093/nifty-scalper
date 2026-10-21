"""
Regime rules and classification logic.

Provides:
  RegimeRule       — NamedTuple defining strategy/direction/sizing per regime.
  REGIME_RULES     — Dict mapping all 13 regime names to their RegimeRule (§6.2).
  NEVER_ENABLED, POSITION_MULTIPLIERS — the declared sets startup checks 3-4 assert.
  get_regime_rule  — Retrieve a rule with dynamic ATR-based size adjustment.
  classify_regime  — Classify the current regime using a strict 9-priority order.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, NamedTuple, Optional

from app.strategies.names import StrategyName as S

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


# ── REGIME_RULES (design §6.2) ───────────────────────────────────────────────
# 13 keys: 7 tradeable regimes, 5 no-trade regimes and the legacy UNKNOWN
# alias. Every one of the 12 strategies is enabled somewhere (NEVER_ENABLED is
# empty) and every multiplier is in POSITION_MULTIPLIERS. Startup checks 2-5
# assert all of that (app/services/trading/startup_checks.py).

_BUY = frozenset({"BUY"})
_SELL = frozenset({"SELL"})
_BOTH = frozenset({"BUY", "SELL"})
_NONE: frozenset = frozenset()

# Explicit no-trade policy for safety-critical regimes. Keep these entries
# centralized so the runner and API cannot drift into different eligibility.
SAFETY_BLOCKED_REGIMES = frozenset({
    "UNCLASSIFIED", "REGIME_TRANSITION", "HIGH_VOL_CHOP", "DETECTOR_ERROR", "UNKNOWN",
})

_TREND = frozenset({S.VWAP_PULLBACK.value, S.EMA_VWAP.value, S.SUPERTREND_VWAP.value,
                    S.CPR_VWAP.value, S.OI_SPIKE.value})
_BREAKOUT = frozenset({S.ORB_VWAP.value, S.PDH_PDL.value, S.BOLLINGER_BREAKOUT.value,
                       S.OI_SPIKE.value, S.IV_CRUSH.value})
# TREND_ENGINE trades only where the detector sees a trend or a breakout; range,
# compression and every no-trade regime keep it idle (its own classifier must
# ALSO find a confirmed UPTREND/DOWNTREND — two independent checks agree).
_ENGINE = frozenset({S.TREND_ENGINE.value})

REGIME_RULES: dict[str, RegimeRule] = {
    # ── Trend regimes ────────────────────────────────────────────────────────
    "BULL_TREND_NORMAL_VOL": RegimeRule(
        enabled_strategies=_TREND | _ENGINE, allowed_directions=_BUY, position_multiplier=1.0,
        notes="Directional continuation with tradeable volatility — pullback and "
              "trend-confirmation edges, CE only",
    ),
    "BEAR_TREND_NORMAL_VOL": RegimeRule(
        enabled_strategies=_TREND | _ENGINE, allowed_directions=_SELL, position_multiplier=1.0,
        notes="Mirror of BULL_TREND_NORMAL_VOL — PE only",
    ),
    "TREND_HIGH_VOL": RegimeRule(
        enabled_strategies=frozenset({S.VWAP_PULLBACK.value, S.SUPERTREND_VWAP.value,
                                      S.IV_CRUSH.value}) | _ENGINE,
        # Both directions here; should_allow_signal narrows by price vs VWAP
        allowed_directions=_BOTH, position_multiplier=0.5,
        notes="Trend intact but ATR elevated: the two widest-stop trend entries plus "
              "the IV-expansion edge, half size",
    ),
    # ── Breakout regimes ─────────────────────────────────────────────────────
    "BREAKOUT_EVENT_BULLISH": RegimeRule(
        enabled_strategies=_BREAKOUT | _ENGINE, allowed_directions=_BUY,
        position_multiplier=1.0,          # 0.5 when ATR_percentile > 70 (get_regime_rule)
        notes="Level broken on volume — range/level breakout edges",
    ),
    "BREAKOUT_EVENT_BEARISH": RegimeRule(
        enabled_strategies=_BREAKOUT | _ENGINE, allowed_directions=_SELL,
        position_multiplier=1.0,          # 0.5 when ATR_percentile > 70 (get_regime_rule)
        notes="Mirror of BREAKOUT_EVENT_BULLISH",
    ),
    # ── Compression / range regimes ──────────────────────────────────────────
    "LOW_VOL_COMPRESSION": RegimeRule(
        enabled_strategies=frozenset({S.BOLLINGER_BREAKOUT.value, S.ORB_VWAP.value,
                                      S.CPR_VWAP.value, S.GAP_VWAP_FILL.value}),
        allowed_directions=_BOTH, position_multiplier=1.0,
        notes="Coiled tape: squeeze-expansion and narrow-CPR/gap-fill edges",
    ),
    "RANGE_NORMAL_VOL": RegimeRule(
        enabled_strategies=frozenset({S.VWAP_REVERSAL.value, S.RSI_VWAP.value,
                                      S.CPR_VWAP.value, S.GAP_VWAP_FILL.value}),
        allowed_directions=_BOTH, position_multiplier=1.0,
        notes="Mean reversion around VWAP / pivot / gap",
    ),
    # ── No-trade regimes (D7) ────────────────────────────────────────────────
    "HIGH_VOL_CHOP": RegimeRule(
        enabled_strategies=_NONE, allowed_directions=_NONE, position_multiplier=0.0,
        notes="High volatility chop — unfavourable by definition (spec Req 8.2/8.3)",
    ),
    "REGIME_TRANSITION": RegimeRule(
        enabled_strategies=_NONE, allowed_directions=_NONE, position_multiplier=0.0,
        notes="Regime transition detected — pause trading during instability",
    ),
    "EXPIRY_DAY": RegimeRule(
        enabled_strategies=_NONE, allowed_directions=_NONE, position_multiplier=0.0,
        notes="Option expiry day — gamma/pin risk",
    ),
    "UNCLASSIFIED": RegimeRule(
        enabled_strategies=_NONE, allowed_directions=_NONE, position_multiplier=0.0,
        notes="No classifier condition matched — no-trade until the regime "
              "histogram says otherwise",
    ),
    "DETECTOR_ERROR": RegimeRule(
        enabled_strategies=_NONE, allowed_directions=_NONE, position_multiplier=0.0,
        notes="The detector raised, or a regime name was not recognised",
    ),
    "UNKNOWN": RegimeRule(
        enabled_strategies=_NONE, allowed_directions=_NONE, position_multiplier=0.0,
        notes="Legacy alias, never emitted — resolves historical rows only",
    ),
}

# Every strategy is mapped to at least one regime: nothing is excluded on purpose.
NEVER_ENABLED: frozenset[str] = frozenset()

# The only multipliers a rule (including the dynamic ATR adjustment) may produce.
POSITION_MULTIPLIERS: frozenset[float] = frozenset({0.0, 0.5, 1.0})


# ── get_regime_rule ───────────────────────────────────────────────────────────

def get_regime_rule(regime: str, atr_percentile: float = 50.0) -> RegimeRule:
    """
    Return the ``RegimeRule`` for *regime* with dynamic sizing adjustments.

    An unrecognised name gets the ``DETECTOR_ERROR`` rule (no strategies,
    0.0) and is logged at ERROR. ``UNKNOWN`` still resolves, via the retained
    legacy key.

    Dynamic adjustment
    ------------------
    For ``BREAKOUT_EVENT_BULLISH`` and ``BREAKOUT_EVENT_BEARISH``, the base
    position_multiplier is 1.0 but is reduced to 0.5 whenever
    ``atr_percentile > 70`` (per Requirements 5.4 and 5.5).
    """
    rule = REGIME_RULES.get(regime)
    if rule is None:
        logger.error("[regime_rules] unrecognised regime %r — applying the DETECTOR_ERROR rule",
                     regime)
        return REGIME_RULES["DETECTOR_ERROR"]

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
    previous_regime: Optional[str],
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
    11. UNCLASSIFIED          — no conditions matched

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

    Hysteresis (previous_regime = the last STABLE regime)
    -----------------------------------------------------
    Readings that hover on a threshold must not flip the regime every minute.
    Staying in a regime needs a weaker reading than entering it:
      * in a trend regime, the trend holds while ADX > ADX_RANGE_THRESHOLD
        (enter needs ADX > ADX_TREND_THRESHOLD);
      * in a range regime, the range holds while ADX < ADX_TREND_THRESHOLD
        (enter needs ADX < ADX_RANGE_THRESHOLD) — this also closes the
        18–22 ADX "UNCLASSIFIED" gap for an established range;
      * LOW_VOL_COMPRESSION holds while ATR%ile < ATR_LOW + 15 and the price is
        within 1.5× the VWAP-proximity band;
      * high volatility holds while ATR%ile > ATR_HIGH − 15.

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

    # Invalid market data must never accidentally satisfy a comparison and
    # produce a tradeable regime. Fail closed until the next valid candle.
    import math
    numeric_values = (atr_pct, price, vwap, indicators.vwap_cross_count,
                      volume, avg_volume)
    if (adx is not None and not math.isfinite(float(adx))) or any(
        not math.isfinite(float(value)) for value in numeric_values
    ):
        logger.warning("[classify_regime] invalid indicator values for %s", underlying)
        return "DETECTOR_ERROR"
    orb_high = indicators.orb_high
    orb_low = indicators.orb_low
    pdh = indicators.pdh
    pdl = indicators.pdl

    # Track which conditions are satisfied for priority-resolution logging
    matched: list[str] = []

    # ── Hysteresis bands (see docstring) ─────────────────────────────────────
    prev = previous_regime or ""
    in_trend = prev in ("BULL_TREND_NORMAL_VOL", "BEAR_TREND_NORMAL_VOL", "TREND_HIGH_VOL")
    in_range = prev in ("RANGE_NORMAL_VOL", "LOW_VOL_COMPRESSION")
    trend_thr = config.ADX_RANGE_THRESHOLD if in_trend else config.ADX_TREND_THRESHOLD
    range_thr = config.ADX_TREND_THRESHOLD if in_range else config.ADX_RANGE_THRESHOLD
    low_pct = config.ATR_LOW_PERCENTILE + (15.0 if prev == "LOW_VOL_COMPRESSION" else 0.0)
    high_pct = config.ATR_HIGH_PERCENTILE - (
        15.0 if prev in ("TREND_HIGH_VOL", "HIGH_VOL_CHOP") else 0.0)
    prox_pct = config.VWAP_PROXIMITY_PERCENT * (1.5 if prev == "LOW_VOL_COMPRESSION" else 1.0)

    # ── Priority 1: EXPIRY_DAY ────────────────────────────────────────────────
    if is_expiry_day:
        logger.debug(
            "[classify_regime] underlying=%s → EXPIRY_DAY (priority 1)",
            underlying,
        )
        return "EXPIRY_DAY"

    # ── Priority 3: HIGH_VOL_CHOP ─────────────────────────────────────────────
    high_vol_chop = (
        atr_pct > high_pct
        and indicators.vwap_cross_count >= config.VWAP_CROSS_CHOP_THRESHOLD
    )
    if high_vol_chop:
        matched.append("HIGH_VOL_CHOP")

    # ── Priority 4 & 5: BREAKOUT_EVENT ───────────────────────────────────────
    # Volume is the near-month index FUTURE's (strategy_runner merges it into
    # the spot candles; NSE index spot itself reports volume=0). With no volume
    # a breakout is unconfirmed, so it is NOT a breakout regime — the same rule
    # the strategies' _vol_surge applies, so the two layers agree.
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
        config.ATR_LOW_PERCENTILE <= atr_pct <= high_pct
    )
    trend_adx = adx is not None and adx > trend_thr

    # A quiet trend is still a trend: the ATR band only separates the HIGH-vol
    # trend regime; a low ATR%ile no longer knocks an ADX-confirmed trend out
    # into LOW_VOL_COMPRESSION / UNCLASSIFIED.
    trend_vol_ok = atr_pct <= high_pct
    bull_trend_normal = trend_adx and price > vwap and trend_vol_ok
    bear_trend_normal = trend_adx and price < vwap and trend_vol_ok
    if bull_trend_normal:
        matched.append("BULL_TREND_NORMAL_VOL")
    if bear_trend_normal:
        matched.append("BEAR_TREND_NORMAL_VOL")

    # ── Priority 8: TREND_HIGH_VOL ────────────────────────────────────────────
    trend_high_vol = trend_adx and atr_pct > high_pct
    if trend_high_vol:
        matched.append("TREND_HIGH_VOL")

    # ── Priority 9: LOW_VOL_COMPRESSION ──────────────────────────────────────
    vwap_proximity_pct = (abs(price - vwap) / vwap * 100) if vwap > 0 else float("inf")
    low_vol_compression = (
        atr_pct < low_pct
        and vwap_proximity_pct < prox_pct
    )
    if low_vol_compression:
        matched.append("LOW_VOL_COMPRESSION")

    # ── Priority 10: RANGE_NORMAL_VOL ─────────────────────────────────────────
    range_adx = adx is not None and adx < range_thr
    range_normal_vol = range_adx and (normal_vol_band or (
        prev == "RANGE_NORMAL_VOL" and atr_pct < config.ATR_LOW_PERCENTILE))
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
        # An ordinary tape that matched no condition — not a failure (§6.2)
        regime = "UNCLASSIFIED"

    logger.debug(
        "[classify_regime] underlying=%s → %s",
        underlying,
        regime,
    )
    return regime
