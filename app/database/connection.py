import sqlite3
import os
import threading
from typing import ContextManager
from contextlib import contextmanager


class DatabaseManager:
    """Thread-safe SQLite Database Manager with WAL mode and memory tuning."""
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._local = threading.local()
        self._init_db()

    def _create_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA cache_size=-64000;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.execute("PRAGMA busy_timeout=10000;")
        return conn

    def _init_db(self):
        with self.get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mapping (
                    entity_type TEXT NOT NULL,
                    rentasst_id TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    tally_guid TEXT,
                    sync_version INTEGER DEFAULT 1,
                    last_hash TEXT,
                    last_synced_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_sync DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_attempt DATETIME DEFAULT CURRENT_TIMESTAMP,
                    status TEXT DEFAULT 'synced',
                    PRIMARY KEY (entity_type, rentasst_id)
                )
                """
            )
            # Ensure all columns exist on older SQLite databases
            for col in ["tally_guid TEXT", "sync_version INTEGER DEFAULT 1", "last_hash TEXT", "last_synced_at DATETIME", "last_sync DATETIME", "last_attempt DATETIME", "status TEXT"]:
                try:
                    conn.execute(f"ALTER TABLE mapping ADD COLUMN {col};")
                except Exception:
                    pass

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    rentasst_id TEXT NOT NULL,
                    external_id TEXT,
                    tally_guid TEXT,
                    status TEXT NOT NULL,
                    details TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            for col in ["tally_guid TEXT", "external_id TEXT", "details TEXT"]:
                try:
                    conn.execute(f"ALTER TABLE sync_history ADD COLUMN {col};")
                except Exception:
                    pass

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_checkpoint (
                    entity_type TEXT PRIMARY KEY,
                    last_sync_at TEXT,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dead_letters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    rentasst_id TEXT NOT NULL,
                    error_message TEXT NOT NULL,
                    payload TEXT,
                    failed_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'Pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    next_attempt_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_error TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_active_queue_job 
                ON sync_queue (entity_type) 
                WHERE status IN ('Pending', 'Running', 'Waiting', 'Retry')
                """
            )

    @contextmanager
    def get_connection(self) -> ContextManager[sqlite3.Connection]:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = self._create_connection()
        try:
            yield self._local.conn
            self._local.conn.commit()
        except Exception:
            self._local.conn.rollback()
            raise

    def close(self):
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None
