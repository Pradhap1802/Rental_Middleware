import time
import json
import re
import hashlib
import threading
from datetime import datetime, timedelta
from typing import Dict, Any, Optional


def _normalize_name_for_match(name: Optional[str]) -> str:
    """
    Lowercases, trims, and collapses internal whitespace runs to a single space —
    used wherever a Tally record name is matched against RentAsst's live list to
    decide "does this already exist" before creating a new record. A plain
    .strip().lower() only handles leading/trailing whitespace and case; confirmed
    live that a RentAsst asset named "Water  Bottle" (double space) failed to match
    Tally's "Water Bottle" (single space) under that comparison, so reverse sync
    never found it and created a genuine duplicate. Collapsing internal whitespace
    on both sides closes that gap without weakening the match otherwise.
    """
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def _synthetic_mobile_number(name: str) -> str:
    """
    Deterministic placeholder mobile number for a Tally party with no real one, used
    when auto-creating that party as a RentAsst customer (which requires a mobile field).
    Must be stable across process restarts for the same name — Python's built-in hash()
    is NOT (it's salted per-process via PYTHONHASHSEED by default), so a retry after a
    crash between push_customer() succeeding and the mapping being saved locally could
    previously generate a different placeholder number for the same party, risking a
    duplicate RentAsst customer record.
    """
    digest = hashlib.md5(name.encode("utf-8")).hexdigest()
    return f"900{int(digest, 16) % 10000000:07d}"


def _extract_primary_mobile(ledger: Dict[str, Any], cust_name: str) -> str:
    """
    LEDGERMOBILE is Tally's own single, clean primary-mobile field — the forward sync
    (RentAsst -> Tally) always writes it as one plain number, unlike LEDGERPHONE, which
    it writes as every mobile/alternate-mobile RentAsst has joined with ", " (e.g.
    "08056997998, 08056997998"). Confirmed live: stripping non-digits from that whole
    joined LEDGERPHONE string concatenates every number into one garbled value (e.g.
    "0805699799808056997998"), which is exactly what reverse sync was pushing back to
    RentAsst as this customer's mobile — a real but different-looking number, not the
    customer's actual one. LEDGERMOBILE must be preferred; only fall back to the FIRST
    comma-separated segment of LEDGERPHONE (never the whole string) for a ledger with no
    LEDGERMOBILE, and only fall back to a synthetic placeholder if neither has a usable
    number at all.
    """
    clean_mobile = re.sub(r"\D", "", str(ledger.get("mobile") or ""))
    if len(clean_mobile) >= 10:
        return clean_mobile
    first_phone_segment = str(ledger.get("phone") or "").split(",")[0]
    clean_phone = re.sub(r"\D", "", first_phone_segment)
    if len(clean_phone) >= 10:
        return clean_phone
    return _synthetic_mobile_number(cust_name)


def _has_real_tally_mobile(ledger: Dict[str, Any]) -> bool:
    """
    True only when this Tally ledger has an actual mobile/phone number entered —
    i.e. _extract_primary_mobile() didn't have to fall back to a synthetic
    placeholder. Used to decide whether reverse sync's UPDATE path is allowed to
    touch RentAsst's 'mobile' field at all: confirmed live that a customer with no
    mobile entered in Tally had their REAL RentAsst mobile silently overwritten with
    a fake placeholder number on every reverse sync, purely because Tally's ledger
    had nothing filled in — not entering a mobile in Tally must mean "leave RentAsst's
    own value alone", not "erase it with a synthetic one".
    """
    clean_mobile = re.sub(r"\D", "", str(ledger.get("mobile") or ""))
    if len(clean_mobile) >= 10:
        return True
    first_phone_segment = str(ledger.get("phone") or "").split(",")[0]
    clean_phone = re.sub(r"\D", "", first_phone_segment)
    return len(clean_phone) >= 10


