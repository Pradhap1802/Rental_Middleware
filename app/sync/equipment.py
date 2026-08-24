import logging
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


def _reconcile_all_stock(rentasst_client: RentAsstClient, external_client: ExternalClient) -> None:
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
    """
    try:
        items = rentasst_client.fetch_equipment()
    except Exception as e:
        logger.warning(f"Skipping stock reconciliation — failed to fetch equipment: {e}")
        return

    for item in items:
        name = (item.get("name") or "").strip()
        qty = item.get("available_quantity")
        if not name or qty is None:
            continue
        try:
            external_client.reconcile_equipment_stock(name, qty, unit=_unit_name(item))
        except Exception as e:
            logger.warning(f"Failed to reconcile Tally stock quantity for '{name}': {e}")


def sync_equipment(
    rentasst_client: RentAsstClient,
    external_client: ExternalClient,
    store: MappingStore,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> Dict[str, Any]:
    stats = run_sync_pipeline(
        entity_type="equipment",
        fetch_func=lambda: filter_by_date_range(rentasst_client.fetch_equipment(), from_date, to_date),
        sync_func=external_client.sync_equipment,
        store=store,
        external_client=external_client,
    )
    _reconcile_all_stock(rentasst_client, external_client)
    return stats
