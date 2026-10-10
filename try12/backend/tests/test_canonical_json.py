"""canonical_json — the single determinism serialiser (design §14.5, §23 finding 14)."""
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.trading_params import TradingParams
from app.runtime.serialize import canonical_json

BACKEND = Path(__file__).resolve().parents[1]


def _h(obj) -> str:
    return hashlib.sha256(canonical_json(obj).encode()).hexdigest()


def test_float_noise_below_1e6_is_ignored():
    assert canonical_json({"x": 0.1 + 0.2}) == canonical_json({"x": 0.3})
    assert _h({"x": 0.3 + 1e-7}) == _h({"x": 0.3})
    assert _h({"x": 0.3 + 1e-5}) != _h({"x": 0.3})


def test_signed_zero_and_fixed_precision():
    assert canonical_json(-0.0) == "0.000000"
    assert canonical_json(1.5) == "1.500000"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_nan_and_inf_are_rejected(bad):
    with pytest.raises(ValueError):
        canonical_json({"x": bad})


def test_keys_sorted_and_compact():
    assert canonical_json({"b": 1, "a": [1, 2], "c": None, "d": True}) == \
        '{"a":[1,2],"b":1,"c":null,"d":true}'


def test_sets_are_sorted():
    assert canonical_json(frozenset({"SELL", "BUY"})) == '["BUY","SELL"]'
    assert canonical_json({"s": {3, 1, 2}}) == '{"s":[1,2,3]}'


def test_trading_params_render():
    s = canonical_json({"params": TradingParams()})
    assert '"min_rr":2.000000' in s and '"trail_tiers":[{' in s


_SNIPPET = (
    "from app.runtime.serialize import canonical_json;"
    "print(canonical_json({'dirs': frozenset({'BUY','SELL'}),"
    " 'names': frozenset({'ORB_VWAP','RSI_VWAP','IV_CRUSH','CPR_VWAP','OI_SPIKE'})}))"
)


def test_frozenset_output_is_stable_across_processes():
    outs = set()
    for seed in ("1", "2", "3"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        r = subprocess.run([sys.executable, "-c", _SNIPPET], cwd=BACKEND, env=env,
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        outs.add(r.stdout.strip())
    assert len(outs) == 1
