import sqlite3
import os
import threading
from typing import ContextManager, Optional
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
                    source_system TEXT DEFAULT 'rentasst' NOT NULL,
                    source_company_id TEXT DEFAULT 'default' NOT NULL,
                    source_id TEXT NOT NULL DEFAULT '',
                    target_system TEXT DEFAULT 'tally' NOT NULL,
                    target_company_id TEXT DEFAULT 'default' NOT NULL,
                    target_id TEXT NOT NULL DEFAULT '',
                    integration_key TEXT,
                    last_synced_hash TEXT,
                    last_source_modified_at DATETIME,
                    last_target_modified_at DATETIME,
                    last_synced_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    sync_version INTEGER DEFAULT 1,
                    status TEXT DEFAULT 'synced',
                    rentasst_id TEXT DEFAULT '',
                    external_id TEXT DEFAULT '',
                    tally_guid TEXT,
                    last_hash TEXT,
                    last_sync DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_attempt DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (entity_type, rentasst_id)
                )
                """
            )
            # Ensure all new columns exist on existing databases
            cols_to_add = [
                "source_system TEXT DEFAULT 'rentasst'",
                "source_company_id TEXT DEFAULT 'default'",
                "source_id TEXT DEFAULT ''",
                "target_system TEXT DEFAULT 'tally'",
                "target_company_id TEXT DEFAULT 'default'",
                "target_id TEXT DEFAULT ''",
                "integration_key TEXT",
                "last_synced_hash TEXT",
                "last_source_modified_at DATETIME",
                "last_target_modified_at DATETIME",
                "tally_guid TEXT",
                "sync_version INTEGER DEFAULT 1",
                "last_hash TEXT",
                "last_synced_at DATETIME",
                "last_sync DATETIME",
                "last_attempt DATETIME",
                "status TEXT DEFAULT 'synced'",
                "rentasst_id TEXT DEFAULT ''",
                "external_id TEXT DEFAULT ''",
            ]
            for col in cols_to_add:
                try:
                    conn.execute(f"ALTER TABLE mapping ADD COLUMN {col};")
                except Exception:
                    pass

            # Migration: Backfill default source_id, target_id, last_synced_hash, integration_key
            try:
                conn.execute(
                    """
                    UPDATE mapping SET
                        source_system = CASE WHEN source_system IS NULL OR source_system = '' THEN 'rentasst' ELSE source_system END,
                        source_company_id = CASE WHEN source_company_id IS NULL OR source_company_id = '' THEN 'default' ELSE source_company_id END,
                        source_id = CASE WHEN source_id IS NULL OR source_id = '' THEN rentasst_id ELSE source_id END,
                        target_system = CASE WHEN target_system IS NULL OR target_system = '' THEN 'tally' ELSE target_system END,
                        target_company_id = CASE WHEN target_company_id IS NULL OR target_company_id = '' THEN 'default' ELSE target_company_id END,
                        target_id = CASE WHEN target_id IS NULL OR target_id = '' THEN external_id ELSE target_id END,
                        last_synced_hash = CASE WHEN last_synced_hash IS NULL OR last_synced_hash = '' THEN last_hash ELSE last_synced_hash END,
                        integration_key = CASE WHEN integration_key IS NULL OR integration_key = '' 
                                               THEN COALESCE(source_system, 'rentasst') || ':' || COALESCE(source_company_id, 'default') || ':' || entity_type || ':' || COALESCE(source_id, rentasst_id)
                                               ELSE integration_key END
                    WHERE integration_key IS NULL OR source_id IS NULL OR source_id = '';
                    """
                )
            except Exception:
                pass

            # Create Indexes & Unique Constraints for multi-company isolation and collision prevention
            try:
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_mapping_integration_key
                    ON mapping (integration_key)
                    WHERE integration_key IS NOT NULL AND integration_key != ''
                    """
                )
            except Exception:
                pass
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_mapping_source_lookup
                ON mapping (source_system, source_company_id, entity_type, source_id)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_mapping_target_lookup
                ON mapping (target_system, target_company_id, entity_type, target_id)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_mapping_rentasst_id
                ON mapping (entity_type, rentasst_id)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_mapping_external_id
                ON mapping (entity_type, external_id)
                """
            )

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
            # Legacy table alias for checkpoints
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    entity_type TEXT PRIMARY KEY,
                    last_sync_at TEXT
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dead_letters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT DEFAULT '',
                    rentasst_id TEXT DEFAULT '',
                    source_id TEXT DEFAULT '',
                    company_id TEXT DEFAULT 'default',
                    source_system TEXT DEFAULT 'rentasst',
                    target_system TEXT DEFAULT 'tally',
                    payload TEXT,
                    error_type TEXT,
                    error_message TEXT NOT NULL,
                    error TEXT,
                    stack_trace TEXT,
                    attempt_count INTEGER DEFAULT 1,
                    status TEXT DEFAULT 'PENDING',
                    first_failed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_failed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    failed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            dlq_cols = [
                "job_id INTEGER",
                "entity_id TEXT DEFAULT ''",
                "rentasst_id TEXT DEFAULT ''",
                "company_id TEXT DEFAULT 'default'",
                "source_system TEXT DEFAULT 'rentasst'",
                "target_system TEXT DEFAULT 'tally'",
                "error_type TEXT",
                "error_message TEXT",
                "stack_trace TEXT",
                "attempt_count INTEGER DEFAULT 1",
                "status TEXT DEFAULT 'PENDING'",
                "first_failed_at DATETIME",
                "last_failed_at DATETIME",
                "failed_at DATETIME",
                "source_id TEXT",
                "error TEXT",
                "created_at DATETIME",
            ]
            for col in dlq_cols:
                try:
                    conn.execute(f"ALTER TABLE dead_letters ADD COLUMN {col};")
                except Exception:
                    pass

            # Legacy table alias for dead_letter
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS dead_letter (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    source_id TEXT,
                    error TEXT NOT NULL,
                    payload TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_locks (
                    lock_key TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    acquired_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    expires_at DATETIME NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_locks_expires ON sync_locks (expires_at)
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT DEFAULT '',
                    company_id TEXT DEFAULT 'default' NOT NULL,
                    direction TEXT DEFAULT 'forward' NOT NULL,
                    payload TEXT,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    started_at DATETIME,
                    completed_at DATETIME,
                    scheduled_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    next_retry_at DATETIME,
                    next_attempt_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_error TEXT,
                    error_message TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            queue_cols = [
                "entity_id TEXT DEFAULT ''",
                "company_id TEXT DEFAULT 'default'",
                "direction TEXT DEFAULT 'forward'",
                "payload TEXT",
                "attempt_count INTEGER DEFAULT 0",
                "attempts INTEGER DEFAULT 0",
                "max_attempts INTEGER DEFAULT 3",
                "started_at DATETIME",
                "completed_at DATETIME",
                "scheduled_at DATETIME",
                "next_retry_at DATETIME",
                "next_attempt_at DATETIME",
                "last_error TEXT",
                "error_message TEXT",
                "updated_at DATETIME",
            ]
            for col in queue_cols:
                try:
                    conn.execute(f"ALTER TABLE sync_queue ADD COLUMN {col};")
                except Exception:
                    pass

            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_active_queue_job 
                ON sync_queue (company_id, entity_type) 
                WHERE status IN ('PENDING', 'PROCESSING', 'RETRYING', 'WAITING_FOR_DEPENDENCY', 'Waiting', 'Retry', 'Pending', 'Running')
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_conflicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    company_id TEXT DEFAULT 'default' NOT NULL,
                    field_name TEXT NOT NULL,
                    rentasst_value TEXT,
                    tally_value TEXT,
                    rentasst_modified_at DATETIME,
                    tally_modified_at DATETIME,
                    status TEXT DEFAULT 'OPEN' NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    resolved_at DATETIME
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reconciliation_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL DEFAULT 'all',
                    run_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    status TEXT DEFAULT 'COMPLETED',
                    summary_json TEXT
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reconciliation_discrepancies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT DEFAULT '',
                    mismatch_type TEXT NOT NULL,
                    rentasst_value TEXT,
                    tally_value TEXT,
                    details TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (run_id) REFERENCES reconciliation_runs(id) ON DELETE CASCADE
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bearer_tokens (
                    email TEXT NOT NULL,
                    token TEXT NOT NULL,
                    tenant_id TEXT DEFAULT 'default',
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (email, tenant_id)
                )
                """
            )
            try:
                bearer_cols = conn.execute("PRAGMA table_info(bearer_tokens)").fetchall()
                email_col = next((col for col in bearer_cols if col["name"] == "email"), None)
                if email_col and email_col["pk"] == 1 and not any(col["name"] == "tenant_id" and col["pk"] == 2 for col in bearer_cols):
                    conn.execute("ALTER TABLE bearer_tokens RENAME TO bearer_tokens_legacy")
                    conn.execute(
                        """
                        CREATE TABLE bearer_tokens (
                            email TEXT NOT NULL,
                            token TEXT NOT NULL,
                            tenant_id TEXT DEFAULT 'default',
                            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            PRIMARY KEY (email, tenant_id)
                        )
                        """
                    )
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO bearer_tokens (email, token, tenant_id, updated_at)
                        SELECT LOWER(email), token, COALESCE(NULLIF(tenant_id, ''), 'default'), updated_at
                        FROM bearer_tokens_legacy
                        """
                    )
                    conn.execute("DROP TABLE bearer_tokens_legacy")
            except Exception:
                pass

    def get_bearer_token(self, email: str, tenant_id: Optional[str] = None) -> Optional[dict]:
        if not email:
            return None
        email_clean = email.strip().lower()
        with self.get_connection() as conn:
            if tenant_id:
                tenant_clean = tenant_id.strip() or "default"
                cur = conn.execute(
                    """
                    SELECT email, token, tenant_id, updated_at
                    FROM bearer_tokens
                    WHERE LOWER(email) = ? AND tenant_id = ?
                    """,
                    (email_clean, tenant_clean),
                )
            else:
                cur = conn.execute(
                    """
                    SELECT email, token, tenant_id, updated_at
                    FROM bearer_tokens
                    WHERE LOWER(email) = ?
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (email_clean,),
                )
            row = cur.fetchone()
            if row:
                return dict(row)
        return None

    def save_bearer_token(self, email: str, token: str, tenant_id: str = "default") -> None:
        if not email or not token:
            return
        email_clean = email.strip().lower()
        with self.get_connection() as conn:
            conn.execute(
                """
                INSERT INTO bearer_tokens (email, token, tenant_id, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(email, tenant_id) DO UPDATE SET
                    token = excluded.token,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (email_clean, token, tenant_id or "default")
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
