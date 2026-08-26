import threading
import time
import xml.etree.ElementTree as ET
import requests
from typing import Callable, Dict, Any, List, Optional, Tuple
from requests.adapters import HTTPAdapter

from ...models.domain import AppConfig
from .xml_builder import sanitize_tally_xml, build_export_collection_envelope, escape_xml
from .parser import validate_tally_accounting_success
from .company import build_fetch_companies_xml, parse_fetch_companies_response
from .ledger import build_customer_ledger_xml
from .stock_item import build_stock_item_xml, build_physical_stock_voucher_xml, build_unit_xml
from .sales_voucher import build_sales_order_voucher_xml, build_sales_invoice_voucher_xml
from .receipt_voucher import build_receipt_voucher_xml
from ...logging.logger import log_event

# Tally Prime's XML/HTTP server cannot safely handle overlapping requests — concurrent
# imports/exports corrupt its current-company context (observed live as "Tally Business
# Error: Could not set 'SVCurrentCompany' to '<name>'"), stall until one request times out,
# or in the worst case crash the whole Tally process (native "Memory Access Violation",
# observed live after a burst of back-to-back STOCKITEM imports with no gap between them).
# The middleware's queue worker runs multiple entity syncs in parallel threads, each with
# its own TallyClient/session, so this lock is process-wide (not per-instance) to serialize
# every request actually reaching Tally, regardless of which client made it. The minimum
# spacing below additionally paces consecutive requests so Tally's single-threaded import
# pipeline gets a moment to settle, rather than being hit again the instant it responds.
_TALLY_HTTP_LOCK = threading.Lock()
_TALLY_MIN_REQUEST_INTERVAL_SECONDS = 0.4
_last_tally_request_at = 0.0


def _xml_has_exact_field_match(xml_text: str, tags: Tuple[str, ...], identifier: str) -> bool:
    """
    Exact, case-insensitive match against the text of specific fields — NOT a substring
    search over the whole response. check_exists() used to do `identifier.lower() in
    clean.lower()` over the entire raw export, which false-positives on any short or
    common identifier that happens to appear anywhere else in the response (a
    description, another item's name, an HSN note, etc.) — confirmed live: a "Piece"
    unit check returned True (there was no such UNIT master at all) purely because the
    word appeared elsewhere in the STOCKITEM export, so the STOCKITEM XML never included
    the UNIT prerequisite and Tally rejected the whole item with "Unit 'Piece' does not
    exist!". Falls back to the old substring behavior only if the response doesn't even
    parse as XML, so a check never turns into a hard failure over a malformed response.
    """
    needle = identifier.strip().lower()
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return needle in xml_text.lower()
    for elem in root.iter():
        if elem.tag in tags and elem.text and elem.text.strip().lower() == needle:
            return True
    return False


def _tally_post(session: requests.Session, base_url: str, data: bytes, timeout: float) -> requests.Response:
    global _last_tally_request_at
    with _TALLY_HTTP_LOCK:
        wait = _TALLY_MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - _last_tally_request_at)
        if wait > 0:
            time.sleep(wait)
        try:
            # Connection: close forces a fresh TCP connection per request instead of
            # requests' default keep-alive pooling — confirmed live: dozens of
            # connections from long-dead middleware processes (killed abruptly during
            # development, never sending a clean close) were still sitting in
            # CLOSE_WAIT on Tally's listening socket hours later. Tally's embedded HTTP
            # gateway is not a robust, battle-tested server; not giving it long-lived
            # connections to leak in the first place is worth the small per-request
            # handshake cost, especially given the 0.4s pacing below already dominates
            # per-request latency.
            return session.post(
                base_url,
                data=data,
                headers={"Content-Type": "text/xml", "Connection": "close"},
                timeout=timeout,
            )
        finally:
            _last_tally_request_at = time.monotonic()


