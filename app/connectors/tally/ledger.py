from typing import Dict, Any, Optional
from .xml_builder import escape_xml, normalize_state_name, build_import_envelope, build_export_collection_envelope


def build_check_ledger_exists_xml(ledger_name: str) -> str:
    """Builds XML collection request to check if a ledger exists in Tally."""
    return build_export_collection_envelope(
        collection_id="CheckExistence",
        tally_type="LEDGER",
        fetch_fields="NAME, REMOTEID",
    )


def build_customer_ledger_xml(data: Dict[str, Any], action: str = "Create", company_name: Optional[str] = None) -> str:
    """Builds full Tally LEDGER XML for Customer / Supplier masters."""
    name = (data.get("name") or data.get("business_name") or f"Customer-{data.get('id')}").strip()
    
    # Determine Parent Group (Sundry Creditors vs Sundry Debtors)
    is_supplier = data.get("is_supplier")
    is_customer = data.get("is_customer")
    if is_supplier and not is_customer:
        parent_group = "Sundry Creditors"
    else:
        parent_group = "Sundry Debtors"

    business_name = (data.get("business_name") or "").strip()
    mailing_name = business_name if business_name else name

    mobile = (data.get("mobile") or data.get("phone") or "").strip()
    alt_mobile1 = (data.get("alternate_mobile") or "").strip()
    alt_mobile2 = (data.get("alternate_mobile_2") or "").strip()

    all_mobiles = [m for m in [mobile, alt_mobile1, alt_mobile2] if m]
    phone_str = ", ".join(all_mobiles) if all_mobiles else ""
    primary_mobile = mobile or (all_mobiles[0] if all_mobiles else "")

    email = (data.get("email") or "").strip()

    # Address resolution
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

    pan = ""
    if gst and len(gst) == 15:
        pan = gst[2:12]
    else:
        raw_pan = (data.get("pan_number") or data.get("pan") or data.get("aadhaar_number") or "").strip().upper()
        if len(raw_pan) == 10 and raw_pan.isalnum():
            pan = raw_pan

    # Bank Account Details
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
            </BANKDETAILS.LIST>"""

    gst_block = ""
    if gst:
        gst_block = f"""
            <LEDGSTREGDETAILS.LIST>
              <APPLICABLEFROM>20240401</APPLICABLEFROM>
              <GSTREGISTRATIONTYPE>{gst_type}</GSTREGISTRATIONTYPE>
              <GSTIN>{escape_xml(gst)}</GSTIN>
              <STATE>{escape_xml(state)}</STATE>
            </LEDGSTREGDETAILS.LIST>"""

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

    pan_block = f"\n            <PANNUMBER>{escape_xml(pan)}</PANNUMBER>\n            <INCOMETAXPAN>{escape_xml(pan)}</INCOMETAXPAN>" if pan else ""

    message_xml = f"""          <LEDGER NAME="{escape_xml(name)}" ACTION="{action}">
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
          </LEDGER>"""

    return build_import_envelope(message_xml, report_name="All Masters", company_name=company_name)
