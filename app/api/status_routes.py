from fastapi import APIRouter, Request
from ..services.status_service import StatusService
from ..models.domain import SystemStatusModel

router = APIRouter(prefix="/api/status", tags=["status"])


@router.get("", response_model=SystemStatusModel)
def get_system_status(request: Request):
    data_dir = request.app.state.data_dir
    svc = StatusService(data_dir)
    scheduler = getattr(request.app.state, "scheduler", None)
    worker = getattr(request.app.state, "worker", None)
    return svc.get_system_status(scheduler_ref=scheduler, worker_ref=worker)
