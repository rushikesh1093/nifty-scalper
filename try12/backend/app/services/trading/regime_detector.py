"""
Regime Detector.

Main entry point for classifying the current market regime from candle data.

Imports RegimeState from regime_persistence (defined there to avoid circular
dependencies), and composes functionality from:
  - regime_indicators  : build_regime_indicators, RegimeIndicators
  - regime_rules       : classify_regime
  - regime_config      : RegimeConfig, reload_config_if_stale
  - regime_persistence : RegimePersistence, RegimeState (shared dataclass)

Module-level singleton ``regime_detector`` is created at the bottom of the
file.

Requirements: 1.1, 1.2, 1.3, 1.6, 1.7, 9.1, 9.5, 15.1, 15.6, 15.7
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.trading.regime_indicators import (
    RegimeIndicators,
    build_regime_indicators,
)
from app.services.trading.regime_persistence import (
    RegimePersistence,
    RegimeState,
    regime_persistence as _persistence_singleton,
)
from app.services.trading.regime_rules import classify_regime
from app.services.trading.regime_config import RegimeConfig, reload_config_if_stale
from app.runtime.context import ctx, now_utc

logger = logging.getLogger(__name__)

# How often to emit a DEBUG heartbeat log when regime is unchanged (Req 15.6)
_LOG_INTERVAL = timedelta(minutes=5)

# Design §5: the transition cooldown is bounded so the retained history always
# holds at least two non-transition entries after a full cooldown.
# TradingParams.validate() enforces both bounds (§6.3 check 6).
TRANSITION_COOLDOWN_CANDLES_MAX = 8


def _history_len(cooldown: int) -> int:
    return max(5, cooldown + 2)


class RegimeDetector:
    """
    Detects and classifies market regimes from 1-minute candle data.

    One instance per application process is sufficient (all state is keyed by
    ``underlying``).  A module-level singleton is provided at the bottom of
    this file.
    """

    def __init__(self) -> None:
        # Use the shared persistence singleton so Redis connections are reused
        self._persistence: RegimePersistence = _persistence_singleton

        # Load a global default config on startup (no DB session available yet)
        self._config: RegimeConfig = reload_config_if_stale(
            "GLOBAL", db_session=None
        )

        # In-memory regime history: underlying → last _history_len(cooldown) states
        self._regime_history: dict[str, list[RegimeState]] = {}

        # Tracks the last time we emitted a "regime unchanged" heartbeat log
        self._last_log_time: dict[str, datetime] = {}
        # Require two consecutive observations before treating a new stable
        # regime as a real flip. This filters one-bar indicator glitches.
        self._candidate_regime: dict[str, str] = {}
        self._candidate_streak: dict[str, int] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    async def detect_regime(
        self,
        candles: list[dict],
        indicators: dict,
        underlying: str,
        is_expiry_day: bool = False,
        db_session=None,
        history: Optional[list[dict]] = None,
    ) -> RegimeState:
        """
        Classify the current market regime from candle data.

        Steps:
          1. Reload per-underlying config (TTL-cached).
          2. Build RegimeIndicators from candles + pre-computed indicators dict.
          3. Classify candidate regime via classify_regime().
          4. Check for rapid regime transition; apply or decrement cooldown.
          5. Build RegimeState, update in-memory history, persist to Redis/DB.
          6. Log regime change at INFO; log unchanged regime every 5 min DEBUG.

        Falls back to ``_fallback_regime()`` and logs ERROR on any unhandled
        exception (Req 1.7, 15.7).

        Args:
            candles:       OHLCV dicts in chronological order (newest last).
            indicators:    Output of ``compute_all()`` from the indicators module.
            underlying:    Instrument name, e.g. "NIFTY" or "BANKNIFTY".
            is_expiry_day: True when today is the weekly options expiry.
            db_session:    Optional async SQLAlchemy session for DB persistence.
            history:       Optional multi-day candles (ADX / ATR-percentile warm-up).

        Returns:
            RegimeState — never raises.
        """
        try:
            # Step 1: Per-underlying config through the RegimeConfigPort
            # (live: the TTL cache + loader; replay: the run's frozen config).
            config: RegimeConfig = ctx().regime_config.for_underlying(underlying)

            # Expiry day is the no-trade EXPIRY_DAY regime unless trading on
            # expiry is enabled; then the day is classified on its indicators.
            from app.runtime.context import params as _params
            classify_as_expiry = bool(is_expiry_day) and not _params().trade_on_expiry_day

            # Step 2: Build regime-specific indicators
            regime_indicators: RegimeIndicators = build_regime_indicators(
                candles, indicators, config, history=history
            )

            # Step 3: Previous states. After a restart the in-memory history is
            # empty, so rehydrate once from Redis; load_regime_history drops
            # every state from another trading day (§5).
            if underlying not in self._regime_history:
                self._regime_history[underlying] = (
                    await self._persistence.load_regime_history(underlying)
                )[-_history_len(config.TRANSITION_COOLDOWN_CANDLES):]
            history: list[RegimeState] = list(self._regime_history[underlying])
            previous_regime: Optional[str] = (
                history[-1].regime if history else None
            )

            # Step 4: Determine if we are in (or should enter) a cooldown
            current_cooldown = (
                history[-1].transition_cooldown_remaining if history else 0
            )

            if current_cooldown > 0:
                # Already in cooldown — decrement counter
                new_cooldown = current_cooldown - 1
                if new_cooldown > 0:
                    # Still cooling down → keep REGIME_TRANSITION
                    regime = "REGIME_TRANSITION"
                    transition_cooldown_remaining = new_cooldown
                    logger.debug(
                        "[RegimeDetector] underlying=%s cooldown active: "
                        "%d candle(s) remaining",
                        underlying, new_cooldown,
                    )
                else:
                    # Cooldown expired → classify normally
                    regime = classify_regime(
                        regime_indicators, underlying, classify_as_expiry,
                        previous_regime, config
                    )
                    transition_cooldown_remaining = 0
            else:
                # Classify candidate regime first
                candidate_regime = classify_regime(
                    regime_indicators, underlying, classify_as_expiry,
                    previous_regime, config
                )

                # Confirm a candidate across two observations before allowing a
                # transition. A single-bar indicator spike must not repeatedly
                # lock the runner in REGIME_TRANSITION.
                if self._candidate_regime.get(underlying) == candidate_regime:
                    self._candidate_streak[underlying] = self._candidate_streak.get(underlying, 0) + 1
                else:
                    self._candidate_regime[underlying] = candidate_regime
                    self._candidate_streak[underlying] = 1
                transition_detected = (
                    self._candidate_streak[underlying] >= 2
                    and self.check_regime_transition(
                        candidate_regime, history, config.REGIME_TRANSITION_WINDOW
                    )
                )

                if transition_detected and candidate_regime not in (
                    "EXPIRY_DAY", "REGIME_TRANSITION"
                ):
                    # Enter transition cooldown (Req 9.1, 9.5)
                    regime = "REGIME_TRANSITION"
                    transition_cooldown_remaining = config.TRANSITION_COOLDOWN_CANDLES
                    logger.info(
                        "[RegimeDetector] underlying=%s REGIME_TRANSITION detected "
                        "(was %s → candidate %s); cooldown=%d candles",
                        underlying, previous_regime, candidate_regime,
                        config.TRANSITION_COOLDOWN_CANDLES,
                    )
                else:
                    regime = candidate_regime
                    transition_cooldown_remaining = 0

            # Step 5: Build RegimeState
            now = now_utc()
            state = RegimeState(
                regime=regime,
                underlying=underlying,
                timestamp=now,
                indicators=regime_indicators,
                previous_regime=previous_regime,
                transition_cooldown_remaining=transition_cooldown_remaining,
            )

            # Step 6: Update in-memory history. It must outlast a full cooldown
            # so at least two stable entries survive it (§5, R2 finding 9).
            history.append(state)
            history = history[-_history_len(config.TRANSITION_COOLDOWN_CANDLES):]
            self._regime_history[underlying] = history

            # Step 7: Logging (Req 15.1, 15.6)
            self._log_regime(state, previous_regime, now, underlying)

            # Step 8: Persist to Redis (Req 13.1, 13.2)
            await self._persistence.save_regime_state(state)
            await self._persistence.save_regime_history(underlying, history)

            # Step 9: Optionally persist to PostgreSQL (Req 13.5)
            if db_session is not None:
                await self._persistence.persist_to_db(state, db_session)

            return state

        except Exception as exc:
            # The history is NOT appended on this path, so a failure cannot
            # manufacture a phantom flip on the next candle (§5).
            logger.error(
                "[RegimeDetector] Unhandled exception for underlying=%s: %s — "
                "returning DETECTOR_ERROR fallback",
                underlying, exc, exc_info=True,
            )
            return self._fallback_regime(underlying)

    # ── Transition detection ──────────────────────────────────────────────────

    @staticmethod
    def check_regime_transition(
        candidate: str,
        history: list[RegimeState],
        window: int = 2,
    ) -> bool:
        """True iff *candidate* differs from the most recent NON-transition regime.

        ``window`` is retained for config compatibility and is unused: a flip
        is a flip the moment it differs from the last stable regime. Returns
        False when no stable regime exists yet (first candles of a session).
        The detector's own REGIME_TRANSITION entries are ignored, which is what
        stops a single flip from re-arming the cooldown (bug 1).
        """
        stable = [s.regime for s in history if s.regime != "REGIME_TRANSITION"]
        return bool(stable) and candidate != stable[-1]

    # ── Query helpers ─────────────────────────────────────────────────────────

    async def get_current_regime(self, underlying: str) -> Optional[RegimeState]:
        """
        Return the most recent RegimeState for *underlying*.

        First checks in-memory history (fastest path), then falls back to
        Redis via RegimePersistence.  Returns None when neither source has data.

        Args:
            underlying: Instrument name.

        Returns:
            Latest RegimeState, or None.
        """
        # Fast path: in-memory history
        history = self._regime_history.get(underlying)
        if history:
            return history[-1]

        # Slow path: Redis cache
        return await self._persistence.load_regime_state(underlying)

    def get_regime_history(
        self, underlying: str, limit: int = 5
    ) -> list[RegimeState]:
        """
        Return the last ``limit`` regime states for *underlying* from the
        in-memory history.

        Args:
            underlying: Instrument name.
            limit:      Maximum number of entries to return (default 5).

        Returns:
            List of RegimeState objects, newest last.  Empty list if none exist.
        """
        return self._regime_history.get(underlying, [])[-limit:]

    # ── Fallback ──────────────────────────────────────────────────────────────

    def _fallback_regime(self, underlying: str) -> RegimeState:
        """
        Build and return a DETECTOR_ERROR RegimeState with all indicators
        zeroed and no cooldown. DETECTOR_ERROR is a no-trade regime (D7): a
        detector that cannot classify the tape does not trade it.

        Used as the error-path return value from ``detect_regime()``.
        """
        zeroed_indicators = RegimeIndicators(
            adx=None,
            atr=0.0,
            atr_percentile=50.0,
            vwap=0.0,
            price=0.0,
            vwap_cross_count=0,
            volume=0.0,
            avg_volume=0.0,
            orb_high=None,
            orb_low=None,
            pdh=None,
            pdl=None,
        )
        return RegimeState(
            regime="DETECTOR_ERROR",
            underlying=underlying,
            timestamp=now_utc(),
            indicators=zeroed_indicators,
            previous_regime=None,
            transition_cooldown_remaining=0,
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _log_regime(
        self,
        state: RegimeState,
        previous_regime: Optional[str],
        now: datetime,
        underlying: str,
    ) -> None:
        """
        Log regime changes at INFO and periodic heartbeats at DEBUG.

        - Regime change      → INFO  (Req 15.1)
        - Regime unchanged   → DEBUG every 5 minutes (Req 15.6)
        """
        ind = state.indicators

        if state.regime != previous_regime:
            # Regime changed — emit INFO with full indicator snapshot (Req 15.1)
            logger.info(
                "[RegimeDetector] underlying=%s REGIME CHANGE: %s → %s | "
                "adx=%s atr=%.2f atr_pct=%.1f vwap=%.2f price=%.2f "
                "vwap_crosses=%d volume=%.0f avg_volume=%.0f "
                "orb_high=%s orb_low=%s pdh=%s pdl=%s ts=%s",
                underlying,
                previous_regime,
                state.regime,
                f"{ind.adx:.2f}" if ind.adx is not None else "None",
                ind.atr,
                ind.atr_percentile,
                ind.vwap,
                ind.price,
                ind.vwap_cross_count,
                ind.volume,
                ind.avg_volume,
                ind.orb_high,
                ind.orb_low,
                ind.pdh,
                ind.pdl,
                state.timestamp.isoformat(),
            )
            # Reset the heartbeat timer on any change
            self._last_log_time[underlying] = now

        else:
            # Regime unchanged — throttled DEBUG heartbeat (Req 15.6)
            last = self._last_log_time.get(underlying)
            if last is None or (now - last) >= _LOG_INTERVAL:
                logger.debug(
                    "[RegimeDetector] underlying=%s regime=%s (unchanged) | "
                    "adx=%s atr=%.2f atr_pct=%.1f vwap=%.2f price=%.2f "
                    "vwap_crosses=%d ts=%s",
                    underlying,
                    state.regime,
                    f"{ind.adx:.2f}" if ind.adx is not None else "None",
                    ind.atr,
                    ind.atr_percentile,
                    ind.vwap,
                    ind.price,
                    ind.vwap_cross_count,
                    state.timestamp.isoformat(),
                )
                self._last_log_time[underlying] = now


# ── Module-level singleton ─────────────────────────────────────────────────────
regime_detector = RegimeDetector()