class TallyClient:
    """
    Modular, production-grade Tally Prime HTTP Client.
    Executes Tally XML transactions, injects dynamic company static variables,
    and enforces strict accounting success validation on HTTP 200 responses.
    """
    def __init__(self, cfg: AppConfig, session: Optional[requests.Session] = None):
        self.cfg = cfg
        self.base_url = cfg.external_url.rstrip("/")
        if session:
            self.session = session
        else:
            self.session = requests.Session()
            adapter = HTTPAdapter(pool_connections=20, pool_maxsize=50)
            self.session.mount("http://", adapter)
            self.session.mount("https://", adapter)

        # A fresh TallyClient/ExternalClient is created for every sync_equipment() batch
        # call (SyncService.execute_sync), so this cache lives exactly as long as one
        # equipment-sync cycle — every item in that cycle sharing the same unit/stock
        # group/stock category (e.g. 9 different assets all using unit "Nos") otherwise
        # re-checked existence AND re-sent a fresh <UNIT ACTION="Create"> for it on every
        # single one of those 9 STOCKITEM imports. Confirmed live: Tally's XML server hit
        # a native "Memory Access Violation" crash after exactly this kind of back-to-back
        # burst of STOCKITEM imports repeatedly recreating the same prerequisite masters —
        # already flagged as a known risk in _tally_post's own module docstring. Caching
        # "confirmed existing" per (entity_type, name) for this client's lifetime removes
        # that redundant traffic entirely instead of just pacing it.
        self._exists_cache: Dict[Tuple[str, str], bool] = {}

        # Set to True the moment auto-detection below (see
        # _send_voucher_with_edu_fallback) discovers this Tally company only accepts
        # Educational-Mode-restricted dates — callers with access to ConfigStore (e.g.
        # SyncService) check this after a sync run to persist tally_edu_mode=True so
        # future runs use it from the start instead of re-discovering it every time.
        self.edu_mode_auto_detected = False

    def _exists_cache_key(self, entity_type: str, identifier: str) -> Tuple[str, str]:
        return ((entity_type or "").lower().strip(), (identifier or "").strip().lower())

    def _mark_exists(self, entity_type: str, identifier: str) -> None:
        """Records a master as now confirmed to exist — called after a STOCKITEM import
        that included a fresh <UNIT>/<STOCKGROUP>/<STOCKCATEGORY> Create succeeds, so the
        next equipment item sharing that same master in this batch skips recreating it."""
        if identifier:
            self._exists_cache[self._exists_cache_key(entity_type, identifier)] = True

    def ping(self) -> bool:
        try:
            with _TALLY_HTTP_LOCK:
                r = self.session.get(self.base_url, timeout=5, verify=self.cfg.verify_ssl)
            return r.status_code in (200, 204, 404, 405)
        except Exception:
            return False

    def send_xml(self, xml_string: str, expect_voucher: bool = False) -> str:
        """
        Posts XML payload to Tally Prime server and enforces strict accounting success criteria.
        Raises ValueError if Tally returns business errors (<LINEERROR>, <ERROR>, <CREATED>0)
        even when the HTTP status code is 200.

        expect_voucher=True (voucher syncs) requires Tally's LASTVCHID to confirm the voucher
        itself was created/altered — see validate_tally_accounting_success for why CREATED/
        ALTERED alone isn't a safe signal for vouchers.
        """
        company_name = getattr(self.cfg, "tally_company_name", None)
        if company_name and "<STATICVARIABLES>" not in xml_string:
            company_var = f"<STATICVARIABLES><SVCURRENTCOMPANY>{escape_xml(company_name)}</SVCURRENTCOMPANY></STATICVARIABLES>"
            xml_string = xml_string.replace("<REQUESTDESC>", f"<REQUESTDESC>{company_var}")

        r = _tally_post(self.session, self.base_url, xml_string.encode("utf-8"), timeout=15)
        r.raise_for_status()

        clean_content = sanitize_tally_xml(r.content)
        is_success, err_msg, tally_id = validate_tally_accounting_success(clean_content, require_voucher=expect_voucher)

        if not is_success:
            raise ValueError(err_msg or "Tally XML transaction failed accounting validation")

        return tally_id or "TALLY-SUCCESS"

    def _send_voucher_with_edu_fallback(self, build_fn: Callable[[bool], str], expect_voucher: bool = True) -> str:
        """
        Some Tally installations run under an Educational/unlicensed mode that silently
        rejects any voucher not dated the 1st, 2nd, or last day of a month — and Tally's
        own rejection ("Voucher date is missing for: '<type>' voucher <name>...") gives
        no hint that it's a license restriction rather than a real data problem.
        Confirmed live: a company running Educational Mode rejected every correctly
        dated Sales/Sales Order voucher this way regardless of payload correctness (a
        hand-built minimal voucher with a valid <DATE> tag was rejected identically),
        while the exact same voucher re-dated to the 1st of the month succeeded
        outright. Detecting this from Tally's own response and retrying once with the
        date forced means an operator no longer has to notice the pattern and manually
        flip tally_edu_mode themselves — it's discovered and remembered automatically.

        build_fn receives the edu_mode to build for and must return the XML to send —
        callers pass a closure since each voucher type's builder has a different
        signature (build_sales_order_voucher_xml, build_receipt_voucher_xml, etc).
        """
        edu_mode = bool(getattr(self.cfg, "tally_edu_mode", False))
        xml = build_fn(edu_mode)
        try:
            return self.send_xml(xml, expect_voucher=expect_voucher)
        except ValueError as e:
            if edu_mode or "voucher date is missing" not in str(e).lower():
                raise
            log_event(
                "ForwardSync",
                f"Tally rejected a real transaction date ('{e}') — this looks like an "
                "Educational/unlicensed Tally company that only accepts vouchers dated "
                "the 1st, 2nd, or last day of a month. Retrying this voucher with that "
                "restriction applied, and switching to it for the rest of this sync.",
            )
            self.cfg.tally_edu_mode = True
            self.edu_mode_auto_detected = True
            retry_xml = build_fn(True)
            return self.send_xml(retry_xml, expect_voucher=expect_voucher)

    def fetch_companies(self) -> List[Dict[str, str]]:
        xml = build_fetch_companies_xml()
        try:
            r = _tally_post(self.session, self.base_url, xml.encode("utf-8"), timeout=10)
            if r.status_code == 200:
                return parse_fetch_companies_response(r.content)
        except Exception:
            pass
        return []

    def check_exists(self, entity_type: str, identifier: str) -> bool:
        if not identifier:
            return True

        cache_key = self._exists_cache_key(entity_type, identifier)
        if cache_key in self._exists_cache:
            return self._exists_cache[cache_key]

        ent = (entity_type or "").lower().strip()
        tally_type = "LEDGER"
        fetch_fields = "NAME"
        match_tags: Tuple[str, ...] = ("NAME",)
        if ent in ("equipment", "product", "products"):
            tally_type = "STOCKITEM"
            fetch_fields = "NAME, MAILINGNAME"
            match_tags = ("NAME", "MAILINGNAME")
        elif ent == "unit":
            tally_type = "UNIT"
            fetch_fields = "NAME"
        elif ent == "stockgroup":
            tally_type = "STOCKGROUP"
            fetch_fields = "NAME"
        elif ent == "stockcategory":
            tally_type = "STOCKCATEGORY"
            fetch_fields = "NAME"
        elif ent in ("rental_orders", "rental_order", "invoices", "invoice", "payments", "payment", "voucher"):
            # NOTE: must be "Voucher" (matching TallyFetcher.fetch_vouchers), not "VOUCHER" —
            # the all-caps native collection name resolves to Voucher Type master config,
            # not actual transaction vouchers. Confirmed live: REMOTEID/VOUCHERNUMBER we send
            # on create are not echoed back by Tally on export, so NARRATION (a plain free-text
            # field Tally preserves verbatim) is what carries our matchable marker instead.
            tally_type = "Voucher"
            fetch_fields = "VOUCHERNUMBER, MASTERID, NARRATION"
            match_tags = ("NARRATION",)

        company_name = getattr(self.cfg, "tally_company_name", None)
        xml = build_export_collection_envelope("CheckExistence", tally_type, fetch_fields, company_name=company_name)


        try:
            r = _tally_post(self.session, self.base_url, xml.encode("utf-8"), timeout=10)
            if r.status_code == 200:
                clean = sanitize_tally_xml(r.content)
                result = _xml_has_exact_field_match(clean, match_tags, identifier)
                self._exists_cache[cache_key] = result
                return result
        except Exception:
            # Not cached — a transient timeout/connection error shouldn't stick as a
            # false "doesn't exist" for the rest of this batch.
            pass
        return False

    def sync_unit(self, name: str, symbol: str = "") -> bool:
        """
        Pre-creates a single Tally UNIT master in its own isolated request, decoupled
        from any STOCKITEM import. Returns True if a Create was actually sent, False if
        the unit was already known to exist (cached or freshly confirmed) — callers use
        this to log/count how many units genuinely needed creating. A no-op for a blank
        name (RentAsst assets without an explicit unit fall back to "Nos" at the
        STOCKITEM layer, not here).
        """
        clean_name = (name or "").strip()
        if not clean_name:
            return False
        if self.check_exists("unit", clean_name):
            return False
        company_name = getattr(self.cfg, "tally_company_name", None)
        xml = build_unit_xml(clean_name, symbol=symbol, action="Create", company_name=company_name)
        self.send_xml(xml, expect_voucher=False)
        self._mark_exists("unit", clean_name)
        return True

    def sync_customer(self, data: Dict[str, Any]) -> str:
        name = (data.get("name") or data.get("business_name") or f"Customer-{data.get('id')}").strip()
        already_in_tally = self.check_exists("customer", name)
        action = "Alter" if already_in_tally else "Create"

        company_name = getattr(self.cfg, "tally_company_name", None)
        xml = build_customer_ledger_xml(data, action=action, company_name=company_name)
        return self.send_xml(xml)

    def sync_equipment(self, data: Dict[str, Any]) -> str:
        name = (data.get("name") or f"Item-{data.get('id')}").strip()

        unit_name = "Nos"
        if isinstance(data.get("asset_unit"), dict):
            unit_name = (data["asset_unit"].get("name") or "Nos").strip()
        elif data.get("asset_unit_name"):
            unit_name = data.get("asset_unit_name").split("(")[0].strip()

        group = "Primary"
        if isinstance(data.get("asset_category"), dict) and data.get("asset_category", {}).get("name"):
            group = data["asset_category"]["name"].strip()

        category = ""
        if isinstance(data.get("asset_brand"), dict) and data.get("asset_brand", {}).get("name"):
            category = data["asset_brand"]["name"].strip()

        unit_exists = self.check_exists("unit", unit_name)
        # Always verify group existence in Tally — even 'Primary' may not exist as an explicit
        # named STOCKGROUP in the company, causing Tally to reject the STOCKITEM with
        # "Stock Group 'Primary' does not exist!"
        group_exists = self.check_exists("stockgroup", group) if group else True
        category_exists = True if not category else self.check_exists("stockcategory", category)

        already_in_tally = self.check_exists("equipment", name)
        action = "Alter" if already_in_tally else "Create"

        company_name = getattr(self.cfg, "tally_company_name", None)
        xml = build_stock_item_xml(
            data,
            action=action,
            unit_exists=unit_exists,
            group_exists=group_exists,
            category_exists=category_exists,
            company_name=company_name,
        )
        result = self.send_xml(xml)

        # Only cache prerequisites we just successfully created — send_xml() already
        # raised above if Tally rejected the import, so reaching here means any Create
        # blocks this XML included genuinely landed. The next equipment item in this
        # same sync cycle sharing the same unit/group/category then skips re-checking
        # and re-sending a Create for it entirely.
        if not unit_exists:
            self._mark_exists("unit", unit_name)
        if group and not group_exists:
            self._mark_exists("stockgroup", group)
        if category and not category_exists:
            self._mark_exists("stockcategory", category)

        return result

    def reconcile_stock_quantity(self, item_name: str, quantity: Any, unit: str = "Nos") -> None:
        """
        Reconciles a stock item's actual Tally quantity via a Physical Stock voucher —
        NOT by re-sending OPENINGBALANCE on the STOCKITEM master. OPENINGBALANCE is a
        fixed baseline as of the books' start date; every Sales voucher pushed since
        keeps consuming against that one baseline, so repeatedly re-sending RentAsst's
        current available_quantity as OPENINGBALANCE never corrects drift (confirmed
        live: one stock item drifted to a CLOSINGBALANCE of -4 despite OPENINGBALANCE
        being resent as its real RentAsst quantity every cycle, because units had
        already been consumed by prior Sales vouchers against that fixed baseline).

        Called unconditionally for every equipment item on every sync cycle — regardless
        of whether that item's own RentAsst data changed — because Tally-side drift comes
        from Sales vouchers consuming stock there, not from RentAsst-side edits, so a
        content-hash "nothing changed, skip" check (as run_sync_pipeline applies to the
        STOCKITEM master push) would never catch it.
        """
        if quantity is None:
            return
        try:
            company_name = getattr(self.cfg, "tally_company_name", None)
            self._send_voucher_with_edu_fallback(
                lambda edu_mode: build_physical_stock_voucher_xml(
                    item_name=item_name, quantity=quantity, unit=unit, company_name=company_name, edu_mode=edu_mode,
                )
            )
        except Exception as e:
            log_event("ForwardSync", f"Failed to reconcile Tally stock quantity for '{item_name}': {e}")

    def sync_rental_order(self, data: Dict[str, Any]) -> str:
        remote_id = f"RENTAL-ORD-{data.get('id')}"
        action = "Alter" if self.check_exists("rental_orders", remote_id) else "Create"
        company_name = getattr(self.cfg, "tally_company_name", None)
        self._send_voucher_with_edu_fallback(
            lambda edu_mode: build_sales_order_voucher_xml(data, action=action, company_name=company_name, edu_mode=edu_mode)
        )
        return remote_id

    def sync_invoice(self, data: Dict[str, Any]) -> str:
        remote_id = f"RENTAL-INV-{data.get('id')}"
        action = "Alter" if self.check_exists("invoices", remote_id) else "Create"
        company_name = getattr(self.cfg, "tally_company_name", None)
        company_state = getattr(self.cfg, "company_state", "") or ""
        self._send_voucher_with_edu_fallback(
            lambda edu_mode: build_sales_invoice_voucher_xml(
                data, action=action, company_state=company_state, company_name=company_name, edu_mode=edu_mode
            )
        )
        return remote_id

    def sync_payment(self, data: Dict[str, Any]) -> str:
        remote_id = f"RENTAL-PAY-{data.get('id')}"
        action = "Alter" if self.check_exists("payments", remote_id) else "Create"
        company_name = getattr(self.cfg, "tally_company_name", None)
        self._send_voucher_with_edu_fallback(
            lambda edu_mode: build_receipt_voucher_xml(data, action=action, company_name=company_name, edu_mode=edu_mode)
        )
        return remote_id
