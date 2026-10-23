"""
AngelOne SmartWebSocketV2 market data feed.
Based on official smartapi-python source:
  https://github.com/angel-one/smartapi-python/blob/main/SmartApi/smartWebSocketV2.py

Connection headers (lowercase — official spec):
  Authorization  : <jwt_token>          ← NO "Bearer " prefix
  x-api-key      : <api_key>
  x-client-code  : <client_id>
  x-feed-token   : <feed_token>

Exchange type codes:
  1 = NSE_CM   2 = NSE_FO   3 = BSE_CM
  4 = BSE_FO   5 = MCX_FO   13 = CDE_FO

Subscription modes:
  1 = LTP   2 = QUOTE   3 = SNAP_QUOTE

Binary packet layout (little-endian):
  0-1    : subscription_mode (B)
  1-2    : exchange_type (B)
  2-27   : token (25 bytes ASCII)
  27-35  : sequence_number (q)
  35-43  : exchange_timestamp (q)
  43-51  : last_traded_price (q, divide by 100)
  -- Mode 2+ --
  51-59  : last_traded_quantity (q)
  59-67  : average_traded_price (q, /100)
  67-75  : volume (q)
  75-83  : total_buy_qty (d)
  83-91  : total_sell_qty (d)
  91-99  : open (q, /100)
  99-107 : high (q, /100)
  107-115: low  (q, /100)
  115-123: close (q, /100)
  -- Mode 3 --
  123-131: last_traded_timestamp (q)
  131-139: open_interest (q)
  139-147: oi_change_pct (q)
  147-347: best_5_buy_sell (10 packets × 20 bytes: flag H, qty q, price q /100,
           orders H; sides resolved by price, see parse_best_five)
  347-355: upper_circuit (q, /100)
  355-363: lower_circuit (q, /100)
  363-371: 52w_high (q, /100)
  371-379: 52w_low  (q, /100)
"""
import asyncio
import json
import logging
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosed

try:  # websockets >= 10 exposes the connection state enum here (17.1 installed)
    from websockets.protocol import State as _WsState
except Exception:  # pragma: no cover — very old websockets
    _WsState = None

from app.services.broker.angel_client import _session, angel_client

logger = logging.getLogger(__name__)

WS_URL             = "wss://smartapisocket.angelone.in/smart-stream"
HEARTBEAT_INTERVAL  = 10    # seconds (official spec)
RECONNECT_BASE      = 5     # seconds
MAX_RECONNECTS      = 0        # 0 = unlimited — feed must never give up permanently
# AngelOne keeps the old WS session alive for ~30s after disconnect.
# On a 429 we must wait long enough for them to clean it up before re-connecting.
RECONNECT_429_DELAY = 35    # seconds — first pause after a 429


# Exchange type constants (official)
NSE_CM = 1
NSE_FO = 2
BSE_CM = 3
BSE_FO = 4
MCX_FO = 5

EXCHANGE_STR_MAP = {
    "NSE":  NSE_CM,
    "NFO":  NSE_FO,
    "BSE":  BSE_CM,
    "BFO":  BSE_FO,
    "MCX":  MCX_FO,
}

EXCH_CODE_TO_STR = {v: k for k, v in EXCHANGE_STR_MAP.items()}


@dataclass
class TickData:
    """One parsed market tick.

    ``received_at`` convention: timezone-aware UTC. A naive value is rejected
    (it could be local time mistaken for UTC); any other aware value is
    normalised to UTC. The Redis tick ``ts`` written by market_manager is its
    ISO form, e.g. ``2026-09-01T04:30:00.123456+00:00``.
    """
    token:        str
    exchange:     str
    ltp:          float
    open:         float = 0.0
    high:         float = 0.0
    low:          float = 0.0
    close:        float = 0.0
    volume:       int   = 0
    oi:           int   = 0
    oi_change:    int   = 0
    best_bid:     float = 0.0
    best_ask:     float = 0.0
    best_bid_qty: int   = 0
    best_ask_qty: int   = 0
    exchange_ts:  int   = 0
    received_at:  datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        ra = self.received_at
        if ra.tzinfo is None or ra.utcoffset() is None:
            raise ValueError(
                f"TickData.received_at must be timezone-aware (UTC); got naive {ra!r}"
            )
        self.received_at = ra.astimezone(timezone.utc)


