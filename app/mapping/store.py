import json
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from ..database.connection import DatabaseManager


from ..utils.cache import TTLCache


class MappingStore:
    """Repository managing mappings, checkpoints, sync history, and dead letter records."""
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.db = DatabaseManager(db_path)
        self.cache = TTLCache(default_ttl_seconds=300)

    def generate_integration_key(
        self,
        entity_type: str,
        source_id: str,
        source_system: str = "rentasst",
        source_company_id: str = "default",
    ) -> str:
        return f"{source_system}:{source_company_id}:{entity_type}:{source_id}"

    # --- Enterprise Multi-Company Repository Methods ---

    def save_mapping(
        self,
        entity_type: str,
        source_id: str,
        target_id: str,
        source_system: str = "rentasst",
        source_company_id: str = "default",
        target_system: str = "tally",
        target_company_id: str = "default",
        integration_key: Optional[str] = None,
        last_synced_hash: Optional[str] = None,
        last_source_modified_at: Optional[str] = None,
        last_target_modified_at: Optional[str] = None,
        sync_version: int = 1,
        status: str = "synced",
        tally_guid: Optional[str] = None,
    ) -> None:
        key = integration_key or self.generate_integration_key(entity_type, source_id, source_system, source_company_id)
        guid = tally_guid or target_id
        # For default company, keep legacy rentasst_id equal to source_id for 100% backward compatibility.
        # For multi-company, scope rentasst_id with source_company_id to prevent SQLite primary key collisions.
        rentasst_key = source_id if (not source_company_id or source_company_id == "default") else f"{source_company_id}:{source_id}"

        with self.db.get_connection() as c:
            c.execute(
                """
                INSERT INTO mapping (
                    entity_type, source_system, source_company_id, source_id,
                    target_system, target_company_id, target_id, integration_key,
                    last_synced_hash, last_source_modified_at, last_target_modified_at,
                    last_synced_at, sync_version, status,
                    rentasst_id, external_id, tally_guid, last_hash, last_sync, last_attempt
                )
                VALUES (
                    ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    CURRENT_TIMESTAMP, ?, ?,
                    ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                ON CONFLICT(entity_type, rentasst_id) DO UPDATE SET
                    source_system=excluded.source_system,
                    source_company_id=excluded.source_company_id,
                    source_id=excluded.source_id,
                    target_system=excluded.target_system,
                    target_company_id=excluded.target_company_id,
                    target_id=excluded.target_id,
                    integration_key=excluded.integration_key,
                    last_synced_hash=excluded.last_synced_hash,
                    last_source_modified_at=COALESCE(excluded.last_source_modified_at, mapping.last_source_modified_at),
                    last_target_modified_at=COALESCE(excluded.last_target_modified_at, mapping.last_target_modified_at),
                    last_synced_at=CURRENT_TIMESTAMP,
                    sync_version=mapping.sync_version + 1,
                    status=excluded.status,
                    external_id=excluded.external_id,
                    tally_guid=excluded.tally_guid,
                    last_hash=excluded.last_synced_hash,
                    last_sync=CURRENT_TIMESTAMP,
                    last_attempt=CURRENT_TIMESTAMP
                """,
                (
                    entity_type, source_system, source_company_id, source_id,
                    target_system, target_company_id, target_id, key,
                    last_synced_hash, last_source_modified_at, last_target_modified_at,
                    sync_version, status,
                    rentasst_key, target_id, guid, last_synced_hash
                ),
            )
            cache_key = f"map:{source_company_id}:{entity_type}:{source_id}"
            self.cache.invalidate(cache_key)

    def save(
        self,
        entity_type: str,
        rentasst_id: str,
        external_id: str,
        tally_guid: Optional[str] = None,
        sync_version: int = 1,
        last_hash: Optional[str] = None,
        status: str = "synced",
        source_system: str = "rentasst",
        source_company_id: str = "default",
        target_system: str = "tally",
        target_company_id: str = "default",
    ) -> None:
        self.save_mapping(
            entity_type=entity_type,
            source_id=rentasst_id,
            target_id=external_id,
            source_system=source_system,
            source_company_id=source_company_id,
            target_system=target_system,
            target_company_id=target_company_id,
            last_synced_hash=last_hash,
            sync_version=sync_version,
            status=status,
            tally_guid=tally_guid,
        )

    def find_by_integration_key(self, integration_key: str) -> Optional[Dict[str, Any]]:
        if not integration_key:
            return None
        with self.db.get_connection() as c:
            cur = c.execute(
                """
                SELECT entity_type, source_system, source_company_id, source_id,
                       target_system, target_company_id, target_id, integration_key,
                       last_synced_hash, last_source_modified_at, last_target_modified_at,
                       last_synced_at, sync_version, status, rentasst_id, external_id, tally_guid, last_hash
                FROM mapping 
                WHERE integration_key=?
                ORDER BY sync_version DESC LIMIT 1
                """,
                (integration_key,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def prefetch_mappings(
        self,
        entity_type: str,
        source_ids: List[str],
        source_company_id: str = "default",
    ) -> Dict[str, Dict[str, Any]]:
        """
        High-performance batch prefetching: Loads multiple entity mappings in a single SQL query,
        populating the in-memory TTL cache to eliminate N+1 database roundtrips.
        """
        if not source_ids:
            return {}
        
        results = {}
        unique_ids = list(set([str(sid).strip() for sid in source_ids if sid]))
        if not unique_ids:
            return {}

        with self.db.get_connection() as c:
            placeholders = ",".join(["?"] * len(unique_ids))
            query = f"""
                SELECT entity_type, source_system, source_company_id, source_id,
                       target_system, target_company_id, target_id, integration_key,
                       last_synced_hash, last_source_modified_at, last_target_modified_at,
                       last_synced_at, sync_version, status, rentasst_id, external_id, tally_guid, last_hash
                FROM mapping
                WHERE source_company_id=? AND entity_type=? AND (source_id IN ({placeholders}) OR rentasst_id IN ({placeholders}))
            """
            params = [source_company_id, entity_type] + unique_ids + unique_ids
            cur = c.execute(query, tuple(params))
            for row in cur.fetchall():
                row_dict = dict(row)
                sid = row_dict.get("source_id") or row_dict.get("rentasst_id")
                if sid:
                    results[sid] = row_dict
                    cache_key = f"map:{source_company_id}:{entity_type}:{sid}"
                    self.cache.set(cache_key, row_dict)

        return results

    def find_mapping(
        self,
        entity_type: str,
        source_id: str,
        source_system: str = "rentasst",
        source_company_id: str = "default",
    ) -> Optional[Dict[str, Any]]:
        cache_key = f"map:{source_company_id}:{entity_type}:{source_id}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        with self.db.get_connection() as c:
            cur = c.execute(
                """
                SELECT entity_type, source_system, source_company_id, source_id,
                       target_system, target_company_id, target_id, integration_key,
                       last_synced_hash, last_source_modified_at, last_target_modified_at,
                       last_synced_at, sync_version, status, rentasst_id, external_id, tally_guid, last_hash
                FROM mapping 
                WHERE source_company_id=? AND entity_type=? AND (source_id=? OR rentasst_id=?)
                ORDER BY sync_version DESC LIMIT 1
                """,
                (source_company_id, entity_type, source_id, source_id),
            )
            row = cur.fetchone()
            res = dict(row) if row else None
            if res:
                self.cache.set(cache_key, res)
            return res

    def find_by_target(
        self,
        entity_type: str,
        target_id: str,
        target_system: str = "tally",
        target_company_id: str = "default",
    ) -> Optional[Dict[str, Any]]:
        with self.db.get_connection() as c:
            cur = c.execute(
                """
                SELECT entity_type, source_system, source_company_id, source_id,
                       target_system, target_company_id, target_id, integration_key,
                       last_synced_hash, last_source_modified_at, last_target_modified_at,
                       last_synced_at, sync_version, status, rentasst_id, external_id, tally_guid, last_hash
                FROM mapping
                WHERE target_company_id=? AND entity_type=? AND target_system=? AND (target_id=? OR external_id=? OR tally_guid=?)
                ORDER BY sync_version DESC LIMIT 1
                """,
                (target_company_id, entity_type, target_system, target_id, target_id, target_id),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def find(self, entity_type: str, rentasst_id: str) -> Optional[Dict[str, Any]]:
        return self.find_mapping(entity_type, rentasst_id)

    def update(self, entity_type: str, rentasst_id: str, **kwargs) -> None:
        if not kwargs:
            return
        fields = []
        values = []
        for k, v in kwargs.items():
            fields.append(f"{k}=?")
            values.append(v)
        fields.append("last_attempt=CURRENT_TIMESTAMP")
        query = f"UPDATE mapping SET {', '.join(fields)} WHERE entity_type=? AND (rentasst_id=? OR source_id=?)"
        values.extend([entity_type, rentasst_id, rentasst_id])
        with self.db.get_connection() as c:
            c.execute(query, tuple(values))

    def delete(self, entity_type: str, rentasst_id: str) -> bool:
        with self.db.get_connection() as c:
            cur = c.execute(
                "DELETE FROM mapping WHERE entity_type=? AND (rentasst_id=? OR source_id=?)",
                (entity_type, rentasst_id, rentasst_id),
            )
            deleted = cur.rowcount > 0
        if deleted:
            # find_mapping()/save_mapping() share this cache keyed on
            # (source_company_id, entity_type, source_id) — save_mapping() already
            # invalidates its own key on write, but delete() never did. Confirmed
            # live: is_tally_voucher_duplicate()'s self-heal path calls find_mapping()
            # (populating the cache) right before delete()-ing that same stale
            # mapping — without invalidating here, the very next find_mapping() call
            # for the same key served the just-deleted row back out of cache for up
            # to the TTL, making a genuinely-deleted record look like it still exists.
            # rentasst_id here is ambiguous between source_id and target_id (delete()
            # matches either), so both possible cache keys are cleared; a miss on one
            # is harmless.
            self.cache.invalidate(f"map:default:{entity_type}:{rentasst_id}")
        return deleted

    def exists(
        self,
        entity_type: str,
        source_id: str,
        source_system: str = "rentasst",
        source_company_id: str = "default",
    ) -> bool:
        return self.find_mapping(entity_type, source_id, source_system, source_company_id) is not None

    def is_duplicate(
        self,
        entity_type: str,
        rentasst_id: str,
        current_hash: Optional[str] = None,
        source_system: str = "rentasst",
        source_company_id: str = "default",
    ) -> bool:
        """Prevents duplicate voucher creation if payload hash is unchanged."""
        mapping = self.find_mapping(entity_type, rentasst_id, source_system, source_company_id)
        if not mapping:
            return False
        stored_hash = mapping.get("last_synced_hash") or mapping.get("last_hash")
        if current_hash and stored_hash == current_hash and mapping.get("status") == "synced":
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

    def get_external_id(
        self,
        entity_type: str,
        rentasst_id: str,
        source_system: str = "rentasst",
        source_company_id: str = "default",
    ) -> Optional[str]:
        mapping = self.find_mapping(entity_type, rentasst_id, source_system, source_company_id)
        if mapping:
            return mapping.get("target_id") or mapping.get("external_id")
        return None

    def get_rentasst_id(
        self,
        entity_type: str,
        external_id: str,
        target_system: str = "tally",
        target_company_id: str = "default",
    ) -> Optional[str]:
        mapping = self.find_by_target(entity_type, external_id, target_system, target_company_id)
        if mapping:
            return mapping.get("source_id") or mapping.get("rentasst_id")
        return None

    def upsert_mapping(self, entity_type: str, rentasst_id: str, external_id: str) -> None:
        self.save(entity_type, rentasst_id, external_id)

    def set_checkpoint(self, entity_type: str, timestamp: str) -> None:
        with self.db.get_connection() as c:
            try:
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
            except Exception:
                pass
            try:
                c.execute(
                    """
                    INSERT INTO checkpoints (entity_type, last_sync_at)
                    VALUES (?, ?)
                    ON CONFLICT(entity_type) DO UPDATE SET last_sync_at=excluded.last_sync_at
                    """,
                    (entity_type, timestamp),
                )
            except Exception:
                pass

    def get_checkpoint(self, entity_type: str) -> Optional[str]:
        with self.db.get_connection() as c:
            cur = c.execute("SELECT last_sync_at FROM sync_checkpoint WHERE entity_type=?", (entity_type,))
            row = cur.fetchone()
            if row:
                return row["last_sync_at"]
            cur = c.execute("SELECT last_sync_at FROM checkpoints WHERE entity_type=?", (entity_type,))
            row = cur.fetchone()
            return row["last_sync_at"] if row else None

    def add_dead_letter(
        self,
        entity_type: str,
        source_id: str,
        error: str,
        payload: Optional[str] = None,
        job_id: Optional[int] = None,
        entity_id: str = "",
        company_id: str = "default",
        source_system: str = "rentasst",
        target_system: str = "tally",
        error_type: Optional[str] = None,
        stack_trace: Optional[str] = None,
        attempt_count: int = 1,
    ) -> int:
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        err_truncated = error[:2000]
        pay_str = payload or ""
        ent_id = entity_id or source_id or ""
        err_t = error_type or "SyncError"

        with self.db.get_connection() as c:
            cur = c.execute(
                """
                INSERT INTO dead_letters (
                    job_id, entity_type, entity_id, rentasst_id, source_id, company_id,
                    source_system, target_system, payload, error_type, error_message, error,
                    stack_trace, attempt_count, status, first_failed_at, last_failed_at, failed_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?)
                """,
                (
                    job_id, entity_type, ent_id, ent_id, ent_id, company_id,
                    source_system, target_system, pay_str, err_t, err_truncated, err_truncated,
                    stack_trace, attempt_count, now_iso, now_iso, now_iso, now_iso
                ),
            )
            return cur.lastrowid

    def get_dead_letter(self, dl_id: int) -> Optional[Dict[str, Any]]:
        with self.db.get_connection() as c:
            cur = c.execute(
                """
                SELECT id as dl_id, id, job_id, entity_type, entity_id, COALESCE(source_id, rentasst_id) as source_id,
                       company_id, source_system, target_system, payload, error_type,
                       COALESCE(error_message, error) as error_message, stack_trace, attempt_count,
                       status, COALESCE(first_failed_at, created_at) as first_failed_at,
                       COALESCE(last_failed_at, failed_at, created_at) as last_failed_at, created_at
                FROM dead_letters WHERE id=?
                """,
                (dl_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def list_dead_letters(
        self,
        entity_type: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        with self.db.get_connection() as c:
            query = """
                SELECT id as dl_id, id, job_id, entity_type, entity_id, COALESCE(source_id, rentasst_id) as source_id,
                       company_id, source_system, target_system, payload, error_type,
                       COALESCE(error_message, error) as error_message, stack_trace, attempt_count,
                       status, COALESCE(first_failed_at, created_at) as first_failed_at,
                       COALESCE(last_failed_at, failed_at, created_at) as last_failed_at, created_at
                FROM dead_letters
            """
            where_clauses = []
            params = []
            if entity_type:
                where_clauses.append("entity_type=?")
                params.append(entity_type)
            if status:
                where_clauses.append("status=?")
                params.append(status)
            if where_clauses:
                query += " WHERE " + " AND ".join(where_clauses)
            query += " ORDER BY id DESC LIMIT ?"
            params.append(limit)

            cur = c.execute(query, tuple(params))
            return [dict(row) for row in cur.fetchall()]

    def mark_dead_letter_status(self, dl_id: int, status: str) -> bool:
        """Sets status to 'RESOLVED' or 'IGNORED' for a DLQ entry."""
        with self.db.get_connection() as c:
            cur = c.execute("UPDATE dead_letters SET status=? WHERE id=?", (status.upper(), dl_id))
            return cur.rowcount > 0

    def requeue_dead_letter(self, dl_id: int) -> bool:
        """Re-enqueues a DLQ item back into sync_queue as PENDING and marks DLQ status RESOLVED."""
        item = self.get_dead_letter(dl_id)
        if not item:
            return False

        from ..queue.queue_store import QueueStore
        q_store = QueueStore(self.db_path)
        payload_obj = None
        if item.get("payload"):
            try:
                payload_obj = json.loads(item["payload"])
            except Exception:
                payload_obj = {"raw_payload": item["payload"]}

        job_id = q_store.enqueue(
            entity_type=item["entity_type"],
            payload=payload_obj,
            priority=True,
            entity_id=item.get("entity_id") or item.get("source_id") or "",
            company_id=item.get("company_id") or "default",
            direction="forward",
        )
        if job_id:
            self.mark_dead_letter_status(dl_id, "RESOLVED")
            return True
        return False

    def requeue_batch_dead_letters(self, dl_ids: List[int]) -> Dict[str, Any]:
        requeued = 0
        failed = 0
        for dl_id in dl_ids:
            if self.requeue_dead_letter(dl_id):
                requeued += 1
            else:
                failed += 1
        return {"requeued_count": requeued, "failed_count": failed}

    def requeue_all_dead_letters(self) -> int:
        pending_items = self.list_dead_letters(status="PENDING", limit=1000)
        requeued = 0
        for item in pending_items:
            if self.requeue_dead_letter(item["id"]):
                requeued += 1
        return requeued

    def clear_dead_letters(self, entity_type: Optional[str] = None) -> int:
        count = 0
        with self.db.get_connection() as c:
            if entity_type:
                try:
                    cur = c.execute("DELETE FROM dead_letters WHERE entity_type=?", (entity_type,))
                    count = cur.rowcount
                except Exception:
                    pass
            else:
                try:
                    cur = c.execute("DELETE FROM dead_letters")
                    count = cur.rowcount
                except Exception:
                    pass
            return count

