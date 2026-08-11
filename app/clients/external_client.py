import html
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


def escape_xml(text: Any) -> str:
    """Safely escapes text for inclusion in Tally XML templates."""
    if text is None:
        return ""
    return html.escape(str(text), quote=True)


def normalize_state_name(state_raw: str) -> str:
    """Extracts state/province name directly from RentAsst database record without predefined hardcoded dictionary."""
    if not state_raw:
        return ""
    clean = str(state_raw).strip()
    return clean.title() if clean.islower() else clean


def format_tally_date(raw_date: Optional[str], edu_mode: bool = True) -> str:
    """Converts dates to Tally YYYYMMDD string format.
    Handles formats: YYYY-MM-DD, YYYY-MM-DDTHH:MM:SS, DD.MM.YYYY, DD.MM.YYYY HH:MM
    In Tally Educational Edition (EDU mode), dates other than 1st, 2nd, 31st are rejected by Tally.
    """
    if not raw_date:
        return datetime.now().strftime("%Y%m01") if edu_mode else datetime.now().strftime("%Y%m%d")
    try:
        raw_str = str(raw_date).strip()
        date_only = raw_str.split(" ")[0].split("T")[0]
        
        parsed_yyyy, parsed_mm, parsed_dd = "", "", ""
        clean = date_only.replace("-", "")
        if len(clean) == 8 and clean.isdigit():
            parsed_yyyy, parsed_mm, parsed_dd = clean[:4], clean[4:6], clean[6:8]
        else:
            parts = re.split(r"[./-]", date_only)
            if len(parts) == 3:
                p0, p1, p2 = parts[0].strip(), parts[1].strip(), parts[2].strip()
                if len(p0) == 4:  # YYYY-MM-DD
                    parsed_yyyy, parsed_mm, parsed_dd = p0, p1.zfill(2), p2.zfill(2)
                elif len(p2) == 4:  # DD.MM.YYYY or DD/MM/YYYY
                    parsed_yyyy, parsed_mm, parsed_dd = p2, p1.zfill(2), p0.zfill(2)

        if parsed_yyyy and parsed_mm and parsed_dd:
            if edu_mode and parsed_dd not in ("01", "02", "31"):
                parsed_dd = "01"
            return f"{parsed_yyyy}{parsed_mm}{parsed_dd}"
    except Exception:
        pass
    return datetime.now().strftime("%Y%m01") if edu_mode else datetime.now().strftime("%Y%m%d")


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

    def check_exists_in_tally(self, entity_type: str, identifier: str) -> bool:
        if self.cfg.external_system_type != "tally":
            return True

        if not identifier:
            return True

        ent = (entity_type or "").lower().strip()
        tally_type = "LEDGER"
        if ent in ("equipment", "product", "products"):
            tally_type = "STOCKITEM"
        elif ent == "unit":
            tally_type = "UNIT"
        elif ent == "stockgroup":
            tally_type = "STOCKGROUP"
        elif ent == "stockcategory":
            tally_type = "STOCKCATEGORY"
        elif ent in ("rental_orders", "rental_order", "invoices", "invoice", "payments", "payment", "voucher"):
            tally_type = "VOUCHER"

        xml = f"""<ENVELOPE>
   <HEADER>
      <VERSION>1</VERSION>
      <TALLYREQUEST>EXPORT</TALLYREQUEST>
      <TYPE>COLLECTION</TYPE>
      <ID>CheckExistence</ID>
   </HEADER>
   <BODY>
      <DESC>
         <STATICVARIABLES>
            <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            <SVFROMDATE>20000101</SVFROMDATE>
            <SVTODATE>20991231</SVTODATE>
         </STATICVARIABLES>
         <TDL>
            <TDLMESSAGE>
               <COLLECTION NAME="CheckExistence" ISMODIFY="No">
                  <TYPE>{tally_type}</TYPE>
                  <FETCH>NAME, VOUCHERNUMBER, REMOTEID</FETCH>
               </COLLECTION>
            </TDLMESSAGE>
         </TDL>
      </DESC>
   </BODY>
</ENVELOPE>"""
        try:
            r = self.session.post(self.base_url, data=xml.encode("utf-8"), headers={"Content-Type": "text/xml"}, timeout=10)
            if r.status_code == 200:
                clean = sanitize_tally_xml(r.content)
                return (identifier.lower() in clean.lower())
        except Exception:
            pass
        return False

    def fetch_tally_companies(self) -> List[Dict[str, str]]:
        """Queries Tally Prime XML server to get all currently loaded/open companies."""
        if self.cfg.external_system_type != "tally":
            return []
        xml = """<ENVELOPE>
   <HEADER>
      <VERSION>1</VERSION>
      <TALLYREQUEST>EXPORT</TALLYREQUEST>
      <TYPE>COLLECTION</TYPE>
      <ID>ListofCompanies</ID>
   </HEADER>
   <BODY>
      <DESC>
         <STATICVARIABLES>
            <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
         </STATICVARIABLES>
         <TDL>
            <TDLMESSAGE>
               <COLLECTION NAME="ListofCompanies" ISMODIFY="No">
                  <TYPE>Company</TYPE>
                  <NATIVEMETHOD>NAME</NATIVEMETHOD>
               </COLLECTION>
            </TDLMESSAGE>
         </TDL>
      </DESC>
   </BODY>
</ENVELOPE>"""
        try:
            r = self.session.post(self.base_url, data=xml.encode("utf-8"), headers={"Content-Type": "text/xml"}, timeout=10)
            if r.status_code == 200:
                clean = sanitize_tally_xml(r.content)
                root = ET.fromstring(clean)
                companies = []
                for comp in root.findall(".//COMPANY"):
                    name = comp.findtext("NAME") or comp.attrib.get("NAME")
                    if name:
                        companies.append({"name": name.strip()})
                return companies
        except Exception:
            pass
        return []

    def _post_tally_xml(self, xml_string: str) -> str:
        # Dynamically inject target Tally Company into STATICVARIABLES if set in config
        if getattr(self.cfg, "tally_company_name", None) and "<STATICVARIABLES>" not in xml_string:
            company_var = f"<STATICVARIABLES><SVCURRENTCOMPANY>{self.cfg.tally_company_name}</SVCURRENTCOMPANY></STATICVARIABLES>"
            xml_string = xml_string.replace("<REQUESTDESC>", f"<REQUESTDESC>{company_var}")

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
            name = (data.get("name") or data.get("business_name") or f"Customer-{data.get('id')}").strip()
            
            # Determine Parent Group (Sundry Creditors vs Sundry Debtors)
            is_supplier = data.get("is_supplier")
            is_customer = data.get("is_customer")
            if is_supplier and not is_customer:
                parent_group = "Sundry Creditors"
            else:
                parent_group = "Sundry Debtors"

            # Mailing Name (uses business_name if present, otherwise name)
            business_name = (data.get("business_name") or "").strip()
            mailing_name = business_name if business_name else name

            # Contact Details (Mobile, Alternate Mobiles, Email)
            mobile = (data.get("mobile") or data.get("phone") or "").strip()
            alt_mobile1 = (data.get("alternate_mobile") or "").strip()
            alt_mobile2 = (data.get("alternate_mobile_2") or "").strip()

            all_mobiles = [m for m in [mobile, alt_mobile1, alt_mobile2] if m]
            phone_str = ", ".join(all_mobiles) if all_mobiles else ""
            primary_mobile = mobile or (all_mobiles[0] if all_mobiles else "")

            email = (data.get("email") or "").strip()

            # Addresses (Address 1, Address 2, Landmark, City, State, Country, Zip Code) purely from RentAsst DB
            addr1, addr2, landmark, city, state, country, pincode = "", "", "", "", "", "", ""
            addresses = data.get("address") or data.get("addresses")
            default_addr = None
            if isinstance(addresses, list) and len(addresses) > 0:
                default_addr = next((a for a in addresses if a.get("is_default") or a.get("is_billing")), addresses[0])
            elif isinstance(addresses, dict):
                default_addr = addresses

            if default_addr and isinstance(default_addr, dict):
                addr1 = (default_addr.get("address1") or "").strip()
                addr2 = (default_addr.get("address2") or "").strip()
                landmark = (default_addr.get("landmark") or "").strip()
                city = (default_addr.get("city") or "").strip()
                state = normalize_state_name(default_addr.get("state") or data.get("state") or "")
                country = (default_addr.get("country") or data.get("country") or "").strip()
                pincode = (default_addr.get("zipcode") or default_addr.get("pincode") or data.get("pincode") or "").strip()
            else:
                state = normalize_state_name(data.get("state") or "")
                country = (data.get("country") or "").strip()
                pincode = (data.get("zipcode") or data.get("pincode") or "").strip()

            addr_lines = [line for line in [addr1, addr2, landmark, city] if line]
            if not addr_lines and default_addr and isinstance(default_addr, dict) and default_addr.get("full_address"):
                addr_lines = [default_addr.get("full_address").strip()]

            addr_nodes = "\n".join([f"              <ADDRESS>{escape_xml(line)}</ADDRESS>" for line in addr_lines]) if addr_lines else ""

            # GST Details
            gst = (data.get("customer_gst_number") or data.get("gst_number") or "").strip().upper()
            gst_type = "Regular" if gst else "Unregistered"

            # PAN Number (extract 10-char PAN from 15-char GSTIN or pan_number)
            pan = ""
            if gst and len(gst) == 15:
                pan = gst[2:12]
            else:
                raw_pan = (data.get("pan_number") or data.get("pan") or data.get("aadhaar_number") or "").strip().upper()
                if len(raw_pan) == 10 and raw_pan.isalnum():
                    pan = raw_pan

            # Bank Account Details (Account Holder, Account No, IFSC, Bank Name, Branch)
            bank_acc = None
            bank_accounts = data.get("bank_accounts") or data.get("bankAccounts") or data.get("bank_account")
            if isinstance(bank_accounts, list) and len(bank_accounts) > 0:
                bank_acc = next((b for b in bank_accounts if b.get("is_default")), bank_accounts[0])
            elif isinstance(bank_accounts, dict):
                bank_acc = bank_accounts

            bank_xml = ""
            if bank_acc and isinstance(bank_acc, dict):
                bank_name = (bank_acc.get("bank_name") or "").strip()
                branch_name = (bank_acc.get("branch_name") or "").strip()
                account_num = (bank_acc.get("account_number") or "").strip()
                ifsc_code = (bank_acc.get("ifsc_code") or bank_acc.get("ifsc") or "").strip()
                account_holder = (bank_acc.get("account_holder_name") or name).strip()

                if account_num or ifsc_code or bank_name:
                    bank_xml = f"""
            <BANKDETAILS.LIST>
              <PAYMENTFAVOURING>{escape_xml(account_holder)}</PAYMENTFAVOURING>
              <ACCOUNTNUMBER>{escape_xml(account_num)}</ACCOUNTNUMBER>
              <IFSCODE>{escape_xml(ifsc_code)}</IFSCODE>
              <BANKNAME>{escape_xml(bank_name)}</BANKNAME>
              <BRANCHNAME>{escape_xml(branch_name)}</BRANCHNAME>
              <TRANSACTIONTYPE>e-Fund Transfer</TRANSACTIONTYPE>
            </BANKDETAILS.LIST>
            <PAYMENTDETAILS.LIST>
              <PAYMENTFAVOURING>{escape_xml(account_holder)}</PAYMENTFAVOURING>
              <ACCOUNTNUMBER>{escape_xml(account_num)}</ACCOUNTNUMBER>
              <IFSCODE>{escape_xml(ifsc_code)}</IFSCODE>
              <BANKNAME>{escape_xml(bank_name)}</BANKNAME>
              <BRANCHNAME>{escape_xml(branch_name)}</BRANCHNAME>
              <TRANSACTIONTYPE>e-Fund Transfer</TRANSACTIONTYPE>
            </PAYMENTDETAILS.LIST>"""

            # GST XML Block
            gst_block = ""
            if gst:
                gst_block = f"""
            <LEDGSTREGDETAILS.LIST>
              <APPLICABLEFROM>20240401</APPLICABLEFROM>
              <GSTREGISTRATIONTYPE>{gst_type}</GSTREGISTRATIONTYPE>
              <GSTIN>{escape_xml(gst)}</GSTIN>
              <STATE>{escape_xml(state)}</STATE>
            </LEDGSTREGDETAILS.LIST>"""

            # Mailing Details XML Block
            mailing_block = f"""
            <LEDMAILINGDETAILS.LIST>
              <APPLICABLEFROM>20240401</APPLICABLEFROM>
              <MAILINGNAME>{escape_xml(mailing_name)}</MAILINGNAME>
              <ADDRESS.LIST TYPE="String">
{addr_nodes}
              </ADDRESS.LIST>
              <STATE>{escape_xml(state)}</STATE>
              <COUNTRY>{escape_xml(country)}</COUNTRY>
              <PINCODE>{escape_xml(pincode)}</PINCODE>
            </LEDMAILINGDETAILS.LIST>"""

            # PAN XML Block
            pan_block = ""
            if pan:
                pan_block = f"""
            <PANNUMBER>{escape_xml(pan)}</PANNUMBER>
            <INCOMETAXPAN>{escape_xml(pan)}</INCOMETAXPAN>"""

            # Action selection for duplicate prevention
            already_in_tally = self.check_exists_in_tally("customer", name)
            action = "Alter" if already_in_tally else "Create"

            xml = f"""<ENVELOPE>
  <HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC><REPORTNAME>All Masters</REPORTNAME></REQUESTDESC>
      <REQUESTDATA>
        <TALLYMESSAGE xmlns:UDF="TallyUDF">
          <LEDGER NAME="{escape_xml(name)}" ACTION="{action}">
            <NAME>{escape_xml(name)}</NAME>
            <PARENT>{escape_xml(parent_group)}</PARENT>
            <MAILINGNAME>{escape_xml(mailing_name)}</MAILINGNAME>
            <LEDGERPHONE>{escape_xml(phone_str or primary_mobile)}</LEDGERPHONE>
            <LEDGERMOBILE>{escape_xml(primary_mobile)}</LEDGERMOBILE>
            <PHONE>{escape_xml(primary_mobile)}</PHONE>
            <MOBILE>{escape_xml(primary_mobile)}</MOBILE>
            <EMAIL>{escape_xml(email)}</EMAIL>
            <ADDRESS.LIST TYPE="String">
{addr_nodes}
            </ADDRESS.LIST>
            <STATENAME>{escape_xml(state)}</STATENAME>
            <LEDSTATENAME>{escape_xml(state)}</LEDSTATENAME>
            <COUNTRYNAME>{escape_xml(country)}</COUNTRYNAME>
            <PINCODE>{escape_xml(pincode)}</PINCODE>
            <GSTREGISTRATIONTYPE>{gst_type}</GSTREGISTRATIONTYPE>
            <PARTYGSTIN>{escape_xml(gst)}</PARTYGSTIN>{gst_block}{mailing_block}{pan_block}{bank_xml}
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
            name = (data.get("name") or f"Item-{data.get('id')}").strip()

            # Unit of Measure & Symbol (Meter / m, Piece / pc, Nos / nos)
            unit_name = "Nos"
            unit_symbol = ""
            if isinstance(data.get("asset_unit"), dict):
                unit_name = (data["asset_unit"].get("name") or "Nos").strip()
                unit_symbol = (data["asset_unit"].get("symbol") or "").strip()
            elif data.get("asset_unit_name"):
                raw_u = data.get("asset_unit_name").strip()
                unit_name = raw_u.split("(")[0].strip()
                if "(" in raw_u and ")" in raw_u:
                    unit_symbol = raw_u.split("(")[1].split(")")[0].strip()

            unit = unit_name if unit_name else (unit_symbol or "Nos")
            symbol_tag = f"\n            <SYMBOL>{escape_xml(unit_symbol)}</SYMBOL>" if unit_symbol else ""

            # Stock Group (Category e.g. Mobile, Laptop)
            group = "Primary"
            if isinstance(data.get("asset_category"), dict) and data.get("asset_category", {}).get("name"):
                group = data["asset_category"]["name"].strip()
            elif data.get("asset_categories_names"):
                group = str(data.get("asset_categories_names")).split(",")[0].strip()

            # Stock Category (Brand e.g. Moto, Dell)
            category = ""
            if isinstance(data.get("asset_brand"), dict) and data.get("asset_brand", {}).get("name"):
                category = data["asset_brand"]["name"].strip()
            elif data.get("asset_brand_name"):
                category = str(data.get("asset_brand_name")).strip()

            # Asset Code / SKU / Barcode / Model / Description
            asset_code = (data.get("asset_code") or data.get("sku") or "").strip()
            barcode = (data.get("bar_code") or data.get("barcode") or "").strip()
            model_name = (data.get("asset_model_name") or "").strip()
            raw_desc = (data.get("description") or "").strip()

            # Store Asset Code and Barcode in Description so Tally doesn't create duplicate alias list entries
            desc_parts = []
            if asset_code:
                desc_parts.append(f"Asset Code: {asset_code}")
            if barcode:
                desc_parts.append(f"Barcode: {barcode}")
            if raw_desc:
                desc_parts.append(raw_desc)
            full_description = " | ".join(desc_parts)

            # Prices & Quantities
            purchase_price = float(data.get("purchase_price") or 0)
            rent_price = float(data.get("rent_price") or data.get("day_based_rent_price") or 0)
            available_qty = data.get("available_quantity") or data.get("original_quantity") or 0

            # GST Rate & HSN Code
            gst_rate = float(data.get("gst_rate") or data.get("gst_percentage") or 0)
            cgst_rate = round(gst_rate / 2.0, 2) if gst_rate else 0
            sgst_rate = cgst_rate
            hsn_code = (data.get("hsn_code") or data.get("hsn_sac_code") or data.get("hsn") or "").strip()

            desc_tag = f"\n            <DESCRIPTION>{escape_xml(full_description)}</DESCRIPTION>" if full_description else ""

            opening_balance_tag = f"\n            <OPENINGBALANCE>{available_qty} {escape_xml(unit)}</OPENINGBALANCE>" if available_qty else ""
            opening_rate_tag = f"\n            <OPENINGRATE>{purchase_price:.2f}/{escape_xml(unit)}</OPENINGRATE>" if purchase_price else ""
            opening_val_tag = f"\n            <OPENINGVALUE>-{purchase_price:.2f}</OPENINGVALUE>" if purchase_price else ""

            gst_block = ""
            if gst_rate or hsn_code:
                hsn_tag = f"\n              <HSNCODE>{escape_xml(hsn_code)}</HSNCODE>\n              <HSN>{escape_xml(hsn_code)}</HSN>" if hsn_code else ""
                rate_of_vat_tag = f"\n            <RATEOFVAT>{gst_rate}</RATEOFVAT>" if gst_rate else ""
                gst_block = f"""{rate_of_vat_tag}
            <GSTAPPLICABLE>Applicable</GSTAPPLICABLE>
            <GSTTYPEOFSUPPLY>Goods</GSTTYPEOFSUPPLY>
            <GSTDETAILS.LIST>
              <APPLICABLEFROM>20240401</APPLICABLEFROM>
              <SRCOFGSTDETAILS>Specify Details Here</SRCOFGSTDETAILS>
              <CALCULATIONTYPE>On Value</CALCULATIONTYPE>
              <TAXABILITY>Taxable</TAXABILITY>{hsn_tag}
              <STATEWISEDETAILS.LIST>
                <STATENAME>Any</STATENAME>
                <RATEDETAILS.LIST>
                  <GSTRATEDUTYHEAD>IGST</GSTRATEDUTYHEAD>
                  <GSTRATE>{gst_rate}</GSTRATE>
                </RATEDETAILS.LIST>
                <RATEDETAILS.LIST>
                  <GSTRATEDUTYHEAD>CGST</GSTRATEDUTYHEAD>
                  <GSTRATE>{cgst_rate}</GSTRATE>
                </RATEDETAILS.LIST>
                <RATEDETAILS.LIST>
                  <GSTRATEDUTYHEAD>SGST/UTGST</GSTRATEDUTYHEAD>
                  <GSTRATE>{sgst_rate}</GSTRATE>
                </RATEDETAILS.LIST>
              </STATEWISEDETAILS.LIST>
            </GSTDETAILS.LIST>"""
                if hsn_code:
                    gst_block += f"""
            <HSNDETAILS.LIST>
              <APPLICABLEFROM>20240401</APPLICABLEFROM>
              <SRCOFHSNDETAILS>Specify Details Here</SRCOFHSNDETAILS>
              <HSNCODE>{escape_xml(hsn_code)}</HSNCODE>
              <HSN>{escape_xml(hsn_code)}</HSN>
              <HSNDESCRIPTION>{escape_xml(hsn_code)}</HSNDESCRIPTION>
            </HSNDETAILS.LIST>"""

            # Price List blocks (using exact Tally tags STANDARDPRICELIST.LIST & STANDARDCOSTLIST.LIST)
            price_xml = ""
            if rent_price:
                price_xml = f"""
            <STANDARDPRICELIST.LIST>
              <DATE>20240401</DATE>
              <RATE>{rent_price:.2f}/{escape_xml(unit)}</RATE>
            </STANDARDPRICELIST.LIST>"""

            cost_xml = ""
            if purchase_price:
                cost_xml = f"""
            <STANDARDCOSTLIST.LIST>
              <DATE>20240401</DATE>
              <RATE>{purchase_price:.2f}/{escape_xml(unit)}</RATE>
            </STANDARDCOSTLIST.LIST>"""

            # Only send master creation XML for Unit, Stock Group, and Stock Category if they don't already exist in Tally
            unit_xml = ""
            if not self.check_exists_in_tally("unit", unit):
                unit_xml = f"""
          <UNIT NAME="{escape_xml(unit)}" ACTION="Create">
            <NAME>{escape_xml(unit)}</NAME>{symbol_tag}
            <ISSIMPLEUNIT>YES</ISSIMPLEUNIT>
          </UNIT>"""

            group_xml = ""
            if group and group != "Primary" and not self.check_exists_in_tally("stockgroup", group):
                group_xml = f"""
          <STOCKGROUP NAME="{escape_xml(group)}" ACTION="Create">
            <NAME>{escape_xml(group)}</NAME>
          </STOCKGROUP>"""

            category_master_xml = ""
            category_item_tag = ""
            if category:
                if not self.check_exists_in_tally("stockcategory", category):
                    category_master_xml = f"""
          <STOCKCATEGORY NAME="{escape_xml(category)}" ACTION="Create">
            <NAME>{escape_xml(category)}</NAME>
          </STOCKCATEGORY>"""
                category_item_tag = f"\n            <CATEGORY>{escape_xml(category)}</CATEGORY>"

            item_action = "Alter" if self.check_exists_in_tally("equipment", name) else "Create"

            xml = f"""<ENVELOPE>
  <HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC><REPORTNAME>All Masters</REPORTNAME></REQUESTDESC>
      <REQUESTDATA>
        <TALLYMESSAGE xmlns:UDF="TallyUDF">{unit_xml}{group_xml}{category_master_xml}
          <STOCKITEM NAME="{escape_xml(name)}" ACTION="{item_action}">
            <NAME>{escape_xml(name)}</NAME>
            <MAILINGNAME.LIST ISMODIFY="Yes" ACTION="Delete"/>
            <PARENT>{escape_xml(group)}</PARENT>{category_item_tag}
            <BASEUNITS>{escape_xml(unit)}</BASEUNITS>{desc_tag}{opening_balance_tag}{opening_rate_tag}{opening_val_tag}{gst_block}{price_xml}{cost_xml}
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
          <VOUCHER VTYPE="Sales Order" ACTION="Create" REMOTEID="RENTAL-ORD-{data.get('id')}">
            <REMOTEID>RENTAL-ORD-{data.get('id')}</REMOTEID>
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
            raw_num = str(data.get("number") or data.get("invoice_number") or "").strip()
            num = raw_num if raw_num and raw_num != "0" else f"INV-{data.get('id')}"
            cust_name = (data.get("customer") or {}).get("name") or data.get("customer_name") or f"Customer-{data.get('customer_id')}"
            
            grand_total = float(data.get("grand_total") or data.get("total_amount") or data.get("amount") or 0)
            subtotal = float(data.get("subtotal") or 0)
            if not subtotal:
                subtotal = grand_total

            tax_amount = round(grand_total - subtotal, 2)
            if tax_amount < 0:
                tax_amount = 0

            # Determine intra-state vs inter-state for CGST/SGST vs IGST
            cust_state = ((data.get("customer_address") or {}).get("state") or "").strip().lower()
            comp_state = (getattr(self.cfg, "company_state", "") or "").strip().lower()
            is_igst = bool(cust_state and comp_state and cust_state != comp_state)

            vtype = "Credit Note" if data.get("document_type") == "credit_note" else "Sales"
            date_str = format_tally_date(data.get("invoice_date") or data.get("date") or data.get("created_at"))

            # Build Duty ledgers creation XML
            prereq_ledgers = f"""
          <LEDGER NAME="{escape_xml(cust_name)}" ACTION="Create">
            <NAME>{escape_xml(cust_name)}</NAME>
            <PARENT>Sundry Debtors</PARENT>
          </LEDGER>
          <LEDGER NAME="Rental Income" ACTION="Create">
            <NAME>Rental Income</NAME>
            <PARENT>Sales Accounts</PARENT>
          </LEDGER>
          <LEDGER NAME="CGST" ACTION="Create">
            <NAME>CGST</NAME>
            <PARENT>Duties &amp; Taxes</PARENT>
            <TAXTYPE>GST</TAXTYPE>
            <GSTDUTYHEAD>Central Tax</GSTDUTYHEAD>
          </LEDGER>
          <LEDGER NAME="SGST" ACTION="Create">
            <NAME>SGST</NAME>
            <PARENT>Duties &amp; Taxes</PARENT>
            <TAXTYPE>GST</TAXTYPE>
            <GSTDUTYHEAD>State Tax</GSTDUTYHEAD>
          </LEDGER>
          <LEDGER NAME="IGST" ACTION="Create">
            <NAME>IGST</NAME>
            <PARENT>Duties &amp; Taxes</PARENT>
            <TAXTYPE>GST</TAXTYPE>
            <GSTDUTYHEAD>Integrated Tax</GSTDUTYHEAD>
          </LEDGER>"""

            # Build Party Ledger Entry (Debit)
            party_entry = f"""
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>{escape_xml(cust_name)}</LEDGERNAME>
              <ISPARTYLEDGER>YES</ISPARTYLEDGER>
              <ISDEEMEDPOSITIVE>YES</ISDEEMEDPOSITIVE>
              <AMOUNT>-{grand_total:.2f}</AMOUNT>
            </ALLLEDGERENTRIES.LIST>"""

            # Build Inventory Allocations under Income Ledger
            items = data.get("items") or []
            inventory_allocations = ""
            if isinstance(items, list) and len(items) > 0:
                for item in items:
                    raw_item_name = item.get("name") or "Equipment"
                    item_name = raw_item_name.split(" - ")[0].strip() if " - " in raw_item_name else raw_item_name.strip()
                    qty = item.get("quantity") or 1
                    price = float(item.get("price") or 0)
                    total = float(item.get("total_price") or item.get("grand_total") or (price * qty))
                    unit = item.get("unit") or "Piece"

                    inventory_allocations += f"""
              <INVENTORYALLOCATIONS.LIST>
                <STOCKITEMNAME>{escape_xml(item_name)}</STOCKITEMNAME>
                <ISDEEMEDPOSITIVE>NO</ISDEEMEDPOSITIVE>
                <RATE>{price:.2f}/{escape_xml(unit)}</RATE>
                <AMOUNT>{total:.2f}</AMOUNT>
                <ACTUALQTY>{qty} {escape_xml(unit)}</ACTUALQTY>
                <BILLEDQTY>{qty} {escape_xml(unit)}</BILLEDQTY>
                <BATCHALLOCATIONS.LIST>
                  <GODOWNNAME>Main Location</GODOWNNAME>
                  <BATCHNAME>Primary Batch</BATCHNAME>
                  <AMOUNT>{total:.2f}</AMOUNT>
                  <ACTUALQTY>{qty} {escape_xml(unit)}</ACTUALQTY>
                  <BILLEDQTY>{qty} {escape_xml(unit)}</BILLEDQTY>
                </BATCHALLOCATIONS.LIST>
              </INVENTORYALLOCATIONS.LIST>"""

            income_entry = f"""
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>Rental Income</LEDGERNAME>
              <ISDEEMEDPOSITIVE>NO</ISDEEMEDPOSITIVE>
              <AMOUNT>{subtotal:.2f}</AMOUNT>{inventory_allocations}
            </ALLLEDGERENTRIES.LIST>"""

            tax_entries = ""
            if tax_amount > 0:
                if is_igst:
                    tax_entries += f"""
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>IGST</LEDGERNAME>
              <ISDEEMEDPOSITIVE>NO</ISDEEMEDPOSITIVE>
              <AMOUNT>{tax_amount:.2f}</AMOUNT>
            </ALLLEDGERENTRIES.LIST>"""
                else:
                    cgst_val = round(tax_amount / 2.0, 2)
                    sgst_val = round(tax_amount - cgst_val, 2)
                    tax_entries += f"""
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>CGST</LEDGERNAME>
              <ISDEEMEDPOSITIVE>NO</ISDEEMEDPOSITIVE>
              <AMOUNT>{cgst_val:.2f}</AMOUNT>
            </ALLLEDGERENTRIES.LIST>
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>SGST</LEDGERNAME>
              <ISDEEMEDPOSITIVE>NO</ISDEEMEDPOSITIVE>
              <AMOUNT>{sgst_val:.2f}</AMOUNT>
            </ALLLEDGERENTRIES.LIST>"""

            xml = f"""<ENVELOPE>
  <HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC><REPORTNAME>Vouchers</REPORTNAME></REQUESTDESC>
      <REQUESTDATA>
        <TALLYMESSAGE xmlns:UDF="TallyUDF">{prereq_ledgers}
          <VOUCHER VTYPE="{vtype}" ACTION="Create" REMOTEID="RENTAL-INV-{data.get('id')}">
            <REMOTEID>RENTAL-INV-{data.get('id')}</REMOTEID>
            <DATE>{date_str}</DATE>
            <EFFECTIVEDATE>{date_str}</EFFECTIVEDATE>
            <VOUCHERTYPENAME>{vtype}</VOUCHERTYPENAME>
            <VOUCHERNUMBER>{num}</VOUCHERNUMBER>
            <PARTYLEDGERNAME>{escape_xml(cust_name)}</PARTYLEDGERNAME>{party_entry}{income_entry}{tax_entries}
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
            # Use number or id as fallback for reference
            raw_ref = str(data.get("reference_id") or data.get("number") or data.get("payment_number") or "").strip()
            ref = raw_ref if raw_ref else f"PAY-{data.get('id')}"
            # Get customer name from paid_by or nested rent.customer
            cust_name = (data.get("paid_by") or (data.get("rent") or {}).get("customer_name") or data.get("customer_name") or "Cash Customer")
            amount = data.get("amount") or data.get("paid_amount") or 0
            date_str = format_tally_date(data.get("payment_date") or data.get("created_at") or data.get("date"))

            # Use payment_type_label if available (e.g. "Cash", "Bank Transfer", "UPI")
            pay_label = str(data.get("payment_type_label") or data.get("payment_method") or data.get("mode") or "").lower()
            cash_bank_ledger = "Bank Account" if any(w in pay_label for w in ["bank", "online", "card", "upi", "cheque", "transfer", "neft", "rtgs"]) else "Cash"
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
          <VOUCHER VTYPE="Receipt" ACTION="Create" REMOTEID="RENTAL-PAY-{data.get('id')}">
            <REMOTEID>RENTAL-PAY-{data.get('id')}</REMOTEID>
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
