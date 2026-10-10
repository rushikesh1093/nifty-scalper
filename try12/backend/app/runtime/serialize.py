"""
canonical_json — the single serialiser for the determinism hash,
replay_runs.params and params_hash (design §14.5).

Rules: object keys sorted; sets/frozensets sorted (Python's string hash is
salted per process, so set order is not stable across runs); tuples render as
lists; dataclasses as objects; enums by value; dates as ISO strings; floats
rounded to 6 dp and rendered fixed-point, with NaN/inf rejected and -0.0
normalised. Compact separators, no whitespace.
"""
from __future__ import annotations

import dataclasses
import enum
import json
import math
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any


def _render_float(v: float) -> str:
    if math.isnan(v) or math.isinf(v):
        raise ValueError(f"canonical_json cannot render {v!r}")
    s = format(round(v, 6), ".6f")
    return "0.000000" if s == "-0.000000" else s      # normalise signed zero


def _enc(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, enum.Enum):
        return _enc(v.value)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return _render_float(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (datetime, date)):
        return json.dumps(v.isoformat())
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return _enc({f.name: getattr(v, f.name) for f in dataclasses.fields(v)})
    if isinstance(v, Mapping):
        items = sorted((str(k), val) for k, val in v.items())
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + _enc(val)
                              for k, val in items) + "}"
    if isinstance(v, (set, frozenset)):
        return "[" + ",".join(sorted(_enc(x) for x in v)) + "]"
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(_enc(x) for x in v) + "]"
    raise TypeError(f"canonical_json cannot render {type(v).__name__}")


def canonical_json(obj: Any) -> str:
    return _enc(obj)
