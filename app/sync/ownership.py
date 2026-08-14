from typing import Dict, Any, List, Set, Tuple, Optional

# Source of Truth Definitions:
# 'rentasst' = RentAsst is authoritative
# 'tally' = Tally Prime is authoritative
# 'both' = Both systems are allowed to send/sync this field (e.g. core entity identifiers, amounts, dates)

FIELD_OWNERSHIP_POLICY: Dict[str, Dict[str, str]] = {
    "customer": {
        "name": "rentasst",
        "business_name": "rentasst",
        "mobile": "rentasst",
        "phone": "rentasst",
        "email": "rentasst",
        "address": "rentasst",
        "customer_gst_number": "rentasst",
        "gst_number": "rentasst",
        "pan_number": "rentasst",
        "opening_balance": "tally",
        "closing_balance": "tally",
        "credit_limit": "tally",
        "payment_terms": "tally",
    },
    "equipment": {
        "name": "rentasst",
        "sku": "rentasst",
        "asset_code": "rentasst",
        "description": "rentasst",
        "rent_price": "rentasst",
        "day_based_rent_price": "rentasst",
        "asset_category": "rentasst",
        "asset_brand": "rentasst",
        "purchase_price": "tally",
        "opening_stock": "tally",
        "opening_quantity": "tally",
        "available_quantity": "tally",
        "hsn_code": "tally",
    },
    "rental_order": {
        "number": "both",
        "rent_code": "both",
        "customer_id": "both",
        "items": "both",
        "amount": "both",
        "rent_date": "both",
        "status": "both",
        "tally_voucher_id": "tally",
        "tally_master_id": "tally",
        "tally_guid": "tally",
    },
    "invoice": {
        "number": "both",
        "invoice_number": "both",
        "customer_id": "both",
        "subtotal": "both",
        "tax_amount": "both",
        "grand_total": "both",
        "total_amount": "both",
        "invoice_date": "both",
        "due_date": "both",
        "bill_from": "both",
        "bill_to": "both",
        "status": "both",
        "notes": "both",
        "tally_voucher_id": "tally",
        "tally_master_id": "tally",
        "accounting_status": "tally",
    },
    "payment": {
        "reference_id": "both",
        "number": "both",
        "amount": "both",
        "paid_amount": "both",
        "payment_date": "both",
        "paid_type": "both",
        "payment_type_id": "both",
        "invoice_id": "both",
        "notes": "both",
        "payment_method": "both",
        "receipt_voucher_number": "tally",
        "bank_reconciliation_date": "tally",
        "tally_guid": "tally",
    },
}


def get_field_owner(entity_type: str, field_name: str) -> str:
    """Returns the authoritative system ('rentasst', 'tally', or 'both') for a given entity field."""
    ent = (entity_type or "").strip().lower()
    norm_ent = "customer" if ent in ("customer", "customers") else \
               "equipment" if ent in ("equipment", "product", "products") else \
               "rental_order" if ent in ("rental_order", "rental_orders") else \
               "invoice" if ent in ("invoice", "invoices") else \
               "payment" if ent in ("payment", "payments") else ent

    ent_policy = FIELD_OWNERSHIP_POLICY.get(norm_ent, {})
    return ent_policy.get(field_name.lower(), "both")


def filter_payload_by_ownership(
    entity_type: str,
    direction: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Filters payload fields to preserve ownership rules and prevent unauthorized overwrites.
    
    direction == 'forward' (RentAsst -> Tally): Strips fields where Tally is uniquely authoritative.
    direction == 'reverse' (Tally -> RentAsst): Strips fields where RentAsst is uniquely authoritative.
    """
    if not payload or not isinstance(payload, dict):
        return payload

    direction_norm = (direction or "forward").strip().lower()
    restricted_authority = "tally" if direction_norm == "forward" else "rentasst"

    filtered = {}
    for key, val in payload.items():
        owner = get_field_owner(entity_type, key)
        # Always allow generic metadata or fields owned by 'both' or the source authority
        if key in ("id", "company_id", "tally_guid", "created_at", "updated_at") or owner == "both" or owner != restricted_authority:
            filtered[key] = val

    return filtered
