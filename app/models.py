from pydantic import BaseModel, Field
from typing import Optional, List


class AppConfig(BaseModel):
    # RentAsst System Config
    rentasst_url: str = Field(default="http://localhost:8000/api")
    rentasst_api_key: str = Field(default="")
    rentasst_tenant_id: Optional[str] = Field(default="default")
    
    # External System Config (e.g. Tally Prime / ERP / Accounting)
    external_url: str = Field(default="http://localhost:9000")
    external_api_key: Optional[str] = Field(default="")
    external_system_type: str = Field(default="tally") # 'tally', 'rest_erp', 'accounting'
    
    # General Settings
    sync_interval_minutes: int = Field(default=15)
    auto_sync_enabled: bool = Field(default=False)
    proxy: Optional[str] = Field(default="")
    verify_ssl: bool = Field(default=True)


class CustomerModel(BaseModel):
    id: str
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    gstin_tax_id: Optional[str] = None


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
