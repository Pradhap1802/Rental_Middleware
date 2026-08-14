import os
import random
import re
import requests
from typing import Optional


class RetryableException(Exception):
    """Exception indicating transient network or server error that should be retried."""
    pass


class NonRetryableException(Exception):
    """Exception indicating permanent validation/mapping error that should not be retried."""
    pass


BACKOFF_SCHEDULE_SECONDS = [
    5 * 60,       # Attempt 1 -> 5 min (300s)
    15 * 60,      # Attempt 2 -> 15 min (900s)
    30 * 60,      # Attempt 3 -> 30 min (1800s)
    60 * 60,      # Attempt 4 -> 1 hour (3600s)
    6 * 3600,     # Attempt 5 -> 6 hours (21600s)
    24 * 3600,    # Attempt 6 -> 24 hours (86400s)
]


class RetryConfig:
    """Configurable Retry & Backoff Parameters."""
    def __init__(
        self,
        max_attempts: Optional[int] = None,
        base_delay: Optional[int] = None,
        max_delay: Optional[int] = None,
        jitter: Optional[float] = None,
    ):
        self.max_attempts = max_attempts or int(os.getenv("RETRY_MAX_ATTEMPTS", "5"))
        self.base_delay = base_delay or int(os.getenv("RETRY_BASE_DELAY", "5"))
        self.max_delay = max_delay or int(os.getenv("RETRY_MAX_DELAY", "3600"))
        self.jitter = jitter if jitter is not None else float(os.getenv("RETRY_JITTER", "0.2"))


DEFAULT_RETRY_CONFIG = RetryConfig()


def get_backoff_delay_seconds(attempt: int, config: Optional[RetryConfig] = None) -> Optional[int]:
    """
    Computes exponential backoff delay with jitter for a given attempt (1-indexed).
    Formula: min(base_delay * 2^(attempt - 1), max_delay) * (1 +- jitter)
    Returns None if attempt > max_attempts.
    """
    cfg = config or DEFAULT_RETRY_CONFIG
    if attempt <= 0:
        attempt = 1
    if attempt > cfg.max_attempts:
        return None

    # Exponential backoff base: base_delay * 2^(attempt-1)
    delay = cfg.base_delay * (2 ** (attempt - 1))
    delay = min(delay, cfg.max_delay)

    # Jitter variation
    if cfg.jitter > 0:
        jitter_delta = delay * cfg.jitter
        delay = delay + random.uniform(-jitter_delta, jitter_delta)

    return max(1, int(round(delay)))


# Regex patterns for string error classification
RETRYABLE_PATTERNS = re.compile(
    r"(connection refused|timeout|timed out|network|temporary|econnrefused|503|500|502|504|429|service unavailable|tally not responding|socket)",
    re.IGNORECASE,
)

NON_RETRYABLE_PATTERNS = re.compile(
    r"(invalid payload|missing ledger|ledger .* does not exist|invalid gst|gstin|tax rate mismatch|business validation|malformed xml|parse error|duplicate voucher|400|401|403|404|422)",
    re.IGNORECASE,
)


def is_retryable_exception(exc: Exception) -> bool:
    """
    Strictly classifies exceptions into retryable (transient) or non-retryable (permanent).
    """
    if isinstance(exc, NonRetryableException):
        return False
    if isinstance(exc, RetryableException):
        return True

    # 1. Inspect HTTP response status codes if present
    if isinstance(exc, requests.exceptions.HTTPError):
        if exc.response is not None:
            code = exc.response.status_code
            if code in (429, 500, 502, 503, 504):
                return True
            if code in (400, 401, 403, 404, 422):
                return False

    # 2. Check standard request/socket network exceptions
    if isinstance(
        exc,
        (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            TimeoutError,
            ConnectionRefusedError,
            ConnectionResetError,
            OSError,
        ),
    ):
        return True

    # 3. Check standard Python data validation exceptions
    if isinstance(exc, (ValueError, KeyError, TypeError, AttributeError)):
        return False

    # 4. Check error message patterns for Tally/RentAsst specific errors
    err_str = str(exc)
    if NON_RETRYABLE_PATTERNS.search(err_str):
        return False
    if RETRYABLE_PATTERNS.search(err_str):
        return True

    # Default to non-retryable to prevent infinite loops on unclassified logic bugs
    return False
