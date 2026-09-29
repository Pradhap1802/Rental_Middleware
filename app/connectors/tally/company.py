from typing import List, Dict
from .xml_builder import build_export_collection_envelope
from .parser import parse_tally_xml


def build_fetch_companies_xml() -> str:
    """Builds XML request envelope to fetch all loaded companies from Tally."""
    return build_export_collection_envelope(
        collection_id="ListofCompanies",
        tally_type="Company",
        fetch_fields="NAME",
    )


def parse_fetch_companies_response(xml_content: str) -> List[Dict[str, str]]:
    """Parses ListofCompanies XML response from Tally."""
    root = parse_tally_xml(xml_content)
    if root is None:
        return []

    companies = []
    for comp in root.findall(".//COMPANY"):
        name = comp.findtext("NAME") or comp.attrib.get("NAME")
        if name:
            companies.append({"name": name.strip()})
    return companies
