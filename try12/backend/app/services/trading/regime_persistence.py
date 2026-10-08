"""
Regime persistence layer.

Handles both Redis caching and PostgreSQL persistence for regime state.

Redis key schema:
  regime:{underlying}:current   — latest RegimeState JSON, TTL=600s
  regime:{underlying}:history   — list of last 5 RegimeState JSON, TTL=28800s

PostgreSQL:
  regime_states — one row per classification event, written by persist_to_db()
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, TYPE_CHECKING

from app.runtime.context import market, now_utc
from app.services.trading.regime_indicators import RegimeIndicators

logger = logging.getLogger(__name__)

# ── TTLs ─────────────────────────────────────────────────────────────────────
TTL_CURRENT = 600       # 10 minutes
TTL_HISTORY = 28800     # 8 hours
MAX_HISTORY = 5         # keep only the last 5 states in history


# ── RegimeState dataclass ─────────────────────────────────────────────────────

@dataclass
class RegimeState:
    """
    Immutable snapshot of one regime classification result.

    Defined here so both RegimePersistence and (later) RegimeDetector can
    import it without circular dependencies.
    """
    regime: str
    underlying: str
    timestamp: datetime
    indicators: RegimeIndicators
    previous_regime: Optional[str] = None
    transition_cooldown_remaining: int = 0


# ── Serialisation helpers ─────────────────────────────────────────────────────

def _state_to_dict(state: RegimeState) -> dict:
    """Convert RegimeState → JSON-serialisable dict."""
    d = asdict(state)
    # datetime → ISO-8601 string
    ts: datetime = state.timestamp
    d["timestamp"] = ts.isoformat()
    return d


def _dict_to_state(raw: dict) -> RegimeState:
    """Convert dict (from JSON) → RegimeState.  Raises on any key error."""
    indicators_raw: dict = raw["indicators"]
    indicators = RegimeIndicators(
        adx=indicators_raw.get("adx"),
        atr=float(indicators_raw["atr"]),
        atr_percentile=float(indicators_raw["atr_percentile"]),
        vwap=float(indicators_raw["vwap"]),
        price=float(indicators_raw["price"]),
        vwap_cross_count=int(indicators_raw["vwap_cross_count"]),
        volume=float(indicators_raw["volume"]),
        avg_volume=float(indicators_raw["avg_volume"]),
        orb_high=indicators_raw.get("orb_high"),
        orb_low=indicators_raw.get("orb_low"),
        pdh=indicators_raw.get("pdh"),
        pdl=indicators_raw.get("pdl"),
    )
    ts = datetime.fromisoformat(raw["timestamp"])
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    return RegimeState(
        regime=raw["regime"],
        underlying=raw["underlying"],
        timestamp=ts,
        indicators=indicators,
        previous_regime=raw.get("previous_regime"),
        transition_cooldown_remaining=int(raw.get("transition_cooldown_remaining", 0)),
    )


# ── RegimePersistence class ───────────────────────────────────────────────────

class RegimePersistence:
    """
    Persistence layer for regime state.

    All public methods are async.  Redis errors are caught and logged at
    WARNING level — they never propagate to callers.  DB errors are also
    caught and logged at WARNING.

    Cache contract (design §14.1): this class passes Python objects to
    ``market().set(key, obj, ttl=...)`` and receives Python objects from
    ``market().get(key)``. The port owns all (de)serialisation, so nothing
    here calls json.dumps / json.loads on cache values.
    """

    # ── Redis: current state ──────────────────────────────────────────────────

    async def save_regime_state(self, state: RegimeState) -> None:
        """
        Serialize *state* and write to Redis key ``regime:{underlying}:current``
        with TTL=600 s.

        Requirements: 13.1, 13.2
        """
        key = f"regime:{state.underlying}:current"
        try:
            await market().set(key, _state_to_dict(state), ttl=TTL_CURRENT)
            logger.debug(
                "[RegimePersistence] Saved regime state for %s: %s",
                state.underlying, state.regime,
            )
        except Exception as exc:
            logger.warning(
                "[RegimePersistence] Failed to save regime state for %s: %s",
                state.underlying, exc,
            )

    async def load_regime_state(self, underlying: str) -> Optional[RegimeState]:
        """
        Load the current regime state for *underlying* from Redis.

        Returns None if:
        - Key does not exist
        - JSON is malformed
        - Stored timestamp is older than 10 minutes
        - Redis is unavailable

        Requirements: 13.1, 13.2
        """
        key = f"regime:{underlying}:current"
        try:
            raw = await market().get(key)
            if raw is None:
                return None

            state = _dict_to_state(raw)

            # Discard stale entries (> 10 minutes old)
            now = now_utc()
            ts = state.timestamp
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if (now - ts) > timedelta(minutes=10):
                logger.debug(
                    "[RegimePersistence] Discarding stale regime state for %s "
                    "(age=%s)",
                    underlying, now - ts,
                )
                return None

            return state

        except Exception as exc:
            logger.warning(
                "[RegimePersistence] Failed to load regime state for %s: %s",
                underlying, exc,
            )
            return None

    # ── Redis: history ────────────────────────────────────────────────────────

    async def save_regime_history(
        self,
        underlying: str,
        history: list[RegimeState],
    ) -> None:
        """
        Serialize the last ``MAX_HISTORY`` states and write to
        ``regime:{underlying}:history`` with TTL=28800 s.

        Requirements: 13.3, 13.4
        """
        key = f"regime:{underlying}:history"
        try:
            # Keep only the most recent MAX_HISTORY entries
            trimmed = history[-MAX_HISTORY:] if len(history) > MAX_HISTORY else history
            await market().set(key, [_state_to_dict(s) for s in trimmed], ttl=TTL_HISTORY)
            logger.debug(
                "[RegimePersistence] Saved %d history entries for %s",
                len(trimmed), underlying,
            )
        except Exception as exc:
            logger.warning(
                "[RegimePersistence] Failed to save regime history for %s: %s",
                underlying, exc,
            )

    async def load_regime_history(self, underlying: str) -> list[RegimeState]:
        """
        Load regime history for *underlying* from Redis.

        Returns an empty list on any failure.

        Requirements: 13.3, 13.4
        """
        key = f"regime:{underlying}:history"
        try:
            raw_list = await market().get(key)
            if raw_list is None:
                return []

            return [_dict_to_state(item) for item in raw_list]

        except Exception as exc:
            logger.warning(
                "[RegimePersistence] Failed to load regime history for %s: %s",
                underlying, exc,
            )
            return []

    # ── PostgreSQL ────────────────────────────────────────────────────────────

    async def persist_to_db(self, state: RegimeState, db_session) -> None:
        """
        Insert a new row into ``regime_states`` for *state*.

        Uses the async SQLAlchemy session passed in — the caller is responsible
        for providing an open session and committing.  This method adds the
        object and flushes (so the PK is populated) but does NOT commit, to
        allow the caller to batch multiple writes in one transaction.

        All exceptions are caught and logged at WARNING; the method never
        raises.

        Requirements: 13.5, 13.6
        """
        try:
            from app.db.models.regime import RegimeStateModel

            ind = state.indicators
            row = RegimeStateModel(
                underlying=state.underlying,
                regime=state.regime,
                timestamp=state.timestamp,
                adx=float(ind.adx) if ind.adx is not None else None,
                atr=float(ind.atr),
                atr_percentile=float(ind.atr_percentile),
                vwap=float(ind.vwap),
                price=float(ind.price),
                vwap_cross_count=int(ind.vwap_cross_count),
                volume=int(ind.volume) if ind.volume is not None else None,
                avg_volume=float(ind.avg_volume) if ind.avg_volume is not None else None,
                previous_regime=state.previous_regime,
                transition_detected=state.previous_regime is not None
                    and state.previous_regime != state.regime,
            )
            db_session.add(row)
            await db_session.flush()
            logger.debug(
                "[RegimePersistence] Persisted regime row id=%s for %s: %s",
                row.id, state.underlying, state.regime,
            )
        except Exception as exc:
            logger.warning(
                "[RegimePersistence] DB persist failed for %s (%s): %s",
                state.underlying, state.regime, exc,
            )


# App-level singleton
regime_persistence = RegimePersistence()
