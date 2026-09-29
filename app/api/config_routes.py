from typing import Dict, Any, Optional
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from ..models.domain import AppConfig
from ..services.config_service import ConfigService
from ..services.discovery_service import DiscoveryService
from ..clients.rentasst_client import RentAsstClient
from ..clients.external_client import ExternalClient
from ..security.masking import mask_secret


router = APIRouter(prefix="/api", tags=["config"])


class SendOtpRequest(BaseModel):
    mobile: Optional[str] = None
    phone: Optional[str] = None
    url: Optional[str] = None


class VerifyOtpRequest(BaseModel):
    mobile: Optional[str] = None
    phone: Optional[str] = None
    otp: Optional[str] = None
    request_id: Optional[str] = None
    url: Optional[str] = None


class RentAsstLoginRequest(BaseModel):
    email: Optional[str] = None
    mail_id: Optional[str] = None
    login_email: Optional[str] = None
    business_code: Optional[str] = None
    tenant_id: Optional[str] = None
    rentasst_tenant_id: Optional[str] = None
    url: Optional[str] = None


def get_config_service(request: Request) -> ConfigService:
    return ConfigService(request.app.state.data_dir)


def _refresh_health_clients(request: Request, cfg: AppConfig) -> None:
    """Rebuilds the RentAsst/Tally clients used by /health so connectivity checks
    always reflect the most recently saved configuration instead of going stale."""
    old_ra = getattr(request.app.state, "ra_client", None)
    old_ext = getattr(request.app.state, "ext_client", None)
    request.app.state.ra_client = RentAsstClient(cfg)
    request.app.state.ext_client = ExternalClient(cfg)
    if old_ra:
        old_ra.close()
    if old_ext:
        old_ext.close()


def _restart_scheduler_if_needed(request: Request, saved_cfg: AppConfig) -> None:
    """Starts/stops the background sync scheduler to match the just-saved config's auto_sync_enabled flag."""
    scheduler = getattr(request.app.state, "scheduler", None)
    if not scheduler:
        return
    if saved_cfg.auto_sync_enabled:
        scheduler.start(saved_cfg.sync_interval_minutes)
    else:
        scheduler.stop()


@router.get("/config")
def get_config(svc: ConfigService = Depends(get_config_service)) -> Dict[str, Any]:
    """Returns the current configuration with all secret fields masked."""
    cfg = svc.get_config()
    return svc.config_store.get_masked_config(cfg)


@router.post("/config")
def save_config(cfg: AppConfig, request: Request, svc: ConfigService = Depends(get_config_service)) -> Dict[str, Any]:
    """Persists a new configuration, refreshes health-check clients, and syncs the scheduler's running state."""
    saved_cfg = svc.save_config(cfg)
    _refresh_health_clients(request, saved_cfg)
    _restart_scheduler_if_needed(request, saved_cfg)
    return {"status": "success", "message": "Configuration saved successfully"}


@router.post("/config/auto-detect")
def auto_detect_config(request: Request, svc: ConfigService = Depends(get_config_service)) -> Dict[str, Any]:
    """Auto-discovers a local RentAsst installation and saves it as the active configuration."""
    auto_cfg = DiscoveryService.auto_discover_rentasst()
    saved_cfg = svc.save_config(auto_cfg)
    _refresh_health_clients(request, saved_cfg)
    if saved_cfg.auto_sync_enabled:
        _restart_scheduler_if_needed(request, saved_cfg)
    return {"status": "success", "message": "RentAsst setup auto-detected successfully!", "config": saved_cfg}


@router.get("/auth/status")
def auth_status(svc: ConfigService = Depends(get_config_service)) -> Dict[str, Any]:
    """Checks whether the middleware is authenticated with a valid RentAsst Bearer Token."""
    cfg = svc.get_config()
    is_authenticated = bool(cfg.rentasst_api_key and cfg.rentasst_api_key.strip())
    return {
        "status": "success",
        "authenticated": is_authenticated,
        "tenant_id": cfg.rentasst_tenant_id or "default",
        "url": cfg.rentasst_url or "http://localhost:8000/api",
    }


@router.post("/auth/logout")
def auth_logout(request: Request, svc: ConfigService = Depends(get_config_service)) -> Dict[str, Any]:
    """Logs out of RentAsst session in middleware by clearing stored bearer token."""
    cfg = svc.get_config()
    cfg.rentasst_api_key = ""
    saved_cfg = svc.save_config(cfg)
    _refresh_health_clients(request, saved_cfg)
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler:
        scheduler.stop()
    return {"status": "success", "message": "Logged out successfully."}


@router.post("/rentasst/send-otp")
def rentasst_send_otp(req: SendOtpRequest, svc: ConfigService = Depends(get_config_service)) -> Dict[str, Any]:
    """Sends OTP to user's mobile number via RentAsst API."""
    mobile = req.mobile or req.phone or ""
    rentasst_url = req.url or "http://localhost:8000/api"
    if not mobile:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Mobile number is required."})

    cfg = svc.get_config()
    cfg.rentasst_url = rentasst_url.rstrip("/")
    client = RentAsstClient(cfg)
    try:
        return client.send_otp(mobile, target_url=rentasst_url)
    except Exception as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(e)})
    finally:
        client.close()


