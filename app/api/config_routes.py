from fastapi import APIRouter, Depends, Request
from ..models.domain import AppConfig
from ..services.config_service import ConfigService
from ..services.discovery_service import DiscoveryService

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


@router.post("/config/auto-detect")
def auto_detect_config(request: Request, svc: ConfigService = Depends(get_config_service)):
    auto_cfg = DiscoveryService.auto_discover_rentasst()
    saved_cfg = svc.save_config(auto_cfg)
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler and saved_cfg.auto_sync_enabled:
        scheduler.start(saved_cfg.sync_interval_minutes)
    return {"status": "success", "message": "RentAsst setup auto-detected successfully!", "config": saved_cfg}


@router.get("/companies/rentasst")
def get_rentasst_companies(svc: ConfigService = Depends(get_config_service)):
    """Fetches list of available RentAsst business companies based on configured URL and Token."""
    cfg = svc.get_config()
    from ..clients.rentasst_client import RentAsstClient
    client = RentAsstClient(cfg)
    try:
        businesses = client.fetch_businesses()
        return {"status": "success", "companies": businesses}
    except Exception as e:
        return {"status": "error", "message": f"Could not fetch RentAsst businesses: {str(e)}", "companies": []}
    finally:
        client.close()


@router.get("/companies/tally")
def get_tally_companies(svc: ConfigService = Depends(get_config_service)):
    """Queries Tally Prime XML server to return all currently open/loaded companies."""
    cfg = svc.get_config()
    from ..clients.external_client import ExternalClient
    client = ExternalClient(cfg)
    try:
        companies = client.fetch_tally_companies()
        return {"status": "success", "companies": companies}
    except Exception as e:
        return {"status": "error", "message": f"Could not fetch Tally companies: {str(e)}", "companies": []}

