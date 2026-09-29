import logging
import logging.handlers
import os
import json
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from ..security.masking import mask_log_message, mask_payload_secrets

LOG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".data", "logs"))
os.makedirs(LOG_DIR, exist_ok=True)
MAIN_LOG_PATH = os.path.join(LOG_DIR, "middleware.log")


class StructuredJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        msg_masked = mask_log_message(record.getMessage())
        log_obj = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "component": getattr(record, "component", record.name.split(".")[-1]),
            "message": msg_masked,
        }
        if hasattr(record, "duration_ms"):
            log_obj["duration"] = record.duration_ms
        if hasattr(record, "metadata") and isinstance(record.metadata, dict):
            masked_meta = mask_payload_secrets(record.metadata)
            log_obj["metadata"] = masked_meta
            # Elevate mandatory sync metadata fields to root log JSON object if present
            for field in [
                "correlation_id", "job_id", "entity_type", "entity_id",
                "company_id", "direction", "source_system", "target_system",
                "attempt", "status"
            ]:
                if field in masked_meta:
                    log_obj[field] = masked_meta[field]

        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_obj, ensure_ascii=False)


def setup_centralized_logger() -> logging.Logger:
    logger = logging.getLogger("RentalMiddleware")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(name)s: %(message)s"))
        logger.addHandler(console_handler)

        file_handler = logging.handlers.RotatingFileHandler(
            MAIN_LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(StructuredJsonFormatter())
        logger.addHandler(file_handler)

    return logger


main_logger = setup_centralized_logger()


def log_event(
    component: str,
    message: str,
    level: int = logging.INFO,
    duration_ms: Optional[float] = None,
    metadata: Optional[Dict[str, Any]] = None,
):
    safe_message = mask_log_message(message)
    safe_metadata = mask_payload_secrets(metadata) if metadata is not None else None

    extra = {"component": component}
    if duration_ms is not None:
        extra["duration_ms"] = duration_ms
    if safe_metadata is not None:
        extra["metadata"] = safe_metadata

    main_logger.log(level, safe_message, extra=extra)


def log_sync_event(
    entity_type: str,
    entity_id: str,
    company_id: str = "default",
    direction: str = "forward",
    source_system: str = "rentasst",
    target_system: str = "tally",
    job_id: Optional[int] = None,
    correlation_id: Optional[str] = None,
    attempt: int = 1,
    status: str = "SUCCESS",
    duration_ms: Optional[float] = None,
    message: str = "",
    metadata: Optional[Dict[str, Any]] = None,
    level: int = logging.INFO,
):
    """
    Standardized logger for every sync operation.
    Ensures mandatory context fields: correlation_id, job_id, entity_type, entity_id,
    company_id, direction, source_system, target_system, attempt, status, duration.
    """
    corr_id = correlation_id or str(uuid.uuid4())
    sync_meta = {
        "correlation_id": corr_id,
        "job_id": job_id,
        "entity_type": entity_type,
        "entity_id": str(entity_id),
        "company_id": company_id,
        "direction": direction,
        "source_system": source_system,
        "target_system": target_system,
        "attempt": attempt,
        "status": status,
        "duration_ms": duration_ms or 0.0,
    }
    if metadata and isinstance(metadata, dict):
        sync_meta.update(metadata)

    log_msg = message or f"Sync {direction} {entity_type} #{entity_id} status={status}"
    log_event(
        component=f"Sync:{entity_type}",
        message=log_msg,
        level=level,
        duration_ms=duration_ms,
        metadata=sync_meta,
    )
