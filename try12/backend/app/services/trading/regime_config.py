"""
Regime configuration loader.

Provides the RegimeConfig dataclass (mirrors RegimeConfigModel thresholds)
and two helpers:

  load_config(underlying, db_session)
      — Query DB for underlying-specific or global config row.
        Falls back to hardcoded defaults on any failure.

  reload_config_if_stale(underlying, max_age_seconds, db_session)
      — Module-level TTL cache around load_config.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# ── Module-level TTL cache ────────────────────────────────────────────────────
# key: underlying (or "GLOBAL"), value: (RegimeConfig, cached_at: datetime)
_config_cache: dict[str, tuple["RegimeConfig", datetime]] = {}


# ── Dataclass ─────────────────────────────────────────────────────────────────

@dataclass
class RegimeConfig:
    """
    Thresholds used by the Regime_Detector.
    Defaults mirror the seed row in RegimeConfigModel.
    """
    ADX_TREND_THRESHOLD: float = 22.0
    ADX_RANGE_THRESHOLD: float = 18.0
    ATR_LOW_PERCENTILE: float = 30.0
    ATR_HIGH_PERCENTILE: float = 70.0
    VWAP_PROXIMITY_PERCENT: float = 0.2
    VWAP_CROSS_CHOP_THRESHOLD: int = 8
    VOLUME_SURGE_MULTIPLIER: float = 1.5
    REGIME_TRANSITION_WINDOW: int = 2
    TRANSITION_COOLDOWN_CANDLES: int = 3


# Hard-coded defaults used as fallback reference values
_DEFAULTS = RegimeConfig()


def _positive(value, default):
    """Return *value* if it is a positive number, else *default*."""
    try:
        v = float(value)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _positive_int(value, default: int) -> int:
    """Return *value* as a positive int, else *default*."""
    try:
        v = int(value)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def load_config(underlying: str, db_session=None) -> "RegimeConfig":
    """
    Load RegimeConfig for *underlying* from the database.

    Priority:
      1. Row where underlying == <underlying>
      2. Row where underlying IS NULL  (global default)
      3. Hardcoded Python defaults     (if DB is unavailable)

    Every field is individually validated; invalid/missing values fall back
    to the hardcoded default for that field.

    Parameters
    ----------
    underlying:
        Instrument name, e.g. "NIFTY" or "BANKNIFTY".
    db_session:
        A **synchronous** SQLAlchemy Session.  Pass None to skip DB lookup
        and return hardcoded defaults immediately.

    Returns
    -------
    RegimeConfig
        Never raises — all exceptions are caught and logged.
    """
    if db_session is None:
        logger.warning(
            "[RegimeConfig] No DB session provided for underlying=%s — "
            "using hardcoded defaults.",
            underlying,
        )
        return RegimeConfig()

    try:
        from app.db.models.regime import RegimeConfigModel
        from sqlalchemy import select

        # 1. Try underlying-specific row first
        row: Optional[RegimeConfigModel] = (
            db_session.execute(
                select(RegimeConfigModel).where(
                    RegimeConfigModel.underlying == underlying,
                    RegimeConfigModel.is_active == True,
                )
            )
            .scalars()
            .first()
        )

        # 2. Fall back to global row (underlying IS NULL)
        if row is None:
            row = (
                db_session.execute(
                    select(RegimeConfigModel).where(
                        RegimeConfigModel.underlying == None,  # noqa: E711
                        RegimeConfigModel.is_active == True,
                    )
                )
                .scalars()
                .first()
            )

        if row is None:
            logger.warning(
                "[RegimeConfig] No active config row found for underlying=%s "
                "(or global) — using hardcoded defaults.",
                underlying,
            )
            return RegimeConfig()

        # 3. Build config, validating each field individually
        cfg = RegimeConfig(
            ADX_TREND_THRESHOLD=_positive(
                row.adx_trend_threshold, _DEFAULTS.ADX_TREND_THRESHOLD
            ),
            ADX_RANGE_THRESHOLD=_positive(
                row.adx_range_threshold, _DEFAULTS.ADX_RANGE_THRESHOLD
            ),
            ATR_LOW_PERCENTILE=_positive(
                row.atr_low_percentile, _DEFAULTS.ATR_LOW_PERCENTILE
            ),
            ATR_HIGH_PERCENTILE=_positive(
                row.atr_high_percentile, _DEFAULTS.ATR_HIGH_PERCENTILE
            ),
            VWAP_PROXIMITY_PERCENT=_positive(
                row.vwap_proximity_percent, _DEFAULTS.VWAP_PROXIMITY_PERCENT
            ),
            VWAP_CROSS_CHOP_THRESHOLD=_positive_int(
                row.vwap_cross_chop_threshold, _DEFAULTS.VWAP_CROSS_CHOP_THRESHOLD
            ),
            VOLUME_SURGE_MULTIPLIER=_positive(
                row.volume_surge_multiplier, _DEFAULTS.VOLUME_SURGE_MULTIPLIER
            ),
            REGIME_TRANSITION_WINDOW=_positive_int(
                row.regime_transition_window, _DEFAULTS.REGIME_TRANSITION_WINDOW
            ),
            TRANSITION_COOLDOWN_CANDLES=_positive_int(
                row.transition_cooldown_candles, _DEFAULTS.TRANSITION_COOLDOWN_CANDLES
            ),
        )

        scope = underlying if row.underlying else "GLOBAL"
        logger.info(
            "[RegimeConfig] Loaded config for underlying=%s (scope=%s): "
            "adx_trend=%.2f adx_range=%.2f atr_pct=[%.1f,%.1f] "
            "vwap_prox=%.3f vwap_chop=%d vol_surge=%.2f "
            "transition_window=%d cooldown=%d",
            underlying, scope,
            cfg.ADX_TREND_THRESHOLD, cfg.ADX_RANGE_THRESHOLD,
            cfg.ATR_LOW_PERCENTILE, cfg.ATR_HIGH_PERCENTILE,
            cfg.VWAP_PROXIMITY_PERCENT, cfg.VWAP_CROSS_CHOP_THRESHOLD,
            cfg.VOLUME_SURGE_MULTIPLIER,
            cfg.REGIME_TRANSITION_WINDOW, cfg.TRANSITION_COOLDOWN_CANDLES,
        )
        return cfg

    except Exception as exc:
        logger.warning(
            "[RegimeConfig] DB query failed for underlying=%s: %s — "
            "falling back to hardcoded defaults.",
            underlying, exc,
        )
        return RegimeConfig()


def reload_config_if_stale(
    underlying: str,
    max_age_seconds: int = 60,
    db_session=None,
) -> "RegimeConfig":
    """
    Return a cached RegimeConfig, refreshing it when the entry is missing
    or older than *max_age_seconds*.

    Parameters
    ----------
    underlying:
        Instrument name, e.g. "NIFTY" or "BANKNIFTY".
    max_age_seconds:
        TTL for cache entries.  Defaults to 60 s.
    db_session:
        Synchronous SQLAlchemy Session passed through to load_config().

    Returns
    -------
    RegimeConfig
        Always returns a valid config — never raises.
    """
    now = datetime.now(timezone.utc)

    cached = _config_cache.get(underlying)
    if cached is not None:
        cfg, cached_at = cached
        age = (now - cached_at).total_seconds()
        if age < max_age_seconds:
            logger.debug(
                "[RegimeConfig] Cache hit for underlying=%s (age=%.1fs)",
                underlying, age,
            )
            return cfg

    logger.debug(
        "[RegimeConfig] Cache miss / stale for underlying=%s — reloading.",
        underlying,
    )
    cfg = load_config(underlying, db_session=db_session)
    _config_cache[underlying] = (cfg, now)
    return cfg
