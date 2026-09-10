import logging
import sys
from datetime import datetime, timedelta, timezone


IST = timezone(timedelta(hours=5, minutes=30))


class ISTFormatter(logging.Formatter):
    """Render log timestamps in India Standard Time for operator visibility."""

    converter = None

    def formatTime(self, record, datefmt=None):
        current = datetime.fromtimestamp(record.created, IST)
        if datefmt:
            return current.strftime(datefmt)
        return current.strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]


def setup_logging(level: str = "INFO"):
    fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(ISTFormatter(fmt, datefmt="%Y-%m-%d %H:%M:%S,%f"))
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=[handler],
    )
    # Quieten noisy libs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
