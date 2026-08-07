from fastapi import APIRouter, Depends, Request
from ..services.sync_service import SyncService

router = APIRouter(prefix="/api/sync", tags=["sync"])


def get_sync_service(request: Request) -> SyncService:
    return SyncService(request.app.state.data_dir)


@router.post("/customers")
def sync_customers_endpoint(svc: SyncService = Depends(get_sync_service)):
    stats = svc.execute_sync("customers")
    return {"status": "success", "stats": stats}


@router.post("/equipment")
def sync_equipment_endpoint(svc: SyncService = Depends(get_sync_service)):
    stats = svc.execute_sync("equipment")
    return {"status": "success", "stats": stats}


@router.post("/rental_orders")
def sync_orders_endpoint(svc: SyncService = Depends(get_sync_service)):
    stats = svc.execute_sync("rental_orders")
    return {"status": "success", "stats": stats}


@router.post("/invoices")
def sync_invoices_endpoint(svc: SyncService = Depends(get_sync_service)):
    stats = svc.execute_sync("invoices")
    return {"status": "success", "stats": stats}


@router.post("/payments")
def sync_payments_endpoint(svc: SyncService = Depends(get_sync_service)):
    stats = svc.execute_sync("payments")
    return {"status": "success", "stats": stats}
