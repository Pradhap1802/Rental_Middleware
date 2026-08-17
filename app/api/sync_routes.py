from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from ..services.sync_service import SyncService

router = APIRouter(prefix="/api/sync", tags=["sync"])


def get_sync_service(request: Request) -> SyncService:
    return SyncService(request.app.state.data_dir)


@router.post("/customers")
def sync_customers_endpoint(svc: SyncService = Depends(get_sync_service)):
    try:
        stats = svc.execute_sync("customers")
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
def sync_equipment_endpoint(svc: SyncService = Depends(get_sync_service)):

    try:
        stats = svc.execute_sync("equipment")
        return {"status": "success", "stats": stats}
    except Exception as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(e)})


@router.post("/rental_orders")
@router.post("/orders")
def sync_rental_orders_endpoint(svc: SyncService = Depends(get_sync_service)):
    try:
        stats = svc.execute_sync("rental_orders")
        return {"status": "success", "stats": stats}
    except Exception as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(e)})


@router.post("/invoices")
def sync_invoices_endpoint(svc: SyncService = Depends(get_sync_service)):
    try:
        stats = svc.execute_sync("invoices")
        return {"status": "success", "stats": stats}
    except Exception as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(e)})


@router.post("/payments")
def sync_payments_endpoint(svc: SyncService = Depends(get_sync_service)):
    try:
        stats = svc.execute_sync("payments")
        return {"status": "success", "stats": stats}
    except Exception as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(e)})


@router.post("/tally_to_rentasst")
def sync_tally_to_rentasst_endpoint(svc: SyncService = Depends(get_sync_service)):
    try:
        stats = svc.execute_sync("tally_to_rentasst")
        return {"status": "success", "stats": stats}
    except Exception as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(e)})

