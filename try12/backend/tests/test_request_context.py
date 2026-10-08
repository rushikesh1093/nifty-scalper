"""
Requests see the live context (design §6.6, §23 R5 finding 1, cases a-e).

uvicorn runs the lifespan in its own task, so the lifespan's bind() is
invisible to request tasks. A direct-call test cannot detect that, so case (a)
reproduces the topology: the lifespan runs in a separate task (create_task
copies the context, exactly as under uvicorn) while requests are made from the
test task. Case (b) is the negative control proving the test detects the
defect.
"""
import asyncio

import httpx
import pytest
from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.runtime.middleware import RuntimeContextMiddleware
from tests.helpers import add_position, get_position, run, seed_config


async def _hold(cm, started: asyncio.Event, stop: asyncio.Event):
    async with cm:
        started.set()
        await stop.wait()


def _client(app, **kw):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, **kw), base_url="http://test")


async def _with_lifespan_in_other_task(app, body):
    """Run app's lifespan in a separate task, then run `body(client)` from this task."""
    from app.main import lifespan
    started, stop = asyncio.Event(), asyncio.Event()
    task = asyncio.create_task(_hold(lifespan(app), started, stop))
    await asyncio.wait_for(started.wait(), 10)
    try:
        return await body()
    finally:
        stop.set()
        await task


def _seed_valid(sqlite_db, fake_redis) -> int:
    run(seed_config(sqlite_db, max_lots_per_trade=2))
    pid = run(add_position(sqlite_db, token="OPT1"))
    fake_redis.put_tick("OPT1", 104.0)
    return pid


# ── (a) live topology: middleware binds app.state.runtime_context ───────────

def test_requests_in_other_tasks_see_the_lifespan_context(no_ctx, sqlite_db, fake_redis, lifespan_spies):
    from app.main import app
    pid = _seed_valid(sqlite_db, fake_redis)

    async def _body():
        async with _client(app) as c:
            cfg = await c.get("/api/v1/trading/config")
            closed = await c.post(f"/api/v1/trading/positions/{pid}/close")
        return cfg, closed

    cfg, closed = run(_with_lifespan_in_other_task(app, _body))
    assert cfg.status_code == 200, cfg.text
    assert cfg.json()["mode"] == "PAPER"
    assert closed.status_code == 200, closed.text
    assert run(get_position(sqlite_db, pid)).is_open is False


# ── (b) negative control: same topology, no middleware -> 500 ────────────────

def test_without_middleware_requests_fail_with_lookup_error(no_ctx, sqlite_db, fake_redis, lifespan_spies):
    from app.api.routes import trading
    from app.main import lifespan
    _seed_valid(sqlite_db, fake_redis)

    bare = FastAPI(lifespan=lifespan)
    bare.include_router(trading.router, prefix="/api/v1")

    async def _body():
        async with _client(bare, raise_app_exceptions=False) as c:
            resp = await c.get("/api/v1/trading/config")
        async with _client(bare) as c:
            with pytest.raises(LookupError):
                await c.get("/api/v1/trading/config")
        return resp

    resp = run(_with_lifespan_in_other_task(bare, _body))
    assert resp.status_code == 500


# ── (c) no lifespan, no fixture binding -> 503 / websocket 1013 ─────────────

def test_unbound_http_is_503(no_ctx):
    from app.main import app

    async def _get():
        async with _client(app) as c:
            return await c.get("/api/v1/trading/config")

    resp = run(_get())
    assert resp.status_code == 503
    assert resp.json() == {"detail": "runtime context unavailable"}


def test_unbound_websocket_is_closed_1013(no_ctx):
    from app.main import app
    client = TestClient(app)          # no `with`: the lifespan does not run
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect("/api/v1/trading/ws/signals"):
            pass
    assert ei.value.code == 1013


# ── (d) a fixture-bound context passes through ───────────────────────────────

def test_fixture_context_passes_through(runtime_ctx, sqlite_db):
    from app.main import app
    run(seed_config(sqlite_db))
    assert getattr(app.state, "runtime_context", None) is None

    async def _get():
        async with _client(app) as c:
            return await c.get("/api/v1/trading/config")

    resp = run(_get())
    assert resp.status_code == 200, resp.text
    assert resp.json()["mode"] == "PAPER"


# ── (e) static: registered, and inside CORS ──────────────────────────────────

def test_middleware_registered_before_cors():
    from app.main import app
    classes = [m.cls for m in app.user_middleware]
    assert RuntimeContextMiddleware in classes and CORSMiddleware in classes
    # add_middleware() inserts at index 0, so the outermost (last-added) comes first:
    # CORS must wrap the context middleware so a 503 still carries CORS headers.
    assert classes.index(CORSMiddleware) < classes.index(RuntimeContextMiddleware)
