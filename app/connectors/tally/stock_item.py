from typing import Dict, Any, Optional
from .xml_builder import escape_xml, build_import_envelope, format_tally_date


def build_stock_item_xml(
    data: Dict[str, Any],
    action: str = "Create",
    unit_exists: bool = True,
    group_exists: bool = True,
    category_exists: bool = True,
    company_name: Optional[str] = None,
) -> str:
    """Builds full Tally STOCKITEM XML including prerequisites (Unit, StockGroup, StockCategory)."""
    name = (data.get("name") or f"Item-{data.get('id')}").strip()

    # Unit of Measure & Symbol
    unit_name = "Nos"
    unit_symbol = ""
    if isinstance(data.get("asset_unit"), dict):
        unit_name = (data["asset_unit"].get("name") or "Nos").strip()
        unit_symbol = (data["asset_unit"].get("symbol") or "").strip()
    elif data.get("asset_unit_name"):
        raw_u = data.get("asset_unit_name").strip()
        unit_name = raw_u.split("(")[0].strip()
        if "(" in raw_u and ")" in raw_u:
            unit_symbol = raw_u.split("(")[1].split(")")[0].strip()

    unit = unit_name if unit_name else (unit_symbol or "Nos")
    symbol_tag = f"\n            <SYMBOL>{escape_xml(unit_symbol)}</SYMBOL>" if unit_symbol else ""

    # Stock Group
    group = "Primary"
    if isinstance(data.get("asset_category"), dict) and data.get("asset_category", {}).get("name"):
        group = data["asset_category"]["name"].strip()
    elif data.get("asset_categories_names"):
        group = str(data.get("asset_categories_names")).split(",")[0].strip()

    # Stock Category
    category = ""
    if isinstance(data.get("asset_brand"), dict) and data.get("asset_brand", {}).get("name"):
        category = data["asset_brand"]["name"].strip()
    elif data.get("asset_brand_name"):
        category = str(data.get("asset_brand_name")).strip()

    asset_code = (data.get("asset_code") or data.get("sku") or "").strip()
    barcode = (data.get("bar_code") or data.get("barcode") or "").strip()
    raw_desc = (data.get("description") or "").strip()

    desc_parts = []
    if asset_code:
        desc_parts.append(f"Asset Code: {asset_code}")
    if barcode:
        desc_parts.append(f"Barcode: {barcode}")
    if raw_desc:
        desc_parts.append(raw_desc)
    full_description = " | ".join(desc_parts)

    purchase_price = float(data.get("purchase_price") or 0)
    rent_price = float(data.get("rent_price") or data.get("day_based_rent_price") or 0)
    available_qty = data.get("available_quantity") or data.get("original_quantity") or 0

    gst_rate = float(data.get("gst_rate") or data.get("gst_percentage") or 0)
    cgst_rate = round(gst_rate / 2.0, 2) if gst_rate else 0
    sgst_rate = cgst_rate
    hsn_code = (data.get("hsn_code") or data.get("hsn_sac_code") or data.get("hsn") or "").strip()

    desc_tag = f"\n            <DESCRIPTION>{escape_xml(full_description)}</DESCRIPTION>" if full_description else ""
    opening_balance_tag = f"\n            <OPENINGBALANCE>{available_qty} {escape_xml(unit)}</OPENINGBALANCE>" if available_qty else ""
    opening_rate_tag = f"\n            <OPENINGRATE>{purchase_price:.2f}/{escape_xml(unit)}</OPENINGRATE>" if purchase_price else ""
    opening_val_tag = f"\n            <OPENINGVALUE>-{purchase_price:.2f}</OPENINGVALUE>" if purchase_price else ""

    gst_block = ""
    if gst_rate or hsn_code:
        hsn_tag = f"\n              <HSNCODE>{escape_xml(hsn_code)}</HSNCODE>\n              <HSN>{escape_xml(hsn_code)}</HSN>" if hsn_code else ""
        rate_of_vat_tag = f"\n            <RATEOFVAT>{gst_rate}</RATEOFVAT>" if gst_rate else ""
        gst_block = f"""{rate_of_vat_tag}
            <GSTAPPLICABLE>Applicable</GSTAPPLICABLE>
            <GSTTYPEOFSUPPLY>Goods</GSTTYPEOFSUPPLY>
            <GSTDETAILS.LIST>
              <APPLICABLEFROM>20240401</APPLICABLEFROM>
              <SRCOFGSTDETAILS>Specify Details Here</SRCOFGSTDETAILS>
              <CALCULATIONTYPE>On Value</CALCULATIONTYPE>
              <TAXABILITY>Taxable</TAXABILITY>{hsn_tag}
              <STATEWISEDETAILS.LIST>
                <STATENAME>Any</STATENAME>
                <RATEDETAILS.LIST>
                  <GSTRATEDUTYHEAD>IGST</GSTRATEDUTYHEAD>
                  <GSTRATE>{gst_rate}</GSTRATE>
                </RATEDETAILS.LIST>
                <RATEDETAILS.LIST>
                  <GSTRATEDUTYHEAD>CGST</GSTRATEDUTYHEAD>
                  <GSTRATE>{cgst_rate}</GSTRATE>
                </RATEDETAILS.LIST>
                <RATEDETAILS.LIST>
                  <GSTRATEDUTYHEAD>SGST/UTGST</GSTRATEDUTYHEAD>
                  <GSTRATE>{sgst_rate}</GSTRATE>
                </RATEDETAILS.LIST>
              </STATEWISEDETAILS.LIST>
            </GSTDETAILS.LIST>"""
        if hsn_code:
            gst_block += f"""
            <HSNDETAILS.LIST>
              <APPLICABLEFROM>20240401</APPLICABLEFROM>
              <SRCOFHSNDETAILS>Specify Details Here</SRCOFHSNDETAILS>
              <HSNCODE>{escape_xml(hsn_code)}</HSNCODE>
              <HSN>{escape_xml(hsn_code)}</HSN>
              <HSNDESCRIPTION>{escape_xml(hsn_code)}</HSNDESCRIPTION>
            </HSNDETAILS.LIST>"""

    price_xml = f"\n            <STANDARDPRICELIST.LIST>\n              <DATE>20240401</DATE>\n              <RATE>{rent_price:.2f}/{escape_xml(unit)}</RATE>\n            </STANDARDPRICELIST.LIST>" if rent_price else ""
    cost_xml = f"\n            <STANDARDCOSTLIST.LIST>\n              <DATE>20240401</DATE>\n              <RATE>{purchase_price:.2f}/{escape_xml(unit)}</RATE>\n            </STANDARDCOSTLIST.LIST>" if purchase_price else ""

    # Prerequisite master XML blocks
    unit_xml = ""
    if not unit_exists:
        unit_xml = f"""          <UNIT NAME="{escape_xml(unit)}" ACTION="Create">\n            <NAME>{escape_xml(unit)}</NAME>{symbol_tag}\n            <ISSIMPLEUNIT>YES</ISSIMPLEUNIT>\n          </UNIT>\n"""

    group_xml = ""
    if group and not group_exists:
        group_xml = f"""          <STOCKGROUP NAME="{escape_xml(group)}" ACTION="Create">\n            <NAME>{escape_xml(group)}</NAME>\n          </STOCKGROUP>\n"""

    category_master_xml = ""
    category_item_tag = ""
    if category:
        if not category_exists:
            category_master_xml = f"""          <STOCKCATEGORY NAME="{escape_xml(category)}" ACTION="Create">\n            <NAME>{escape_xml(category)}</NAME>\n          </STOCKCATEGORY>\n"""
        category_item_tag = f"\n            <CATEGORY>{escape_xml(category)}</CATEGORY>"

    item_xml = f"""{unit_xml}{group_xml}{category_master_xml}          <STOCKITEM NAME="{escape_xml(name)}" ACTION="{action}">
            <NAME>{escape_xml(name)}</NAME>
            <MAILINGNAME.LIST ISMODIFY="Yes" ACTION="Delete"/>
            <PARENT>{escape_xml(group)}</PARENT>{category_item_tag}
            <BASEUNITS>{escape_xml(unit)}</BASEUNITS>{desc_tag}{opening_balance_tag}{opening_rate_tag}{opening_val_tag}{gst_block}{price_xml}{cost_xml}
          </STOCKITEM>"""

    return build_import_envelope(item_xml, report_name="All Masters", company_name=company_name)


