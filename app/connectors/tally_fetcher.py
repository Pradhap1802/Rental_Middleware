import re
import xml.etree.ElementTree as ET
import requests
from typing import Dict, Any, List, Optional
from ..models.domain import AppConfig
from .tally.client import _tally_post


def _to_tally_date(value: Optional[str]) -> Optional[str]:
    """Converts an ISO-ish date string (e.g. '2026-01-01') to Tally's YYYYMMDD format."""
    if not value:
        return None
    digits = re.sub(r"[^0-9]", "", str(value)[:10])
    return digits if len(digits) == 8 else None


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
        self.session = requests.Session()

    def _post_xml(self, xml_payload: str) -> Optional[str]:
        """
        Routes through the same _tally_post()/_TALLY_HTTP_LOCK TallyClient uses for
        every forward-sync request — this used to call bare requests.post() directly,
        completely bypassing that lock. QueueWorker runs up to 4 jobs concurrently
        (ThreadPoolExecutor), and tally_to_rentasst (which uses this fetcher) is enqueued
        every cycle alongside equipment/invoices/payments/rental_orders (which use
        TallyClient) — so this fetcher's requests could and did race against TallyClient's
        requests, unserialized, from within a single process. Confirmed live: a burst of
        "Could not set 'SVCurrentCompany'" errors on equipment sync while reverse sync
        was also active, the exact concurrency signature this codebase's own comments
        already document as capable of corrupting or crashing Tally.
        """
        try:
            r = _tally_post(self.session, self.tally_url, xml_payload.encode("utf-8"), timeout=10)
            r.raise_for_status()
            return sanitize_tally_xml(r.content)
        except Exception:
            return None

    def fetch_vouchers(
        self,
        last_alter_id: int = 0,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Build TDL Collection XML query to fetch Vouchers from Tally.
        """
        sv_from_date = _to_tally_date(from_date) or "20000101"
        sv_to_date = _to_tally_date(to_date) or "20991231"
        xml_req = f"""<ENVELOPE>
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
            <SVFROMDATE>{sv_from_date}</SVFROMDATE>
            <SVTODATE>{sv_to_date}</SVTODATE>
         </STATICVARIABLES>
         <TDL>
            <TDLMESSAGE>
               <COLLECTION NAME="VouchersCollection" ISMODIFY="No">
                  <TYPE>Voucher</TYPE>
                  <FETCH>MASTERID, ALTERID, GUID, VOUCHERNUMBER, VOUCHERTYPENAME, DATE, PARTYNAME, PARTYLEDGERNAME, AMOUNT, REFERENCE, NARRATION, ALLLEDGERENTRIES.LIST, ALLINVENTORYENTRIES.LIST, BILLALLOCATIONS.LIST</FETCH>
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
                  <FETCH>MASTERID, ALTERID, GUID, NAME, PARENT, MAILINGNAME, LEDGERPHONE, LEDGERMOBILE, EMAIL, PARTYGSTIN, LEDGERCONTACT, ADDRESS.LIST, PINCODE, COUNTRYNAME, LEDSTATENAME, LEDGSTREGDETAILS.LIST</FETCH>
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
                # LEDGERPHONE can hold multiple comma-joined numbers (the forward sync
                # writes every mobile/alternate-mobile RentAsst has into it as one
                # string, e.g. "08056997998, 08056997998") — confirmed live. Stripping
                # non-digits from that whole string concatenates every number into one
                # garbled value, so LEDGERMOBILE (Tally's own single clean primary
                # mobile field) is fetched separately and preferred by the caller;
                # "phone" is kept only as a fallback for ledgers with no LEDGERMOBILE.
                phone = (l_node.findtext("LEDGERPHONE") or "").strip()
                mobile = (l_node.findtext("LEDGERMOBILE") or "").strip()
                email = (l_node.findtext("EMAIL") or "").strip()
                # PARTYGSTIN is Tally's flat legacy GSTIN field — confirmed live it stays
                # EMPTY for a ledger whose GST was entered through Tally Prime's detailed
                # "Set/Alter GST Details" flow (multi-registration), which instead writes
                # into LEDGSTREGDETAILS.LIST/GSTIN. A ledger can carry multiple
                # LEDGSTREGDETAILS.LIST entries over time (one per APPLICABLEFROM change),
                # so the LAST one is Tally's own current/most-recent registration.
                gstin = (l_node.findtext("PARTYGSTIN") or "").strip()
                if not gstin:
                    gst_reg_entries = l_node.findall("LEDGSTREGDETAILS.LIST")
                    if gst_reg_entries:
                        gstin = (gst_reg_entries[-1].findtext("GSTIN") or "").strip()
                address_lines = [
                    (a.text or "").strip()
                    for a in l_node.findall("ADDRESS.LIST/ADDRESS")
                    if (a.text or "").strip()
                ]
                pincode = (l_node.findtext("PINCODE") or "").strip()
                country = (l_node.findtext("COUNTRYNAME") or "").strip()
                state = (l_node.findtext("LEDSTATENAME") or "").strip()

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
                    "mobile": mobile,
                    "email": email,
                    "gstin": gstin,
                    "address_lines": address_lines,
                    "pincode": pincode,
                    "country": country,
                    "state": state,
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
                  <FETCH>MASTERID, ALTERID, GUID, NAME, PARENT, BASEUNITS, OPENINGBALANCE, OPENINGRATE, OPENINGVALUE, CLOSINGBALANCE, DESCRIPTION, HSNCODE, HSNDETAILS.LIST, GSTDETAILS.LIST, RATEOFVAT, STANDARDPRICELIST.LIST</FETCH>
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

                # CLOSINGBALANCE is Tally's actual current stock-on-hand — OPENINGBALANCE
                # is fixed at item creation and never reflects later stock movement (a
                # Physical Stock voucher, a sale, a purchase). Confirmed live: a stock item
                # with OPENINGBALANCE "6 pc" and no later movement still reports
                # CLOSINGBALANCE "6 pc" (they only diverge once vouchers post against it),
                # so CLOSINGBALANCE is the only field that's ever correct for "how many are
                # there right now" — the same reasoning the forward-sync stock
                # reconciliation already uses (Physical Stock vouchers, not OPENINGBALANCE).
                closing_text = (s_node.findtext("CLOSINGBALANCE") or "").strip()
                qty_match = re.match(r"[-+]?\d+(\.\d+)?", closing_text)
                quantity = float(qty_match.group(0)) if qty_match else 0.0

                # Extract HSN Code from top-level or HSNDETAILS.LIST
                hsn = (s_node.findtext("HSNCODE") or s_node.findtext(".//HSNDETAILS.LIST/HSNCODE") or s_node.findtext(".//HSNCODE") or "").strip()

                # GST rate lives in one of two completely different places depending on
                # how it was entered in Tally — confirmed live against two real stock
                # items. A forward-synced item (created via this middleware, or entered
                # in Tally's simple GST mode) carries it as the flat top-level RATEOFVAT,
                # with GSTDETAILS.LIST/STATEWISEDETAILS.LIST present but EMPTY — Tally
                # itself normalizes detailed rate blocks down to RATEOFVAT-only on import
                # in this company's GST configuration. An item entered through Tally's
                # detailed "Set/Alter GST Details" flow instead has RATEOFVAT at 0 and the
                # real rate nested in STATEWISEDETAILS.LIST/RATEDETAILS.LIST/GSTRATE. Only
                # reading RATEDETAILS.LIST (the old behavior) silently read 0 for every
                # forward-synced item's real GST rate.
                gst_rate = 0.0
                rate_of_vat_text = (s_node.findtext("RATEOFVAT") or "").strip()
                try:
                    gst_rate = float(rate_of_vat_text) if rate_of_vat_text else 0.0
                except ValueError:
                    gst_rate = 0.0
                if not gst_rate:
                    for r_node in s_node.findall(".//RATEDETAILS.LIST"):
                        head = (r_node.findtext("GSTRATEDUTYHEAD") or "").strip().upper()
                        val = (r_node.findtext("GSTRATE") or "0").strip()
                        if head == "IGST" and val:
                            try:
                                gst_rate = float(val)
                            except ValueError:
                                pass

                # Rental price also lives in one of two different places depending on how
                # it was entered, exactly like GST above — confirmed live against a real
                # user screenshot of Tally's Stock Item Alteration screen. A stock item
                # entered through Tally's plain, everyday screen has its Rate set directly
                # in the Opening Balance row (Quantity / Rate / Value) — that Rate is
                # OPENINGRATE, and STANDARDPRICELIST.LIST ("Standard Selling Price", a
                # separate, rarely-used price-list feature) is completely empty for it. A
                # stock item this middleware's own forward sync created instead has its
                # rent_price written into STANDARDPRICELIST.LIST (see build_stock_item_xml)
                # with OPENINGRATE left at 0/unset (that field carries purchase_price for
                # forward-synced items, not rent_price). Prefer STANDARDPRICELIST.LIST
                # when it's populated; fall back to OPENINGRATE for the common case of a
                # human directly entering a rate in Tally's everyday UI.
                price_text = (s_node.findtext(".//STANDARDPRICELIST.LIST/RATE") or "").strip()
                price_match = re.match(r"[-+]?\d+(\.\d+)?", price_text)
                rent_price = float(price_match.group(0)) if price_match else 0.0
                if not rent_price:
                    opening_rate_text = (s_node.findtext("OPENINGRATE") or "").strip()
                    opening_rate_match = re.match(r"[-+]?\d+(\.\d+)?", opening_rate_text)
                    rent_price = float(opening_rate_match.group(0)) if opening_rate_match else 0.0

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
                    "quantity": quantity,
                    "rent_price": rent_price,
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

                # Parse Inventory Items if present. Confirmed live against real Tally XML
                # export (both "Sales Order" and "Sales" voucher types): the actual tag is
                # ALLINVENTORYENTRIES.LIST — there is no bare INVENTORYENTRIES.LIST tag in
                # Tally's response at all, so this always returned zero items regardless of
                # voucher type, silently defeating push_invoice_items()/push_rentout_items()
                # every single time (they always received an empty list to push).
                items = []
                for item_node in v_node.findall(".//ALLINVENTORYENTRIES.LIST"):
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
