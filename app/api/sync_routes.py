from typing import Optional
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from ..services.sync_service import SyncService

router = APIRouter(prefix="/api/sync", tags=["sync"])


def get_sync_service(request: Request) -> SyncService:
    return SyncService(request.app.state.data_dir)


@router.post("/customers")
def sync_customers_endpoint(from_date: Optional[str] = None, to_date: Optional[str] = None, svc: SyncService = Depends(get_sync_service)):
    try:
        stats = svc.execute_sync("customers", from_date=from_date, to_date=to_date)
        return {"status": "success", "stats": stats}
    except Exception as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(e)})


@router.post("/units")
@router.post("/asset_units")
def sync_units_endpoint(svc: SyncService = Depends(get_sync_service)):
    try:
        stats = svc.execute_sync("units")
        return {"status": "success", "stats": stats}
    except Exception as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(e)})


@router.post("/equipment")
def sync_equipment_endpoint(from_date: Optional[str] = None, to_date: Optional[str] = None, svc: SyncService = Depends(get_sync_service)):
    try:
        stats = svc.execute_sync("equipment", from_date=from_date, to_date=to_date)
        return {"status": "success", "stats": stats}
    except Exception as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(e)})


@router.post("/rental_orders")
@router.post("/orders")
def sync_rental_orders_endpoint(from_date: Optional[str] = None, to_date: Optional[str] = None, svc: SyncService = Depends(get_sync_service)):
    try:
        stats = svc.execute_sync("rental_orders", from_date=from_date, to_date=to_date)
        return {"status": "success", "stats": stats}
    except Exception as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(e)})


@router.post("/invoices")
def sync_invoices_endpoint(from_date: Optional[str] = None, to_date: Optional[str] = None, svc: SyncService = Depends(get_sync_service)):
    try:
        stats = svc.execute_sync("invoices", from_date=from_date, to_date=to_date)
        return {"status": "success", "stats": stats}
    except Exception as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(e)})


@router.post("/payments")
def sync_payments_endpoint(from_date: Optional[str] = None, to_date: Optional[str] = None, svc: SyncService = Depends(get_sync_service)):
    try:
        stats = svc.execute_sync("payments", from_date=from_date, to_date=to_date)
        return {"status": "success", "stats": stats}
    except Exception as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(e)})


@router.post("/tally_to_rentasst")
def sync_tally_to_rentasst_endpoint(from_date: Optional[str] = None, to_date: Optional[str] = None, svc: SyncService = Depends(get_sync_service)):
    try:
        stats = svc.execute_sync("tally_to_rentasst", from_date=from_date, to_date=to_date)
        return {"status": "success", "stats": stats}
    except Exception as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(e)})
