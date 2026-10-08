"""
Strategy Orchestrator.

Translates regime state into strategy enable/disable flags and position
size multipliers. Sits between RegimeDetector and StrategyRunner.

Responsibilities:
  - Convert regime → strategy_flags dict (all 12 strategies)
  - Enforce REGIME_TRANSITION cooldown
  - Filter signals by regime-permitted directions
  - Record per-regime P&L performance in the DB
  - Expose current regime via RegimePersistence

Requirements: 11.2–11.7, 12.2, 16.1–16.7, 19.1, 19.2, 19.5, 19.6, 20.1–20.5
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from app.strategies.base import SignalEvent

from app.services.trading.regime_persistence import RegimeState, regime_persistence
from app.services.trading.regime_rules import get_regime_rule

logger = logging.getLogger(__name__)

# ── All strategy names known to the system ────────────────────────────────────
_ALL_STRATEGIES: tuple[str, ...] = (
    "ORB_VWAP",
    "VWAP_PULLBACK",
    "EMA_VWAP",
    "SUPERTREND_VWAP",
    "PDH_PDL",
    "VWAP_REVERSAL",
    "BOLLINGER_BREAKOUT",
    "RSI_VWAP",
    "OI_SPIKE",
    "IV_CRUSH",
    "PCR_SIGNAL",
    "GAP_FILL",
)

# NOTE: the REGIME_TRANSITION cooldown length is owned by RegimeDetector
# (RegimeConfig.TRANSITION_COOLDOWN_CANDLES). The orchestrator only mirrors
# the detector's state; it does not run a second, independent counter.


class StrategyOrchestrator:
    """
    Manages strategy enable/disable states and position sizing based on the
    detected market regime.
    """

    def __init__(self) -> None:
        # underlying → remaining cooldown candle count
        self._transition_cooldown: dict[str, int] = {}
        # underlying → last known regime name
        self._current_regime: dict[str, str] = {}

    # ── on_regime_detected ────────────────────────────────────────────────────

    async def on_regime_detected(
        self,
        regime_state: RegimeState,
    ) -> dict[str, bool]:
        """
        Called after every regime classification.

        Derives enabled-strategy flags from REGIME_RULES, enforces transition
        cooldown, logs every strategy state change and position-size multiplier
        change, then returns the flag dict.

        Requirements: 11.2, 11.3, 11.4, 11.6, 11.7, 15.2, 15.3, 15.4,
                      20.1, 20.2, 20.3, 20.4, 20.5
        """
        underlying = regime_state.underlying
        regime = regime_state.regime
        atr_pct = regime_state.indicators.atr_percentile
        now_ts = regime_state.timestamp

        # ── Get regime rule ───────────────────────────────────────────────────
        rule = get_regime_rule(regime, atr_pct)

        # ── Build base flags from rule ────────────────────────────────────────
        flags: dict[str, bool] = {
            name: (name in rule.enabled_strategies)
            for name in _ALL_STRATEGIES
        }

        # ── Transition cooldown bookkeeping ───────────────────────────────────
        # The detector owns the cooldown (TRANSITION_COOLDOWN_CANDLES). The
        # orchestrator mirrors it: in cooldown iff the current state is
        # REGIME_TRANSITION. Re-arming a second counter on every TRANSITION
        # candle (old behaviour) suppressed the first normal candles after
        # each flip and, together with the detector bug, locked trading out.
        if regime == "REGIME_TRANSITION":
            remaining = max(int(regime_state.transition_cooldown_remaining or 0), 1)
            self._transition_cooldown[underlying] = remaining
            logger.debug(
                "[StrategyOrchestrator] underlying=%s REGIME_TRANSITION — "
                "%d cooldown candle(s) remaining (detector-owned)",
                underlying, remaining,
            )
        elif self._transition_cooldown.pop(underlying, None) is not None:
            logger.info(
                "[StrategyOrchestrator] underlying=%s cooldown expired — "
                "regime=%s strategies evaluated from this candle",
                underlying, regime,
            )

        # ── Log per-strategy state changes ────────────────────────────────────
        # Req 15.2, 15.3: log every enable/disable with regime/underlying/ts
        prev_regime = self._current_regime.get(underlying)
        if prev_regime != regime:
            # Regime changed — log every strategy that changed state
            # We don't know previous flags by name, but any regime change
            # potentially flips strategies, so log everything that is enabled
            # or disabled under the new regime.
            for name in _ALL_STRATEGIES:
                new_state = flags[name]
                if new_state:
                    logger.info(
                        "[StrategyOrchestrator] ENABLED  strategy=%s "
                        "regime=%s underlying=%s ts=%s",
                        name, regime, underlying, now_ts.isoformat(),
                    )
                else:
                    logger.info(
                        "[StrategyOrchestrator] DISABLED strategy=%s "
                        "regime=%s underlying=%s reason=regime_rule ts=%s",
                        name, regime, underlying, now_ts.isoformat(),
                    )

            # ── Log position-size multiplier change (Req 15.4) ───────────────
            old_multiplier: Optional[float] = None
            if prev_regime is not None:
                try:
                    old_rule = get_regime_rule(prev_regime, atr_pct)
                    old_multiplier = old_rule.position_multiplier
                except Exception:
                    pass

            new_multiplier = rule.position_multiplier
            if old_multiplier is None or old_multiplier != new_multiplier:
                logger.info(
                    "[StrategyOrchestrator] Position_Size_Multiplier changed "
                    "from %s → %.1f  regime=%s underlying=%s ts=%s",
                    f"{old_multiplier:.1f}" if old_multiplier is not None else "N/A",
                    new_multiplier, regime, underlying, now_ts.isoformat(),
                )

        # ── Update current regime ─────────────────────────────────────────────
        self._current_regime[underlying] = regime

        return flags

    # ── get_position_multiplier ───────────────────────────────────────────────

    def get_position_multiplier(self, underlying: str) -> float:
        """
        Return the position size multiplier for *underlying*'s current regime.

        Returns 0.0 on any exception or when no regime is known.

        Requirements: 12.2
        """
        try:
            regime = self._current_regime.get(underlying, "UNKNOWN")
            rule = get_regime_rule(regime)
            return rule.position_multiplier
        except Exception as exc:
            logger.warning(
                "[StrategyOrchestrator] get_position_multiplier failed for "
                "underlying=%s: %s — returning 0.0",
                underlying, exc,
            )
            return 0.0

    # ── should_allow_signal ───────────────────────────────────────────────────

    def should_allow_signal(
        self,
        signal: "SignalEvent",
        regime_state: Optional[RegimeState],
    ) -> tuple[bool, str]:
        """
        Check whether *signal* is permitted by the current regime rules.

        If *regime_state* is None the signal passes through (no regime data →
        don't block).

        Requirements: 16.1–16.7
        """
        if regime_state is None:
            return True, ""

        underlying = signal.underlying
        regime = regime_state.regime
        atr_pct = regime_state.indicators.atr_percentile
        rule = get_regime_rule(regime, atr_pct)

        # No directions allowed — regime blocks all trades
        if not rule.allowed_directions:
            reason = f"Regime {regime} allows no trades"
            logger.info(
                "[StrategyOrchestrator] SIGNAL SUPPRESSED strategy=%s "
                "direction=%s underlying=%s regime=%s reason='%s' ts=%s",
                signal.strategy_name, signal.direction.value, underlying,
                regime, reason,
                datetime.now(timezone.utc).isoformat(),
            )
            return False, reason

        # Direction not permitted
        if signal.direction.value not in rule.allowed_directions:
            reason = (
                f"Regime {regime} does not permit direction "
                f"{signal.direction.value} (allowed: {rule.allowed_directions})"
            )
            # Req 16.6: log suppression
            logger.info(
                "[StrategyOrchestrator] SIGNAL SUPPRESSED strategy=%s "
                "direction=%s underlying=%s regime=%s reason='%s' ts=%s",
                signal.strategy_name, signal.direction.value, underlying,
                regime, reason,
                datetime.now(timezone.utc).isoformat(),
            )
            return False, reason

        # Transition cooldown blocks everything
        if self.is_in_cooldown(underlying):
            reason = f"Transition cooldown active for {underlying}"
            return False, reason

        return True, ""

    # ── is_in_cooldown ────────────────────────────────────────────────────────

    def is_in_cooldown(self, underlying: str) -> bool:
        """
        Return True while the post-transition cooldown counter is > 0.

        Requirements: 20.2, 20.3
        """
        return self._transition_cooldown.get(underlying, 0) > 0

    # ── record_trade_result ───────────────────────────────────────────────────

    async def record_trade_result(
        self,
        regime: str,
        underlying: str,
        strategy_name: str,
        pnl: float,
        rr: float,
        db_session=None,
    ) -> None:
        """
        Upsert a ``regime_performance`` row with trade outcome data.

        Requirements: 19.1, 19.2, 19.5, 19.6
        """
        if db_session is None:
            logger.warning(
                "[StrategyOrchestrator] record_trade_result called without "
                "db_session — skipping performance update for "
                "regime=%s underlying=%s strategy=%s pnl=%.2f",
                regime, underlying, strategy_name, pnl,
            )
            return

        try:
            from sqlalchemy import select
            from app.db.models.regime import RegimePerformanceModel

            # Load or create the performance row
            row: Optional[RegimePerformanceModel] = (
                await db_session.execute(
                    select(RegimePerformanceModel).where(
                        RegimePerformanceModel.regime == regime,
                        RegimePerformanceModel.underlying == underlying,
                        RegimePerformanceModel.strategy_name == strategy_name,
                    )
                )
            ).scalars().first()

            if row is None:
                row = RegimePerformanceModel(
                    regime=regime,
                    underlying=underlying,
                    strategy_name=strategy_name,
                    total_trades=0,
                    winning_trades=0,
                    losing_trades=0,
                    total_pnl=0.0,
                    first_trade_at=datetime.now(timezone.utc),
                )
                db_session.add(row)

            # Update counters
            row.total_trades = (row.total_trades or 0) + 1
            if pnl > 0:
                row.winning_trades = (row.winning_trades or 0) + 1
            else:
                row.losing_trades = (row.losing_trades or 0) + 1

            row.total_pnl = float(row.total_pnl or 0.0) + pnl
            row.avg_pnl = row.total_pnl / row.total_trades
            row.win_rate = (row.winning_trades / row.total_trades) * 100.0

            # Rolling average R:R
            if row.avg_rr is None:
                row.avg_rr = rr
            else:
                # Simple running mean: avg = old_avg + (new - old_avg) / n
                n = row.total_trades
                row.avg_rr = float(row.avg_rr) + (rr - float(row.avg_rr)) / n

            row.last_trade_at = datetime.now(timezone.utc)

            await db_session.flush()

            logger.debug(
                "[StrategyOrchestrator] Recorded trade result: "
                "regime=%s underlying=%s strategy=%s pnl=%.2f "
                "total_trades=%d win_rate=%.1f%%",
                regime, underlying, strategy_name, pnl,
                row.total_trades, row.win_rate,
            )

        except Exception as exc:
            # Req 19.6: catch and log, never raise
            logger.warning(
                "[StrategyOrchestrator] Failed to record trade result for "
                "regime=%s underlying=%s strategy=%s: %s",
                regime, underlying, strategy_name, exc,
            )

    # ── get_current_regime ────────────────────────────────────────────────────

    async def get_current_regime(self, underlying: str) -> Optional[RegimeState]:
        """
        Return the current RegimeState for *underlying* from Redis cache.

        Thin wrapper over ``RegimePersistence.load_regime_state``.

        Requirements: 20.1
        """
        return await regime_persistence.load_regime_state(underlying)


# ── Module-level singleton ────────────────────────────────────────────────────
strategy_orchestrator = StrategyOrchestrator()
