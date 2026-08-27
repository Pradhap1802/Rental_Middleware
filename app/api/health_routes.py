import time
from fastapi import APIRouter, Depends, Request, Response, status
from typing import Dict, Any

from ..mapping.store import MappingStore
from ..security.auth import require_api_key

health_router = APIRouter(prefix="", tags=["health"])


def check_db_health(request: Request) -> Dict[str, Any]:
    db_path = getattr(request.app.state, "db_path", None) or ".data/state.db"
    start_time = time.time()
    try:
        store = getattr(request.app.state, "mapping_store", None) or MappingStore(db_path)
        with store.db.get_connection() as c:
            cur = c.execute("SELECT 1")
            cur.fetchone()
        latency_ms = round((time.time() - start_time) * 1000, 2)
        return {"status": "UP", "latency_ms": latency_ms, "database": db_path}
    except Exception as ex:
        return {"status": "DOWN", "error": str(ex), "database": db_path}


def check_rentasst_health(request: Request) -> Dict[str, Any]:
    ra_client = getattr(request.app.state, "ra_client", None)
    start_time = time.time()
    if not ra_client:
        return {"status": "UNKNOWN", "message": "RentAsst client not initialized"}
    try:
        is_healthy = ra_client.ping()
        latency_ms = round((time.time() - start_time) * 1000, 2)
        return {
            "status": "UP" if is_healthy else "DOWN",
            "url": ra_client.config.rentasst_url,
            "tenant_id": ra_client.config.rentasst_tenant_id,
            "latency_ms": latency_ms,
        }
    except Exception as ex:
        return {"status": "DOWN", "url": getattr(ra_client.config, "rentasst_url", ""), "error": str(ex)}


def check_tally_health(request: Request) -> Dict[str, Any]:
    ext_client = getattr(request.app.state, "ext_client", None)
    start_time = time.time()
    if not ext_client:
        return {"status": "UNKNOWN", "message": "Tally client not initialized"}
    try:
        is_healthy = ext_client.ping()
        latency_ms = round((time.time() - start_time) * 1000, 2)
        return {
            "status": "UP" if is_healthy else "DOWN",
            "url": ext_client.config.external_url,
            "system_type": ext_client.config.external_system_type,
            "latency_ms": latency_ms,
        }
    except Exception as ex:
        return {"status": "DOWN", "url": getattr(ext_client.config, "external_url", ""), "error": str(ex)}


def check_worker_health(request: Request) -> Dict[str, Any]:
    worker = getattr(request.app.state, "worker", None)
    if not worker:
        return {"status": "UNKNOWN", "message": "Worker not initialized"}
    return {
        "status": "UP" if worker.is_running else "DOWN",
        "running": worker.is_running,
        "current_job": getattr(worker, "current_job_info", "Idle"),
        "max_workers": getattr(worker, "max_workers", 1),
    }


def check_scheduler_health(request: Request) -> Dict[str, Any]:
    scheduler = getattr(request.app.state, "scheduler", None)
    if not scheduler:
        return {"status": "UNKNOWN", "message": "Scheduler not initialized"}
    return {
        "status": "UP" if scheduler.is_running else "STOPPED",
        "running": scheduler.is_running,
        "interval_minutes": getattr(scheduler, "interval_minutes", 10),
    }


@health_router.get("/health", response_model=Dict[str, Any], dependencies=[Depends(require_api_key)])
def health_overview(request: Request):
    """Comprehensive health check combining RentAsst, Tally, Database, Worker, and Scheduler status."""
    db_info = check_db_health(request)
    ra_info = check_rentasst_health(request)
    tally_info = check_tally_health(request)
    worker_info = check_worker_health(request)
    sched_info = check_scheduler_health(request)

    overall_status = "UP"
    if db_info["status"] == "DOWN":
        overall_status = "DOWN"
    elif ra_info.get("status") == "DOWN" or tally_info.get("status") == "DOWN":
        overall_status = "DEGRADED"

    return {
        "status": overall_status,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "components": {
            "database": db_info,
            "rentasst_api": ra_info,
            "tally_prime": tally_info,
            "worker": worker_info,
            "scheduler": sched_info,
        },
    }


@health_router.get("/health/live", response_model=Dict[str, Any])
def liveness_probe():
    """Liveness probe: Returns HTTP 200 UP as long as middleware application process is alive."""
    return {"status": "UP"}


@health_router.get("/health/ready", response_model=Dict[str, Any])
def readiness_probe(request: Request, response: Response):
    """Readiness probe: Returns HTTP 200 if Database and Worker pool are functional, HTTP 503 if unavailable."""
    db_info = check_db_health(request)
    worker_info = check_worker_health(request)

    if db_info["status"] == "DOWN":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "DOWN", "reason": "Database connection failed", "database": db_info}

    return {"status": "READY", "database": db_info, "worker": worker_info}


@health_router.get("/health/rentasst", response_model=Dict[str, Any], dependencies=[Depends(require_api_key)])
def rentasst_health(request: Request):
    """Dedicated probe for RentAsst API connectivity."""
    return check_rentasst_health(request)


@health_router.get("/health/tally", response_model=Dict[str, Any], dependencies=[Depends(require_api_key)])
def tally_health(request: Request):
    """Dedicated probe for Tally Prime XML server connectivity."""
    return check_tally_health(request)
