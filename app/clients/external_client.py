import requests
from requests.adapters import HTTPAdapter
from typing import Dict, Any, List, Optional
from ..models.domain import AppConfig


class ExternalClient:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.base_url = cfg.external_url.rstrip("/")
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=20, pool_maxsize=50)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if cfg.external_api_key:
            self.headers["Authorization"] = f"Bearer {cfg.external_api_key}"

    def ping(self) -> bool:
        try:
            r = self.session.get(self.base_url, headers=self.headers, timeout=5, verify=self.cfg.verify_ssl)
            return r.status_code in (200, 204, 404, 405) # Active host connection test
        except Exception:
            return False

    def sync_customer(self, customer_data: Dict[str, Any]) -> str:
        """Push customer data to external target system and return external reference ID."""
        url = f"{self.base_url}/api/customers"
        r = self.session.post(url, json=customer_data, headers=self.headers, timeout=10, verify=self.cfg.verify_ssl)
        r.raise_for_status()
        res = r.json()
        return res.get("id") or res.get("external_id") or str(customer_data.get("id"))

    def sync_equipment(self, equipment_data: Dict[str, Any]) -> str:
        """Push rental equipment/product data to external target system."""
        url = f"{self.base_url}/api/equipment"
        r = self.session.post(url, json=equipment_data, headers=self.headers, timeout=10, verify=self.cfg.verify_ssl)
        r.raise_for_status()
        res = r.json()
        return res.get("id") or str(equipment_data.get("id"))

    def sync_rental_order(self, order_data: Dict[str, Any]) -> str:
        """Push rental order contract data to external target system."""
        url = f"{self.base_url}/api/rental-orders"
        r = self.session.post(url, json=order_data, headers=self.headers, timeout=10, verify=self.cfg.verify_ssl)
        r.raise_for_status()
        res = r.json()
        return res.get("id") or str(order_data.get("id"))

    def sync_invoice(self, invoice_data: Dict[str, Any]) -> str:
        """Push invoice record to external target system."""
        url = f"{self.base_url}/api/invoices"
        r = self.session.post(url, json=invoice_data, headers=self.headers, timeout=10, verify=self.cfg.verify_ssl)
        r.raise_for_status()
        res = r.json()
        return res.get("id") or str(invoice_data.get("id"))

    def sync_payment(self, payment_data: Dict[str, Any]) -> str:
        """Push payment receipt to external target system."""
        url = f"{self.base_url}/api/payments"
        r = self.session.post(url, json=payment_data, headers=self.headers, timeout=10, verify=self.cfg.verify_ssl)
        r.raise_for_status()
        res = r.json()
        return res.get("id") or str(payment_data.get("id"))

    def close(self):
        self.session.close()
