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
