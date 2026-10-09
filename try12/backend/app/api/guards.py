"""
Request guards shared by the routers (design §17.2, §17.4, §21).

* ``run_id`` — every query against a run-shared table is scoped by it; the
  query parameter defaults to the live paper run and must match
  ``^(PAPER|RPL-[A-Za-z0-9]{1,26})$`` (HTTP 400 otherwise).
* ``X-API-Key`` — the asymmetric posture of §17.4: endpoints that only STOP
  trading accept a request when no key is configured; endpoints that resume
  trading or destroy data return 503 until ``ADMIN_API_KEY`` is set. The
  comparison is constant-time and the key is never logged or returned.
"""
from __future__ import annotations

import hmac
import re
from datetime import date
from typing import Optional

from fastapi import Header, HTTPException, Query

from app.core.config import settings

LIVE_RUN_ID = "PAPER"
RUN_ID_PATTERN = re.compile(r"^(PAPER|RPL-[A-Za-z0-9]{1,26})$")
MAX_RANGE_DAYS = 366


def validate_run_id(value: Optional[str]) -> str:
    if value is None or not RUN_ID_PATTERN.match(value):
        raise HTTPException(status_code=400,
                            detail=f"run_id must match {RUN_ID_PATTERN.pattern}")
    return value


def run_id_query(run_id: str = Query(LIVE_RUN_ID, description="PAPER or RPL-<id>")) -> str:
    """FastAPI dependency: the validated run_id query parameter (default PAPER)."""
    return validate_run_id(run_id)


def date_range(date_from: Optional[str], date_to: Optional[str]) -> tuple[Optional[date], Optional[date]]:
    """Parse ISO dates; 400 on a bad value, an inverted range or > 366 days (§21)."""
    out = []
    for name, v in (("date_from", date_from), ("date_to", date_to)):
        if v is None:
            out.append(None)
            continue
        try:
            out.append(date.fromisoformat(v))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"{name} must be an ISO date (YYYY-MM-DD)")
    d_from, d_to = out
    if d_from and d_to:
        if d_from > d_to:
            raise HTTPException(status_code=400, detail="date_from must be <= date_to")
        if (d_to - d_from).days > MAX_RANGE_DAYS:
            raise HTTPException(status_code=400,
                                detail=f"date range must be <= {MAX_RANGE_DAYS} days")
    return d_from, d_to


def admin_key_configured() -> bool:
    return bool(settings.ADMIN_API_KEY)


def _key_matches(supplied: Optional[str]) -> bool:
    expected = settings.ADMIN_API_KEY or ""
    return bool(supplied) and hmac.compare_digest(supplied.encode(), expected.encode())


def require_admin_key(x_api_key: Optional[str] = Header(None, alias="X-API-Key")) -> str:
    """Resume/destroy endpoints: 503 when no key is configured, 401 on a bad key.
    Returns the configured label (never the key) for audit columns."""
    if not admin_key_configured():
        raise HTTPException(status_code=503, detail="admin key not configured")
    if not _key_matches(x_api_key):
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")
    return settings.ADMIN_API_KEY_LABEL


def optional_admin_key(x_api_key: Optional[str] = Header(None, alias="X-API-Key")) -> str:
    """Stop-only endpoints (engage a halt): always allowed when no key is configured;
    when one is, it must be supplied. Never 503."""
    if admin_key_configured() and not _key_matches(x_api_key):
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")
    return settings.ADMIN_API_KEY_LABEL if admin_key_configured() else "unauthenticated"


ADMIN_ONLY_ENDPOINTS = (
    "POST /api/v1/trading/halt/release",
    "POST /api/v1/trading/kill-switch/release",
    "POST /api/v1/trading/capital/reset-daily",
    "POST /api/v1/trading/clear-test-trades",
    "POST /api/v1/analytics/reconcile",
)
