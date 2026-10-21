"""
Persistent research telemetry for the unified trend engine (table trend_decisions).

Why: the engine's in-memory DecisionLog is bounded and lost on restart, so it
cannot answer the research question "does the engine's direction call predict the
underlying's next 15 / 30 / 60 minutes?". This module records, for every completed
1-min candle the strategy runner processes:

  * the engine's decision on that candle — BUY CE / BUY PE / NO TRADE with its
    reason code, trend, strength and 0-6 evidence score. When the live path ran
    the engine, that exact decision is used; when a runner gate (ATR, VIX, regime
    map, cooldown, LEGACY mode, ...) stopped the live path first, the engine's pure
    ``decide()`` is computed on the same candles so the research sample is not
    filtered by the gates. ``gate`` records which gate stopped the live path;
  * the regime and ATR percentile from the regime detector;
  * later, as candles complete, the underlying INDEX price at +15 / +30 / +60
    minutes, the next candle's open and the next two candles' high/low (for the
    entry-timing study) and the 60-minute high/low.

It is independent of order execution: nothing here reads or changes positions,
signals or risk state, every write runs in its own session, and every failure is
a rate-limited WARNING that never stops the cycle.
"""
import logging
import time as _time
from datetime import datetime, timedelta, time as dtime
from typing import Optional

from sqlalchemy import select

from app.db.models.research import TrendDecisionRecord
from app.runtime.context import ctx, now_utc, sessions
from app.services.market_data.timestamps import try_normalize_candle_timestamp
from app.strategies.trend_engine import (
    BUY_CE, BUY_PE, DOWNTREND, NO_TRADE, UPTREND, TrendDecision, TrendFollowingEngine,
    decision_log,
)

logger = logging.getLogger(__name__)

FWD_HORIZONS = (15, 30, 60)          # minutes after the decided candle's close
ASOF_TOLERANCE_MIN = 2               # a missing minute may use a candle ≤ 2 min earlier
SESSION_LAST_CANDLE = dtime(15, 29)  # the last 1-min candle of the NSE session (IST)
WARN_INTERVAL_SECS = 600


def _minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def session_candles(raw: list[dict], day, now_ist: datetime) -> list[tuple[datetime, dict]]:
    """[(IST minute, candle)] for *day*'s completed session candles, sorted and
    de-duplicated per minute (last row wins). Malformed rows are skipped."""
    by_minute: dict[datetime, dict] = {}
    current = _minute(now_ist)
    for c in raw or []:
        dt = try_normalize_candle_timestamp(c.get("timestamp"))
        if dt is None or dt.date() != day or dt.time().replace(tzinfo=None) < dtime(9, 15):
            continue
        if _minute(dt) >= current:
            continue                                   # still forming
        try:
            float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"])
        except (KeyError, TypeError, ValueError):
            continue
        by_minute[_minute(dt)] = c
    return sorted(by_minute.items())


def forward_path(t0: datetime, series: list[tuple[datetime, dict]]) -> dict:
    """Forward fields for a decision on the candle starting at *t0* (IST minute).

    ``px_h`` = close of the candle starting at t0 + h minutes, i.e. the index level
    h minutes after the decided candle closed (as-of ≤ ASOF_TOLERANCE_MIN earlier
    when that minute is missing). The 60-minute high/low cover the candles
    t0+1 … t0+60 and are set only once that window is complete or the session has
    ended (``complete``). Values are INDEX points."""
    by_minute = dict(series)
    last = series[-1][0] if series else None
    session_over = last is not None and last.time().replace(tzinfo=None) >= SESSION_LAST_CANDLE
    out: dict = {}

    def asof(m: datetime) -> Optional[dict]:
        for back in range(ASOF_TOLERANCE_MIN + 1):
            c = by_minute.get(m - timedelta(minutes=back))
            if c is not None and m - timedelta(minutes=back) > t0:
                return c
        return None

    nxt = by_minute.get(t0 + timedelta(minutes=1))
    if nxt is not None:
        out["next_open"] = float(nxt["open"])
    two = [by_minute.get(t0 + timedelta(minutes=k)) for k in (1, 2)]
    if all(c is not None for c in two):
        out["next2_high"] = max(float(c["high"]) for c in two)
        out["next2_low"] = min(float(c["low"]) for c in two)
    for h in FWD_HORIZONS:
        m = t0 + timedelta(minutes=h)
        if last is not None and last >= m:
            c = asof(m)
            if c is not None:
                out[f"px_{h}"] = float(c["close"])
    window = [c for m, c in series if t0 < m <= t0 + timedelta(minutes=60)]
    horizon_reached = last is not None and last >= t0 + timedelta(minutes=60)
    if window and (horizon_reached or session_over):
        out["high_60"] = max(float(c["high"]) for c in window)
        out["low_60"] = min(float(c["low"]) for c in window)
    out["complete"] = bool(horizon_reached or session_over)
    return out


