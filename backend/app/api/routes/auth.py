from fastapi import APIRouter, HTTPException
from app.schemas.auth import LoginRequest, LoginResponse, SessionStatusResponse
from app.services.broker.angel_client import angel_client, _session
from app.services.market_data.feed import market_feed

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
async def login(req: LoginRequest):
    """
    Login to AngelOne using provided credentials.
    In production these come from .env; this endpoint is useful for
    runtime credential override / re-login.

    Note: credentials are passed directly to angel_client.login() rather
    than mutating the global settings singleton, which is not thread-safe
    and would permanently override .env values for all subsequent operations.
    """
    try:
        session = await angel_client.login(
            api_key=req.api_key,
            client_id=req.client_id,
            password=req.password,
            totp_secret=req.totp_secret,
        )
        return LoginResponse(
            status=True,
            message="Login successful",
            client_code=session.client_code,
            feed_token=session.feed_token,
            logged_in_at=session.logged_in_at.isoformat() if session.logged_in_at else None,
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail=str(exc))


@router.get("/status", response_model=SessionStatusResponse)
async def session_status():
    """Check current AngelOne session status and feed connectivity."""
    return SessionStatusResponse(
        is_logged_in=_session.is_valid(),
        client_code=_session.client_code,
        logged_in_at=_session.logged_in_at.isoformat() if _session.logged_in_at else None,
        feed_connected=market_feed.is_connected,
        subscriptions=market_feed.subscription_count,
    )


@router.post("/logout")
async def logout():
    await angel_client.logout()
    return {"status": True, "message": "Logged out"}


@router.post("/refresh")
async def refresh_token():
    """
    Refresh AngelOne session tokens and reconnect the WebSocket feed.
    Called by the frontend 'Reconnect' button when the session appears stale.
    """
    try:
        session = await angel_client.refresh_tokens()
        # Persist the fresh session so /auth/status immediately returns is_logged_in=True
        await angel_client._persist_to_redis()
    except Exception as exc:
        # refresh_tokens falls back to full login on failure — if even that fails, propagate
        raise HTTPException(status_code=400, detail=f"Token refresh failed: {exc}")

    # Reconnect the feed with the new feed_token (non-blocking)
    from app.services.market_data.feed import market_feed
    import asyncio
    if not market_feed.is_connected:
        asyncio.create_task(market_feed.start())

    return {
        "status": True,
        "is_logged_in": session.is_valid(),
        "client_code": session.client_code,
        "logged_in_at": session.logged_in_at.isoformat() if session.logged_in_at else None,
        "feed_connected": market_feed.is_connected,
    }
