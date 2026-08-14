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
                  <FETCH>MASTERID, ALTERID, GUID, VOUCHERNUMBER, VOUCHERTYPENAME, DATE, PARTYNAME, PARTYLEDGERNAME, AMOUNT, REFERENCE, ALLLEDGERENTRIES.LIST, INVENTORYENTRIES.LIST</FETCH>
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

    def _parse_vouchers_xml(self, clean_xml: str, last_alter_id: int) -> List[Dict[str, Any]]:
        vouchers = []
        try:
            root = ET.fromstring(clean_xml)
            for v_node in root.findall(".//VOUCHER"):
                v_type = v_node.findtext("VOUCHERTYPENAME") or v_node.attrib.get("VCHTYPE") or ""
                alter_id_text = (v_node.findtext("ALTERID") or "0").strip()
                guid = (v_node.findtext("GUID") or v_node.attrib.get("REMOTEID") or "").strip()
                v_no = (v_node.findtext("VOUCHERNUMBER") or "").strip()
                v_date = (v_node.findtext("DATE") or "").strip()
                party_name = (v_node.findtext("PARTYLEDGERNAME") or v_node.findtext("PARTYNAME") or "").strip()
                reference = (v_node.findtext("REFERENCE") or "").strip()
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
                        "alter_id": alter_id,
                        "voucher_number": v_no,
                        "voucher_type": v_type,
                        "date": v_date,
                        "party_name": party_name,
                        "amount": amount,
                        "reference": reference,
                        "rentasst_id": rentasst_id,
                        "ledgers": ledgers,
                        "items": items,
                    })
        except Exception:
            pass

        return vouchers
