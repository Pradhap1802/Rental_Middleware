from typing import Dict, Any, Optional
from .xml_builder import escape_xml, format_tally_date, build_import_envelope


def build_sales_order_voucher_xml(data: Dict[str, Any], action: str = "Create", company_name: Optional[str] = None, edu_mode: bool = False) -> str:
    """
    Builds Tally VOUCHER XML for Rent Outs, using Tally's "Sales Order" voucher type so
    the order shows up in Tally's own Sales Order Book/Order Outstanding reports as soon
    as it's created in RentAsst.

    Requires "Order Processing" to be enabled under the Tally company's F11 features —
    without it, Tally rejects this voucher type with EXCEPTIONS>0 (confirmed live). This
    is a Tally application setting the client must enable; the middleware cannot toggle it.

    Invoice sync (build_sales_invoice_voucher_xml) references this order's VOUCHERNUMBER
    via ORDERALLOCATIONS.LIST so Tally can track fulfillment from order to invoice.
    """
    num = (
        data.get("number") or data.get("rent_code") or data.get("rent_number")
        or data.get("code") or data.get("quotation_number") or f"ORD-{data.get('id')}"
    )
    cust_name = (
        data.get("customer_name") or (data.get("customer") or {}).get("name")
        or data.get("client_name") or f"Customer-{data.get('customer_id')}"
    )
    amount = float(
        data.get("amount") or data.get("total_amount") or data.get("grand_total")
        or data.get("rent_amount") or data.get("subtotal") or 0
    )
    date_str = format_tally_date(data.get("rent_date") or data.get("order_date") or data.get("date") or data.get("created_at"), edu_mode=edu_mode)

    prereq_ledgers = f"""          <LEDGER NAME="{escape_xml(cust_name)}" ACTION="Create">
            <NAME>{escape_xml(cust_name)}</NAME>
            <PARENT>Sundry Debtors</PARENT>
          </LEDGER>
          <LEDGER NAME="Sales Account" ACTION="Create">
            <NAME>Sales Account</NAME>
            <PARENT>Sales Accounts</PARENT>
          </LEDGER>\n"""

    party_entry = f"""            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>{escape_xml(cust_name)}</LEDGERNAME>
              <ISPARTYLEDGER>YES</ISPARTYLEDGER>
              <ISDEEMEDPOSITIVE>YES</ISDEEMEDPOSITIVE>
              <AMOUNT>-{amount:.2f}</AMOUNT>
            </ALLLEDGERENTRIES.LIST>"""

    items = data.get("rent_items") or data.get("items") or data.get("assets") or data.get("details") or []
    inventory_allocations = ""
    if isinstance(items, list) and len(items) > 0:
        for item in items:
            if isinstance(item, dict):
                raw_item_name = item.get("name") or (item.get("asset") or {}).get("name") or item.get("asset_name") or "Equipment"
                item_name = raw_item_name.split(" - ")[0].strip() if " - " in raw_item_name else raw_item_name.strip()
                qty = item.get("quantity") or item.get("qty") or 1
                price = float(item.get("price") or item.get("rent_price") or item.get("rate") or 0)
                item_total = float(item.get("total_price") or item.get("amount") or item.get("total") or (price * qty))
                unit = item.get("unit") or "Nos"

                inventory_allocations += f"""
              <INVENTORYALLOCATIONS.LIST>
                <STOCKITEMNAME>{escape_xml(item_name)}</STOCKITEMNAME>
                <ISDEEMEDPOSITIVE>NO</ISDEEMEDPOSITIVE>
                <RATE>{price:.2f}/{escape_xml(unit)}</RATE>
                <AMOUNT>{item_total:.2f}</AMOUNT>
                <ACTUALQTY>{qty} {escape_xml(unit)}</ACTUALQTY>
                <BILLEDQTY>{qty} {escape_xml(unit)}</BILLEDQTY>
                <BATCHALLOCATIONS.LIST>
                  <GODOWNNAME>Main Location</GODOWNNAME>
                  <BATCHNAME>Primary Batch</BATCHNAME>
                  <AMOUNT>{item_total:.2f}</AMOUNT>
                  <ACTUALQTY>{qty} {escape_xml(unit)}</ACTUALQTY>
                  <BILLEDQTY>{qty} {escape_xml(unit)}</BILLEDQTY>
                </BATCHALLOCATIONS.LIST>
              </INVENTORYALLOCATIONS.LIST>"""

    sales_entry = f"""
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>Sales Account</LEDGERNAME>
              <ISDEEMEDPOSITIVE>NO</ISDEEMEDPOSITIVE>
              <AMOUNT>{amount:.2f}</AMOUNT>{inventory_allocations}
            </ALLLEDGERENTRIES.LIST>"""

    msg = f"""{prereq_ledgers}          <VOUCHER VTYPE="Sales Order" ACTION="{action}" REMOTEID="RENTAL-ORD-{data.get('id')}">
            <REMOTEID>RENTAL-ORD-{data.get('id')}</REMOTEID>
            <DATE>{date_str}</DATE>
            <EFFECTIVEDATE>{date_str}</EFFECTIVEDATE>
            <VOUCHERTYPENAME>Sales Order</VOUCHERTYPENAME>
            <VOUCHERNUMBER>{escape_xml(num)}</VOUCHERNUMBER>
            <NARRATION>RENTAL-ORD-{data.get('id')}</NARRATION>
            <PARTYLEDGERNAME>{escape_xml(cust_name)}</PARTYLEDGERNAME>{party_entry}{sales_entry}
          </VOUCHER>"""

    return build_import_envelope(msg, report_name="Vouchers", company_name=company_name)


