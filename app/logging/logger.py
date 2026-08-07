import logging
import logging.handlers
import os
import json
from datetime import datetime, timezone
from typing import Dict, Any, Optional

LOG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".data", "logs"))
os.makedirs(LOG_DIR, exist_ok=True)
MAIN_LOG_PATH = os.path.join(LOG_DIR, "middleware.log")


class StructuredJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "component": getattr(record, "component", record.name.split(".")[-1]),
            "message": record.getMessage(),
        }
        if hasattr(record, "duration_ms"):
            log_obj["duration_ms"] = record.duration_ms
        if hasattr(record, "metadata") and isinstance(record.metadata, dict):
            log_obj["metadata"] = record.metadata
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_obj, ensure_ascii=False)


def setup_centralized_logger() -> logging.Logger:
    logger = logging.getLogger("RentalMiddleware")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        # Daily rotating file handler
        file_handler = logging.handlers.TimedRotatingFileHandler(
            MAIN_LOG_PATH, when="midnight", interval=1, backupCount=30, encoding="utf-8"
        )
        file_handler.setFormatter(StructuredJsonFormatter())
        logger.addHandler(file_handler)

        # Standard console handler
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(name)s: %(message)s"))
        logger.addHandler(console_handler)

    return logger


main_logger = setup_centralized_logger()


def log_event(
    component: str,
    message: str,
    level: int = logging.INFO,
    duration_ms: Optional[float] = None,
    metadata: Optional[Dict[str, Any]] = None,
):
    extra = {"component": component}
    if duration_ms is not None:
        extra["duration_ms"] = duration_ms
    if metadata is not None:
        extra["metadata"] = metadata
    main_logger.log(level, message, extra=extra)
