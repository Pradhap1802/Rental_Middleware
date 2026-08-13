from typing import Dict, Any, Optional
from fastapi import APIRouter, Depends, Request
from ..models.domain import AppConfig
from ..services.config_service import ConfigService
from ..services.discovery_service import DiscoveryService


router = APIRouter(prefix="/api", tags=["config"])


def get_config_service(request: Request) -> ConfigService:
    return ConfigService(request.app.state.data_dir)


@router.get("/config")
def get_config(svc: ConfigService = Depends(get_config_service)):
    cfg = svc.get_config()
    return svc.config_store.get_masked_config(cfg)


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


@router.post("/rentasst/login")
def rentasst_login(req: Dict[str, Any], request: Request, svc: ConfigService = Depends(get_config_service)):
    """Authenticates user credentials against RentAsst API, retrieves Sanctum token & tenant info, and updates configuration."""
    from ..clients.rentasst_client import RentAsstClient
    email = req.get("email") or ""
    password = req.get("password") or ""
    rentasst_url = req.get("url") or "http://localhost:8000/api"
    
    if not email or not password:
        return {"status": "error", "message": "Email/Username and Password are required."}
    
    cfg = svc.get_config()
    cfg.rentasst_url = rentasst_url.rstrip("/")
    client = RentAsstClient(cfg)
    
    try:
        res = client.login(email, password, target_url=rentasst_url)
        token = res.get("token") or res.get("data", {}).get("token")
        if not token:
            return {"status": "error", "message": "Authentication response did not contain an API token.", "raw": res}
        
        # Extract available business companies
        businesses = res.get("business") or res.get("data", {}).get("business") or []
        tenant_id = req.get("business_code") or ""
        if not tenant_id and isinstance(businesses, list) and len(businesses) > 0:
            first_b = businesses[0]
            tenant_id = first_b.get("business_code") or first_b.get("code") or first_b.get("id") or "default"
        
        if not tenant_id:
            tenant_id = cfg.rentasst_tenant_id or "default"

        cfg.rentasst_api_key = token
        cfg.rentasst_tenant_id = tenant_id
        saved_cfg = svc.save_config(cfg)
        
        scheduler = getattr(request.app.state, "scheduler", None)
        if scheduler and saved_cfg.auto_sync_enabled:
            scheduler.start(saved_cfg.sync_interval_minutes)
            
        return {
            "status": "success",
            "message": f"Successfully authenticated as {email}!",
            "token": token,
            "tenant_id": tenant_id,
            "businesses": businesses,
            "config": svc.config_store.get_masked_config(saved_cfg)
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        client.close()



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

