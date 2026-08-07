import sqlite3
import os
from typing import Optional, Tuple, List, Dict, Any


class MappingStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init()

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def _init(self):
        with self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS mapping (
                    entity_type TEXT NOT NULL,
                    rentasst_id TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    last_synced_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (entity_type, rentasst_id)
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    entity_type TEXT PRIMARY KEY,
                    last_sync_at TEXT
                )
                """
            )
            c.execute(
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

    def get_external_id(self, entity_type: str, rentasst_id: str) -> Optional[str]:
        with self._conn() as c:
            cur = c.execute(
                "SELECT external_id FROM mapping WHERE entity_type=? AND rentasst_id=?",
                (entity_type, rentasst_id),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def get_rentasst_id(self, entity_type: str, external_id: str) -> Optional[str]:
        with self._conn() as c:
            cur = c.execute(
                "SELECT rentasst_id FROM mapping WHERE entity_type=? AND external_id=?",
                (entity_type, external_id),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def upsert_mapping(self, entity_type: str, rentasst_id: str, external_id: str) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO mapping (entity_type, rentasst_id, external_id, last_synced_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(entity_type, rentasst_id) DO UPDATE SET
                    external_id=excluded.external_id,
                    last_synced_at=CURRENT_TIMESTAMP
                """,
                (entity_type, rentasst_id, external_id),
            )

    def set_checkpoint(self, entity_type: str, timestamp: str) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO checkpoints (entity_type, last_sync_at)
                VALUES (?, ?)
                ON CONFLICT(entity_type) DO UPDATE SET last_sync_at=excluded.last_sync_at
                """,
                (entity_type, timestamp),
            )

    def get_checkpoint(self, entity_type: str) -> Optional[str]:
        with self._conn() as c:
            cur = c.execute("SELECT last_sync_at FROM checkpoints WHERE entity_type=?", (entity_type,))
            row = cur.fetchone()
            return row[0] if row else None

    def add_dead_letter(self, entity_type: str, source_id: str, error: str, payload: Optional[str] = None) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO dead_letter (entity_type, source_id, error, payload) VALUES (?, ?, ?, ?)",
                (entity_type, source_id or "", error[:1000], (payload or "")[:5000]),
            )

    def list_dead_letters(self, entity_type: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        with self._conn() as c:
            if entity_type:
                cur = c.execute(
                    "SELECT id, entity_type, source_id, error, created_at FROM dead_letter WHERE entity_type=? ORDER BY id DESC LIMIT ?",
                    (entity_type, limit),
                )
            else:
                cur = c.execute(
                    "SELECT id, entity_type, source_id, error, created_at FROM dead_letter ORDER BY id DESC LIMIT ?",
                    (limit,),
                )
            return [dict(zip([col[0] for col in cur.description], row)) for row in cur.fetchall()]
