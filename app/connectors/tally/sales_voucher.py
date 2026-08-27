from typing import Dict, Any, Optional
from .xml_builder import escape_xml, format_tally_date, build_import_envelope


def build_sales_order_voucher_xml(data: Dict[str, Any], action: str = "Create", company_name: Optional[str] = None, edu_mode: bool = False) -> str:
    """
    Builds Tally VOUCHER XML for Rent Outs.

    Uses Tally's "Sales" voucher type, NOT "Sales Order" — despite the function name
    (kept for caller compatibility; callers still treat this as "the Rent Out voucher").
    Confirmed live against this company's real Tally server: "Sales Order" (and even
    "Delivery Note") rejects EVERY import attempt with EXCEPTIONS>0 regardless of XML
    shape — tried top-level ALLINVENTORYENTRIES.LIST, nested INVENTORYALLOCATIONS.LIST,
    with/without ORDERALLOCATIONS.LIST self-references, with/without BILLALLOCATIONS.LIST,
    with/without an explicit VOUCHERNUMBER, and a byte-for-byte replica of a real
    Tally-exported Sales Order (see REAL_SALES_ORDER_XML in tests/test_tally_fetcher.py) —
    every single one failed identically, most surfacing "Bad Order Number in Voucher!".
    A plain "Sales" voucher with the exact same party/items succeeded immediately
    (CREATED=1, EXCEPTIONS=0). This points to Order Processing being unavailable on this
    Tally installation (it already runs in unlicensed/Educational mode — see
    tally_edu_mode / TallyClient._send_voucher_with_edu_fallback), which the middleware
    cannot toggle. Per explicit user decision, Rent Outs are now booked as a real "Sales"
    voucher immediately at rent-out time rather than staying unsynced — note this means
    Tally's Sales register reflects revenue at order time, and if the same order is later
    also invoiced via build_sales_invoice_voucher_xml, that revenue is booked a second
    time (a real bookkeeping duplication the user accepted as the tradeoff for having
    Rent Outs show up in Tally at all).

    Items go in a nested INVENTORYALLOCATIONS.LIST inside the Sales Account ledger
    entry — the same accounting-invoice shape build_sales_invoice_voucher_xml uses,
    confirmed live to be the only shape Tally accepts for one of this company's
    Sales-type vouchers. The party ledger carries a BILLALLOCATIONS.LIST ("New Ref")
    since this company's customer ledgers are bill-wise enabled (confirmed live:
    without it, even a plain "Sales" voucher was rejected the same way orders were).

    No ORDERALLOCATIONS.LIST/ORDERNO anywhere — this is no longer an Order voucher, so
    there is no order for a later invoice to fulfill against in Tally's own order
    tracking (build_sales_invoice_voucher_xml no longer sends this either, for the same
    reason — see its docstring).

    The Sales Account ledger entry's own AMOUNT must equal the sum of its nested
    INVENTORYALLOCATIONS.LIST lines, or Tally rejects the whole voucher (confirmed
    live — this is what broke the first version of this fix: it put the GST-inclusive
    order `amount` on the ledger entry while the item lines only summed to the
    pre-tax subtotal). So the item subtotal goes on Sales Account, and GST is computed
    from RentAsst's own `gst` percentage field applied to that subtotal — NOT derived
    as (amount - item subtotal), which was wrong whenever grand_total included
    non-taxable extras (shipping/labour/deposit): that leftover silently got booked as
    GST too. Split evenly as CGST/SGST (the rental_order payload has no customer state
    to determine IGST vs CGST/SGST, same limitation build_sales_invoice_voucher_xml
    has for its own no-breakdown fallback). A single ad-hoc "GST" ledger without a
    GSTDUTYHEAD was tried first and rejected by Tally (confirmed live — CREATED
    climbed by 1 for the new ledger master but the voucher itself still landed in
    EXCEPTIONS); CGST/SGST already carry a valid GSTDUTYHEAD.

    Uses a dedicated "RentAsst Sales" voucher type (a child of the reserved "Sales"
    type, self-healing via ACTION="Create" the same way prereq ledgers are), not
    "Sales" directly. Confirmed live: the reserved "Sales" voucher type's
    NUMBERINGMETHOD is "Default" (Tally's built-in automatic numbering) — every custom
    VOUCHERNUMBER sent to it was silently discarded and replaced with Tally's own
    sequential number (e.g. we sent "R1-CUSTOM-99", Tally stored "5"). Altering the
    shared reserved "Sales" type would change numbering behavior for every voucher the
    company's own accountant enters by hand too, so instead this dedicated type is
    created once with NUMBERINGMETHOD="Manual", which Tally then honors exactly
    (confirmed live: the same "R1-CUSTOM-99" number came back unchanged).
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

    items = data.get("rent_items") or data.get("items") or data.get("assets") or data.get("details") or []
    inventory_allocations = ""
    item_subtotal = 0.0
    if isinstance(items, list) and len(items) > 0:
        for item in items:
            if isinstance(item, dict):
                raw_item_name = item.get("name") or (item.get("asset") or {}).get("name") or item.get("asset_name") or "Equipment"
                item_name = raw_item_name.split(" - ")[0].strip() if " - " in raw_item_name else raw_item_name.strip()
                qty = item.get("quantity") or item.get("qty") or 1
                price = float(item.get("price") or item.get("rent_price") or item.get("rate") or 0)
                item_total = float(item.get("total_price") or item.get("amount") or item.get("total") or (price * qty))
                unit = item.get("unit") or "Nos"
                item_subtotal += item_total

                inventory_allocations += f"""
              <INVENTORYALLOCATIONS.LIST>
                <STOCKITEMNAME>{escape_xml(item_name)}</STOCKITEMNAME>
                <ISDEEMEDPOSITIVE>NO</ISDEEMEDPOSITIVE>
                <RATE>{price:.2f}/{escape_xml(unit)}</RATE>
                <AMOUNT>{item_total:.2f}</AMOUNT>
                <ACTUALQTY>{qty} {escape_xml(unit)}</ACTUALQTY>
                <BILLEDQTY>{qty} {escape_xml(unit)}</BILLEDQTY>
              </INVENTORYALLOCATIONS.LIST>"""

    gst_percent = _resolve_order_gst_percent(data)
    if item_subtotal > 0 and gst_percent > 0:
        tax_amount = round(item_subtotal * gst_percent / 100.0, 2)
        amount = round(item_subtotal + tax_amount, 2)
    elif item_subtotal > 0:
        # RentAsst didn't report a usable GST rate, but there's still a real gap
        # between the item subtotal and the order's total amount (grand_total
        # commonly already includes tax even when the rate itself isn't broken out) —
        # keep the voucher balanced by booking that gap as tax, same as before this
        # fix, rather than silently dropping it and leaving party != Sales Account.
        tax_amount = round(amount - item_subtotal, 2)
        if tax_amount < 0:
            tax_amount = 0.0
            item_subtotal = amount
    else:
        # A header-only order with no items to apply a percentage to.
        tax_amount = 0.0
        item_subtotal = amount

    prereq_ledgers = f"""          <VOUCHERTYPE NAME="RentAsst Sales" ACTION="Create">
            <NAME>RentAsst Sales</NAME>
            <PARENT>Sales</PARENT>
            <NUMBERINGMETHOD>Manual</NUMBERINGMETHOD>
          </VOUCHERTYPE>
          <LEDGER NAME="{escape_xml(cust_name)}" ACTION="Create">
            <NAME>{escape_xml(cust_name)}</NAME>
            <PARENT>Sundry Debtors</PARENT>
          </LEDGER>
          <LEDGER NAME="Sales Account" ACTION="Create">
            <NAME>Sales Account</NAME>
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
          </LEDGER>\n"""

    bill_name = f"RENTAL-ORD-{data.get('id')}"
    party_entry = f"""            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>{escape_xml(cust_name)}</LEDGERNAME>
              <ISPARTYLEDGER>YES</ISPARTYLEDGER>
              <ISDEEMEDPOSITIVE>YES</ISDEEMEDPOSITIVE>
              <AMOUNT>-{amount:.2f}</AMOUNT>
              <BILLALLOCATIONS.LIST>
                <NAME>{escape_xml(bill_name)}</NAME>
                <BILLTYPE>New Ref</BILLTYPE>
                <AMOUNT>-{amount:.2f}</AMOUNT>
              </BILLALLOCATIONS.LIST>
            </ALLLEDGERENTRIES.LIST>"""

    sales_entry = f"""
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>Sales Account</LEDGERNAME>
              <ISDEEMEDPOSITIVE>NO</ISDEEMEDPOSITIVE>
              <AMOUNT>{item_subtotal:.2f}</AMOUNT>{inventory_allocations}
            </ALLLEDGERENTRIES.LIST>"""

    tax_entry = ""
    if tax_amount > 0:
        cgst_val = round(tax_amount / 2.0, 2)
        sgst_val = round(tax_amount - cgst_val, 2)
        tax_entry = f"""
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

    msg = f"""{prereq_ledgers}          <VOUCHER VTYPE="RentAsst Sales" ACTION="{action}" REMOTEID="RENTAL-ORD-{data.get('id')}">
            <REMOTEID>RENTAL-ORD-{data.get('id')}</REMOTEID>
            <DATE>{date_str}</DATE>
            <EFFECTIVEDATE>{date_str}</EFFECTIVEDATE>
            <VOUCHERTYPENAME>RentAsst Sales</VOUCHERTYPENAME>
            <VOUCHERNUMBER>{escape_xml(num)}</VOUCHERNUMBER>
            <NARRATION>RENTAL-ORD-{data.get('id')}</NARRATION>
            <PARTYLEDGERNAME>{escape_xml(cust_name)}</PARTYLEDGERNAME>{party_entry}{sales_entry}{tax_entry}
          </VOUCHER>"""

    return build_import_envelope(msg, report_name="Vouchers", company_name=company_name)


