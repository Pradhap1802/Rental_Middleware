import json
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any
from ..database.connection import DatabaseManager
from ..logging.logger import log_event


def normalize_entity_type(entity_type: str) -> str:
    ent = (entity_type or "").strip().lower()
    if ent in ("customer", "customers"):
        return "customers"
    elif ent in ("equipment", "product", "products"):
        return "equipment"
    elif ent in ("invoice", "invoices"):
        return "invoices"
    elif ent in ("payment", "payments"):
        return "payments"
    elif ent in ("rental_order", "rental_orders"):
        return "rental_orders"
    elif ent in ("tally_to_rentasst", "reverse_sync"):
        return "tally_to_rentasst"
    return ent


class QueueStore:
    """SQLite Queue Engine repository managing sync_queue status lifecycle, explicit states, and deduplication."""
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.db = DatabaseManager(db_path)

    def enqueue(
        self,
        entity_type: str,
        payload: Optional[Dict[str, Any]] = None,
        priority: bool = False,
        entity_id: str = "",
        company_id: str = "default",
        direction: str = "forward",
    ) -> Optional[int]:
        """
        Enqueues job for entity_type into sync_queue with status='PENDING'.
        Returns job ID if enqueued, or None if duplicate active job exists.
        """
        norm_type = normalize_entity_type(entity_type)
        payload_str = json.dumps(payload, ensure_ascii=False) if payload else None
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        scheduled_at = (datetime.now(timezone.utc) - timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S") if priority else now_iso

        with self.db.get_connection() as c:
            # Deduplication: check active job matching status
            cur = c.execute(
                """
                SELECT id FROM sync_queue 
                WHERE company_id=? AND (entity_type=? OR entity_type=?) 
                  AND status IN ('PENDING', 'PROCESSING', 'RETRYING', 'WAITING_FOR_DEPENDENCY', 'Waiting', 'Retry', 'Pending', 'Running')
                """,
                (company_id, norm_type, entity_type),
            )
            if cur.fetchone():
                return None  # Deduplicated

            cur = c.execute(
                """
                INSERT INTO sync_queue (
                    entity_type, entity_id, company_id, direction, payload,
                    status, attempt_count, attempts, max_attempts,
                    created_at, updated_at, scheduled_at, next_retry_at, next_attempt_at
                )
                VALUES (?, ?, ?, ?, ?, 'PENDING', 0, 0, 3, ?, ?, ?, ?, ?)
                """,
                (norm_type, entity_id, company_id, direction, payload_str, now_iso, now_iso, scheduled_at, scheduled_at, scheduled_at),
            )
            return cur.lastrowid

    def claim_next_job(self) -> Optional[Dict[str, Any]]:
        """Atomically claims next eligible pending/waiting job, updating status to PROCESSING and populating started_at."""
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self.db.get_connection() as c:
            # Find candidate job
            cur_find = c.execute(
                """
                SELECT id FROM sync_queue 
                WHERE status IN ('PENDING', 'RETRYING', 'WAITING_FOR_DEPENDENCY', 'Pending', 'Waiting', 'Retry') 
                  AND (scheduled_at <= ? OR next_retry_at <= ? OR next_attempt_at <= ?)
                ORDER BY id ASC LIMIT 1
                """,
                (now_iso, now_iso, now_iso),
            )
            candidate = cur_find.fetchone()
            if not candidate:
                return None

            job_id = candidate["id"]
            # Atomic conditional UPDATE -> status='PROCESSING'
            cur_upd = c.execute(
                """
                UPDATE sync_queue 
                SET status='PROCESSING', started_at=?, updated_at=?
                WHERE id=? AND status IN ('PENDING', 'RETRYING', 'WAITING_FOR_DEPENDENCY', 'Pending', 'Waiting', 'Retry')
                """,
                (now_iso, now_iso, job_id),
            )
            if cur_upd.rowcount == 0:
                return None

            cur = c.execute(
                """
                SELECT id as job_id, id, entity_type, entity_id, company_id, direction, payload,
                       status, attempt_count, attempts, max_attempts, started_at, completed_at,
                       last_error, error_message, next_retry_at, created_at, updated_at
                FROM sync_queue WHERE id=?
                """,
                (job_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def mark_waiting_for_dependency(self, job_id: int, reason: str, delay_seconds: int = 60) -> None:
        """Transitions job to WAITING_FOR_DEPENDENCY state with scheduled retry delay."""
        now_dt = datetime.now(timezone.utc)
        scheduled_dt = now_dt + timedelta(seconds=delay_seconds)
        now_iso = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        scheduled_iso = scheduled_dt.strftime("%Y-%m-%d %H:%M:%S")
        err_truncated = reason[:1000]

        with self.db.get_connection() as c:
            c.execute(
                """
                UPDATE sync_queue
                SET status='WAITING_FOR_DEPENDENCY', last_error=?, error_message=?, updated_at=?,
                    scheduled_at=?, next_retry_at=?, next_attempt_at=?
                WHERE id=?
                """,
                (err_truncated, err_truncated, now_iso, scheduled_iso, scheduled_iso, scheduled_iso, job_id),
            )

    def mark_success(self, job_id: int, partial: bool = False) -> None:
        """Transitions job to SUCCESS or PARTIAL_SUCCESS state."""
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        final_status = "PARTIAL_SUCCESS" if partial else "SUCCESS"
        with self.db.get_connection() as c:
            c.execute(
                """
                UPDATE sync_queue 
                SET status=?, completed_at=?, updated_at=?, last_error=NULL, error_message=NULL 
                WHERE id=?
                """,
                (final_status, now_iso, now_iso, job_id),
            )

    def mark_completed(self, job_id: int) -> None:
        self.mark_success(job_id, partial=False)

    def mark_retrying(self, job_id: int, error_msg: str, delay_seconds: int) -> None:
        """Transitions job to RETRYING state with backoff schedule."""
        now_dt = datetime.now(timezone.utc)
        scheduled_dt = now_dt + timedelta(seconds=delay_seconds)
        now_iso = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        scheduled_iso = scheduled_dt.strftime("%Y-%m-%d %H:%M:%S")
        err_truncated = error_msg[:1000]

        with self.db.get_connection() as c:
            c.execute(
                """
                UPDATE sync_queue
                SET status='RETRYING', attempt_count=attempt_count+1, attempts=attempts+1,
                    last_error=?, error_message=?, updated_at=?, scheduled_at=?, next_retry_at=?, next_attempt_at=?
                WHERE id=?
                """,
                (err_truncated, err_truncated, now_iso, scheduled_iso, scheduled_iso, scheduled_iso, job_id),
            )

    def mark_retry(self, job_id: int, error_msg: str, delay_seconds: int) -> None:
        self.mark_retrying(job_id, error_msg, delay_seconds)

    def mark_dlq(self, job_id: int, error_msg: str) -> None:
        """Transitions job to DLQ state (max retries exhausted) and logs dead-letter record."""
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        err_truncated = error_msg[:1000]

        with self.db.get_connection() as c:
            cur = c.execute("SELECT entity_type, entity_id, payload FROM sync_queue WHERE id=?", (job_id,))
            row = cur.fetchone()

            c.execute(
                """
                UPDATE sync_queue 
                SET status='DLQ', attempt_count=attempt_count+1, attempts=attempts+1,
                    last_error=?, error_message=?, completed_at=?, updated_at=?
                WHERE id=?
                """,
                (err_truncated, err_truncated, now_iso, now_iso, job_id),
            )

            if row:
                ent = row["entity_type"]
                eid = row["entity_id"] or str(job_id)
                pay = row["payload"]
                try:
                    c.execute(
                        "INSERT INTO dead_letters (entity_type, rentasst_id, source_id, error_message, error, payload) VALUES (?, ?, ?, ?, ?, ?)",
                        (ent, eid, eid, err_truncated, err_truncated, pay),
                    )
                except Exception:
                    pass

    def mark_failed(self, job_id: int, error_msg: str) -> None:
        """Transitions job to FAILED or DLQ state."""
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        err_truncated = error_msg[:1000]
        with self.db.get_connection() as c:
            cur = c.execute("SELECT attempt_count, max_attempts FROM sync_queue WHERE id=?", (job_id,))
            row = cur.fetchone()
            if row and row["attempt_count"] >= row["max_attempts"]:
                self.mark_dlq(job_id, error_msg)
            else:
                c.execute(
                    """
                    UPDATE sync_queue 
                    SET status='FAILED', attempt_count=attempt_count+1, attempts=attempts+1,
                        last_error=?, error_message=?, completed_at=?, updated_at=?
                    WHERE id=?
                    """,
                    (err_truncated, err_truncated, now_iso, now_iso, job_id),
                )

    def recover_crashed_jobs(self, stale_threshold_seconds: int = 300) -> Dict[str, int]:
        """
        Detects jobs left stuck in PROCESSING state due to process termination, Windows reboots, or worker crashes.
        Transitions eligible jobs to RETRYING or DLQ, purges stale locks, and returns recovery stats.
        """
        stats = {"recovered_retrying": 0, "recovered_dlq": 0, "locks_purged": 0}
        now_dt = datetime.now(timezone.utc)
        now_iso = now_dt.strftime("%Y-%m-%d %H:%M:%S")

        from .lock_manager import LockManager
        lock_mgr = LockManager(self.db_path)
        stats["locks_purged"] = lock_mgr.purge_expired_locks()

        with self.db.get_connection() as c:
            cur = c.execute(
                """
                SELECT id, entity_type, entity_id, company_id, direction, attempt_count, attempts, max_attempts, started_at
                FROM sync_queue 
                WHERE status IN ('PROCESSING', 'Running')
                """
            )
            crashed_jobs = [dict(row) for row in cur.fetchall()]

            for job in crashed_jobs:
                job_id = job["id"]
                started_str = job.get("started_at")
                is_stale = True

                if started_str:
                    try:
                        started_clean = started_str.replace("T", " ").split(".")[0]
                        started_dt = datetime.strptime(started_clean, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                        elapsed = (now_dt - started_dt).total_seconds()
                        if elapsed < stale_threshold_seconds:
                            is_stale = False
                    except Exception:
                        is_stale = True

                if not is_stale:
                    continue

                curr_attempts = (job.get("attempt_count") if job.get("attempt_count") is not None else job.get("attempts", 0)) + 1
                max_attempts = job.get("max_attempts", 3)
                err_msg = f"Recovered from process termination/worker crash (Stuck in PROCESSING for >{stale_threshold_seconds}s)"

                if curr_attempts >= max_attempts:
                    self.mark_dlq(job_id, err_msg)
                    stats["recovered_dlq"] += 1
                else:
                    c.execute(
                        """
                        UPDATE sync_queue 
                        SET status='RETRYING', attempt_count=?, attempts=?, last_error=?, error_message=?,
                            updated_at=?, scheduled_at=?, next_retry_at=?, next_attempt_at=?
                        WHERE id=?
                        """,
                        (curr_attempts, curr_attempts, err_msg, err_msg, now_iso, now_iso, now_iso, now_iso, job_id),
                    )
                    stats["recovered_retrying"] += 1

        if stats["recovered_retrying"] > 0 or stats["recovered_dlq"] > 0:
            log_event(
                "QueueRecovery",
                f"Startup crash recovery completed: {stats['recovered_retrying']} jobs set to RETRYING, {stats['recovered_dlq']} moved to DLQ, {stats['locks_purged']} locks purged.",
                metadata=stats,
            )

        return stats

    def cancel_job(self, job_id: int) -> bool:
        """Cancels a pending or processing job."""
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self.db.get_connection() as c:
            cur = c.execute(
                """
                UPDATE sync_queue 
                SET status='CANCELLED', completed_at=?, updated_at=?
                WHERE id=? AND status IN ('PENDING', 'PROCESSING', 'RETRYING', 'WAITING_FOR_DEPENDENCY', 'Pending', 'Running', 'Waiting', 'Retry')
                """,
                (now_iso, now_iso, job_id),
            )
            return cur.rowcount > 0

    def get_metrics(self) -> Dict[str, int]:
        with self.db.get_connection() as c:
            cur = c.execute("SELECT status, COUNT(*) as cnt FROM sync_queue GROUP BY status")
            stats = {
                "PENDING": 0, "PROCESSING": 0, "SUCCESS": 0, "PARTIAL_SUCCESS": 0,
                "FAILED": 0, "RETRYING": 0, "WAITING_FOR_DEPENDENCY": 0, "DLQ": 0, "CANCELLED": 0,
                "Pending": 0, "Running": 0, "Waiting": 0, "Completed": 0, "Retry": 0
            }
            for row in cur.fetchall():
                st = row["status"]
                stats[st] = row["cnt"]
            return stats

    def retry_failed_jobs(self) -> int:
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self.db.get_connection() as c:
            cur = c.execute(
                """
                UPDATE sync_queue 
                SET status='PENDING', attempt_count=0, attempts=0, last_error=NULL, error_message=NULL,
                    updated_at=?, scheduled_at=?, next_retry_at=?, next_attempt_at=?
                WHERE status IN ('FAILED', 'DLQ', 'Failed')
                """,
                (now_iso, now_iso, now_iso, now_iso),
            )
            return cur.rowcount

    def list_recent_jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.db.get_connection() as c:
            cur = c.execute(
                """
                SELECT id as job_id, id, entity_type, entity_id, company_id, direction,
                       status, COALESCE(attempt_count, attempts) as attempt_count, max_attempts,
                       started_at, completed_at, COALESCE(last_error, error_message) as last_error,
                       COALESCE(next_retry_at, next_attempt_at) as next_retry_at, created_at, updated_at
                FROM sync_queue ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]

