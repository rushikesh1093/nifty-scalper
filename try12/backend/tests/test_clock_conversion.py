"""
Clock conversion completeness (design §14.2, §23 review finding 2).

An enumerated list of call sites is a plan; this scan is the fact. Every
decision-path module reads time through the ctx clock, so a replay is measured
against the replayed day and never against the wall clock.

Widening ALLOWLIST is a visible diff in this file — the same posture the
paper-only guard takes for ALLOWED_ORDER_PATHS.
"""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]

FORBIDDEN = (r"datetime\.now\(", r"date\.today\(", r"datetime\.utcnow\(", r"time\.time\(")
SCOPE = ("app/services/trading/", "app/strategies/", "app/services/metrics/",
         "app/services/replay/", "app/services/broker/instrument_sync.py")
ALLOWLIST = {
    "app/services/trading/regime_config.py",   # TTL-cache age; live-only (design §14.2 table)
    "app/runtime/live.py",                      # LiveClock itself
    "app/services/replay/ports_impl.py",        # ReplayClock construction
}
_PATTERN = re.compile("|".join(FORBIDDEN))


def _scope_files(root: Path):
    for entry in SCOPE:
        p = root / entry
        if p.is_file():
            yield p
        elif p.is_dir():
            yield from sorted(p.rglob("*.py"))


def _hits(root: Path) -> list[str]:
    out = []
    for p in _scope_files(root):
        rel = p.relative_to(root).as_posix()
        if rel in ALLOWLIST:
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if _PATTERN.search(line):
                out.append(f"{rel}:{n}: {line.strip()}")
    return out


def test_no_wall_clock_read_in_decision_code():
    hits = _hits(BACKEND)
    assert not hits, "wall-clock reads outside the ctx clock:\n" + "\n".join(hits)


def test_scan_fails_on_a_synthetic_module(tmp_path):
    mod = tmp_path / "app" / "services" / "trading" / "synthetic.py"
    mod.parent.mkdir(parents=True)
    mod.write_text("from datetime import datetime, timezone\n"
                   "def f():\n    return datetime.now(timezone.utc)\n", encoding="utf-8")
    assert _hits(tmp_path) == ["app/services/trading/synthetic.py:3: return datetime.now(timezone.utc)"]


def test_tick_age_follows_the_replay_clock(runtime_ctx):
    """Under a clock set to a 2026-09-01 session time, a 2026-09-01 tick is fresh.
    With the old wall-clock default this age was months, every quote was stale,
    and a replay would silently produce zero trades."""
    from app.services.trading.paper_engine import (
        STALE_TICK_SECONDS, _quote_valid, _tick_age_seconds,
    )
    session_time = datetime(2026, 9, 1, 5, 0, 0, tzinfo=timezone.utc)      # 10:30 IST
    runtime_ctx.clock.set(session_time)
    tick = {"ts": (session_time - timedelta(seconds=5)).replace(tzinfo=None).isoformat(),
            "best_bid": 99.5, "best_ask": 100.0}
    age = _tick_age_seconds(tick)
    assert age == pytest.approx(5.0)
    assert age < STALE_TICK_SECONDS
    assert _quote_valid(tick["best_bid"], tick["best_ask"], age) is True


def test_tick_age_has_no_now_parameter():
    import inspect
    from app.services.trading.paper_engine import _tick_age_seconds
    assert list(inspect.signature(_tick_age_seconds).parameters) == ["tick"]


def test_signal_time_and_aggregator_use_ctx_clock(runtime_ctx):
    from app.strategies.base import Direction, SignalAggregator
    from app.strategies.strategies import ORBVWAPStrategy

    sig = ORBVWAPStrategy("NIFTY")._make_signal(Direction.BUY, 22600.0, {"vwap": 22590.0})
    assert sig.signal_time == runtime_ctx.clock.now_utc()

    agg = SignalAggregator()
    agg.record_acted(sig)
    assert agg.should_act(sig)[0] is False
    runtime_ctx.clock.advance(SignalAggregator.SAME_DIR_COOLDOWN_SECS + 1)
    assert agg.should_act(sig)[0] is True
