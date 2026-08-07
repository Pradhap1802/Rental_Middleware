import time
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Callable
from .queue_store import QueueStore
from ..logging.logger import log_event
from ..retry.engine import is_retryable_exception, get_backoff_delay_seconds


class QueueWorker:
    """Background Queue Worker thread managing job pickup, retries, and execution."""
    def __init__(self, data_dir: str, sync_executor: Optional[Callable] = None, max_workers: int = 4):
        self.data_dir = data_dir
        self.queue_store = QueueStore(f"{data_dir}/state.db")
        self.sync_executor = sync_executor
        self.max_workers = max_workers
        self.is_running = False
        self._thread: Optional[threading.Thread] = None
        self._pool: Optional[ThreadPoolExecutor] = None
        self.current_job_info: str = "Idle"

    def set_sync_executor(self, sync_executor: Callable):
        self.sync_executor = sync_executor

    def _process_job(self, job: dict):
        job_id = job["id"]
        entity_type = job["entity_type"]
        attempts = job["attempts"]
        max_attempts = job["max_attempts"]

        self.current_job_info = f"Syncing {entity_type} (Job #{job_id})"
        start_time = time.time()
        log_event("Queue", f"Worker claimed job #{job_id} for entity '{entity_type}' (Attempt {attempts + 1}/{max_attempts})")

        try:
            if self.sync_executor:
                self.sync_executor(entity_type)
            self.queue_store.mark_completed(job_id)
            duration_ms = (time.time() - start_time) * 1000
            log_event("Queue", f"Job #{job_id} ('{entity_type}') completed successfully", duration_ms=duration_ms)
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            err_msg = str(e)
            retryable = is_retryable_exception(e)
            next_attempt = attempts + 1
            delay_seconds = get_backoff_delay_seconds(next_attempt)

            if retryable and delay_seconds is not None and next_attempt < max_attempts:
                log_event(
                    "Queue",
                    f"Job #{job_id} ('{entity_type}') failed with transient error: {err_msg}. Retrying in {delay_seconds}s.",
                    duration_ms=duration_ms,
                    metadata={"error": err_msg, "next_attempt": next_attempt},
                )
                self.queue_store.mark_retry(job_id, err_msg, delay_seconds)
            else:
                log_event(
                    "Queue",
                    f"Job #{job_id} ('{entity_type}') permanently failed: {err_msg}. Moving to dead letters.",
                    duration_ms=duration_ms,
                    metadata={"error": err_msg, "attempts": next_attempt},
                )
                self.queue_store.mark_failed(job_id, err_msg)
        finally:
            self.current_job_info = "Idle"

    def _worker_loop(self):
        log_event("Queue", "Background Queue Worker thread started.")
        while self.is_running:
            try:
                job = self.queue_store.claim_next_job()
                if job:
                    if self._pool:
                        self._pool.submit(self._process_job, job)
                    else:
                        self._process_job(job)
                else:
                    time.sleep(1.0)
            except Exception as e:
                log_event("Queue", f"Error in QueueWorker loop: {str(e)}", metadata={"error": str(e)})
                time.sleep(2.0)
        log_event("Queue", "Background Queue Worker thread stopped.")

    def start(self):
        if not self.is_running:
            self.is_running = True
            self._pool = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="QueueWorkerPool")
            self._thread = threading.Thread(target=self._worker_loop, daemon=True, name="QueueWorkerThread")
            self._thread.start()

    def stop(self):
        if self.is_running:
            self.is_running = False
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=3.0)
            if self._pool:
                self._pool.shutdown(wait=False)
                self._pool = None
