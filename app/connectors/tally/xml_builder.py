import html
import re
from datetime import datetime
from typing import Any, Optional


def sanitize_tally_xml(raw: Any) -> str:
    """Sanitizes raw Tally XML responses by stripping control chars, BOM, and namespaces."""
    if isinstance(raw, bytes):
        txt = raw.decode("utf-8", errors="replace")
    else:
        txt = str(raw)
    txt = re.sub(r"&#\d+;", "", txt)
    txt = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", txt)
    txt = txt.lstrip("\ufeff")
    txt = re.sub(r'\s+xmlns(?::[A-Za-z_][A-Za-z0-9_.-]*)?\s*=\s*"[^"]*"', "", txt)
    txt = re.sub(r"\s+xmlns(?::[A-Za-z_][A-Za-z0-9_.-]*)?\s*=\s*'[^']*'", "", txt)
    txt = re.sub(r"<(/?)([A-Za-z_][A-Za-z0-9_.-]*):([A-Za-z_][A-Za-z0-9_.-]*)", r"<\1\3", txt)
    return txt.strip()


def escape_xml(text: Any) -> str:
    """Safely escapes text for inclusion in Tally XML templates."""
    if text is None:
        return ""
    return html.escape(str(text), quote=True)


def normalize_state_name(state_raw: str) -> str:
    """Extracts state/province name directly from RentAsst database record."""
    if not state_raw:
        return ""
    clean = str(state_raw).strip()
    return clean.title() if clean.islower() else clean


def format_tally_date(raw_date: Optional[str], edu_mode: bool = True) -> str:
    """Converts dates to Tally YYYYMMDD string format."""
    if not raw_date:
        return datetime.now().strftime("%Y%m01") if edu_mode else datetime.now().strftime("%Y%m%d")
    try:
        raw_str = str(raw_date).strip()
        date_only = raw_str.split(" ")[0].split("T")[0]
        
        parsed_yyyy, parsed_mm, parsed_dd = "", "", ""
        clean = date_only.replace("-", "")
        if len(clean) == 8 and clean.isdigit():
            parsed_yyyy, parsed_mm, parsed_dd = clean[:4], clean[4:6], clean[6:8]
        else:
            parts = re.split(r"[./-]", date_only)
            if len(parts) == 3:
                p0, p1, p2 = parts[0].strip(), parts[1].strip(), parts[2].strip()
                if len(p0) == 4:
                    parsed_yyyy, parsed_mm, parsed_dd = p0, p1.zfill(2), p2.zfill(2)
                elif len(p2) == 4:
                    parsed_yyyy, parsed_mm, parsed_dd = p2, p1.zfill(2), p0.zfill(2)

        if parsed_yyyy and parsed_mm and parsed_dd:
            if edu_mode and parsed_dd not in ("01", "02", "31"):
                parsed_dd = "01"
            return f"{parsed_yyyy}{parsed_mm}{parsed_dd}"
    except Exception:
        pass
    return datetime.now().strftime("%Y%m01") if edu_mode else datetime.now().strftime("%Y%m%d")


def build_import_envelope(tally_messages_xml: str, report_name: str = "All Masters", company_name: Optional[str] = None) -> str:
    """Builds standard Tally Import Data XML Envelope."""
    company_var = ""
    if company_name:
        company_var = f"<STATICVARIABLES><SVCURRENTCOMPANY>{escape_xml(company_name)}</SVCURRENTCOMPANY></STATICVARIABLES>"

    return f"""<ENVELOPE>
  <HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC><REPORTNAME>{report_name}</REPORTNAME>{company_var}</REQUESTDESC>
      <REQUESTDATA>
        <TALLYMESSAGE xmlns:UDF="TallyUDF">
{tally_messages_xml}
        </TALLYMESSAGE>
      </REQUESTDATA>
    </IMPORTDATA>
  </BODY>
</ENVELOPE>"""


def build_export_collection_envelope(collection_id: str, tally_type: str, fetch_fields: str = "NAME", company_name: Optional[str] = None) -> str:
    """Builds standard Tally Export Collection XML Envelope."""
    company_var = ""
    if company_name:
        company_var = f"<SVCURRENTCOMPANY>{escape_xml(company_name)}</SVCURRENTCOMPANY>\n            "

    return f"""<ENVELOPE>
   <HEADER>
      <VERSION>1</VERSION>
      <TALLYREQUEST>EXPORT</TALLYREQUEST>
      <TYPE>COLLECTION</TYPE>
      <ID>{collection_id}</ID>
   </HEADER>
   <BODY>
      <DESC>
         <STATICVARIABLES>
            {company_var}<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
            <SVFROMDATE>20000101</SVFROMDATE>
            <SVTODATE>20991231</SVTODATE>
         </STATICVARIABLES>
         <TDL>
            <TDLMESSAGE>
               <COLLECTION NAME="{collection_id}" ISMODIFY="No">
                  <TYPE>{tally_type}</TYPE>
                  <FETCH>{fetch_fields}</FETCH>
               </COLLECTION>
            </TDLMESSAGE>
         </TDL>
      </DESC>
   </BODY>
</ENVELOPE>"""
