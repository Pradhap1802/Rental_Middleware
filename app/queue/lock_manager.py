import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional
from ..database.connection import DatabaseManager


class LockManager:
    """
    Thread-safe & process-safe SQLite Lock/Lease Manager supporting automatic stale-lock purge,
    lease renewal, and explicit worker ownership.
    """
    def __init__(self, db_path: str, default_lease_seconds: int = 300):
        self.db_path = db_path
        self.db = DatabaseManager(db_path)
        self.default_lease_seconds = default_lease_seconds

    def generate_lock_key(
        self,
        company: str,
        entity_type: str,
        direction: str,
        record_id: str,
    ) -> str:
        c = (company or "default").strip().lower()
        ent = (entity_type or "").strip().lower()
        d = (direction or "forward").strip().lower()
        rid = str(record_id or "").strip()
        return f"{c}:{ent}:{d}:{rid}"

    def purge_expired_locks(self) -> int:
        """Purges stale expired leases from sync_locks."""
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self.db.get_connection() as c:
            cur = c.execute("DELETE FROM sync_locks WHERE expires_at <= ?", (now_iso,))
            return cur.rowcount

    def acquire_lock(
        self,
        lock_key: str,
        worker_id: str,
        lease_seconds: Optional[int] = None,
    ) -> bool:
        """
        Attempts to acquire lock for lock_key.
        Automatically purges stale expired locks first.
        Returns True if acquired, False if currently locked by an active lease.
        """
        if not lock_key or not worker_id:
            return False

        duration = lease_seconds if lease_seconds is not None else self.default_lease_seconds
        now_dt = datetime.now(timezone.utc)
        expires_dt = now_dt + timedelta(seconds=duration)
        now_iso = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        expires_iso = expires_dt.strftime("%Y-%m-%d %H:%M:%S")

        with self.db.get_connection() as c:
            # 1. Clear any expired lock on this key
            c.execute("DELETE FROM sync_locks WHERE lock_key=? AND expires_at <= ?", (lock_key, now_iso))

            # 2. Try inserting new lock
            try:
                c.execute(
                    """
                    INSERT INTO sync_locks (lock_key, worker_id, acquired_at, expires_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (lock_key, worker_id, now_iso, expires_iso),
                )
                return True
            except sqlite3.IntegrityError:
                # Lock is active and owned by another worker
                return False

    def release_lock(self, lock_key: str, worker_id: str) -> bool:
        """Releases active lock owned by worker_id."""
        if not lock_key or not worker_id:
            return False
        with self.db.get_connection() as c:
            cur = c.execute("DELETE FROM sync_locks WHERE lock_key=? AND worker_id=?", (lock_key, worker_id))
            return cur.rowcount > 0

    def renew_lock(self, lock_key: str, worker_id: str, extension_seconds: Optional[int] = None) -> bool:
        """Renews/extends active lease owned by worker_id."""
        duration = extension_seconds if extension_seconds is not None else self.default_lease_seconds
        expires_dt = datetime.now(timezone.utc) + timedelta(seconds=duration)
        expires_iso = expires_dt.strftime("%Y-%m-%d %H:%M:%S")

        with self.db.get_connection() as c:
            cur = c.execute(
                "UPDATE sync_locks SET expires_at=? WHERE lock_key=? AND worker_id=?",
                (expires_iso, lock_key, worker_id),
            )
            return cur.rowcount > 0
