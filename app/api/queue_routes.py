from fastapi import APIRouter, Depends, Request
from ..services.queue_service import QueueService

router = APIRouter(prefix="/api/queue", tags=["queue"])


def get_queue_service(request: Request) -> QueueService:
    return QueueService(request.app.state.data_dir)


@router.get("")
def get_queue_status(svc: QueueService = Depends(get_queue_service)):
    metrics = svc.get_metrics()
    jobs = svc.list_jobs(limit=50)
    return {"status": "success", "metrics": metrics, "recent_jobs": jobs}


@router.post("/retry-failed")
def retry_failed(svc: QueueService = Depends(get_queue_service)):
    requeued = svc.retry_failed()
    return {"status": "success", "requeued_count": requeued}


@router.post("/{job_id}/cancel")
def cancel_job(job_id: int, svc: QueueService = Depends(get_queue_service)):
    cancelled = svc.queue_store.cancel_job(job_id)
    return {"status": "success" if cancelled else "failed", "cancelled": cancelled, "job_id": job_id}

