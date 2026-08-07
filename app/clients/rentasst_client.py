import requests
from requests.adapters import HTTPAdapter
from typing import Dict, Any, List, Optional
from ..models.domain import AppConfig


class RentAsstClient:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.base_url = cfg.rentasst_url.rstrip("/")
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=20, pool_maxsize=50)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if cfg.rentasst_api_key:
            self.headers["Authorization"] = f"Bearer {cfg.rentasst_api_key}"

    def ping(self) -> bool:
        try:
            r = self.session.get(f"{self.base_url}/health", headers=self.headers, timeout=5, verify=self.cfg.verify_ssl)
            return r.status_code in (200, 204)
        except Exception:
            return False

    def fetch_customers(self, updated_after: Optional[str] = None) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/customers"
        params = {}
        if updated_after:
            params["updated_after"] = updated_after
        r = self.session.get(url, headers=self.headers, params=params, timeout=10, verify=self.cfg.verify_ssl)
        r.raise_for_status()
        data = r.json()
        return data.get("data", data) if isinstance(data, dict) else data

    def fetch_equipment(self) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/equipment"
        r = self.session.get(url, headers=self.headers, timeout=10, verify=self.cfg.verify_ssl)
        r.raise_for_status()
        data = r.json()
        return data.get("data", data) if isinstance(data, dict) else data

    def fetch_rental_orders(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/rental-orders"
        params = {}
        if status:
            params["status"] = status
        r = self.session.get(url, headers=self.headers, params=params, timeout=10, verify=self.cfg.verify_ssl)
        r.raise_for_status()
        data = r.json()
        return data.get("data", data) if isinstance(data, dict) else data

    def fetch_invoices(self) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/invoices"
        r = self.session.get(url, headers=self.headers, timeout=10, verify=self.cfg.verify_ssl)
        r.raise_for_status()
        data = r.json()
        return data.get("data", data) if isinstance(data, dict) else data

    def fetch_payments(self) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/payments"
        r = self.session.get(url, headers=self.headers, timeout=10, verify=self.cfg.verify_ssl)
        r.raise_for_status()
        data = r.json()
        return data.get("data", data) if isinstance(data, dict) else data

    def close(self):
        self.session.close()
