from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


@router.post("/pause")
def pause_scheduler(request: Request):
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler:
        scheduler.pause()
        return {"status": "success", "message": "Scheduler paused"}
    return {"status": "error", "message": "Scheduler not available"}


@router.post("/resume")
def resume_scheduler(request: Request):
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler:
        scheduler.resume()
        return {"status": "success", "message": "Scheduler resumed"}
    return {"status": "error", "message": "Scheduler not available"}


@router.post("/trigger")
def trigger_scheduler(request: Request, entity_type: str = None):
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler:
        count = scheduler.trigger_manual_sync(entity_type)
        return {"status": "success", "message": f"Triggered sync for {entity_type or 'all entities'}", "jobs_enqueued": count}
    return {"status": "error", "message": "Scheduler not available"}
