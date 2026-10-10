"""
Startup assertions (design §6.3, §6.6, §13).

Called from BOTH entry points — the ``app.main`` lifespan and (later) the
replay engine — at the positions fixed in §6.6, and from neither inside a
broad exception handler. Each check collects every violation before raising,
so one restart surfaces every problem.

Checks: 1 (paper guard), 2-5 (strategy names, regime coverage, multipliers,
regime keys and the UNKNOWN scan), 6 (params) and 7 (the 1:2 floor has exactly
one source, ``params().min_rr``).
"""
from __future__ import annotations

import ast
import logging
import re
from pathlib import Path
from typing import Optional

from sqlalchemy import text

from app.db.models.paper_trade import TradingMode
from app.runtime.context import ctx, params

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


# ── checks 2-5: names, coverage, multipliers, regime keys (design §6.3) ─────

EXPECTED_REGIMES: frozenset[str] = frozenset({
    "BULL_TREND_NORMAL_VOL", "BEAR_TREND_NORMAL_VOL", "TREND_HIGH_VOL",
    "BREAKOUT_EVENT_BULLISH", "BREAKOUT_EVENT_BEARISH", "LOW_VOL_COMPRESSION",
    "RANGE_NORMAL_VOL", "HIGH_VOL_CHOP", "REGIME_TRANSITION", "EXPIRY_DAY",
    "UNCLASSIFIED", "DETECTOR_ERROR", "UNKNOWN",
})

# Verified non-emitting uses of the literal "UNKNOWN" as a regime value
REGIME_UNKNOWN_ALLOWED: frozenset[str] = frozenset({
    "app/services/trading/regime_rules.py",      # the legacy read-only rule key
    "app/services/metrics/trade_metrics.py",     # reads historical regime_states rows
})

_APP_DIR = Path(__file__).resolve().parents[2]          # .../backend/app
_REGIME_IDENT = re.compile(r"(?i)regime")


def check_regime_map() -> list[str]:
    """Checks 2-5 over the live REGIME_RULES and the built strategies."""
    from app.services.trading import regime_rules
    from app.strategies.names import ALL_STRATEGY_NAMES
    from app.strategies.strategies import build_strategies

    rules = regime_rules.REGIME_RULES
    names = set(ALL_STRATEGY_NAMES)
    v: list[str] = []

    # 2. name resolution (R3.3)
    built = {s.name for s in build_strategies("NIFTY")}
    if built != names:
        v.append(f"check 2: built strategies {sorted(built)} != canonical {sorted(names)}")
    for regime, rule in rules.items():
        unknown = set(rule.enabled_strategies) - names
        if unknown:
            v.append(f"check 2: regime {regime} enables unknown strategy {sorted(unknown)}")

    # 3. coverage (R3.4)
    union = set().union(*(r.enabled_strategies for r in rules.values())) if rules else set()
    missing = names - (union | set(regime_rules.NEVER_ENABLED))
    if missing:
        v.append(f"check 3: strategies never enabled by any regime and not declared "
                 f"in NEVER_ENABLED: {sorted(missing)}")

    # 4. multipliers (R18.3), including the dynamic ATR adjustment
    allowed = regime_rules.POSITION_MULTIPLIERS
    for regime in rules:
        for atr_pct in (0.0, 50.0, 70.0, 70.01, 100.0):
            m = regime_rules.get_regime_rule(regime, atr_pct).position_multiplier
            if m not in allowed:
                v.append(f"check 4: regime {regime} multiplier {m} at ATR%ile {atr_pct} "
                         f"not in {sorted(allowed)}")
                break

    # 5. regime key set, and nothing emits the legacy UNKNOWN
    if set(rules) != EXPECTED_REGIMES:
        v.append(f"check 5: regime keys {sorted(rules)} != expected {sorted(EXPECTED_REGIMES)}")
    v += [f"check 5: regime-valued UNKNOWN at {hit}" for hit in scan_regime_unknown()]
    return v


