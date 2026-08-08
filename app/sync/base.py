import time
import hashlib
import json
from typing import Dict, Any, List, Callable, Optional
from ..mapping.store import MappingStore
from ..logging.logger import log_event


def compute_payload_hash(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def extract_identifier(entity_type: str, item: Dict[str, Any]) -> str:
    if entity_type == "customer":
        return str(item.get("name") or item.get("business_name") or "")
    elif entity_type == "equipment":
        return str(item.get("name") or "")
    elif entity_type == "rental_orders":
        return str(item.get("number") or item.get("rent_code") or f"ORD-{item.get('id')}")
    elif entity_type == "invoices":
        return str(item.get("number") or item.get("invoice_number") or f"INV-{item.get('id')}")
    elif entity_type == "payments":
        return str(item.get("reference_id") or item.get("payment_number") or f"PAY-{item.get('id')}")
    return ""


def run_sync_pipeline(
    entity_type: str,
    fetch_func: Callable[[], List[Dict[str, Any]]],
    sync_func: Callable[[Dict[str, Any]], str],
    store: MappingStore,
    external_client: Optional[Any] = None,
    batch_size: int = 100,
) -> Dict[str, Any]:
    """
    Generic resilient synchronization pipeline runner with chunked batching, 
    content hash deduplication, Tally DB existence check, and dead-letter queueing.
    """
    stats = {"processed": 0, "created": 0, "updated": 0, "failed": 0, "skipped": 0}
    start_time = time.time()

    try:
        items = fetch_func()
        total_count = len(items)

        for i in range(0, total_count, batch_size):
            batch = items[i : i + batch_size]
            for item in batch:
                stats["processed"] += 1
                item_id = str(item.get("id"))
                payload_hash = compute_payload_hash(item)
                identifier = extract_identifier(entity_type, item)

                # Content hash deduplication & Tally DB existence check
                if store.is_duplicate(entity_type, item_id, payload_hash):
                    # If record is in middleware DB, check if it was deleted in Tally DB
                    if external_client and external_client.ping() and not external_client.check_exists_in_tally(entity_type, identifier):
                        log_event("Synchronization", f"Record {entity_type} #{item_id} ('{identifier}') exists in middleware DB but was deleted in Tally. Resyncing...")
                    else:
                        stats["skipped"] += 1
                        continue

                try:
                    ext_id = store.get_external_id(entity_type, item_id)
                    new_ext_id = sync_func(item)
                    final_ext_id = new_ext_id or ext_id or item_id
                    
                    store.save(
                        entity_type=entity_type,
                        rentasst_id=item_id,
                        external_id=final_ext_id,
                        tally_guid=final_ext_id,
                        last_hash=payload_hash,
                        status="synced",
                    )
                    store.add_history(entity_type, item_id, "synced", external_id=final_ext_id)

                    if ext_id:
                        stats["updated"] += 1
                    else:
                        stats["created"] += 1

                except Exception as ex:
                    stats["failed"] += 1
                    error_msg = str(ex)
                    log_event("Synchronization", f"Failed to sync {entity_type} {item_id}: {error_msg}")
                    store.add_history(entity_type, item_id, "failed", details=error_msg)
                    store.add_dead_letter(entity_type, item_id, error_msg, json.dumps(item))
                    raise ex

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
