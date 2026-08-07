import logging
from typing import Dict, Any
from ..clients.rentasst_client import RentAsstClient
from ..clients.external_client import ExternalClient
from ..mapping.store import MappingStore
from .base import run_sync_pipeline

logger = logging.getLogger(__name__)


def sync_customers(rentasst_client: RentAsstClient, external_client: ExternalClient, store: MappingStore) -> Dict[str, Any]:
    return run_sync_pipeline(
        entity_type="customer",
        fetch_func=rentasst_client.fetch_customers,
        sync_func=external_client.sync_customer,
        store=store,
    )