def build_sales_order_voucher_xml_native(data: Dict[str, Any], action: str = "Create", company_name: Optional[str] = None, edu_mode: bool = False) -> str:
    """
    Builds a REAL Tally "Sales Order" voucher for Rent Outs — the correct, non-posting
    representation on a Tally company where "Order Processing" (F11) is actually
    enabled, so Rent Outs show up in Tally's own Sales Order Book without prematurely
    booking revenue the way build_sales_order_voucher_xml's "Sales" fallback does.

    Only usable where Order Processing works — confirmed live this rejects on an
    unlicensed/Educational-mode install (see build_sales_order_voucher_xml's docstring
    for the exhaustive live verification). TallyClient.sync_rental_order tries this
    first and falls back to build_sales_order_voucher_xml on failure, remembering the
    outcome in cfg.tally_order_processing_available the same way tally_edu_mode is
    auto-detected, so a working install always gets real orders and a non-working one
    stops re-attempting this path every cycle.

    Item lines go in a top-level ALLINVENTORYENTRIES.LIST (a sibling of
    ALLLEDGERENTRIES.LIST, directly under VOUCHER) — matching a real Tally-exported
    Sales Order (REAL_SALES_ORDER_XML in tests/test_tally_fetcher.py). No GST ledger
    lines: Order vouchers are non-posting in Tally (they don't affect account
    balances), so the party/sales ledger entries just carry the order's full amount —
    GST is booked for real only later, on the actual Sales invoice.
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
    inventory_entries = ""
    if isinstance(items, list) and len(items) > 0:
        for item in items:
            if isinstance(item, dict):
                raw_item_name = item.get("name") or (item.get("asset") or {}).get("name") or item.get("asset_name") or "Equipment"
                item_name = raw_item_name.split(" - ")[0].strip() if " - " in raw_item_name else raw_item_name.strip()
                qty = item.get("quantity") or item.get("qty") or 1
                price = float(item.get("price") or item.get("rent_price") or item.get("rate") or 0)
                item_total = float(item.get("total_price") or item.get("amount") or item.get("total") or (price * qty))
                unit = item.get("unit") or "Nos"

                inventory_entries += f"""
            <ALLINVENTORYENTRIES.LIST>
              <STOCKITEMNAME>{escape_xml(item_name)}</STOCKITEMNAME>
              <ISDEEMEDPOSITIVE>NO</ISDEEMEDPOSITIVE>
              <RATE>{price:.2f}/{escape_xml(unit)}</RATE>
              <AMOUNT>{item_total:.2f}</AMOUNT>
              <ACTUALQTY>{qty} {escape_xml(unit)}</ACTUALQTY>
              <BILLEDQTY>{qty} {escape_xml(unit)}</BILLEDQTY>
            </ALLINVENTORYENTRIES.LIST>"""

    sales_entry = f"""
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>Sales Account</LEDGERNAME>
              <ISDEEMEDPOSITIVE>NO</ISDEEMEDPOSITIVE>
              <AMOUNT>{amount:.2f}</AMOUNT>
            </ALLLEDGERENTRIES.LIST>"""

    msg = f"""{prereq_ledgers}          <VOUCHER VTYPE="Sales Order" ACTION="{action}" REMOTEID="RENTAL-ORD-{data.get('id')}">
            <REMOTEID>RENTAL-ORD-{data.get('id')}</REMOTEID>
            <DATE>{date_str}</DATE>
            <EFFECTIVEDATE>{date_str}</EFFECTIVEDATE>
            <VOUCHERTYPENAME>Sales Order</VOUCHERTYPENAME>
            <VOUCHERNUMBER>{escape_xml(num)}</VOUCHERNUMBER>
            <NARRATION>RENTAL-ORD-{data.get('id')}</NARRATION>
            <PARTYLEDGERNAME>{escape_xml(cust_name)}</PARTYLEDGERNAME>{inventory_entries}{party_entry}{sales_entry}
          </VOUCHER>"""

    return build_import_envelope(msg, report_name="Vouchers", company_name=company_name)


def _resolve_order_gst_percent(data: Dict[str, Any]) -> float:
    """
    RentAsst's rental_order payload carries GST as a flat percentage, under a few
    possible field names (confirmed live: real orders use `gst`, e.g. `"gst": 18`) —
    unlike invoices, it never reports actual CGST/SGST/IGST amounts, so there is no
    amount-based path here the way build_sales_invoice_voucher_xml has. `cgst`/`sgst`
    are deliberately NOT combined into a sum: confirmed live, a real order reported
    `"gst": 18, "cgst": 18, "sgst": 18` all as the SAME 18% figure (not 9+9), so
    treating them as independent additive components would double-count the rate.
    """
    for key in ("gst", "gst_percent", "gst_percentage", "gst_rate", "tax_percent"):
        raw = data.get(key)
        if raw in (None, ""):
            continue
        try:
            pct = float(raw)
        except (TypeError, ValueError):
            continue
        if pct > 0:
            return pct
    return 0.0


def build_sales_invoice_voucher_xml(data: Dict[str, Any], action: str = "Create", company_state: str = "", company_name: Optional[str] = None, edu_mode: bool = False) -> str:
    """
    Builds Tally VOUCHER XML for Sales Invoices and Credit Notes.

    When the invoice originated from a Rent Out, RentAsst's API includes a nested
    `rent: {id, number, status}` object (no date field at list-endpoint granularity).
    We reference that rent's `number` via a voucher-level REFERENCE tag (plain free
    text) so a human can trace the invoice back to its order in Tally.

    Deliberately does NOT use ORDERALLOCATIONS.LIST/ORDERNO — that only makes sense
    against a real Tally Sales Order voucher for Tally to track fulfillment against, and
    this company's Tally installation rejects Sales Order voucher creation outright (see
    build_sales_order_voucher_xml's docstring), so no such order ever exists in Tally's
    own Order Book to reference. Confirmed live: including it here reproduces the exact
    same "Bad Order Number in Voucher!" rejection this invoice path would otherwise not
    have, since build_sales_order_voucher_xml now books Rent Outs as plain "Sales"
    vouchers instead.
    """
    raw_num = str(data.get("number") or data.get("invoice_number") or "").strip()
    num = raw_num if raw_num and raw_num != "0" else f"INV-{data.get('id')}"
    cust_name = (data.get("customer") or {}).get("name") or data.get("customer_name") or f"Customer-{data.get('customer_id')}"
    order_number = str((data.get("rent") or {}).get("number") or data.get("rent_number") or "").strip()

    grand_total = float(data.get("grand_total") or data.get("total_amount") or data.get("amount") or 0)
    subtotal = float(data.get("subtotal") or 0)
    if not subtotal:
        subtotal = grand_total

    # Prefer RentAsst's own CGST/SGST/IGST breakdown — top-level, or summed across line
    # items — over reverse-deriving a single number from grand_total - subtotal. The
    # subtraction can't tell CGST/SGST/IGST apart (it only ever produces an even 50/50
    # split or all-IGST guess), so it's used only when RentAsst genuinely provides no tax
    # breakdown at all. Any residual mismatch between the real tax figures and
    # grand_total/subtotal still balances out via the Round Off entry below, same as today.
    explicit_cgst = float(data.get("cgst_amount") or data.get("cgst") or 0)
    explicit_sgst = float(data.get("sgst_amount") or data.get("sgst") or 0)
    explicit_igst = float(data.get("igst_amount") or data.get("igst") or 0)
    explicit_tax = float(data.get("tax_amount") or data.get("tax") or data.get("gst_amount") or 0)

    if not (explicit_cgst or explicit_sgst or explicit_igst or explicit_tax):
        items_for_tax = data.get("items") or []
        if isinstance(items_for_tax, list):
            for it in items_for_tax:
                if isinstance(it, dict):
                    explicit_cgst += float(it.get("cgst_amount") or it.get("cgst") or 0)
                    explicit_sgst += float(it.get("sgst_amount") or it.get("sgst") or 0)
                    explicit_igst += float(it.get("igst_amount") or it.get("igst") or 0)
                    explicit_tax += float(it.get("tax_amount") or it.get("tax") or it.get("gst_amount") or 0)
            explicit_cgst, explicit_sgst, explicit_igst, explicit_tax = (
                round(explicit_cgst, 2), round(explicit_sgst, 2), round(explicit_igst, 2), round(explicit_tax, 2)
            )

    has_explicit_split = bool(explicit_cgst or explicit_sgst or explicit_igst)
    if has_explicit_split:
        tax_amount = round(explicit_cgst + explicit_sgst + explicit_igst, 2)
    elif explicit_tax > 0:
        tax_amount = explicit_tax
    else:
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

    # A "New Ref" bill allocation is what makes this invoice a distinct, trackable bill
    # in Tally's own bill-wise outstanding/payment-summary reports — without it, Tally
    # silently books the entry "On Account" (confirmed live: every invoice this
    # middleware pushed showed BILLTYPE=On Account with no bill name at all). REMOTEID's
    # RENTAL-INV-{id} marker doubles as the bill name since it's already stable and
    # unique; build_receipt_voucher_xml references this exact same string as its "Agst
    # Ref" bill name so a Receipt can settle against this specific invoice instead of
    # just reducing the party's flat running balance.
    bill_name = f"RENTAL-INV-{data.get('id')}"
    party_entry = f"""            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>{escape_xml(cust_name)}</LEDGERNAME>
              <ISPARTYLEDGER>YES</ISPARTYLEDGER>
              <ISDEEMEDPOSITIVE>YES</ISDEEMEDPOSITIVE>
              <AMOUNT>-{grand_total:.2f}</AMOUNT>
              <BILLALLOCATIONS.LIST>
                <NAME>{escape_xml(bill_name)}</NAME>
                <BILLTYPE>New Ref</BILLTYPE>
                <AMOUNT>-{grand_total:.2f}</AMOUNT>
              </BILLALLOCATIONS.LIST>
            </ALLLEDGERENTRIES.LIST>"""

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
    if has_explicit_split:
        # RentAsst told us the real per-head amounts directly — use them as-is rather
        # than re-deriving a CGST/SGST/IGST split ourselves.
        if explicit_cgst > 0:
            tax_entries += f"""
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>CGST</LEDGERNAME>
              <ISDEEMEDPOSITIVE>NO</ISDEEMEDPOSITIVE>
              <AMOUNT>{explicit_cgst:.2f}</AMOUNT>
            </ALLLEDGERENTRIES.LIST>"""
        if explicit_sgst > 0:
            tax_entries += f"""
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>SGST</LEDGERNAME>
              <ISDEEMEDPOSITIVE>NO</ISDEEMEDPOSITIVE>
              <AMOUNT>{explicit_sgst:.2f}</AMOUNT>
            </ALLLEDGERENTRIES.LIST>"""
        if explicit_igst > 0:
            tax_entries += f"""
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>IGST</LEDGERNAME>
              <ISDEEMEDPOSITIVE>NO</ISDEEMEDPOSITIVE>
              <AMOUNT>{explicit_igst:.2f}</AMOUNT>
            </ALLLEDGERENTRIES.LIST>"""
    elif tax_amount > 0:
        # No real breakdown available from RentAsst — fall back to India's standard GST
        # rule (an even CGST/SGST split for intra-state, all-IGST for inter-state) applied
        # to the one tax total we do have.
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
