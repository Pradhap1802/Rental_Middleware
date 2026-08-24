import time
import json
import re
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

# REMOTEID prefixes the forward (RentAsst -> Tally) sync stamps onto every voucher it
# creates (see build_sales_order_voucher_xml / build_sales_invoice_voucher_xml /
# build_receipt_voucher_xml). Any Tally voucher carrying one of these already
# originated from RentAsst via this middleware, so reverse sync must never push it
# back — doing so would create a duplicate record in RentAsst.
FORWARD_REMOTE_ID_PATTERN = re.compile(r"^RENTAL-(ORD|INV|PAY)-(\d+)$")
FORWARD_ENTITY_BY_PREFIX = {"ORD": "rental_order", "INV": "invoice", "PAY": "payment"}

# RentAsst's Rent.settings column is cast to a PHP object, and several of its own
# services (confirmed live: RentItemsService::updateRentDeposit()) read
# $rent->settings->refund_type / ->global_deposit with no null-safe fallback — a rentout
# with a null settings column throws "Attempt to read property on null" as soon as its
# first rent item is created, which aborts that whole DB transaction (the item insert
# gets rolled back too). RentAsst's own frontend always sends a fully-populated settings
# object, which is why this only surfaces for rentouts created through this API-only
# path. Sending an empty {} does NOT fix it either — RentDetailsRequest::all() decodes
# settings with json_decode(..., true), so an empty object becomes an empty PHP array,
# which round-trips through the 'object' cast as an empty array too (json_encode([]) is
# "[]", not "{}") — ->refund_type on an array is a different, equally fatal error. This
# needs at least one real key so PHP's json_encode emits a JSON object, not an array.
DEFAULT_RENTOUT_SETTINGS = {
    "refund_type": 1,  # RefundTypes::TOTAL_DEPOSIT_REFUND — same default used elsewhere
    "roundoff_enabled": False,
    "gst_enabled": False,
    "is_discount": False,
    "is_draft": False,
    "is_due_notified": False,
    "enable_discount_slabs": False,
    "can_suggest_coupon": False,
    "calculate_without_rent_amount": False,
    "enable_discount": False,
    "enable_shipping": False,
    "invoice_enabled": False,
    "global_rent_amount": False,
    "enable_payment_type": False,
    "enable_labour_charge": False,
    "enable_other_amounts": False,
    "enable_transfer_order": False,
    "calendar_month_rental_duration": False,
    "date_to_date_monthly_rental_duration": False,
    "collect_deposit_with_rent_payment": False,
    "global_deposit": False,
}

from ..connectors.tally_fetcher import TallyFetcher
from ..mapping.store import MappingStore
from ..logging.logger import log_event
from .idempotency import generate_integration_key
from .ownership import filter_payload_by_ownership
from ..validation.validator import validate_entity_payload


def format_iso_date(tally_date: Optional[str]) -> str:
    """Converts Tally date string (YYYYMMDD or YYYY-MM-DD) into ISO YYYY-MM-DD format."""
    if not tally_date:
        return datetime.now().strftime("%Y-%m-%d")
    raw = str(tally_date).strip().replace("-", "")
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    try:
        dt = datetime.strptime(str(tally_date).strip(), "%Y-%m-%d")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def _parse_leading_number(raw: Any) -> float:
    """Extracts the leading numeric value from a Tally-formatted string like '1 Piece' or
    '97.00/Piece', where the actual number is followed by a unit suffix Tally includes."""
    if raw is None:
        return 0.0
    text = str(raw).strip().split("/")[0].strip()
    match = re.match(r"[-+]?\d+(\.\d+)?", text)
    return float(match.group(0)) if match else 0.0


