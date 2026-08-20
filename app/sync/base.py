import time
import hashlib
import json
from datetime import datetime
from typing import Dict, Any, List, Callable, Optional
from ..mapping.store import MappingStore
from ..logging.logger import log_event, log_sync_event


def compute_payload_hash(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def filter_by_date_range(
    items: List[Dict[str, Any]],
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Filters RentAsst records by their created_at (falling back to updated_at) timestamp.
    Records with no parseable timestamp are kept, since RentAsst's own API doesn't filter
    by record timestamp server-side — this is a best-effort client-side narrowing, not a
    guarantee, so failing open avoids silently dropping records we can't classify.
    """
    if not from_date and not to_date:
        return items

    from_dt = _parse_date_boundary(from_date)
    to_dt = _parse_date_boundary(to_date, end_of_day=True)

    filtered = []
    for item in items:
        raw_ts = item.get("updated_at") or item.get("created_at")
        if not raw_ts:
            filtered.append(item)
            continue
        item_dt = _parse_date_boundary(raw_ts)
        if item_dt is None:
            filtered.append(item)
            continue
        if from_dt and item_dt < from_dt:
            continue
        if to_dt and item_dt > to_dt:
            continue
        filtered.append(item)
    return filtered


def _parse_date_boundary(value: Optional[str], end_of_day: bool = False) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip().replace("Z", "").split("+")[0].split(".")[0]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
            if end_of_day and fmt == "%Y-%m-%d":
                dt = dt.replace(hour=23, minute=59, second=59)
            return dt
        except ValueError:
            continue
    return None


def extract_identifier(entity_type: str, item: Dict[str, Any]) -> str:
    """
    Returns the identity used to probe the target system for timeout-recovery
    (base.py's "was this already created by a previous, timed-out attempt" check).

    For voucher-based entities this MUST be the same deterministic RENTAL-XXX-{id}
    marker that sync_rental_order/sync_invoice/sync_payment stamp into REMOTEID/
    NARRATION (see tally/client.py) — not the human-facing business number. A bare
    number like "26" is a generic short string that produces false-positive
    substring matches against unrelated MASTERID/NARRATION text elsewhere in the
    Tally company's voucher export, wrongly marking a never-created record as
    already synced (confirmed live).
    """
    ent = (entity_type or "").lower().strip()
    if ent in ("customer", "customers"):
        return str(item.get("name") or item.get("business_name") or "").strip()
    elif ent in ("equipment", "product", "products"):
        return str(item.get("name") or "").strip()
    elif ent in ("rental_orders", "rental_order", "order", "orders"):
        return f"RENTAL-ORD-{item.get('id')}"
    elif ent in ("invoices", "invoice"):
        return f"RENTAL-INV-{item.get('id')}"
    elif ent in ("payments", "payment"):
        return f"RENTAL-PAY-{item.get('id')}"
    return ""


import threading
from .idempotency import generate_integration_key, check_target_system_record_exists
from .conflicts import ConflictDetector
from ..queue.lock_manager import LockManager
from ..validation.validator import validate_entity_payload
from .dependencies import DependencyResolver, MissingDependencyException


def detect_customer_conflicts(
    item: Dict[str, Any],
    external_client: Any,
    store: MappingStore,
    company_id: str = "default",
) -> None:
    """
    Before Altering an existing Tally customer ledger with RentAsst's data, checks
    whether the ledger's mobile/email/GST fields were independently edited directly
    in Tally since our last push. Any field where both sides carry a different,
    non-empty value is recorded as an OPEN conflict via ConflictDetector — informational
    only, RentAsst's value still wins the Alter as before (unchanged behavior); this
    just makes the divergence visible instead of a silent overwrite.
    """
    name = (item.get("name") or item.get("business_name") or "").strip()
    if not name or not hasattr(external_client, "fetch_ledger_snapshot"):
        return
    try:
        tally_snapshot = external_client.fetch_ledger_snapshot(name)
    except Exception:
        tally_snapshot = None
    if not tally_snapshot:
        return

    rentasst_data = {
        "mobile": str(item.get("mobile") or item.get("phone") or "").strip(),
        "email": str(item.get("email") or "").strip(),
        "gst_number": str(item.get("customer_gst_number") or item.get("gst_number") or "").strip(),
    }
    try:
        ConflictDetector(store).detect_and_record_conflicts(
            entity_type="customer",
            entity_id=name,
            rentasst_data=rentasst_data,
            tally_data=tally_snapshot,
            company_id=company_id,
        )
    except Exception as e:
        log_event("Conflict", f"Customer conflict detection note for '{name}': {e}")


def run_sync_pipeline(
    entity_type: str,
    fetch_func: Callable[[], List[Dict[str, Any]]],
    sync_func: Callable[[Dict[str, Any]], str],
    store: MappingStore,
    external_client: Optional[Any] = None,
    ra_client: Optional[Any] = None,
    source_company_id: str = "default",
    target_company_id: str = "default",
    batch_size: int = 100,
) -> Dict[str, Any]:
    """
    Generic resilient synchronization pipeline runner with chunked batching, 
    pre-flight data validation, dependency checking, deterministic integration key idempotency, 
    target system timeout recovery, record-level lock concurrency protection, and dead-letter queueing.
    """
    stats = {"processed": 0, "created": 0, "updated": 0, "failed": 0, "skipped": 0}
    start_time = time.time()
    lock_mgr = LockManager(store.db_path)
    worker_id = f"thread-{threading.get_ident()}"

    try:
        items = fetch_func()
        total_count = len(items)

        for i in range(0, total_count, batch_size):
            batch = items[i : i + batch_size]
            # High-performance batch prefetching into in-memory TTL cache
            batch_ids = [str(it.get("id")) for it in batch if it.get("id")]
            store.prefetch_mappings(entity_type, batch_ids, source_company_id=source_company_id)

            for item in batch:
                stats["processed"] += 1
                item_id = str(item.get("id"))
                payload_hash = compute_payload_hash(item)
                identifier = extract_identifier(entity_type, item)
                integration_key = generate_integration_key(
                    source_company=source_company_id,
                    entity_type=entity_type,
                    source_id=item_id,
                    sync_direction="forward",
                )

                # Concurrency Protection: Acquire Record-Level Lock
                lock_key = lock_mgr.generate_lock_key(source_company_id, entity_type, "forward", item_id)
                if not lock_mgr.acquire_lock(lock_key, worker_id, lease_seconds=300):
                    log_event(
                        "Concurrency",
                        f"Item '{lock_key}' is currently locked by another active worker. Skipping concurrent execution.",
                    )
                    stats["skipped"] += 1
                    continue

                try:
                    # 1. Pre-Flight Data Validation Check (Task 10)
                    is_valid, val_err = validate_entity_payload(entity_type, item)
                    if not is_valid:
                        log_event("Validation", f"Payload validation failed for {entity_type} #{item_id}: {val_err}")
                        store.add_history(entity_type, item_id, "failed", details=f"Validation Failure: {val_err}")
                        store.add_dead_letter(
                            entity_type=entity_type,
                            source_id=item_id,
                            error=f"Validation Failure: {val_err}",
                            payload=json.dumps(item),
                            company_id=source_company_id,
                            error_type="ValidationError",
                        )
                        stats["failed"] += 1
                        continue

                    # 2. Dependency Resolution Check (Task 11)
                    has_deps, missing_reason, missing_ent, missing_id = DependencyResolver.check_dependencies(
                        entity_type=entity_type,
                        data=item,
                        store=store,
                        source_company_id=source_company_id,
                    )
                    if not has_deps:
                        log_event("Dependencies", f"Dependency check failed for {entity_type} #{item_id}: {missing_reason}")
                        raise MissingDependencyException(missing_reason, missing_entity=missing_ent, missing_id=missing_id)

                    # 3. Integration Key & Content Hash Deduplication Check
                    existing_key_mapping = store.find_by_integration_key(integration_key)
                    if existing_key_mapping and store.is_duplicate(entity_type, item_id, payload_hash, source_company_id=source_company_id):
                        # Master data (equipment/customer) can be deleted or reset directly in Tally
                        # after we last synced it — the content hash alone can't detect that, since
                        # the RentAsst-side record hasn't changed. Re-verify it's still actually there
                        # before trusting the skip; otherwise a Tally-side reset permanently strands
                        # the record as "synced" and every dependent voucher fails forever (confirmed
                        # live: "Moto G45" stayed skipped while Tally reported it did not exist).
                        target_still_exists = True
                        if (
                            entity_type.lower() in ("equipment", "product", "products", "customer", "customers")
                            and identifier
                            and external_client
                            and hasattr(external_client, "check_exists_in_tally")
                        ):
                            try:
                                target_still_exists = external_client.check_exists_in_tally(entity_type.lower(), identifier)
                            except Exception:
                                target_still_exists = True  # fail open: don't force a re-sync storm on a transient Tally error

                        if target_still_exists:
                            stats["skipped"] += 1
                            continue

                        log_event(
                            "Idempotency",
                            f"Record '{identifier}' for entity '{entity_type}' #{item_id} was marked synced but no longer exists in Tally — re-syncing instead of skipping.",
                        )


                    # 4. Timeout Recovery / Target System Pre-Check
                    # If local mapping is missing, check if record was already created in Tally during a previous timed-out attempt
                    if not existing_key_mapping and identifier:
                        if check_target_system_record_exists(
                            entity_type=entity_type,
                            identifier=identifier,
                            sync_direction="forward",
                            external_client=external_client,
                            ra_client=ra_client,
                        ):
                            target_id = identifier
                            log_event(
                                "Idempotency",
                                f"Timeout recovery: Record '{identifier}' for entity '{entity_type}' already exists in target system. Adopted target ID without duplicate creation.",
                            )
                            store.save_mapping(
                                entity_type=entity_type,
                                source_id=item_id,
                                target_id=target_id,
                                source_company_id=source_company_id,
                                target_company_id=target_company_id,
                                integration_key=integration_key,
                                last_synced_hash=payload_hash,
                                status="synced",
                            )
                            store.add_history(entity_type, item_id, "synced", external_id=target_id, details="Timeout recovery from target system")
                            stats["skipped"] += 1
                            continue

                    # 5. Create / Update Sync Execution
                    try:
                        ext_id = store.get_external_id(entity_type, item_id, source_company_id=source_company_id)

                        if ext_id and entity_type.lower() in ("customer", "customers") and external_client:
                            detect_customer_conflicts(item, external_client, store, source_company_id)

                        new_ext_id = sync_func(item)
                        final_ext_id = new_ext_id or ext_id or item_id
                        
                        store.save_mapping(
                            entity_type=entity_type,
                            source_id=item_id,
                            target_id=final_ext_id,
                            source_company_id=source_company_id,
                            target_company_id=target_company_id,
                            integration_key=integration_key,
                            last_synced_hash=payload_hash,
                            status="synced",
                        )
                        store.add_history(entity_type, item_id, "synced", external_id=final_ext_id)

                        if ext_id:
                            stats["updated"] += 1
                            log_sync_event(
                            entity_type=entity_type,
                            entity_id=item_id,
                            company_id=source_company_id,
                            direction="forward",
                            source_system="rentasst",
                            target_system="tally",
                            status="SUCCESS",
                            message=f"Successfully synced {entity_type} #{item_id} -> Tally '{final_ext_id}'",
                        )
                        else:
                            stats["created"] += 1
                    except MissingDependencyException as mde:
                        raise mde
                    except Exception as ex:
                        stats["failed"] += 1
                        error_msg = str(ex)
                        log_sync_event(
                            entity_type=entity_type,
                            entity_id=item_id,
                            company_id=source_company_id,
                            direction="forward",
                            source_system="rentasst",
                            target_system="tally",
                            status="FAILED",
                            message=f"Failed to sync {entity_type} {item_id}: {error_msg}",
                            metadata={"error": error_msg},
                        )
                        store.add_history(entity_type, item_id, "failed", details=error_msg)
                        store.add_dead_letter(entity_type, item_id, error_msg, json.dumps(item))
                        # Resilient batch processing: continue loop without aborting batch
                finally:
                    lock_mgr.release_lock(lock_key, worker_id)

        duration_ms = (time.time() - start_time) * 1000
        log_event(
            "Synchronization",
            f"{entity_type.capitalize()} batch sync completed: {stats}",
            duration_ms=duration_ms,
            metadata=stats,
        )
        return stats
    except Exception as e:
        log_event("Synchronization", f"{entity_type.capitalize()} sync error: {str(e)}")
        raise e

