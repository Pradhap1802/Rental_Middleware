from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from typing import Optional, List, Dict, Any

from ..mapping.store import MappingStore
from ..sync.conflicts import ConflictDetector

conflict_router = APIRouter(prefix="/api/conflicts", tags=["conflicts"])


class ResolveConflictRequest(BaseModel):
    resolution: str  # 'use_rentasst', 'use_tally', 'ignore'


class BatchResolveConflictRequest(BaseModel):
    ids: List[int]
    resolution: str


def get_detector(request: Request) -> ConflictDetector:
    store = getattr(request.app.state, "mapping_store", None)
    if not store:
        db_path = getattr(request.app.state, "db_path", ".data/state.db")
        store = MappingStore(db_path)
    return ConflictDetector(store)


@conflict_router.get("", response_model=Dict[str, Any])
def list_conflicts(
    request: Request,
    status: Optional[str] = Query(None, description="Filter by status (OPEN, RESOLVED_RENTASST, RESOLVED_TALLY, IGNORED)"),
    entity_type: Optional[str] = Query(None, description="Filter by entity_type"),
):
    detector = get_detector(request)
    conflicts = detector.list_conflicts(status_filter=status, entity_type=entity_type)
    return {"total": len(conflicts), "conflicts": conflicts}


@conflict_router.get("/{id}", response_model=Dict[str, Any])
def get_conflict_detail(id: int, request: Request):
    detector = get_detector(request)
    conflicts = detector.list_conflicts()
    found = next((c for c in conflicts if c["id"] == id), None)
    if not found:
        raise HTTPException(status_code=404, detail=f"Conflict ID #{id} not found")
    return {"conflict": found}


@conflict_router.post("/{id}/resolve", response_model=Dict[str, Any])
def resolve_conflict(id: int, body: ResolveConflictRequest, request: Request):
    detector = get_detector(request)
    res = detector.resolve_conflict(id, body.resolution)
    if not res:
        raise HTTPException(status_code=404, detail=f"Conflict ID #{id} not found")
    return {"success": True, "conflict": res}


@conflict_router.post("/resolve-batch", response_model=Dict[str, Any])
def resolve_batch_conflicts(body: BatchResolveConflictRequest, request: Request):
    detector = get_detector(request)
    resolved_count = 0
    for cid in body.ids:
        res = detector.resolve_conflict(cid, body.resolution)
        if res:
            resolved_count += 1
    return {"success": True, "resolved_count": resolved_count}
