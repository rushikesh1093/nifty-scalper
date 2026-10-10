"""
Replay implementations of the runtime ports (design §14.1, §14.3).

``ReplayMarketData`` owns every timestamp conversion, so no consumer under
app/services/trading/ or app/strategies/ changes its parsing:

  * candles: ``timestamp`` = ``row.timestamp.astimezone(IST).isoformat()`` —
    the shape and format tick_writer pushes to Redis; only CLOSED candles
    (``timestamp + 1 min <= now``) are visible.
  * ticks: ``ts`` = the UTC ISO time of the stored tick, so ``_tick_age_seconds``
    measures a real age against the replay clock.
  * chain: ``snapshot_time`` rendered like ``ts``.

No interpolation, ever (R13.4): a token with no tick and no snapshot within
``replay_max_bracket_gap_s`` of the clock has no price (``None``).

This module is the clock-scan allowlisted home of ReplayClock construction.
"""
from __future__ import annotations

import bisect
import copy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from app.services.trading.regime_config import RegimeConfig

IST = timezone(timedelta(hours=5, minutes=30))
CANDLE = timedelta(minutes=1)


def utc(ts: datetime) -> datetime:
    """Stored timestamps are UTC; SQLite hands them back naive."""
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc)


# ── clock ────────────────────────────────────────────────────────────────────

class ReplayClock:
    """Holds a mutable UTC instant advanced only by the engine."""

    def __init__(self, at: datetime):
        self._now = utc(at)

    def set(self, at: datetime) -> None:
        self._now = utc(at)

    def now_utc(self) -> datetime:
        return self._now

    def now_ist(self) -> datetime:
        return self._now.astimezone(IST)

    def trading_day(self) -> date:
        return self.now_ist().date()


# ── params / flags / regime config ───────────────────────────────────────────

class ReplayTradingParams:
    """ParamsPort over the run's frozen TradingParams."""

    def __init__(self, params) -> None:
        self._params = params

    def current(self):
        return self._params


class FrozenUserFlags:
    """UserFlagsPort over RunConfig.user_strategy_flags; never the live row."""

    def __init__(self, flags: Optional[dict] = None) -> None:
        self._flags = dict(flags or {})

    def current(self) -> dict:
        return dict(self._flags)


class FrozenRunControl:
    """RunControlPort over the run's frozen RunConfig; never the shared
    trading_config row (review R1). A params-file halt reads as a halt."""

    def __init__(self, cfg) -> None:
        self._cfg = cfg

    async def persisted_halt(self) -> tuple[bool, str]:
        if self._cfg.trading_halted:
            return True, "trading_halted in the run config"
        return False, ""

    async def study_phase(self) -> bool:
        return bool(self._cfg.study_phase)

    async def square_off_time(self) -> Optional[str]:
        return self._cfg.square_off_time


class ReplayRegimeConfig:
    """RegimeConfigPort: one frozen RegimeConfig per underlying for the whole run."""

    def __init__(self, configs: Optional[dict[str, RegimeConfig]] = None) -> None:
        self._configs = dict(configs or {})

    def for_underlying(self, underlying: str) -> RegimeConfig:
        return self._configs.setdefault(underlying.upper(), RegimeConfig())


# ── instruments ──────────────────────────────────────────────────────────────

