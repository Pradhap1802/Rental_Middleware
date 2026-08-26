from datetime import datetime, timezone
from typing import Dict, Any, Optional
from ..configuration.store import ConfigStore
from ..mapping.store import MappingStore
from ..clients.rentasst_client import RentAsstClient
from ..clients.external_client import ExternalClient
from ..sync.customers import sync_customers
from ..sync.equipment import sync_equipment
from ..sync.rental_orders import sync_rental_orders
from ..sync.invoices import sync_invoices
from ..sync.payments import sync_payments
from ..sync.tally_to_rentasst import sync_tally_to_rentasst


class SyncService:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir

    def execute_sync(
        self,
        entity_type: str,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        cfg_store = ConfigStore(self.data_dir)
        cfg = cfg_store.require()
        if not cfg or not cfg.rentasst_api_key or not cfg.rentasst_api_key.strip():
            raise ValueError("Middleware is not authenticated. Please log in with your RentAsst mobile number to authenticate.")
        db_path = f"{self.data_dir}/state.db"
        store = MappingStore(db_path)
        ra_client = RentAsstClient(cfg)
        ext_client = ExternalClient(cfg)

        def mark_synced(status_key: str) -> None:
            # Records that a sync attempt for this entity completed just now, regardless
            # of whether any record actually changed — MAX(last_synced_at) on the mapping
            # table only advances when a record changes, which froze "Last Synced" on any
            # run that legitimately found nothing new in the requested date range.
            store.set_checkpoint(f"last_synced:{status_key}", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))

        def persist_edu_mode_if_auto_detected() -> None:
            # TallyClient auto-detects and self-corrects an Educational/unlicensed Tally
            # company mid-sync (see _send_voucher_with_edu_fallback) by flipping
            # cfg.tally_edu_mode in memory for the rest of THIS run — but cfg itself is
            # re-loaded fresh from disk on every execute_sync() call, so without writing
            # it back here the very next sync cycle would rediscover (and re-fail) the
            # exact same thing all over again instead of starting with it already known.
            if getattr(ext_client.tally, "edu_mode_auto_detected", False):
                cfg.tally_edu_mode = True
                cfg_store.save(cfg)

        try:
            entity = (entity_type or "").lower()
            if entity in ("tally_to_rentasst", "reverse_sync"):
                res = sync_tally_to_rentasst(ra_client, ext_client, store, from_date=from_date, to_date=to_date)
                mark_synced("reverse_sync")
                return res
            elif entity in ("customers", "customer"):
                res = sync_customers(ra_client, ext_client, store, from_date=from_date, to_date=to_date)
                mark_synced("customers")
                return res
            elif entity in ("equipment", "product"):
                res = sync_equipment(ra_client, ext_client, store, from_date=from_date, to_date=to_date)
                mark_synced("equipment")
                return res
            elif entity in ("rental_orders", "rental_order", "orders", "order", "rents", "rent"):
                res = sync_rental_orders(ra_client, ext_client, store, from_date=from_date, to_date=to_date)
                mark_synced("rental_orders")
                return res
            elif entity in ("invoices", "invoice"):
                res = sync_invoices(ra_client, ext_client, store, from_date=from_date, to_date=to_date)
                mark_synced("invoices")
                return res
            elif entity in ("payments", "payment"):
                res = sync_payments(ra_client, ext_client, store, from_date=from_date, to_date=to_date)
                mark_synced("payments")
                return res
            else:
                # Sync all (Forward RentAsst -> Tally + Reverse Tally -> RentAsst)
                res_c = sync_customers(ra_client, ext_client, store, from_date=from_date, to_date=to_date)
                mark_synced("customers")
                res_e = sync_equipment(ra_client, ext_client, store, from_date=from_date, to_date=to_date)
                mark_synced("equipment")
                res_o = sync_rental_orders(ra_client, ext_client, store, from_date=from_date, to_date=to_date)
                mark_synced("rental_orders")
                res_i = sync_invoices(ra_client, ext_client, store, from_date=from_date, to_date=to_date)
                mark_synced("invoices")
                res_p = sync_payments(ra_client, ext_client, store, from_date=from_date, to_date=to_date)
                mark_synced("payments")
                res_t = sync_tally_to_rentasst(ra_client, ext_client, store, from_date=from_date, to_date=to_date)
                mark_synced("reverse_sync")
                return {
                    "processed": res_c["processed"] + res_e["processed"] + res_o["processed"] + res_i["processed"] + res_p["processed"] + res_t["processed"],
                    "created": res_c["created"] + res_e["created"] + res_o["created"] + res_i["created"] + res_p["created"] + res_t["created"],
                    "updated": res_c["updated"] + res_e["updated"] + res_o["updated"] + res_i["updated"] + res_p["updated"] + res_t["updated"],
                    "failed": res_c["failed"] + res_e["failed"] + res_o["failed"] + res_i["failed"] + res_p["failed"] + res_t["failed"],
                }
        finally:
            persist_edu_mode_if_auto_detected()
            ra_client.close()
            ext_client.close()


