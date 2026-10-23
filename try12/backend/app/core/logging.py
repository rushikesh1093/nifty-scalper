import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

FMT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def _file_handler(path: Path, max_mb: int, backups: int, level=logging.NOTSET) -> RotatingFileHandler:
    h = RotatingFileHandler(path, maxBytes=max_mb * 1024 * 1024, backupCount=backups,
                            encoding="utf-8", delay=True)
    h.setFormatter(logging.Formatter(FMT))
    h.setLevel(level)
    h._nifty_file = True            # marks our handlers so a second call doesn't add twice
    return h


def setup_logging(level: str = "INFO", log_dir: Optional[str] = None,
                  max_mb: int = 20, backups: int = 10):
    """Console, plus (with *log_dir*) a rotating ``backend.log`` and an
    ``alerts.log`` that holds only app.core.alerts records (audit D9: the
    backend logged only to a console window, so a crash left no trace)."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=FMT,
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    if log_dir:
        d = Path(log_dir)
        d.mkdir(parents=True, exist_ok=True)
        root = logging.getLogger()
        if not any(getattr(h, "_nifty_file", False) for h in root.handlers):
            root.addHandler(_file_handler(d / "backend.log", max_mb, backups))
            alerts = logging.getLogger("alerts")
            alerts.addHandler(_file_handler(d / "alerts.log", max_mb, backups))
    # Quieten noisy libs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