def _yields_unknown(expr) -> bool:
    """*expr* can evaluate to the literal "UNKNOWN" directly (constant,
    conditional branch, `or` default, or a call's literal argument such as
    dict.get(key, "UNKNOWN")). Dict literals are not regime assignments."""
    if isinstance(expr, ast.Constant):
        return expr.value == "UNKNOWN"
    if isinstance(expr, ast.IfExp):
        return _yields_unknown(expr.body) or _yields_unknown(expr.orelse)
    if isinstance(expr, ast.BoolOp):
        return any(_yields_unknown(x) for x in expr.values)
    if isinstance(expr, ast.Call):
        return any(isinstance(a, ast.Constant) and a.value == "UNKNOWN"
                   for a in list(expr.args) + [k.value for k in expr.keywords])
    return False


def _ident(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_regime(name: str) -> bool:
    return bool(name) and bool(_REGIME_IDENT.search(name))


def _unknown_hits(tree) -> list[int]:
    hits: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if node.value is not None and any(_is_regime(_ident(t)) for t in targets) \
                    and _yields_unknown(node.value):
                hits.append(node.lineno)
        elif isinstance(node, ast.keyword):
            if _is_regime(node.arg or "") and _yields_unknown(node.value):
                hits.append(node.value.lineno)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            positional = a.posonlyargs + a.args
            pairs = list(zip(positional[len(positional) - len(a.defaults):], a.defaults))
            pairs += [(arg, d) for arg, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None]
            for arg, default in pairs:
                if _is_regime(arg.arg) and _yields_unknown(default):
                    hits.append(default.lineno)
            if _is_regime(node.name):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Return) and sub.value is not None \
                            and _yields_unknown(sub.value):
                        hits.append(sub.lineno)
        elif isinstance(node, ast.Compare):
            operands = [node.left] + list(node.comparators)
            if any(_is_regime(_ident(o)) for o in operands) and \
                    any(isinstance(o, ast.Constant) and o.value == "UNKNOWN" for o in operands):
                hits.append(node.lineno)
    return sorted(set(hits))


def scan_regime_unknown(root: Optional[Path] = None,
                        allowed: frozenset[str] = REGIME_UNKNOWN_ALLOWED) -> list[str]:
    """Check 5's static half: no regime-valued expression outside *allowed*
    resolves to "UNKNOWN". Paths in *allowed* are relative to root's parent
    (``app/...``). Returns "path:line" hits; empty = pass."""
    root = root or _APP_DIR
    out: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root.parent).as_posix()
        if rel in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        out += [f"{rel}:{ln}" for ln in _unknown_hits(tree)]
    return out


def _check_params() -> list[str]:
    """Check 6: the resolved TradingParams (through the bound ParamsPort) validate."""
    return [f"params: {v}" for v in params().validate()]


# Operator-facing fix per check-6 key (the trading_config column of the same name).
PARAM_FIXES: dict[str, str] = {
    "max_lots_per_trade": "set >= 2 (partial exit needs 2 lots)",
    "trade_amount": "set NULL after migration 0013, or within ₹500 of fixed_fraction_pct × capital",
    "capital": "set > 0",
    "max_daily_loss": "set within (0, capital]",
    "max_daily_loss_pct": "set within (0, 100]",
    "fixed_fraction_pct": "set within (0, 100] and <= kelly_fraction_cap_pct",
    "kelly_fraction_cap_pct": "set within [fixed_fraction_pct, 25]",
    "kelly_multiplier": "set within (0, 0.5]",
    "min_rr": "set >= 2.0",
}
GENERIC_PARAM_FIX = "correct it as the violation above states"
RECONCILE_PREVIEW = "python scripts/reconcile_live_config.py --database-url <url>"
_VIOLATION_KEY = re.compile(r"^params: ([A-Za-z_][A-Za-z0-9_]*)(?:\[[^\]]*\])?(?:=(\S+))?")


