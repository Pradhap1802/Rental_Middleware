import requests
from typing import Optional


class RetryableException(Exception):
    """Exception indicating transient network or server error that should be retried."""
    pass


class NonRetryableException(Exception):
    """Exception indicating permanent validation/mapping error that should not be retried."""
    pass


# Exponential backoff schedule in seconds: 5m, 15m, 30m, 1h, 6h, 24h
BACKOFF_SCHEDULE_SECONDS = [
    5 * 60,       # Attempt 1 -> 5 min (300s)
    15 * 60,      # Attempt 2 -> 15 min (900s)
    30 * 60,      # Attempt 3 -> 30 min (1800s)
    60 * 60,      # Attempt 4 -> 1 hour (3600s)
    6 * 3600,     # Attempt 5 -> 6 hours (21600s)
    24 * 3600,    # Attempt 6 -> 24 hours (86400s)
]


def is_retryable_exception(exc: Exception) -> bool:
    """Classifies exceptions as retryable (transient) or non-retryable (permanent)."""
    if isinstance(exc, NonRetryableException):
        return False
    if isinstance(exc, RetryableException):
        return True

    # Check requests library exceptions
    if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True
    
    if isinstance(exc, requests.exceptions.HTTPError):
        if exc.response is not None:
            status_code = exc.response.status_code
            if status_code in (429, 500, 502, 503, 504):
                return True
            if status_code in (400, 401, 403, 404, 422):
                return False

    # Standard built-in network / socket timeouts
    if isinstance(exc, (TimeoutError, ConnectionRefusedError, ConnectionResetError, OSError)):
        return True

    # Standard data/validation exceptions are non-retryable
    if isinstance(exc, (ValueError, KeyError, TypeError, AttributeError)):
        return False

    # Default to False for unidentified exceptions to avoid infinite retries on code logic bugs
    return False


def get_backoff_delay_seconds(attempts: int) -> Optional[int]:
    """
    Returns delay duration in seconds for given attempt count (1-indexed).
    Returns None if max attempts exhausted.
    """
    if attempts <= 0:
        return BACKOFF_SCHEDULE_SECONDS[0]
    idx = attempts - 1
    if idx < len(BACKOFF_SCHEDULE_SECONDS):
        return BACKOFF_SCHEDULE_SECONDS[idx]
    return None  # Exhausted
