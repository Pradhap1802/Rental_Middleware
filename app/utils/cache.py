import time
import threading
from typing import Dict, Any, Optional, Tuple


class TTLCache:
    """
    Thread-safe In-Memory TTL Cache supporting automatic expiration and explicit invalidation.
    Used for mapping lookups, Tally company lists, and RentAsst metadata.
    """
    def __init__(self, default_ttl_seconds: int = 300):
        self.default_ttl_seconds = default_ttl_seconds
        self._cache: Dict[str, Tuple[Any, float]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            if key not in self._cache:
                return None
            val, expires_at = self._cache[key]
            if time.time() > expires_at:
                del self._cache[key]
                return None
            return val

    def set(self, key: str, value: Any, ttl_seconds: Optional[int] = None) -> None:
        duration = ttl_seconds if ttl_seconds is not None else self.default_ttl_seconds
        expires_at = time.time() + duration
        with self._lock:
            self._cache[key] = (value, expires_at)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._cache.pop(key, None)

    def invalidate_prefix(self, prefix: str) -> None:
        with self._lock:
            keys_to_del = [k for k in self._cache if k.startswith(prefix)]
            for k in keys_to_del:
                del self._cache[k]

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