@router.post("/rentasst/verify-otp")
def rentasst_verify_otp(
    req: VerifyOtpRequest, request: Request, svc: ConfigService = Depends(get_config_service)
) -> Dict[str, Any]:
    """Verifies OTP code with RentAsst API, retrieves Sanctum Bearer Token, and updates config."""
    mobile = req.mobile or req.phone or ""
    otp = req.otp or ""
    request_id = req.request_id or ""
    rentasst_url = req.url or "http://localhost:8000/api"

    if not mobile or not otp:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "Mobile number and OTP code are required."},
        )

    cfg = svc.get_config()
    cfg.rentasst_url = rentasst_url.rstrip("/")
    client = RentAsstClient(cfg)
    db_mgr = getattr(request.app.state, "db", None)

    try:
        res = client.verify_otp(mobile, otp, request_id=request_id, target_url=rentasst_url, db_mgr=db_mgr)
        token = res.get("token")
        if not token:
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": "Verification succeeded but no Bearer token was returned.", "raw": res},
            )

        tenant_id = res.get("tenant_id") or cfg.rentasst_tenant_id or "default"
        cfg.rentasst_api_key = token
        cfg.rentasst_tenant_id = tenant_id
        saved_cfg = svc.save_config(cfg)
        _refresh_health_clients(request, saved_cfg)

        if db_mgr and hasattr(db_mgr, "save_bearer_token"):
            db_mgr.save_bearer_token(mobile, token, tenant_id)

        if saved_cfg.auto_sync_enabled:
            _restart_scheduler_if_needed(request, saved_cfg)

        return {
            "status": "success",
            "message": "RentAsst Mobile OTP verified successfully!",
            "token": mask_secret(token),
            "tenant_id": tenant_id,
            "businesses": res.get("businesses", []),
            "config": svc.config_store.get_masked_config(saved_cfg),
        }
    except Exception as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(e)})
    finally:
        client.close()


@router.post("/rentasst/login")
def rentasst_login(
    req: RentAsstLoginRequest, request: Request, svc: ConfigService = Depends(get_config_service)
) -> Dict[str, Any]:
    """Fetches bearer token using only the login mail ID from the database or RentAsst API, and updates configuration."""
    email = req.email or req.mail_id or req.login_email or ""
    business_code = req.business_code or req.tenant_id or req.rentasst_tenant_id or ""
    rentasst_url = req.url or "http://localhost:8000/api"

    if not email:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Login Mail ID (Email) is required."})

    cfg = svc.get_config()
    cfg.rentasst_url = rentasst_url.rstrip("/")
    client = RentAsstClient(cfg)
    db_mgr = getattr(request.app.state, "db", None)

    try:
        res = client.login(email, business_code=business_code, target_url=rentasst_url, db_mgr=db_mgr)
        token = res.get("token") or res.get("data", {}).get("token") or res.get("bearer_token")
        if not token:
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": "Authentication response did not contain an API token.", "raw": res},
            )

        businesses = res.get("business") or res.get("data", {}).get("business") or []
        tenant_id = business_code or res.get("tenant_id") or res.get("business_code") or ""
        if not tenant_id and isinstance(businesses, list) and len(businesses) > 0:
            first_b = businesses[0]
            tenant_id = first_b.get("business_code") or first_b.get("code") or first_b.get("id") or "default"
        if not tenant_id:
            tenant_id = cfg.rentasst_tenant_id or "default"

        cfg.rentasst_api_key = token
        cfg.rentasst_tenant_id = tenant_id
        saved_cfg = svc.save_config(cfg)
        _refresh_health_clients(request, saved_cfg)

        if db_mgr and hasattr(db_mgr, "save_bearer_token"):
            db_mgr.save_bearer_token(email, token, tenant_id)

        if saved_cfg.auto_sync_enabled:
            _restart_scheduler_if_needed(request, saved_cfg)

        return {
            "status": "success",
            "message": f"Successfully retrieved bearer token for {email}!",
            "token": mask_secret(token),
            "tenant_id": tenant_id,
            "businesses": businesses,
            "config": svc.config_store.get_masked_config(saved_cfg),
        }
    except Exception as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(e)})
    finally:
        client.close()


@router.get("/companies/rentasst")
def get_rentasst_companies(svc: ConfigService = Depends(get_config_service)) -> Dict[str, Any]:
    """Fetches list of available RentAsst business companies based on configured URL and Token."""
    cfg = svc.get_config()
    client = RentAsstClient(cfg)
    try:
        businesses = client.fetch_businesses()
        return {"status": "success", "companies": businesses}
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": f"Could not fetch RentAsst businesses: {str(e)}", "companies": []},
        )
    finally:
        client.close()


@router.get("/companies/tally")
def get_tally_companies(svc: ConfigService = Depends(get_config_service)) -> Dict[str, Any]:
    """Queries Tally Prime XML server to return all currently open/loaded companies."""
    cfg = svc.get_config()
    client = ExternalClient(cfg)
    try:
        companies = client.fetch_tally_companies()
        return {"status": "success", "companies": companies}
    except Exception as e:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": f"Could not fetch Tally companies: {str(e)}", "companies": []},
        )
    finally:
        client.close()
