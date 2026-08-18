import requests
from typing import Dict, Any, List, Optional
from requests.adapters import HTTPAdapter

from ...models.domain import AppConfig
from .xml_builder import sanitize_tally_xml, build_export_collection_envelope
from .parser import validate_tally_accounting_success, parse_tally_xml
from .company import build_fetch_companies_xml, parse_fetch_companies_response
from .ledger import build_customer_ledger_xml
from .stock_item import build_stock_item_xml
from .sales_voucher import build_sales_order_voucher_xml, build_sales_invoice_voucher_xml
from .receipt_voucher import build_receipt_voucher_xml


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

    def ping(self) -> bool:
        try:
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
            company_var = f"<STATICVARIABLES><SVCURRENTCOMPANY>{company_name}</SVCURRENTCOMPANY></STATICVARIABLES>"
            xml_string = xml_string.replace("<REQUESTDESC>", f"<REQUESTDESC>{company_var}")

        headers = {"Content-Type": "text/xml"}
        r = self.session.post(self.base_url, data=xml_string.encode("utf-8"), headers=headers, timeout=15)
        r.raise_for_status()

        clean_content = sanitize_tally_xml(r.content)
        is_success, err_msg, tally_id = validate_tally_accounting_success(clean_content, require_voucher=expect_voucher)

        if not is_success:
            raise ValueError(err_msg or "Tally XML transaction failed accounting validation")

        return tally_id or "TALLY-SUCCESS"

    def fetch_companies(self) -> List[Dict[str, str]]:
        xml = build_fetch_companies_xml()
        try:
            r = self.session.post(self.base_url, data=xml.encode("utf-8"), headers={"Content-Type": "text/xml"}, timeout=10)
            if r.status_code == 200:
                return parse_fetch_companies_response(r.content)
        except Exception:
            pass
        return []

    def check_exists(self, entity_type: str, identifier: str) -> bool:
        if not identifier:
            return True

        ent = (entity_type or "").lower().strip()
        tally_type = "LEDGER"
        fetch_fields = "NAME"
        if ent in ("equipment", "product", "products"):
            tally_type = "STOCKITEM"
            fetch_fields = "NAME, MAILINGNAME"
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

        company_name = getattr(self.cfg, "tally_company_name", None)
        xml = build_export_collection_envelope("CheckExistence", tally_type, fetch_fields, company_name=company_name)


        try:
            r = self.session.post(self.base_url, data=xml.encode("utf-8"), headers={"Content-Type": "text/xml"}, timeout=10)
            if r.status_code == 200:
                clean = sanitize_tally_xml(r.content)
                return identifier.lower() in clean.lower()
        except Exception:
            pass
        return False

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
        return self.send_xml(xml)

    def sync_rental_order(self, data: Dict[str, Any]) -> str:
        remote_id = f"RENTAL-ORD-{data.get('id')}"
        action = "Alter" if self.check_exists("rental_orders", remote_id) else "Create"
        company_name = getattr(self.cfg, "tally_company_name", None)
        xml = build_sales_order_voucher_xml(data, action=action, company_name=company_name)
        self.send_xml(xml, expect_voucher=True)
        return remote_id

    def sync_invoice(self, data: Dict[str, Any]) -> str:
        remote_id = f"RENTAL-INV-{data.get('id')}"
        action = "Alter" if self.check_exists("invoices", remote_id) else "Create"
        company_name = getattr(self.cfg, "tally_company_name", None)
        company_state = getattr(self.cfg, "company_state", "") or ""
        xml = build_sales_invoice_voucher_xml(data, action=action, company_state=company_state, company_name=company_name)
        self.send_xml(xml, expect_voucher=True)
        return remote_id

    def sync_payment(self, data: Dict[str, Any]) -> str:
        remote_id = f"RENTAL-PAY-{data.get('id')}"
        action = "Alter" if self.check_exists("payments", remote_id) else "Create"
        company_name = getattr(self.cfg, "tally_company_name", None)
        xml = build_receipt_voucher_xml(data, action=action, company_name=company_name)
        self.send_xml(xml, expect_voucher=True)
        return remote_id
