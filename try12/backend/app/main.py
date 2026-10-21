"""
Nifty Scalper — FastAPI application entry point.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.logging import setup_logging
from app.db import replay_store
from app.db.base import AsyncSessionLocal
from app.runtime.context import bind
from app.runtime.live import live_context
from app.runtime.middleware import RuntimeContextMiddleware
from app.services.cache.redis_client import redis_cache
from app.services.trading.startup_checks import (
    StartupAssertionError, assert_paper_mode, run_startup_checks,
)
from app.services.market_data.market_manager import market_manager
from app.api.routes import auth, market, instruments, trading
from app.api.routes.report import router as report_router
from app.api.routes.regime import router as regime_router
from app.api.routes.analytics import router as analytics_router
from app.api.routes.backtest import router as backtest_router
from app.api.routes.research import router as research_router
from app.services.backtest import jobs as backtest_jobs

setup_logging("DEBUG" if settings.DEBUG else "INFO")
logger = logging.getLogger(__name__)

MARKET_RETRY_INTERVAL_S = 60
MARKET_RETRY_MAX_ATTEMPTS = 60


async def _retry_market_startup() -> None:
    """Retry MarketManager.startup until the live feed is up. One-time steps that
    already succeeded (instrument sync, tick handler) are not repeated."""
    import asyncio as _asyncio
    for _ in range(MARKET_RETRY_MAX_ATTEMPTS):
        await _asyncio.sleep(MARKET_RETRY_INTERVAL_S)
        try:
            await market_manager.startup()
            logger.info("MarketManager startup succeeded on attempt %d — live feed starting",
                        market_manager.startup_attempts)
            return
        except _asyncio.CancelledError:
            raise
        except Exception:
            logger.error("MarketManager startup retry %d failed at %s — next try in %ds",
                         market_manager.startup_attempts, market_manager.startup_error,
                         MARKET_RETRY_INTERVAL_S)
    logger.error("MarketManager startup gave up after %d attempts; restart the backend",
                 MARKET_RETRY_MAX_ATTEMPTS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────────────────
    # Order is fixed by design §6.6. Nothing above the narrowed loop-start
    # `try` may sit inside a handler that catches Exception: the paper guard
    # must be able to stop the process (tests/test_lifespan_guard.py).
    logger.info("Starting %s...", settings.APP_NAME)

    from app.services.trading.strategy_runner import strategy_runner
    from app.services.market_data.study_collector import study_collector
    from app.services.trading.mode_manager import mode_manager

    # Connect Redis
    await redis_cache.connect()

    try:
        # 1. Paper preflight: raw-scalar read, BEFORE any ORM hydration of trading_config
        v = await assert_paper_mode(AsyncSessionLocal, allow_absent=True)
        if v:
            raise StartupAssertionError(v)
        cfg_row = await mode_manager.ensure_config(session_factory=AsyncSessionLocal)  # creates the row if absent
        await mode_manager.reconcile_used_capital(session_factory=AsyncSessionLocal)
        live = live_context(cfg_row, AsyncSessionLocal)   # builds RunConfig incl. mode
        bind(live)                                     # for tasks created below IN THIS TASK (loops, feed)
        await run_startup_checks(require_config_row=True)   # raises on violation
        app.state.runtime_context = live               # for request tasks, which do NOT inherit the
                                                       # lifespan task's context (R5 finding 1); set only
                                                       # after every check has passed
    except StartupAssertionError:
        logger.critical("Startup assertions failed -- refusing to start")  # violations already logged at ERROR
        await redis_cache.disconnect()
        raise                                           # propagates out of lifespan -> uvicorn exits non-zero
    except Exception as exc:                            # DB unreachable, etc.
        await redis_cache.disconnect()
        raise StartupAssertionError([f"startup could not prove paper mode: {exc!r}"]) from exc

    # 2. Market data: only after the guard has passed (login → instrument sync → feed)
    market_started = False
    try:
        await market_manager.startup()
        market_started = True
        try:                                            # logging only: never blocks startup
            from app.services.trading.option_selector import option_selector
            async with AsyncSessionLocal() as _db:
                await option_selector.log_startup_summary(_db)
        except Exception as exc:
            logger.warning("Option-selector startup summary unavailable: %s", exc)
    except Exception as exc:                            # unchanged tolerance: app may run feedless
        logger.error("MarketManager startup error: %s", exc, exc_info=True)
        logger.warning(
            "⚠️  App started WITHOUT live market feed — retrying every %ds "
            "(GET /health shows the failing step).", MARKET_RETRY_INTERVAL_S,
        )

    # 2b. Capital sufficiency: WARNING only (blockers-plan item 6), never blocks startup
    try:
        from app.runtime.context import ctx, params
        from app.services.trading.capital_check import (
            REFERENCE_LOT_SIZE, capital_sufficiency_warnings,
        )
        lot_sizes: dict = {}
        lot_source: dict = {}
        async with AsyncSessionLocal() as _db:
            for _und, _ref_lot in REFERENCE_LOT_SIZE.items():
                _lot = None
                try:
                    _rows = await ctx().instruments.instruments_for(_db, _und)
                    _lot = next((r.lot_size for r in _rows if r.lot_size), None)
                except Exception as exc:
                    logger.debug("capital check: %s lot size unavailable: %s", _und, exc)
                    await _db.rollback()
                lot_sizes[_und] = _lot or _ref_lot
                lot_source[_und] = "instruments" if _lot else "reference"
        for _w in capital_sufficiency_warnings(ctx().cfg.capital, params(), lot_sizes,
                                               lot_source=lot_source):
            logger.warning("Capital check: %s", _w)
    except Exception as exc:
        logger.warning("capital check unavailable: %s", exc)

    # 3. Loops: the ONLY tolerant block left
    try:
        await strategy_runner.start()
        await study_collector.start()
    except Exception as exc:
        logger.error("Strategy runner startup error: %s", exc, exc_info=True)

    # ── Proactive JWT token refresh every 6 hours ─────────────────────────────
    # AngelOne sessions expire after 8 hours. Without a refresh task the token
    # will expire mid-session (e.g. app started at 8 AM → token dies at 4 PM).
    # We refresh every 6 hours so there is always a 2-hour safety margin.
    async def _token_refresh_loop():
        import asyncio as _asyncio
        while True:
            await _asyncio.sleep(6 * 3600)   # 6 hours
            try:
                from app.services.broker.angel_client import angel_client as _ac
                await _ac.ensure_logged_in()
                logger.info("Proactive JWT token refresh completed.")
            except Exception as _e:
                logger.error("Proactive JWT token refresh failed: %s", _e)

    import asyncio as _asyncio
    _token_refresh_task = _asyncio.create_task(_token_refresh_loop())
    # A feedless start is retried in the background instead of waiting for a
    # manual restart (the scrip-master download or AngelOne can fail transiently).
    _market_retry_task = (None if market_started
                          else _asyncio.create_task(_retry_market_startup()))

    # ── Admin key posture (design §17.4, §20.1) ───────────────────────────────
    from app.api.guards import ADMIN_ONLY_ENDPOINTS, admin_key_configured
    if not admin_key_configured():
        logger.warning(
            "ADMIN_API_KEY is not set: %s return 503 until it is. Engaging a halt "
            "(POST /api/v1/trading/halt) still works; releasing one needs a restart "
            "with a key.", ", ".join(ADMIN_ONLY_ENDPOINTS))

    # ── Study-phase gate warning ──────────────────────────────────────────────
    try:
        from app.services.trading.mode_manager import mode_manager as _mm
        _cfg = await _mm.get_config()
        if _cfg and _cfg.study_phase:
            logger.warning(
                "⚠️  STUDY PHASE IS ACTIVE — all strategy signals will be RECORDED "
                "but NO paper positions will be opened.  "
                "Set study_phase=false via the API when ready to trade."
            )
        else:
            logger.info("✅ Study phase OFF — paper engine will open positions on signals.")
        if not market_started:
            logger.warning(
                "⚠️  MARKET FEED IS DOWN — strategies will not receive candles. "
                "Fix credentials and restart, or POST /api/v1/market/feed/subscribe to reconnect."
            )
    except Exception:
        pass

    yield

    # ── Shutdown ─────────────────────────────────────────────────────────────
    logger.info("Shutting down %s...", settings.APP_NAME)
    await backtest_jobs.shutdown()      # persists a running job as CANCELLED; never raises
    await replay_store.dispose_all()
    _token_refresh_task.cancel()
    if _market_retry_task is not None:
        _market_retry_task.cancel()
    from app.services.trading.strategy_runner import strategy_runner
    from app.services.market_data.study_collector import study_collector
    await strategy_runner.stop()
    await study_collector.stop()
    await market_manager.shutdown()
    await redis_cache.disconnect()
    app.state.runtime_context = None    # nothing may run against a shut-down context


app = FastAPI(
    title=settings.APP_NAME,
    description="Automated Nifty options scalping system",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS — allow Next.js dev server on any localhost port (3000-3010)
# and common prod origins. Add CORS_ORIGINS to .env to extend.
_LOCALHOST_ORIGINS = [
    f"http://localhost:{p}" for p in range(3000, 3011)
] + [
    f"http://127.0.0.1:{p}" for p in range(3000, 3011)
]

# Registered BEFORE CORS: Starlette makes the last-added middleware outermost,
# so CORS wraps this one and its 503 still carries CORS headers (design §6.6).
app.add_middleware(RuntimeContextMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_LOCALHOST_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(auth.router,        prefix="/api/v1")
app.include_router(market.router,      prefix="/api/v1")
app.include_router(instruments.router, prefix="/api/v1")
app.include_router(trading.router,     prefix="/api/v1")
app.include_router(report_router,      prefix="/api/v1")
app.include_router(regime_router,      prefix="/api/v1/regime")
app.include_router(analytics_router,   prefix="/api/v1")
app.include_router(backtest_router,    prefix="/api/v1")
app.include_router(research_router,    prefix="/api/v1")   # read-only history download


@app.get("/")
async def root():
    return {"app": settings.APP_NAME, "status": "running", "docs": "/docs"}


@app.get("/health")
async def health():
    from app.services.market_data.feed import market_feed
    from app.services.broker.angel_client import _session
    try:
        redis_ok = await redis_cache.health()
    except Exception:
        redis_ok = False
    try:
        feed_up = market_feed.is_connected
    except Exception:
        feed_up = False
    try:
        session_valid = _session.is_valid()
    except Exception:
        session_valid = False
    try:
        subs = market_feed.subscription_count
    except Exception:
        subs = 0
    status = "ok" if (redis_ok and feed_up and session_valid) else "degraded"
    return {
        "status":          status,
        "redis":           "ok" if redis_ok else "down",
        "feed_connected":  feed_up,
        "session_valid":   session_valid,
        "subscriptions":   subs,
        "market_startup":  {
            "stage":    getattr(market_manager, "startup_stage", None),
            "error":    getattr(market_manager, "startup_error", None),
            "attempts": getattr(market_manager, "startup_attempts", None),
        },
    }
