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


def sync_rental_orders(
    rentasst_client: RentAsstClient,
    external_client: ExternalClient,
    store: MappingStore,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> Dict[str, Any]:
    def _fetch():
        items = filter_by_date_range(rentasst_client.fetch_rental_orders(), from_date, to_date)
        return _exclude_cancelled(items)

    return run_sync_pipeline(
        entity_type="rental_order",
        fetch_func=_fetch,
        sync_func=external_client.sync_rental_order,
        store=store,
        external_client=external_client,
    )
