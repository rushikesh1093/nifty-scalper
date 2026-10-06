"""Fix 8: _build_candidates early exits must return 3-tuples (callers unpack 3)."""
from datetime import date

from app.services.trading.option_selector import option_selector
from app.strategies.base import Direction
from tests.helpers import run


class _Result:
    def __init__(self, scalar=None, rows=None):
        self._scalar = scalar
        self._rows = rows or []

    def scalar(self):
        return self._scalar

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, *results):
        self._results = list(results)

    async def execute(self, *_args, **_kwargs):
        return self._results.pop(0)


def test_no_expiry_returns_none_without_unpack_error():
    assert run(option_selector.select(
        db=_FakeDB(_Result(scalar=None)), direction=Direction.BUY,
        underlying="NIFTY", spot_price=22600.0,
    )) is None
    assert run(option_selector.select_top_n(
        db=_FakeDB(_Result(scalar=None)), direction=Direction.BUY,
        underlying="NIFTY", spot_price=22600.0,
    )) == []


def test_no_instruments_returns_none_without_unpack_error():
    exp = date(2026, 10, 13)
    assert run(option_selector.select(
        db=_FakeDB(_Result(scalar=exp), _Result(rows=[])), direction=Direction.SELL,
        underlying="NIFTY", spot_price=22600.0,
    )) is None
    assert run(option_selector.select_top_n(
        db=_FakeDB(_Result(scalar=exp), _Result(rows=[])), direction=Direction.SELL,
        underlying="NIFTY", spot_price=22600.0,
    )) == []


def test_build_candidates_shape():
    out = run(option_selector._build_candidates(
        _FakeDB(_Result(scalar=None)), Direction.BUY, "NIFTY", 22600.0,
    ))
    assert out == ([], 0, 0)