def _extract_address_payload(ledger: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Builds a RentAsst-shaped address record from a Tally ledger's ADDRESS.LIST/PINCODE/
    COUNTRYNAME/LEDSTATENAME fields — reverse sync never fetched or pushed any of these
    before, so a customer's address was never synced back from Tally at all, only
    created blank. Tally's ADDRESS.LIST is free-text lines with no city/street split, so
    the best available structure is all lines joined into address1, with the
    state/country/pincode RentAsst does track kept in their own separate fields.

    Returns a single dict (NOT a list) — confirmed live that RentAsst's address record is
    managed through its own dedicated endpoints (create_customer_address/
    update_customer_address), one record at a time, never as an embedded array on the
    customer payload (which silently ignores it entirely on both create and update).

    'full_address' is required by RentAsst's create-address endpoint (confirmed live: a
    422 "The full address field is required" without it) — built in the same
    country/city/address1/state/zipcode order RentAsst itself uses when rendering an
    existing address's own full_address field.

    Returns None (omit entirely) when Tally has no address data at all, rather than
    pushing an empty address that would overwrite a real one already in RentAsst.
    """
    lines = ledger.get("address_lines") or []
    state = ledger.get("state") or ""
    country = ledger.get("country") or ""
    pincode = ledger.get("pincode") or ""
    if not lines and not state and not country and not pincode:
        return None
    address1 = ", ".join(lines)
    full_address = ", ".join(p for p in [country, address1, state, pincode] if p)
    return {
        "address1": address1,
        "city": "",
        "state": state,
        "country": country,
        "zipcode": pincode,
        "full_address": full_address,
        "is_default": True,
        "is_billing": True,
    }


def _push_customer_address(ra_client: Any, ra_id: str, address_payload: Dict[str, Any]) -> None:
    """
    Creates or updates a RentAsst customer's address, choosing the right call by checking
    whether the customer already has an address record on file (RentAsst's address
    create/update are two distinct endpoints, keyed by the address record's own id — see
    create_customer_address/update_customer_address). Best-effort: an address failure must
    never abort the surrounding customer create/update, which already succeeded.
    """
    try:
        current = ra_client.get_customer(ra_id)
        existing_addresses = (current or {}).get("address") or []
        existing_id = existing_addresses[0].get("id") if existing_addresses else None
    except Exception as e:
        log_event("ReverseSync", f"Could not fetch RentAsst customer {ra_id} to resolve its address record: {e}")
        return

    try:
        if existing_id:
            ra_client.update_customer_address(ra_id, existing_id, address_payload)
        else:
            ra_client.create_customer_address(ra_id, address_payload)
    except Exception as e:
        log_event("ReverseSync", f"Failed to sync address for RentAsst customer {ra_id}: {e}")


def _equipment_change_hash(
    hsn_code: str, gst_rate: float, quantity: float, parent_category: str,
    unit_name: str, rent_price: float = 0.0, description: str = "",
) -> str:
    """
    Hashes every Tally-side field this reverse sync actually pushes into the create/
    update payload (update_payload/asset_payload below) — anything sent but left out of
    this hash would silently never trigger an update once a stock item is first synced,
    because a real change to it wouldn't move the hash at all. Confirmed live: an HSN
    code, GST rate, quantity, or rental price corrected/changed in Tally after the item's
    first reverse sync never reached RentAsst before this hash existed; 'description' was
    the same kind of gap found afterward — sent in both payloads but never part of the
    hash, so a Tally-side description edit alone would never resolve to "push an update".
    Comparing this hash against what was last pushed lets an unrelated Tally edit still
    resolve to "unchanged, skip" while a genuine change resolves to "push an update".
    """
    raw = json.dumps(
        {"hsn_code": hsn_code, "gst_rate": gst_rate, "quantity": quantity,
         "parent_category": parent_category, "unit_name": unit_name, "rent_price": rent_price,
         "description": description},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _customer_contact_hash(mobile: str, email: str, gst_number: str, address: Optional[Dict[str, Any]]) -> str:
    """
    Hashes the Tally-side fields this reverse sync is responsible for keeping RentAsst's
    customer record in sync with. Without this, a customer whose mapping already exists
    (created either by an earlier reverse sync or by the forward sync originally) was
    skipped unconditionally the moment a mapping was found — confirmed live: a mobile
    number corrected in Tally, a GST number added later, or an address filled in after
    the customer's first sync never reached RentAsst at all, because the loop `continue`d
    before any of these fields were ever looked at again. Comparing this hash against
    what was last pushed lets an unrelated Tally ledger edit still resolve to "unchanged,
    skip" while a genuine contact/GST/address change resolves to "push an update".
    """
    raw = json.dumps(
        {"mobile": mobile, "email": email, "gst_number": gst_number, "address": address},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

# REMOTEID prefixes the forward (RentAsst -> Tally) sync stamps onto every voucher it
# creates (see build_sales_order_voucher_xml / build_sales_invoice_voucher_xml /
# build_receipt_voucher_xml). Any Tally voucher carrying one of these already
# originated from RentAsst via this middleware, so reverse sync must never push it
# back — doing so would create a duplicate record in RentAsst.
FORWARD_REMOTE_ID_PATTERN = re.compile(r"^RENTAL-(ORD|INV|PAY)-(\d+)$")
FORWARD_ENTITY_BY_PREFIX = {"ORD": "rental_order", "INV": "invoice", "PAY": "payment"}

# Synthetic entity_type used purely as a dedup ledger (via the mapping store's existing
# find_mapping/save_mapping helpers) for GST/HSN/rent-price/description drift on a
# RentAsst-native equipment item — never a real synced entity, and deliberately NEVER
# entity_type="equipment", which run_sync_pipeline's forward-sync guard (app/sync/base.py)
# reads to mean "this record originated in Tally." Mirrors the "stock_reconciliation"
# synthetic entity_type equipment.py already uses for the same reason.
EQUIPMENT_REVERSE_WATCH_ENTITY = "equipment_reverse_watch"

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
from ..queue.lock_manager import LockManager


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


def _resolve_price_and_total(rate: Any, amount: Any, qty: int) -> "tuple[float, float]":
    """
    Derives a consistent (price, total_price) pair from Tally's per-item RATE/AMOUNT
    fields. Confirmed live: a "Sales" voucher's inventory lines carry AMOUNT but leave
    RATE blank (unlike "Sales Order" lines, which always populate both) — parsing RATE
    alone left price=0 for every invoice item pushed from a Sales voucher. RentAsst's own
    item-creation endpoints then compound this: invoice items always recompute
    total_price server-side as quantity*price (discarding whatever total_price the
    client sends), and rent items derive their total_price from price*quantity*duration
    — either way, a zero price here becomes a zero total on the RentAsst side no matter
    what total_price value is sent, so price must never come out 0 while a real amount
    exists.
    """
    price = _parse_leading_number(rate)
    total_price = _parse_leading_number(amount)
    if not price and qty:
        price = round(total_price / qty, 2) if total_price else 0.0
    if not total_price:
        total_price = round(price * qty, 2)
    return price, total_price


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
        clean_mobile = _synthetic_mobile_number(party_name)
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
            # A matching integration_key used to return True immediately here, before
            # ever reaching the check_exists_in_rentasst staleness check below — since
            # save_mapping() always sets integration_key on create, that made this the
            # path virtually every already-mapped voucher took, and the self-heal
            # logic below effectively never ran in practice. Resolve ra_id from
            # EITHER source first, then verify existence once, so a genuinely deleted
            # RentAsst record still gets self-healed regardless of which lookup found
            # its mapping (confirmed live: rentout 9's mapping was only found via
            # integration_key, never get_rentasst_id, so it kept being treated as
            # "still exists" forever with no self-heal).
            rev_key = generate_integration_key("default", ent, tally_guid, "reverse")
            key_match = store.find_by_integration_key(rev_key)
            ra_id = store.get_rentasst_id(ent, tally_guid) or ((key_match or {}).get("target_id") if key_match else None)
            if not ra_id:
                continue
            if ra_client and not ra_client.check_exists_in_rentasst(ent, ra_id):
                log_event("ReverseSync", f"Record Tally GUID {tally_guid} ('{v_no}') exists in middleware DB but was deleted in RentAsst. Resyncing...")
                # store.delete() matches its own mapping table's rentasst_id/source_id
                # columns — for every reverse-sync-created voucher mapping those are
                # always tally_guid (save_mapping is always called with
                # source_id=tally_guid throughout this module), never ra_id (the
                # RentAsst-side target_id). Passing ra_id here matched zero rows,
                # silently no-opping the delete — the stale mapping was never actually
                # dropped, so is_tally_voucher_duplicate kept re-discovering and
                # re-reporting the same "deleted in RentAsst, resyncing" state forever
                # without ever letting a fresh create through.
                store.delete(ent, tally_guid)
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

        elif v_type in ("sales order", "sales orders", "order", "orders", "rental order", "rental orders", "rentasst sales"):
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
    lock_mgr = LockManager(store.db_path)
    worker_id = f"reverse-thread-{threading.get_ident()}"

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

            # Reverse-sync-vs-reverse-sync guard: a second overlapping reverse sync run
            # (e.g. the scheduler firing while a manual trigger is still in progress)
            # could independently process this exact Tally customer at the same time —
            # both reading "no mapping yet" and both deciding to create it, producing a
            # duplicate RentAsst customer. Reserve this identifier for the whole of this
            # customer's decision + action (not just the create call itself), since the
            # create-vs-update decision reads mapping state that must not be read
            # concurrently with another worker's write of it. A racing worker that loses
            # the acquire skips this cycle; by the next cycle the winner's mapping
            # already exists, so the loser's own lookup then correctly finds it and
            # takes the update path instead of creating a duplicate.
            record_lock_key = lock_mgr.generate_lock_key("default", "customer", "reverse-self", cust_name)
            if not lock_mgr.acquire_lock(record_lock_key, worker_id, lease_seconds=120):
                stats["skipped"] += 1
                continue

            mobile_number = _extract_primary_mobile(l, cust_name)
            has_real_mobile = _has_real_tally_mobile(l)
            address_payload = _extract_address_payload(l)
            email = l.get("email") or ""
            gst_number = l.get("gstin") or ""
            current_hash = _customer_contact_hash(mobile_number, email, gst_number, address_payload)

            # Resolve which RentAsst customer this Tally ledger already maps to, if any —
            # via a mapping this reverse sync saved earlier (find_mapping matches by
            # source_id, which a prior reverse-sync create/update sets to the Tally
            # ledger name — get_rentasst_id/find_by_target do NOT work here since they
            # match by target_id/external_id, which hold the RentAsst id, not the Tally
            # name) or, failing that, by name against RentAsst's own live customer list
            # (covers a customer that already exists there from before this mapping
            # tracking, or was created outside this middleware).
            #
            # Either way, a match used to mean "skip forever, no matter what changes in
            # Tally afterward" — confirmed live: a customer's mobile number, GST number,
            # and address never reached RentAsst even after being corrected/added in
            # Tally well after the customer's first sync, because both paths returned
            # unconditionally the moment a match was found, before any field was ever
            # compared against what was last pushed.
            existing_mapping = store.find_mapping("customer", cust_name)
            ra_id = (existing_mapping or {}).get("target_id") or (existing_mapping or {}).get("external_id")

            customer_existence_check_failed = False
            if not ra_id:
                try:
                    cloud_custs = ra_client.fetch_customers()
                    if isinstance(cloud_custs, list):
                        for c in cloud_custs:
                            if _normalize_name_for_match(c.get("name")) == _normalize_name_for_match(cust_name):
                                ra_id = str(c.get("id"))
                                break
                    else:
                        customer_existence_check_failed = True
                except Exception as e:
                    # Same class of bug already fixed for equipment (confirmed live:
                    # transient RentAsst API failures produced real duplicate assets) —
                    # a failed existence check here must NOT fall through to create,
                    # since the customer may already exist in RentAsst under this exact
                    # name. Skip this cycle instead; it's retried next cycle once the
                    # API is reachable again.
                    customer_existence_check_failed = True
                    log_event(
                        "ReverseSync",
                        f"Could not verify whether customer '{cust_name}' already exists in "
                        f"RentAsst (will retry next cycle instead of risking a duplicate create): {e}",
                    )

            if not ra_id and customer_existence_check_failed:
                lock_mgr.release_lock(record_lock_key, worker_id)
                stats["skipped"] += 1
                continue

            # A mapped RentAsst id can go stale (record deleted / DB reset on the RentAsst
            # side) — confirmed live: PUT /customer/{id} against a deleted id 404s forever
            # with no self-healing. Drop the stale mapping and fall through to the create
            # path below, exactly like a genuinely new customer.
            if ra_id and not ra_client.check_exists_in_rentasst("customer", ra_id):
                log_event(
                    "ReverseSync",
                    f"RentAsst customer {ra_id} (mapped to Tally party '{cust_name}') no longer "
                    "exists — dropping stale mapping and re-creating.",
                )
                store.delete("customer", cust_name)
                existing_mapping = None
                ra_id = None

            if ra_id:
                stored_hash = (existing_mapping or {}).get("last_synced_hash") or (existing_mapping or {}).get("last_hash")
                if stored_hash == current_hash:
                    lock_mgr.release_lock(record_lock_key, worker_id)
                    stats["skipped"] += 1
                    continue
                stats["processed"] += 1
                try:
                    # PUT /customer/{id} validates the FULL record (confirmed live: omitting
                    # 'name' 422s with "The name field is required") even though only
                    # contact/GST fields are actually changing here. RentAsst's own field for
                    # GST is 'customer_gst_number' — 'gst_number' is silently ignored
                    # (confirmed live). Address is handled separately below — an embedded
                    # 'address' key here does nothing on this endpoint.
                    update_payload = {
                        "name": cust_name,
                        "email": email,
                        "customer_gst_number": gst_number,
                    }
                    if has_real_mobile:
                        update_payload["mobile"] = mobile_number
                    else:
                        # RentAsst's PUT /customer/{id} validates the full record —
                        # 'mobile' is required, omitting it 422s (confirmed live) — so
                        # when Tally has no real mobile, read back and resend RentAsst's
                        # OWN current value unchanged rather than the synthetic
                        # placeholder, which would otherwise overwrite a real mobile the
                        # customer already has on file.
                        try:
                            current_detail = ra_client.get_customer(ra_id) or {}
                        except Exception:
                            current_detail = {}
                        update_payload["mobile"] = current_detail.get("mobile") or mobile_number
                    ra_client.update_customer(ra_id, update_payload)
                    if address_payload is not None:
                        _push_customer_address(ra_client, ra_id, address_payload)
                    store.save_mapping(
                        entity_type="customer",
                        source_id=cust_name,
                        target_id=ra_id,
                        source_system="tally",
                        target_system="rentasst",
                        last_synced_hash=current_hash,
                    )
                    store.add_history(
                        "customer", ra_id, "synced", external_id=cust_name,
                        details="Tally Customer Reverse Sync (updated contact/GST/address)",
                    )
                    stats["updated"] += 1
                except Exception as e:
                    stats["failed"] += 1
                    log_event("ReverseSync", f"Failed to update Tally customer '{cust_name}' in RentAsst: {e}")
                lock_mgr.release_lock(record_lock_key, worker_id)
                continue

            stats["processed"] += 1
            try:
                # RentAsst's customer create endpoint also silently ignores an embedded
                # 'address' key (confirmed live) — pushed separately below, once the
                # customer id exists. GST uses the same 'customer_gst_number' field as
                # update (confirmed live: 'gst_number' is a no-op here too).
                create_payload = {
                    "name": cust_name,
                    "company_name": cust_name,
                    "mobile": mobile_number,
                    "email": email,
                    "customer_gst_number": gst_number,
                }

                res = ra_client.push_customer(create_payload)
                ra_id = str(res.get("id") or f"RA-CUST-{l_alter_id}")
                # Reserve this brand-new RentAsst id under forward sync's OWN lock key
                # (same company/entity_type/"forward"/item_id scheme app/sync/base.py
                # uses) immediately, before any further network calls — a concurrently
                # running forward sync that fetches this record before our mapping row
                # is saved would otherwise treat it as RentAsst-native (no mapping yet
                # found) and push it right back into Tally as a duplicate. Holding this
                # lock through save_mapping makes forward sync's own acquire_lock() for
                # the same id fail and skip that item for this cycle instead.
                xdir_lock_key = lock_mgr.generate_lock_key("default", "customer", "forward", ra_id)
                lock_mgr.acquire_lock(xdir_lock_key, worker_id, lease_seconds=120)
                try:
                    if address_payload is not None:
                        _push_customer_address(ra_client, ra_id, address_payload)
                    store.save_mapping(
                        entity_type="customer",
                        source_id=cust_name,
                        target_id=ra_id,
                        source_system="tally",
                        target_system="rentasst",
                        last_synced_hash=current_hash,
                    )
                    store.add_history("customer", ra_id, "synced", external_id=cust_name, details="Tally Customer Reverse Sync")
                    stats["created"] += 1
                finally:
                    lock_mgr.release_lock(xdir_lock_key, worker_id)
            except Exception as e:
                stats["failed"] += 1
                log_event("ReverseSync", f"Failed to push Tally customer '{cust_name}': {e}")
            finally:
                lock_mgr.release_lock(record_lock_key, worker_id)

        # 2. Reverse sync new Assets / Equipment (Stock Items) from Tally to RentAsst
        stock_items = fetcher.fetch_stock_items(last_alter_id=last_alter_id)
        for s in stock_items:
            item_name = s.get("name") or ""
            parent_category = s.get("parent") or ""
            unit_name = s.get("unit") or ""
            hsn_code = s.get("hsn_code") or ""
            gst_rate = s.get("gst_rate") or 0.0
            quantity = s.get("quantity") or 0.0
            rent_price = s.get("rent_price") or 0.0
            description = s.get("description") or ""
            s_alter_id = s.get("alter_id") or 0
            if s_alter_id > max_alter_id:
                max_alter_id = s_alter_id

            if not item_name:
                continue

            # Reverse-sync-vs-reverse-sync guard: see the matching comment on the
            # customer loop above — a second overlapping reverse sync run could
            # independently process this exact Tally stock item at the same time and
            # both decide to create it. Reserve this identifier for the whole of this
            # item's decision + action, released at every exit point below.
            record_lock_key = lock_mgr.generate_lock_key("default", "equipment", "reverse-self", item_name)
            if not lock_mgr.acquire_lock(record_lock_key, worker_id, lease_seconds=120):
                stats["skipped"] += 1
                continue

            current_hash = _equipment_change_hash(
                hsn_code, gst_rate, quantity, parent_category, unit_name, rent_price, description,
            )

            # Resolve which RentAsst asset this Tally stock item already maps to, if any —
            # via a mapping this reverse sync saved earlier (find_mapping matches by
            # source_id, which a prior reverse-sync create/update sets to the Tally item
            # name — get_rentasst_id/find_by_target do NOT work here for the same reason
            # confirmed live for the equivalent customer lookup: they match by target_id/
            # external_id, which hold the RentAsst id, not the Tally name) or, failing
            # that, by name against RentAsst's own live equipment list (covers an asset
            # that already exists there from before this mapping tracking existed).
            existing_mapping = store.find_mapping("equipment", item_name)
            ra_id = (existing_mapping or {}).get("target_id") or (existing_mapping or {}).get("external_id")
            # True when ra_id was resolved only by a live name-match against RentAsst's
            # own asset list, never via a mapping this reverse sync created itself — i.e.
            # a RentAsst-native (forward-sync-owned) asset that happens to share this
            # Tally item's name, as opposed to one this loop's own create/update path
            # actually produced.
            forward_owned = False
            existence_check_failed = False

            if not ra_id:
                try:
                    cloud_assets = ra_client.fetch_equipment()
                    if isinstance(cloud_assets, list):
                        for a in cloud_assets:
                            if _normalize_name_for_match(a.get("name")) == _normalize_name_for_match(item_name):
                                ra_id = str(a.get("id"))
                                break
                    else:
                        existence_check_failed = True
                except Exception as e:
                    # Confirmed live: RentAsst's local API is not always reachable
                    # (transient timeouts/connection resets). A failed existence check
                    # must NEVER silently fall through to the create path below — that
                    # created real duplicate assets in RentAsst (e.g. "Dell Laptop"
                    # ended up as both id=1 and a newer id=18) every time this call
                    # happened to fail while the asset genuinely already existed.
                    existence_check_failed = True
                    log_event(
                        "ReverseSync",
                        f"Could not verify whether '{item_name}' already exists in RentAsst "
                        f"({e}) — skipping this cycle instead of risking a duplicate create.",
                    )
                if ra_id:
                    forward_owned = True

            if not ra_id and existence_check_failed:
                lock_mgr.release_lock(record_lock_key, worker_id)
                stats["skipped"] += 1
                continue

            # A mapped RentAsst id can go stale (record deleted / DB reset on the
            # RentAsst side) — same reused-id collision confirmed live for customers. Runs
            # regardless of ownership: if either kind of asset was deleted from RentAsst,
            # Tally's copy should still be free to create a fresh one.
            if ra_id and not ra_client.check_exists_in_rentasst("equipment", ra_id):
                log_event(
                    "ReverseSync",
                    f"RentAsst asset {ra_id} (mapped to Tally item '{item_name}') no longer "
                    "exists — dropping stale mapping and re-creating.",
                )
                if not forward_owned:
                    store.delete("equipment", item_name)
                existing_mapping = None
                ra_id = None
                forward_owned = False

            category_id = ra_client.resolve_category_id(parent_category) if parent_category else None
            unit_id = ra_client.resolve_unit_id(unit_name) if unit_name else None
            qty_int = int(quantity)
            # RentAsst's asset update endpoint requires a non-empty 'branch' array just to
            # pass validation ("Please select branch and quantity") — confirmed live the
            # array's own contents aren't what persists the quantity (available_quantity
            # is), but the key must be present and non-empty regardless. branch_id 1 is
            # this business's only branch (confirmed live against the one real asset,
            # 'Dell Laptop', whose own branch record uses branch_id 1 — matches Tally's own
            # single-godown company setup too).
            branch_payload = [{"branch_id": 1, "quantity": qty_int}]

            if ra_id and forward_owned:
                # A RentAsst-native asset — GST/HSN/rent-price/description edits made in
                # Tally must still reach it (confirmed live this is exactly the "existing
                # record changed, must sync, not skip" case for an asset RentAsst itself
                # created), but this loop must never touch quantity/skip_inventory/
                # category/unit for it: quantity here is governed by the existing
                # RentAsst -> Tally stock reconciliation, not this direction, and forcing
                # skip_inventory (the old behavior) 500s with "Asset has inventory
                # history. Archive stock first before disabling inventory tracking." the
                # moment the asset has any real rental history — confirmed live.
                #
                # The per-item hash is tracked under a SEPARATE synthetic entity_type
                # (never "equipment") — confirmed live that writing a source_system="tally"
                # row under entity_type="equipment" here is exactly the shape
                # run_sync_pipeline's forward-sync guard (app/sync/base.py) reads as "this
                # record originated in Tally, never forward-sync it again", which silently
                # blackholed ALL future forward-sync updates for this same asset.
                watch_mapping = store.find_mapping(EQUIPMENT_REVERSE_WATCH_ENTITY, item_name)
                stored_hash = (watch_mapping or {}).get("last_synced_hash")
                if stored_hash == current_hash:
                    lock_mgr.release_lock(record_lock_key, worker_id)
                    stats["skipped"] += 1
                    continue
                stats["processed"] += 1
                try:
                    current_detail = ra_client.get_equipment(ra_id) or {}
                    preserved_branch = [
                        {"branch_id": b.get("branch_id"), "quantity": b.get("quantity")}
                        for b in (current_detail.get("branch") or [])
                        if b.get("branch_id") is not None
                    ] or [{"branch_id": 1, "quantity": current_detail.get("available_quantity") or 0}]
                    update_payload = {
                        "name": item_name,
                        "calculation_method": current_detail.get("calculation_methods") or "[1]",
                        "hsn_code": hsn_code,
                        "gst_rate": gst_rate if gst_rate > 0 else None,
                        "description": description,
                        "rent_price": f"{rent_price:.2f}",
                        "day_based_rent_price": f"{rent_price:.2f}",
                        "skip_inventory": current_detail.get("skip_inventory", False),
                        "available_quantity": current_detail.get("available_quantity") or 0,
                        "branch": preserved_branch,
                        "enabled_for_rent": current_detail.get("enabled_for_rent", True),
                        "unit_id": current_detail.get("unit_id"),
                        "category_id": current_detail.get("category_id"),
                        "category_ids": (
                            json.dumps(current_detail.get("category_ids"))
                            if current_detail.get("category_ids") else None
                        ),
                    }
                    ra_client.update_equipment(ra_id, update_payload)
                    store.save_mapping(
                        entity_type=EQUIPMENT_REVERSE_WATCH_ENTITY,
                        source_id=item_name,
                        target_id=ra_id,
                        source_system="tally",
                        target_system="rentasst",
                        last_synced_hash=current_hash,
                    )
                    store.add_history(
                        "equipment", ra_id, "synced", external_id=item_name,
                        details="Tally Stock Item Reverse Sync (updated GST/HSN/rent price on a RentAsst-native asset)",
                    )
                    stats["updated"] += 1
                except Exception as e:
                    stats["failed"] += 1
                    log_event("ReverseSync", f"Failed to update Tally stock item '{item_name}' in RentAsst: {e}")
                lock_mgr.release_lock(record_lock_key, worker_id)
                continue

            if ra_id:
                stored_hash = (existing_mapping or {}).get("last_synced_hash") or (existing_mapping or {}).get("last_hash")
                if stored_hash == current_hash:
                    lock_mgr.release_lock(record_lock_key, worker_id)
                    stats["skipped"] += 1
                    continue
                stats["processed"] += 1
                try:
                    # Never hardcode skip_inventory here — confirmed live this is not
                    # just a "genuinely reverse-owned" asset in principle, a stale/
                    # mislabeled entity_type="equipment" mapping row (e.g. from an older
                    # version of this code, or any other cause) can route a RentAsst-
                    # native asset with real rental history through this exact branch,
                    # and forcing True on one that's currently False 500s with "Asset has
                    # inventory history. Archive stock first before disabling inventory
                    # tracking." Reading the asset's own current value back is always
                    # safe: a genuinely Tally-created asset already has it True from its
                    # own push_equipment call below, so this is a no-op for that case.
                    current_skip_inventory = True
                    try:
                        current_detail = ra_client.get_equipment(ra_id) or {}
                        current_skip_inventory = current_detail.get("skip_inventory", True)
                    except Exception:
                        pass
                    update_payload = {
                        "name": item_name,
                        "calculation_method": "[1]",
                        "hsn_code": hsn_code,
                        "gst_rate": gst_rate if gst_rate > 0 else None,
                        "unit_id": unit_id,
                        "category_id": category_id,
                        "category_ids": json.dumps([category_id]) if category_id else None,
                        "skip_inventory": current_skip_inventory,
                        "enabled_for_rent": True,
                        "description": description,
                        "available_quantity": qty_int,
                        "branch": branch_payload,
                        "rent_price": f"{rent_price:.2f}",
                        "day_based_rent_price": f"{rent_price:.2f}",
                    }
                    ra_client.update_equipment(ra_id, update_payload)
                    store.save_mapping(
                        entity_type="equipment",
                        source_id=item_name,
                        target_id=ra_id,
                        source_system="tally",
                        target_system="rentasst",
                        last_synced_hash=current_hash,
                    )
                    store.add_history(
                        "equipment", ra_id, "synced", external_id=item_name,
                        details="Tally Stock Item Reverse Sync (updated HSN/GST/quantity/rent price)",
                    )
                    stats["updated"] += 1
                except Exception as e:
                    stats["failed"] += 1
                    log_event("ReverseSync", f"Failed to update Tally stock item '{item_name}' in RentAsst: {e}")
                lock_mgr.release_lock(record_lock_key, worker_id)
                continue

            stats["processed"] += 1
            asset_payload = {
                "name": item_name,
                "calculation_method": "[1]",
                "rent_price": f"{rent_price:.2f}",
                "day_based_rent_price": f"{rent_price:.2f}",
                "purchase_price": "0.00",
                "hsn_code": hsn_code,
                "gst_rate": gst_rate if gst_rate > 0 else None,
                "unit_id": unit_id,
                "category_id": category_id,
                "category_ids": json.dumps([category_id]) if category_id else None,
                "skip_inventory": False,
                "enabled_for_rent": True,
                "description": description,
                "available_quantity": qty_int,
                "branch": branch_payload,
            }

            # Create brand new asset in RentAsst
            try:
                res = ra_client.push_equipment(asset_payload)
                ra_id = str(res.get("id") or f"RA-ASSET-{s_alter_id}")
                # See the matching comment on the customer create path above: reserve
                # this id under forward sync's own "forward" lock key immediately so a
                # concurrently running forward sync can't treat it as RentAsst-native
                # and push it back into Tally as a duplicate before our mapping lands.
                xdir_lock_key = lock_mgr.generate_lock_key("default", "equipment", "forward", ra_id)
                lock_mgr.acquire_lock(xdir_lock_key, worker_id, lease_seconds=120)
                try:
                    store.save_mapping(
                        entity_type="equipment",
                        source_id=item_name,
                        target_id=ra_id,
                        source_system="tally",
                        target_system="rentasst",
                        last_synced_hash=current_hash,
                    )
                    store.add_history("equipment", ra_id, "synced", external_id=item_name, details="Tally Stock Item Reverse Sync")
                    stats["created"] += 1
                finally:
                    lock_mgr.release_lock(xdir_lock_key, worker_id)
            except Exception as e:
                stats["failed"] += 1
                log_event("ReverseSync", f"Failed to push Tally stock item '{item_name}': {e}")
            finally:
                lock_mgr.release_lock(record_lock_key, worker_id)

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
            # "rentasst sales" is TallyClient.sync_rental_order's own dedicated voucher
            # type (build_sales_order_voucher_xml — see its docstring for why it's not
            # a real "Sales Order"). A forward-synced rentout is already excluded above
            # by is_own_forward_sync_voucher regardless of type, but any NEW rentout a
            # human enters directly in Tally naturally lands under this same type too,
            # since it's the only Order-like option this company's Tally now has —
            # confirmed live: a real, non-forward-synced "RentAsst Sales" voucher fell
            # through unclassified (matched neither is_invoice_type nor is_rentout_type)
            # and was silently never reverse-synced at all.
            is_rentout_type = v_type in ("sales order", "sales orders", "order", "orders", "rental order", "rental orders", "rentasst sales")

            # Invoices and rentouts get their own create-vs-update-vs-skip handling further
            # down (an already-synced record must still be checked for missing line items
            # and backfilled — see the "backfill" comments below — not skipped forever) —
            # payments keep the plain skip-on-duplicate behavior, since a receipt is never
            # revised after creation.
            if not is_invoice_type and not is_rentout_type and is_tally_voucher_duplicate(v, store, ra_client):
                stats["skipped"] += 1
                continue

            # Reverse-sync-vs-reverse-sync guard: see the matching comment on the
            # customer create path above — a second overlapping reverse sync run (the
            # scheduler firing while a manual trigger is still in progress, or two
            # scheduler workers) could independently process this exact Tally voucher
            # at the same time and both decide to create it, producing a duplicate
            # RentAsst rentout/invoice/payment for the same Tally source. tally_guid is
            # globally unique across voucher types, so one lock per voucher is enough —
            # held for this voucher's entire processing (not just the create call)
            # since the create-vs-update decision itself reads mapping state that must
            # not be read concurrently with another worker's write of it. A racing
            # worker that loses the acquire skips this cycle; by the next cycle the
            # winner's mapping already exists, so the loser's own existing_ra_id lookup
            # correctly finds it and takes the update/backfill path instead of create.
            voucher_entity_type = (
                "rental_order" if is_rentout_type
                else "invoice" if is_invoice_type
                else "payment" if v_type in ("receipt", "payment", "receipts", "payments")
                else None
            )
            record_lock_key = None
            if voucher_entity_type and tally_guid:
                record_lock_key = lock_mgr.generate_lock_key("default", voucher_entity_type, "reverse-self", tally_guid)
                if not lock_mgr.acquire_lock(record_lock_key, worker_id, lease_seconds=120):
                    stats["skipped"] += 1
                    continue

            try:
                party_name = v.get("party_name") or "Customer"
                iso_date = format_iso_date(v.get("date"))

                if v_type in ("sales order", "sales orders", "order", "orders", "rental order", "rental orders", "rentasst sales"):
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
                        price, total_price = _resolve_price_and_total(it.get("rate"), it.get("amount"), qty)
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
                            # RentAsst's RentService::calculateStandardPrice() always
                            # overwrites total_price server-side as
                            # rented_quantity*price*duration, where duration comes from
                            # matching calculation_method against 1=days/2=hours/3=months
                            # and DEFAULTS TO 0 for any other value including null/missing
                            # — confirmed live: every rentout item silently landed at
                            # total_price=0 (and the whole rentout's grand_total with it)
                            # because this field was never sent. 4 = AssetCalculationMethods
                            # ::FLAT_PRICE, which computes total_price = quantity*price with
                            # no duration multiplier — correct for a one-time Tally sale line
                            # rather than a per-day/hour/month rental rate.
                            "calculation_method": 4,
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
                            # A rentout whose item push has already failed repeatedly (see
                            # the 3-strike logic below) is skipped instantly, without a
                            # network round-trip or a fresh error log every single cycle
                            # forever. record_lock_key is released by the outer finally
                            # below regardless of which branch/continue is taken.
                            failure_tracker = store.find_mapping("rentout_backfill_failures", tally_guid)
                            if (failure_tracker or {}).get("status") == "blocked":
                                stats["skipped"] += 1
                                continue
                            try:
                                # get_rentout() hits 'get-rent-details/{id}' — confirmed live
                                # to 404 on this RentAsst deployment, unlike the sibling
                                # create-rent-details/update-rent-details endpoints which are
                                # verified real routes. Relying on it here made every single
                                # backfill attempt fail permanently (this exact rentout kept
                                # failing on every sync cycle, never actually backfilling),
                                # which is what surfaced as reverse-synced Sales Orders never
                                # getting their rent items into RentAsst. get_rent_items() —
                                # 'get-rent-items/{id}' — is a separately confirmed-working
                                # endpoint, so use its result length as the "already has
                                # items" signal instead of rent_items_count from the broken
                                # endpoint.
                                try:
                                    current_items = ra_client.get_rent_items(existing_ra_id)
                                except Exception:
                                    current_items = []
                                current_count = len(current_items) if isinstance(current_items, list) else 0
                                if not current_count:
                                    try:
                                        ra_client.push_rentout_items(existing_ra_id, resolved_items)
                                        store.add_history("rental_order", existing_ra_id, "synced", external_id=tally_guid, details="Tally Sales Order Reverse Sync — backfilled missing rent items")
                                        stats["updated"] += 1
                                    except Exception as push_err:
                                        # A rentout created before 'settings' was included on
                                        # create (below) still has a null settings column,
                                        # which crashes RentAsst's own item-creation
                                        # transaction (see DEFAULT_RENTOUT_SETTINGS). Patching
                                        # settings via update-rent-details was tried here
                                        # before, but confirmed live (against this exact
                                        # deployment) that update-rent-details unconditionally
                                        # overwrites customer_id/customer_address_id/settings
                                        # on EVERY call, and crashes with "Attempt to read
                                        # property 'customer_id' on null" for any rentout
                                        # whose OWN customer_id is already null in RentAsst's
                                        # database — reproduced against real rentouts with
                                        # every payload shape tried (with/without customer_id,
                                        # nested/flat), so for THOSE specific records this is
                                        # a RentAsst-side data/server bug no payload from here
                                        # can work around. Not every push_rentout_items()
                                        # failure is necessarily that same unrecoverable case
                                        # though (a transient network blip is still possible),
                                        # so this doesn't give up on the very first failure —
                                        # sync_version on the failures mapping counts
                                        # consecutive attempts (save_mapping auto-increments it
                                        # on every upsert to the same key) and only stops
                                        # retrying after 3 straight failures, avoiding a
                                        # forever-noisy retry loop for a truly permanent case
                                        # while still giving a one-off transient failure a
                                        # couple of cycles to recover on its own.
                                        store.save_mapping(
                                            entity_type="rentout_backfill_failures",
                                            source_id=tally_guid,
                                            target_id=existing_ra_id,
                                            source_system="tally",
                                            target_system="rentasst",
                                            status="failed",
                                        )
                                        attempts = (store.find_mapping("rentout_backfill_failures", tally_guid) or {}).get("sync_version") or 1
                                        if attempts >= 3:
                                            store.save_mapping(
                                                entity_type="rentout_backfill_failures",
                                                source_id=tally_guid,
                                                target_id=existing_ra_id,
                                                source_system="tally",
                                                target_system="rentasst",
                                                status="blocked",
                                            )
                                            log_event(
                                                "ReverseSync",
                                                f"RentAsst rentout {existing_ra_id} (Tally GUID {tally_guid}) has failed to "
                                                f"receive its rent items {attempts} sync cycles in a row ({push_err}) — likely "
                                                "a null/broken settings or customer link on this specific rentout in "
                                                "RentAsst's own database that the API can't repair (update-rent-details "
                                                "itself 500s on records in this state). Not retrying automatically anymore "
                                                "— fix this rentout directly in RentAsst.",
                                            )
                                        else:
                                            log_event(
                                                "ReverseSync",
                                                f"Failed to backfill rent items for RentAsst rentout {existing_ra_id} "
                                                f"(Tally GUID {tally_guid}), attempt {attempts}/3: {push_err}",
                                            )
                                        stats["failed"] += 1
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

                    # Reserve this brand-new RentAsst id under forward sync's own
                    # "forward" lock key immediately — see the matching comment on the
                    # customer create path above. Prevents a concurrently running
                    # forward sync from treating this rentout as RentAsst-native and
                    # pushing it right back into Tally as a duplicate voucher before
                    # our mapping row lands.
                    xdir_lock_key = lock_mgr.generate_lock_key("default", "rental_order", "forward", ra_id)
                    lock_mgr.acquire_lock(xdir_lock_key, worker_id, lease_seconds=120)
                    try:
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
                    finally:
                        lock_mgr.release_lock(xdir_lock_key, worker_id)

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
                        price, total_price = _resolve_price_and_total(it.get("rate"), it.get("amount"), qty)
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
                            # Fetch current state once, up front, and use it to decide both
                            # whether a status update is even needed and whether items still
                            # need backfilling — avoids two separate GET calls and lets us skip
                            # the update call entirely once status already matches.
                            current = None
                            try:
                                current = ra_client.get_invoice(existing_ra_id)
                            except Exception as e:
                                log_event("ReverseSync", f"Failed to fetch RentAsst invoice {existing_ra_id} (Tally GUID {tally_guid}) for reverse sync: {e}")

                            current_status = current.get("status") if isinstance(current, dict) else None

                            # RentAsst's InvoiceService::canEditInvoice() permanently locks
                            # header edits (including status) on any invoice that isn't
                            # draft/confirmed-with-no-payments — confirmed live: once an
                            # invoice reaches paid/partiallyPaid, PUT /invoices/{id} 422s with
                            # "Invoice cannot be edited..." forever. Gating on "current_status
                            # != invoice_status" alone isn't enough: invoice_status is
                            # recomputed fresh each cycle from whichever receipt vouchers
                            # happen to be in THIS run's fetch batch, so if the settling
                            # receipt falls outside this run's date range it recomputes as
                            # "confirmed" even though the invoice is actually "paid" — that
                            # mismatch re-triggers the same doomed update, and 422, every
                            # single cycle. The reliable signal is RentAsst's own edit-lock
                            # rule, not our guess at the target status — gate on a "known
                            # locked" denylist rather than an "editable" allowlist so an
                            # unknown/failed status fetch still allows the update attempt
                            # (previous behavior) instead of silently blocking it.
                            LOCKED_INVOICE_STATUSES = (
                                "partiallyPaid", "paid", "overdue", "cancelled",
                                "refunded", "partiallyRefunded", "excessPaid", "excessRefunded",
                            )
                            if current_status not in LOCKED_INVOICE_STATUSES and current_status != invoice_status:
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
                                    log_event("ReverseSync", f"RentAsst invoice {existing_ra_id} (Tally GUID {tally_guid}) status left at '{current_status}' — could not update to '{invoice_status}': {e}")

                            # Backfill: an invoice created before push_invoice_items() existed
                            # (or whose item push failed) still has zero items — check the
                            # live item count first, not just "did we try before", so this is
                            # safe to run every sync: once items exist, this never re-pushes.
                            if resolved_items:
                                current_items = current.get("items") if isinstance(current, dict) else None
                                if not current_items:
                                    try:
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

                    # Reserve this brand-new RentAsst id under forward sync's own
                    # "forward" lock key immediately — see the matching comment on the
                    # customer create path above. Prevents a concurrently running
                    # forward sync from treating this invoice as RentAsst-native and
                    # pushing it right back into Tally as a duplicate before our
                    # mapping row lands.
                    xdir_lock_key = lock_mgr.generate_lock_key("default", "invoice", "forward", ra_id)
                    lock_mgr.acquire_lock(xdir_lock_key, worker_id, lease_seconds=120)
                    try:
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
                    finally:
                        lock_mgr.release_lock(xdir_lock_key, worker_id)

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
                                # Invoice numbers alone aren't unique across customers —
                                # GST invoice numbering commonly resets per financial year,
                                # so the same number can legitimately belong to a different
                                # customer's (often older) invoice. Matching on number alone
                                # silently misfiled a payment against the wrong customer's
                                # invoice/outstanding balance whenever numbers collided.
                                # Require the invoice's own customer name to match this
                                # receipt's Tally party too.
                                candidates = []
                                for inv in inv_list:
                                    inv_num = str(inv.get("number") or "").strip()
                                    if inv_num and inv_num.lower() == bill_ref.lower() and inv.get("id"):
                                        candidates.append(inv)
                                if len(candidates) == 1:
                                    invoice_id = int(candidates[0]["id"])
                                elif len(candidates) > 1 and party_name:
                                    for inv in candidates:
                                        inv_cust = ((inv.get("customer") or {}).get("name") or "").strip()
                                        if inv_cust and inv_cust.lower() == party_name.strip().lower():
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

                    # Reserve this brand-new RentAsst id under forward sync's own
                    # "forward" lock key immediately — see the matching comment on the
                    # customer create path above. Prevents a concurrently running
                    # forward sync from treating this payment as RentAsst-native and
                    # pushing it right back into Tally as a duplicate before our
                    # mapping row lands.
                    xdir_lock_key = lock_mgr.generate_lock_key("default", "payment", "forward", ra_id)
                    lock_mgr.acquire_lock(xdir_lock_key, worker_id, lease_seconds=120)
                    try:
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
                    finally:
                        lock_mgr.release_lock(xdir_lock_key, worker_id)
                else:
                    stats["skipped"] += 1

            except Exception as ex:
                stats["failed"] += 1
                error_msg = str(ex)
                log_event("ReverseSync", f"Failed to push Tally voucher {tally_guid} ({v_type}): {error_msg}")
                store.add_dead_letter("voucher", tally_guid, error_msg, json.dumps(v))
            finally:
                if record_lock_key:
                    lock_mgr.release_lock(record_lock_key, worker_id)

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
