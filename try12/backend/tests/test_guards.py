"""The suite must never reach the production DB, Redis or broker."""
import pytest

from app.core.config import settings


def test_settings_point_at_blocked_ports():
    assert settings.POSTGRES_PORT == 1
    assert settings.POSTGRES_DB == "pytest_blocked"
    assert settings.REDIS_PORT == 1
    assert not settings.angel_credentials_configured


def test_async_session_is_blocked():
    import app.db.base as base
    from app.services.trading import mode_manager as mm, paper_engine as pe

    for factory in (base.AsyncSessionLocal, mm.AsyncSessionLocal, pe.AsyncSessionLocal):
        with pytest.raises(RuntimeError, match="DB access blocked"):
            factory()


def test_redis_not_connected():
    from app.services.cache.redis_client import redis_cache

    assert redis_cache._redis is None
    with pytest.raises(RuntimeError):
        redis_cache._r()
