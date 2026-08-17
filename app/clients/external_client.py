import requests
from requests.adapters import HTTPAdapter
from typing import Dict, Any, List, Optional
from ..models.domain import AppConfig
from ..connectors.tally import (
    TallyClient,
    sanitize_tally_xml,
    escape_xml,
    format_tally_date,
    normalize_state_name,
)


class ExternalClient:
    """
    Unified External Client facade delegating Tally Prime operations to TallyClient
    or REST endpoints to external HTTP APIs.
    """
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

        # Modular Tally Client Engine
        self.tally = TallyClient(cfg, session=self.session)

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

    def ping(self) -> bool:
        if self.cfg.external_system_type == "tally":
            return self.tally.ping()
        try:
            r = self.session.get(self.base_url, headers=self.headers, timeout=5, verify=self.cfg.verify_ssl)
            return r.status_code in (200, 204, 404, 405)
        except Exception:
            return False

    def check_exists_in_tally(self, entity_type: str, identifier: str) -> bool:
        if self.cfg.external_system_type != "tally":
            return True
        return self.tally.check_exists(entity_type, identifier)

    def fetch_tally_companies(self) -> List[Dict[str, str]]:
        if self.cfg.external_system_type != "tally":
            return []
        return self.tally.fetch_companies()

    def sync_customer(self, data: Dict[str, Any]) -> str:
        if self.cfg.external_system_type == "tally":
            return self.tally.sync_customer(data)
        else:
            url = f"{self.base_url}/api/customers"
            r = self.session.post(url, json=data, headers=self.headers, timeout=10, verify=self.cfg.verify_ssl)
            r.raise_for_status()
            res = r.json()
            return str(res.get("id") or res.get("external_id") or data.get("id"))

    def sync_unit(self, data: Dict[str, Any]) -> str:
        if self.cfg.external_system_type == "tally":
            return self.tally.sync_unit(data)
        else:
            url = f"{self.base_url}/api/units"
            r = self.session.post(url, json=data, headers=self.headers, timeout=10, verify=self.cfg.verify_ssl)
            r.raise_for_status()
            res = r.json()
            return str(res.get("id") or data.get("id") or data.get("name"))

    def sync_equipment(self, data: Dict[str, Any]) -> str:

        if self.cfg.external_system_type == "tally":
            return self.tally.sync_equipment(data)
        else:
            url = f"{self.base_url}/api/equipment"
            r = self.session.post(url, json=data, headers=self.headers, timeout=10, verify=self.cfg.verify_ssl)
            r.raise_for_status()
            res = r.json()
            return str(res.get("id") or data.get("id"))

    def sync_rental_order(self, data: Dict[str, Any]) -> str:
        if self.cfg.external_system_type == "tally":
            return self.tally.sync_rental_order(data)
        else:
            url = f"{self.base_url}/api/rental-orders"
            r = self.session.post(url, json=data, headers=self.headers, timeout=10, verify=self.cfg.verify_ssl)
            r.raise_for_status()
            res = r.json()
            return str(res.get("id") or data.get("id"))

    def sync_invoice(self, data: Dict[str, Any]) -> str:
        if self.cfg.external_system_type == "tally":
            return self.tally.sync_invoice(data)
        else:
            url = f"{self.base_url}/api/invoices"
            r = self.session.post(url, json=data, headers=self.headers, timeout=10, verify=self.cfg.verify_ssl)
            r.raise_for_status()
            res = r.json()
            return str(res.get("id") or data.get("id"))

    def sync_payment(self, data: Dict[str, Any]) -> str:
        if self.cfg.external_system_type == "tally":
            return self.tally.sync_payment(data)
        else:
            url = f"{self.base_url}/api/payments"
            r = self.session.post(url, json=data, headers=self.headers, timeout=10, verify=self.cfg.verify_ssl)
            r.raise_for_status()
            res = r.json()
            return str(res.get("id") or data.get("id"))
