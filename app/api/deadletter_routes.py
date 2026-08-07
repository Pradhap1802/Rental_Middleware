from fastapi import APIRouter, Request
from ..mapping.store import MappingStore

router = APIRouter(prefix="/api/deadletters", tags=["deadletters"])


@router.get("")
def get_deadletters(limit: int = 50, request: Request = None):
    data_dir = request.app.state.data_dir if request else ".data"
    store = MappingStore(f"{data_dir}/state.db")
    return store.list_dead_letters(limit=limit)


@router.post("/clear")
def clear_deadletters(request: Request = None):
    data_dir = request.app.state.data_dir if request else ".data"
    store = MappingStore(f"{data_dir}/state.db")
    cleared = store.clear_dead_letters()
    return {"status": "success", "cleared_count": cleared}
