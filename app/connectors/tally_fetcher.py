import re
import xml.etree.ElementTree as ET
import requests
from typing import Dict, Any, List, Optional
from ..models.domain import AppConfig


def sanitize_tally_xml(raw: Any) -> str:
    """Sanitizes raw Tally XML responses by stripping control chars, BOM, and numeric entities."""
    if isinstance(raw, bytes):
        txt = raw.decode("utf-8", errors="replace")
    else:
        txt = str(raw)
    txt = re.sub(r"&#\d+;", "", txt)
    txt = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", txt)
    txt = txt.lstrip("\ufeff")
    return txt.strip()


class TallyFetcher:
    """Fetcher engine to query and extract incremental entities from Tally Prime via TDL XML."""
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.tally_url = cfg.external_url.rstrip("/")

    def _post_xml(self, xml_payload: str) -> Optional[str]:
        headers = {"Content-Type": "text/xml;charset=utf-8"}
        try:
            r = requests.post(self.tally_url, data=xml_payload.encode("utf-8"), headers=headers, timeout=10)
            r.raise_for_status()
            return sanitize_tally_xml(r.content)
        except Exception:
            return None

    def fetch_vouchers(self, last_alter_id: int = 0) -> List[Dict[str, Any]]:
        """
        Build TDL Collection XML query to fetch Vouchers from Tally.
        """
        xml_req = """<ENVELOPE>
   <HEADER>
      <VERSION>1</VERSION>
      <TALLYREQUEST>EXPORT</TALLYREQUEST>
      <TYPE>COLLECTION</TYPE>
      <ID>VouchersCollection</ID>
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
               <COLLECTION NAME="VouchersCollection" ISMODIFY="No">
                  <TYPE>Voucher</TYPE>
                  <FETCH>MASTERID, ALTERID, GUID, VOUCHERNUMBER, VOUCHERTYPENAME, DATE, PARTYNAME, PARTYLEDGERNAME, AMOUNT, REFERENCE, NARRATION, ALLLEDGERENTRIES.LIST, INVENTORYENTRIES.LIST, BILLALLOCATIONS.LIST</FETCH>
               </COLLECTION>
            </TDLMESSAGE>
         </TDL>
      </DESC>
   </BODY>
</ENVELOPE>"""

        clean_xml = self._post_xml(xml_req)
        if not clean_xml:
            return []

        return self._parse_vouchers_xml(clean_xml, last_alter_id)

    def fetch_ledgers(self, last_alter_id: int = 0) -> List[Dict[str, Any]]:
        """Fetch Customer Ledgers (Sundry Debtors) from Tally Prime."""
        xml_req = """<ENVELOPE>
   <HEADER>
      <VERSION>1</VERSION>
      <TALLYREQUEST>EXPORT</TALLYREQUEST>
      <TYPE>COLLECTION</TYPE>
      <ID>LedgersCollection</ID>
   </HEADER>
   <BODY>
      <DESC>
         <STATICVARIABLES>
            <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
         </STATICVARIABLES>
         <TDL>
            <TDLMESSAGE>
               <COLLECTION NAME="LedgersCollection" ISMODIFY="No">
                  <TYPE>Ledger</TYPE>
                  <FETCH>MASTERID, ALTERID, GUID, NAME, PARENT, MAILINGNAME, LEDGERPHONE, EMAIL, PARTYGSTIN, LEDGERCONTACT</FETCH>
               </COLLECTION>
            </TDLMESSAGE>
         </TDL>
      </DESC>
   </BODY>
</ENVELOPE>"""
        clean_xml = self._post_xml(xml_req)
        if not clean_xml:
            return []

        ledgers = []
        try:
            root = ET.fromstring(clean_xml)
            for l_node in root.findall(".//LEDGER"):
                name = (l_node.findtext("NAME") or l_node.attrib.get("NAME") or "").strip()
                parent = (l_node.findtext("PARENT") or "").strip()
                guid = (l_node.findtext("GUID") or "").strip()
                alter_id_text = (l_node.findtext("ALTERID") or "0").strip()
                phone = (l_node.findtext("LEDGERPHONE") or "").strip()
                email = (l_node.findtext("EMAIL") or "").strip()
                gstin = (l_node.findtext("PARTYGSTIN") or "").strip()

                # Filter out system ledgers
                if not name or name.lower() in ("profit & loss a/c", "cash", "sales account", "rental income", "cgst", "sgst", "igst"):
                    continue

                # Only sync Sundry Debtors (Customers) or customer-type ledgers
                if parent and "debtor" not in parent.lower() and "customer" not in parent.lower():
                    continue

                try:
                    alter_id = int(alter_id_text)
                except ValueError:
                    alter_id = 0

                if last_alter_id > 0 and alter_id <= last_alter_id:
                    continue

                ledgers.append({
                    "name": name,
                    "parent": parent,
                    "tally_guid": guid,
                    "alter_id": alter_id,
                    "phone": phone,
                    "email": email,
                    "gstin": gstin,
                })
        except Exception:
            pass

        return ledgers

    def fetch_stock_items(self, last_alter_id: int = 0) -> List[Dict[str, Any]]:
        """Fetch Stock Items (Assets/Equipment) from Tally Prime."""
        xml_req = """<ENVELOPE>
   <HEADER>
      <VERSION>1</VERSION>
      <TALLYREQUEST>EXPORT</TALLYREQUEST>
      <TYPE>COLLECTION</TYPE>
      <ID>StockItemsCollection</ID>
   </HEADER>
   <BODY>
      <DESC>
         <STATICVARIABLES>
            <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
         </STATICVARIABLES>
         <TDL>
            <TDLMESSAGE>
               <COLLECTION NAME="StockItemsCollection" ISMODIFY="No">
                  <TYPE>StockItem</TYPE>
                  <FETCH>MASTERID, ALTERID, GUID, NAME, PARENT, BASEUNITS, OPENINGBALANCE, OPENINGRATE, OPENINGVALUE, DESCRIPTION, HSNCODE, HSNDETAILS.LIST, GSTDETAILS.LIST</FETCH>
               </COLLECTION>
            </TDLMESSAGE>
         </TDL>
      </DESC>
   </BODY>
</ENVELOPE>"""
        clean_xml = self._post_xml(xml_req)
        if not clean_xml:
            return []

        items = []
        try:
            root = ET.fromstring(clean_xml)
            for s_node in root.findall(".//STOCKITEM"):
                name = (s_node.findtext("NAME") or s_node.attrib.get("NAME") or "").strip()
                parent = (s_node.findtext("PARENT") or "").strip()
                guid = (s_node.findtext("GUID") or "").strip()
                alter_id_text = (s_node.findtext("ALTERID") or "0").strip()
                unit = (s_node.findtext("BASEUNITS") or "Nos").strip()
                desc = (s_node.findtext("DESCRIPTION") or "").strip()

                # Extract HSN Code from top-level or HSNDETAILS.LIST
                hsn = (s_node.findtext("HSNCODE") or s_node.findtext(".//HSNDETAILS.LIST/HSNCODE") or s_node.findtext(".//HSNCODE") or "").strip()

                # Extract GST Rate from GSTDETAILS.LIST
                gst_rate = 0.0
                for r_node in s_node.findall(".//RATEDETAILS.LIST"):
                    head = (r_node.findtext("GSTRATEDUTYHEAD") or "").strip().upper()
                    val = (r_node.findtext("GSTRATE") or "0").strip()
                    if head == "IGST" and val:
                        try:
                            gst_rate = float(val)
                        except ValueError:
                            pass

                if not name:
                    continue

                try:
                    alter_id = int(alter_id_text)
                except ValueError:
                    alter_id = 0

                if last_alter_id > 0 and alter_id <= last_alter_id:
                    continue

                items.append({
                    "name": name,
                    "parent": parent,
                    "tally_guid": guid,
                    "alter_id": alter_id,
                    "unit": unit,
                    "description": desc,
                    "hsn_code": hsn,
                    "gst_rate": gst_rate,
                })
        except Exception:
            pass

        return items

    def _parse_vouchers_xml(self, clean_xml: str, last_alter_id: int) -> List[Dict[str, Any]]:
        vouchers = []
        try:
            root = ET.fromstring(clean_xml)
            for v_node in root.findall(".//VOUCHER"):
                v_type = v_node.findtext("VOUCHERTYPENAME") or v_node.attrib.get("VCHTYPE") or ""
                alter_id_text = (v_node.findtext("ALTERID") or "0").strip()
                remote_id = (v_node.attrib.get("REMOTEID") or "").strip()
                guid = (v_node.findtext("GUID") or remote_id or "").strip()
                v_no = (v_node.findtext("VOUCHERNUMBER") or "").strip()
                v_date = (v_node.findtext("DATE") or "").strip()
                party_name = (v_node.findtext("PARTYLEDGERNAME") or v_node.findtext("PARTYNAME") or "").strip()
                reference = (v_node.findtext("REFERENCE") or "").strip()
                narration = (v_node.findtext("NARRATION") or "").strip()
                rentasst_id = (v_node.findtext("UDF_RENTASST_ID") or v_node.findtext("RENTASST_ID") or "").strip()

                try:
                    alter_id = int(alter_id_text)
                except ValueError:
                    alter_id = 0

                # Filter by ALTERID if required
                if last_alter_id > 0 and alter_id <= last_alter_id:
                    continue

                # Parse Ledgers & Amount
                amount = 0.0
                ledgers = []
                bill_ref = ""
                for l_entry in v_node.findall(".//ALLLEDGERENTRIES.LIST"):
                    lname = l_entry.findtext("LEDGERNAME")
                    amt_text = l_entry.findtext("AMOUNT")
                    if lname:
                        ledgers.append({"ledger": lname, "amount": amt_text})
                        if not party_name and not lname.startswith("Sales") and not lname.startswith("Rental") and not lname.startswith("CGST") and not lname.startswith("SGST") and not lname.startswith("IGST"):
                            party_name = lname
                        try:
                            val = abs(float(amt_text))
                            if val > amount:
                                amount = val
                        except (ValueError, TypeError):
                            pass
                    if not bill_ref:
                        for bill in l_entry.findall(".//BILLALLOCATIONS.LIST"):
                            bill_name = (bill.findtext("NAME") or "").strip()
                            bill_type = (bill.findtext("BILLTYPE") or "").strip()
                            if bill_name and bill_type.lower() in ("agst ref", "against reference"):
                                bill_ref = bill_name
                                break

                # Parse Inventory Items if present
                items = []
                for item_node in v_node.findall(".//INVENTORYENTRIES.LIST"):
                    item_name = item_node.findtext("STOCKITEMNAME")
                    qty = item_node.findtext("ACTUALQTY") or item_node.findtext("BILLEDQTY")
                    rate = item_node.findtext("RATE")
                    item_amt = item_node.findtext("AMOUNT")
                    if item_name:
                        items.append({
                            "name": item_name,
                            "quantity": qty,
                            "rate": rate,
                            "amount": item_amt
                        })

                if guid or v_no or alter_id > 0:
                    vouchers.append({
                        "tally_guid": guid,
                        "remote_id": remote_id,
                        "narration": narration,
                        "alter_id": alter_id,
                        "voucher_number": v_no,
                        "voucher_type": v_type,
                        "date": v_date,
                        "party_name": party_name,
                        "amount": amount,
                        "reference": reference,
                        "bill_ref": bill_ref,
                        "rentasst_id": rentasst_id,
                        "ledgers": ledgers,
                        "items": items,
                    })
        except Exception:
            pass

        return vouchers
