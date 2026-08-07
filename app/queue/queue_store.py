import json
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any
from ..database.connection import DatabaseManager


class QueueStore:
    """SQLite Queue Engine repository managing sync_queue status lifecycle and deduplication."""
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.db = DatabaseManager(db_path)

    def enqueue(self, entity_type: str, payload: Optional[Dict[str, Any]] = None, priority: bool = False) -> Optional[int]:
        """
        Enqueues job for entity_type into sync_queue.
        Returns job ID if enqueued, or None if duplicate pending/running job exists.
        """
        payload_str = json.dumps(payload, ensure_ascii=False) if payload else None
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        scheduled_at = (datetime.now(timezone.utc) - timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S") if priority else now_iso

        with self.db.get_connection() as c:
            # Check for existing active job (deduplication)
            cur = c.execute(
                """
                SELECT id FROM sync_queue 
                WHERE entity_type=? AND status IN ('Pending', 'Running', 'Waiting', 'Retry')
                """,
                (entity_type,),
            )
            if cur.fetchone():
                return None  # Deduplicated

            cur = c.execute(
                """
                INSERT INTO sync_queue (entity_type, payload, status, attempts, max_attempts, created_at, updated_at, scheduled_at)
                VALUES (?, ?, 'Pending', 0, 3, ?, ?, ?)
                """,
                (entity_type, payload_str, now_iso, now_iso, scheduled_at),
            )
            return cur.lastrowid

    def claim_next_job(self) -> Optional[Dict[str, Any]]:
        """Atomically locks and claims next eligible pending/waiting job, updating status to Running."""
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self.db.get_connection() as c:
            cur = c.execute(
                """
                SELECT id, entity_type, payload, status, attempts, max_attempts 
                FROM sync_queue 
                WHERE status IN ('Pending', 'Waiting', 'Retry') AND scheduled_at <= ?
                ORDER BY id ASC LIMIT 1
                """,
                (now_iso,),
            )
            row = cur.fetchone()
            if not row:
                return None

            job_id = row["id"]
            c.execute(
                """
                UPDATE sync_queue 
                SET status='Running', updated_at=?
                WHERE id=?
                """,
                (now_iso, job_id),
            )
            return dict(row)

    def mark_completed(self, job_id: int) -> None:
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self.db.get_connection() as c:
            c.execute(
                "UPDATE sync_queue SET status='Completed', updated_at=?, error_message=NULL WHERE id=?",
                (now_iso, job_id),
            )

    def mark_retry(self, job_id: int, error_msg: str, delay_seconds: int) -> None:
        now_dt = datetime.now(timezone.utc)
        scheduled_dt = now_dt + timedelta(seconds=delay_seconds)
        now_iso = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        scheduled_iso = scheduled_dt.strftime("%Y-%m-%d %H:%M:%S")
        with self.db.get_connection() as c:
            c.execute(
                """
                UPDATE sync_queue 
                SET status='Waiting', attempts=attempts+1, error_message=?, updated_at=?, scheduled_at=?
                WHERE id=?
                """,
                (error_msg[:1000], now_iso, scheduled_iso, job_id),
            )

    def mark_failed(self, job_id: int, error_msg: str) -> None:
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self.db.get_connection() as c:
            c.execute(
                """
                UPDATE sync_queue 
                SET status='Failed', attempts=attempts+1, error_message=?, updated_at=?
                WHERE id=?
                """,
                (error_msg[:1000], now_iso, job_id),
            )

    def get_metrics(self) -> Dict[str, int]:
        with self.db.get_connection() as c:
            cur = c.execute(
                """
                SELECT status, COUNT(*) as cnt FROM sync_queue GROUP BY status
                """
            )
            stats = {"Pending": 0, "Running": 0, "Retry": 0, "Completed": 0, "Failed": 0, "Waiting": 0}
            for row in cur.fetchall():
                st = row["status"]
                if st in stats:
                    stats[st] = row["cnt"]
            return stats

    def retry_failed_jobs(self) -> int:
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self.db.get_connection() as c:
            cur = c.execute(
                """
                UPDATE sync_queue 
                SET status='Pending', attempts=0, error_message=NULL, updated_at=?, scheduled_at=?
                WHERE status='Failed'
                """,
                (now_iso, now_iso),
            )
            return cur.rowcount

    def list_recent_jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.db.get_connection() as c:
            cur = c.execute(
                "SELECT id, entity_type, status, attempts, max_attempts, error_message, created_at, updated_at FROM sync_queue ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]
