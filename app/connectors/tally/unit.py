from typing import Dict, Any, Optional
from .xml_builder import escape_xml, build_import_envelope

# Standard GST Unique Quantity Codes (UQC) mapping for Tally
UQC_MAPPING = {
    "NOS": "NOS-NUMBERS",
    "NUMBERS": "NOS-NUMBERS",
    "PCS": "PCS-PIECES",
    "PIECE": "PCS-PIECES",
    "PIECES": "PCS-PIECES",

    "BOX": "BOX-BOXES",
    "BOXES": "BOX-BOXES",
    "SET": "SET-SETS",
    "SETS": "SET-SETS",
    "MTR": "MTR-METRES",
    "METRE": "MTR-METRES",
    "METRES": "MTR-METRES",
    "KG": "KGS-KILOGRAMS",
    "KGS": "KGS-KILOGRAMS",
    "KILOGRAMS": "KGS-KILOGRAMS",
    "LTR": "LTR-LITRES",
    "LITRE": "LTR-LITRES",
    "LITRES": "LTR-LITRES",
    "HRS": "HRS-HOURS",
    "HOURS": "HRS-HOURS",
    "DAY": "OTH-OTHERS",
    "DAYS": "OTH-OTHERS",
    "MONTH": "OTH-OTHERS",
    "MONTHS": "OTH-OTHERS",
    "UNIT": "UNT-UNITS",
    "UNITS": "UNT-UNITS",
    "PAC": "PAC-PACKS",
    "PACK": "PAC-PACKS",
    "PACKS": "PAC-PACKS",
    "ROLL": "ROL-ROLLS",
    "ROLLS": "ROL-ROLLS",
}


def resolve_gstrepuom(unit_name: str, symbol: Optional[str] = None) -> str:
    """Resolves standard GST Reporting UOM / UQC for Tally Prime."""
    key = (symbol or unit_name or "").upper().strip()
    if key in UQC_MAPPING:
        return UQC_MAPPING[key]
    key_name = (unit_name or "").upper().strip()
    if key_name in UQC_MAPPING:
        return UQC_MAPPING[key_name]
    return "OTH-OTHERS"


def build_unit_xml(
    data: Dict[str, Any],
    action: str = "Create",
    company_name: Optional[str] = None,
) -> str:
    """
    Builds Tally Prime <UNIT> master XML envelope.
    Supports unit name, symbol, decimal places, and GST UQC code.
    """
    # 1. Extract Unit Name and Symbol
    raw_name = (data.get("name") or data.get("unit_name") or data.get("symbol") or "Nos").strip()
    raw_symbol = (data.get("symbol") or data.get("code") or "").strip()
    
    if "(" in raw_name and ")" in raw_name and not raw_symbol:
        parts = raw_name.split("(")
        unit_name = parts[0].strip()
        raw_symbol = parts[1].split(")")[0].strip()
    else:
        unit_name = raw_name

    if not unit_name:
        unit_name = raw_symbol or "Nos"

    symbol = raw_symbol or unit_name
    decimal_places = int(data.get("decimal_places") or data.get("decimalPlaces") or 0)
    formal_name = (data.get("formal_name") or data.get("original_name") or unit_name).strip()
    gstrepuom = data.get("uqc_code") or resolve_gstrepuom(unit_name, symbol)

    symbol_tag = f"\n            <SYMBOL>{escape_xml(symbol)}</SYMBOL>" if symbol else ""
    orig_name_tag = f"\n            <ORIGINALNAME>{escape_xml(formal_name)}</ORIGINALNAME>" if formal_name else ""
    gstrepuom_tag = f"\n            <GSTREPUOM>{escape_xml(gstrepuom)}</GSTREPUOM>" if gstrepuom else ""

    unit_xml = f"""          <UNIT NAME="{escape_xml(unit_name)}" ACTION="{action}">
            <NAME>{escape_xml(unit_name)}</NAME>{orig_name_tag}{symbol_tag}
            <DECIMALPLACES>{decimal_places}</DECIMALPLACES>
            <ISSIMPLEUNIT>YES</ISSIMPLEUNIT>{gstrepuom_tag}
          </UNIT>"""

    return build_import_envelope(unit_xml, report_name="All Masters", company_name=company_name)
