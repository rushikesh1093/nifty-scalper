from pydantic_settings import BaseSettings
from typing import Optional


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
        from urllib.parse import quote_plus
        return (
            f"postgresql+asyncpg://{quote_plus(self.POSTGRES_USER)}:{quote_plus(self.POSTGRES_PASSWORD)}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    # Sync URL for Alembic migrations
    @property
    def SYNC_DATABASE_URL(self) -> str:
        from urllib.parse import quote_plus
        return (
            f"postgresql+psycopg2://{quote_plus(self.POSTGRES_USER)}:{quote_plus(self.POSTGRES_PASSWORD)}"
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

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
