import logging
from typing import Dict, Any, Optional
from ..clients.rentasst_client import RentAsstClient
from ..clients.external_client import ExternalClient
from ..mapping.store import MappingStore
from .base import run_sync_pipeline, filter_by_date_range

logger = logging.getLogger(__name__)


def sync_equipment(
    rentasst_client: RentAsstClient,
    external_client: ExternalClient,
    store: MappingStore,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> Dict[str, Any]:
    return run_sync_pipeline(
        entity_type="equipment",
        fetch_func=lambda: filter_by_date_range(rentasst_client.fetch_equipment(), from_date, to_date),
        sync_func=external_client.sync_equipment,
        store=store,
        external_client=external_client,
    )
