from fastapi import APIRouter, Depends, Request
from ..services.backup_service import BackupService

router = APIRouter(prefix="/api/backups", tags=["backups"])


def get_backup_service(request: Request) -> BackupService:
    return BackupService(request.app.state.data_dir)


@router.get("")
def list_backups(svc: BackupService = Depends(get_backup_service)):
    return svc.list_backups()


@router.post("")
def trigger_backup(svc: BackupService = Depends(get_backup_service)):
    return svc.trigger_backup()


@router.post("/verify/{filename}")
def verify_backup(filename: str, svc: BackupService = Depends(get_backup_service)):
    import os
    backup_path = os.path.join(svc.backup_dir, filename)
    verified = svc.verify_backup(backup_path)
    return {"filename": filename, "verified": verified}


@router.post("/restore/{filename}")
def restore_backup(filename: str, svc: BackupService = Depends(get_backup_service)):
    return svc.restore_backup(filename)
