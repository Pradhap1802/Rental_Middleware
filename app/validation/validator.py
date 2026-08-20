from typing import Dict, Any, Tuple, Optional


class PayloadValidator:
    """
    Validation engine ensuring data integrity before generating Tally XML payloads.
    Directly routes invalid payloads to DLQ with descriptive error messages.
    """

    @staticmethod
    def validate_customer(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        if not data or not isinstance(data, dict):
            return False, "Customer payload must be a valid JSON dictionary"

        cid = data.get("id") or data.get("customer_id")
        name = (data.get("name") or data.get("business_name") or "").strip()

        if not cid and not name:
            return False, "Customer payload is missing required field: 'id' or 'name'"

        # Mobile number check if present
        mobile = str(data.get("mobile") or data.get("phone") or "").strip()
        if mobile and len(mobile) < 7:
            return False, f"Invalid customer phone/mobile number '{mobile}' (must be at least 7 digits)"

        return True, None

    @staticmethod
    def validate_equipment(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        if not data or not isinstance(data, dict):
            return False, "Equipment payload must be a valid JSON dictionary"

        eid = data.get("id") or data.get("equipment_id")
        name = (data.get("name") or "").strip()

        if not eid and not name:
            return False, "Equipment payload is missing required field: 'id' or 'name'"

        p_price = float(data.get("purchase_price") or 0)
        r_price = float(data.get("rent_price") or data.get("day_based_rent_price") or 0)
        if p_price < 0 or r_price < 0:
            return False, f"Equipment prices cannot be negative (purchase_price: {p_price}, rent_price: {r_price})"

        return True, None

    @staticmethod
    def validate_rental_order(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        if not data or not isinstance(data, dict):
            return False, "Rental Order payload must be a valid JSON dictionary"

        oid = data.get("id") or data.get("rent_code") or data.get("number")
        if not oid:
            return False, "Rental Order payload is missing required field: 'id' or 'number'"

        cust = data.get("customer_name") or data.get("customer_id") or (data.get("customer") or {}).get("name")
        if not cust:
            return False, "Rental Order is missing required customer reference ('customer_id' or 'customer_name')"

        amount = float(data.get("amount") or data.get("total_amount") or data.get("grand_total") or data.get("rent_amount") or 0)
        if amount < 0:
            return False, f"Rental Order amount cannot be negative (amount: {amount})"
        if amount <= 0:
            return False, f"Rental Order has no amount to sync yet (amount: {amount}) — likely an incomplete/draft order in RentAsst"

        return True, None

    @staticmethod
    def validate_invoice(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        if not data or not isinstance(data, dict):
            return False, "Invoice payload must be a valid JSON dictionary"

        inv_id = data.get("id") or data.get("number") or data.get("invoice_number")
        if not inv_id:
            return False, "Invoice payload is missing required field: 'id' or 'number'"

        cust_id = data.get("customer_id") or (data.get("customer") or {}).get("id") or data.get("customer_name")
        if not cust_id:
            return False, "Invoice is missing required customer reference ('customer_id' or 'customer')"

        grand_total = float(data.get("grand_total") or data.get("total_amount") or data.get("amount") or data.get("grandTotal") or 0)
        subtotal = float(data.get("subtotal") or data.get("sub_total") or data.get("subTotal") or 0)

        # 1. Direct & Breakdown Tax Fields
        tax = float(
            data.get("tax_amount") or data.get("tax") or data.get("vat") or data.get("tax_total")
            or data.get("total_tax") or data.get("gst_amount") or data.get("vat_amount") or data.get("taxAmount") or 0
        )
        if tax == 0:
            cgst = float(data.get("cgst_amount") or data.get("cgst") or 0)
            sgst = float(data.get("sgst_amount") or data.get("sgst") or 0)
            igst = float(data.get("igst_amount") or data.get("igst") or 0)
            tax = cgst + sgst + igst

        # 2. Line Item Tax & Subtotal Aggregation if explicit fields are missing
        items = data.get("items") or data.get("invoice_items") or data.get("details") or data.get("order_items") or []
        if isinstance(items, list) and items:
            if tax == 0:
                item_tax_sum = 0.0
                for it in items:
                    if isinstance(it, dict):
                        t_val = float(
                            it.get("tax_amount") or it.get("tax") or it.get("vat") or it.get("gst_amount")
                            or (float(it.get("cgst_amount") or 0) + float(it.get("sgst_amount") or 0) + float(it.get("igst_amount") or 0))
                        )
                        item_tax_sum += t_val
                if item_tax_sum > 0:
                    tax = item_tax_sum

            if subtotal == 0:
                item_sub_sum = 0.0
                for it in items:
                    if isinstance(it, dict):
                        s_val = float(it.get("subtotal") or it.get("amount") or it.get("total") or 0)
                        if s_val == 0 and "price" in it and "quantity" in it:
                            s_val = float(it["price"]) * float(it["quantity"])
                        item_sub_sum += s_val
                if item_sub_sum > 0:
                    subtotal = item_sub_sum

        charges = float(
            data.get("extra_charges") or data.get("shipping") or data.get("delivery_charge")
            or data.get("shipping_charge") or data.get("other_charges") or 0
        )
        discount = float(data.get("discount") or data.get("discount_amount") or data.get("discountAmount") or 0)
        roundoff = float(
            data.get("roundoff_amount") or data.get("round_off_amount")
            or data.get("roundoff") or data.get("round_off") or 0
        )

        # Fall back subtotal if not provided separately
        if subtotal == 0 and grand_total > 0:
            subtotal = max(0.0, grand_total - tax - charges + discount - roundoff)

        doc_type = data.get("document_type")
        if doc_type != "credit_note" and grand_total <= 0:
            return False, f"Invoice grand total must be greater than zero (grand_total: {grand_total})"

        # 3. Handle implicit tax/adjustments where grand_total > subtotal + charges - discount
        if tax == 0 and grand_total > (subtotal + charges - discount + roundoff):
            tax = round(grand_total - (subtotal + charges - discount + roundoff), 2)

        # Invoice Math Validation: subtotal + tax + charges - discount + roundoff = grand_total (±0.05 tolerance)
        # roundoff accounts for RentAsst's optional invoice-level round-off (e.g. "round to nearest 1"),
        # which deliberately makes grand_total differ from the raw subtotal/tax/charges/discount sum.
        calculated_total = round(subtotal + tax + charges - discount + roundoff, 2)
        diff = abs(calculated_total - round(grand_total, 2))
        if diff > 0.05:
            return (
                False,
                f"Invoice math validation failure: subtotal ({subtotal:.2f}) + tax ({tax:.2f}) + charges ({charges:.2f}) - discount ({discount:.2f}) = {calculated_total:.2f}, but grand_total is {grand_total:.2f} (diff: {diff:.2f})",
            )

        return True, None

    @staticmethod
    def validate_payment(data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        if not data or not isinstance(data, dict):
            return False, "Payment payload must be a valid JSON dictionary"

        pid = (
            data.get("id") or data.get("reference_id") or data.get("number")
            or data.get("receipt_number") or data.get("voucher_number") or data.get("payment_code")
        )
        if not pid:
            return False, "Payment payload is missing required field: 'id' or 'reference_id'"

        amount = float(data.get("amount") or data.get("paid_amount") or data.get("total_amount") or 0)
        if amount <= 0:
            return False, f"Payment amount must be greater than zero (amount: {amount})"

        ref = (
            data.get("invoice_id") or data.get("customer_id") or data.get("paid_by")
            or data.get("customer_name") or data.get("party_name") or data.get("ledger_name")
            or data.get("invoice_number") or data.get("rental_order_id") or data.get("order_id")
            or (data.get("customer") or {}).get("name") or (data.get("customer") or {}).get("id")
            or (data.get("invoice") or {}).get("number") or (data.get("invoice") or {}).get("id")
            or data.get("payment_method") or data.get("mode") or data.get("reference")
            or data.get("notes") or data.get("description")
        )
        if not ref:
            # Fallback to payment ID if explicit reference link is not provided in payload
            ref = str(pid)

        return True, None


def validate_entity_payload(entity_type: str, data: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """
    Main validation router dispatching entity payload validation.
    Returns (is_valid: bool, error_message: Optional[str]).
    """
    ent = (entity_type or "").strip().lower()
    if ent in ("customer", "customers"):
        return PayloadValidator.validate_customer(data)
    elif ent in ("equipment", "product", "products"):
        return PayloadValidator.validate_equipment(data)
    elif ent in ("rental_order", "rental_orders"):
        return PayloadValidator.validate_rental_order(data)
    elif ent in ("invoice", "invoices"):
        return PayloadValidator.validate_invoice(data)
    elif ent in ("payment", "payments"):
        return PayloadValidator.validate_payment(data)

    return True, None
