from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any


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
    
    # General Settings
    sync_interval_minutes: int = Field(default=10)
    auto_sync_enabled: bool = Field(default=True)
    proxy: Optional[str] = Field(default="")
    verify_ssl: bool = Field(default=True)


class RentAsstLoginRequest(BaseModel):
    url: Optional[str] = Field(default="http://localhost:8000/api")
    email: str = Field(..., description="RentAsst account login mail ID")
    business_code: Optional[str] = Field(default="", description="Target business code (optional)")



class CustomerModel(BaseModel):
    id: str
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    gstin_tax_id: Optional[str] = None


class AssetUnitModel(BaseModel):
    id: str
    name: str
    symbol: Optional[str] = None
    formal_name: Optional[str] = None
    uqc_code: Optional[str] = None
    decimal_places: int = 0


class EquipmentModel(BaseModel):
    id: str
    name: str

    code_sku: Optional[str] = None
    category: Optional[str] = None
    daily_rate: float = 0.0
    monthly_rate: float = 0.0
    stock_quantity: int = 1


class RentalOrderLineModel(BaseModel):
    equipment_id: str
    quantity: int = 1
    unit_price: float = 0.0
    rental_start_date: str
    rental_end_date: str


class RentalOrderModel(BaseModel):
    id: str
    order_number: str
    customer_id: str
    order_date: str
    status: str = "draft" # draft, confirmed, dispatched, returned, cancelled
    lines: List[RentalOrderLineModel] = []
    total_amount: float = 0.0


class InvoiceModel(BaseModel):
    id: str
    invoice_number: str
    rental_order_id: Optional[str] = None
    customer_id: str
    invoice_date: str
    due_date: str
    subtotal: float = 0.0
    tax_amount: float = 0.0
    total_amount: float = 0.0


class PaymentModel(BaseModel):
    id: str
    payment_number: str
    customer_id: str
    invoice_id: Optional[str] = None
    rental_order_id: Optional[str] = None
    payment_date: str
    amount: float = 0.0
    payment_type: str = "rental_payment" # 'rental_payment', 'security_deposit', 'deposit_refund'
    payment_mode: str = "bank"


class DeadLetterModel(BaseModel):
    id: int
    entity_type: str
    source_id: str
    error: str
    created_at: str


class QueueJobModel(BaseModel):
    job_id: int
    entity_type: str
    entity_id: Optional[str] = ""
    company_id: Optional[str] = "default"
    direction: Optional[str] = "forward"
    payload: Optional[str] = None
    status: str # PENDING, PROCESSING, SUCCESS, PARTIAL_SUCCESS, FAILED, RETRYING, DLQ, CANCELLED
    attempt_count: int = 0
    max_attempts: int = 3
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    last_error: Optional[str] = None
    next_retry_at: Optional[str] = None
    created_at: str
    updated_at: str


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