class DecisionTelemetry:
    def __init__(self) -> None:
        self._table_ready = False
        self._warned_at: dict[str, float] = {}

    def reset(self) -> None:
        self._table_ready = False
        self._warned_at.clear()

    def _warn(self, key: str, msg: str, *args) -> None:
        now = _time.monotonic()
        last = self._warned_at.get(key)
        if last is None or now - last >= WARN_INTERVAL_SECS:
            self._warned_at[key] = now
            logger.warning(msg, *args)

    async def _ensure_table(self, db) -> None:
        """Create trend_decisions on first use if migration 0014 has not run."""
        if self._table_ready:
            return
        conn = await db.connection()
        await conn.run_sync(lambda sync_conn: TrendDecisionRecord.__table__.create(
            sync_conn, checkfirst=True))
        await db.commit()
        self._table_ready = True

    # ── the decision row ─────────────────────────────────────────────────────

    @staticmethod
    def engine_decision(engine: TrendFollowingEngine, candles: list[dict],
                        indicators: dict) -> tuple[TrendDecision, bool]:
        """(decision for the last candle, whether the live path produced it)."""
        ts = str(candles[-1].get("timestamp"))
        live = decision_log.latest(engine.underlying)
        if live is not None and live.candle_time == ts:
            return live, True
        return engine.decide(candles, indicators), False

    async def record(self, *, underlying: str, engine: TrendFollowingEngine,
                     candles: list[dict], indicators: dict, gate: Optional[str],
                     regime_state=None) -> Optional[int]:
        """Insert the row for the last candle of *candles* (idempotent per run,
        underlying and candle). Returns the row id, or None. Never raises."""
        try:
            if not candles:
                return None
            t0 = try_normalize_candle_timestamp(candles[-1].get("timestamp"))
            if t0 is None:
                return None
            t0 = _minute(t0)
            d, evaluated = self.engine_decision(engine, candles, indicators)
            if not evaluated and gate is None:
                gate = "NOT_EVALUATED"      # e.g. engine disabled or the cycle raised
            rc = ctx()
            ind = getattr(regime_state, "indicators", None)
            row = TrendDecisionRecord(
                run_id=rc.run_id, trading_day=t0.date(), underlying=underlying,
                candle_ts=t0, evaluated_at=now_utc(),
                decision=d.decision, reason_code=d.reason_code,
                reason=(d.reason or "")[:400] or None, trend=d.trend,
                direction=1 if d.trend == UPTREND else -1 if d.trend == DOWNTREND else 0,
                strength=d.strength, score=d.score,
                entry_trigger=(d.entry_trigger or None) and d.entry_trigger[:80],
                gate=gate, engine_evaluated=evaluated,
                signal_emitted=bool(evaluated and gate is None
                                    and d.decision in (BUY_CE, BUY_PE)),
                regime=getattr(regime_state, "regime", None),
                atr_percentile=(float(ind.atr_percentile)
                                if ind is not None and ind.atr_percentile is not None else None),
                spot=float(candles[-1]["close"]),   # reference for forward returns
                vwap=d.vwap, atr=d.atr,
                stop_points=d.stop_points, target_points=d.target1_points,
                features=d.features or None,
                fwd_complete=False,
            )
            async with sessions()() as db:
                await self._ensure_table(db)
                exists = (await db.execute(select(TrendDecisionRecord.id).where(
                    TrendDecisionRecord.run_id == rc.run_id,
                    TrendDecisionRecord.underlying == underlying,
                    TrendDecisionRecord.candle_ts == t0,
                ))).scalar_one_or_none()
                if exists is not None:
                    return exists
                db.add(row)
                await db.commit()
                await db.refresh(row)
                return row.id
        except Exception as exc:
            self._warn(f"record:{underlying}", "[%s] trend-decision telemetry not recorded "
                       "(non-fatal): %s", underlying, exc)
            return None

    # ── forward prices ───────────────────────────────────────────────────────

    async def fill_forward(self, underlying: str, raw_candles: list[dict],
                           now_ist: datetime) -> int:
        """Fill forward fields of today's incomplete rows from the session candles.
        Returns the number of rows updated. Never raises."""
        try:
            day = now_ist.date()
            series = session_candles(raw_candles, day, now_ist)
            if not series:
                return 0
            rc = ctx()
            updated = 0
            async with sessions()() as db:
                await self._ensure_table(db)
                rows = (await db.execute(select(TrendDecisionRecord).where(
                    TrendDecisionRecord.run_id == rc.run_id,
                    TrendDecisionRecord.underlying == underlying,
                    TrendDecisionRecord.trading_day == day,
                    TrendDecisionRecord.fwd_complete == False,  # noqa: E712
                ))).scalars().all()
                for r in rows:
                    t0 = r.candle_ts
                    if t0.tzinfo is None:                    # SQLite drops the offset
                        t0 = t0.replace(tzinfo=series[0][0].tzinfo)
                    t0 = _minute(t0.astimezone(series[0][0].tzinfo))
                    fwd = forward_path(t0, series)
                    changed = False
                    for k in ("next_open", "next2_high", "next2_low", "px_15", "px_30",
                              "px_60", "high_60", "low_60"):
                        if k in fwd and getattr(r, k) != fwd[k]:
                            setattr(r, k, fwd[k])
                            changed = True
                    if fwd["complete"]:
                        r.fwd_complete = True
                        changed = True
                    if changed:
                        r.fwd_updated_at = now_utc()
                        updated += 1
                await db.commit()
            return updated
        except Exception as exc:
            self._warn(f"fill:{underlying}", "[%s] trend-decision forward fill failed "
                       "(non-fatal): %s", underlying, exc)
            return 0


decision_telemetry = DecisionTelemetry()
