import time
import hashlib
import json
from typing import Dict, Any, List, Callable
from ..mapping.store import MappingStore
from ..logging.logger import log_event


def compute_payload_hash(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def run_sync_pipeline(
    entity_type: str,
    fetch_func: Callable[[], List[Dict[str, Any]]],
    sync_func: Callable[[Dict[str, Any]], str],
    store: MappingStore,
    batch_size: int = 100,
) -> Dict[str, Any]:
    """
    Generic resilient synchronization pipeline runner with chunked batching, 
    content hash deduplication, and dead-letter queueing.
    """
    stats = {"processed": 0, "created": 0, "updated": 0, "failed": 0, "skipped": 0}
    start_time = time.time()

    try:
        items = fetch_func()
        total_count = len(items)

        # Process items in configurable chunked batches
        for i in range(0, total_count, batch_size):
            batch = items[i : i + batch_size]
            for item in batch:
                stats["processed"] += 1
                item_id = str(item.get("id"))
                payload_hash = compute_payload_hash(item)

                # Content hash deduplication check
                if store.is_duplicate(entity_type, item_id, payload_hash):
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
                except Exception as e:
                    stats["failed"] += 1
                    error_msg = f"Failed to sync {entity_type} {item_id}: {str(e)}"
                    log_event("Synchronization", error_msg, metadata={"entity_type": entity_type, "item_id": item_id})
                    store.add_dead_letter(entity_type, item_id, error_msg, str(item))
                    store.add_history(entity_type, item_id, "failed", details=error_msg)

        duration_ms = (time.time() - start_time) * 1000
        log_event(
            "Performance",
            f"{entity_type.capitalize()} batch sync completed: {stats}",
            duration_ms=duration_ms,
            metadata={"entity_type": entity_type, "stats": stats},
        )
    except Exception as e:
        log_event("Synchronization", f"{entity_type.capitalize()} sync error: {str(e)}")
        raise e

    return stats