def _int_or_none(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip()
    return int(text) if text.isdigit() else None


def resolve_customer_id(party_name: str, ra_client: Any, store: MappingStore) -> int:
    """Finds or resolves Customer ID from RentAsst DB or auto-creates it if new in Tally."""
    if not party_name or party_name.lower() in ("cash", "bank", "sales"):
        party_name = "Walk-in Customer"

    cached_id = store.get_rentasst_id("customer", party_name)
    if cached_id and cached_id.isdigit():
        return int(cached_id)

    try:
        customers = ra_client.fetch_customers()
        if isinstance(customers, list) and len(customers) > 0:
            for c in customers:
                c_name = str(c.get("name") or c.get("business_name") or "").strip()
                if c_name.lower() == party_name.lower() and c.get("id"):
                    cid = int(c["id"])
                    store.save_mapping(
                        entity_type="customer",
                        source_id=party_name,
                        target_id=str(cid),
                        source_system="tally",
                        target_system="rentasst",
                    )
                    return cid

        # If customer does not exist in RentAsst, auto-create customer in RentAsst
        clean_mobile = f"900{abs(hash(party_name)) % 10000000:07d}"
        new_cust = ra_client.push_customer({
            "name": party_name,
            "company_name": party_name,
            "mobile": clean_mobile,
        })
        if new_cust and new_cust.get("id"):
            cid = int(new_cust["id"])
            store.save_mapping(
                entity_type="customer",
                source_id=party_name,
                target_id=str(cid),
                source_system="tally",
                target_system="rentasst",
            )
            return cid
    except Exception as e:
        log_event("ReverseSync", f"Customer resolution lookup note: {e}")
        raise Exception(f"Could not resolve or create RentAsst customer for Tally party '{party_name}': {e}")

    raise Exception(f"Could not resolve or create RentAsst customer for Tally party '{party_name}'.")


def is_own_forward_sync_voucher(v: Dict[str, Any], store: MappingStore) -> bool:
    """
    Detects a voucher that this middleware itself created via forward (RentAsst -> Tally)
    sync, identified by the deterministic marker it stamps into NARRATION on creation.

    NOTE: the marker is carried in NARRATION, not REMOTEID/GUID — confirmed live against
    the real Tally server that REMOTEID/VOUCHERNUMBER supplied on import are NOT echoed
    back on export (Tally reports its own internally-generated GUID/number instead), so
    they can't be used to recognize a previously forward-synced voucher on re-fetch.
    NARRATION is a plain free-text field Tally preserves verbatim.

    Backfills the mapping row if missing (e.g. after a DB reset) so future lookups
    resolve correctly, and always returns True so reverse sync never pushes it back
    into RentAsst.
    """
    marker = (v.get("narration") or "").strip()
    match = FORWARD_REMOTE_ID_PATTERN.match(marker)
    if not match:
        return False

    prefix, rentasst_id = match.group(1), match.group(2)
    entity_type = FORWARD_ENTITY_BY_PREFIX[prefix]
    if not store.find_by_target(entity_type, marker, target_system="tally"):
        store.save_mapping(
            entity_type=entity_type,
            source_id=rentasst_id,
            target_id=marker,
            source_system="rentasst",
            target_system="tally",
            status="synced",
        )
    return True


def is_tally_voucher_duplicate(v: Dict[str, Any], store: MappingStore, ra_client: Optional[Any] = None) -> bool:
    """Checks if a Tally voucher or master record already exists in RentAsst and SQLite mapping store."""
    tally_guid = (v.get("tally_guid") or "").strip()
    v_no = (v.get("voucher_number") or "").strip()
    rentasst_tag = (v.get("rentasst_id") or "").strip()
    v_type = (v.get("voucher_type") or "").lower().strip()

    if rentasst_tag:
        return True

    if is_own_forward_sync_voucher(v, store):
        return True

    if tally_guid:
        for ent in ("rental_orders", "rental_order", "invoice", "payment"):
            rev_key = generate_integration_key("default", ent, tally_guid, "reverse")
            if store.find_by_integration_key(rev_key):
                return True
            ra_id = store.get_rentasst_id(ent, tally_guid)
            if ra_id:
                if ra_client and not ra_client.check_exists_in_rentasst(ent, ra_id):
                    log_event("ReverseSync", f"Record Tally GUID {tally_guid} ('{v_no}') exists in middleware DB but was deleted in RentAsst. Resyncing...")
                    store.delete(ent, ra_id)
                    return False
                return True

    if v_no:
        for ent in ("rental_orders", "rental_order", "invoice", "payment"):
            ra_id = (
                store.get_rentasst_id(ent, v_no)
                or store.get_rentasst_id(ent, f"RENT-{v_no}")
                or store.get_rentasst_id(ent, f"ORD-{v_no}")
                or store.get_rentasst_id(ent, f"INV-{v_no}")
                or store.get_rentasst_id(ent, f"PAY-{v_no}")
            )
            if ra_id:
                if ra_client and not ra_client.check_exists_in_rentasst(ent, ra_id):
                    log_event("ReverseSync", f"Record Tally Voucher #{v_no} exists in middleware DB but was deleted in RentAsst. Resyncing...")
                    store.delete(ent, ra_id)
                    return False
                return True

    # Cloud Deduplication: Check if record already exists on RentAsst server
    if ra_client and (v_no or tally_guid):
        if v_type in ("sales", "sales invoice", "invoice"):
            try:
                cloud_invoices = ra_client.fetch_invoices()
                if isinstance(cloud_invoices, list):
                    for inv in cloud_invoices:
                        inv_num = str(inv.get("number") or "").strip()
                        inv_notes = str(inv.get("notes") or "").strip()
                        if inv_num == v_no or inv_num == f"INV-{v_no}" or f"Voucher #{v_no}" in inv_notes or (tally_guid and tally_guid in inv_notes):
                            cloud_id = str(inv.get("id"))
                            store.save_mapping(
                                entity_type="invoice",
                                source_id=tally_guid or v_no,
                                target_id=cloud_id,
                                source_system="tally",
                                target_system="rentasst",
                                integration_key=generate_integration_key("default", "invoice", tally_guid or v_no, "reverse"),
                                status="synced",
                            )
                            log_event("ReverseSync", f"Invoice for Tally Voucher #{v_no} already exists in RentAsst Cloud DB (ID: {cloud_id}). Saved mapping and skipping duplicate creation.")
                            return True
            except Exception as e:
                log_event("ReverseSync", f"Cloud invoice deduplication lookup note: {e}")

        elif v_type in ("sales order", "sales orders", "order", "orders", "rental order", "rental orders"):
            try:
                cloud_orders = ra_client.fetch_rental_orders()
                if isinstance(cloud_orders, list):
                    for ord_item in cloud_orders:
                        ord_num = str(ord_item.get("number") or ord_item.get("rent_code") or "").strip()
                        ord_notes = str(ord_item.get("notes") or "").strip()
                        if ord_num == v_no or ord_num == f"ORD-{v_no}" or f"Tally #{v_no}" in ord_notes or (tally_guid and tally_guid in ord_notes):
                            cloud_id = str(ord_item.get("id"))
                            store.save_mapping(
                                entity_type="rental_order",
                                source_id=tally_guid or v_no,
                                target_id=cloud_id,
                                source_system="tally",
                                target_system="rentasst",
                                integration_key=generate_integration_key("default", "rental_order", tally_guid or v_no, "reverse"),
                                status="synced",
                            )
                            log_event("ReverseSync", f"Rental Order for Tally Voucher #{v_no} already exists in RentAsst Cloud DB (ID: {cloud_id}). Saved mapping and skipping duplicate creation.")
                            return True
            except Exception as e:
                log_event("ReverseSync", f"Cloud rental order deduplication lookup note: {e}")

        elif v_type in ("receipt", "payment", "receipts", "payments"):
            try:
                cloud_payments = ra_client.fetch_payments()
                if isinstance(cloud_payments, list):
                    for pay_item in cloud_payments:
                        pay_ref = str(pay_item.get("reference_id") or pay_item.get("payment_number") or "").strip()
                        pay_notes = str(pay_item.get("notes") or "").strip()
                        if pay_ref == v_no or pay_ref == f"PAY-{v_no}" or f"Receipt #{v_no}" in pay_notes or (tally_guid and tally_guid in pay_notes):
                            cloud_id = str(pay_item.get("id"))
                            store.save_mapping(
                                entity_type="payment",
                                source_id=tally_guid or v_no,
                                target_id=cloud_id,
                                source_system="tally",
                                target_system="rentasst",
                                integration_key=generate_integration_key("default", "payment", tally_guid or v_no, "reverse"),
                                status="synced",
                            )
                            log_event("ReverseSync", f"Payment for Tally Receipt #{v_no} already exists in RentAsst Cloud DB (ID: {cloud_id}). Saved mapping and skipping duplicate creation.")
                            return True
            except Exception as e:
                log_event("ReverseSync", f"Cloud payment deduplication lookup note: {e}")

    return False


def sync_tally_to_rentasst(
    ra_client: Any,
    ext_client: Any,
    store: MappingStore,
    force_full_sync: bool = True,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Production-grade reverse synchronization runner:
    Fetches Vouchers (Sales Orders, Sales Invoices, Receipts) from Tally Prime, applies reverse field-ownership policy filtering,
    validates payload schemas, posts to RentAsst Cloud API, and persists SQLite mapping
    ONLY AFTER confirmed HTTP success response.
    """
    stats = {"processed": 0, "created": 0, "updated": 0, "failed": 0, "skipped": 0}
    start_time = time.time()

    fetcher = TallyFetcher(ext_client.cfg)
    if force_full_sync:
        last_alter_id = 0
    else:
        raw_checkpoint = store.get_checkpoint("tally_alter_id")
        try:
            last_alter_id = int(raw_checkpoint) if raw_checkpoint else 0
        except ValueError:
            last_alter_id = 0

    max_alter_id = 0

    try:
        # 1. Reverse sync new Customers (Sundry Debtors) from Tally to RentAsst
        ledgers = fetcher.fetch_ledgers(last_alter_id=last_alter_id)
        for l in ledgers:
            cust_name = l.get("name") or ""
            l_alter_id = l.get("alter_id") or 0
            if l_alter_id > max_alter_id:
                max_alter_id = l_alter_id

            if not cust_name:
                continue

            cached_id = store.get_rentasst_id("customer", cust_name)
            if cached_id:
                continue

            stats["processed"] += 1
            cloud_id = None
            try:
                cloud_custs = ra_client.fetch_customers()
                if isinstance(cloud_custs, list):
                    for c in cloud_custs:
                        if (c.get("name") or "").strip().lower() == cust_name.lower():
                            cloud_id = str(c.get("id"))
                            break
            except Exception:
                pass

            if cloud_id:
                store.save_mapping(
                    entity_type="customer",
                    source_id=cust_name,
                    target_id=cloud_id,
                    source_system="tally",
                    target_system="rentasst",
                )
                stats["skipped"] += 1
                continue

            try:
                clean_phone = re.sub(r"\D", "", str(l.get("phone") or l.get("mobile") or ""))
                mobile_number = clean_phone if len(clean_phone) >= 10 else f"900{abs(hash(cust_name)) % 10000000:07d}"

                res = ra_client.push_customer({
                    "name": cust_name,
                    "company_name": cust_name,
                    "mobile": mobile_number,
                    "email": l.get("email") or "",
                    "gst_number": l.get("gstin") or "",
                })
                ra_id = str(res.get("id") or f"RA-CUST-{l_alter_id}")
                store.save_mapping(
                    entity_type="customer",
                    source_id=cust_name,
                    target_id=ra_id,
                    source_system="tally",
                    target_system="rentasst",
                )
                store.add_history("customer", ra_id, "synced", external_id=cust_name, details="Tally Customer Reverse Sync")
                stats["created"] += 1
            except Exception as e:
                stats["failed"] += 1
                log_event("ReverseSync", f"Failed to push Tally customer '{cust_name}': {e}")

        # 2. Reverse sync new Assets / Equipment (Stock Items) from Tally to RentAsst
        stock_items = fetcher.fetch_stock_items(last_alter_id=last_alter_id)
        for s in stock_items:
            item_name = s.get("name") or ""
            parent_category = s.get("parent") or ""
            unit_name = s.get("unit") or ""
            hsn_code = s.get("hsn_code") or ""
            gst_rate = s.get("gst_rate") or 0.0
            s_alter_id = s.get("alter_id") or 0
            if s_alter_id > max_alter_id:
                max_alter_id = s_alter_id

            cached_id = store.get_rentasst_id("equipment", item_name)
            if cached_id:
                stats["skipped"] += 1
                continue

            stats["processed"] += 1

            # Check if asset already exists in RentAsst Cloud
            cloud_id = None
            try:
                cloud_assets = ra_client.fetch_equipment()
                if isinstance(cloud_assets, list):
                    for a in cloud_assets:
                        if (a.get("name") or "").strip().lower() == item_name.lower():
                            cloud_id = str(a.get("id"))
                            break
            except Exception:
                pass

            if cloud_id:
                store.save_mapping(
                    entity_type="equipment",
                    source_id=item_name,
                    target_id=cloud_id,
                    source_system="tally",
                    target_system="rentasst",
                )
                stats["skipped"] += 1
                continue

            # Resolve Category & Unit for new asset creation
            category_id = ra_client.resolve_category_id(parent_category) if parent_category else None
            unit_id = ra_client.resolve_unit_id(unit_name) if unit_name else None

            asset_payload = {
                "name": item_name,
                "calculation_method": "[1]",
                "rent_price": "0.00",
                "day_based_rent_price": "0.00",
                "purchase_price": "0.00",
                "hsn_code": hsn_code,
                "gst_rate": gst_rate if gst_rate > 0 else None,
                "unit_id": unit_id,
                "category_id": category_id,
                "category_ids": json.dumps([category_id]) if category_id else None,
                "skip_inventory": True,
                "enabled_for_rent": True,
                "description": s.get("description") or "",
            }

            # Create brand new asset in RentAsst
            try:
                res = ra_client.push_equipment(asset_payload)
                ra_id = str(res.get("id") or f"RA-ASSET-{s_alter_id}")
                store.save_mapping(
                    entity_type="equipment",
                    source_id=item_name,
                    target_id=ra_id,
                    source_system="tally",
                    target_system="rentasst",
                )
                store.add_history("equipment", ra_id, "synced", external_id=item_name, details="Tally Stock Item Reverse Sync")
                stats["created"] += 1
            except Exception as e:
                stats["failed"] += 1
                log_event("ReverseSync", f"Failed to push Tally stock item '{item_name}': {e}")

        # 3. Reverse sync Vouchers (Sales Orders, Invoices, Receipts)
        vouchers = fetcher.fetch_vouchers(last_alter_id=last_alter_id, from_date=from_date, to_date=to_date)
        
        for v in vouchers:
            stats["processed"] += 1
            tally_guid = v.get("tally_guid") or ""
            alter_id = v.get("alter_id") or 0
            if alter_id > max_alter_id:
                max_alter_id = alter_id

            v_type = (v.get("voucher_type") or "").lower().strip()
            is_invoice_type = v_type in ("sales", "sales invoice", "invoice")
            is_rentout_type = v_type in ("sales order", "sales orders", "order", "orders", "rental order", "rental orders")

            # Invoices and rentouts get their own create-vs-update-vs-skip handling further
            # down (an already-synced record must still be checked for missing line items
            # and backfilled — see the "backfill" comments below — not skipped forever) —
            # payments keep the plain skip-on-duplicate behavior, since a receipt is never
            # revised after creation.
            if not is_invoice_type and not is_rentout_type and is_tally_voucher_duplicate(v, store, ra_client):
                stats["skipped"] += 1
                continue

            try:
                party_name = v.get("party_name") or "Customer"
                iso_date = format_iso_date(v.get("date"))

                if v_type in ("sales order", "sales orders", "order", "orders", "rental order", "rental orders"):
                    cust_id = resolve_customer_id(party_name, ra_client, store)
                    amount = float(v.get("amount") or 0.0)
                    # RentAsst's create-rent-details endpoint (RentDetailsRequest) requires
                    # rent_from/rent_to as full "Y-m-d H:i:s" timestamps — a date-only string
                    # fails Laravel's date_format validation with a 422, confirmed against
                    # RentAsst's own RentDetailsRequest::rules() source.
                    iso_datetime = f"{iso_date} 00:00:00"
                    # Rent ITEMS need a genuinely non-zero rent_from/rent_to gap — RentAsst
                    # rejects an item with identical start/end ("Invalid rental duration:
                    # Start and end times are identical") even though the Rent header itself
                    # is perfectly valid as a same-day order. One day is the same minimum
                    # RentAsst's own rental_duration_value validation enforces elsewhere.
                    item_rent_to_datetime = f"{(datetime.strptime(iso_date, '%Y-%m-%d') + timedelta(days=1)).strftime('%Y-%m-%d')} 00:00:00"

                    rentout_payload = {
                        "number": str(v.get("voucher_number") or f"ORD-{tally_guid[:8]}"),
                        "customer_id": cust_id,
                        "rent_from": iso_datetime,
                        "rent_to": iso_datetime,
                        "order_booking_date": iso_datetime,
                        "grand_total": amount,
                        "total_amount": amount,
                        # RentAsst's 'status' column is a numeric code (RentStatuses::UPCOMING
                        # = 1), validated as nullable|numeric|between:0,10 — a string like
                        # "confirmed" fails that validation with a 422.
                        "status": 1,
                        "notes": f"Imported from Tally Sales Order #{v.get('voucher_number')}",
                        "tally_guid": tally_guid,
                        "settings": DEFAULT_RENTOUT_SETTINGS,
                    }

                    # RentAsst's create-rent-details endpoint silently drops an 'items'
                    # field on the rentout payload itself (RentItem is a separate
                    # rent_items table, not a column on Rent) — each Tally inventory line
                    # must be resolved to a RentAsst asset_id via the equipment
                    # reverse-mapping populated in step 2 above, then pushed through
                    # push_rentout_items() once the rentout exists.
                    resolved_items = []
                    for it in (v.get("items") or []):
                        item_name = str(it.get("name") or "").strip()
                        if not item_name:
                            continue
                        qty = int(_parse_leading_number(it.get("quantity"))) or 1
                        price = _parse_leading_number(it.get("rate"))
                        total_price = _parse_leading_number(it.get("amount")) or round(price * qty, 2)
                        resolved_items.append({
                            # 'id' must be present (even as None/new) — RentAsst's own
                            # RentService::prepareAssetAvailabilityData() unconditionally
                            # reads $item['id']/$item['rent_from']/$item['rent_to'] before an
                            # item exists, for every item with a non-null asset_id (confirmed
                            # live: omitting rent_from raised "Undefined array key 'rent_from'"
                            # inside that availability check and surfaced as an HTTP 500).
                            "id": None,
                            "asset_id": _int_or_none(store.get_external_id("equipment", item_name)),
                            "asset_name": item_name,
                            "rented_quantity": qty,
                            "price": price,
                            "total_price": total_price,
                            "rent_from": iso_datetime,
                            "rent_to": item_rent_to_datetime,
                            # discount_is_percentage has a NOT NULL DB constraint with no
                            # default — confirmed live via a raw SQLSTATE[23502] error when
                            # omitted.
                            "discount_value": 0,
                            "discount_is_percentage": False,
                        })

                    # 1. Field Ownership Policy Filter (Reverse Direction: Tally -> RentAsst)
                    filtered_payload = filter_payload_by_ownership("rental_order", "reverse", rentout_payload)

                    # 2. Check whether THIS reverse sync already created this rentout before
                    # (captured before is_tally_voucher_duplicate, which can itself write a
                    # fresh mapping on a cloud-dedup match — that's a different, unrelated
                    # rentout we didn't create, so it stays a plain skip, not a backfill
                    # target — same reasoning as the invoice branch below).
                    existing_ra_id = store.get_external_id("rental_order", tally_guid)

                    if is_tally_voucher_duplicate(v, store, ra_client):
                        # Backfill: a rentout we created before that still has zero rent items
                        # (either because it predates push_rentout_items() existing, or a
                        # prior push failed) gets its lines added now. Checking the live item
                        # count first — rather than just "did we ever try before" — makes this
                        # safe to run on every sync: once items exist, this never re-pushes
                        # and never duplicates them.
                        if existing_ra_id and resolved_items:
                            try:
                                current = ra_client.get_rentout(existing_ra_id)
                                current_count = (current.get("rent_items_count") or 0) if isinstance(current, dict) else 0
                                if not current_count:
                                    # A rentout created before 'settings' was included on
                                    # create (below) still has a null settings column, which
                                    # crashes RentAsst's own item-creation transaction (see
                                    # DEFAULT_RENTOUT_SETTINGS) — patch it first so the
                                    # backfill below doesn't get silently rolled back.
                                    if isinstance(current, dict) and not current.get("settings"):
                                        ra_client.update_rentout(existing_ra_id, {
                                            "settings": DEFAULT_RENTOUT_SETTINGS,
                                            "status": current.get("status") or 1,
                                            "customer_id": cust_id,
                                            "rent_from": iso_datetime,
                                            "rent_to": iso_datetime,
                                        })
                                    ra_client.push_rentout_items(existing_ra_id, resolved_items)
                                    store.add_history("rental_order", existing_ra_id, "synced", external_id=tally_guid, details="Tally Sales Order Reverse Sync — backfilled missing rent items")
                                    stats["updated"] += 1
                                else:
                                    stats["skipped"] += 1
                            except Exception as e:
                                log_event("ReverseSync", f"Failed to backfill rent items for RentAsst rentout {existing_ra_id} (Tally GUID {tally_guid}): {e}")
                                stats["skipped"] += 1
                        else:
                            stats["skipped"] += 1
                        continue

                    # 3. Post to RentAsst Cloud REST API
                    res = ra_client.push_rentout(filtered_payload)

                    # 4. Save SQLite mapping ONLY after confirmed HTTP success
                    ra_id = str(res.get("id") or res.get("rentasst_id") or f"RA-ORD-{alter_id}")
                    rev_key = generate_integration_key("default", "rental_order", tally_guid, "reverse")

                    store.save_mapping(
                        entity_type="rental_order",
                        source_id=tally_guid,
                        target_id=ra_id,
                        source_system="tally",
                        target_system="rentasst",
                        integration_key=rev_key,
                        status="synced",
                    )
                    store.add_history("rental_order", ra_id, "synced", external_id=tally_guid, details="Tally Sales Order Reverse Sync")

                    # 5. Push asset/quantity/price lines once, right after creation
                    if resolved_items:
                        try:
                            ra_client.push_rentout_items(ra_id, resolved_items)
                        except Exception as e:
                            log_event("ReverseSync", f"Failed to push rent items for RentAsst rentout {ra_id} (Tally GUID {tally_guid}): {e}")

                    stats["created"] += 1

                elif v_type in ("sales", "sales invoice", "invoice"):
                    cust_id = resolve_customer_id(party_name, ra_client, store)
                    amount = float(v.get("amount") or 0.0)
                    voucher_number = str(v.get("voucher_number") or "").strip()

                    # RentAsst's paid_amount is computed live from linked RentPayment rows,
                    # not a field we can set directly — but its persisted 'status' column
                    # isn't, so derive it from any receipt in this same batch that settled
                    # against this invoice's bill (bill_ref == this voucher's number).
                    paid_so_far = 0.0
                    for other in vouchers:
                        other_type = (other.get("voucher_type") or "").lower().strip()
                        if other_type not in ("receipt", "payment", "receipts", "payments"):
                            continue
                        other_bill_ref = (other.get("bill_ref") or "").strip()
                        if voucher_number and other_bill_ref.lower() == voucher_number.lower():
                            paid_so_far += float(other.get("amount") or 0.0)

                    if paid_so_far <= 0:
                        invoice_status = "confirmed"
                    elif paid_so_far + 0.01 < amount:
                        invoice_status = "partiallyPaid"
                    else:
                        invoice_status = "paid"

                    invoice_payload = {
                        "number": voucher_number or f"INV-{tally_guid[:8]}",
                        "customer_id": cust_id,
                        "invoice_date": iso_date,
                        "due_date": iso_date,
                        "bill_from": iso_date,
                        "bill_to": iso_date,
                        "subtotal": amount,
                        "grand_total": amount,
                        "total_amount": amount,
                        "status": invoice_status,
                        "notes": f"Imported from Tally Sales Register Voucher #{v.get('voucher_number')}",
                        "tally_guid": tally_guid,
                    }

                    # Resolve each Tally inventory line to a RentAsst asset_id via the
                    # equipment reverse-mapping populated in step 2 above, so product lines
                    # carry a real link rather than just a free-text name. RentAsst's invoice
                    # create/update endpoints silently drop an 'items' field on the invoice
                    # payload itself (InvoiceItem is a separate resource) — these must be
                    # pushed through push_invoice_items() after the invoice exists.
                    resolved_items = []
                    for it in (v.get("items") or []):
                        item_name = str(it.get("name") or "").strip()
                        if not item_name:
                            continue
                        qty = int(_parse_leading_number(it.get("quantity"))) or 1
                        price = _parse_leading_number(it.get("rate"))
                        total_price = _parse_leading_number(it.get("amount")) or round(price * qty, 2)
                        resolved_items.append({
                            "name": item_name,
                            "asset_id": _int_or_none(store.get_external_id("equipment", item_name)),
                            "quantity": qty,
                            "price": price,
                            "total_price": total_price,
                            "product_type": "product",
                        })

                    # 1. Field Ownership Policy Filter (Reverse Direction: Tally -> RentAsst)
                    filtered_payload = filter_payload_by_ownership("invoice", "reverse", invoice_payload)

                    # 2. Pre-Flight Data Validation Check
                    is_valid, val_err = validate_entity_payload("invoice", filtered_payload)
                    if not is_valid:
                        log_event("ReverseSync", f"Payload validation failed for Tally Invoice reverse sync (GUID {tally_guid}): {val_err}")
                        store.add_dead_letter("invoice", tally_guid, f"Reverse Sync Validation Failure: {val_err}", json.dumps(v))
                        stats["failed"] += 1
                        continue

                    # 3. Check whether THIS reverse sync already created this invoice before
                    # (captured before is_tally_voucher_duplicate, which can itself write a
                    # fresh mapping on a cloud-dedup match — that's a different, unrelated
                    # invoice we didn't create, so it stays a plain skip, not an update target).
                    existing_ra_id = store.get_external_id("invoice", tally_guid)

                    if is_tally_voucher_duplicate(v, store, ra_client):
                        if existing_ra_id:
                            # Already synced previously — refresh status/amount only. Never
                            # blindly re-push items here: items-bulk-create appends rows
                            # rather than replacing them, so repeating it on an invoice that
                            # already has its lines would duplicate them.
                            try:
                                ra_client.update_invoice(existing_ra_id, {
                                    "status": invoice_status,
                                    "subtotal": amount,
                                    "grand_total": amount,
                                    "total_amount": amount,
                                })
                                store.add_history("invoice", existing_ra_id, "synced", external_id=tally_guid, details=f"Tally Sales Register Reverse Sync update (status: {invoice_status})")
                                stats["updated"] += 1
                            except Exception as e:
                                stats["failed"] += 1
                                log_event("ReverseSync", f"Failed to update RentAsst invoice {existing_ra_id} (Tally GUID {tally_guid}): {e}")

                            # Backfill: an invoice created before push_invoice_items() existed
                            # (or whose item push failed) still has zero items — check the
                            # live item count first, not just "did we try before", so this is
                            # safe to run every sync: once items exist, this never re-pushes.
                            if resolved_items:
                                try:
                                    current = ra_client.get_invoice(existing_ra_id)
                                    current_items = current.get("items") if isinstance(current, dict) else None
                                    if not current_items:
                                        ra_client.push_invoice_items(existing_ra_id, resolved_items)
                                        store.add_history("invoice", existing_ra_id, "synced", external_id=tally_guid, details="Tally Sales Register Reverse Sync — backfilled missing line items")
                                except Exception as e:
                                    log_event("ReverseSync", f"Failed to backfill line items for RentAsst invoice {existing_ra_id} (Tally GUID {tally_guid}): {e}")
                        else:
                            stats["skipped"] += 1
                        continue

                    # 4. Post to RentAsst Cloud REST API
                    res = ra_client.push_invoice(filtered_payload)

                    # 5. Save SQLite mapping ONLY after confirmed HTTP success
                    ra_id = str(res.get("id") or res.get("rentasst_id") or f"RA-INV-{alter_id}")
                    rev_key = generate_integration_key("default", "invoice", tally_guid, "reverse")

                    store.save_mapping(
                        entity_type="invoice",
                        source_id=tally_guid,
                        target_id=ra_id,
                        source_system="tally",
                        target_system="rentasst",
                        integration_key=rev_key,
                        status="synced",
                    )
                    store.add_history("invoice", ra_id, "synced", external_id=tally_guid, details="Tally Sales Register Reverse Sync")

                    # 6. Push product lines once, right after creation
                    if resolved_items:
                        try:
                            ra_client.push_invoice_items(ra_id, resolved_items)
                        except Exception as e:
                            log_event("ReverseSync", f"Failed to push line items for RentAsst invoice {ra_id} (Tally GUID {tally_guid}): {e}")

                    stats["created"] += 1

                elif v_type in ("receipt", "payment", "receipts", "payments"):
                    amount = float(v.get("amount") or 0.0)
                    payment_payload = {
                        "paid_type": 1,
                        "payment_type_id": 1,
                        "amount": amount,
                        "reference_id": str(v.get("voucher_number") or f"PAY-{tally_guid[:8]}")[:45],
                        "notes": f"Tally Receipt #{v.get('voucher_number')} ({party_name})",
                        "tally_guid": tally_guid,
                    }

                    # Resolve the invoice this receipt is settling via Tally's bill-wise
                    # "Agst Ref" allocation (the invoice's own voucher number), NOT the
                    # payment's own GUID — a payment's GUID can never match an invoice's
                    # identity, so that lookup always missed and silently fell back to an
                    # arbitrary invoice. bill_ref is the Tally-side source of truth for
                    # which bill this receipt is against.
                    bill_ref = (v.get("bill_ref") or "").strip()
                    invoice_id = None
                    if bill_ref:
                        try:
                            inv_list = ra_client.fetch_invoices()
                            if isinstance(inv_list, list):
                                for inv in inv_list:
                                    inv_num = str(inv.get("number") or "").strip()
                                    if inv_num and inv_num.lower() == bill_ref.lower() and inv.get("id"):
                                        invoice_id = int(inv["id"])
                                        break
                        except Exception as e:
                            log_event("ReverseSync", f"Invoice lookup by bill reference '{bill_ref}' failed: {e}")

                    if invoice_id is None:
                        store.add_dead_letter(
                            "payment", tally_guid,
                            f"Could not resolve the RentAsst invoice for Tally Receipt #{v.get('voucher_number')} "
                            f"(bill reference: '{bill_ref or 'none found'}'). Refusing to attach payment to an "
                            f"arbitrary invoice.",
                            json.dumps(v),
                        )
                        stats["failed"] += 1
                        continue

                    payment_payload["invoice_id"] = invoice_id

                    # 1. Field Ownership Policy Filter
                    filtered_payload = filter_payload_by_ownership("payment", "reverse", payment_payload)

                    # 2. Pre-Flight Data Validation Check
                    is_valid, val_err = validate_entity_payload("payment", filtered_payload)
                    if not is_valid:
                        log_event("ReverseSync", f"Payload validation failed for Tally Receipt reverse sync (GUID {tally_guid}): {val_err}")
                        store.add_dead_letter("payment", tally_guid, f"Reverse Sync Validation Failure: {val_err}", json.dumps(v))
                        stats["failed"] += 1
                        continue

                    # 3. Post to RentAsst Cloud REST API
                    res = ra_client.push_payment(filtered_payload)

                    # 4. Save SQLite mapping ONLY after confirmed HTTP success
                    ra_id = str(res.get("id") or res.get("rentasst_id") or f"RA-PAY-{alter_id}")
                    rev_key = generate_integration_key("default", "payment", tally_guid, "reverse")

                    store.save_mapping(
                        entity_type="payment",
                        source_id=tally_guid,
                        target_id=ra_id,
                        source_system="tally",
                        target_system="rentasst",
                        integration_key=rev_key,
                        status="synced",
                    )
                    store.add_history("payment", ra_id, "synced", external_id=tally_guid, details="Tally Voucher Reverse Sync")
                    stats["created"] += 1
                else:
                    stats["skipped"] += 1

            except Exception as ex:
                stats["failed"] += 1
                error_msg = str(ex)
                log_event("ReverseSync", f"Failed to push Tally voucher {tally_guid} ({v_type}): {error_msg}")
                store.add_dead_letter("voucher", tally_guid, error_msg, json.dumps(v))

        if max_alter_id > last_alter_id:
            store.set_checkpoint("tally_alter_id", str(max_alter_id))

        duration_ms = (time.time() - start_time) * 1000
        log_event(
            "ReverseSync",
            f"Tally to RentAsst sync completed: {stats} (Max ALTERID: {max_alter_id})",
            duration_ms=duration_ms,
            metadata=stats,
        )
        return stats

    except Exception as e:
        log_event("ReverseSync", f"Tally to RentAsst sync error: {str(e)}")
        return stats
