from datetime import time
from typing import ClassVar, Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # App
    APP_NAME: str = "Nifty Scalper"
    DEBUG: bool = False
    ENVIRONMENT: str = "development"

    # PostgreSQL (local)
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "1093"
    POSTGRES_DB: str = "nifty_scalper"

    # Async DB URL for SQLAlchemy
    @property
    def DATABASE_URL(self) -> str:
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    # Sync URL for Alembic migrations
    @property
    def SYNC_DATABASE_URL(self) -> str:
        return (
            f"postgresql+psycopg2://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    # Redis (local)
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: Optional[str] = None

    @property
    def REDIS_URL(self) -> str:
        if self.REDIS_PASSWORD:
            return f"redis://:{self.REDIS_PASSWORD}@{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    # AngelOne SmartAPI credentials (fill in .env)
    ANGEL_API_KEY: str = ""
    ANGEL_CLIENT_ID: str = ""
    ANGEL_PASSWORD: str = ""
    ANGEL_TOTP_SECRET: str = ""

    @property
    def angel_credentials_configured(self) -> bool:
        """True only when all four AngelOne credentials are non-empty."""
        return all([
            self.ANGEL_API_KEY,
            self.ANGEL_CLIENT_ID,
            self.ANGEL_PASSWORD,
            self.ANGEL_TOTP_SECRET,
        ])

    # Market data settings
    NIFTY_TOKEN: str = "26000"          # NSE Nifty 50 spot token
    BANKNIFTY_TOKEN: str = "26009"      # NSE BankNifty spot token
    VIX_TOKEN: str = "26017"            # India VIX token on NSE
    INSTRUMENT_DUMP_URL: str = (
        "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    )

    # Candle intervals supported
    SUPPORTED_INTERVALS: list = ["ONE_MINUTE", "THREE_MINUTE", "FIVE_MINUTE", "FIFTEEN_MINUTE"]

    # Signal-generation window (IST). These bound signal generation, not the
    # exchange session (which closes at 15:30). ClassVar so they cannot be
    # overridden per environment — the frontend mirrors them only as a
    # pre-fetch fallback and renders the diagnostics payload's session_window.
    SESSION_START_IST: ClassVar[time] = time(9, 15)
    SESSION_END_IST:   ClassVar[time] = time(15, 25)

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
