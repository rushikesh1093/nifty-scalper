"""
Strategy base class and signal bus.
Every strategy inherits BaseStrategy and emits SignalEvent objects.
"""
import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Callable, Awaitable
from enum import Enum

from app.runtime.context import now_utc, params
from app.services.trading import no_trade
from app.services.trading.no_trade import ReasonCode

logger = logging.getLogger(__name__)


class Direction(str, Enum):
    # IMPORTANT: In this system we always BUY options — never sell/write them.
    # BUY  = buy a CE (bullish signal — underlying expected to go up)
    # SELL = buy a PE (bearish signal — underlying expected to go down)
    # The name "SELL" refers to the market direction, NOT selling/shorting options.
    BUY  = "BUY"
    SELL = "SELL"


@dataclass
class SignalEvent:
    """Emitted by a strategy when entry conditions are met."""
    strategy_name:   str
    underlying:      str          # NIFTY / BANKNIFTY
    direction:       Direction
    spot_price:      float
    signal_time:     datetime
    vwap:            Optional[float] = None
    indicators:      dict = field(default_factory=dict)
    # Per-strategy risk params (carried from BaseStrategy)
    # Standard 1:2 R:R — entry=100, SL=90 (−10%), target=120 (+20%)
    sl_pct:          float = 0.10   # SL as 10% of option LTP
    target_pct:      float = 0.20   # Target as 20% of option LTP → R:R = 2.0
    min_rr:          float = 2.0    # RECORD of the floor applied (params().min_rr); never a source
    # Suggested entry params (filled by option selector later)
    suggested_strike: Optional[float] = None
    entry_price_ref:  Optional[float] = None
    stop_loss_ref:    Optional[float] = None
    target_ref:       Optional[float] = None
    risk_reward:      Optional[float] = None
    notes:            str = ""
    # Spot-based exit plan (exit_plan.ExitPlan.to_dict()); None = generic ladder
    exit_plan:        Optional[dict] = None


# Type alias for async signal handlers
SignalHandler = Callable[[SignalEvent], Awaitable[None]]


class SignalAggregator:
    """
    Sits between strategies and the paper engine.
    Prevents acting on the same direction for the same underlying twice
    within a short window (avoids multiple strategies triggering the same trade).

    Rules:
      - Same underlying + same direction within 3 minutes → SKIP (already acted)
      - Opposite direction within 1 minute → SKIP (conflicting signals)
    """
    SAME_DIR_COOLDOWN_SECS = 90    # 90s cooldown — allows ~4 trades/underlying/session
    CONFLICT_COOLDOWN_SECS  = 30   # 30s conflict window (was 60 — too long)

    def __init__(self):
        # key: (underlying, direction) → last acted timestamp
        self._last_acted: dict[tuple, datetime] = {}

    def should_act(self, signal: SignalEvent) -> tuple[bool, Optional[ReasonCode], str]:
        """(ok, code, reason). The aggregator is the one component that knows
        which rule fired, so it returns the code; callers never parse reason."""
        now = now_utc()
        key_same = (signal.underlying, signal.direction)
        key_opp  = (
            signal.underlying,
            Direction.SELL if signal.direction == Direction.BUY else Direction.BUY,
        )

        # Check same direction cooldown
        if key_same in self._last_acted:
            last_t = self._last_acted[key_same]
            if last_t.tzinfo is None:
                last_t = last_t.replace(tzinfo=timezone.utc)
            elapsed = (now - last_t).total_seconds()
            if elapsed < self.SAME_DIR_COOLDOWN_SECS:
                return False, ReasonCode.AGGREGATOR_SAME_DIRECTION, (
                    f"Same direction ({signal.direction}) already acted on "
                    f"{signal.underlying} {elapsed:.0f}s ago. "
                    f"Cooldown: {self.SAME_DIR_COOLDOWN_SECS}s"
                )

        # Check conflicting direction cooldown
        if key_opp in self._last_acted:
            last_opp = self._last_acted[key_opp]
            if last_opp.tzinfo is None:
                last_opp = last_opp.replace(tzinfo=timezone.utc)
            elapsed = (now - last_opp).total_seconds()
            if elapsed < self.CONFLICT_COOLDOWN_SECS:
                return False, ReasonCode.AGGREGATOR_CONFLICT, (
                    f"Opposite direction signal on {signal.underlying} "
                    f"fired {elapsed:.0f}s ago — conflicting signals."
                )

        return True, None, ""

    def record_acted(self, signal: SignalEvent):
        key = (signal.underlying, signal.direction)
        self._last_acted[key] = now_utc()

    def clear_acted(self, signal: SignalEvent):
        """Remove an optimistic cooldown entry when no position was opened."""
        key = (signal.underlying, signal.direction)
        self._last_acted.pop(key, None)

    def reset(self):
        self._last_acted.clear()


# Global aggregator instance (shared across all strategies)
signal_aggregator = SignalAggregator()