class ReplayInstruments:
    """InstrumentPort: the instruments that actually traded on the replayed day
    (data_loader.replay_instruments), ignoring is_active and expiry >= today.
    The two ports differ in which rows the query returns — a data difference,
    not a code branch."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, date], list] = {}

    async def instruments_for(self, db, underlying: str) -> list:
        from app.runtime.context import trading_day
        from app.services.replay.data_loader import replay_instruments

        key = (underlying.upper(), trading_day())
        if key not in self._cache:
            rows = await replay_instruments(db, underlying, key[1])
            for r in rows:
                db.expunge(r)
            self._cache[key] = list(rows)
        return list(self._cache[key])

    def invalidate(self) -> None:          # POST /instruments/sync: nothing to drop in replay
        pass


# ── market data ──────────────────────────────────────────────────────────────

@dataclass
class _Series:
    """Time-ordered (ts, payload) pairs with a parallel key list for bisect."""
    times: list[datetime] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)

    def at_or_before(self, now: datetime) -> Optional[tuple[datetime, dict]]:
        i = bisect.bisect_right(self.times, now) - 1
        return (self.times[i], self.rows[i]) if i >= 0 else None

    def between(self, lo: datetime, hi: datetime) -> list[datetime]:
        """Timestamps in (lo, hi]."""
        a = bisect.bisect_right(self.times, lo)
        b = bisect.bisect_right(self.times, hi)
        return self.times[a:b]


def series(pairs) -> _Series:
    s = _Series()
    for ts, row in sorted(pairs, key=lambda p: p[0]):
        s.times.append(ts)
        s.rows.append(row)
    return s


class ReplayMarketData:
    """MarketDataPort answering from the loaded day at the clock's current time."""

    def __init__(self, clock: ReplayClock, *, spot_tokens: dict[str, str], vix_token: str,
                 max_bracket_gap_s: int) -> None:
        self.clock = clock
        self.spot_tokens = dict(spot_tokens)          # token -> underlying
        self.vix_token = vix_token
        self.max_gap = timedelta(seconds=max_bracket_gap_s)
        self.kv: dict[str, Any] = {}
        self.published = 0
        self.candles: dict[str, _Series] = {}         # spot token -> candles keyed by CLOSE time
        self.ticks: dict[str, _Series] = {}           # option token -> stored ticks
        self.snaps: dict[str, _Series] = {}           # option token -> chain-snapshot rows
        self.chains: dict[tuple[str, str], _Series] = {}   # (underlying, expiry) -> strike lists
        self.vix: Optional[float] = None
        self.overrides: dict[str, dict] = {}          # synthesised path-2 ticks, one step only

    def load_day(self, day_data) -> None:
        self.candles = {tok: series((utc(c["timestamp"]) + CANDLE, c) for c in rows)
                        for tok, rows in day_data.candles.items()}
        self.ticks = {tok: series((utc(t["timestamp"]), t) for t in rows)
                      for tok, rows in day_data.ticks.items()}
        self.snaps = {tok: series((utc(s["snapshot_time"]), s) for s in rows)
                      for tok, rows in day_data.snapshots.items()}
        self.chains = {key: series((utc(ts), strikes) for ts, strikes in pairs)
                       for key, pairs in day_data.chains.items()}
        self.vix = day_data.vix
        self.overrides.clear()

    # rendering ---------------------------------------------------------------

    @staticmethod
    def _render_candle(c: dict) -> dict:
        return {
            "token": c["token"], "interval": "ONE_MINUTE",
            "timestamp": utc(c["timestamp"]).astimezone(IST).isoformat(),
            "open": float(c["open"]), "high": float(c["high"]), "low": float(c["low"]),
            "close": float(c["close"]), "volume": int(c.get("volume") or 0),
            "oi": int(c.get("oi") or 0),
        }

    @staticmethod
    def _render_tick(t: dict, ts: datetime) -> dict:
        return {
            "token": t["token"], "ltp": float(t["ltp"] or 0.0),
            "open": t.get("open"), "high": t.get("high"), "low": t.get("low"),
            "close": t.get("close"), "volume": t.get("volume"), "oi": t.get("oi"),
            "best_bid": t.get("best_bid"), "best_ask": t.get("best_ask"),
            "ts": utc(ts).isoformat(),
        }

    @staticmethod
    def _render_snapshot(s: dict, ts: datetime) -> dict:
        return {
            "token": s["token"], "ltp": float(s.get("ltp") or 0.0),
            "open": None, "high": None, "low": None, "close": None,
            "volume": s.get("volume"), "oi": s.get("oi"),
            "best_bid": s.get("bid"), "best_ask": s.get("ask"), "iv": s.get("iv"),
            "ts": utc(ts).isoformat(),
        }

    # MarketDataPort ----------------------------------------------------------

    async def get_candles(self, token: str, interval: str, count: int = 100) -> list[dict]:
        s = self.candles.get(token)
        if s is None or interval != "ONE_MINUTE":
            return []
        i = bisect.bisect_right(s.times, self.clock.now_utc())
        return [self._render_candle(c) for c in s.rows[max(0, i - count):i]]

    async def get_tick(self, token: str) -> Optional[dict]:
        if token in self.overrides:
            return copy.deepcopy(self.overrides[token])
        now = self.clock.now_utc()
        s = self.ticks.get(token)
        hit = s.at_or_before(now) if s is not None else None
        if hit is not None:
            return self._render_tick(hit[1], hit[0])
        snap = self.snaps.get(token)
        hit = snap.at_or_before(now) if snap is not None else None
        if hit is not None and now - hit[0] <= self.max_gap:
            return self._render_snapshot(hit[1], hit[0])
        return None

    async def get_ltp(self, token: str) -> Optional[float]:
        if token in self.spot_tokens:
            s = self.candles.get(token)
            hit = s.at_or_before(self.clock.now_utc()) if s is not None else None
            return float(hit[1]["close"]) if hit is not None else None
        if token == self.vix_token:
            return self.vix
        tick = await self.get_tick(token)
        return float(tick["ltp"]) if tick and tick.get("ltp") else None

    async def get_option_chain(self, underlying: str, expiry: str) -> Optional[dict]:
        s = self.chains.get((underlying.upper(), str(expiry)))
        hit = s.at_or_before(self.clock.now_utc()) if s is not None else None
        if hit is None:
            return None
        return {"underlying": underlying.upper(), "expiry": str(expiry),
                "snapshot_time": utc(hit[0]).isoformat(), "strikes": copy.deepcopy(hit[1])}

    async def get(self, key: str) -> Optional[Any]:
        v = self.kv.get(key)
        return copy.deepcopy(v) if v is not None else None

    async def set(self, key: str, value: Any, ttl: int = 60) -> None:
        self.kv[key] = copy.deepcopy(value)            # TTL ignored (§14.3)

    async def delete(self, key: str) -> None:
        self.kv.pop(key, None)

    async def publish_signal(self, strategy: str, signal: dict) -> None:
        self.published += 1                            # UI fan-out: a counter in replay

    # engine helpers (not part of the port) -----------------------------------

    def tick_times(self, token: str, lo: datetime, hi: datetime) -> list[datetime]:
        s = self.ticks.get(token)
        return s.between(lo, hi) if s is not None else []

    def bracketing_snapshots(self, token: str, lo: datetime, hi: datetime):
        """(before, after) snapshot rows bracketing (lo, hi] within the max gap, else None."""
        s = self.snaps.get(token)
        if s is None:
            return None
        before = s.at_or_before(lo)
        j = bisect.bisect_left(s.times, hi)
        after = (s.times[j], s.rows[j]) if j < len(s.times) else None
        if before is None or after is None:
            return None
        if lo - before[0] > self.max_gap or after[0] - hi > self.max_gap:
            return None
        return before, after
