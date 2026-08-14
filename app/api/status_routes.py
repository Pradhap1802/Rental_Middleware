from fastapi import APIRouter, Request
from ..services.status_service import StatusService
from ..models.domain import SystemStatusModel

router = APIRouter(prefix="/api/status", tags=["status"])


@router.get("", response_model=SystemStatusModel)
def get_system_status(request: Request):
    data_dir = request.app.state.data_dir
    db_path = getattr(request.app.state, "db_path", f"{data_dir}/state.db")
    svc = StatusService(data_dir, db_path=db_path)
    scheduler = getattr(request.app.state, "scheduler", None)
    worker = getattr(request.app.state, "worker", None)
    ra_client = getattr(request.app.state, "ra_client", None)
    ext_client = getattr(request.app.state, "ext_client", None)
    return svc.get_system_status(
        scheduler_ref=scheduler,
        worker_ref=worker,
        ra_client_ref=ra_client,
        ext_client_ref=ext_client,
    )
