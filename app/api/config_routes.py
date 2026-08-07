from fastapi import APIRouter, Depends, Request
from ..models.domain import AppConfig
from ..services.config_service import ConfigService

router = APIRouter(prefix="/api", tags=["config"])


def get_config_service(request: Request) -> ConfigService:
    return ConfigService(request.app.state.data_dir)


@router.get("/config", response_model=AppConfig)
def get_config(svc: ConfigService = Depends(get_config_service)):
    return svc.get_config()


@router.post("/config")
def save_config(cfg: AppConfig, request: Request, svc: ConfigService = Depends(get_config_service)):
    saved_cfg = svc.save_config(cfg)
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler:
        if saved_cfg.auto_sync_enabled:
            scheduler.start(saved_cfg.sync_interval_minutes)
        else:
            scheduler.stop()
    return {"status": "success", "message": "Configuration saved successfully"}
