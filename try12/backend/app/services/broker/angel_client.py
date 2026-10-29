"""
AngelOne SmartAPI REST client.

Header reference (from official SmartAPI docs):
  Login endpoint   → X-PrivateKey: <api_key>
  All other calls  → Authorization: Bearer <jwt>
                     X-PrivateKey:  <api_key>
                     X-ClientCode:  <client_id>
                     X-FeedToken:   <feed_token>   (market data calls)
"""
import asyncio
import logging
import time
import pyotp
import httpx
from datetime import datetime
from typing import Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://apiconnect.angelbroking.com"

# getCandleData is rate limited (about 3 requests/s per key). Bursts get HTTP 403
# "Access denied because of exceeding access rate", which used to send the
# startup seed straight to its (often empty) DB fallback. Space the calls out and
# retry that answer with backoff.
HIST_MIN_INTERVAL_S = 0.4
HIST_RETRY_DELAYS_S = (1.5, 3.0, 6.0, 12.0)
_hist_lock = asyncio.Lock()
_hist_last = 0.0


# Session lifetime. AngelOne sessions last about 8 h; we treat one as expired at
# 7 h, and the session keeper (run_session_keeper) renews an existing session
# once it is 6 h old, checking every 5 min, so it never reaches the 7 h limit.
SESSION_MAX_AGE_S = 7 * 3600
SESSION_RENEW_AGE_S = 6 * 3600
SESSION_CHECK_INTERVAL_S = 300


def _rate_limited(resp) -> bool:
    if resp.status_code == 429:
        return True
    return resp.status_code == 403 and "exceeding access rate" in (resp.text or "").lower()

# Common headers sent on every request
_BASE = {
    "Content-Type":       "application/json",
    "Accept":             "application/json",
    "X-UserType":         "USER",
    "X-SourceID":         "WEB",
    "X-ClientLocalIP":    "127.0.0.1",
    "X-ClientPublicIP":   "127.0.0.1",
    "X-MACAddress":       "00:00:00:00:00:00",
}


class AngelSession:
    """Holds live session tokens from an AngelOne login."""

    def __init__(self):
        self.jwt_token:     Optional[str]      = None
        self.refresh_token: Optional[str]      = None
        self.feed_token:    Optional[str]      = None
        self.client_code:   Optional[str]      = None
        self.logged_in_at:  Optional[datetime] = None

    # Headers for every authenticated REST call
    @property
    def auth_headers(self) -> dict:
        return {
            **_BASE,
            "Authorization": f"Bearer {self.jwt_token}",
            "X-PrivateKey":  settings.ANGEL_API_KEY,
            "X-ClientCode":  settings.ANGEL_CLIENT_ID,
            "X-FeedToken":   self.feed_token or "",
        }

    # Headers for the market-data WebSocket (official spec: lowercase keys,
    # jwt WITHOUT the "Bearer " prefix). Built here so credential settings are
    # read in exactly one module (R1.7).
    @property
    def ws_feed_headers(self) -> dict:
        return {
            "Authorization": self.jwt_token or "",
            "x-api-key":     settings.ANGEL_API_KEY,
            "x-client-code": settings.ANGEL_CLIENT_ID,
            "x-feed-token":  self.feed_token or "",
        }

    def age_s(self) -> Optional[float]:
        """Seconds since the tokens were issued; None when there is no session."""
        if not self.jwt_token or not self.logged_in_at:
            return None
        return (datetime.now() - self.logged_in_at).total_seconds()

    def is_valid(self) -> bool:
        age = self.age_s()
        return age is not None and age < SESSION_MAX_AGE_S

    def needs_renewal(self) -> bool:
        """True when a session exists and is old enough for the keeper to renew."""
        age = self.age_s()
        return age is not None and age >= SESSION_RENEW_AGE_S


def set_credentials(api_key: str, client_id: str, password: str, totp_secret: str) -> None:
    """Runtime credential override (POST /auth/login). Values are held in-process only."""
    settings.ANGEL_API_KEY = api_key
    settings.ANGEL_CLIENT_ID = client_id
    settings.ANGEL_PASSWORD = password
    settings.ANGEL_TOTP_SECRET = totp_secret


