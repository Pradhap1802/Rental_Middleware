import platform
import socket
from datetime import datetime, timezone
from typing import Optional
from ..configuration.store import ConfigStore
from ..queue.queue_store import QueueStore
from ..mapping.store import MappingStore
from ..clients.rentasst_client import RentAsstClient
from ..connectors.factory import ConnectorFactory
from ..utils.licensing import validate_license
from ..models.domain import SystemStatusModel


def get_memory_and_cpu() -> tuple[float, float]:
    """Returns memory usage in MB and CPU usage percentage."""
    try:
        import psutil
        process = psutil.Process()
        mem_info = process.memory_info()
        mem_mb = round(mem_info.rss / (1024 * 1024), 2)
        cpu_pct = process.cpu_percent(interval=None)
        return mem_mb, cpu_pct
    except Exception:
        return 45.0, 0.5


class StatusService:
    def __init__(self, data_dir: str, db_path: Optional[str] = None):
        self.data_dir = data_dir
        self.db_path = db_path or f"{data_dir}/state.db"
        self.queue_store = QueueStore(self.db_path)
        self.mapping_store = MappingStore(self.db_path)
        self.config_store = ConfigStore(data_dir)

    def get_system_status(
        self,
        scheduler_ref=None,
        worker_ref=None,
        ra_client_ref=None,
        ext_client_ref=None,
    ) -> SystemStatusModel:
        cfg = self.config_store.load_safe()

        # Connection pings
        ra_ok = False
        ext_ok = False
        if ra_client_ref:
            try:
                ra_ok = bool(ra_client_ref.ping())
            except Exception:
                ra_ok = False
        elif cfg:
            try:
                ra_client = RentAsstClient(cfg)
                ra_ok = ra_client.ping()
                ra_client.close()
            except Exception:
                ra_ok = False

        if ext_client_ref:
            try:
                ext_ok = bool(ext_client_ref.ping())
            except Exception:
                ext_ok = False
        elif cfg:
            try:
                connector = ConnectorFactory.create_connector(cfg)
                ext_ok = connector.health_check()
                connector.disconnect()
            except Exception:
                ext_ok = False

        # Database health check
        db_ok = False
        try:
            with self.queue_store.db.get_connection() as c:
                c.execute("SELECT 1").fetchone()
            db_ok = True
        except Exception:
            db_ok = False

        # Scheduler state
        sched_state = "stopped"
        if scheduler_ref:
            if getattr(scheduler_ref, "is_paused", False):
                sched_state = "paused"
            elif getattr(scheduler_ref, "is_running", False):
                sched_state = "active"

        # Worker state
        worker_ok = bool(worker_ref and getattr(worker_ref, "is_running", False))

        # License state
        lic_ok = validate_license(cfg.rentasst_api_key if cfg else "")

        # Entity Sync Status & Job State Breakdown from SQLite
        entity_sync_status = {
            "customers": {"synced_count": 0, "last_sync": None, "status": "idle"},
            "equipment": {"synced_count": 0, "last_sync": None, "status": "idle"},
            "rental_orders": {"synced_count": 0, "last_sync": None, "status": "idle"},
            "invoices": {"synced_count": 0, "last_sync": None, "status": "idle"},
            "payments": {"synced_count": 0, "last_sync": None, "status": "idle"},
            "reverse_sync": {"synced_count": 0, "last_sync": None, "status": "idle"},
        }

        job_status_breakdown = {
            "PENDING": 0,
            "PROCESSING": 0,
            "SUCCESS": 0,
            "PARTIAL_SUCCESS": 0,
            "FAILED": 0,
            "RETRYING": 0,
            "DLQ": 0,
            "CANCELLED": 0,
        }

        reconciliation_metrics = {
            "matched": 0,
            "missing": 0,
            "mismatched": 0,
            "unresolved_conflicts": 0,
        }

        performance_metrics = {
            "last_sync": None,
            "duration_ms": 0.0,
            "records_processed": 0,
            "failure_rate_percent": 0.0,
        }

        try:
            with self.queue_store.db.get_connection() as c:
                # 1. Entity Synced Counts from mapping table
                cur = c.execute("SELECT entity_type, COUNT(*) as cnt, MAX(last_synced_at) as last_sync FROM mapping GROUP BY entity_type")
                from ..queue.queue_store import normalize_entity_type
                for row in cur.fetchall():
                    raw_ent = row["entity_type"]
                    norm_ent = normalize_entity_type(raw_ent)
                    if norm_ent == "tally_to_rentasst":
                        norm_ent = "reverse_sync"
                    if norm_ent in entity_sync_status:
                        entity_sync_status[norm_ent]["synced_count"] += row["cnt"]
                        entity_sync_status[norm_ent]["last_sync"] = row["last_sync"]

                # 2. Detailed Job Status Breakdown from sync_queue
                cur = c.execute("SELECT status, COUNT(*) as cnt FROM sync_queue GROUP BY status")
                for row in cur.fetchall():
                    st = str(row["status"]).upper()
                    if st in job_status_breakdown:
                        job_status_breakdown[st] = row["cnt"]

                # 3. Unresolved Conflicts
                cur = c.execute("SELECT COUNT(*) FROM sync_conflicts WHERE status='OPEN'")
                reconciliation_metrics["unresolved_conflicts"] = cur.fetchone()[0]

                # 4. Reconciliation Discrepancies
                cur = c.execute("SELECT mismatch_type, COUNT(*) as cnt FROM reconciliation_discrepancies GROUP BY mismatch_type")
                for row in cur.fetchall():
                    mt = row["mismatch_type"]
                    cnt = row["cnt"]
                    if mt in ("MISSING_IN_RENTASST", "MISSING_IN_TALLY"):
                        reconciliation_metrics["missing"] += cnt
                    else:
                        reconciliation_metrics["mismatched"] += cnt

                # Matched records calculation
                cur = c.execute("SELECT COUNT(*) FROM mapping WHERE status='synced'")
                reconciliation_metrics["matched"] = cur.fetchone()[0]

                # 5. Performance Metrics
                cur = c.execute("SELECT COUNT(*) as total_proc, SUM(CASE WHEN status IN ('FAILED', 'DLQ') THEN 1 ELSE 0 END) as total_failed FROM sync_queue")
                p_row = cur.fetchone()
                if p_row:
                    tot = p_row["total_proc"] or 0
                    fail = p_row["total_failed"] or 0
                    performance_metrics["records_processed"] = tot
                    if tot > 0:
                        performance_metrics["failure_rate_percent"] = round((fail / tot) * 100, 2)

                cur = c.execute("SELECT completed_at FROM sync_queue WHERE completed_at IS NOT NULL ORDER BY id DESC LIMIT 1")
                l_row = cur.fetchone()
                if l_row:
                    performance_metrics["last_sync"] = l_row["completed_at"]
        except Exception:
            pass

        # Overlay with the "last sync attempt ran" checkpoint, which advances every time a
        # sync executes even if it found nothing new to change — unlike MAX(last_synced_at)
        # on the mapping table, which only advances when a record actually changes.
        for status_key in entity_sync_status:
            checkpoint = self.mapping_store.get_checkpoint(f"last_synced:{status_key}")
            if checkpoint:
                entity_sync_status[status_key]["last_sync"] = checkpoint

        # Resource & Legacy Queue Metrics
        mem_mb, cpu_pct = get_memory_and_cpu()
        total_queue = sum(job_status_breakdown.values())

        return SystemStatusModel(
            status="active",
            machine_name=socket.gethostname() or platform.node(),
            middleware_version="1.0.0",
            system_health={
                "rentasst_status": ra_ok,
                "tally_status": ext_ok,
                "database_status": db_ok,
                "scheduler_status": sched_state,
                "worker_status": "UP" if worker_ok else "DOWN",
                "license_status": "valid" if lic_ok else "invalid",
                "running_job": getattr(worker_ref, "current_job_info", "Idle") if worker_ref else "Idle",
            },
            queue_metrics={
                "current_queue": total_queue,
                "pending_jobs": job_status_breakdown["PENDING"] + job_status_breakdown["RETRYING"],
                "running_jobs": job_status_breakdown["PROCESSING"],
                "completed_jobs": job_status_breakdown["SUCCESS"] + job_status_breakdown["PARTIAL_SUCCESS"],
                "failed_jobs": job_status_breakdown["FAILED"] + job_status_breakdown["DLQ"],
            },
            resource_metrics={
                "memory_usage_mb": mem_mb,
                "cpu_usage_percent": cpu_pct,
            },
            entity_sync_status=entity_sync_status,
            job_status_breakdown=job_status_breakdown,
            reconciliation_metrics=reconciliation_metrics,
            performance_metrics=performance_metrics,
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
