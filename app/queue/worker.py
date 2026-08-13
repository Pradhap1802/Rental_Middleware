import time
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Callable, Any, Dict
from .queue_store import QueueStore
from ..logging.logger import log_event
from ..retry.engine import is_retryable_exception, get_backoff_delay_seconds
from ..sync.dependencies import MissingDependencyException


class QueueWorker:
    """Background Queue Worker thread managing job pickup, retries, and execution."""
    def __init__(self, data_dir: Any, sync_executor: Optional[Callable] = None, max_workers: int = 4):
        if isinstance(data_dir, QueueStore):
            self.queue_store = data_dir
            self.data_dir = getattr(data_dir, "db_path", "")
        else:
            self.data_dir = str(data_dir)
            path = f"{data_dir}/state.db" if not str(data_dir).endswith(".db") else str(data_dir)
            self.queue_store = QueueStore(path)
        self.sync_executor = sync_executor
        self.max_workers = max_workers
        self.is_running = False
        self._thread: Optional[threading.Thread] = None
        self._pool: Optional[ThreadPoolExecutor] = None
        self.current_job_info: str = "Idle"

    def set_sync_executor(self, sync_executor: Callable):
        self.sync_executor = sync_executor

    def _process_job(self, job: dict):
        job_id = job.get("job_id") or job["id"]
        entity_type = job["entity_type"]
        attempts = job.get("attempt_count") if job.get("attempt_count") is not None else job.get("attempts", 0)
        max_attempts = job.get("max_attempts", 3)

        self.current_job_info = f"Syncing {entity_type} (Job #{job_id})"
        start_time = time.time()
        log_event("Queue", f"Worker claimed job #{job_id} for entity '{entity_type}' (Attempt {attempts + 1}/{max_attempts})")

        try:
            stats = None
            if self.sync_executor:
                stats = self.sync_executor(entity_type)

            partial = False
            if isinstance(stats, dict) and stats.get("failed", 0) > 0 and (stats.get("created", 0) > 0 or stats.get("updated", 0) > 0 or stats.get("skipped", 0) > 0):
                partial = True

            self.queue_store.mark_success(job_id, partial=partial)
            duration_ms = (time.time() - start_time) * 1000
            status_text = "PARTIAL_SUCCESS" if partial else "SUCCESS"
            log_event("Queue", f"Job #{job_id} ('{entity_type}') completed with status {status_text}", duration_ms=duration_ms)
        except MissingDependencyException as dep_ex:
            duration_ms = (time.time() - start_time) * 1000
            reason = str(dep_ex)
            log_event(
                "Queue",
                f"Job #{job_id} ('{entity_type}') paused: {reason}. Scheduled retry in 60s (WAITING_FOR_DEPENDENCY).",
                duration_ms=duration_ms,
                metadata={"reason": reason, "missing_entity": dep_ex.missing_entity, "missing_id": dep_ex.missing_id},
            )
            self.queue_store.mark_waiting_for_dependency(job_id, reason, delay_seconds=60)
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
                self.queue_store.mark_retrying(job_id, err_msg, delay_seconds)
            else:
                log_event(
                    "Queue",
                    f"Job #{job_id} ('{entity_type}') max retries/fatal error: {err_msg}. Moved to DLQ.",
                    duration_ms=duration_ms,
                    metadata={"error": err_msg, "attempts": next_attempt},
                )
                self.queue_store.mark_dlq(job_id, err_msg)
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

    def start(self, stale_threshold_seconds: int = 0):
        """Starts background worker and sweeps stale PROCESSING jobs left by crashes/terminations."""
        if not self.is_running:
            self.is_running = True
            try:
                self.queue_store.recover_crashed_jobs(stale_threshold_seconds=stale_threshold_seconds)
            except Exception as ex:
                log_event("Queue", f"Startup crash recovery warning: {ex}")

            self._pool = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="QueueWorkerPool")
            self._thread = threading.Thread(target=self._worker_loop, daemon=True)
            self._thread.start()

    def stop(self):
        if self.is_running:
            self.is_running = False
            if self._thread and self._thread.is_alive():
                self._thread.join(timeout=3.0)
            if self._pool:
                self._pool.shutdown(wait=False)
                self._pool = None
