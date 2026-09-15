from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from ..mapping.store import MappingStore


class ConflictDetector:
    """
    Records, lists, and resolves bidirectional field conflicts in the SQLite
    sync_conflicts table (see app/api/conflict_routes.py for the API surface).
    """
    def __init__(self, store: MappingStore):
        self.store = store

    def record_conflict(
        self,
        entity_type: str,
        entity_id: str,
        field_name: str,
        rentasst_value: str,
        tally_value: str,
        rentasst_mod_time: Optional[str] = None,
        tally_mod_time: Optional[str] = None,
        company_id: str = "default",
    ) -> Dict[str, Any]:
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self.store.db.get_connection() as c:
            cur_dup = c.execute(
                """
                SELECT id FROM sync_conflicts 
                WHERE entity_type=? AND entity_id=? AND field_name=? AND status='OPEN'
                """,
                (entity_type, entity_id, field_name),
            )
            existing = cur_dup.fetchone()
            if existing:
                cid = existing["id"]
                c.execute(
                    """
                    UPDATE sync_conflicts 
                    SET rentasst_value=?, tally_value=?, rentasst_modified_at=?, tally_modified_at=?
                    WHERE id=?
                    """,
                    (rentasst_value, tally_value, rentasst_mod_time, tally_mod_time, cid),
                )
                return {"id": cid, "entity_type": entity_type, "entity_id": entity_id, "field_name": field_name, "status": "OPEN"}

            cur = c.execute(
                """
                INSERT INTO sync_conflicts (
                    entity_type, entity_id, company_id, field_name,
                    rentasst_value, tally_value, rentasst_modified_at, tally_modified_at,
                    status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
                """,
                (
                    entity_type, entity_id, company_id, field_name,
                    rentasst_value, tally_value, rentasst_mod_time, tally_mod_time,
                    now_iso,
                ),
            )
            return {
                "id": cur.lastrowid,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "field_name": field_name,
                "status": "OPEN",
            }

    def list_conflicts(self, status_filter: Optional[str] = None, entity_type: Optional[str] = None) -> List[Dict[str, Any]]:
        query = "SELECT * FROM sync_conflicts WHERE 1=1"
        params = []
        if status_filter:
            query += " AND status=?"
            params.append(status_filter)
        if entity_type:
            query += " AND entity_type=?"
            params.append(entity_type)

        query += " ORDER BY id DESC"
        with self.store.db.get_connection() as c:
            cur = c.execute(query, tuple(params))
            return [dict(row) for row in cur.fetchall()]

    def resolve_conflict(self, conflict_id: int, resolution: str) -> Optional[Dict[str, Any]]:
        """
        Resolves conflict:
        resolution in ('use_rentasst', 'RESOLVED_RENTASST') -> sets status='RESOLVED_RENTASST'
        resolution in ('use_tally', 'RESOLVED_TALLY') -> sets status='RESOLVED_TALLY'
        resolution in ('ignore', 'IGNORED') -> sets status='IGNORED'
        """
        norm_res = (resolution or "").strip().lower()
        new_status = "RESOLVED_RENTASST" if norm_res in ("use_rentasst", "resolved_rentasst", "rentasst") else \
                     "RESOLVED_TALLY" if norm_res in ("use_tally", "resolved_tally", "tally") else \
                     "IGNORED"

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self.store.db.get_connection() as c:
            c.execute(
                "UPDATE sync_conflicts SET status=?, resolved_at=? WHERE id=?",
                (new_status, now_iso, conflict_id),
            )
            cur = c.execute("SELECT * FROM sync_conflicts WHERE id=?", (conflict_id,))
            row = cur.fetchone()
            return dict(row) if row else None