def param_fix_line(params_v: list[str]) -> str:
    """One operator line naming each trading_config field to fix, built from the
    leading key of each check-6 violation."""
    parts: list[str] = []
    seen: set[str] = set()
    for v in params_v:
        m = _VIOLATION_KEY.match(v)
        if not m or m.group(1) in seen:
            continue
        key, current = m.group(1), m.group(2)
        seen.add(key)
        fix = PARAM_FIXES.get(key, GENERIC_PARAM_FIX)
        parts.append(f"{key} ({current if current is not None else '?'} -> {fix})")
    return f"fix trading_config: {'; '.join(parts)}; preview with: {RECONCILE_PREVIEW}"


MIN_RR_LITERAL = re.compile(r"\b(MIN_RR|HF_MIN_RR)\s*=\s*[\d.]+")


def scan_min_rr_literals(root: Optional[Path] = None) -> list[str]:
    """Check 7's static half: no numeric assignment to a MIN_RR / HF_MIN_RR name
    anywhere under app/. Returns "path:line" hits; empty = pass."""
    root = root or _APP_DIR
    out: list[str] = []
    for path in sorted(root.rglob("*.py")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if MIN_RR_LITERAL.search(line):
                out.append(f"{path.relative_to(root.parent).as_posix()}:{n}")
    return out


def check_min_rr_single_source() -> list[str]:
    """Check 7. A module constant bound at import cannot track an operator edit of
    the trading_config column, so there are no constants: the floor is read from
    ``params().min_rr`` at the point of use. This is a literal scan rather than an
    identity comparison for that reason (design §6.3 check 7)."""
    from app.strategies.strategies import build_strategies

    v: list[str] = []
    if params().min_rr < 2.0:
        v.append(f"check 7: params().min_rr={params().min_rr} must be >= 2.0")
    v += [f"check 7: MIN_RR literal at {hit}" for hit in scan_min_rr_literals()]
    for s in build_strategies("NIFTY"):
        if hasattr(type(s), "min_rr"):
            v.append(f"check 7: strategy {s.name} declares a min_rr attribute")
    return v


async def run_startup_checks(*, require_config_row: bool) -> None:
    """Run every implemented startup check against the bound ctx(); raise
    StartupAssertionError listing every violation. Never swallows.

    ``require_config_row`` is True for live (ensure_config has just created the
    row) and False for replay (a replay database may have no row). It is a
    parameter, not an is_replay branch.
    """
    violations: list[str] = []
    # 1. paper guard — DB half (raw read, its own session) ...
    violations += await assert_paper_mode(ctx().sessions,
                                          allow_absent=not require_config_row)
    # ... and the in-process half: the frozen RunConfig.mode gate check 1 reads
    if ctx().cfg.mode != TradingMode.PAPER.value:
        v = f"ctx().cfg.mode is {ctx().cfg.mode!r}, expected 'PAPER'"
        logger.error("Startup paper guard: %s", v)
        violations.append(v)
    # 2-5. strategy names, regime coverage, multipliers, regime keys / no UNKNOWN
    map_v = check_regime_map()
    for v in map_v:
        logger.error("Startup check: %s", v)
    violations += map_v
    # 6. params
    params_v = _check_params()
    for v in params_v:
        logger.error("Startup check: %s", v)
    violations += params_v
    # 7. the 1:2 floor has exactly one source
    rr_v = check_min_rr_single_source()
    for v in rr_v:
        logger.error("Startup check: %s", v)
    violations += rr_v
    # operator line for check 6: which trading_config fields, and how to preview the fix
    if params_v:
        fix = param_fix_line(params_v)
        logger.critical("Startup check: %s", fix)
        violations.append(fix)

    if violations:
        raise StartupAssertionError(violations)
    logger.info("Startup checks passed (paper mode confirmed).")
