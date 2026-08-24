import logging
from typing import Dict, Any, List, Optional
from ..clients.rentasst_client import RentAsstClient
from ..clients.external_client import ExternalClient
from ..mapping.store import MappingStore
from .base import run_sync_pipeline, filter_by_date_range

logger = logging.getLogger(__name__)

# RentAsst's RentStatuses::CANCELLED. A cancelled order has no amount and no business
# being pushed to Tally as a sales voucher — forward-syncing it anyway just fails the
# zero-amount validation and writes a fresh dead-letter entry every single sync cycle
# forever, since there's nothing to fix (confirmed live: rentouts #9-#12, all
# status=7/amount=None, re-dead-lettered on every 10-minute run).
CANCELLED_STATUS = 7


def _exclude_cancelled(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [it for it in items if it.get("status") != CANCELLED_STATUS]


def _attach_rent_items(rentasst_client: RentAsstClient, orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    fetch_rental_orders()'s list view only returns a bare 'rent_items_count' integer, no
    item detail at all — confirmed live: build_sales_order_voucher_xml's item lookup
    (data.get('rent_items')/('items')/...) always found nothing, so every forward-synced
    Sales Order voucher landed in Tally as header-only, with zero inventory lines,
    regardless of how many real rent items the order had. fetch_invoices() doesn't have
    this gap — its list view already embeds a full 'items' array — so only rental_orders
    needs this extra per-order fetch.
    """
    for order in orders:
        if not order.get("rent_items_count"):
            continue
        try:
            raw_items = rentasst_client.get_rent_items(order.get("id"))
        except Exception:
            continue
        order["items"] = [
            {
                "name": it.get("asset_name"),
                "quantity": it.get("rented_quantity"),
                "price": it.get("price"),
                "total_price": it.get("total_price"),
                "unit": ((it.get("asset") or {}).get("asset_unit") or {}).get("name"),
            }
            for it in raw_items
        ]
    return orders


def sync_rental_orders(
    rentasst_client: RentAsstClient,
    external_client: ExternalClient,
    store: MappingStore,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> Dict[str, Any]:
    def _fetch():
        items = filter_by_date_range(rentasst_client.fetch_rental_orders(), from_date, to_date)
        items = _exclude_cancelled(items)
        return _attach_rent_items(rentasst_client, items)

    return run_sync_pipeline(
        entity_type="rental_order",
        fetch_func=_fetch,
        sync_func=external_client.sync_rental_order,
        store=store,
        external_client=external_client,
    )