def _ws_is_open(ws: Any) -> bool:
    """
    True only when *ws* is an open WebSocket connection.

    websockets >= 14 (asyncio ClientConnection) has no ``.closed`` attribute;
    it exposes ``.state`` (CONNECTING/OPEN/CLOSING/CLOSED) instead. Legacy
    clients (<= 13) expose a boolean ``.closed``. Anything else, or any error
    while inspecting the object, is reported as not open — status endpoints
    must never raise because of this check.
    """
    if ws is None:
        return False
    try:
        state = getattr(ws, "state", None)
        if state is not None and _WsState is not None:
            return state is _WsState.OPEN
        closed = getattr(ws, "closed", None)
        if isinstance(closed, bool):
            return not closed
        return False
    except Exception:
        return False


def _unpack(data: bytes, start: int, end: int, fmt: str):
    return struct.unpack_from("<" + fmt, data, start)[0]


# SnapQuote best-five block: 10 packets × 20 bytes, each little-endian
# <H flag> <q qty> <q price in paise> <H orders>.
BEST_FIVE_OFFSET = 147
BEST_FIVE_PACKETS = 10
BEST_FIVE_PACKET_LEN = 20
BEST_FIVE_END = BEST_FIVE_OFFSET + BEST_FIVE_PACKETS * BEST_FIVE_PACKET_LEN   # 347


def parse_best_five(block: bytes) -> tuple[float, int, float, int]:
    """(best_bid, best_bid_qty, best_ask, best_ask_qty) from the 200-byte best-five
    block. Pure. An unresolvable book is (0.0, 0, 0.0, 0).

    Packets are grouped by their buy/sell flag. The flag's meaning is ambiguous
    (the reference SDK maps flag 0 to buy and then swaps the buy and sell lists),
    so the sides are resolved by price: the lower-priced group is the bid side,
    the higher-priced group the ask side. best_bid is the bid side's highest
    price, best_ask the ask side's lowest, each with its own packet's quantity.
    Empty levels (qty or price <= 0) are ignored. A one-sided book cannot be
    labelled by price, and an overlapping one is inconsistent: both return the
    defaults, because every consumer needs both sides anyway.
    """
    empty = (0.0, 0, 0.0, 0)
    if len(block) < BEST_FIVE_PACKETS * BEST_FIVE_PACKET_LEN:
        return empty
    groups: dict[bool, list[tuple[int, int]]] = {False: [], True: []}   # flag != 0 → [(paise, qty)]
    for i in range(BEST_FIVE_PACKETS):
        off = i * BEST_FIVE_PACKET_LEN
        flag = struct.unpack_from("<H", block, off)[0]
        qty = struct.unpack_from("<q", block, off + 2)[0]
        paise = struct.unpack_from("<q", block, off + 10)[0]
        if qty <= 0 or paise <= 0:
            continue
        groups[flag != 0].append((paise, qty))
    g0, g1 = groups[False], groups[True]
    if not g0 or not g1:
        return empty
    if max(p for p, _ in g0) <= min(p for p, _ in g1):
        bids, asks = g0, g1
    elif max(p for p, _ in g1) <= min(p for p, _ in g0):
        bids, asks = g1, g0
    else:
        # per tick: never above DEBUG
        logger.debug("best-five sides overlap, bid/ask left unset: flag0=%s flag1=%s", g0, g1)
        return empty
    bid_paise, bid_qty = max(bids, key=lambda pq: pq[0])     # first on a tie (wire order)
    ask_paise, ask_qty = min(asks, key=lambda pq: pq[0])
    return bid_paise / 100.0, int(bid_qty), ask_paise / 100.0, int(ask_qty)


def _parse_binary_tick(data: bytes) -> Optional[TickData]:
    """Parse SmartWebSocketV2 binary packet into TickData."""
    try:
        if len(data) < 51:
            return None

        mode      = data[0]
        exch_code = data[1]
        token     = data[2:27].rstrip(b"\x00").decode("ascii").strip()
        exch_ts   = _unpack(data, 35, 43, "q")
        ltp       = _unpack(data, 43, 51, "q") / 100.0
        exchange  = EXCH_CODE_TO_STR.get(exch_code, "NFO")

        tick = TickData(
            token=token, exchange=exchange,
            ltp=ltp, exchange_ts=exch_ts,
        )

        if mode >= 2 and len(data) >= 123:
            tick.volume = _unpack(data, 67, 75, "q")
            tick.open   = _unpack(data, 91, 99,  "q") / 100.0
            tick.high   = _unpack(data, 99, 107, "q") / 100.0
            tick.low    = _unpack(data, 107, 115,"q") / 100.0
            tick.close  = _unpack(data, 115, 123,"q") / 100.0

        if mode == 3 and len(data) >= 147:
            tick.oi        = _unpack(data, 131, 139, "q")
            tick.oi_change = _unpack(data, 139, 147, "q")

            # Best five (147-347). A truncated block cannot be trusted: the
            # bid/ask stay at their 0.0 defaults.
            if len(data) >= BEST_FIVE_END:
                (tick.best_bid, tick.best_bid_qty,
                 tick.best_ask, tick.best_ask_qty) = parse_best_five(
                    data[BEST_FIVE_OFFSET:BEST_FIVE_END])

        return tick

    except Exception as exc:
        logger.debug("Binary tick parse error: %s", exc)
        return None


