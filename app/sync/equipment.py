import hashlib
import logging
from datetime import datetime
from typing import Dict, Any, Optional
from ..clients.rentasst_client import RentAsstClient
from ..clients.external_client import ExternalClient
from ..mapping.store import MappingStore
from .base import run_sync_pipeline, filter_by_date_range

logger = logging.getLogger(__name__)


def _unit_name(item: Dict[str, Any]) -> str:
    unit = item.get("asset_unit")
    if isinstance(unit, dict) and unit.get("name"):
        return str(unit["name"]).strip()
    name = item.get("asset_unit_name")
    return str(name).split("(")[0].strip() if name else "Nos"


def _presync_units(rentasst_client: RentAsstClient, external_client: ExternalClient) -> None:
    """
    Pre-creates every unit RentAsst has as its own isolated Tally UNIT master BEFORE any
    stock item sync runs, instead of creating units piecemeal — bundled inline into
    whichever STOCKITEM import happens to need one first, interleaved with the item's
    own GST/pricing/group data. Confirmed live: a burst of back-to-back STOCKITEM
    imports (several of which also carried a brand-new master-creation payload) preceded
    a native Tally "Memory Access Violation" crash. Only meaningful for a Tally target;
    a no-op for a REST external system. Failures here are logged and swallowed per unit
    — a RentAsst unit RentAsst itself never uses on an asset shouldn't block the sync,
    and sync_equipment()'s own per-item check_exists remains the safety net for any unit
    this pass didn't know about (e.g. a free-text unit name not in RentAsst's own Unit
    master list).
    """
    if getattr(external_client.cfg, "external_system_type", "tally") != "tally":
        return
    try:
        units = rentasst_client.fetch_units()
    except Exception as e:
        logger.warning(f"Skipping unit pre-sync — failed to fetch RentAsst units: {e}")
        return

    created = 0
    for u in units:
        name = str(u.get("name") or "").strip()
        symbol = str(u.get("symbol") or "").strip()
        if not name:
            continue
        try:
            if external_client.tally.sync_unit(name, symbol=symbol):
                created += 1
        except Exception as e:
            logger.warning(f"Failed to pre-create Tally unit '{name}': {e}")

    if created:
        logger.info(f"Unit pre-sync: created {created} new Tally unit master(s) before stock item sync.")


def _reconcile_all_stock(rentasst_client: RentAsstClient, external_client: ExternalClient, store: MappingStore) -> None:
    """
    Reconciles every equipment item's Tally quantity every single cycle, independent of
    run_sync_pipeline's content-hash "nothing changed, skip" gate on the master-data
    push. That gate compares RentAsst's own payload for drift, but Tally-side stock
    drift comes from Sales vouchers consuming stock there, not from RentAsst-side
    edits — an item can sit unchanged in RentAsst for weeks while Tally's own
    CLOSINGBALANCE keeps drifting away from RentAsst's available_quantity, so the
    dedup gate that's correct for the master-data push would silently suppress
    reconciliation forever. Runs unfiltered (ignoring any from_date/to_date used for the
    master-data sync above) since reconciliation isn't about which items RECENTLY
    changed in RentAsst.

    build_physical_stock_voucher_xml always sends ACTION="Create" with no REMOTEID, so
    without a dedup gate here Tally accumulates a brand-new "Physical Stock" voucher for
    every item on every scheduler cycle (confirmed live: a 1-minute cycle produced a
    fresh voucher per item per minute, all recording the exact same quantity) — "the
    physical stock voucher is updated in Tally repeatedly". A Physical Stock voucher is
    only meaningful once per day per item unless the quantity actually changes, so this
    keys a dedup hash on (item name, today's date, quantity) via the mapping store's
    existing is_duplicate/save_mapping helpers, reusing entity_type="stock_reconciliation"
    (a synthetic type, not a real synced entity) purely as a dedup ledger.
    """
    try:
        items = rentasst_client.fetch_equipment()
    except Exception as e:
        logger.warning(f"Skipping stock reconciliation — failed to fetch equipment: {e}")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    for item in items:
        name = (item.get("name") or "").strip()
        qty = item.get("available_quantity")
        if not name or qty is None:
            continue
        current_hash = hashlib.sha256(f"{today}:{qty}".encode("utf-8")).hexdigest()
        if store.is_duplicate("stock_reconciliation", name, current_hash):
            continue
        try:
            external_client.reconcile_equipment_stock(name, qty, unit=_unit_name(item))
            store.save_mapping(
                entity_type="stock_reconciliation",
                source_id=name,
                target_id=name,
                last_synced_hash=current_hash,
                status="synced",
            )
        except Exception as e:
            logger.warning(f"Failed to reconcile Tally stock quantity for '{name}': {e}")


def sync_equipment(
    rentasst_client: RentAsstClient,
    external_client: ExternalClient,
    store: MappingStore,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> Dict[str, Any]:
    _presync_units(rentasst_client, external_client)
    stats = run_sync_pipeline(
        entity_type="equipment",
        fetch_func=lambda: filter_by_date_range(rentasst_client.fetch_equipment(), from_date, to_date),
        sync_func=external_client.sync_equipment,
        store=store,
        external_client=external_client,
    )
    _reconcile_all_stock(rentasst_client, external_client, store)
    return stats
