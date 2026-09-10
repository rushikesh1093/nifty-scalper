"""
Nifty Scalper — FastAPI application entry point.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.logging import setup_logging
from app.services.cache.redis_client import redis_cache
from app.services.market_data.market_manager import market_manager
from app.api.routes import auth, market, instruments, trading
from app.api.routes.report import router as report_router

setup_logging("DEBUG" if settings.DEBUG else "INFO")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────────────────
    logger.info("Starting %s...", settings.APP_NAME)

    # Connect Redis
    await redis_cache.connect()

    # Start market data pipeline (login → instrument sync → feed)
    market_started = False
    try:
        await market_manager.startup()
        market_started = True
    except Exception as exc:
        logger.error("MarketManager startup error: %s", exc, exc_info=True)
        logger.warning(
            "⚠️  App started WITHOUT live market feed. "
            "Check AngelOne credentials and Redis/Postgres connectivity."
        )

    # Start strategy runner + study collector
    from app.services.trading.strategy_runner import strategy_runner
    from app.services.market_data.study_collector import study_collector
    from app.services.trading.mode_manager import mode_manager
    try:
        await mode_manager.ensure_config()
        await mode_manager.reconcile_used_capital()
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
    _token_refresh_task.cancel()
    from app.services.trading.strategy_runner import strategy_runner
    from app.services.market_data.study_collector import study_collector
    await strategy_runner.stop()
    await study_collector.stop()
    await market_manager.shutdown()
    await redis_cache.disconnect()


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
_CONFIGURED_ORIGINS = [
    origin.strip()
    for origin in settings.CORS_ORIGINS.split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_LOCALHOST_ORIGINS + _CONFIGURED_ORIGINS,
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


@app.get("/")
async def root():
    return {"app": settings.APP_NAME, "status": "running", "docs": "/docs"}


@app.get("/health")
async def health():
    from app.services.market_data.feed import market_feed
    from app.services.broker.angel_client import _session
    redis_ok = await redis_cache.health()
    feed_up  = market_feed.is_connected
    session_valid = _session.is_valid()
    status = "ok" if (redis_ok and feed_up and session_valid) else "degraded"
    return {
        "status":          status,
        "redis":           "ok" if redis_ok else "down",
        "feed_connected":  feed_up,
        "session_valid":   session_valid,
        "subscriptions":   market_feed.subscription_count,
    }
