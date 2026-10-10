"""The PROCESS exits non-zero when the paper guard fails (review R7, design §6.6).

tests/test_lifespan_guard.py proves the lifespan raises; this proves the real
uvicorn process built on it refuses to serve and exits non-zero (uvicorn
0.30.x: STARTUP_FAILURE = 3), and that a valid row starts and exits 0.
The child is tests/startup_exit_app.py over a throwaway SQLite file."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
ISOLATION = {
    "POSTGRES_HOST": "127.0.0.1", "POSTGRES_PORT": "1", "POSTGRES_DB": "pytest_blocked",
    "REDIS_HOST": "127.0.0.1", "REDIS_PORT": "1", "REDIS_DB": "15",
    "ANGEL_API_KEY": "", "ANGEL_CLIENT_ID": "", "ANGEL_PASSWORD": "", "ANGEL_TOTP_SECRET": "",
}


def _run(tmp_path, scenario):
    env = dict(os.environ, **ISOLATION)
    proc = subprocess.run(
        [sys.executable, "-m", "tests.startup_exit_app", "--db", str(tmp_path / "s.db"),
         "--scenario", scenario],
        cwd=str(BACKEND), env=env, capture_output=True, text=True, timeout=90)
    return proc.returncode, proc.stdout + proc.stderr


@pytest.mark.parametrize("scenario, key", [
    ("live_mode", "mode"),
    ("live_confirmed", "live_mode_confirmed"),
    ("bad_params", "max_lots_per_trade"),
])
def test_a_failed_startup_check_exits_the_process_non_zero(tmp_path, scenario, key):
    code, out = _run(tmp_path, scenario)
    assert code != 0, out
    assert code == 3, out                       # uvicorn STARTUP_FAILURE
    assert "Startup assertions failed" in out
    assert key in out
    assert "Uvicorn running on" not in out


def test_a_valid_row_starts_and_exits_zero(tmp_path):
    code, out = _run(tmp_path, "ok")
    assert code == 0, out
    assert "Application startup complete" in out
    assert "Startup assertions failed" not in out