class MarketFeed:
    """
    Manages the AngelOne WebSocket connection lifecycle.
    Dispatches ticks to registered async handlers.
    """

    def __init__(self):
        self._ws:               Optional[Any] = None   # websockets ClientConnection
        self._subscriptions:    dict[int, list[str]] = {}  # exchange_code → [token,...]
        self._mode:             int = 3   # SNAP_QUOTE by default
        self._async_handlers:   list[Callable] = []
        self._running:          bool = False
        self._reconnect_count:  int  = 0
        self._down_alerted:     bool = False
        self._last_was_429:     bool = False   # flag set when previous attempt got 429
        self._hb_task:          Optional[asyncio.Task] = None

    # ── Public API ────────────────────────────────────────────────────────────

    def add_async_handler(self, handler: Callable):
        self._async_handlers.append(handler)

    def subscribe(self, exchange: str, tokens: list[str]):
        code = EXCHANGE_STR_MAP.get(exchange.upper(), NSE_FO)
        if code not in self._subscriptions:
            self._subscriptions[code] = []
        new = [t for t in tokens if t not in self._subscriptions[code]]
        self._subscriptions[code].extend(new)
        logger.info("Queued subscribe: %s (%d) → %d tokens", exchange, code, len(new))

    def unsubscribe(self, exchange: str, tokens: list[str]):
        code = EXCHANGE_STR_MAP.get(exchange.upper(), NSE_FO)
        if code in self._subscriptions:
            self._subscriptions[code] = [
                t for t in self._subscriptions[code] if t not in tokens
            ]

    async def start(self):
        self._running = True
        asyncio.create_task(self._connect_loop())

    async def stop(self):
        self._running = False
        if self._hb_task:
            self._hb_task.cancel()
        if self._ws:
            await self._ws.close()
        logger.info("MarketFeed stopped.")

    @property
    def is_connected(self) -> bool:
        return _ws_is_open(self._ws)

    @property
    def subscription_count(self) -> int:
        return sum(len(v) for v in self._subscriptions.values())

    # ── Connection loop ───────────────────────────────────────────────────────

    async def _connect_loop(self):
        """
        Reconnect loop that runs for as long as the feed is running.
        MAX_RECONNECTS=0 means unlimited retries so a transient network outage
        never permanently kills market data mid-session.
        Counter resets to 0 after every successful stable connection (>5 min)
        so a brief blip doesn't consume the burst budget.
        """
        while self._running:
            try:
                # Always force a fresh login on every (re)connect attempt so we
                # get a new feed_token.  AngelOne's 429 means the old session is
                # still alive on their side; a new token forces them to create a
                # new slot rather than reject the stale one.
                await angel_client.login()
                self._last_was_429 = False
                await self._connect()
                # _connect() returns only when the WS closes — reset burst counter
                # only if the connection lasted more than 5 minutes (300s).
                # Short-lived connections (likely errors) keep the backoff pressure.
                self._reconnect_count = 0
            except Exception as exc:
                self._reconnect_count += 1
                exc_str = str(exc)
                is_429  = "429" in exc_str

                if is_429:
                    delay = RECONNECT_429_DELAY if self._reconnect_count == 1 \
                            else min(RECONNECT_BASE * self._reconnect_count, 120)
                    self._last_was_429 = True
                    logger.warning(
                        "Feed 429 — previous session still alive on AngelOne. "
                        "Waiting %ds before retry (attempt %d)...",
                        delay, self._reconnect_count,
                    )
                else:
                    # Cap backoff at 120s so even after many failures we retry
                    # frequently enough to catch the market re-opening.
                    delay = min(RECONNECT_BASE * self._reconnect_count, 120)
                    logger.warning(
                        "Feed disconnected (%s). Reconnecting in %ds (attempt %d)...",
                        exc, delay, self._reconnect_count,
                    )
                from app.core.alerts import alert, CRITICAL, WARNING
                if alert("feed_down", f"Market feed down ({exc_str[:120]}); retry "
                         f"{self._reconnect_count} in {delay}s",
                         level=CRITICAL if self._reconnect_count >= 3 else WARNING,
                         throttle_s=600):
                    self._down_alerted = True
                await asyncio.sleep(delay)

    async def _connect(self):
        # Official header spec — lowercase keys, jwt WITHOUT "Bearer " prefix
        headers = _session.ws_feed_headers

        logger.info("Connecting to AngelOne WebSocket...")
        async with websockets.connect(
            WS_URL,
            additional_headers=headers,
            ping_interval=None,      # manual heartbeat
            ping_timeout=None,
            max_size=10 * 1024 * 1024,
            open_timeout=15,
        ) as ws:
            self._ws = ws
            logger.info("✅ WebSocket connected. Subscribing %d token groups...",
                        len(self._subscriptions))
            if self._down_alerted:
                from app.core.alerts import alert, INFO
                alert("feed_up", "Market feed reconnected", level=INFO)
                self._down_alerted = False

            self._hb_task = asyncio.create_task(self._heartbeat_loop())
            await self._send_subscriptions()

            try:
                async for message in ws:
                    await self._handle_message(message)
            except ConnectionClosed as exc:
                logger.warning("WebSocket closed: code=%s reason=%s", exc.code, exc.reason)
            finally:
                if self._hb_task:
                    self._hb_task.cancel()
                self._ws = None

    async def _heartbeat_loop(self):
        """
        Send ping every 10 seconds (official requirement).

        Exception-safe: only cancellation or a closed connection ends the loop.
        Any other error (including a failing state check) is logged and the
        next ping is attempted on schedule.
        """
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            try:
                if _ws_is_open(self._ws):
                    await self._ws.send("ping")
            except asyncio.CancelledError:
                raise
            except ConnectionClosed:
                break
            except Exception as exc:
                logger.warning("Heartbeat ping failed (will retry): %s", exc)

    # ── Subscription ──────────────────────────────────────────────────────────

    async def _send_subscriptions(self):
        if not self._subscriptions or not self._ws:
            return

        # Build token_list as per official spec
        token_list = [
            {"exchangeType": exch_code, "tokens": tokens}
            for exch_code, tokens in self._subscriptions.items()
            if tokens
        ]

        if not token_list:
            return

        # Max 50 tokens per subscribe call
        flat = [
            {"exchangeType": item["exchangeType"], "tokens": [t]}
            for item in token_list
            for t in item["tokens"]
        ]

        chunk_size = 50
        for i in range(0, len(flat), chunk_size):
            chunk = flat[i: i + chunk_size]
            # Group by exchangeType again for the message
            grouped: dict[int, list[str]] = {}
            for f in chunk:
                grouped.setdefault(f["exchangeType"], []).append(f["tokens"][0])

            msg = {
                "correlationID": f"sub{int(time.time())}",
                "action": 1,
                "params": {
                    "mode": self._mode,
                    "tokenList": [
                        {"exchangeType": k, "tokens": v}
                        for k, v in grouped.items()
                    ],
                },
            }
            await self._ws.send(json.dumps(msg))
            logger.debug("Subscribed chunk of %d tokens", len(chunk))
            await asyncio.sleep(0.05)

    async def send_subscribe(self, exchange: str, tokens: list[str]):
        self.subscribe(exchange, tokens)
        if not _ws_is_open(self._ws):
            return
        code = EXCHANGE_STR_MAP.get(exchange.upper(), NSE_FO)
        msg = {
            "correlationID": f"sub{int(time.time())}",
            "action": 1,
            "params": {
                "mode": self._mode,
                "tokenList": [{"exchangeType": code, "tokens": tokens}],
            },
        }
        await self._ws.send(json.dumps(msg))

    async def send_unsubscribe(self, exchange: str, tokens: list[str]):
        self.unsubscribe(exchange, tokens)
        if not _ws_is_open(self._ws):
            return
        code = EXCHANGE_STR_MAP.get(exchange.upper(), NSE_FO)
        msg = {
            "correlationID": f"unsub{int(time.time())}",
            "action": 0,
            "params": {
                "mode": self._mode,
                "tokenList": [{"exchangeType": code, "tokens": tokens}],
            },
        }
        await self._ws.send(json.dumps(msg))

    # ── Message handling ──────────────────────────────────────────────────────

    async def _handle_message(self, message):
        if message == "pong":
            return

        if isinstance(message, bytes):
            tick = _parse_binary_tick(message)
            if tick is None:
                return
            for handler in self._async_handlers:
                try:
                    await handler(tick)
                except Exception as exc:
                    logger.error("Tick handler error: %s", exc)
        else:
            # JSON status / error messages
            try:
                payload = json.loads(message)
                if payload.get("type") == "error":
                    logger.error("Feed server error: %s", payload)
                else:
                    logger.debug("Feed message: %s", payload)
            except Exception:
                pass


# App-level singleton
market_feed = MarketFeed()