# App-level session singleton.
# NOTE: This is intentionally a module-level singleton shared by all AngelClient
# instances (there should only ever be one).  It is NOT thread/task-safe to create
# multiple AngelClient objects simultaneously — doing so in tests should use mocking
# rather than instantiation.
_session = AngelSession()


class AngelClient:
    """Async wrapper around AngelOne SmartAPI."""

    def __init__(self):
        self._http: Optional[httpx.AsyncClient] = None

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                base_url=BASE_URL,
                timeout=httpx.Timeout(15.0),
                headers=_BASE,
            )
        return self._http

    # ─────────────────────────────────────────────────────────────────────────
    # Auth
    # ─────────────────────────────────────────────────────────────────────────

    async def login(self) -> AngelSession:
        """
        Login with client_id + password + TOTP.
        AngelOne login endpoint requires X-PrivateKey (= API key).
        """
        totp = pyotp.TOTP(settings.ANGEL_TOTP_SECRET).now()
        payload = {
            "clientcode": settings.ANGEL_CLIENT_ID,
            "password":   settings.ANGEL_PASSWORD,
            "totp":       totp,
            "state":      "STATE_VARIABLE",
        }
        login_headers = {
            **_BASE,
            "X-PrivateKey": settings.ANGEL_API_KEY,
        }

        http = await self._client()
        resp = await http.post(
            "/rest/auth/angelbroking/user/v1/loginByPassword",
            json=payload,
            headers=login_headers,
        )

        # Always read the body — 400 responses have useful error messages
        try:
            data = resp.json()
        except Exception:
            resp.raise_for_status()
            return _session

        if resp.status_code != 200 or not data.get("status"):
            raise RuntimeError(
                f"AngelOne login failed [{resp.status_code}]: "
                f"{data.get('message', 'unknown error')} | "
                f"errorCode={data.get('errorCode')}"
            )

        d = data["data"]
        _session.jwt_token     = d["jwtToken"]
        _session.refresh_token = d["refreshToken"]
        _session.feed_token    = d["feedToken"]
        _session.client_code   = settings.ANGEL_CLIENT_ID
        _session.logged_in_at  = datetime.now()

        logger.info("✅ AngelOne login OK")   # credential values are never logged (R1.7)
        await self._persist_to_redis()        # every new session is cached, in one format
        return _session

    async def refresh_tokens(self) -> AngelSession:
        """Refresh JWT using the refresh token."""
        http = await self._client()
        resp = await http.post(
            "/rest/auth/angelbroking/jwt/v1/generateTokens",
            json={"refreshToken": _session.refresh_token},
            headers={**_BASE, "X-PrivateKey": settings.ANGEL_API_KEY,
                     "Authorization": f"Bearer {_session.jwt_token}"},
        )
        try:
            data = resp.json()
        except Exception:
            logger.warning("Token refresh parse error — re-logging in")
            return await self.login()

        if not data.get("status"):
            logger.warning("Token refresh failed — re-logging in: %s", data.get("message"))
            return await self.login()

        _session.jwt_token     = data["data"]["jwtToken"]
        _session.refresh_token = data["data"]["refreshToken"]
        if data["data"].get("feedToken"):
            _session.feed_token = data["data"]["feedToken"]
        _session.logged_in_at  = datetime.now()
        logger.info("AngelOne tokens refreshed.")
        await self._persist_to_redis()
        return _session

    async def session_works(self) -> bool:
        """True when AngelOne accepts the current JWT (one getProfile call).

        Age alone cannot tell: a login elsewhere (another script, or the AngelOne
        web/app) can invalidate a token that is still young. Network errors
        propagate; the caller decides."""
        if not _session.jwt_token:
            return False
        http = await self._client()
        resp = await http.get("/rest/secure/angelbroking/user/v1/getProfile",
                              headers=_session.auth_headers)
        try:
            data = resp.json()
        except Exception:
            return False
        return resp.status_code == 200 and bool(data.get("status"))

    async def resume_or_login(self) -> AngelSession:
        """Startup: reuse the session cached in Redis when it is still valid and
        AngelOne still accepts it; otherwise log in. A restart therefore costs no
        login while the cached session works, and still fixes a dead session."""
        try:
            if (await self._restore_from_redis() and _session.is_valid()
                    and await self.session_works()):
                logger.info("AngelOne session reused from Redis (%.1f h old).",
                            (_session.age_s() or 0) / 3600)
                return _session
        except Exception as exc:
            logger.warning("Cached AngelOne session could not be checked (%s); logging in.", exc)
        return await self.login()

    async def keep_alive(self) -> bool:
        """Renew an existing session once it is older than SESSION_RENEW_AGE_S.

        It never creates the first session: startup and the feed log in, and a
        keeper that retried a failing login every few minutes could get the
        account locked. Uses the refresh token when there is one (refresh_tokens
        itself falls back to a full login when AngelOne refuses it), otherwise a
        TOTP login. Returns True when it renewed, False when there was nothing to
        do. Raises when neither works (network down, bad credentials)."""
        if not _session.needs_renewal():
            return False
        if _session.refresh_token:
            try:
                await self.refresh_tokens()
            except Exception as exc:
                logger.warning("AngelOne token refresh failed (%s); logging in again.", exc)
                await self.login()
        else:
            await self.login()
        return True

    async def run_session_keeper(self, interval_s: float = SESSION_CHECK_INTERVAL_S):
        """Check the session every *interval_s* (first check immediately) and renew
        it before it goes stale. Runs until cancelled. A failure is logged, alerted
        (throttled) and retried at the next check."""
        from app.core.alerts import alert, CRITICAL, INFO, WARNING
        failures = 0
        while True:
            try:
                if await self.keep_alive():
                    logger.info("AngelOne session renewed by the session keeper.")
                if failures:
                    alert("session_ok", f"AngelOne session renewed after {failures} "
                          f"failed attempt(s)", level=INFO)
                failures = 0
            except Exception as exc:
                failures += 1
                logger.error("AngelOne session renewal failed (attempt %d): %s", failures, exc)
                alert("session_renew_failed",
                      f"AngelOne session renewal failed ({str(exc)[:120]}); "
                      f"retrying every {int(interval_s)}s",
                      level=CRITICAL if failures >= 3 else WARNING, throttle_s=1800)
            await asyncio.sleep(interval_s)

    async def ensure_logged_in(self) -> AngelSession:
        """
        Ensure a live session — in order:
          1. Already valid in memory → return immediately
          2. Try restore from Redis (survives server restarts)
          3. Try refresh with existing refresh token
          4. Full re-login with TOTP
        """
        if _session.is_valid():
            return _session

        # Try restore from Redis before doing a full login
        restored = await self._restore_from_redis()
        if restored and _session.is_valid():
            logger.info("AngelOne session restored from Redis cache.")
            return _session

        if _session.refresh_token:
            try:
                await self.refresh_tokens()      # persists to Redis itself
                return _session
            except Exception as e:
                logger.warning("Token refresh failed (%s) — re-logging in.", e)

        await self.login()                       # persists to Redis itself
        return _session

    async def _persist_to_redis(self):
        """Save current session tokens to Redis so they survive restarts."""
        try:
            from app.services.cache.redis_client import redis_cache
            import json
            if not redis_cache._redis:
                return
            data = {
                "jwt_token":     _session.jwt_token,
                "refresh_token": _session.refresh_token,
                "feed_token":    _session.feed_token,
                "client_code":   _session.client_code,
                "logged_in_at":  _session.logged_in_at.isoformat() if _session.logged_in_at else None,
            }
            await redis_cache._redis.setex("session:angel", 8 * 3600, json.dumps(data))
            logger.debug("AngelOne session persisted to Redis.")
        except Exception as e:
            logger.debug("Could not persist session to Redis: %s", e)

    async def _restore_from_redis(self) -> bool:
        """Try to load session tokens from Redis. Returns True if restored."""
        try:
            from app.services.cache.redis_client import redis_cache
            import json
            from datetime import datetime
            if not redis_cache._redis:
                return False
            raw = await redis_cache._redis.get("session:angel")
            if not raw:
                return False
            data = json.loads(raw)
            if not data.get("jwt_token"):
                return False
            ts = data.get("logged_in_at")
            cached_at = datetime.fromisoformat(ts) if ts else None
            # The feed logs in on every reconnect without writing Redis, so the
            # cached copy can be older than the tokens in memory: never go back.
            if _session.jwt_token and _session.logged_in_at and (
                    cached_at is None or cached_at <= _session.logged_in_at):
                return False
            _session.jwt_token     = data["jwt_token"]
            _session.refresh_token = data.get("refresh_token")
            _session.feed_token    = data.get("feed_token")
            _session.client_code   = data.get("client_code")
            _session.logged_in_at  = cached_at
            return True
        except Exception as e:
            logger.debug("Could not restore session from Redis: %s", e)
            return False

    async def logout(self):
        if not _session.jwt_token:
            return
        http = await self._client()
        try:
            await http.post(
                "/rest/secure/angelbroking/user/v1/logout",
                json={"clientcode": settings.ANGEL_CLIENT_ID},
                headers=_session.auth_headers,
            )
        except Exception:
            pass
        _session.jwt_token = _session.refresh_token = _session.feed_token = None
        try:
            from app.services.cache.redis_client import redis_cache
            if redis_cache._redis:
                await redis_cache._redis.delete("session:angel")
        except Exception:
            pass
        logger.info("AngelOne logged out.")

    # ─────────────────────────────────────────────────────────────────────────
    # Instruments
    # ─────────────────────────────────────────────────────────────────────────

    async def get_instrument_dump(self) -> list[dict]:
        """Download full AngelOne scrip master (~100k instruments). No auth needed."""
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(settings.INSTRUMENT_DUMP_URL)
            resp.raise_for_status()
            return resp.json()

    async def search_scrip(self, exchange: str, search_text: str) -> list[dict]:
        await self.ensure_logged_in()
        http = await self._client()
        resp = await http.post(
            "/rest/secure/angelbroking/order/v1/searchScrip",
            json={"exchange": exchange, "searchscrip": search_text},
            headers=_session.auth_headers,
        )
        resp.raise_for_status()
        return resp.json().get("data", []) or []

    # ─────────────────────────────────────────────────────────────────────────
    # Market data
    # ─────────────────────────────────────────────────────────────────────────

    async def get_candle_data(
        self,
        exchange: str,
        symbol_token: str,
        interval: str,
        from_date: str,
        to_date: str,
    ) -> list[list]:
        """
        Fetch historical OHLC candles.
        interval: ONE_MINUTE | THREE_MINUTE | FIVE_MINUTE | TEN_MINUTE |
                  FIFTEEN_MINUTE | THIRTY_MINUTE | ONE_HOUR | ONE_DAY
        from_date / to_date: "YYYY-MM-DD HH:MM"
        Returns: [[timestamp, open, high, low, close, volume], ...]
        """
        global _hist_last
        await self.ensure_logged_in()
        http = await self._client()
        body = {"exchange": exchange, "symboltoken": symbol_token, "interval": interval,
                "fromdate": from_date, "todate": to_date}
        for attempt in range(len(HIST_RETRY_DELAYS_S) + 1):
            async with _hist_lock:                      # one at a time, spaced out
                wait = HIST_MIN_INTERVAL_S - (time.monotonic() - _hist_last)
                if wait > 0:
                    await asyncio.sleep(wait)
                resp = await http.post("/rest/secure/angelbroking/historical/v1/getCandleData",
                                       json=body, headers=_session.auth_headers)
                _hist_last = time.monotonic()
            if not _rate_limited(resp) or attempt == len(HIST_RETRY_DELAYS_S):
                break
            delay = HIST_RETRY_DELAYS_S[attempt]
            logger.info("getCandleData rate-limited (%s %s) — retry %d after %.1fs",
                        exchange, symbol_token, attempt + 1, delay)
            await asyncio.sleep(delay)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("status"):
            logger.warning("getCandleData: %s", data.get("message"))
            return []
        return data.get("data", []) or []

    async def get_ltp(self, exchange: str, symbol_token: str, trading_symbol: str = "") -> dict:
        """LTP for a single symbol (REST fallback when WebSocket is down)."""
        await self.ensure_logged_in()
        http = await self._client()
        resp = await http.post(
            "/rest/secure/angelbroking/market/v1/quote/",
            json={"mode": "LTP", "exchangeTokens": {exchange: [symbol_token]}},
            headers=_session.auth_headers,
        )
        resp.raise_for_status()
        fetched = resp.json().get("data", {}).get("fetched", [])
        return fetched[0] if fetched else {}

    async def get_full_quote(self, exchange: str, tokens: list[str]) -> list[dict]:
        """Full market depth for up to 50 tokens."""
        await self.ensure_logged_in()
        http = await self._client()
        resp = await http.post(
            "/rest/secure/angelbroking/market/v1/quote/",
            json={"mode": "FULL", "exchangeTokens": {exchange: tokens}},
            headers=_session.auth_headers,
        )
        resp.raise_for_status()
        return resp.json().get("data", {}).get("fetched", []) or []

    async def get_option_chain(self, name: str, expiry_date: str, strike_price: str) -> dict:
        """Option chain for a given underlying, expiry, and center strike."""
        await self.ensure_logged_in()
        http = await self._client()
        resp = await http.get(
            "/rest/secure/angelbroking/market/v1/optionChain",
            params={"name": name, "expirydate": expiry_date, "strikeprice": strike_price},
            headers=_session.auth_headers,
        )
        resp.raise_for_status()
        return resp.json().get("data", {}) or {}

    # ─────────────────────────────────────────────────────────────────────────
    # Orders — PAPER TRADING ONLY (design §13, R1.2)
    #
    # This client has NO order-placing, order-modifying or order-cancelling
    # method. They were deleted rather than guarded: a guard is a thing that
    # can be mis-set. tests/test_paper_only_guard.py fails if one reappears.
    #
    # Three read-only methods remain on AngelOne's `order/v1` path, because
    # AngelOne groups them there even though none of them places anything:
    #   search_scrip    -> order/v1/searchScrip   (instrument/token lookup)
    #   get_order_book  -> order/v1/getOrderBook  (empty for a paper account)
    #   get_positions   -> order/v1/getPosition   (operator reconciliation)
    # They are the only entries of ALLOWED_ORDER_PATHS in the guard test. That
    # allowlist is not a loophole: any new order/v1 path fails the suite until
    # it is deliberately added there, and broker paths may appear in no module
    # other than this one.
    # ─────────────────────────────────────────────────────────────────────────

    async def get_order_book(self) -> list[dict]:
        await self.ensure_logged_in()
        http = await self._client()
        resp = await http.get(
            "/rest/secure/angelbroking/order/v1/getOrderBook",
            headers=_session.auth_headers,
        )
        resp.raise_for_status()
        return resp.json().get("data", []) or []

    async def get_positions(self) -> list[dict]:
        await self.ensure_logged_in()
        http = await self._client()
        resp = await http.get(
            "/rest/secure/angelbroking/order/v1/getPosition",
            headers=_session.auth_headers,
        )
        resp.raise_for_status()
        return resp.json().get("data", []) or []

    async def get_funds(self) -> dict:
        await self.ensure_logged_in()
        http = await self._client()
        resp = await http.get(
            "/rest/secure/angelbroking/user/v1/getRMS",
            headers=_session.auth_headers,
        )
        resp.raise_for_status()
        return resp.json().get("data", {}) or {}

    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()


# Singletons
angel_client = AngelClient()


async def get_angel_client() -> AngelClient:
    """FastAPI dependency injection."""
    return angel_client