class SignalBus:
    """
    Pub/sub bus for strategy signals.
    Routes: Strategy → SignalAggregator → PaperEngine
    """
    _handlers: list[SignalHandler] = []

    @classmethod
    def subscribe(cls, handler: SignalHandler):
        cls._handlers.append(handler)

    @classmethod
    def reset_handlers(cls):
        """Drop every subscriber (used by wiring.wire_signal_handlers)."""
        cls._handlers.clear()

    @classmethod
    async def publish(cls, signal: SignalEvent) -> bool:
        """
        Route signal through aggregator → handlers.
        Returns True if a handler opened a position (used to consume cooldown).
        record_acted is only called on a confirmed position open so a rejected
        signal (no option, capital exhausted, etc.) does not waste the 90s
        same-direction cooldown for subsequent valid signals.
        """
        no_trade.note_signal_emitted()      # cycle_stats.signals_emitted

        # ── Signal aggregator gate (a recorded decision, design §12) ──────────
        ok, code, reason = signal_aggregator.should_act(signal)
        if not ok:
            logger.debug(
                "[SignalBus] AGGREGATOR blocked %s %s %s: %s",
                signal.strategy_name, signal.underlying, signal.direction, reason
            )
            # own short-lived session, commits, never raises
            await no_trade.record_signal_gate(
                code=code, reason=reason,
                underlying=signal.underlying, strategy_name=signal.strategy_name,
            )
            return False

        # Record the cooldown BEFORE calling handlers (optimistic lock).
        # This prevents two strategies firing the same underlying+direction
        # in the same candle cycle from both reaching PaperEngine concurrently,
        # causing the second to fail with "capital reserved by another trade".
        # If the position ultimately fails to open, we clear the record so the
        # next genuine signal for this direction is not suppressed.
        signal_aggregator.record_acted(signal)

        position_opened = False
        for handler in cls._handlers:
            try:
                result = await handler(signal)
                if result is True:
                    position_opened = True
            except Exception as e:
                logger.error("SignalBus handler error: %s", e, exc_info=True)

        # If nothing opened (no option, capital exhausted, ATR fail, etc.)
        # clear the optimistic cooldown so the next valid signal for this
        # underlying+direction is not suppressed for 90 seconds.
        if not position_opened:
            signal_aggregator.clear_acted(signal)

        return position_opened


class BaseStrategy(ABC):
    """
    Abstract base for the strategies (app.strategies.names).
    Subclasses implement `evaluate()` which is called on every new candle,
    and may implement `candle_exit()` for exits that need a closed bar.
    """
    name:        str = "BASE"
    underlying:  str = "NIFTY"
    enabled:     bool = True

    # Premium hard stop (fraction of the option fill). The spot exit plan runs
    # alongside it — whichever hits first.
    sl_pct:      float = 0.10
    target_pct:  float = 0.20

    # Today-only 1-min candles this strategy needs before it is evaluated;
    # None = the runner's default (strategy_runner.MIN_TODAY_CANDLES).
    MIN_TODAY_CANDLES: Optional[int] = None

    def __init__(self, underlying: str = "NIFTY"):
        self.underlying = underlying
        self._last_signal_time: Optional[datetime] = None
        # Multi-day 1-min candles (oldest first) for indicator warm-up; the
        # runner sets it every cycle. Empty → strategies use today's candles.
        self.history: list[dict] = []
        self.open_positions: int = 0
        # Respect per-strategy cooldown set as a class attribute.  Only fall
        # back to the default of 5 minutes when the subclass hasn't declared one.
        if not hasattr(self, '_cooldown_minutes'):
            self._cooldown_minutes: int = 5   # default: min gap between signals

    async def on_candle(self, candles: list[dict], indicators: dict):
        """Called by strategy runner on every new closed candle (after it has
        set ``self.history``)."""
        if not self.enabled:
            return
        if not self._cooldown_ok():
            return
        signal = await self.evaluate(candles, indicators)
        if signal:
            self._last_signal_time = signal.signal_time
            logger.info("[%s] Signal: %s %s @ %.2f",
                        self.name, signal.direction, self.underlying, signal.spot_price)
            if await SignalBus.publish(signal):
                self.open_positions += 1
                self.on_position_opened(signal)

    @abstractmethod
    async def evaluate(self, candles: list[dict], indicators: dict) -> Optional[SignalEvent]:
        """
        Evaluate current market state and return a SignalEvent or None.
        `candles` — today's 1-min OHLCV dicts, newest last.
        `indicators` — output of compute_all(candles).
        """

    def candle_exit(self, plan, candles: list[dict], indicators: dict) -> Optional[str]:
        """Exit reason for an open position of this strategy on a candle
        close, or None to hold. *plan* is its exit_plan.ExitPlan, and may be
        tightened in place (the caller persists it). Default: no bar exits."""
        return None

    # ── position lifecycle hooks (paper_engine → strategy) ───────────────────

    def on_position_opened(self, signal: SignalEvent) -> None:
        """A signal of this strategy opened a position."""

    def position_closed(self, direction: Direction, reason: str) -> None:
        """Called by paper_engine after a final close of this strategy's position."""
        self.open_positions = max(0, self.open_positions - 1)
        self.on_position_closed(direction, reason)

    def on_position_closed(self, direction: Direction, reason: str) -> None:
        """Subclass hook for per-day trade rules (cooldowns, reverse trades)."""

    def _cooldown_ok(self) -> bool:
        if not self._last_signal_time:
            return True
        last_time = self._last_signal_time
        if last_time.tzinfo is None:
            last_time = last_time.replace(tzinfo=timezone.utc)
        elapsed = (now_utc() - last_time).total_seconds() / 60
        return elapsed >= self._cooldown_minutes

    def _make_signal(self, direction: Direction, spot: float,
                     indicators: dict, notes: str = "",
                     exit_plan: Optional[dict] = None) -> SignalEvent:
        return SignalEvent(
            strategy_name=self.name,
            underlying=self.underlying,
            direction=direction,
            spot_price=spot,
            signal_time=now_utc(),
            vwap=indicators.get("vwap"),
            indicators=indicators,
            sl_pct=self.sl_pct,
            target_pct=self.target_pct,
            min_rr=params().min_rr,   # a record of the floor applied; params() is the only source
            notes=notes,
            exit_plan=exit_plan,
        )
