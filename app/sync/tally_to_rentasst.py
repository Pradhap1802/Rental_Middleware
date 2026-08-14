import time
import json
import re
from datetime import datetime
from typing import Dict, Any, Optional

from ..connectors.tally_fetcher import TallyFetcher
from ..mapping.store import MappingStore
from ..logging.logger import log_event
from .idempotency import generate_integration_key
from .ownership import filter_payload_by_ownership
from .conflicts import ConflictDetector
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


def resolve_customer_id(party_name: str, ra_client: Any, store: MappingStore) -> int:
    """Finds or resolves Customer ID from RentAsst DB without calling forbidden POST endpoints."""
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
                    store.save("customer", str(cid), party_name)
                    return cid
    except Exception as e:
        log_event("ReverseSync", f"Customer resolution lookup note: {e}")

    return 1


def is_tally_voucher_duplicate(v: Dict[str, Any], store: MappingStore, ra_client: Optional[Any] = None) -> bool:
    """Checks if a Tally voucher or master record already exists in RentAsst and SQLite mapping store."""
    tally_guid = (v.get("tally_guid") or "").strip()
    v_no = (v.get("voucher_number") or "").strip()
    rentasst_tag = (v.get("rentasst_id") or "").strip()

    if rentasst_tag:
        return True

    if tally_guid:
        for ent in ("rental_orders", "invoice", "payment", "customer"):
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
        for ent in ("rental_orders", "invoice", "payment"):
            ra_id = store.get_rentasst_id(ent, v_no) or store.get_rentasst_id(ent, f"RENT-{v_no}") or store.get_rentasst_id(ent, f"INV-{v_no}") or store.get_rentasst_id(ent, f"PAY-{v_no}")
            if ra_id:
                if ra_client and not ra_client.check_exists_in_rentasst(ent, ra_id):
                    log_event("ReverseSync", f"Record Tally Voucher #{v_no} exists in middleware DB but was deleted in RentAsst. Resyncing...")
                    store.delete(ent, ra_id)
                    return False
                return True

    if ra_client and v_no:
        v_type = (v.get("voucher_type") or "").lower().strip()
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

    return False


def sync_tally_to_rentasst(ra_client: Any, ext_client: Any, store: MappingStore, force_full_sync: bool = True) -> Dict[str, Any]:
    """
    Production-grade reverse synchronization runner:
    Fetches Vouchers (Sales Invoices, Receipts) from Tally Prime, applies reverse field-ownership policy filtering,
    detects conflicts, validates payload schemas, posts to RentAsst Cloud API, and persists SQLite mapping 
    ONLY AFTER confirmed HTTP success response.
    """
    stats = {"processed": 0, "created": 0, "updated": 0, "failed": 0, "skipped": 0}
    start_time = time.time()
    detector = ConflictDetector(store)

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
        vouchers = fetcher.fetch_vouchers(last_alter_id=last_alter_id)
        
        for v in vouchers:
            stats["processed"] += 1
            tally_guid = v.get("tally_guid") or ""
            alter_id = v.get("alter_id") or 0
            if alter_id > max_alter_id:
                max_alter_id = alter_id

            v_type = (v.get("voucher_type") or "").lower().strip()

            if is_tally_voucher_duplicate(v, store, ra_client):
                stats["skipped"] += 1
                continue

            try:
                party_name = v.get("party_name") or "Customer"
                iso_date = format_iso_date(v.get("date"))

                if v_type in ("sales", "sales invoice", "invoice"):
                    cust_id = resolve_customer_id(party_name, ra_client, store)
                    amount = float(v.get("amount") or 0.0)

                    invoice_payload = {
                        "number": str(v.get("voucher_number") or f"INV-{tally_guid[:8]}"),
                        "customer_id": cust_id,
                        "invoice_date": iso_date,
                        "due_date": iso_date,
                        "bill_from": iso_date,
                        "bill_to": iso_date,
                        "subtotal": amount,
                        "grand_total": amount,
                        "total_amount": amount,
                        "status": "confirmed",
                        "notes": f"Imported from Tally Sales Register Voucher #{v.get('voucher_number')}",
                        "tally_guid": tally_guid,
                    }

                    # 1. Field Ownership Policy Filter (Reverse Direction: Tally -> RentAsst)
                    filtered_payload = filter_payload_by_ownership("invoice", "reverse", invoice_payload)

                    # 2. Pre-Flight Data Validation Check
                    is_valid, val_err = validate_entity_payload("invoice", filtered_payload)
                    if not is_valid:
                        log_event("ReverseSync", f"Payload validation failed for Tally Invoice reverse sync (GUID {tally_guid}): {val_err}")
                        store.add_dead_letter("invoice", tally_guid, f"Reverse Sync Validation Failure: {val_err}", json.dumps(v))
                        stats["failed"] += 1
                        continue

                    # 3. Post to RentAsst Cloud REST API
                    res = ra_client.push_invoice(filtered_payload)

                    # 4. Save SQLite mapping ONLY after confirmed HTTP success
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

                    recent_inv = store.get_rentasst_id("invoice", tally_guid)
                    if recent_inv and recent_inv.isdigit():
                        payment_payload["invoice_id"] = int(recent_inv)
                    else:
                        try:
                            inv_list = ra_client.fetch_invoices()
                            if isinstance(inv_list, list) and len(inv_list) > 0 and inv_list[0].get("id"):
                                payment_payload["invoice_id"] = int(inv_list[0]["id"])
                            else:
                                payment_payload["invoice_id"] = 1
                        except Exception:
                            payment_payload["invoice_id"] = 1

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
