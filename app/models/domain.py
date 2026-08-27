from pydantic import BaseModel, Field
from typing import Optional, Dict, Any


class AppConfig(BaseModel):
    # RentAsst System Config
    rentasst_url: str = Field(default="http://localhost:8000/api")
    rentasst_api_key: str = Field(default="")
    rentasst_tenant_id: Optional[str] = Field(default="default")
    
    # External System Config (e.g. Tally Prime / ERP / Accounting)
    external_url: str = Field(default="http://localhost:9000")
    external_api_key: Optional[str] = Field(default="")
    external_system_type: str = Field(default="tally") # 'tally', 'rest_erp', 'accounting'
    tally_company_name: Optional[str] = Field(default="") # Target Tally Company Name for multi-company setup
    # Tally "Educational Mode" (the free/unlicensed mode) only accepts vouchers dated the
    # 1st, 2nd, or last day of a month — rejecting any other date. Default False: real
    # transaction dates always go through. Only set True against an Educational-mode Tally
    # install (e.g. a demo/test company), where it forces the day-of-month so imports don't
    # get rejected outright.
    tally_edu_mode: bool = Field(default=False)

    # Whether this Tally company's "Order Processing" F11 feature actually accepts
    # Sales Order voucher imports via the XML gateway. None = unknown (try the native
    # "Sales Order" voucher first). False = confirmed unavailable (e.g. an unlicensed/
    # Educational-mode install, or Order Processing disabled) — auto-detected the same
    # way tally_edu_mode is, so Rent Out sync stops re-attempting a voucher type this
    # Tally install will never accept. True = confirmed working.
    tally_order_processing_available: Optional[bool] = Field(default=None)

    # General Settings
    sync_interval_minutes: int = Field(default=10)
    auto_sync_enabled: bool = Field(default=True)
    proxy: Optional[str] = Field(default="")
    verify_ssl: bool = Field(default=True)


class SystemStatusModel(BaseModel):
    status: str
    machine_name: str
    middleware_version: str
    system_health: Dict[str, Any]
    queue_metrics: Dict[str, int]
    resource_metrics: Dict[str, float]
    entity_sync_status: Dict[str, Any] = Field(default_factory=dict)
    job_status_breakdown: Dict[str, int] = Field(default_factory=dict)
    reconciliation_metrics: Dict[str, Any] = Field(default_factory=dict)
    performance_metrics: Dict[str, Any] = Field(default_factory=dict)
    timestamp: str


class BackupModel(BaseModel):
    filename: str
    size_bytes: int
    created_at: str
