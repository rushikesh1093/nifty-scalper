"""
AngelOne SmartAPI REST client.

Header reference (from official SmartAPI docs):
  Login endpoint   → X-PrivateKey: <api_key>
  All other calls  → Authorization: Bearer <jwt>
                     X-PrivateKey:  <api_key>
                     X-ClientCode:  <client_id>
                     X-FeedToken:   <feed_token>   (market data calls)
"""
import logging
import pyotp
import httpx
from datetime import datetime
from typing import Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://apiconnect.angelbroking.com"

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

    def is_valid(self) -> bool:
        if not self.jwt_token or not self.logged_in_at:
            return False
        elapsed = (datetime.now() - self.logged_in_at).total_seconds()
        return elapsed < 7 * 3600   # refresh before 8-hour expiry


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
        _session.logged_in_at  = datetime.now()
        logger.info("AngelOne tokens refreshed.")
        return _session

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
                await self.refresh_tokens()
                await self._persist_to_redis()
                return _session
            except Exception as e:
                logger.warning("Token refresh failed (%s) — re-logging in.", e)

        await self.login()
        await self._persist_to_redis()
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
            _session.jwt_token     = data["jwt_token"]
            _session.refresh_token = data.get("refresh_token")
            _session.feed_token    = data.get("feed_token")
            _session.client_code   = data.get("client_code")
            ts = data.get("logged_in_at")
            _session.logged_in_at  = datetime.fromisoformat(ts) if ts else None
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
        await self.ensure_logged_in()
        http = await self._client()
        resp = await http.post(
            "/rest/secure/angelbroking/historical/v1/getCandleData",
            json={
                "exchange":    exchange,
                "symboltoken": symbol_token,
                "interval":    interval,
                "fromdate":    from_date,
                "todate":      to_date,
            },
            headers=_session.auth_headers,
        )
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

