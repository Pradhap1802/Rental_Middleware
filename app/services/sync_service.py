from typing import Dict, Any
from ..configuration.store import ConfigStore
from ..mapping.store import MappingStore
from ..clients.rentasst_client import RentAsstClient
from ..clients.external_client import ExternalClient
from ..sync.customers import sync_customers
from ..sync.equipment import sync_equipment
from ..sync.rental_orders import sync_rental_orders
from ..sync.invoices import sync_invoices
from ..sync.payments import sync_payments


class SyncService:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir

    def execute_sync(self, entity_type: str) -> Dict[str, Any]:
        cfg_store = ConfigStore(self.data_dir)
        cfg = cfg_store.require()
        db_path = f"{self.data_dir}/state.db"
        store = MappingStore(db_path)
        ra_client = RentAsstClient(cfg)
        ext_client = ExternalClient(cfg)

        try:
            entity = (entity_type or "").lower()
            if entity in ("customers", "customer"):
                return sync_customers(ra_client, ext_client, store)
            elif entity in ("equipment", "product"):
                return sync_equipment(ra_client, ext_client, store)
            elif entity in ("rental_orders", "rental_order", "orders"):
                return sync_rental_orders(ra_client, ext_client, store)
            elif entity in ("invoices", "invoice"):
                return sync_invoices(ra_client, ext_client, store)
            elif entity in ("payments", "payment"):
                return sync_payments(ra_client, ext_client, store)
            else:
                # Sync all
                res_c = sync_customers(ra_client, ext_client, store)
                res_e = sync_equipment(ra_client, ext_client, store)
                res_o = sync_rental_orders(ra_client, ext_client, store)
                res_i = sync_invoices(ra_client, ext_client, store)
                res_p = sync_payments(ra_client, ext_client, store)
                return {
                    "processed": res_c["processed"] + res_e["processed"] + res_o["processed"] + res_i["processed"] + res_p["processed"],
                    "created": res_c["created"] + res_e["created"] + res_o["created"] + res_i["created"] + res_p["created"],
                    "updated": res_c["updated"] + res_e["updated"] + res_o["updated"] + res_i["updated"] + res_p["updated"],
                    "failed": res_c["failed"] + res_e["failed"] + res_o["failed"] + res_i["failed"] + res_p["failed"],
                }
        finally:
            ra_client.close()
            ext_client.close()
