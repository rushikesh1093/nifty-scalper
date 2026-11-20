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

    # Expiry-day iron fly paper trading (app/services/trading/iron_fly.py): a
    # separate paper book; False stops it opening new spreads.
    IRON_FLY_PAPER_ENABLED: bool = True
    INSTRUMENT_DUMP_URL: str = (
        "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    )

    # Tick retention (app/services/market_data/tick_retention.py): ticks older
    # than this many days are backed up to TICK_BACKUP_DIR as .csv.gz, then
    # deleted, daily at TICK_CLEANUP_TIME_IST. A LOW DISK warning is logged
    # below LOW_DISK_WARN_GB free.
    TICK_RETENTION_DAYS: int = 30
    TICK_BACKUP_DIR: str = "backups/ticks"
    TICK_CLEANUP_TIME_IST: str = "16:05"
    # Option archive (app/research/option_archive.py): before each retention
    # pass, every recorded day not yet archived is added to this SQLite file as
    # per-minute option quotes (bid/ask, OI, volume) - the permanent history the
    # research needs; ticks themselves are deleted after TICK_RETENTION_DAYS.
    # Run in a separate process. Empty path disables it.
    OPTION_ARCHIVE_PATH: str = "backups/options_archive.db"
    # ~1.3 GB of ticks a trading day: 10 GB is about a week's warning.
    LOW_DISK_WARN_GB: float = 10.0

    # Logs (app/core/logging.py): rotating backend.log and alerts.log in
    # LOG_DIR; empty disables the files. Alerts (app/core/alerts.py) also go to
    # Telegram when both ALERT_TELEGRAM_* are set.
    LOG_DIR: str = "logs"
    LOG_MAX_MB: int = 20
    LOG_BACKUPS: int = 10
    ALERT_TELEGRAM_BOT_TOKEN: Optional[str] = None
    ALERT_TELEGRAM_CHAT_ID: Optional[str] = None

    # Candle intervals supported
    SUPPORTED_INTERVALS: list = ["ONE_MINUTE", "THREE_MINUTE", "FIVE_MINUTE", "FIFTEEN_MINUTE"]

    # Signal-generation window (IST). These bound signal generation, not the
    # exchange session (which closes at 15:30). ClassVar so they cannot be
    # overridden per environment — the frontend mirrors them only as a
    # pre-fetch fallback and renders the diagnostics payload's session_window.
    SESSION_START_IST: ClassVar[time] = time(9, 15)
    SESSION_END_IST:   ClassVar[time] = time(15, 25)

    # Operator key for mutating risk routes (halt release, reconcile, ...).
    # No default: unset means those routes return 503, while engaging a halt
    # always works (design §17.4). Never logged or returned by any route.
    ADMIN_API_KEY: Optional[str] = None
    # Recorded as risk_halts.released_by on a release — the label, never the key.
    ADMIN_API_KEY_LABEL: str = "admin"

    # Replay engine knobs (design §14.6). The seed default is a recorded
    # constant, never "random", so an unseeded run is still reproducible.
    REPLAY_DEFAULT_SEED: int = 20260101
    REPLAY_MAX_RANGE_DAYS: int = 366
    REPLAY_ARTIFACT_DIR: str = ""      # empty → the CLI's --out default

    # Backtest page (design §4.1). Safe defaults: unset → no replay store.
    # REPLAY_DATABASE_URL must never name the paper DB (refused) and is never returned.
    REPLAY_DATABASE_URL: Optional[str] = None
    REPLAY_DATASET_DIR: str = ""       # empty → <system temp>/nifty-replay-datasets
    REPLAY_JOB_TIMEOUT_S: int = 1800
    REPLAY_DATASET_TIMEOUT_S: int = 600
    REPLAY_JOB_HISTORY: int = 20
    BACKTEST_SYNTHETIC_MAX_DAYS: int = 60
    BACKTEST_MAX_ARTIFACT_FILES: int = 200
    BACKTEST_MAX_LAUNCH_RANGE_DAYS: int = 90

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
