import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import MagicMock
from concurrent.futures import ThreadPoolExecutor

from app.queue.lock_manager import LockManager
from app.queue.queue_store import QueueStore, normalize_entity_type
from app.mapping.store import MappingStore
from app.sync.base import run_sync_pipeline
from app.connectors.tally.client import TallyClient
from app.models.domain import AppConfig
import threading


class TestConcurrencyAndLocks(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_concurrency.db")
        self.lock_mgr = LockManager(self.db_path, default_lease_seconds=2)
        self.queue_store = QueueStore(self.db_path)
        self.mapping_store = MappingStore(self.db_path)

    def tearDown(self):
        if hasattr(self, "mapping_store") and self.mapping_store:
            self.mapping_store.db.close()
        if hasattr(self, "queue_store") and self.queue_store:
            self.queue_store.db.close()
        if hasattr(self, "lock_mgr") and self.lock_mgr:
            self.lock_mgr.db.close()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_lock_acquisition_and_release(self):
        lock_key = "default:customer:forward:101"
        worker_a = "worker-thread-A"
        worker_b = "worker-thread-B"

        # Worker A acquires lock
        acquired_a = self.lock_mgr.acquire_lock(lock_key, worker_a, lease_seconds=10)
        self.assertTrue(acquired_a)

        # Worker B tries to acquire same lock -> must fail
        acquired_b = self.lock_mgr.acquire_lock(lock_key, worker_b, lease_seconds=10)
        self.assertFalse(acquired_b)

        # Worker A releases lock
        released = self.lock_mgr.release_lock(lock_key, worker_a)
        self.assertTrue(released)

        # Worker B can now acquire lock
        acquired_b_now = self.lock_mgr.acquire_lock(lock_key, worker_b, lease_seconds=10)
        self.assertTrue(acquired_b_now)

    def test_stale_lock_auto_expiration_crash_recovery(self):
        lock_key = "default:invoice:forward:505"
        crashed_worker = "worker-crashed-process"
        new_worker = "worker-recovery-process"

        # Crashed worker acquired a 1-second lock lease and crashed without releasing
        self.lock_mgr.acquire_lock(lock_key, crashed_worker, lease_seconds=1)

        # Immediately, new worker cannot acquire
        self.assertFalse(self.lock_mgr.acquire_lock(lock_key, new_worker, lease_seconds=10))

        # Wait 1.1s for lease to expire
        time.sleep(1.1)

        # Stale lock automatically expires and purges; new worker acquires lock cleanly!
        self.assertTrue(self.lock_mgr.acquire_lock(lock_key, new_worker, lease_seconds=10))

    def test_queue_entity_normalization_and_deduplication(self):
        self.assertEqual(normalize_entity_type("customer"), "customers")
        self.assertEqual(normalize_entity_type("customers"), "customers")
        self.assertEqual(normalize_entity_type("invoice"), "invoices")
        self.assertEqual(normalize_entity_type("invoices"), "invoices")

        # Enqueue "customers"
        job1 = self.queue_store.enqueue("customers")
        self.assertIsNotNone(job1)

        # Enqueue "customer" (singular) while "customers" is Pending -> must be deduplicated
        job2 = self.queue_store.enqueue("customer")
        self.assertIsNone(job2)

    def test_multi_worker_record_concurrency_isolation(self):
        """
        Tests that two workers attempting to sync the exact same record concurrently
        cannot create duplicate records; the lock manager blocks worker B while worker A processes.
        """
        mock_ext_client = MagicMock()
        mock_ext_client.ping.return_value = True
        mock_ext_client.check_exists_in_tally.return_value = False

        creation_call_count = 0
        sync_barrier = False

        def mock_sync_func(item):
            nonlocal creation_call_count
            creation_call_count += 1
            # Simulate work time inside critical creation section
            time.sleep(0.1)
            return f"TALLY-CONC-{item['id']}"

        items = [{"id": "999", "name": "Shared Customer"}]

        def worker_task(worker_name):
            store = MappingStore(self.db_path)
            try:
                return run_sync_pipeline(
                    entity_type="customer",
                    fetch_func=lambda: items,
                    sync_func=mock_sync_func,
                    store=store,
                    external_client=mock_ext_client,
                    source_company_id="company_test",
                )
            finally:
                store.db.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            fut1 = executor.submit(worker_task, "Worker1")
            fut2 = executor.submit(worker_task, "Worker2")
            res1 = fut1.result()
            res2 = fut2.result()

        # Exactly 1 creation call executed
        self.assertEqual(creation_call_count, 1)

        # One worker created, the other skipped due to lock contention
        total_created = res1["created"] + res2["created"]
        total_skipped = res1["skipped"] + res2["skipped"]
        self.assertEqual(total_created, 1)
        self.assertEqual(total_skipped, 1)

    def test_tally_client_serializes_concurrent_requests_across_instances(self):
        """
        Tally Prime's XML HTTP server cannot safely handle overlapping requests — confirmed
        live as 'Tally Business Error: Could not set SVCurrentCompany' when the queue
        worker's thread pool ran multiple entity syncs (each with its own TallyClient/
        session) at the same time. The fix is a process-wide lock, so this proves it holds
        even across five entirely separate TallyClient instances/sessions, not just within one.
        """
        cfg = AppConfig(external_url="http://localhost:9000", external_system_type="tally")
        state_lock = threading.Lock()
        concurrent_count = 0
        max_concurrent = 0

        class SlowMockSession:
            def post(self, *args, **kwargs):
                nonlocal concurrent_count, max_concurrent
                with state_lock:
                    concurrent_count += 1
                    max_concurrent = max(max_concurrent, concurrent_count)
                time.sleep(0.05)
                with state_lock:
                    concurrent_count -= 1
                resp = MagicMock()
                resp.status_code = 200
                resp.content = (
                    b"<ENVELOPE><HEADER><VERSION>1</VERSION></HEADER><BODY>"
                    b"<IMPORTRESULT><CREATED>1</CREATED></IMPORTRESULT></BODY></ENVELOPE>"
                )
                return resp

        def worker():
            client = TallyClient(cfg, session=SlowMockSession())
            client.send_xml("<ENVELOPE>Test</ENVELOPE>")

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(max_concurrent, 1)


if __name__ == "__main__":
    unittest.main()
