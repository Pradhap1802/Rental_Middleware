from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from ..services.test_service import TestService

router = APIRouter(prefix="/api/test", tags=["test"])


def get_test_service(request: Request) -> TestService:
    return TestService(request.app.state.data_dir)


@router.post("/rentasst")
def test_rentasst(svc: TestService = Depends(get_test_service)):
    res = svc.test_rentasst()
    if res.get("status") == "success":
        return res
    return JSONResponse(status_code=400, content=res)


@router.post("/external")
def test_external(svc: TestService = Depends(get_test_service)):
    res = svc.test_external()
    if res.get("status") == "success":
        return res
    return JSONResponse(status_code=400, content=res)