def build_sales_invoice_voucher_xml(data: Dict[str, Any], action: str = "Create", company_state: str = "", company_name: Optional[str] = None, edu_mode: bool = False) -> str:
    """
    Builds Tally VOUCHER XML for Sales Invoices and Credit Notes.

    When the invoice originated from a Rent Out, RentAsst's API includes a nested
    `rent: {id, number, status}` object (no date field at list-endpoint granularity).
    We reference that rent's `number` — the same VOUCHERNUMBER used by the Sales Order
    voucher built in build_sales_order_voucher_xml — via ORDERALLOCATIONS.LIST on each
    inventory line, plus a voucher-level REFERENCE, so Tally can track order fulfillment.
    This linkage needs live verification against a Tally company with Order Processing
    enabled (not available during this change) before relying on it in production.
    """
    raw_num = str(data.get("number") or data.get("invoice_number") or "").strip()
    num = raw_num if raw_num and raw_num != "0" else f"INV-{data.get('id')}"
    cust_name = (data.get("customer") or {}).get("name") or data.get("customer_name") or f"Customer-{data.get('customer_id')}"
    order_number = str((data.get("rent") or {}).get("number") or data.get("rent_number") or "").strip()

    grand_total = float(data.get("grand_total") or data.get("total_amount") or data.get("amount") or 0)
    subtotal = float(data.get("subtotal") or 0)
    if not subtotal:
        subtotal = grand_total

    tax_amount = round(grand_total - subtotal, 2)
    if tax_amount < 0:
        tax_amount = 0.0

    cust_state = ((data.get("customer_address") or {}).get("state") or "").strip().lower()
    comp_state = (company_state or "").strip().lower()
    is_igst = bool(cust_state and comp_state and cust_state != comp_state)

    vtype = "Credit Note" if data.get("document_type") == "credit_note" else "Sales"
    date_str = format_tally_date(data.get("invoice_date") or data.get("date") or data.get("created_at"), edu_mode=edu_mode)

    prereq_ledgers = f"""          <LEDGER NAME="{escape_xml(cust_name)}" ACTION="Create">
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
          </LEDGER>
          <LEDGER NAME="Round Off" ACTION="Create">
            <NAME>Round Off</NAME>
            <PARENT>Indirect Expenses</PARENT>
          </LEDGER>\n"""

    party_entry = f"""            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>{escape_xml(cust_name)}</LEDGERNAME>
              <ISPARTYLEDGER>YES</ISPARTYLEDGER>
              <ISDEEMEDPOSITIVE>YES</ISDEEMEDPOSITIVE>
              <AMOUNT>-{grand_total:.2f}</AMOUNT>
            </ALLLEDGERENTRIES.LIST>"""

    order_allocation = f"""
                <ORDERALLOCATIONS.LIST>
                  <ORDERNO>{escape_xml(order_number)}</ORDERNO>
                </ORDERALLOCATIONS.LIST>""" if order_number else ""

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
                <BILLEDQTY>{qty} {escape_xml(unit)}</BILLEDQTY>{order_allocation}
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

    # RentAsst commonly rounds grand_total to the nearest whole rupee while subtotal/tax
    # keep paise precision (e.g. grand_total=409 for a 409.46 subtotal) — a standard "round
    # off" convention, not a data error. Without accounting for it, party (debit, grand_total)
    # wouldn't balance against income+tax (credit, subtotal+tax_amount), and Tally requires
    # every voucher's ledger entries to sum to zero.
    round_off_amount = round(grand_total - subtotal - tax_amount, 2)
    round_off_entry = ""
    if abs(round_off_amount) >= 0.01:
        sign = "YES" if round_off_amount < 0 else "NO"
        round_off_entry = f"""
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>Round Off</LEDGERNAME>
              <ISDEEMEDPOSITIVE>{sign}</ISDEEMEDPOSITIVE>
              <AMOUNT>{round_off_amount:.2f}</AMOUNT>
            </ALLLEDGERENTRIES.LIST>"""

    reference_tag = f"<REFERENCE>{escape_xml(order_number)}</REFERENCE>\n            " if order_number else ""
    msg = f"""{prereq_ledgers}          <VOUCHER VTYPE="{vtype}" ACTION="{action}" REMOTEID="RENTAL-INV-{data.get('id')}">
            <REMOTEID>RENTAL-INV-{data.get('id')}</REMOTEID>
            <DATE>{date_str}</DATE>
            <EFFECTIVEDATE>{date_str}</EFFECTIVEDATE>
            <VOUCHERTYPENAME>{vtype}</VOUCHERTYPENAME>
            <VOUCHERNUMBER>{escape_xml(num)}</VOUCHERNUMBER>
            {reference_tag}<NARRATION>RENTAL-INV-{data.get('id')}</NARRATION>
            <PARTYLEDGERNAME>{escape_xml(cust_name)}</PARTYLEDGERNAME>{party_entry}{income_entry}{tax_entries}{round_off_entry}
          </VOUCHER>"""

    return build_import_envelope(msg, report_name="Vouchers", company_name=company_name)
