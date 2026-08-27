import time
import threading
import concurrent.futures
import requests
from requests.adapters import HTTPAdapter
from typing import Dict, Any, List, Optional
from ..models.domain import AppConfig
from ..retry.engine import RetryableException, NonRetryableException


class RentAsstClient:
    # Remembers which endpoint/method actually worked for a given base_url + logical
    # record type, so repeat syncs skip straight to it instead of re-probing every
    # candidate endpoint from scratch. Shared across client instances (a new client
    # is created per sync execution, but the cache should outlive any single one).
    _endpoint_cache: Dict[str, Dict[str, str]] = {}
    _endpoint_cache_lock = threading.Lock()

    # Callers (status polling, idempotency checks, config routes) each construct a
    # fresh RentAsstClient per call rather than sharing one long-lived instance, so
    # the ping cache must live at class level (keyed by base_url) to actually
    # suppress repeat liveness probes across those short-lived instances.
    _ping_cache: Dict[str, Dict[str, Any]] = {}
    _ping_cache_lock = threading.Lock()

    # Single-flight lock per base_url: without this, concurrent/overlapping ping()
    # calls (e.g. overlapping dashboard polls when the backend is slow to respond)
    # can all observe an empty/expired cache at once and each launch their own
    # 5-endpoint probe, multiplying load on an already-struggling backend. Only
    # one probe should ever be in flight per base_url at a time; everyone else
    # waits for it and reuses its result.
    _ping_locks: Dict[str, threading.Lock] = {}
    _ping_locks_creation_lock = threading.Lock()

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.base_url = cfg.rentasst_url.rstrip("/")
        self.session = requests.Session()
        adapter = HTTPAdapter(pool_connections=20, pool_maxsize=50)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self._equipment_cache: Optional[List[Dict[str, Any]]] = None

        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if cfg.rentasst_api_key:
            self.headers["Authorization"] = f"Bearer {cfg.rentasst_api_key}"
            self.headers["X-API-KEY"] = cfg.rentasst_api_key
            self.headers["X-Api-Key"] = cfg.rentasst_api_key
            self.headers["apiKey"] = cfg.rentasst_api_key
            self.headers["apikey"] = cfg.rentasst_api_key
            self.headers["Test-Auth-Key"] = cfg.rentasst_api_key
            self.headers["test-auth-key"] = cfg.rentasst_api_key
            self.headers["testauthkey"] = cfg.rentasst_api_key
        if cfg.rentasst_tenant_id:
            self.headers["BusinessCode"] = cfg.rentasst_tenant_id
            self.headers["businesscode"] = cfg.rentasst_tenant_id
            self.headers["business_code"] = cfg.rentasst_tenant_id
            self.headers["Business-Code"] = cfg.rentasst_tenant_id
            self.headers["TenantId"] = cfg.rentasst_tenant_id
            self.headers["tenant_id"] = cfg.rentasst_tenant_id

    @property
    def config(self) -> AppConfig:
        return self.cfg

    def _get_ping_lock(self) -> threading.Lock:
        lock = self._ping_locks.get(self.base_url)
        if lock is None:
            with self._ping_locks_creation_lock:
                lock = self._ping_locks.get(self.base_url)
                if lock is None:
                    lock = threading.Lock()
                    self._ping_locks[self.base_url] = lock
        return lock

    def _cache_get(self, cache_key: str) -> Optional[Dict[str, str]]:
        return self._endpoint_cache.get(f"{self.base_url}:{cache_key}")

    def _cache_set(self, cache_key: str, method: str, endpoint: str) -> None:
        with self._endpoint_cache_lock:
            self._endpoint_cache[f"{self.base_url}:{cache_key}"] = {"method": method, "endpoint": endpoint}

    @staticmethod
    def _first_success(session: requests.Session, urls: List[str], headers: Dict[str, str], timeout: int, verify: bool, ok_codes=(200, 201, 204)) -> Optional[requests.Response]:
        """
        Fires GET requests at every candidate URL concurrently and returns the first
        response matching ok_codes, without waiting for the slower candidates to finish.
        Safe for read-only endpoint discovery/health probes only.
        """
        if not urls:
            return None
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=min(len(urls), 4))
        futures = {executor.submit(session.get, u, headers=headers, timeout=timeout, verify=verify): u for u in urls}
        result = None
        try:
            for fut in concurrent.futures.as_completed(futures, timeout=timeout + 2):
                try:
                    r = fut.result()
                except Exception:
                    continue
                if r.status_code in ok_codes:
                    result = r
                    break
        except concurrent.futures.TimeoutError:
            pass
        finally:
            executor.shutdown(wait=False)
        return result

    def _post_datatable_fallback(self, endpoints: List[str], body: Dict[str, Any], cache_key: Optional[str] = None, timeout: int = 8) -> Optional[List[Dict[str, Any]]]:
        ordered = endpoints
        if cache_key:
            cached = self._cache_get(cache_key)
            if cached and cached.get("method") == "POST" and cached.get("endpoint") in endpoints:
                ordered = [cached["endpoint"]] + [e for e in endpoints if e != cached["endpoint"]]

        req_body = dict(body or {})
        if self.cfg.rentasst_tenant_id and "business_code" not in req_body:
            req_body["business_code"] = self.cfg.rentasst_tenant_id

        for ep in ordered:
            url = f"{self.base_url}/{ep.lstrip('/')}"
            try:
                r = self.session.post(url, json=req_body, headers=self.headers, timeout=timeout, verify=self.cfg.verify_ssl)
                if r.status_code in (200, 204):
                    content_type = r.headers.get("content-type", "").lower()
                    if "text/html" not in content_type and not (r.text or "").strip().startswith(("<html", "<!doctype")):
                        data = r.json()
                        items = data.get("data", data) if isinstance(data, dict) else data
                        if isinstance(items, list) and len(items) > 0:
                            if cache_key:
                                self._cache_set(cache_key, "POST", ep)
                            return items
            except Exception:
                pass
        return None

    def _request_with_fallback(self, endpoints: List[str], params: Optional[Dict[str, Any]] = None, timeout: int = 8, cache_key: Optional[str] = None) -> Any:
        last_error = None
        req_params = dict(params or {})
        if self.cfg.rentasst_tenant_id:
            if "business_code" not in req_params and "businesscode" not in req_params:
                req_params["business_code"] = self.cfg.rentasst_tenant_id
                req_params["businesscode"] = self.cfg.rentasst_tenant_id

        ordered_endpoints = endpoints
        if cache_key:
            cached = self._cache_get(cache_key)
            if cached and cached.get("method") == "GET" and cached.get("endpoint") in endpoints:
                ordered_endpoints = [cached["endpoint"]] + [e for e in endpoints if e != cached["endpoint"]]

        for endpoint in ordered_endpoints:
            url = f"{self.base_url}/{endpoint.lstrip('/')}"
            try:
                r = self.session.get(url, headers=self.headers, params=req_params, timeout=timeout, verify=self.cfg.verify_ssl)
                if r.status_code in (401, 403):
                    last_error = NonRetryableException(f"RentAsst API {r.status_code} Unauthorized at {url}. Check your API Key/Token.")
                    continue
                if r.status_code in (404, 405):
                    continue
                r.raise_for_status()

                # Ignore non-JSON SPA / HTML responses (e.g. index.html fallback)
                content_type = r.headers.get("content-type", "").lower()
                text_snippet = (r.text or "").strip()
                if "text/html" in content_type or text_snippet.startswith(("<html", "<!doctype", "<!DOCTYPE", "<head", "<body")):
                    continue

                data = r.json()
                result = data.get("data", data) if isinstance(data, dict) else data
                if cache_key:
                    self._cache_set(cache_key, "GET", endpoint)
                return result
            except (requests.exceptions.JSONDecodeError, requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                continue
            except requests.exceptions.HTTPError:
                if r.status_code in (404, 405):
                    continue
                err_text = (r.text or "")[:200].replace("\n", " ").strip()
                last_error = RetryableException(
                    f"RentAsst API at {url} HTTP {r.status_code} Error: {err_text}"
                )
            except NonRetryableException as nre:
                last_error = nre
            except Exception as e:
                last_error = e

        # If all failed with 401, attempt auto-discovery token refresh if available
        if isinstance(last_error, NonRetryableException) and "401" in str(last_error):
            try:
                from ..services.discovery_service import DiscoveryService
                auto_cfg = DiscoveryService.auto_discover_rentasst()
                if auto_cfg and auto_cfg.rentasst_api_key and auto_cfg.rentasst_api_key != self.cfg.rentasst_api_key:
                    self.cfg.rentasst_api_key = auto_cfg.rentasst_api_key
                    self.headers["Authorization"] = f"Bearer {auto_cfg.rentasst_api_key}"
                    for endpoint in endpoints:
                        url = f"{self.base_url}/{endpoint.lstrip('/')}"
                        r = self.session.get(url, headers=self.headers, params=req_params, timeout=timeout, verify=self.cfg.verify_ssl)
                        if r.status_code in (200, 201):
                            text_snippet = (r.text or "").strip()
                            if not text_snippet.startswith(("<html", "<!doctype", "<!DOCTYPE")):
                                data = r.json()
                                return data.get("data", data) if isinstance(data, dict) else data
            except Exception:
                pass
        
        if last_error:
            raise last_error
        raise RetryableException(f"RentAsst API endpoints {endpoints} not found (404)")




    def ping(self) -> bool:
        # Liveness only changes on the timescale of the target server going up/down,
        # so a short-lived cache avoids re-probing on every idempotency/status check -
        # callers construct a fresh client per call, so this is cached by base_url,
        # not on the instance (see _ping_cache class attribute).
        cached = self._ping_cache.get(self.base_url)
        if cached and (time.time() - cached["time"]) < 15:
            return cached["ok"]

        # Single-flight: only one thread actually probes per base_url. Everyone
        # else blocks briefly here and then reuses that probe's result instead of
        # each launching their own redundant burst of requests (a cache-stampede
        # that can overwhelm a slow backend faster than any single probe would).
        with self._get_ping_lock():
            cached = self._ping_cache.get(self.base_url)
            if cached and (time.time() - cached["time"]) < 15:
                return cached["ok"]

            endpoints = ["health", "user/profile", "customer", "customers", ""]
            base = self.base_url.rstrip("/")
            urls = [f"{base}/{ep}".rstrip("/") for ep in endpoints]
            result = self._first_success(self.session, urls, self.headers, timeout=8, verify=self.cfg.verify_ssl, ok_codes=(200, 201, 204, 401, 403))

            if result is not None:
                ok = True
            else:
                try:
                    root_url = base.replace("/api", "")
                    r = self.session.get(root_url, timeout=8, verify=self.cfg.verify_ssl)
                    ok = r.status_code in (200, 204, 301, 302, 404)
                except Exception:
                    ok = False

            with self._ping_cache_lock:
                self._ping_cache[self.base_url] = {"ok": ok, "time": time.time()}
            return ok


    def check_token_validity(self) -> bool:
        """Verifies if the current Bearer token is valid with RentAsst API."""
        if not self.cfg.rentasst_api_key or not self.cfg.rentasst_api_key.strip():
            return False
        endpoints = ["user/profile", "user", "customer", "customers", "asset", "invoices"]
        urls = [f"{self.base_url}/{endpoint}" for endpoint in endpoints]

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=min(len(urls), 4))
        futures = {executor.submit(self.session.get, u, headers=self.headers, timeout=8, verify=self.cfg.verify_ssl): u for u in urls}
        result = True
        try:
            for fut in concurrent.futures.as_completed(futures, timeout=10):
                try:
                    r = fut.result()
                except Exception:
                    continue
                if r.status_code in (200, 201):
                    result = True
                    break
                if r.status_code in (401, 403):
                    result = False
                    break
        except concurrent.futures.TimeoutError:
            pass
        finally:
            executor.shutdown(wait=False)
        return result

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
        elif ent in ("equipment", "equipments", "asset", "assets", "product", "products"):
            endpoints = [f"asset/{rid}", f"equipment/{rid}"]
        else:
            return True

        got_any_response = False
        for endpoint in endpoints:
            url = f"{self.base_url}/{endpoint.lstrip('/')}"
            try:
                r = self.session.get(url, headers=self.headers, timeout=5, verify=self.cfg.verify_ssl)
                got_any_response = True
                if r.status_code not in (200, 204):
                    continue
                # Not every endpoint tried above is a real API route for every entity
                # (e.g. 'customers/{id}' and 'invoice/{id}' aren't real RentAsst routes —
                # only 'customer/{id}' and 'invoices/{id}' are) — confirmed live, an
                # invalid path falls through to the SPA's catch-all route, which returns
                # HTTP 200 with a generic HTML shell instead of a 404. A raw status-code
                # check alone treats that HTML page as "record exists", which is exactly
                # backwards: it masks a genuinely deleted record as still present and
                # blocks any self-healing (re-create) logic keyed off this check forever.
                # A real API response is always JSON; require that before trusting a 200.
                try:
                    data = r.json()
                except ValueError:
                    continue
                payload = data.get("data", data) if isinstance(data, dict) else data
                if isinstance(payload, dict) and payload:
                    return True
                if isinstance(payload, list) and payload:
                    return True
            except Exception:
                continue

        if not got_any_response:
            # Every single attempt failed at the network level (timeout/connection
            # error) — RentAsst's local API is confirmed live to have intermittent
            # outages. Returning False here would tell reverse sync's self-heal logic
            # "this record was deleted," which drops a perfectly valid mapping and
            # creates a genuine duplicate asset the moment the connection recovers
            # (confirmed live: this happened repeatedly to 'Dell Laptop'/'Dell Mouse').
            # Only a real response (even a 404) is trusted to mean "doesn't exist" —
            # a total connectivity failure must fail open instead.
            return True
        return False




    def _deduplicate_customers(self, customers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Deduplicates customer records and disambiguates names to ensure all distinct customers
        can be synchronized to Tally Prime without collisions.
        """
        if not customers or not isinstance(customers, list):
            return []

        unique_customers = []
        seen_keys = set()
        name_counts: Dict[str, int] = {}

        # 1. First pass: Remove exact duplicate customer entries (by ID, or by identical name + phone)
        for c in customers:
            if not isinstance(c, dict):
                continue
            cid = str(c.get("id") or "").strip()
            name = str(c.get("name") or c.get("business_name") or "").strip()
            phone = str(c.get("mobile") or c.get("phone") or "").strip().replace(" ", "").replace("-", "")
            email = str(c.get("email") or "").strip().lower()

            if cid and cid != "0":
                dedup_key = f"id:{cid}"
            elif name and phone:
                dedup_key = f"name_phone:{name.lower()}:{phone}"
            elif name and email:
                dedup_key = f"name_email:{name.lower()}:{email}"
            elif name:
                dedup_key = f"name:{name.lower()}"
            else:
                dedup_key = f"raw:{id(c)}"

            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)
            unique_customers.append(c)

            clean_name = name or f"Customer-{cid}"
            name_counts[clean_name] = name_counts.get(clean_name, 0) + 1

        # 2. Second pass: Disambiguate Tally ledger name if multiple distinct customers share identical names
        for c in unique_customers:
            cid = str(c.get("id") or "").strip()
            name = str(c.get("name") or c.get("business_name") or "").strip() or f"Customer-{cid}"
            phone = str(c.get("mobile") or c.get("phone") or "").strip().replace(" ", "").replace("-", "")

            if name_counts.get(name, 0) > 1:
                if phone:
                    c["tally_name"] = f"{name} ({phone})"
                elif cid:
                    c["tally_name"] = f"{name} #{cid}"
                else:
                    c["tally_name"] = name
            else:
                c["tally_name"] = name

        return unique_customers

    def fetch_units(self) -> List[Dict[str, Any]]:
        """
        Fetches RentAsst's own Unit master list (name/symbol/id) — the same
        'units-dropdown'/'units' endpoint resolve_unit_id() already uses to resolve a
        unit by name. Used by equipment.py's _presync_units to pre-create every unit
        as its own isolated Tally master BEFORE any stock item references one,
        instead of creating units piecemeal, bundled inline into whichever STOCKITEM
        import happens to need one first. Distinct from fetch_asset_units() below,
        which is the full, mapping-tracked "units" sync entity's fetch_func and
        returns a richer, standardized shape (formal_name/uqc_code/decimal_places).
        """
        units = self._request_with_fallback(["units-dropdown", "units"])
        return units if isinstance(units, list) else []

    def fetch_customers(self, updated_after: Optional[str] = None) -> List[Dict[str, Any]]:
        params = {"per_page": 1000, "limit": 1000, "length": 1000}
        if updated_after:
            params["updated_after"] = updated_after

        cache_key = "customers"
        get_endpoints = ["customer", "customers", "customer-list"]
        post_endpoints = ["customer-list", "customers", "customer"]
        post_body: Dict[str, Any] = {"start": 0, "length": 1000}

        raw_customers = None
        # Fast path: last sync's winning method/endpoint, skips discovery entirely.
        cached = self._cache_get(cache_key)
        if cached and cached.get("method") == "POST":
            raw_customers = self._post_datatable_fallback(post_endpoints, post_body, cache_key=cache_key)

        if not raw_customers:
            # 1. Try standard GET endpoints first
            try:
                raw_customers = self._request_with_fallback(get_endpoints, params, timeout=8, cache_key=cache_key)
            except Exception:
                raw_customers = None

            # 2. Try POST DataTable API if GET returned nothing
            if not raw_customers or not isinstance(raw_customers, list) or len(raw_customers) == 0:
                raw_customers = self._post_datatable_fallback(post_endpoints, post_body, cache_key=cache_key)

        if isinstance(raw_customers, list):
            return self._deduplicate_customers(raw_customers)
        return []

    def fetch_asset_units(self) -> List[Dict[str, Any]]:
        """
        Fetches asset units (Units of Measure) from RentAsst REST API.
        Falls back to extracting unique units from asset catalog if dedicated endpoint is unavailable.
        """
        unit_endpoints = ["asset-units", "asset-unit", "units", "unit"]
        try:
            units = self._request_with_fallback(unit_endpoints, timeout=8, cache_key="asset_units")
            if isinstance(units, list) and len(units) > 0:
                standardized = []
                for u in units:
                    if isinstance(u, dict):
                        uid = str(u.get("id") or u.get("name") or u.get("symbol") or "")
                        name = str(u.get("name") or u.get("unit_name") or u.get("symbol") or "Nos").strip()
                        sym = str(u.get("symbol") or u.get("code") or "").strip()
                        standardized.append({
                            "id": uid or name,
                            "name": name,
                            "symbol": sym or name,
                            "formal_name": str(u.get("formal_name") or u.get("original_name") or name).strip(),
                            "uqc_code": str(u.get("uqc_code") or u.get("gstrepuom") or "").strip(),
                            "decimal_places": int(u.get("decimal_places") or u.get("decimalPlaces") or 0),
                        })
                if standardized:
                    return standardized
        except Exception:
            pass

        # Fallback: Extract unique asset units from equipment/assets catalog
        try:
            assets = self.fetch_equipment()
            if isinstance(assets, list):
                extracted = {}
                for item in assets:
                    if not isinstance(item, dict):
                        continue
                    u_name = ""
                    u_sym = ""
                    u_id = ""
                    if isinstance(item.get("asset_unit"), dict):
                        u_obj = item["asset_unit"]
                        u_name = (u_obj.get("name") or "").strip()
                        u_sym = (u_obj.get("symbol") or "").strip()
                        u_id = str(u_obj.get("id") or u_name or "")
                    elif item.get("asset_unit_name"):
                        raw_u = str(item.get("asset_unit_name")).strip()
                        u_name = raw_u.split("(")[0].strip()
                        if "(" in raw_u and ")" in raw_u:
                            u_sym = raw_u.split("(")[1].split(")")[0].strip()
                        u_id = u_name
                    elif item.get("unit"):
                        u_name = str(item.get("unit")).strip()
                        u_id = u_name

                    if u_name and u_name not in extracted:
                        extracted[u_name] = {
                            "id": u_id or u_name,
                            "name": u_name,
                            "symbol": u_sym or u_name,
                            "formal_name": u_name,
                            "decimal_places": 0,
                        }
                if extracted:
                    return list(extracted.values())
        except Exception:
            pass

        return [{"id": "Nos", "name": "Nos", "symbol": "Nos", "formal_name": "Numbers", "decimal_places": 0}]

    def fetch_equipment(self) -> List[Dict[str, Any]]:
        # A single sync execution can call this twice (once as fetch_asset_units'
        # fallback, once for the actual equipment sync step) - reuse the result
        # instead of issuing a second round of network calls. Safe because a fresh
        # RentAsstClient is created per sync execution, so this never goes stale
        # across different sync runs.
        if self._equipment_cache is not None:
            return self._equipment_cache

        cache_key = "equipment"
        get_endpoints = ["equipment", "asset", "assets", "asset-list"]
        post_endpoints = ["asset-list", "assets", "equipment"]
        post_body: Dict[str, Any] = {"start": 0, "length": 1000}

        raw_assets = None
        cached = self._cache_get(cache_key)
        if cached and cached.get("method") == "POST":
            raw_assets = self._post_datatable_fallback(post_endpoints, post_body, cache_key=cache_key)

        if not raw_assets:
            # 1. Try GET equipment / assets first
            try:
                raw_assets = self._request_with_fallback(get_endpoints, {"per_page": 1000, "limit": 1000}, timeout=8, cache_key=cache_key)
            except Exception:
                raw_assets = None

            # 2. Try POST DataTable API if GET returned nothing
            if not raw_assets or not isinstance(raw_assets, list) or len(raw_assets) == 0:
                raw_assets = self._post_datatable_fallback(post_endpoints, post_body, cache_key=cache_key)

        result = raw_assets if isinstance(raw_assets, list) else []

        # The list endpoint's per-item shape often omits hsn_code — fetch the full
        # detail record for any item missing it so reverse-sync's GST/HSN fields
        # actually have something to read.
        enriched = []
        for item in result:
            if isinstance(item, dict) and "id" in item and not item.get("hsn_code"):
                try:
                    detail = self._request_with_fallback([f"asset/{item['id']}", f"equipment/{item['id']}"])
                    if isinstance(detail, dict) and "id" in detail:
                        for k, v in detail.items():
                            if v is not None or k not in item:
                                item[k] = v
                except Exception:
                    pass
            enriched.append(item)

        self._equipment_cache = enriched
        return enriched

    def fetch_rental_orders(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        cache_key = "rental_orders"
        post_endpoints = ["rent-list", "rents", "rental-orders"]
        get_endpoints = ["rents", "rental-orders", "rent-list"]
        post_body: Dict[str, Any] = {"start": 0, "length": 1000}
        if status:
            post_body["status"] = [status]
        params = {"per_page": 1000, "limit": 1000, "length": 1000}
        if status:
            params["status"] = status

        # Fast path: if GET was the winning method last time, skip straight to it
        # (original discovery order tries POST first, so this only fires once warm).
        cached = self._cache_get(cache_key)
        if cached and cached.get("method") == "GET":
            try:
                items = self._request_with_fallback(get_endpoints, params, timeout=8, cache_key=cache_key)
                if isinstance(items, list) and items:
                    return items
            except Exception:
                pass

        # 1. Try POST DataTable API endpoints first (original discovery order)
        items = self._post_datatable_fallback(post_endpoints, post_body, cache_key=cache_key)
        if items:
            return items

        # 2. GET fallback
        try:
            result = self._request_with_fallback(get_endpoints, params, timeout=8, cache_key=cache_key)
            return result if isinstance(result, list) else []
        except Exception:
            return []

    def fetch_invoices(self) -> List[Dict[str, Any]]:
        cache_key = "invoices"
        post_endpoints = ["invoice-list", "invoices"]
        get_endpoints = ["invoices", "invoice-list", "invoice"]
        post_body: Dict[str, Any] = {"start": 0, "length": 1000}

        cached = self._cache_get(cache_key)
        if cached and cached.get("method") == "GET":
            try:
                items = self._request_with_fallback(get_endpoints, {"per_page": 1000, "limit": 1000}, timeout=8, cache_key=cache_key)
                if isinstance(items, list) and items:
                    return items
            except Exception:
                pass

        # 1. Try POST DataTable API endpoints first (original discovery order)
        items = self._post_datatable_fallback(post_endpoints, post_body, cache_key=cache_key)
        if items:
            return items

        try:
            result = self._request_with_fallback(get_endpoints, {"per_page": 1000, "limit": 1000}, timeout=8, cache_key=cache_key)
            return result if isinstance(result, list) else []
        except Exception:
            return []

    def get_invoice(self, invoice_id: str) -> Dict[str, Any]:
        """Fetches a single invoice's full detail, including its 'items' array — used to
        detect an already-synced invoice that's still missing its line items."""
        return self._request_with_fallback([f"invoices/{invoice_id}", f"invoice/{invoice_id}"])

    def get_rentout(self, rent_id: str) -> Dict[str, Any]:
        """Fetches a single rentout's full detail, including 'rent_items_count' — used to
        detect an already-synced rentout that's still missing its asset/quantity/price lines."""
        return self._request_with_fallback([f"get-rent-details/{rent_id}"])

    def get_rent_items(self, rent_id: str) -> List[Dict[str, Any]]:
        """
        Fetches the actual rent_items rows (asset_name, rented_quantity, price,
        total_price) for a rentout — unlike fetch_rental_orders()'s list view, which only
        returns a bare 'rent_items_count' integer with no item detail at all. Confirmed
        live: without this, forward-syncing a Rent Out to Tally always produced a
        header-only Sales Order voucher with zero inventory lines, regardless of how many
        real rent items the order actually had (fetch_invoices() doesn't have this gap —
        its list view already embeds a full 'items' array).
        """
        url = f"{self.base_url}/get-rent-items/{rent_id}"
        r = self.session.get(url, headers=self.headers, timeout=30, verify=self.cfg.verify_ssl)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else data.get("data", [])

    def fetch_payments(self) -> List[Dict[str, Any]]:
        cache_key = "payments"
        post_endpoints = ["payment-list", "payments"]
        get_endpoints = ["payments", "payment-list", "payment"]
        post_body: Dict[str, Any] = {"start": 0, "length": 1000}

        payments = None
        cached = self._cache_get(cache_key)
        if cached and cached.get("method") == "GET":
            try:
                items = self._request_with_fallback(get_endpoints, {"per_page": 1000, "limit": 1000}, timeout=8, cache_key=cache_key)
                if isinstance(items, list) and items:
                    payments = items
            except Exception:
                pass

        if payments is None:
            # 1. Try POST DataTable API endpoints first (original discovery order)
            payments = self._post_datatable_fallback(post_endpoints, post_body, cache_key=cache_key)

        if not payments:
            try:
                result = self._request_with_fallback(get_endpoints, {"per_page": 1000, "limit": 1000}, timeout=8, cache_key=cache_key)
                payments = result if isinstance(result, list) else []
            except Exception:
                payments = []

        if isinstance(payments, list):
            enriched = []
            for pay in payments:
                # Confirmed live: the list endpoint has no 'payment_date' field at all (only
                # 'created_at', which is close but not the same thing) and no 'paid_by'/
                # customer name — the Receipt voucher this builds then falls back to a
                # generic "Cash Customer" ledger instead of the real customer. The detail
                # endpoint (GET /payment/{id}) has payment_date but 'paid_by' is still null
                # there for an invoice-linked payment (only 'rent'-linked payments get a
                # denormalized customer name) — resolve it via the linked invoice instead.
                if isinstance(pay, dict) and pay.get("id") and (not pay.get("payment_date") or not pay.get("paid_by")):
                    try:
                        detail = self._request_with_fallback([f"payment/{pay['id']}"])
                        if isinstance(detail, dict) and "id" in detail:
                            for k, v in detail.items():
                                if v is not None or k not in pay:
                                    pay[k] = v
                    except Exception:
                        pass

                if isinstance(pay, dict) and not pay.get("paid_by") and not (pay.get("rent") or {}).get("customer_name") and pay.get("invoice_id"):
                    try:
                        invoice = self.get_invoice(str(pay["invoice_id"]))
                        invoice = invoice.get("data", invoice) if isinstance(invoice, dict) else invoice
                        cust_name = (invoice or {}).get("customer", {}).get("name") if isinstance(invoice, dict) else None
                        if cust_name:
                            pay["paid_by"] = cust_name
                    except Exception:
                        pass
                enriched.append(pay)
            return enriched
        return payments

    def fetch_businesses(self) -> List[Dict[str, Any]]:
        """
        Fetches list of available RentAsst business companies for multi-tenant company
        selection. 'get_user_business_list' (UserController@getUserActiveBusinesses) is
        the real route confirmed in RentAsst's own routes/api.php — none of
        'user/businesses'/'business'/'tenants'/'businesses' exist, so every call 404'd
        through the whole fallback list and surfaced as a 400 on /api/companies/rentasst.
        """
        return self._request_with_fallback(["get_user_business_list", "user/businesses", "business", "tenants", "businesses"])

    def _post_with_fallback(self, endpoints: List[str], payload: Dict[str, Any]) -> Any:
        last_error = None
        req_payload = dict(payload or {})
        if self.cfg.rentasst_tenant_id:
            if "business_code" not in req_payload and "businesscode" not in req_payload:
                req_payload["business_code"] = self.cfg.rentasst_tenant_id
                req_payload["businesscode"] = self.cfg.rentasst_tenant_id

        for endpoint in endpoints:
            url = f"{self.base_url}/{endpoint.lstrip('/')}"
            try:
                r = self.session.post(url, json=req_payload, headers=self.headers, timeout=8, verify=self.cfg.verify_ssl)


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
        # Every candidate endpoint either errored or 404/405'd (e.g. a wrong base_url
        # or an unreleased route on this RentAsst deployment) — this must be a hard
        # failure, not a fabricated success. Silently returning a fake "RA-MOCK-ID"
        # here previously made the caller save a "synced" mapping for a record that
        # was never actually created in RentAsst Cloud, with no error logged, no dead
        # letter, and no retry — a real financial record (order/invoice/payment/
        # customer/asset) permanently believed synced while not existing at all.
        if last_error:
            raise last_error
        raise RuntimeError(
            f"All candidate endpoints {endpoints} for this push returned 404/405 — "
            "no working RentAsst route found. Refusing to report a fabricated success."
        )

    def push_payment(self, payment_data: Dict[str, Any]) -> Dict[str, Any]:
        """Push a payment/receipt record from Tally to RentAsst Cloud API."""
        return self._post_with_fallback(["payment", "payments", "receipt", "receipts"], payment_data)

    def push_customer(self, customer_data: Dict[str, Any]) -> Dict[str, Any]:
        """Push a customer master from Tally to RentAsst Cloud API."""
        return self._post_with_fallback(["customer", "customers"], customer_data)

    def get_customer(self, customer_id: str) -> Dict[str, Any]:
        """Fetches a single RentAsst customer's full detail (including its 'address' array
        with real address record ids) — used before an address update to know whether to
        POST a new address or PUT an existing one."""
        return self._request_with_fallback([f"customer/{customer_id}"])

    def update_customer(self, customer_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Update an existing RentAsst customer with corrected Tally-side contact/GST
        details — used when a customer's Tally ledger changes after its first reverse sync
        (mirrors update_equipment's pattern for the same reason).

        NOTE: confirmed live against RentAsst's own PUT /customer/{id} — it validates the
        FULL record, not a partial patch: omitting 'name' 422s with "The name field is
        required" even though only mobile/email/GST are being changed. The caller MUST
        include 'name' in payload. Also confirmed live: this endpoint's own 'gst_number'
        key is silently ignored — RentAsst's actual field is 'customer_gst_number' (matches
        what GET returns) — and an embedded 'address' key does nothing at all on this
        endpoint; address changes must go through create_customer_address/
        update_customer_address instead.
        """
        url = f"{self.base_url}/customer/{customer_id}"
        update_payload = dict(payload)
        if str(customer_id).isdigit():
            update_payload["id"] = int(customer_id)

        r = self.session.put(url, json=update_payload, headers=self.headers, timeout=30, verify=self.cfg.verify_ssl)
        r.raise_for_status()
        data = r.json()
        return data.get("data", data) if isinstance(data, dict) else data

    def create_customer_address(self, customer_id: str, address: Dict[str, Any]) -> Dict[str, Any]:
        """
        Creates a new address record for a RentAsst customer. Confirmed live: this is a
        completely separate resource from the customer record itself — POST /customer/{id}
        (via push_customer) and PUT /customer/{id} (via update_customer) both silently
        ignore an embedded 'address' key. The real endpoint is POST /customer/{id}/address,
        which requires 'full_address' (a single pre-joined string — confirmed live via a
        422 "The full address field is required" when it's missing).
        """
        url = f"{self.base_url}/customer/{customer_id}/address"
        create_payload = dict(address)
        if str(customer_id).isdigit():
            create_payload.setdefault("customer_id", int(customer_id))
        r = self.session.post(url, json=create_payload, headers=self.headers, timeout=30, verify=self.cfg.verify_ssl)
        r.raise_for_status()
        data = r.json()
        return data.get("data", data) if isinstance(data, dict) else data

    def update_customer_address(self, customer_id: str, address_id: str, address: Dict[str, Any]) -> Dict[str, Any]:
        """Updates an existing RentAsst customer address record via PUT /customer/{id}/
        address/{address_id} — confirmed live as the counterpart to create_customer_address
        for a customer that already has an address on file."""
        url = f"{self.base_url}/customer/{customer_id}/address/{address_id}"
        r = self.session.put(url, json=dict(address), headers=self.headers, timeout=30, verify=self.cfg.verify_ssl)
        r.raise_for_status()
        data = r.json()
        return data.get("data", data) if isinstance(data, dict) else data

    def push_equipment(self, equipment_data: Dict[str, Any]) -> Dict[str, Any]:
        """Push an equipment/asset record from Tally to RentAsst Cloud API."""
        return self._post_with_fallback(["asset", "equipment", "assets"], equipment_data)

    def get_equipment(self, asset_id: str) -> Dict[str, Any]:
        """
        Fetches a single RentAsst asset's full detail — used before updating a
        RentAsst-native asset from Tally-side data (GST/HSN/rent price/description) to
        preserve fields reverse sync must never blindly recompute or force, most
        importantly skip_inventory. Confirmed live: PUT /asset/{id} with
        skip_inventory=True on an asset that's currently skip_inventory=False and has real
        rental history 500s with "Asset has inventory history. Archive stock first before
        disabling inventory tracking." — the asset's own CURRENT skip_inventory value must
        be read back and sent unchanged, never forced either way.
        """
        return self._request_with_fallback([f"asset/{asset_id}"])

    def update_equipment(self, asset_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Update an existing asset/equipment in RentAsst with Tally attributes."""
        url = f"{self.base_url}/asset/{asset_id}"
        update_payload = dict(payload)
        if str(asset_id).isdigit():
            update_payload["id"] = int(asset_id)
        update_payload.setdefault("skip_inventory", True)

        r = self.session.put(url, json=update_payload, headers=self.headers, timeout=30, verify=self.cfg.verify_ssl)
        r.raise_for_status()
        data = r.json()
        return data.get("data", data) if isinstance(data, dict) else data

    def resolve_category_id(self, category_name: str) -> Optional[int]:
        """Resolve or auto-create Category ID in RentAsst."""
        if not category_name or category_name.lower() in ("primary", "not applicable", ""):
            return None
        clean_name = category_name.strip()
        try:
            cats = self._request_with_fallback(["asset-category-dropdown", "asset-category", "categories"])
            if isinstance(cats, list):
                for c in cats:
                    if str(c.get("name") or "").strip().lower() == clean_name.lower() and c.get("id"):
                        return int(c["id"])
            # Create category if not present
            res = self._post_with_fallback(["asset-category", "categories"], {"name": clean_name})
            if isinstance(res, dict) and res.get("id"):
                return int(res["id"])
        except Exception:
            pass
        return None

    def resolve_unit_id(self, unit_name: str) -> Optional[int]:
        """Resolve Unit ID in RentAsst."""
        if not unit_name or unit_name.lower() in ("not applicable", ""):
            return None
        clean_unit = unit_name.strip().lower()
        try:
            units = self._request_with_fallback(["units-dropdown", "units"])
            if isinstance(units, list):
                for u in units:
                    uname = str(u.get("name") or "").strip().lower()
                    usym = str(u.get("symbol") or "").strip().lower()
                    if (clean_unit in (uname, usym) or (clean_unit in ("pc", "pcs", "piece", "pieces") and usym in ("pc", "pcs", "piece", "pieces", "nos"))) and u.get("id"):
                        return int(u["id"])
        except Exception:
            pass
        return None

    def push_rentout(self, rentout_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Push a Tally Sales Order voucher as a Rentout / Rental Order to RentAsst Cloud API.

        NOTE: 'create-rent-details' (RentController@createRentDetails) is the only real,
        validated create endpoint for this entity — confirmed against RentAsst's own
        routes/api.php and RentDetailsRequest source. Previous versions of this fallback
        list also tried 'rent'/'rents'/'rental-orders'/'invoice'/'invoices': 'rent' POST
        maps to a store() method RentController doesn't define, and 'invoice'/'invoices'
        are a completely different resource. _post_with_fallback only advances past a 404/
        405 ("this path doesn't exist"), not past a 422 ("this path exists but rejected the
        payload") — so any genuine validation error at create-rent-details was silently
        masked by cascading into the invoice-create endpoint instead, which naturally also
        422'd on a rentout-shaped payload and produced a misleading error pointing at
        /invoices. Keeping a single, correct endpoint here makes create-rent-details'
        actual validation error the one that surfaces.
        """
        return self._post_with_fallback(["create-rent-details"], rentout_data)

    def update_rentout(self, rent_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Updates header fields on an existing RentAsst rentout via the same
        create-rent-details endpoint used to create it (POST, not PUT — confirmed against
        RentAsst's own routes/api.php: 'update-rent-details/{id}' is a POST route, and
        RentDetailsRequest's rules() branch on $this->isMethod('post')/('put'), not the
        HTTP verb's REST semantics). Used to patch a rentout's 'settings' column when it's
        null — see DEFAULT_RENTOUT_SETTINGS in tally_to_rentasst.py for why that's needed
        before rent items can be added to it.
        """
        url = f"{self.base_url}/update-rent-details/{rent_id}"
        r = self.session.post(url, json=payload, headers=self.headers, timeout=30, verify=self.cfg.verify_ssl)
        r.raise_for_status()
        data = r.json()
        return data.get("data", data) if isinstance(data, dict) else data

    def push_rentout_items(self, rent_id: str, items: List[Dict[str, Any]]) -> Any:
        """
        Creates rent items (asset, quantity, price) on an existing RentAsst rentout via
        the bulk rent-items endpoint. create-rent-details's own 'items' field is silently
        ignored — RentItem is a separate model/table (rent_items), not a column on Rent
        (confirmed against RentAsst's own RentService::createNewRent(), which calls
        Rent::create($requestData) directly) — so line items must be pushed here instead,
        after the rentout itself exists.

        RentItem::arrayRules() validates the POST body as a plain top-level JSON array of
        item objects (not wrapped in an {"items": [...]} envelope), each requiring at
        least 'rented_quantity'. This must only be called once per rentout creation —
        there's no upsert-by-asset here, so calling it again would create duplicate rows.
        """
        url = f"{self.base_url}/store/rent_items/{rent_id}"
        payload = []
        for it in items:
            row = dict(it)
            if str(rent_id).isdigit():
                row["rent_id"] = int(rent_id)
            payload.append(row)
        r = self.session.post(url, json=payload, headers=self.headers, timeout=30, verify=self.cfg.verify_ssl)
        r.raise_for_status()
        data = r.json()
        return data.get("data", data) if isinstance(data, dict) else data

    def push_invoice(self, invoice_data: Dict[str, Any]) -> Dict[str, Any]:
        """Push an invoice record from Tally to RentAsst Cloud API."""
        return self._post_with_fallback(["invoice", "invoices", "sales"], invoice_data)

    def update_invoice(self, invoice_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Update header fields (status, amounts, dates) on an existing RentAsst invoice.
        NOTE: RentAsst's invoice update endpoint silently ignores an 'items' key (not a
        fillable column on the Invoice model) — line items must go through
        push_invoice_items() instead, never through this call.
        """
        url = f"{self.base_url}/invoices/{invoice_id}"
        r = self.session.put(url, json=payload, headers=self.headers, timeout=30, verify=self.cfg.verify_ssl)
        r.raise_for_status()
        data = r.json()
        return data.get("data", data) if isinstance(data, dict) else data

    def push_invoice_items(self, invoice_id: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Creates line items on an existing RentAsst invoice via the bulk-create endpoint.
        Invoice line items are a separate resource from the invoice itself in RentAsst
        (InvoiceItem, not a column on Invoice) — sending them as part of the invoice
        create/update payload is silently dropped, confirmed against RentAsst's own
        InvoiceService/InvoiceItemController source. This must only be called once per
        invoice creation — the endpoint appends rows rather than replacing them, so
        calling it again on an already-itemized invoice would create duplicates.
        """
        url = f"{self.base_url}/invoices/{invoice_id}/items-bulk-create"
        r = self.session.post(url, json={"items": items}, headers=self.headers, timeout=30, verify=self.cfg.verify_ssl)
        r.raise_for_status()
        data = r.json()
        return data.get("data", data) if isinstance(data, dict) else data

    def close(self):
        self.session.close()




