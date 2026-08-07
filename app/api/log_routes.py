from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse
from ..services.log_service import LogService

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("")
def get_logs(lines: int = 100):
    recent_logs = LogService.get_recent_logs(lines=lines)
    return {"status": "success", "lines": recent_logs}


@router.get("/download")
def download_log_file():
    log_path = LogService.get_log_download_path()
    if log_path:
        return FileResponse(
            path=log_path,
            filename="middleware.log",
            media_type="text/plain",
        )
    return JSONResponse(status_code=404, content={"status": "error", "message": "Log file not found"})
