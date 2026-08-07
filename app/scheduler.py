import logging
from apscheduler.schedulers.background import BackgroundScheduler
from typing import Optional
from .config import ConfigStore
from .mapping_store import MappingStore
from .rentasst_client import RentAsstClient
from .external_client import ExternalClient
from .sync.customers import sync_customers
from .sync.equipment import sync_equipment
from .sync.rental_orders import sync_rental_orders
from .sync.invoices import sync_invoices
from .sync.payments import sync_payments

logger = logging.getLogger(__name__)


class SyncScheduler:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.scheduler = BackgroundScheduler()
        self.is_running = False

    def _sync_job(self):
        logger.info("Executing scheduled sync background job...")
        try:
            cfg_store = ConfigStore(self.data_dir)
            cfg = cfg_store.load_safe()
            if not cfg or not cfg.auto_sync_enabled:
                return

            db_path = f"{self.data_dir}/state.db"
            store = MappingStore(db_path)
            ra_client = RentAsstClient(cfg)
            ext_client = ExternalClient(cfg)

            sync_customers(ra_client, ext_client, store)
            sync_equipment(ra_client, ext_client, store)
            sync_rental_orders(ra_client, ext_client, store)
            sync_invoices(ra_client, ext_client, store)
            sync_payments(ra_client, ext_client, store)
            logger.info("Scheduled sync completed successfully.")
        except Exception as e:
            logger.error(f"Error during background sync job: {str(e)}")

    def start(self, interval_minutes: int = 15):
        if not self.is_running:
            self.scheduler.add_job(
                self._sync_job,
                "interval",
                minutes=max(1, interval_minutes),
                id="rentasst_sync_job",
                replace_existing=True,
            )
            self.scheduler.start()
            self.is_running = True
            logger.info(f"Sync scheduler started with {interval_minutes} minute interval.")

    def stop(self):
        if self.is_running:
            self.scheduler.shutdown(wait=False)
            self.is_running = False
            logger.info("Sync scheduler stopped.")
