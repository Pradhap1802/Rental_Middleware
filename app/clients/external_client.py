import re
import xml.etree.ElementTree as ET
import requests
from datetime import datetime
from requests.adapters import HTTPAdapter
from typing import Dict, Any, List, Optional
from ..models.domain import AppConfig


def sanitize_tally_xml(raw: Any) -> str:
    """Sanitizes raw Tally XML responses by stripping control chars, BOM, and namespaces."""
    if isinstance(raw, bytes):
        txt = raw.decode("utf-8", errors="replace")
    else:
        txt = str(raw)
    txt = re.sub(r"&#\d+;", "", txt)
    txt = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", txt)
    txt = txt.lstrip("\ufeff")
    txt = re.sub(r'\s+xmlns(?::[A-Za-z_][A-Za-z0-9_.-]*)?\s*=\s*"[^"]*"', "", txt)
    txt = re.sub(r"\s+xmlns(?::[A-Za-z_][A-Za-z0-9_.-]*)?\s*=\s*'[^']*'", "", txt)
    txt = re.sub(r"<(/?)([A-Za-z_][A-Za-z0-9_.-]*):([A-Za-z_][A-Za-z0-9_.-]*)", r"<\1\3", txt)
    return txt.strip()


def normalize_state_name(state_raw: str) -> str:
    """Passes state name directly as it is from RentAsst to Tally."""
    if not state_raw:
        return ""
    return str(state_raw).strip().title()


