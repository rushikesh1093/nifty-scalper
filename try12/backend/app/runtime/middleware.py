"""
RuntimeContextMiddleware (design §6.6, R5 finding 1).

uvicorn runs the lifespan in its own task, so its ``bind()`` is invisible to
request tasks. This pure ASGI middleware binds ``app.state.runtime_context``
— the same object the loops inherited — for every http/websocket scope.
BaseHTTPMiddleware is not used: a pure middleware wraps the whole call, so
streaming bodies, background tasks and websockets run inside the binding.

Registered BEFORE CORSMiddleware; Starlette makes the last-added middleware
outermost, so CORS wraps this one and a 503 still carries CORS headers.
"""
from __future__ import annotations

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.runtime.context import is_bound, using


class RuntimeContextMiddleware:
    """Binds app.state.runtime_context for the duration of each http/websocket scope."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)        # "lifespan" passes through untouched
        rc = getattr(scope["app"].state, "runtime_context", None)   # Starlette.__call__ sets scope["app"]
        if rc is None:
            if is_bound():                                     # a test fixture bound one in this task
                return await self.app(scope, receive, send)
            if scope["type"] == "http":
                return await JSONResponse({"detail": "runtime context unavailable"},
                                          status_code=503)(scope, receive, send)
            return await send({"type": "websocket.close", "code": 1013})   # "try again later"
        with using(rc):
            return await self.app(scope, receive, send)
