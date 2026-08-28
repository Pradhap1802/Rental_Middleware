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


# Logged once per (company, entity_type, item_id) per process lifetime instead of every
# sync cycle — confirmed live: a handful of permanently-orphaned reverse-synced records
# (never repaired because reverse sync only revisits a Tally voucher when its ALTERID
# changes, not just because forward sync keeps flagging it) produced the same identical
# line for every one of them, every 10 minutes, forever, drowning out everything else in
# the cycle's log. The underlying skip behavior is unchanged; only the repeat noise is.
_orphaned_reverse_marker_warned: set = set()


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
    Builds the identifier used to ask the target system "does this record already exist"
    (check_target_system_record_exists) and, on a timeout-recovery adopt, saved as the
    mapping's target_id.

    For vouchers (rental_order/invoice/payment), this MUST be the same deterministic
    RENTAL-{ORD,INV,PAY}-{id} marker TallyClient.sync_rental_order/sync_invoice/
    sync_payment stamps into REMOTEID/NARRATION on creation (and returns as their own
    result) — confirmed live that RentAsst's own display number (e.g. 'R100016') never
    appears anywhere in Tally's export (Tally auto-assigns its own VOUCHERNUMBER and does
    not echo back what's sent), so using it here made check_exists()'s substring search
    against Tally's NARRATION/VOUCHERNUMBER fields fail for every already-synced
    voucher, every single cycle — the pipeline concluded "no longer exists in target
    system" and needlessly re-pushed an Alter forever, even though nothing had changed
    (confirmed live across two consecutive full sync passes: rental_order and payment
    entities never stabilized to a full skip, unlike customer/equipment/invoice).
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
from ..queue.lock_manager import LockManager
from ..validation.validator import validate_entity_payload
from .dependencies import DependencyResolver, MissingDependencyException


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

                # Reverse sync (Tally -> RentAsst) saves its own mapping the moment a
                # record originates from Tally (source_system="tally", target_id=this
                # RentAsst id) — forward-syncing that same record back to Tally isn't
                # just redundant, it's a genuine duplicate-creation attempt for vouchers:
                # Tally never recognizes it as pre-existing (only vouchers WE create
                # forward carry the REMOTEID/NARRATION marker checked elsewhere), so
                # Tally rejects the import with an opaque "EXCEPTIONS>0" business error.
                # Confirmed live: a RentAsst rentout created by reverse sync from a real
                # Tally Sales Order got re-pushed by every scheduled forward sync and
                # failed with exactly that error every time.
                #
                # Only vouchers carry that risk. Customer/equipment masters are matched
                # and Alter'd by NAME in Tally regardless of which system created them
                # first (TallyClient.check_exists), so an Alter never duplicates — it
                # just updates the existing ledger/stock item. Applying this same skip
                # to master data instead permanently blocks every RentAsst-owned field
                # (e.g. a customer's address — see ownership.py's FIELD_OWNERSHIP_POLICY)
                # from ever reaching a customer/equipment record that originated in
                # Tally: confirmed live, customers reverse-synced from Tally kept a real
                # RentAsst address forever un-pushed because this guard skipped them on
                # every single forward cycle.
                reverse_mapping = None
                if entity_type in ("rental_order", "invoice", "payment"):
                    reverse_mapping = store.find_by_target(
                        entity_type, item_id, target_system="rentasst", target_company_id=target_company_id
                    )
                if reverse_mapping and reverse_mapping.get("source_system") == "tally":
                    # This guard exists to stop a record Tally originally created (reverse
                    # sync) from being pushed right back into Tally as a duplicate — it's
                    # keyed purely on RentAsst's numeric id matching. Confirmed live: after
                    # RentAsst's own DB was reset, numeric ids got reused for genuinely new,
                    # RentAsst-native records (e.g. equipment id 1 used to be a
                    # reverse-synced Tally item, then became a brand-new "Dell Laptop"
                    # created directly in RentAsst) — the stale mapping still matched on id
                    # alone and permanently blocked the new record from ever forward-syncing.
                    # Verify the mapping's own Tally-side source record still exists before
                    # trusting it; if it doesn't, this is a stale/reused-id collision, not a
                    # real reverse-synced record, so drop it and fall through to sync normally.
                    reverse_source_name = reverse_mapping.get("source_id") or reverse_mapping.get("rentasst_id")
                    source_still_in_tally = True
                    if external_client and hasattr(external_client, "check_exists_in_tally") and reverse_source_name:
                        try:
                            source_still_in_tally = external_client.check_exists_in_tally(entity_type, reverse_source_name)
                        except Exception:
                            source_still_in_tally = True  # fail open — never risk a duplicate push on a transient error
                    if source_still_in_tally:
                        stats["skipped"] += 1
                        continue
                    log_event(
                        "Idempotency",
                        f"Reverse-sync mapping for {entity_type} id '{item_id}' points at Tally record "
                        f"'{reverse_source_name}', which no longer exists there — this id was reused for a "
                        "new RentAsst-native record. Dropping the stale mapping and forward-syncing normally.",
                    )
                    store.delete(entity_type, reverse_source_name)

                elif entity_type in ("rental_order", "invoice"):
                    # Fallback for exactly the gap the mapping-based guard above can't
                    # cover: reverse sync (tally_to_rentasst.py) saves its mapping in a
                    # separate step right AFTER the record is created in RentAsst — a
                    # process restart landing in that narrow window leaves the record
                    # created but permanently unmapped. Confirmed live: rentouts #114/
                    # #115/#116, all carrying reverse sync's own
                    # "Imported from Tally Sales Order #N" marker in 'notes', had no
                    # mapping row at all (interrupted by this session's own restarts)
                    # — #114 and #115 were duplicate-pushed into Tally as brand-new
                    # RENTAL-ORD-114/115 vouchers before this check existed, alongside
                    # the original Tally voucher reverse sync had read them from.
                    # 'notes' is a plain RentAsst field, always present on the fetched
                    # item regardless of whether a mapping survived, so it's a reliable
                    # second signal even when the mapping itself is gone. Recovering
                    # the exact tally_guid to backfill a real mapping isn't attempted
                    # here — the marker is enough to know forward-syncing this record
                    # would create a duplicate, which is the actual harm to prevent;
                    # a future reverse sync cycle re-reading the same Tally voucher is
                    # what actually repairs the mapping.
                    notes = str(item.get("notes") or "")
                    if notes.startswith("Imported from Tally "):
                        warn_key = (source_company_id, entity_type, item_id)
                        if warn_key not in _orphaned_reverse_marker_warned:
                            _orphaned_reverse_marker_warned.add(warn_key)
                            log_event(
                                "Idempotency",
                                f"{entity_type} #{item_id} carries reverse sync's own marker ('{notes}') "
                                "but has no local mapping — likely an interrupted mapping-save rather than "
                                "a genuinely RentAsst-native record. Skipping instead of risking a duplicate "
                                "push into Tally; a future reverse sync cycle should repair the mapping. "
                                "(repeats of this line for the same record are suppressed until restart)",
                            )
                        stats["skipped"] += 1
                        continue

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
                        # Raising here used to propagate all the way out of run_sync_pipeline,
                        # aborting the ENTIRE batch on the first item with a missing dependency —
                        # confirmed live: one rental order referencing an equipment item that
                        # hadn't synced yet (a routine, self-resolving race — equipment syncs
                        # concurrently with rental_orders every cycle) meant every OTHER rental
                        # order in that same fetch was silently skipped too, cycle after cycle,
                        # not just the one with the real gap. This item's own dependency will
                        # simply be re-checked on the next full sync cycle since it's never
                        # marked synced, so skip just this item and keep processing the rest of
                        # the batch instead of blocking on it.
                        log_event("Dependencies", f"Dependency check failed for {entity_type} #{item_id}: {missing_reason}")
                        stats["skipped"] += 1
                        continue

                    # 3. Integration Key & Content Hash Deduplication Check
                    existing_key_mapping = store.find_by_integration_key(integration_key)
                    if existing_key_mapping and store.is_duplicate(entity_type, item_id, payload_hash, source_company_id=source_company_id):
                        # A matching local hash only proves the payload hasn't changed since our
                        # last successful sync — it does NOT prove the record still exists at the
                        # destination. Tally data can be wiped/reloaded independently of this
                        # mapping table, which otherwise causes every record to be skipped forever.
                        if identifier and not check_target_system_record_exists(
                            entity_type=entity_type,
                            identifier=identifier,
                            sync_direction="forward",
                            external_client=external_client,
                            ra_client=ra_client,
                        ):
                            log_event(
                                "Idempotency",
                                f"Record '{identifier}' for entity '{entity_type}' #{item_id} was marked synced but no longer exists in target system — re-syncing instead of skipping.",
                            )
                        else:
                            stats["skipped"] += 1
                            continue


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

