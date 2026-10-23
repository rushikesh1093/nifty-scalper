"""
ReplayEngine — bug 11 (design §14.4-§14.6, §6.6).

It drives the PRODUCTION step functions — strategy_runner.run_cycle,
paper_engine.handle_signal (via the SignalBus), risk_gate, option_selector,
fills, paper_engine.monitor_once / square_off_all and reconcile — under a
replay RuntimeContext. Nothing here reimplements a decision; the replay
context differs only in which data its ports return.

Run order (§14.6, §6.6), every step fail-closed:

  assert_paper_mode(allow_absent=True)      raw read; a LIVE row exits 3, no replay_runs row
  bind(replay context, cfg.mode="PAPER")
  wire_signal_handlers()                    or every signal is silently dropped (§14.1a)
  run_startup_checks(require_config_row=False)   all seven checks; exits 3 on violation
  INSERT replay_runs (RUNNING)
  coverage.report()                         no TRADEABLE day -> exit 2 (D11)
  per tradeable day: _pre_market_setup, then per candle close run_cycle and the
      intra-candle monitor steps; square_off_all("EOD"); assert open_capital_used == 0
  reconcile_performance(run_id) -> trade_metrics -> UPDATE replay_runs (COMPLETE)

Intra-candle stepping (§14.4), per open position, for the minute after each
candle close: (1) its option ticks, deduplicated to whole seconds; else (2)
chain snapshots bracketing the minute within replay_max_bracket_gap_s,
stepped PESSIMISTICALLY open -> low -> high -> close; else (3) a recorded
DATA_GAP and no exit evaluated. Prices are never interpolated.

The replay never reads or writes trading_config.used_capital and never
writes trading_config at all. PAPER ONLY: nothing here can reach a broker.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import secrets
import subprocess
import time as _perf
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import select, update

from app.core.config import settings
from app.core.trading_params import TradingParams, _coerce, _key_violations
from app.db.models.paper_trade import PaperPosition
from app.db.models.replay import ReplayRun
from app.runtime.context import RunConfig, RuntimeContext, bind, unbind
from app.runtime.serialize import canonical_json
from app.services.metrics import trade_metrics as tm
from app.services.replay import coverage as coverage_mod
from app.services.replay.data_loader import SPOT_TOKENS, load_day, spot_token
from app.services.replay.ports_impl import (
    CANDLE, IST, FrozenRunControl, FrozenUserFlags, ReplayClock, ReplayInstruments,
    ReplayMarketData,
    ReplayRegimeConfig, ReplayTradingParams, utc,
)
from app.services.trading import no_trade, risk_state
from app.services.trading.no_trade import ReasonCode
from app.services.trading.session_times import parse_square_off
from app.services.trading.startup_checks import (
    StartupAssertionError, assert_paper_mode, run_startup_checks,
)
from app.services.trading.wiring import wire_signal_handlers
from app.strategies.strategies import build_strategies

logger = logging.getLogger(__name__)

INTRA_CANDLE_ORDER = "PESSIMISTIC_LOW_FIRST"
RUN_CONFIG_ONLY_KEYS = ("square_off_time", "user_strategy_flags", "study_phase",
                        "trading_halted")
EXIT_OK, EXIT_FAILED, EXIT_NO_TRADEABLE_DAY, EXIT_STARTUP_ASSERTION = 0, 1, 2, 3
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class ReplayInvariantError(RuntimeError):
    """A position of this run is still open after square-off (NFR 5)."""


def new_run_id() -> str:
    """RPL-<ULID>: 48-bit wall-clock ms + 80 random bits, Crockford base32 (26 chars).
    The id is excluded from the determinism hash by construction."""
    from app.runtime.live import LiveClock
    ms = int(LiveClock().now_utc().timestamp() * 1000)
    n = (ms << 80) | secrets.randbits(80)
    chars = []
    for _ in range(26):
        n, r = divmod(n, 32)
        chars.append(_CROCKFORD[r])
    return "RPL-" + "".join(reversed(chars))


def code_version() -> str:
    try:
        out = subprocess.run(["git", "describe", "--always", "--dirty"], capture_output=True,
                             text=True, timeout=10, cwd=Path(__file__).resolve().parent)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


@dataclass
class ReplayArgs:
    date_from: date
    date_to: date
    underlyings: tuple[str, ...] = ("NIFTY",)
    params_overrides: dict = field(default_factory=dict)
    seed: int = 20260101
    oos_split: float = 0.70
    walk_forward: int = 0
    label: str = ""
    notes: str = ""                         # declared fitted parameters (§18 item 9)
    bootstrap_iterations: int = 10_000
    out_dir: Optional[Path] = None          # artefact directory; None = do not write
    synthetic: Optional[bool] = None        # declared data provenance; None = label heuristic


@dataclass
class ReplayResult:
    exit_code: int
    status: str
    run_id: Optional[str] = None
    violations: list[str] = field(default_factory=list)
    coverage: Optional[coverage_mod.CoverageReport] = None
    trades: list = field(default_factory=list)
    metrics: Optional[tm.MetricSet] = None
    determinism_hash: Optional[str] = None
    params_hash: Optional[str] = None
    stats: dict = field(default_factory=dict)
    validation: Optional[dict] = None
    artefacts: list[Path] = field(default_factory=list)
    error: Optional[str] = None


def resolve_params(overrides: dict) -> tuple[TradingParams, dict, list[str]]:
    """(TradingParams, RunConfig-only keys, key violations). Value violations are
    left to startup check 6, which reports every one of them."""
    tp_over = {k: v for k, v in overrides.items() if k not in RUN_CONFIG_ONLY_KEYS}
    extra = {k: overrides[k] for k in RUN_CONFIG_ONLY_KEYS if k in overrides}
    violations = _key_violations(tp_over)
    if violations:
        return TradingParams(), extra, violations
    try:
        return dataclasses.replace(TradingParams(), **_coerce(dict(tp_over))), extra, []
    except (TypeError, ValueError) as exc:
        return TradingParams(), extra, [f"invalid TradingParams value: {exc}"]


def run_config_for(p: TradingParams, extra: dict, underlyings) -> RunConfig:
    """The replay's frozen RunConfig: from the params file over the stored
    defaults, mode hardcoded PAPER, user flags all-enabled unless the file says
    otherwise — never read from the live trading_config row (§14.1)."""
    return RunConfig(
        mode="PAPER", capital=p.capital, max_trades_per_day=p.max_trades_per_day,
        max_open_positions=p.max_open_positions, max_lots_per_trade=p.max_lots_per_trade,
        max_daily_loss=p.max_daily_loss, max_daily_loss_pct=p.max_daily_loss_pct,
        open_exposure_cap_pct=p.open_exposure_cap_pct,
        square_off_time=str(extra.get("square_off_time", "15:15")),
        entry_cutoff_buffer_min=p.entry_cutoff_buffer_min,
        user_strategy_flags=dict(extra.get("user_strategy_flags") or {}),
        underlyings=tuple(u.upper() for u in underlyings),
        study_phase=bool(extra.get("study_phase", False)),
        trading_halted=bool(extra.get("trading_halted", False)),
    )


def params_record(p: TradingParams, cfg: RunConfig) -> tuple[dict, str]:
    """(replay_runs.params JSON, params_hash). The hash covers params AND cfg, so
    a capital change is a different parameter set (§14.1, §18 item 9)."""
    core = {"params": p, "cfg": cfg}
    params_hash = hashlib.sha256(canonical_json(core).encode("utf-8")).hexdigest()
    record = json.loads(canonical_json(core))
    record["intra_candle_order"] = INTRA_CANDLE_ORDER
    return record, params_hash


def reset_process_state() -> None:
    """The in-memory state a process restart clears. Replay applies it at the start
    of the run and of every replayed day (one simulated process per day); every
    risk counter is derived from persisted rows, so nothing decision-relevant is lost."""
    from app.services.trading import risk_gate
    from app.services.trading.paper_engine import paper_engine
    from app.services.trading.regime_detector import regime_detector
    from app.services.trading.strategy_orchestrator import strategy_orchestrator
    from app.services.trading.strategy_runner import strategy_runner
    from app.strategies.base import signal_aggregator

    paper_engine._last_known.clear()
    paper_engine._last_persist.clear()
    paper_engine._stale_warned.clear()
    paper_engine._stale_deferred.clear()
    paper_engine._square_off_cache = None
    regime_detector._regime_history.clear()
    regime_detector._last_log_time.clear()
    strategy_orchestrator._regime_cooldown.clear()
    strategy_orchestrator._flag_state.clear()
    strategy_orchestrator._current_regime.clear()
    getattr(strategy_orchestrator, "_daily_halts", {}).clear()
    strategy_runner._seen.clear()
    strategy_runner._seen_day = None
    strategy_runner._strategy_gate_last.clear()
    signal_aggregator.reset()
    risk_gate._sqlite_locks.clear()


def _ceil_second(ts: datetime) -> datetime:
    return ts if ts.microsecond == 0 else ts.replace(microsecond=0) + timedelta(seconds=1)


class ReplayEngine:
    def __init__(self, session_factory: Callable, *, report_builder: Optional[Callable] = None,
                 strategy_factory: Callable = build_strategies):
        self.sessions = session_factory
        self.report_builder = report_builder
        # The production strategy set; a test may supply scripted strategies so a
        # hand-computed dataset has known signals (R13.11). Not a decision branch.
        self.strategy_factory = strategy_factory
        self.clock: Optional[ReplayClock] = None
        self.market: Optional[ReplayMarketData] = None
        self.stats: dict = {}

    # ── context ──────────────────────────────────────────────────────────────

    def replay_context(self, run_id: str, p: TradingParams, cfg: RunConfig,
                       start: datetime) -> RuntimeContext:
        self.clock = ReplayClock(start)
        self.market = ReplayMarketData(
            self.clock, spot_tokens={tok: u for u, tok in SPOT_TOKENS.items()},
            vix_token=settings.VIX_TOKEN, max_bracket_gap_s=p.replay_max_bracket_gap_s)
        return RuntimeContext(
            run_id=run_id, clock=self.clock, market=self.market, sessions=self.sessions,
            regime_config=ReplayRegimeConfig(), params=ReplayTradingParams(p), cfg=cfg,
            user_flags=FrozenUserFlags(cfg.user_strategy_flags),
            instruments=ReplayInstruments(), control=FrozenRunControl(cfg), is_replay=True,
        )

    # ── run ──────────────────────────────────────────────────────────────────

    async def run(self, args: ReplayArgs) -> ReplayResult:
        unds = tuple(u.upper() for u in args.underlyings)
        bad = [u for u in unds if u not in SPOT_TOKENS]
        if bad or not unds:
            return ReplayResult(EXIT_FAILED, "INVALID_ARGS",
                                violations=[f"unknown underlyings {bad or list(unds)}"])
        if args.date_from > args.date_to:
            return ReplayResult(EXIT_FAILED, "INVALID_ARGS",
                                violations=["--from must be <= --to"])

        # 1. paper preflight: raw read, before any binding or ORM read of the row
        v = await assert_paper_mode(self.sessions, allow_absent=True)
        if v:
            return ReplayResult(EXIT_STARTUP_ASSERTION, "STARTUP_ASSERTION", violations=v)

        p, extra, key_v = resolve_params(args.params_overrides)
        if key_v:
            return ReplayResult(EXIT_STARTUP_ASSERTION, "STARTUP_ASSERTION", violations=key_v)
        cfg = run_config_for(p, extra, unds)
        record, params_hash = params_record(p, cfg)
        run_id = new_run_id()
        start = datetime.combine(args.date_from, datetime.min.time().replace(hour=9), IST)
        token = bind(self.replay_context(run_id, p, cfg, start))
        try:
            wire_signal_handlers()
            try:
                await run_startup_checks(require_config_row=False)
            except StartupAssertionError as exc:
                return ReplayResult(EXIT_STARTUP_ASSERTION, "STARTUP_ASSERTION",
                                    violations=exc.violations)
            return await self._run_bound(args, run_id, p, cfg, record, params_hash, unds)
        finally:
            unbind(token)

    async def _run_bound(self, args, run_id, p, cfg, record, params_hash, unds) -> ReplayResult:
        from app.runtime.live import LiveClock
        record = dict(record, label=args.label)
        async with self.sessions() as db:
            db.add(ReplayRun(
                run_id=run_id, created_at=LiveClock().now_utc(), date_from=args.date_from,
                date_to=args.date_to, underlyings=list(unds), params=record,
                params_hash=params_hash, seed=args.seed, code_version=code_version(),
                status="RUNNING", notes=args.notes or None))
            await db.commit()
        result = ReplayResult(EXIT_OK, "RUNNING", run_id=run_id, params_hash=params_hash)
        self.stats = {"steps_from_ticks": 0, "steps_from_snapshots": 0,
                      "candles_skipped_no_data": 0, "wall_seconds_per_underlying_day": {},
                      "days_replayed": 0}
        result.stats = self.stats
        try:
            async with self.sessions() as db:
                cov = await coverage_mod.report(db, unds, args.date_from, args.date_to, p)
            result.coverage = cov
            if not cov.tradeable:
                await self._finish(run_id, "NO_DATA", coverage=cov.as_dict())
                result.exit_code, result.status = EXIT_NO_TRADEABLE_DAY, "NO_DATA"
                await self._build_report(args, result, p, cfg, record)
                return result

            for day in cov.tradeable_days():
                day_unds = tuple(d.underlying for d in cov.tradeable if d.day == day)
                await self._replay_day(day, day_unds, cfg)

            from app.services.trading.reconcile import reconcile_performance
            await reconcile_performance(run_id)
            async with self.sessions() as db:
                trades = await tm.load_trades(db, tm.TradeFilter(run_id=run_id),
                                              flat_epsilon=p.flat_epsilon)
                fallback_ids = (await db.execute(select(PaperPosition.id).where(
                    PaperPosition.run_id == run_id,
                    PaperPosition.entry_fill_source == "LTP_FALLBACK"))).scalars().all()
            self.stats["ltp_only_fallbacks"] = len(fallback_ids)
            metrics = tm.compute(trades, cfg.capital, p)
            result.trades, result.metrics = trades, metrics
            result.determinism_hash = tm.determinism_hash(trades, metrics)
            result.status = "COMPLETE"
            await self._build_report(args, result, p, cfg, record)
            await self._finish(run_id, "COMPLETE", coverage=cov.as_dict(),
                               metrics=dict(tm.as_dict(metrics),
                                            determinism_hash=result.determinism_hash,
                                            stats=self.stats),
                               validation=result.validation)
            return result
        except Exception as exc:
            logger.error("Replay %s failed: %s", run_id, exc, exc_info=True)
            await self._finish(run_id, "FAILED", notes=traceback.format_exc())
            result.exit_code, result.status, result.error = EXIT_FAILED, "FAILED", repr(exc)
            return result

    async def _build_report(self, args, result, p, cfg, record) -> None:
        if self.report_builder is not None:
            await self.report_builder(self, args, result, p, cfg, record)

    async def _finish(self, run_id: str, status: str, **values) -> None:
        values = {k: (json.loads(json.dumps(v, default=str)) if k != "notes" else v)
                  for k, v in values.items() if v is not None}
        async with self.sessions() as db:
            if status == "FAILED" and "notes" in values:
                prior = (await db.execute(select(ReplayRun.notes).where(
                    ReplayRun.run_id == run_id))).scalar()
                values["notes"] = ((prior + "\n\n") if prior else "") + values["notes"]
            await db.execute(update(ReplayRun).where(ReplayRun.run_id == run_id)
                             .values(status=status, **values))
            await db.commit()

    # ── one trading day ──────────────────────────────────────────────────────

    async def _replay_day(self, day: date, unds: tuple[str, ...], cfg: RunConfig) -> None:
        from app.runtime.context import ctx
        from app.services.trading.paper_engine import paper_engine
        from app.services.trading.strategy_runner import strategy_runner

        t0 = _perf.perf_counter()
        async with self.sessions() as db:
            data = await load_day(db, day, unds)
        self.market.load_day(data)
        reset_process_state()
        strategy_runner._strategies = {u: self.strategy_factory(u) for u in unds}

        self.clock.set(datetime.combine(day, datetime.min.time().replace(hour=9), IST))
        await strategy_runner._pre_market_setup()

        # one merged, time-ordered stream of candle closes (§14.4) so the shared
        # caps interleave across underlyings exactly as they would live
        # Only this day's candles are stepped: the loaded series also carries the
        # previous sessions as indicator history (data_loader.HISTORY_LOOKBACK_DAYS).
        closes: dict[datetime, list[str]] = {}
        for u in unds:
            for c in data.candles.get(spot_token(u), []):
                if utc(c["timestamp"]).astimezone(IST).date() != day:
                    continue
                closes.setdefault(utc(c["timestamp"]) + CANDLE, []).append(u)
        square_off = parse_square_off(cfg.square_off_time)
        squared = False
        for close in sorted(closes):
            self.clock.set(close)
            for u in sorted(closes[close], key=unds.index):
                await strategy_runner.run_cycle(u)
            if self.clock.now_ist().time().replace(tzinfo=None) >= square_off:
                await paper_engine.square_off_all("EOD")
                squared = True
                break
            await self._intra_candle(close, close + CANDLE)
        if not squared:
            eod = datetime.combine(day, square_off, IST)
            if eod > self.clock.now_utc():
                self.clock.set(eod)
            await paper_engine.square_off_all("EOD")

        async with self.sessions() as db:
            left = await risk_state.open_capital_used(db, ctx().run_id)
            still_open = (await db.execute(select(PaperPosition.id).where(
                PaperPosition.run_id == ctx().run_id,
                PaperPosition.is_open == True))).scalars().all()  # noqa: E712
        if still_open or abs(left) > 1e-9:
            raise ReplayInvariantError(
                f"{day}: positions {list(still_open)} still open after square-off "
                f"(open_capital_used={left})")
        secs = round(_perf.perf_counter() - t0, 3)
        for u in unds:
            self.stats["wall_seconds_per_underlying_day"][f"{u}:{day.isoformat()}"] = \
                round(secs / len(unds), 3)
        self.stats["days_replayed"] += 1

    async def _intra_candle(self, lo: datetime, hi: datetime) -> None:
        """Monitor steps for (lo, hi] in the §14.4 precedence order."""
        from app.runtime.context import ctx
        from app.services.trading.paper_engine import paper_engine

        async with self.sessions() as db:
            open_pos = (await db.execute(select(PaperPosition.id, PaperPosition.token).where(
                PaperPosition.run_id == ctx().run_id,
                PaperPosition.is_open == True))).all()  # noqa: E712
        if not open_pos:
            return
        events: dict[datetime, dict[str, Optional[dict]]] = {}
        gaps: list[int] = []
        for pos_id, tok in open_pos:
            times = self.market.tick_times(tok, lo, hi)
            if times:                                            # path 1: real ticks
                for t in times:
                    events.setdefault(_ceil_second(t), {})
                self.stats["steps_from_ticks"] += len({_ceil_second(t) for t in times})
                continue
            bracket = self.market.bracketing_snapshots(tok, lo, hi)
            if bracket is not None:                              # path 2: pessimistic
                (_, before), (_, after) = bracket
                o, c = float(before.get("ltp") or 0), float(after.get("ltp") or 0)
                if o > 0 and c > 0:
                    path = (o, min(o, c), max(o, c), c)           # open -> low -> high -> close
                    for i, price in enumerate(path, 1):
                        t = lo + (hi - lo) * i / len(path)
                        events.setdefault(t, {})[tok] = {
                            "token": tok, "ltp": price, "open": None, "high": None,
                            "low": None, "close": None, "volume": None, "oi": None,
                            "best_bid": None, "best_ask": None, "ts": t.isoformat()}
                    self.stats["steps_from_snapshots"] += len(path)
                    continue
            gaps.append(pos_id)                                  # path 3: a recorded gap
        for pos_id in gaps:
            self.stats["candles_skipped_no_data"] += 1
            await no_trade.record_degradation_once(
                ctx().run_id, ReasonCode.DATA_GAP, position_id=pos_id,
                context={"what": "intra_candle_gap", "first_candle_close": lo.isoformat()},
                reason=f"No option tick or bracketing snapshot for position {pos_id} in the "
                       f"minute after {lo.isoformat()}; no exit evaluated (no interpolation)")
        for t in sorted(events):
            self.clock.set(t)
            self.market.overrides = dict(events[t])
            try:
                await paper_engine.monitor_once()
            finally:
                self.market.overrides = {}
