import logging
from typing import Dict, Any
from ..clients.rentasst_client import RentAsstClient
from ..clients.external_client import ExternalClient
from ..mapping.store import MappingStore
from .base import run_sync_pipeline

logger = logging.getLogger(__name__)


def sync_units(rentasst_client: RentAsstClient, external_client: ExternalClient, store: MappingStore) -> Dict[str, Any]:
    """
    Synchronizes Asset Units (Units of Measure / UOM) from RentAsst to Tally Prime / external ERP.
    Must be executed before starting Equipment/Asset sync.
    """
    return run_sync_pipeline(
        entity_type="units",
        fetch_func=rentasst_client.fetch_asset_units,
        sync_func=external_client.sync_unit,
        store=store,
        external_client=external_client,
        ra_client=rentasst_client,
    )
