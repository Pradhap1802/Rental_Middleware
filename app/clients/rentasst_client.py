import requests
from requests.adapters import HTTPAdapter
from typing import Dict, Any, List, Optional
from ..models.domain import AppConfig
from ..retry.engine import RetryableException, NonRetryableException


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
        if cfg.rentasst_tenant_id:
            self.headers["BusinessCode"] = cfg.rentasst_tenant_id
            self.headers["businesscode"] = cfg.rentasst_tenant_id

    def _request_with_fallback(self, endpoints: List[str], params: Optional[Dict[str, Any]] = None) -> Any:
        last_error = None
        for endpoint in endpoints:
            url = f"{self.base_url}/{endpoint.lstrip('/')}"
            try:
                r = self.session.get(url, headers=self.headers, params=params or {}, timeout=10, verify=self.cfg.verify_ssl)
                if r.status_code in (401, 403):
                    raise NonRetryableException(f"RentAsst API 401 Unauthorized at {url}. Check your API Key/Token.")
                if r.status_code in (404, 405):
                    continue
                r.raise_for_status()
                data = r.json()
                return data.get("data", data) if isinstance(data, dict) else data
            except requests.exceptions.JSONDecodeError as e:
                snippet = (r.text or "")[:150].replace("\n", " ").strip()
                raise RetryableException(
                    f"RentAsst API at {url} returned invalid non-JSON response (Status {r.status_code}): {snippet}"
                ) from e
            except requests.exceptions.HTTPError as e:
                err_text = (r.text or "")[:200].replace("\n", " ").strip()
                raise RetryableException(
                    f"RentAsst API at {url} HTTP {r.status_code} Error: {err_text}"
                ) from e
            except NonRetryableException:
                raise
            except Exception as e:
                last_error = e
        
        if last_error:
            raise last_error
        raise RetryableException(f"RentAsst API endpoints {endpoints} not found (404)")

    def ping(self) -> bool:
        try:
            r = self.session.get(f"{self.base_url}/health", headers=self.headers, timeout=5, verify=self.cfg.verify_ssl)
            return r.status_code in (200, 204)
        except Exception:
            return False

    def fetch_customers(self, updated_after: Optional[str] = None) -> List[Dict[str, Any]]:
        params = {}
        if updated_after:
            params["updated_after"] = updated_after
        customers = self._request_with_fallback(["customer", "customers"], params)
        if isinstance(customers, list):
            enriched = []
            for cust in customers:
                if isinstance(cust, dict) and "id" in cust and "address" not in cust:
                    try:
                        detail = self._request_with_fallback([f"customer/{cust['id']}"])
                        if isinstance(detail, dict) and "id" in detail:
                            cust = detail
                    except Exception:
                        pass
                enriched.append(cust)
            return enriched
        return customers

    def fetch_equipment(self) -> List[Dict[str, Any]]:
        return self._request_with_fallback(["asset", "equipment"])

    def fetch_rental_orders(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        params = {}
        if status:
            params["status"] = status
        return self._request_with_fallback(["quotation", "rental-orders", "transfer-orders"], params)

    def fetch_invoices(self) -> List[Dict[str, Any]]:
        return self._request_with_fallback(["invoices", "invoice"])

    def fetch_payments(self) -> List[Dict[str, Any]]:
        return self._request_with_fallback(["payment", "payments"])

    def close(self):
        self.session.close()
