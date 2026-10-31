"""
Strategy Orchestrator.

Translates regime state into strategy enable/disable flags and position
size multipliers. Sits between RegimeDetector and StrategyRunner.

Responsibilities:
  - Convert regime → strategy_flags dict (all 12 strategies, design §6.2)
  - Merge regime flags with user flags (absent = disabled, user disable wins)
    and remember the result, so paper_engine can re-check authority (§6.4)
  - Filter signals by regime-permitted directions (fail closed on no state)
  - Record per-regime performance (one TradeOutcome, on the close's session)
  - Mirror the detector's transition cooldown for display only (§5)

The REGIME_TRANSITION cooldown is owned by RegimeDetector alone. This class
holds no counter: during a transition the REGIME_TRANSITION rule enables no
strategy, which is the suppression. ``is_in_cooldown`` is a read-only,
expiring mirror for the API and gates nothing.

Requirements: 11.2–11.7, 12.2, 16.1–16.7, 19.1, 19.2, 19.5, 19.6, 20.1–20.5
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.trading.trade_outcome import TradeOutcome
    from app.strategies.base import SignalEvent

from app.services.trading.regime_persistence import RegimeState, regime_persistence
from app.services.trading.regime_rules import get_regime_rule
from app.strategies.names import ALL_STRATEGY_NAMES
from app.runtime.context import ctx, now_utc, params
from app.services.trading.no_trade import ReasonCode

DETECTOR_ERROR = "DETECTOR_ERROR"      # the detector's fail-closed regime
# Regimes enforced even in shadow mode (regime_gating False): a detector that
# cannot classify (broken data), and EXPIRY_DAY, which exists only when the
# operator chose trade_on_expiry_day = False - a policy, not a regime opinion.
ENFORCED_REGIMES = frozenset({DETECTOR_ERROR, "EXPIRY_DAY"})

logger = logging.getLogger(__name__)

# Closed set of per-strategy reasons (design §6.5)
REASON_ENABLED = "enabled"
REASON_REGIME_RULE = "regime_rule"
REASON_USER_DISABLED = "user_disabled"
REASON_TRANSITION_COOLDOWN = "transition_cooldown"
REASON_DAILY_STRATEGY_HALT = "daily_strategy_halt"
REASON_REGIME_UNAVAILABLE = "regime_unavailable"
REASON_NOT_COMPUTED = "regime flags not yet computed"


class StrategyOrchestrator:
    """
    Manages strategy enable/disable states and position sizing based on the
    detected market regime.
    """

    def __init__(self) -> None:
        # underlying → (cooldown remaining, as_of_utc): display mirror with a TTL
        self._regime_cooldown: dict[str, tuple[int, datetime]] = {}
        # underlying → last known regime name
        self._current_regime: dict[str, str] = {}
        # underlying → {strategy: (enabled, reason)} from the last merge
        self._flag_state: dict[str, dict[str, tuple[bool, str]]] = {}
        # (run_id, trading_day) → strategies halted for the day by the losing
        # streak (design §9.4). A cache of state derived from closed positions
        # by risk_state and pushed here; never a counter.
        self._daily_halts: dict[tuple, frozenset] = {}

    # ── on_regime_detected ────────────────────────────────────────────────────

    async def on_regime_detected(
        self,
        regime_state: RegimeState,
    ) -> dict[str, bool]:
        """
        Called after every regime classification.

        Derives the 12 strategy flags from REGIME_RULES, refreshes the cooldown
        mirror from the detector's state, logs every strategy state change and
        position-size multiplier change, then returns the flag dict.

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
        # Shadow mode (params().regime_gating False): every strategy is left to
        # the user's flags; only ENFORCED_REGIMES still disable them all.
        gating = params().regime_gating or regime in ENFORCED_REGIMES
        flags: dict[str, bool] = {
            name: (name in rule.enabled_strategies) if gating else True
            for name in ALL_STRATEGY_NAMES
        }

        # ── Cooldown mirror (display only; the detector owns the counter) ─────
        remaining = (int(regime_state.transition_cooldown_remaining or 0)
                     if regime == "REGIME_TRANSITION" else 0)
        self._regime_cooldown[underlying] = (remaining, now_utc())

        # ── Log per-strategy state changes ────────────────────────────────────
        # Req 15.2, 15.3: log every enable/disable with regime/underlying/ts
        prev_regime = self._current_regime.get(underlying)
        if prev_regime != regime:
            for name in ALL_STRATEGY_NAMES:
                if flags[name]:
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
                old_multiplier = get_regime_rule(prev_regime, atr_pct).position_multiplier

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

    # ── flag merge (design §6.4) ──────────────────────────────────────────────

    def merge_flags(
        self, underlying: str, regime_flags: dict[str, bool], user_flags: dict[str, bool],
    ) -> dict[str, bool]:
        """Final flags over the 12 canonical names only.

        A strategy missing from *regime_flags* is disabled (R3.6); a
        user-disabled strategy stays disabled in every regime (R3.7). The user
        default stays True so a fresh strategy_flags JSON disables nothing.
        The result is remembered for ``is_strategy_enabled``.
        """
        in_transition = self._current_regime.get(underlying) == "REGIME_TRANSITION"
        state: dict[str, tuple[bool, str]] = {}
        for name in ALL_STRATEGY_NAMES:
            if not user_flags.get(name, True):
                state[name] = (False, REASON_USER_DISABLED)
            elif not regime_flags.get(name, False):
                state[name] = (False, REASON_TRANSITION_COOLDOWN if in_transition
                               else REASON_REGIME_RULE)
            else:
                state[name] = (True, REASON_ENABLED)
        self._flag_state[underlying] = state
        return {name: on for name, (on, _) in state.items()}

    def mark_regime_unavailable(self, underlying: str) -> dict[str, bool]:
        """Detection failed: every flag False (never the raw user flags)."""
        self._flag_state[underlying] = {
            name: (False, REASON_REGIME_UNAVAILABLE) for name in ALL_STRATEGY_NAMES
        }
        return {name: False for name in ALL_STRATEGY_NAMES}

    def current_regime_name(self, underlying: str) -> Optional[str]:
        """The last regime the orchestrator saw for *underlying*, or None."""
        return self._current_regime.get(underlying)

    def is_strategy_enabled(self, underlying: str, strategy_name: str) -> tuple[bool, str]:
        """(enabled, reason) from the last merged flag set for *underlying*.
        Fails closed when no flags have been computed yet."""
        state = self._flag_state.get(underlying)
        if state is None:
            return False, REASON_NOT_COMPUTED
        enabled, why = state.get(strategy_name, (False, REASON_REGIME_RULE))
        if enabled and strategy_name in self._daily_halts.get(
                (ctx().run_id, ctx().clock.trading_day()), frozenset()):
            return False, REASON_DAILY_STRATEGY_HALT
        return enabled, why

    def set_daily_halts(self, strategies) -> None:
        """Publish today's streak-halted strategies (risk_state.streak_state)."""
        key = (ctx().run_id, ctx().clock.trading_day())
        self._daily_halts = {key: frozenset(strategies)}

    # ── get_position_multiplier ───────────────────────────────────────────────

    def get_position_multiplier(self, underlying: str) -> float:
        """
        Return the position size multiplier for *underlying*'s current regime.

        Returns 0.0 on any exception or when no regime is known: an underlying
        with no regime computed yet resolves to the no-trade DETECTOR_ERROR
        rule, never to a tradeable-looking default.

        Requirements: 12.2
        """
        try:
            regime = self._current_regime.get(underlying, DETECTOR_ERROR)
            if not params().regime_gating and regime not in ENFORCED_REGIMES:
                return 1.0                       # shadow mode: the regime sizes nothing
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
    ) -> tuple[bool, Optional[ReasonCode], str]:
        """
        Check whether *signal* is permitted by the current regime rules.
        Returns (ok, code, reason); the caller records the code (design §12),
        which keeps the orchestrator I/O-free.

        No regime state fails closed (§6.4): a signal the system cannot place
        in a regime is not traded. TREND_HIGH_VOL permits only the direction
        on the price side of VWAP.

        Requirements: 16.1–16.7
        """
        if regime_state is None:
            return False, ReasonCode.REGIME_STATE_UNAVAILABLE, "regime state unavailable"

        underlying = signal.underlying
        regime = regime_state.regime
        if not params().regime_gating and regime not in ENFORCED_REGIMES:
            return True, None, ""                # shadow mode: no direction rule
        atr_pct = regime_state.indicators.atr_percentile
        rule = get_regime_rule(regime, atr_pct)

        reason = ""
        if not rule.allowed_directions:
            reason = f"Regime {regime} allows no trades"
        elif signal.direction.value not in rule.allowed_directions:
            reason = (
                f"Regime {regime} does not permit direction "
                f"{signal.direction.value} (allowed: {sorted(rule.allowed_directions)})"
            )
        elif regime == "TREND_HIGH_VOL":
            ind = regime_state.indicators
            required = "BUY" if ind.price > ind.vwap else "SELL"
            if signal.direction.value != required:
                reason = f"TREND_HIGH_VOL permits {required} only (price vs VWAP)"

        if reason:
            # Req 16.6: log suppression
            logger.info(
                "[StrategyOrchestrator] SIGNAL SUPPRESSED strategy=%s "
                "direction=%s underlying=%s regime=%s reason='%s' ts=%s",
                signal.strategy_name, signal.direction.value, underlying,
                regime, reason, now_utc().isoformat(),
            )
            return False, ReasonCode.REGIME_DIRECTION_MISMATCH, reason

        return True, None, ""

    # ── cooldown mirror (read-only; gates nothing) ────────────────────────────

    def cooldown_remaining(self, underlying: str) -> int:
        """The detector's remaining cooldown as last mirrored, or 0 when the
        mirror is absent or older than cooldown_mirror_ttl_s."""
        entry = self._regime_cooldown.get(underlying)
        if entry is None:
            return 0                       # never detected: the cycle gate owns this case
        remaining, as_of = entry
        if (now_utc() - as_of) > timedelta(seconds=params().cooldown_mirror_ttl_s):
            return 0                       # stale mirror: REGIME_UNAVAILABLE gates the cycle
        return remaining

    def is_in_cooldown(self, underlying: str) -> bool:
        """
        True while the detector's last mirrored state is REGIME_TRANSITION and
        the mirror is fresh. Display only (GET /regime/strategy-map and the
        diagnostics probe): a static test asserts it gates nothing outside
        app/api/.

        Requirements: 20.2, 20.3
        """
        return self.cooldown_remaining(underlying) > 0

    # ── record_trade_result ───────────────────────────────────────────────────

    async def record_trade_result(
        self,
        db_session,
        *,
        regime: str,
        underlying: str,
        strategy_name: str,
        outcome: "TradeOutcome",
    ) -> None:
        """
        Upsert the run's ``regime_performance`` row with one TradeOutcome, on
        the CLOSE's session (design §7). The caller commits.

        A None session is a programming error, never a silent skip (R4.2).
        Write failures propagate: paper_engine rolls the stats back, closes
        the position without them and records PERF_WRITE_FAILED, and
        reconcile_performance backfills.

        Exact sums, not running means, so reconcile can recompute every
        derived field (R15.6). Legacy Numeric columns are rounded to their
        scale, as reconcile stores them.

        Requirements: 19.1, 19.2, 19.5, 19.6
        """
        if db_session is None:
            raise TypeError("record_trade_result requires the close's db session")

        from sqlalchemy import select
        from app.db.models.regime import RegimePerformanceModel
        from app.services.trading.trade_outcome import accumulate

        run_id = ctx().run_id
        row: Optional[RegimePerformanceModel] = (
            await db_session.execute(
                select(RegimePerformanceModel).where(
                    RegimePerformanceModel.regime == regime,
                    RegimePerformanceModel.underlying == underlying,
                    RegimePerformanceModel.strategy_name == strategy_name,
                    RegimePerformanceModel.run_id == run_id,
                )
            )
        ).scalars().first()

        if row is None:
            row = RegimePerformanceModel(
                run_id=run_id, regime=regime, underlying=underlying,
                strategy_name=strategy_name, first_trade_at=outcome.exit_time,
            )
            db_session.add(row)

        accumulate(row, outcome)
        row.total_pnl = round(float(row.total_pnl or 0.0) + outcome.net_pnl, 2)
        row.avg_pnl = round(row.expectancy_inr, 2)
        row.win_rate = round(row.win_rate, 2)
        row.avg_r_multiple = row.expectancy_r
        row.avg_rr = round(row.expectancy_r, 3) if row.expectancy_r is not None else None
        row.last_trade_at = outcome.exit_time

        await db_session.flush()

        logger.debug(
            "[StrategyOrchestrator] Recorded trade result: regime=%s underlying=%s "
            "strategy=%s net=%.2f R=%s total_trades=%d win_rate=%.1f%%",
            regime, underlying, strategy_name, outcome.net_pnl, outcome.r_multiple,
            row.total_trades, float(row.win_rate),
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
