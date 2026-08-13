from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from ..mapping.store import MappingStore
from .ownership import get_field_owner


class ConflictDetector:
    """
    Bidirectional Conflict Detection & Resolution Engine.
    Detects when both RentAsst and Tally modified a record since last_synced_at.
    Prevents silent overwrites of conflicting fields and records entries in SQLite sync_conflicts table.
    """
    def __init__(self, store: MappingStore):
        self.store = store

    def detect_and_record_conflicts(
        self,
        entity_type: str,
        entity_id: str,
        rentasst_data: Dict[str, Any],
        tally_data: Dict[str, Any],
        last_synced_at: Optional[str] = None,
        rentasst_mod_time: Optional[str] = None,
        tally_mod_time: Optional[str] = None,
        company_id: str = "default",
    ) -> List[Dict[str, Any]]:
        """
        Compares rentasst_data vs tally_data for conflicting field changes.
        If both modified since last_synced_at and field values differ, records an open conflict in DB.
        Returns list of open conflicts.
        """
        conflicts = []
        if not rentasst_data or not tally_data:
            return conflicts

        for field_name, ra_val in rentasst_data.items():
            if field_name in ("id", "company_id", "created_at", "updated_at"):
                continue

            tally_val = tally_data.get(field_name)
            if tally_val is None:
                continue

            str_ra = str(ra_val).strip() if ra_val is not None else ""
            str_tally = str(tally_val).strip() if tally_val is not None else ""

            if str_ra and str_tally and str_ra != str_tally:
                conflict_entry = self.record_conflict(
                    entity_type=entity_type,
                    entity_id=entity_id,
                    field_name=field_name,
                    rentasst_value=str_ra,
                    tally_value=str_tally,
                    rentasst_mod_time=rentasst_mod_time,
                    tally_mod_time=tally_mod_time,
                    company_id=company_id,
                )
                conflicts.append(conflict_entry)

        return conflicts

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
