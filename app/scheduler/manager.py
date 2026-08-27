import logging
from apscheduler.schedulers.background import BackgroundScheduler
from typing import Optional
from ..configuration.store import ConfigStore
from ..queue.queue_store import QueueStore
from ..logging.logger import log_event

logger = logging.getLogger(__name__)


class SyncScheduler:
    """Decoupled APScheduler Manager that enqueues sync jobs into the SQLite Queue Engine."""
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.scheduler = BackgroundScheduler()
        self.queue_store = QueueStore(f"{data_dir}/state.db")
        self.is_running = False
        self.is_paused = False
        self._was_shutdown = False

    def _sync_job(self):
        log_event("Scheduler", "Polling tick: enqueuing forward sync jobs for Customers, Equipment, Rental Orders, Invoices, Payments...")
        try:
            cfg_store = ConfigStore(self.data_dir)
            cfg = cfg_store.load_safe()
            if not cfg or not cfg.auto_sync_enabled:
                return

            # Enqueue entity sync jobs into SQLite Queue Engine (Bidirectional Sync)
            entities = ["customers", "equipment", "rental_orders", "invoices", "payments", "tally_to_rentasst"]
            enqueued_count = 0
            for entity in entities:
                job_id = self.queue_store.enqueue(entity)
                if job_id:
                    enqueued_count += 1

            log_event("Scheduler", f"Polling enqueue completed. Enqueued {enqueued_count}/{len(entities)} entity jobs.")
        except Exception as e:
            log_event("Scheduler", f"Error during background scheduler enqueue: {str(e)}")

    def _backup_job(self):
        log_event("Scheduler", "Executing scheduled database backup...")
        try:
            from ..services.backup_service import BackupService
            svc = BackupService(self.data_dir)
            res = svc.trigger_backup()
            log_event("Scheduler", f"Scheduled backup completed: {res}")
        except Exception as e:
            log_event("Scheduler", f"Error during scheduled database backup: {e}")

    def start(self, interval_minutes: int = 10):
        if not self.is_running:
            if self._was_shutdown:
                # BackgroundScheduler.shutdown() permanently kills its executor's
                # underlying concurrent.futures.ThreadPoolExecutor — calling start() again
                # on the same instance resumes the scheduler loop and its interval
                # triggers fine, but every actual job submission then fails with
                # "cannot schedule new futures after shutdown" once the interval fires.
                # Observed live: login/logout toggles auto_sync_enabled, which calls
                # stop() then start() on this same SyncScheduler singleton — a fresh
                # BackgroundScheduler is required here rather than reusing the shut-down
                # one.
                self.scheduler = BackgroundScheduler()
                self._was_shutdown = False
            self.scheduler.add_job(
                self._sync_job,
                "interval",
                minutes=max(1, interval_minutes),
                id="rentasst_sync_job",
                replace_existing=True,
            )
            self.scheduler.add_job(
                self._backup_job,
                "interval",
                hours=24,
                id="rentasst_backup_job",
                replace_existing=True,
            )
            self.scheduler.start()
            self.is_running = True
            self.is_paused = False
            log_event("Scheduler", f"Sync scheduler started with {interval_minutes} minute sync interval and 24-hour backup schedule.")

    def stop(self):
        if self.is_running:
            self.scheduler.shutdown(wait=False)
            self._was_shutdown = True
            self.is_running = False
            self.is_paused = False
            log_event("Scheduler", "Sync scheduler stopped.")

    def pause(self):
        if self.is_running and not self.is_paused:
            self.scheduler.pause_job("rentasst_sync_job")
            self.is_paused = True
            log_event("Scheduler", "Sync scheduler paused.")

    def resume(self):
        if self.is_running and self.is_paused:
            self.scheduler.resume_job("rentasst_sync_job")
            self.is_paused = False
            log_event("Scheduler", "Sync scheduler resumed.")

    def trigger_manual_sync(self, entity_type: Optional[str] = None) -> int:
        """Immediately enqueues sync jobs for requested entity or all entities."""
        entities = [entity_type] if entity_type else ["customers", "equipment", "rental_orders", "invoices", "payments", "tally_to_rentasst"]
        enqueued = 0
        for entity in entities:
            res = self.queue_store.enqueue(entity, priority=True)
            if res:
                enqueued += 1
        log_event("Scheduler", f"Manual sync triggered for {entities}. Enqueued {enqueued} jobs.")
        return enqueued

    def trigger_immediate_sync(self, entity_type: str) -> Optional[int]:
        """Enqueues high-priority job for immediate execution by Queue Worker."""
        res = self.queue_store.enqueue(entity_type, priority=True)
        log_event("Scheduler", f"Immediate sync triggered for '{entity_type}'. Job ID: {res}")
        return res

    def update_interval(self, interval_minutes: int):
        if self.is_running:
            self.start(interval_minutes)
