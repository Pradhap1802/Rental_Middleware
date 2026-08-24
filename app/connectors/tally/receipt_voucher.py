from typing import Dict, Any, Optional
from .xml_builder import escape_xml, format_tally_date, build_import_envelope


def build_receipt_voucher_xml(data: Dict[str, Any], action: str = "Create", company_name: Optional[str] = None) -> str:
    """Builds Tally VOUCHER XML for Receipt / Payment transactions."""
    raw_ref = str(data.get("reference_id") or data.get("number") or data.get("payment_number") or "").strip()
    ref = raw_ref if raw_ref else f"PAY-{data.get('id')}"

    cust_name = (data.get("paid_by") or (data.get("rent") or {}).get("customer_name") or data.get("customer_name") or "Cash Customer")
    amount = float(data.get("amount") or data.get("paid_amount") or 0)
    date_str = format_tally_date(data.get("payment_date") or data.get("created_at") or data.get("date"))

    pay_label = str(data.get("payment_type_label") or data.get("payment_method") or data.get("mode") or "").lower()
    cash_bank_ledger = "Bank Account" if any(w in pay_label for w in ["bank", "online", "card", "upi", "cheque", "transfer", "neft", "rtgs"]) else "Cash"
    parent_group = "Bank Accounts" if cash_bank_ledger == "Bank Account" else "Cash-in-Hand"

    msg = f"""          <LEDGER NAME="{escape_xml(cust_name)}" ACTION="Create">
            <NAME>{escape_xml(cust_name)}</NAME>
            <PARENT>Sundry Debtors</PARENT>
          </LEDGER>
          <LEDGER NAME="{escape_xml(cash_bank_ledger)}" ACTION="Create">
            <NAME>{escape_xml(cash_bank_ledger)}</NAME>
            <PARENT>{escape_xml(parent_group)}</PARENT>
          </LEDGER>
          <VOUCHER VTYPE="Receipt" ACTION="{action}" REMOTEID="RENTAL-PAY-{data.get('id')}">
            <REMOTEID>RENTAL-PAY-{data.get('id')}</REMOTEID>
            <DATE>{date_str}</DATE>
            <EFFECTIVEDATE>{date_str}</EFFECTIVEDATE>
            <VOUCHERTYPENAME>Receipt</VOUCHERTYPENAME>
            <VOUCHERNUMBER>{escape_xml(ref)}</VOUCHERNUMBER>
            <NARRATION>RENTAL-PAY-{data.get('id')}</NARRATION>
            <PARTYLEDGERNAME>{escape_xml(cust_name)}</PARTYLEDGERNAME>
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>{escape_xml(cash_bank_ledger)}</LEDGERNAME>
              <ISDEEMEDPOSITIVE>YES</ISDEEMEDPOSITIVE>
              <AMOUNT>-{amount:.2f}</AMOUNT>
            </ALLLEDGERENTRIES.LIST>
            <ALLLEDGERENTRIES.LIST>
              <LEDGERNAME>{escape_xml(cust_name)}</LEDGERNAME>
              <ISDEEMEDPOSITIVE>NO</ISDEEMEDPOSITIVE>
              <AMOUNT>{amount:.2f}</AMOUNT>
            </ALLLEDGERENTRIES.LIST>
          </VOUCHER>"""

    return build_import_envelope(msg, report_name="Vouchers", company_name=company_name)