def format_tally_date(raw_date: Optional[str]) -> str:
    """Converts dates to Tally YYYYMMDD string format."""
    if not raw_date:
        return datetime.now().strftime("%Y%m%d")
    try:
        dt_str = str(raw_date).split("T")[0].replace("-", "")
        if len(dt_str) == 8 and dt_str.isdigit():
            return dt_str
        parts = re.split(r"[./-]", str(raw_date).split("T")[0])
        if len(parts) == 3:
            if len(parts[0]) == 4:
                return f"{parts[0]}{parts[1].zfill(2)}{parts[2].zfill(2)}"
            elif len(parts[2]) == 4:
                return f"{parts[2]}{parts[1].zfill(2)}{parts[0].zfill(2)}"
    except Exception:
        pass
    return datetime.now().strftime("%Y%m%d")


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
            return r.status_code in (200, 204, 404, 405)
        except Exception:
            return False

    def _post_tally_xml(self, xml_string: str) -> str:
        headers = {"Content-Type": "text/xml"}
        r = self.session.post(self.base_url, data=xml_string.encode("utf-8"), headers=headers, timeout=15)
        r.raise_for_status()
        clean = sanitize_tally_xml(r.content)
        try:
            root = ET.fromstring(clean)
            line_error = root.findtext(".//LINEERROR")
            if line_error:
                raise ValueError(f"Tally XML Import Error: {line_error}")
            created = root.findtext(".//CREATED")
            altered = root.findtext(".//ALTERED")
            if created or altered:
                return f"TALLY-ID-{created or altered}"
        except ValueError:
            raise
        except Exception:
            pass
        return "TALLY-SUCCESS"

    def sync_customer(self, data: Dict[str, Any]) -> str:
        if self.cfg.external_system_type == "tally":
            name = data.get("name") or data.get("business_name") or f"Customer-{data.get('id')}"
            mailing_name = data.get("business_name") or name
            mobile = data.get("mobile") or data.get("phone") or ""
            email = data.get("email") or ""
            gst = data.get("customer_gst_number") or data.get("gst_number") or ""
            gst_type = "Regular" if gst else "Unregistered"

            addr1, addr2, city, state, country, pincode = "", "", "", "", "India", ""
            addresses = data.get("address")
            if isinstance(addresses, list) and len(addresses) > 0:
                default_addr = next((a for a in addresses if a.get("is_default")), addresses[0])
                addr1 = default_addr.get("address1") or ""
                addr2 = default_addr.get("address2") or ""
                city = default_addr.get("city") or ""
                state = normalize_state_name(default_addr.get("state") or "")
                country = default_addr.get("country") or "India"
                pincode = default_addr.get("zipcode") or default_addr.get("pincode") or ""
            elif isinstance(addresses, dict):
                addr1 = addresses.get("address1") or ""
                addr2 = addresses.get("address2") or ""
                city = addresses.get("city") or ""
                state = normalize_state_name(addresses.get("state") or "")
                country = addresses.get("country") or "India"
                pincode = addresses.get("zipcode") or addresses.get("pincode") or ""

            addr_nodes = ""
            for line in [addr1, addr2, city]:
                if line:
                    addr_nodes += f"<ADDRESS>{line}</ADDRESS>\n"
            if not addr_nodes:
                addr_nodes = f"<ADDRESS>{name}</ADDRESS>\n"

            gst_block = ""
            if gst:
                gst_block = f"""<LEDGSTREGDETAILS.LIST>
              <APPLICABLEFROM>20260401</APPLICABLEFROM>
              <GSTREGISTRATIONTYPE>{gst_type}</GSTREGISTRATIONTYPE>
              <GSTIN>{gst}</GSTIN>
            </LEDGSTREGDETAILS.LIST>"""

            xml = f"""<ENVELOPE>
  <HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC><REPORTNAME>All Masters</REPORTNAME></REQUESTDESC>
      <REQUESTDATA>
        <TALLYMESSAGE xmlns:UDF="TallyUDF">
          <LEDGER NAME="{name}" ACTION="Create">
            <NAME>{name}</NAME>
            <PARENT>Sundry Debtors</PARENT>
            <MAILINGNAME>{mailing_name}</MAILINGNAME>
            <LEDGERPHONE>{mobile}</LEDGERPHONE>
            <LEDGERMOBILE>{mobile}</LEDGERMOBILE>
            <PHONE>{mobile}</PHONE>
            <EMAIL>{email}</EMAIL>
            <ADDRESS.LIST>
              {addr_nodes}
            </ADDRESS.LIST>
            <LEDSTATENAME>{state}</LEDSTATENAME>
            <COUNTRYNAME>{country}</COUNTRYNAME>
            <PINCODE>{pincode}</PINCODE>
            <GSTREGISTRATIONTYPE>{gst_type}</GSTREGISTRATIONTYPE>
            <PARTYGSTIN>{gst}</PARTYGSTIN>
            {gst_block}
          </LEDGER>
        </TALLYMESSAGE>
      </REQUESTDATA>
    </IMPORTDATA>
  </BODY>
</ENVELOPE>"""
            return self._post_tally_xml(xml)
        else:
            url = f"{self.base_url}/api/customers"
            r = self.session.post(url, json=data, headers=self.headers, timeout=10, verify=self.cfg.verify_ssl)
            r.raise_for_status()
            res = r.json()
            return str(res.get("id") or res.get("external_id") or data.get("id"))

    def sync_equipment(self, data: Dict[str, Any]) -> str:
        if self.cfg.external_system_type == "tally":
            name = data.get("name") or f"Item-{data.get('id')}"
            unit = "Nos"
            if isinstance(data.get("asset_unit"), dict) and data.get("asset_unit", {}).get("name"):
                unit = data["asset_unit"]["name"]
            elif data.get("asset_unit_name"):
                unit = data.get("asset_unit_name").split("(")[0].strip()

            group = "Primary"
            if isinstance(data.get("asset_category"), dict) and data.get("asset_category", {}).get("name"):
                group = data["asset_category"]["name"]

            purchase_price = data.get("purchase_price") or 0
            rent_price = data.get("rent_price") or data.get("day_based_rent_price") or 0

            xml = f"""<ENVELOPE>
  <HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC><REPORTNAME>All Masters</REPORTNAME></REQUESTDESC>
      <REQUESTDATA>
        <TALLYMESSAGE xmlns:UDF="TallyUDF">
          <UNIT NAME="{unit}" ACTION="Create">
            <NAME>{unit}</NAME>
            <ISSIMPLEUNIT>YES</ISSIMPLEUNIT>
          </UNIT>
          <STOCKGROUP NAME="{group}" ACTION="Create">
            <NAME>{group}</NAME>
          </STOCKGROUP>
          <STOCKITEM NAME="{name}" ACTION="Create">
            <NAME>{name}</NAME>
            <PARENT>{group}</PARENT>
            <BASEUNITS>{unit}</BASEUNITS>
            <OPENINGRATE>{rent_price}/{unit}</OPENINGRATE>
            <OPENINGVALUE>{purchase_price}</OPENINGVALUE>
            <STANDARDPRICE.LIST>
              <RATE>{rent_price}/{unit}</RATE>
            </STANDARDPRICE.LIST>
            <STANDARDCOST.LIST>
              <RATE>{purchase_price}/{unit}</RATE>
            </STANDARDCOST.LIST>
          </STOCKITEM>
        </TALLYMESSAGE>
      </REQUESTDATA>
    </IMPORTDATA>
  </BODY>
</ENVELOPE>"""
            return self._post_tally_xml(xml)
        else:
            url = f"{self.base_url}/api/equipment"
            r = self.session.post(url, json=data, headers=self.headers, timeout=10, verify=self.cfg.verify_ssl)
            r.raise_for_status()
            res = r.json()
            return str(res.get("id") or data.get("id"))

    def sync_rental_order(self, data: Dict[str, Any]) -> str:
        if self.cfg.external_system_type == "tally":
            num = data.get("number") or data.get("rent_code") or f"ORD-{data.get('id')}"
            cust_name = data.get("customer_name") or data.get("customer", {}).get("name") or f"Customer-{data.get('customer_id')}"
            amount = data.get("amount") or data.get("total_amount") or 0
            date_str = format_tally_date(data.get("rent_date") or data.get("date") or data.get("created_at"))

            xml = f"""<ENVELOPE>
  <HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC><REPORTNAME>Vouchers</REPORTNAME></REQUESTDESC>
      <REQUESTDATA>
        <TALLYMESSAGE xmlns:UDF="TallyUDF">
          <LEDGER NAME="{cust_name}" ACTION="Create">
            <NAME>{cust_name}</NAME>
            <PARENT>Sundry Debtors</PARENT>
          </LEDGER>
          <LEDGER NAME="Sales Account" ACTION="Create">
            <NAME>Sales Account</NAME>
            <PARENT>Sales Accounts</PARENT>
          </LEDGER>
          <VOUCHER VTYPE="Sales Order" ACTION="Create">
            <DATE>{date_str}</DATE>
            <EFFECTIVEDATE>{date_str}</EFFECTIVEDATE>
            <VOUCHERTYPENAME>Sales Order</VOUCHERTYPENAME>
            <VOUCHERNUMBER>{num}</VOUCHERNUMBER>
            <PARTYLEDGERNAME>{cust_name}</PARTYLEDGERNAME>
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>{cust_name}</LEDGERNAME>
              <ISDEEMEDPOSITIVE>YES</ISDEEMEDPOSITIVE>
              <AMOUNT>-{amount}</AMOUNT>
            </ALLLEDGERENTRIES.LIST>
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>Sales Account</LEDGERNAME>
              <ISDEEMEDPOSITIVE>NO</ISDEEMEDPOSITIVE>
              <AMOUNT>{amount}</AMOUNT>
            </ALLLEDGERENTRIES.LIST>
          </VOUCHER>
        </TALLYMESSAGE>
      </REQUESTDATA>
    </IMPORTDATA>
  </BODY>
</ENVELOPE>"""
            return self._post_tally_xml(xml)
        else:
            url = f"{self.base_url}/api/rental-orders"
            r = self.session.post(url, json=data, headers=self.headers, timeout=10, verify=self.cfg.verify_ssl)
            r.raise_for_status()
            res = r.json()
            return str(res.get("id") or data.get("id"))

    def sync_invoice(self, data: Dict[str, Any]) -> str:
        if self.cfg.external_system_type == "tally":
            num = data.get("number") or data.get("invoice_number") or f"INV-{data.get('id')}"
            cust_name = data.get("customer_name") or data.get("customer", {}).get("name") or f"Customer-{data.get('customer_id')}"
            amount = data.get("amount") or data.get("total_amount") or data.get("net_amount") or 0
            vtype = "Credit Note" if data.get("document_type") == "credit_note" else "Sales"
            date_str = format_tally_date(data.get("invoice_date") or data.get("date") or data.get("created_at"))

            xml = f"""<ENVELOPE>
  <HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC><REPORTNAME>Vouchers</REPORTNAME></REQUESTDESC>
      <REQUESTDATA>
        <TALLYMESSAGE xmlns:UDF="TallyUDF">
          <LEDGER NAME="{cust_name}" ACTION="Create">
            <NAME>{cust_name}</NAME>
            <PARENT>Sundry Debtors</PARENT>
          </LEDGER>
          <LEDGER NAME="Rental Income" ACTION="Create">
            <NAME>Rental Income</NAME>
            <PARENT>Sales Accounts</PARENT>
          </LEDGER>
          <VOUCHER VTYPE="{vtype}" ACTION="Create">
            <DATE>{date_str}</DATE>
            <EFFECTIVEDATE>{date_str}</EFFECTIVEDATE>
            <VOUCHERTYPENAME>{vtype}</VOUCHERTYPENAME>
            <VOUCHERNUMBER>{num}</VOUCHERNUMBER>
            <PARTYLEDGERNAME>{cust_name}</PARTYLEDGERNAME>
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>{cust_name}</LEDGERNAME>
              <ISDEEMEDPOSITIVE>YES</ISDEEMEDPOSITIVE>
              <AMOUNT>-{amount}</AMOUNT>
            </ALLLEDGERENTRIES.LIST>
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>Rental Income</LEDGERNAME>
              <ISDEEMEDPOSITIVE>NO</ISDEEMEDPOSITIVE>
              <AMOUNT>{amount}</AMOUNT>
            </ALLLEDGERENTRIES.LIST>
          </VOUCHER>
        </TALLYMESSAGE>
      </REQUESTDATA>
    </IMPORTDATA>
  </BODY>
</ENVELOPE>"""
            return self._post_tally_xml(xml)
        else:
            url = f"{self.base_url}/api/invoices"
            r = self.session.post(url, json=data, headers=self.headers, timeout=10, verify=self.cfg.verify_ssl)
            r.raise_for_status()
            res = r.json()
            return str(res.get("id") or data.get("id"))

    def sync_payment(self, data: Dict[str, Any]) -> str:
        if self.cfg.external_system_type == "tally":
            ref = data.get("reference_id") or data.get("payment_number") or f"PAY-{data.get('id')}"
            cust_name = data.get("customer_name") or data.get("customer", {}).get("name") or "Bank/Cash Customer"
            amount = data.get("amount") or data.get("paid_amount") or 0
            date_str = format_tally_date(data.get("payment_date") or data.get("date") or data.get("created_at"))

            pay_method = str(data.get("payment_method") or data.get("mode") or "").lower()
            cash_bank_ledger = "Bank Account" if any(w in pay_method for w in ["bank", "online", "card", "upi", "cheque", "transfer"]) else "Cash"
            parent_group = "Bank Accounts" if cash_bank_ledger == "Bank Account" else "Cash-in-Hand"

            xml = f"""<ENVELOPE>
  <HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC><REPORTNAME>Vouchers</REPORTNAME></REQUESTDESC>
      <REQUESTDATA>
        <TALLYMESSAGE xmlns:UDF="TallyUDF">
          <LEDGER NAME="{cust_name}" ACTION="Create">
            <NAME>{cust_name}</NAME>
            <PARENT>Sundry Debtors</PARENT>
          </LEDGER>
          <LEDGER NAME="{cash_bank_ledger}" ACTION="Create">
            <NAME>{cash_bank_ledger}</NAME>
            <PARENT>{parent_group}</PARENT>
          </LEDGER>
          <VOUCHER VTYPE="Receipt" ACTION="Create">
            <DATE>{date_str}</DATE>
            <EFFECTIVEDATE>{date_str}</EFFECTIVEDATE>
            <VOUCHERTYPENAME>Receipt</VOUCHERTYPENAME>
            <VOUCHERNUMBER>{ref}</VOUCHERNUMBER>
            <PARTYLEDGERNAME>{cust_name}</PARTYLEDGERNAME>
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>{cash_bank_ledger}</LEDGERNAME>
              <ISDEEMEDPOSITIVE>YES</ISDEEMEDPOSITIVE>
              <AMOUNT>-{amount}</AMOUNT>
            </ALLLEDGERENTRIES.LIST>
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>{cust_name}</LEDGERNAME>
              <ISDEEMEDPOSITIVE>NO</ISDEEMEDPOSITIVE>
              <AMOUNT>{amount}</AMOUNT>
            </ALLLEDGERENTRIES.LIST>
          </VOUCHER>
        </TALLYMESSAGE>
      </REQUESTDATA>
    </IMPORTDATA>
  </BODY>
</ENVELOPE>"""
            return self._post_tally_xml(xml)
        else:
            url = f"{self.base_url}/api/payments"
            r = self.session.post(url, json=data, headers=self.headers, timeout=10, verify=self.cfg.verify_ssl)
            r.raise_for_status()
            res = r.json()
            return str(res.get("id") or data.get("id"))

    def close(self):
        self.session.close()