def build_physical_stock_voucher_xml(
    item_name: str,
    quantity: float,
    unit: str = "Nos",
    company_name: Optional[str] = None,
    edu_mode: bool = False,
) -> str:
    """
    Builds a Tally "Physical Stock" voucher — the correct mechanism for reconciling a
    stock item's actual quantity, unlike re-sending OPENINGBALANCE on the STOCKITEM
    master itself. OPENINGBALANCE is a fixed baseline as of the books' start date, not a
    live "current stock" field: every Sales voucher pushed afterward keeps consuming
    against that same fixed baseline, so simply re-sending RentAsst's current
    available_quantity as OPENINGBALANCE every equipment-sync cycle does NOT correct
    drift — confirmed live, "Dell Laptop 3440" ended up with a CLOSINGBALANCE of -4 in
    Tally despite OPENINGBALANCE being resent as 11 (its real RentAsst quantity) on every
    cycle, because ~15 units had already been consumed by prior Sales vouchers against
    that one fixed baseline. A Physical Stock voucher instead records a dated inventory
    count that Tally treats as the new "actual truth" for that date, correctly resetting
    CLOSINGBALANCE going forward regardless of the voucher history that preceded it.
    """
    date_str = format_tally_date(None, edu_mode=edu_mode)
    msg = f"""          <VOUCHER VCHTYPE="Physical Stock" ACTION="Create">
            <DATE>{date_str}</DATE>
            <EFFECTIVEDATE>{date_str}</EFFECTIVEDATE>
            <VOUCHERTYPENAME>Physical Stock</VOUCHERTYPENAME>
            <NARRATION>RentAsst stock reconciliation for {escape_xml(item_name)}</NARRATION>
            <ALLINVENTORYENTRIES.LIST>
              <STOCKITEMNAME>{escape_xml(item_name)}</STOCKITEMNAME>
              <ACTUALQTY>{quantity} {escape_xml(unit)}</ACTUALQTY>
              <BILLEDQTY>{quantity} {escape_xml(unit)}</BILLEDQTY>
            </ALLINVENTORYENTRIES.LIST>
          </VOUCHER>"""

    return build_import_envelope(msg, report_name="Vouchers", company_name=company_name)
