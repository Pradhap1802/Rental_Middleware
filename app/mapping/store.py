import sqlite3
import os
from typing import Optional, List, Dict, Any
from ..database.connection import DatabaseManager


class MappingStore:
    """Repository managing mappings, checkpoints, sync history, and dead letter records."""
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.db = DatabaseManager(db_path)

    # --- Modern Repository API Methods ---

    def find(self, entity_type: str, rentasst_id: str) -> Optional[Dict[str, Any]]:
        with self.db.get_connection() as c:
            cur = c.execute(
                """
                SELECT entity_type, rentasst_id, external_id, tally_guid, sync_version, 
                       last_hash, last_synced_at, last_sync, last_attempt, status
                FROM mapping WHERE entity_type=? AND rentasst_id=?
                """,
                (entity_type, rentasst_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def save(
        self,
        entity_type: str,
        rentasst_id: str,
        external_id: str,
        tally_guid: Optional[str] = None,
        sync_version: int = 1,
        last_hash: Optional[str] = None,
        status: str = "synced",
    ) -> None:
        guid = tally_guid or external_id
        with self.db.get_connection() as c:
            c.execute(
                """
                INSERT INTO mapping (
                    entity_type, rentasst_id, external_id, tally_guid, 
                    sync_version, last_hash, last_synced_at, last_sync, last_attempt, status
                )
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?)
                ON CONFLICT(entity_type, rentasst_id) DO UPDATE SET
                    external_id=excluded.external_id,
                    tally_guid=excluded.tally_guid,
                    sync_version=mapping.sync_version + 1,
                    last_hash=excluded.last_hash,
                    last_synced_at=CURRENT_TIMESTAMP,
                    last_sync=CURRENT_TIMESTAMP,
                    last_attempt=CURRENT_TIMESTAMP,
                    status=excluded.status
                """,
                (entity_type, rentasst_id, external_id, guid, sync_version, last_hash, status),
            )

    def update(self, entity_type: str, rentasst_id: str, **kwargs) -> None:
        if not kwargs:
            return
        fields = []
        values = []
        for k, v in kwargs.items():
            fields.append(f"{k}=?")
            values.append(v)
        fields.append("last_attempt=CURRENT_TIMESTAMP")
        query = f"UPDATE mapping SET {', '.join(fields)} WHERE entity_type=? AND rentasst_id=?"
        values.extend([entity_type, rentasst_id])
        with self.db.get_connection() as c:
            c.execute(query, tuple(values))

    def delete(self, entity_type: str, rentasst_id: str) -> bool:
        with self.db.get_connection() as c:
            cur = c.execute("DELETE FROM mapping WHERE entity_type=? AND rentasst_id=?", (entity_type, rentasst_id))
            return cur.rowcount > 0

    def exists(self, entity_type: str, rentasst_id: str) -> bool:
        with self.db.get_connection() as c:
            cur = c.execute("SELECT 1 FROM mapping WHERE entity_type=? AND rentasst_id=?", (entity_type, rentasst_id))
            return cur.fetchone() is not None

    def is_duplicate(self, entity_type: str, rentasst_id: str, current_hash: Optional[str] = None) -> bool:
        """Prevents duplicate voucher creation if payload hash is unchanged."""
        mapping = self.find(entity_type, rentasst_id)
        if not mapping:
            return False
        if current_hash and mapping.get("last_hash") == current_hash and mapping.get("status") == "synced":
            return True
        return False

    def add_history(
        self,
        entity_type: str,
        rentasst_id: str,
        status: str,
        external_id: Optional[str] = None,
        tally_guid: Optional[str] = None,
        details: Optional[str] = None,
    ) -> None:
        guid = tally_guid or external_id
        with self.db.get_connection() as c:
            c.execute(
                """
                INSERT INTO sync_history (entity_type, rentasst_id, external_id, tally_guid, status, details)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (entity_type, rentasst_id, external_id, guid, status, details),
            )

    # --- Backward-Compatible Legacy Adapter Methods ---

    def get_external_id(self, entity_type: str, rentasst_id: str) -> Optional[str]:
        with self.db.get_connection() as c:
            cur = c.execute(
                "SELECT external_id FROM mapping WHERE entity_type=? AND rentasst_id=?",
                (entity_type, rentasst_id),
            )
            row = cur.fetchone()
            return row["external_id"] if row else None

    def get_rentasst_id(self, entity_type: str, external_id: str) -> Optional[str]:
        with self.db.get_connection() as c:
            cur = c.execute(
                "SELECT rentasst_id FROM mapping WHERE entity_type=? AND (external_id=? OR tally_guid=?)",
                (entity_type, external_id, external_id),
            )
            row = cur.fetchone()
            return row["rentasst_id"] if row else None

    def upsert_mapping(self, entity_type: str, rentasst_id: str, external_id: str) -> None:
        self.save(entity_type, rentasst_id, external_id)

    def set_checkpoint(self, entity_type: str, timestamp: str) -> None:
        with self.db.get_connection() as c:
            c.execute(
                """
                INSERT INTO checkpoints (entity_type, last_sync_at)
                VALUES (?, ?)
                ON CONFLICT(entity_type) DO UPDATE SET last_sync_at=excluded.last_sync_at
                """,
                (entity_type, timestamp),
            )
            c.execute(
                """
                INSERT INTO sync_checkpoint (entity_type, last_sync_at, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(entity_type) DO UPDATE SET 
                    last_sync_at=excluded.last_sync_at,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (entity_type, timestamp),
            )

    def get_checkpoint(self, entity_type: str) -> Optional[str]:
        with self.db.get_connection() as c:
            cur = c.execute("SELECT last_sync_at FROM checkpoints WHERE entity_type=?", (entity_type,))
            row = cur.fetchone()
            return row["last_sync_at"] if row else None

    def add_dead_letter(self, entity_type: str, source_id: str, error: str, payload: Optional[str] = None) -> None:
        err_truncated = error[:1000]
        pay_truncated = (payload or "")[:5000]
        with self.db.get_connection() as c:
            c.execute(
                "INSERT INTO dead_letter (entity_type, source_id, error, payload) VALUES (?, ?, ?, ?)",
                (entity_type, source_id or "", err_truncated, pay_truncated),
            )
            c.execute(
                "INSERT INTO dead_letters (entity_type, source_id, error, payload) VALUES (?, ?, ?, ?)",
                (entity_type, source_id or "", err_truncated, pay_truncated),
            )

    def list_dead_letters(self, entity_type: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        with self.db.get_connection() as c:
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
            return [dict(row) for row in cur.fetchall()]

    def clear_dead_letters(self, entity_type: Optional[str] = None) -> int:
        with self.db.get_connection() as c:
            if entity_type:
                cur = c.execute("DELETE FROM dead_letter WHERE entity_type=?", (entity_type,))
                c.execute("DELETE FROM dead_letters WHERE entity_type=?", (entity_type,))
            else:
                cur = c.execute("DELETE FROM dead_letter")
                c.execute("DELETE FROM dead_letters")
            return cur.rowcount
