import os
import re
from typing import Dict, Any, Optional
from ..models.domain import AppConfig
from ..logging.logger import log_event

COMMON_RENTASST_PATHS = [
    r"C:\RentAsst\RentalApi\.env",
    r"C:\RentAsst\.env",
    r"C:\RentalApi\.env",
    r"C:\Program Files\RentAsst\.env",
    r"C:\xampp\htdocs\RentalApi\.env",
    r"C:\xampp\htdocs\rentasst\.env",
    r"C:\laragon\www\RentalApi\.env",
    r"C:\laragon\www\rentasst\.env",
]


class DiscoveryService:
    """Auto-discovery service to detect local RentAsst installation, tenant business code, and API keys."""
    @staticmethod
    def parse_env_file(env_path: str) -> Dict[str, str]:
        env_vars = {}
        try:
            with open(env_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        env_vars[k.strip()] = v.strip().strip('"').strip("'")
        except Exception:
            pass
        return env_vars

    @classmethod
    def auto_discover_rentasst(cls) -> AppConfig:
        discovered_url = "http://localhost:8000/api"
        discovered_key = ""
        discovered_tenant = "B100001"

        if os.getenv("RENTASST_URL"):
            discovered_url = os.getenv("RENTASST_URL")
        if os.getenv("RENTASST_API_KEY"):
            discovered_key = os.getenv("RENTASST_API_KEY")
        if os.getenv("RENTASST_TENANT_ID") or os.getenv("BUSINESS_CODE"):
            discovered_tenant = os.getenv("RENTASST_TENANT_ID") or os.getenv("BUSINESS_CODE")

        for path in COMMON_RENTASST_PATHS:
            if os.path.exists(path):
                env_vars = cls.parse_env_file(path)
                app_url = env_vars.get("APP_URL") or env_vars.get("RENTASST_URL")
                api_key = env_vars.get("RENTASST_API_KEY") or env_vars.get("API_KEY") or env_vars.get("SANCTUM_TOKEN")
                tenant_code = env_vars.get("BUSINESS_CODE") or env_vars.get("RENTASST_TENANT_ID") or env_vars.get("TENANT_ID")
                
                if app_url:
                    if not app_url.endswith("/api"):
                        discovered_url = f"{app_url.rstrip('/')}/api"
                    else:
                        discovered_url = app_url
                if api_key:
                    discovered_key = api_key
                if tenant_code:
                    discovered_tenant = tenant_code
                log_event("Discovery", f"Auto-discovered RentAsst config at {path} (Tenant: {discovered_tenant})")
                break

        return AppConfig(
            rentasst_url=discovered_url,
            rentasst_api_key=discovered_key,
            rentasst_tenant_id=discovered_tenant,
            external_url="http://localhost:9000",
            external_system_type="tally",
            sync_interval_minutes=10,
            auto_sync_enabled=True,
        )
