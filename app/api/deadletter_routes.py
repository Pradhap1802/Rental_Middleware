from typing import List, Optional
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from ..mapping.store import MappingStore

router = APIRouter(prefix="/api/deadletters", tags=["deadletters"])


class BatchRetryRequest(BaseModel):
    ids: List[int]


def get_store(request: Request) -> MappingStore:
    data_dir = getattr(request.app.state, "data_dir", ".data") if request and hasattr(request, "app") else ".data"
    db_path = f"{data_dir}/state.db" if not str(data_dir).endswith(".db") else str(data_dir)
    return MappingStore(db_path)


@router.get("")
def list_deadletters(
    entity_type: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    request: Request = None,
):
    store = get_store(request)
    return store.list_dead_letters(entity_type=entity_type, status=status, limit=limit)


@router.get("/{id}")
def get_deadletter_detail(id: int, request: Request = None):
    store = get_store(request)
    item = store.get_dead_letter(id)
    if not item:
        raise HTTPException(status_code=404, detail=f"Dead letter record #{id} not found")
    return item


@router.post("/{id}/retry")
def retry_deadletter(id: int, request: Request = None):
    store = get_store(request)
    requeued = store.requeue_dead_letter(id)
    if not requeued:
        raise HTTPException(status_code=400, detail=f"Failed to requeue dead letter record #{id}")
    return {"status": "success", "requeued_id": id}


@router.post("/retry-batch")
def retry_batch_deadletters(body: BatchRetryRequest, request: Request = None):
    store = get_store(request)
    res = store.requeue_batch_dead_letters(body.ids)
    return {"status": "success", **res}


@router.post("/retry-all")
def retry_all_deadletters(request: Request = None):
    store = get_store(request)
    requeued_count = store.requeue_all_dead_letters()
    return {"status": "success", "requeued_count": requeued_count}


@router.post("/{id}/ignore")
def ignore_deadletter(id: int, request: Request = None):
    store = get_store(request)
    ok = store.mark_dead_letter_status(id, "IGNORED")
    return {"status": "success" if ok else "failed", "dl_id": id, "action": "ignored"}


@router.post("/{id}/resolve")
def resolve_deadletter(id: int, request: Request = None):
    store = get_store(request)
    ok = store.mark_dead_letter_status(id, "RESOLVED")
    return {"status": "success" if ok else "failed", "dl_id": id, "action": "resolved"}


@router.post("/clear")
def clear_deadletters(request: Request = None):
    store = get_store(request)
    cleared = store.clear_dead_letters()
    return {"status": "success", "cleared_count": cleared}
