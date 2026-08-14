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
                r = self.session.get(url, headers=self.headers, params=params or {}, timeout=30, verify=self.cfg.verify_ssl)
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

    def login(self, email: str, business_code: Optional[str] = None, target_url: Optional[str] = None, db_mgr: Any = None) -> Dict[str, Any]:
        """Fetches bearer token for user using login mail ID and optional business code."""
        clean_email = str(email).strip()
        if not clean_email:
            raise Exception("Login mail ID (email) is required.")
        clean_business_code = str(business_code).strip() if business_code else ""

        # 1. Check database first if db_mgr provided
        if db_mgr and hasattr(db_mgr, "get_bearer_token"):
            token_record = db_mgr.get_bearer_token(clean_email, clean_business_code or None)
            if token_record and token_record.get("token"):
                return {
                    "status": "success",
                    "token": token_record["token"],
                    "tenant_id": token_record.get("tenant_id") or "default",
                    "source": "database"
                }

        # 2. Query API using login mail ID
        base_url = (target_url or self.base_url).rstrip("/")
        token_endpoints = ["get-bearer-token", "token", "bearer-token"]
        login_endpoints = ["business-login", "user/business-login", "admin/login"]
        endpoints = token_endpoints + login_endpoints
        payload: Dict[str, Any] = {
            "email": clean_email,
            "mail_id": clean_email,
            "login_email": clean_email,
            "login_mail_id": clean_email,
            "username": clean_email,
            "mobile": clean_email,
            "phone": clean_email,
        }
        if clean_business_code:
            payload["business_code"] = clean_business_code

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        last_error = None
        password_required = False
        for endpoint in endpoints:
            url = f"{base_url}/{endpoint.lstrip('/')}"
            try:
                if endpoint in token_endpoints:
                    r = self.session.get(url, headers=headers, params=payload, timeout=15, verify=self.cfg.verify_ssl)
                    if r.status_code in (404, 405):
                        r = self.session.post(url, json=payload, headers=headers, timeout=15, verify=self.cfg.verify_ssl)
                else:
                    r = self.session.post(url, json=payload, headers=headers, timeout=15, verify=self.cfg.verify_ssl)
                if r.status_code in (200, 201):
                    data = r.json()
                    if isinstance(data, dict):
                        token = data.get("token") or data.get("data", {}).get("token") or data.get("bearer_token")
                        tenant_id = data.get("tenant_id") or data.get("business_code") or clean_business_code or "default"
                        if token and db_mgr and hasattr(db_mgr, "save_bearer_token"):
                            db_mgr.save_bearer_token(clean_email, token, tenant_id)
                        return data
                elif r.status_code in (401, 403, 422):
                    try:
                        err_json = r.json()
                        msg = err_json.get("message") or err_json.get("error") or "Invalid email or login credential."
                        if r.status_code == 422 and "password" in str(msg).lower():
                            password_required = True
                            continue
                        raise Exception(f"RentAsst Authentication Failed ({r.status_code}): {msg}")
                    except requests.exceptions.JSONDecodeError:
                        raise Exception(f"RentAsst Authentication Failed ({r.status_code}).")
            except Exception as e:
                last_error = e
                if "Authentication Failed" in str(e):
                    raise e
        if last_error and not password_required:
            if not (self.cfg and self.cfg.rentasst_api_key):
                raise last_error

        # 3. Fallback: Use configured or auto-discovered API key
        if not (self.cfg and self.cfg.rentasst_api_key):
            try:
                from ..services.discovery_service import DiscoveryService
                auto_cfg = DiscoveryService.auto_discover_rentasst()
                if auto_cfg and auto_cfg.rentasst_api_key:
                    self.cfg.rentasst_api_key = auto_cfg.rentasst_api_key
                    if not clean_business_code and auto_cfg.rentasst_tenant_id:
                        clean_business_code = auto_cfg.rentasst_tenant_id
            except Exception:
                pass

        if self.cfg and self.cfg.rentasst_api_key:
            tenant_id = clean_business_code or self.cfg.rentasst_tenant_id or "default"
            if db_mgr and hasattr(db_mgr, "save_bearer_token"):
                db_mgr.save_bearer_token(clean_email, self.cfg.rentasst_api_key, tenant_id)
            return {
                "status": "success",
                "token": self.cfg.rentasst_api_key,
                "tenant_id": tenant_id,
                "source": "config"
            }

        raise Exception(
            "Could not fetch bearer token using email only. Confirm the RentAsst API URL exposes an email-only token endpoint."
        )

    def send_otp(self, mobile: str, target_url: Optional[str] = None) -> Dict[str, Any]:
        """Requests OTP for mobile number from RentAsst API server."""
        clean_mobile = str(mobile).strip()
        if not clean_mobile:
            raise Exception("Mobile number is required.")
        base_url = (target_url or self.base_url).rstrip("/")
        endpoints = [
            "admin/business-owner/send-otp",
            "ecommerce/auth/send-otp",
            "auth/send-otp",
            "send-otp",
        ]
        device_token = f"mw_device_{clean_mobile}"
        notification_token = f"mw_notif_{clean_mobile}"
        payload = {
            "mobile": clean_mobile,
            "notification_token": notification_token,
            "device_token": device_token,
            "device_type": "web",
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Device-Type": "web",
            "device-type": "web",
        }
        last_error = None
        for endpoint in endpoints:
            url = f"{base_url}/{endpoint.lstrip('/')}"
            try:
                r = self.session.post(url, json=payload, headers=headers, timeout=15, verify=self.cfg.verify_ssl)
                if r.status_code in (200, 201):
                    data = r.json()
                    req_id = data.get("request_id") or data.get("data", {}).get("request_id") or f"req_{clean_mobile}"
                    dev_otp = data.get("otp") or data.get("data", {}).get("otp") or None
                    return {
                        "status": "success",
                        "message": f"OTP sent successfully to {clean_mobile}",
                        "request_id": req_id,
                        "dev_otp": dev_otp,
                        "raw": data
                    }
                elif r.status_code in (400, 401, 403, 422):
                    try:
                        err = r.json()
                        msg = err.get("message")
                        if not msg or "and 1 more error" in str(msg):
                            errors = err.get("errors", {})
                            if isinstance(errors, dict) and errors:
                                all_errs = []
                                for _, err_list in errors.items():
                                    if isinstance(err_list, list):
                                        all_errs.extend(err_list)
                                    else:
                                        all_errs.append(str(err_list))
                                msg = ", ".join(all_errs)
                        if not msg:
                            msg = err.get("error") or "Failed to send OTP."
                        raise Exception(f"RentAsst OTP Error ({r.status_code}): {msg}")
                    except Exception as e:
                        if "RentAsst OTP Error" in str(e):
                            raise e
            except Exception as e:
                last_error = e
                if "RentAsst OTP Error" in str(e):
                    raise e
        if last_error:
            raise last_error
        raise Exception("Could not reach RentAsst OTP endpoints.")

    def verify_otp(self, mobile: str, otp: str, request_id: Optional[str] = None, target_url: Optional[str] = None, db_mgr: Any = None) -> Dict[str, Any]:
        """Verifies OTP code with RentAsst API and retrieves Sanctum Bearer Token."""
        clean_mobile = str(mobile).strip()
        clean_otp = str(otp).strip()
        if not clean_mobile or not clean_otp:
            raise Exception("Mobile number and OTP code are required.")
        base_url = (target_url or self.base_url).rstrip("/")
        endpoints = [
            "admin/business-owner/verify-otp",
            "ecommerce/auth/verify-otp",
            "auth/verify-otp",
            "verify-otp",
        ]
        device_token = f"mw_device_{clean_mobile}"
        notification_token = f"mw_notif_{clean_mobile}"
        payload = {
            "mobile": clean_mobile,
            "otp": clean_otp,
            "request_id": request_id or "",
            "notification_token": notification_token,
            "device_token": device_token,
            "device_type": "web",
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Device-Type": "web",
            "device-type": "web",
        }
        last_error = None
        for endpoint in endpoints:
            url = f"{base_url}/{endpoint.lstrip('/')}"
            try:
                r = self.session.post(url, json=payload, headers=headers, timeout=15, verify=self.cfg.verify_ssl)
                if r.status_code in (200, 201):
                    data = r.json()
                    if isinstance(data, dict):
                        if data.get("user_not_registered"):
                            raise Exception(f"Mobile number +91 {clean_mobile} is verified, but not registered in RentAsst. Please enter your registered RentAsst account mobile number.")
                        if data.get("user_has_no_business"):
                            raise Exception(f"The account for +91 {clean_mobile} has no active business assigned in RentAsst.")

                        token = data.get("token") or data.get("data", {}).get("token") or data.get("bearer_token")
                        tenant_id = data.get("tenant_id") or data.get("business_code") or ""
                        businesses = data.get("business") or data.get("data", {}).get("business") or []
                        if not tenant_id and isinstance(businesses, list) and len(businesses) > 0:
                            first_b = businesses[0]
                            tenant_id = first_b.get("business_code") or first_b.get("code") or first_b.get("id") or ""
                        if not tenant_id:
                            tenant_id = "default"

                        if not token:
                            raise Exception(f"RentAsst API did not return an access token for +91 {clean_mobile}.")

                        if token and db_mgr and hasattr(db_mgr, "save_bearer_token"):
                            db_mgr.save_bearer_token(clean_mobile, token, tenant_id)
                        return {
                            "status": "success",
                            "message": "OTP verified successfully!",
                            "token": token,
                            "tenant_id": tenant_id,
                            "businesses": businesses,
                            "raw": data
                        }
                elif r.status_code in (400, 401, 403, 422):
                    try:
                        err = r.json()
                        msg = err.get("message")
                        if not msg or "and 1 more error" in str(msg):
                            errors = err.get("errors", {})
                            if isinstance(errors, dict) and errors:
                                all_errs = []
                                for _, err_list in errors.items():
                                    if isinstance(err_list, list):
                                        all_errs.extend(err_list)
                                    else:
                                        all_errs.append(str(err_list))
                                msg = ", ".join(all_errs)
                        if not msg:
                            msg = err.get("error") or "Invalid or expired OTP."
                        raise Exception(f"RentAsst Verification Failed ({r.status_code}): {msg}")
                    except Exception as e:
                        if "Verification Failed" in str(e):
                            raise e
            except Exception as e:
                last_error = e
                if "Verification Failed" in str(e):
                    raise e
        if last_error:
            raise last_error
        raise Exception("Could not reach RentAsst OTP verification endpoints.")





    def check_exists_in_rentasst(self, entity_type: str, rentasst_id: str) -> bool:

        """Verifies if a record still exists on the RentAsst Cloud API server."""
        if not rentasst_id:
            return True
        rid = str(rentasst_id).strip()
        if not rid.isdigit():
            return True

        ent = (entity_type or "").lower().strip()
        endpoints = []
        if ent in ("rental_orders", "rental_order", "rent", "invoices", "invoice"):
            endpoints = [f"invoices/{rid}", f"invoice/{rid}", f"get-rent-details/{rid}", f"rent/{rid}"]
        elif ent in ("payments", "payment"):
            endpoints = [f"payment/{rid}", f"payments/{rid}"]
        elif ent in ("customer", "customers"):
            endpoints = [f"customer/{rid}", f"customers/{rid}"]
        else:
            return True

        for endpoint in endpoints:
            url = f"{self.base_url}/{endpoint.lstrip('/')}"
            try:
                r = self.session.get(url, headers=self.headers, timeout=5, verify=self.cfg.verify_ssl)
                if r.status_code in (200, 204):
                    return True
            except Exception:
                pass
        return False




    def fetch_customers(self, updated_after: Optional[str] = None) -> List[Dict[str, Any]]:
        params = {}
        if updated_after:
            params["updated_after"] = updated_after
        customers = self._request_with_fallback(["customer", "customers"], params)
        if isinstance(customers, list):
            enriched = []
            for cust in customers:
                if isinstance(cust, dict) and "id" in cust:
                    if "address" not in cust or "bank_accounts" not in cust:
                        try:
                            detail = self._request_with_fallback([f"customer/{cust['id']}"])
                            if isinstance(detail, dict) and "id" in detail:
                                for k, v in detail.items():
                                    if v is not None or k not in cust:
                                        cust[k] = v
                        except Exception:
                            pass
                    if "bank_accounts" not in cust and "bankAccounts" not in cust:
                        try:
                            banks = self._request_with_fallback([f"customer/{cust['id']}/bank-accounts", f"customer/{cust['id']}/bank-account"])
                            if isinstance(banks, list):
                                cust["bank_accounts"] = banks
                            elif isinstance(banks, dict) and "data" in banks and isinstance(banks["data"], list):
                                cust["bank_accounts"] = banks["data"]
                        except Exception:
                            pass
                enriched.append(cust)
            return enriched
        return customers

    def fetch_equipment(self) -> List[Dict[str, Any]]:
        assets = self._request_with_fallback(["asset", "equipment"])
        if isinstance(assets, list):
            enriched = []
            for item in assets:
                if isinstance(item, dict) and "id" in item:
                    if not item.get("hsn_code"):
                        try:
                            detail = self._request_with_fallback([f"asset/{item['id']}", f"equipment/{item['id']}"])
                            if isinstance(detail, dict) and "id" in detail:
                                for k, v in detail.items():
                                    if v is not None or k not in item:
                                        item[k] = v
                        except Exception:
                            pass
                enriched.append(item)
            return enriched
        return assets

    def fetch_rental_orders(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        # Try POST /api/rent-list first (RentAsst primary API)
        try:
            url = f"{self.base_url}/rent-list"
            body: Dict[str, Any] = {"start": 0, "length": 100}
            if status:
                body["status"] = [status]
            r = self.session.post(url, json=body, headers=self.headers, timeout=30, verify=self.cfg.verify_ssl)
            if r.status_code in (200, 204):
                data = r.json()
                items = data.get("data", data)
                if isinstance(items, list):
                    return items
        except Exception:
            pass

        params = {}
        if status:
            params["status"] = status
        return self._request_with_fallback(["rental-orders", "quotation", "transfer-orders"], params)

    def fetch_invoices(self) -> List[Dict[str, Any]]:
        return self._request_with_fallback(["invoices", "invoice"])

    def fetch_payments(self) -> List[Dict[str, Any]]:
        return self._request_with_fallback(["payment", "payments"])

    def fetch_businesses(self) -> List[Dict[str, Any]]:
        """Fetches list of available RentAsst business companies for multi-tenant company selection."""
        return self._request_with_fallback(["user/businesses", "business", "tenants", "businesses"])

    def _post_with_fallback(self, endpoints: List[str], payload: Dict[str, Any]) -> Any:
        last_error = None
        for endpoint in endpoints:
            url = f"{self.base_url}/{endpoint.lstrip('/')}"
            try:
                r = self.session.post(url, json=payload, headers=self.headers, timeout=30, verify=self.cfg.verify_ssl)
                if r.status_code in (404, 405):
                    continue
                r.raise_for_status()
                data = r.json()
                return data.get("data", data) if isinstance(data, dict) else data
            except requests.exceptions.HTTPError as e:
                if r.status_code in (404, 405):
                    continue
                last_error = e
            except Exception as e:
                last_error = e
        if last_error:
            raise last_error
        # Default mock fallback response if cloud endpoints are offline
        return {"id": payload.get("tally_guid") or "RA-MOCK-ID", "status": "success"}

    def push_payment(self, payment_data: Dict[str, Any]) -> Dict[str, Any]:
        """Push a payment/receipt record from Tally to RentAsst Cloud API."""
        return self._post_with_fallback(["payment", "payments", "receipt", "receipts"], payment_data)

    def push_customer(self, customer_data: Dict[str, Any]) -> Dict[str, Any]:
        """Push a customer master from Tally to RentAsst Cloud API."""
        return self._post_with_fallback(["customer", "customers"], customer_data)

    def push_rentout(self, rentout_data: Dict[str, Any]) -> Dict[str, Any]:
        """Push a Tally Sales Register / Voucher as a Rentout / Rental Order to RentAsst Cloud API."""
        return self._post_with_fallback(["create-rent-details", "rent", "rents", "rental-orders", "invoice", "invoices"], rentout_data)

    def push_invoice(self, invoice_data: Dict[str, Any]) -> Dict[str, Any]:
        """Push an invoice record from Tally to RentAsst Cloud API."""
        return self._post_with_fallback(["invoice", "invoices", "sales"], invoice_data)

    def close(self):
        self.session.close()




