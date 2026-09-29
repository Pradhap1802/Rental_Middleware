import logging
from typing import Dict, Any, List, Optional
from ..clients.rentasst_client import RentAsstClient
from ..clients.external_client import ExternalClient
from ..mapping.store import MappingStore
from .base import run_sync_pipeline, filter_by_date_range

logger = logging.getLogger(__name__)


def _attach_rent_items(rentasst_client: RentAsstClient, invoices: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    An invoice generated from a Rent Out (order_type == "rent") carries its line items
    on the underlying rentout's rent_items table, not on its own invoice_items —
    RentAsst's invoice list AND single-invoice detail endpoints both return an empty
    'items' array for these (confirmed live: GET /invoices/34, generated from rentout
    #22, returns "items": [] even though get-rent-items/22 has the real Dell Mouse
    line). Without this, every such invoice forward-synced to Tally with zero inventory
    lines — and since Tally then genuinely has nothing recorded, the Tally-to-RentAsst
    reverse sync had nothing to read back either, leaving line items empty in both
    directions. get_rent_items() is the same endpoint rental_orders.py already uses to
    solve the identical gap for Rent Out sync.

    Also attaches a 'rent' object carrying the underlying order's own RentAsst number
    (e.g. 'R100014') — build_sales_invoice_voucher_xml reads data['rent']['number'] to
    stamp a <REFERENCE> tag on the Tally invoice voucher so it's traceable back to its
    order, but fetch_invoices()/get_invoice() never return a nested 'rent' object at
    all (confirmed live: GET /invoices/4 returns only a flat order_id), so that
    reference was silently always blank. get_rent_items() already returns each item's
    parent 'rent' relation, so this reuses the same call rather than an extra one —
    and runs regardless of whether 'items' was already populated (a prior sync
    attempt may have already backfilled items without ever having had this reference
    attached), so an already-populated invoice doesn't stay permanently unreferenced.
    """
    for inv in invoices:
        order_id = inv.get("order_id")
        if not order_id or (inv.get("order_type") or "").strip().lower() != "rent":
            continue
        needs_items = not inv.get("items")
        needs_reference = not (inv.get("rent") or {}).get("number")
        if not needs_items and not needs_reference:
            continue
        try:
            raw_items = rentasst_client.get_rent_items(order_id)
        except Exception:
            continue
        if not raw_items:
            continue
        if needs_items:
            inv["items"] = [
                {
                    "name": it.get("asset_name") or (it.get("asset") or {}).get("name"),
                    "asset_id": it.get("asset_id"),
                    "quantity": it.get("rented_quantity"),
                    "price": it.get("price"),
                    "total_price": it.get("total_price"),
                    "unit": ((it.get("asset") or {}).get("asset_unit") or {}).get("name"),
                }
                for it in raw_items
            ]
        if needs_reference:
            rent_number = (raw_items[0].get("rent") or {}).get("number")
            if rent_number:
                inv["rent"] = {"number": rent_number}
    return invoices


def sync_invoices(
    rentasst_client: RentAsstClient,
    external_client: ExternalClient,
    store: MappingStore,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> Dict[str, Any]:
    def _fetch():
        items = filter_by_date_range(rentasst_client.fetch_invoices(), from_date, to_date)
        return _attach_rent_items(rentasst_client, items)

    return run_sync_pipeline(
        entity_type="invoice",
        fetch_func=_fetch,
        sync_func=external_client.sync_invoice,
        store=store,
        external_client=external_client,
    )
