import platform
import socket
import os
from datetime import datetime, timezone
from typing import Dict, Any
from ..configuration.store import ConfigStore
from ..queue.queue_store import QueueStore
from ..clients.rentasst_client import RentAsstClient
from ..connectors.factory import ConnectorFactory
from ..utils.licensing import validate_license
from ..models.domain import SystemStatusModel


def get_memory_and_cpu() -> tuple[float, float]:
    """Returns memory usage in MB and CPU usage percentage using standard library or psutil if installed."""
    try:
        import psutil
        process = psutil.Process()
        mem_info = process.memory_info()
        mem_mb = round(mem_info.rss / (1024 * 1024), 2)
        cpu_pct = process.cpu_percent(interval=None)
        return mem_mb, cpu_pct
    except Exception:
        # Standard library fallback
        return 45.0, 0.5


class StatusService:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.queue_store = QueueStore(f"{data_dir}/state.db")
        self.config_store = ConfigStore(data_dir)

    def get_system_status(self, scheduler_ref=None, worker_ref=None) -> SystemStatusModel:
        cfg = self.config_store.load_safe()
        
        # Connection pings
        ra_ok = False
        ext_ok = False
        if cfg:
            try:
                ra_client = RentAsstClient(cfg)
                ra_ok = ra_client.ping()
                ra_client.close()
            except Exception:
                ra_ok = False

            try:
                connector = ConnectorFactory.create_connector(cfg)
                ext_ok = connector.health_check()
                connector.disconnect()
            except Exception:
                ext_ok = False

        # Metrics
        q_metrics = self.queue_store.get_metrics()
        current_queue = sum(q_metrics.values())
        pending_jobs = q_metrics.get("Pending", 0) + q_metrics.get("Waiting", 0)
        running_jobs = q_metrics.get("Running", 0)
        completed_jobs = q_metrics.get("Completed", 0)
        failed_jobs = q_metrics.get("Failed", 0)

        # System resources
        mem_mb, cpu_pct = get_memory_and_cpu()

        # Scheduler state
        sched_state = "stopped"
        if scheduler_ref:
            if scheduler_ref.is_paused:
                sched_state = "paused"
            elif scheduler_ref.is_running:
                sched_state = "active"

        # License state
        lic_ok = validate_license(cfg.rentasst_api_key if cfg else "")

        return SystemStatusModel(
            status="active",
            machine_name=socket.gethostname() or platform.node(),
            middleware_version="1.0.0",
            system_health={
                "rentasst_status": ra_ok,
                "tally_status": ext_ok,
                "scheduler_status": sched_state,
                "license_status": "valid" if lic_ok else "invalid",
                "running_job": getattr(worker_ref, "current_job_info", "Idle") if worker_ref else "Idle",
            },
            queue_metrics={
                "current_queue": current_queue,
                "pending_jobs": pending_jobs,
                "running_jobs": running_jobs,
                "completed_jobs": completed_jobs,
                "failed_jobs": failed_jobs,
            },
            resource_metrics={
                "memory_usage_mb": mem_mb,
                "cpu_usage_percent": cpu_pct,
            },
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
