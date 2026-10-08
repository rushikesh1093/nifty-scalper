"""
Startup assertions (design §6.3, §6.6, §13).

Called from BOTH entry points — the ``app.main`` lifespan and (later) the
replay engine — at the positions fixed in §6.6, and from neither inside a
broad exception handler. Each check collects every violation before raising,
so one restart surfaces every problem.

Implemented so far: check 1 (paper guard) and check 6 (params). Checks 2-5
land with the regime-map rewrite and check 7 with the MIN_RR deletion; they
are appended to ``run_startup_checks`` in that order.
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from app.db.models.paper_trade import TradingMode

logger = logging.getLogger(__name__)


class StartupAssertionError(Exception):
    """A startup invariant failed. Propagates out of the lifespan; uvicorn exits."""

    def __init__(self, violations):
        if isinstance(violations, str):
            violations = [violations]
        self.violations: list[str] = list(violations)
        super().__init__("; ".join(self.violations))


async def assert_paper_mode(session_factory, *, allow_absent: bool) -> list[str]:
    """Check 1's DB half: raw-scalar read of trading_config.mode / live_mode_confirmed.

    Returns violations (empty = pass). Takes the session factory explicitly so it
    can run BEFORE any RuntimeContext is bound and before any ORM read of
    trading_config.

    This is deliberately a ``text()`` read, not an ORM select. ``mode`` is a
    SAEnum column: the ORM hands back an already-coerced TradingMode, and a
    malformed stored value raises LookupError during hydration, before any check
    can name the key. The point of this check is to observe what the column
    *holds*, which an enum-coercing read cannot do. Do not "clean this up".
    Both column names are literals, not interpolated values.
    """
    violations: list[str] = []
    async with session_factory() as db:          # fresh session, no identity-map reuse
        row = (await db.execute(text(
            "SELECT mode, live_mode_confirmed FROM trading_config WHERE id = 1"
        ))).one_or_none()
    if row is None:
        if not allow_absent:
            violations.append("trading_config row absent at assertion time")
    else:
        raw_mode, live_confirmed = row
        try:
            mode = TradingMode(raw_mode)         # raw string -> ValueError is reachable here
        except ValueError:
            violations.append(f"mode column unparseable: {raw_mode!r}")  # fail closed
        else:
            if mode is not TradingMode.PAPER:
                violations.append(f"mode is {mode.value}, expected PAPER")
        if live_confirmed:
            violations.append("live_mode_confirmed is True")
    for v in violations:
        logger.error("Startup paper guard: %s", v)
    return violations


def _check_params() -> list[str]:
    """Check 6: the resolved TradingParams validate."""
    from app.core.trading_params import TradingParams
    return [f"params: {v}" for v in TradingParams().validate()]


async def run_startup_checks(*, require_config_row: bool) -> None:
    """Run every implemented startup check; raise StartupAssertionError listing
    every violation. Never swallows.

    ``require_config_row`` is True for live (ensure_config has just created the
    row) and False for replay (a replay database may have no row). It is a
    parameter, not an is_replay branch.
    """
    from app.db import base as db_base

    violations: list[str] = []
    # 1. paper guard, DB half
    violations += await assert_paper_mode(db_base.AsyncSessionLocal,
                                          allow_absent=not require_config_row)
    # 6. params
    params_v = _check_params()
    for v in params_v:
        logger.error("Startup check: %s", v)
    violations += params_v

    if violations:
        raise StartupAssertionError(violations)
    logger.info("Startup checks passed (paper mode confirmed).")
