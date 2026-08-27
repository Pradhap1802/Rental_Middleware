import xml.etree.ElementTree as ET
from typing import Tuple, Optional, List, Union
from .xml_builder import sanitize_tally_xml


def parse_tally_xml(xml_content: Union[str, bytes]) -> Optional[ET.Element]:
    """Sanitizes raw Tally response and parses into an ElementTree Element."""
    try:
        clean = sanitize_tally_xml(xml_content)
        if not clean:
            return None
        return ET.fromstring(clean)
    except Exception:
        return None


def extract_tally_errors(xml_or_root: Union[str, bytes, ET.Element]) -> List[str]:
    """
    Inspects Tally XML response for business errors, schema failures, and line errors.
    Detects <LINEERROR>, <ERROR>, <EXCEPTION>, and <STATUS>0</STATUS>.
    """
    if isinstance(xml_or_root, (str, bytes)):
        root = parse_tally_xml(xml_or_root)
    else:
        root = xml_or_root

    if root is None:
        return ["Malformed or empty Tally XML response"]

    errors = []

    # 1. Check <LINEERROR> tags (e.g. Ledger missing, duplicate voucher, invalid date)
    for line_err in root.findall(".//LINEERROR"):
        if line_err.text and line_err.text.strip():
            errors.append(line_err.text.strip())

    # 2. Check <ERROR> or <EXCEPTION> tags
    for err in root.findall(".//ERROR"):
        if err.text and err.text.strip():
            errors.append(err.text.strip())
    for exc in root.findall(".//EXCEPTION"):
        if exc.text and exc.text.strip():
            errors.append(exc.text.strip())

    # 3. Check <STATUS>0</STATUS> in response envelope
    status_elem = root.find(".//STATUS")
    if status_elem is not None and status_elem.text and status_elem.text.strip() == "0":
        msg = root.findtext(".//RESPONSE") or root.findtext(".//ERRORMESSAGE") or "Tally import returned status 0 (Failure)"
        if msg not in errors:
            errors.append(msg.strip())

    return errors


def validate_tally_accounting_success(
    xml_content: Union[str, bytes], require_voucher: bool = False
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Strictly validates whether Tally Prime successfully processed and committed the accounting transaction.

    IMPORTANT: Tally Prime returns HTTP 200 even when transaction import fails!
    This function parses the XML body to confirm that:
    1. No <LINEERROR> or business exception tags exist, and <EXCEPTIONS> count is 0 —
       confirmed live that a failed voucher import can still report ALTERED>0 (from
       prerequisite ledgers that already existed) while EXCEPTIONS>0 signals the
       voucher itself was rejected.
    2. At least 1 record was CREATED (>0) or ALTERED (>0), or a valid LASTVCHID was assigned.
       (Note: Tally's real field is LASTVCHID, not LASTVOUCHERID.)

    When require_voucher=True (voucher syncs — Rent Out/Invoice/Payment), success requires
    LASTVCHID > 0 specifically — CREATED/ALTERED alone is not enough, since those counts
    include prerequisite ledger/stock-item master upserts that can succeed independently
    of whether the voucher itself was actually created or altered.

    Returns:
        (is_success: bool, error_message: Optional[str], tally_id: Optional[str])
    """
    root = parse_tally_xml(xml_content)
    if root is None:
        return False, "Failed to parse Tally XML response structure", None

    # Check for business errors in XML
    errors = extract_tally_errors(root)
    if errors:
        err_msg = "Tally Business Error: " + "; ".join(errors)
        return False, err_msg, None

    # Parse creation, alteration, and exception counts
    created_text = root.findtext(".//CREATED")
    altered_text = root.findtext(".//ALTERED")
    exceptions_text = root.findtext(".//EXCEPTIONS")
    last_vch_text = root.findtext(".//LASTVCHID") or root.findtext(".//LASTVOUCHERID")

    created = int(created_text) if created_text and created_text.isdigit() else 0
    altered = int(altered_text) if altered_text and altered_text.isdigit() else 0
    exceptions = int(exceptions_text) if exceptions_text and exceptions_text.isdigit() else 0
    last_vch_id = int(last_vch_text) if last_vch_text and last_vch_text.isdigit() else 0

    if exceptions > 0:
        return (
            False,
            f"Tally import failed: {exceptions} exception(s) reported by Tally "
            f"(created={created}, altered={altered}, voucher touched={last_vch_id > 0}). "
            f"This usually means the voucher type is not usable in the current Tally "
            f"company configuration (e.g. a required feature like Order Processing is disabled).",
            None,
        )

    if require_voucher:
        if last_vch_id > 0:
            return True, None, f"TALLY-ID-{last_vch_id}"
        return (
            False,
            "Tally import failed: no voucher was created or altered — only prerequisite "
            "ledgers/stock items (if any) were touched.",
            None,
        )

    if created > 0 or altered > 0 or last_vch_id > 0:
        tally_id = f"TALLY-ID-{last_vch_id or created or altered}"
        return True, None, tally_id

    # Check for generic success <RESPONSE> or <IMPORTRESULT>
    import_result = root.find(".//IMPORTRESULT")
    if import_result is not None:
        created_sub = import_result.findtext("CREATED")
        if created_sub and created_sub.isdigit() and int(created_sub) > 0:
            return True, None, f"TALLY-ID-{created_sub}"

    # If HTTP 200 returned but CREATED=0 and ALTERED=0, treatment as accounting failure!
    return False, "Tally import failed: 0 records created or altered in Tally database", None
