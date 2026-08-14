import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

from app.mapping.store import MappingStore
from app.queue.queue_store import QueueStore
from app.queue.worker import QueueWorker
from app.queue.lock_manager import LockManager
from app.sync.base import run_sync_pipeline
from app.sync.idempotency import generate_integration_key


class TestCrashRecovery(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_crash_rec.db")
        self.store = MappingStore(self.db_path)
        self.q_store = QueueStore(self.db_path)
        self.lock_mgr = LockManager(self.db_path)

    def tearDown(self):
        if hasattr(self, "store") and self.store:
            self.store.db.close()
        if hasattr(self, "q_store") and self.q_store:
            self.q_store.db.close()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_stale_processing_job_recovery(self):
        # 1. Manually insert a crashed job in PROCESSING status
        job_id = self.q_store.enqueue(entity_type="customer", entity_id="CRASH-CUST-1")
        
        # Simulate worker crash during PROCESSING
        with self.q_store.db.get_connection() as c:
            c.execute(
                "UPDATE sync_queue SET status='PROCESSING', started_at='2000-01-01 00:00:00', attempt_count=0 WHERE id=?",
                (job_id,),
            )

        # 2. Trigger crash recovery
        stats = self.q_store.recover_crashed_jobs(stale_threshold_seconds=0)

        self.assertEqual(stats["recovered_retrying"], 1)

        # 3. Assert job status transitioned to RETRYING with attempt_count = 1
        jobs = [j for j in self.q_store.list_recent_jobs() if j["job_id"] == job_id]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["status"], "RETRYING")
        self.assertEqual(jobs[0]["attempt_count"], 1)

    def test_crashed_job_exceeding_max_attempts_moves_to_dlq(self):
        job_id = self.q_store.enqueue(entity_type="invoice", entity_id="CRASH-INV-999")

        # Simulate job at max_attempts stuck in PROCESSING
        with self.q_store.db.get_connection() as c:
            c.execute(
                "UPDATE sync_queue SET status='PROCESSING', started_at='2000-01-01 00:00:00', attempt_count=3, max_attempts=3 WHERE id=?",
                (job_id,),
            )

        stats = self.q_store.recover_crashed_jobs(stale_threshold_seconds=0)

        self.assertEqual(stats["recovered_dlq"], 1)

        # Assert moved to DLQ
        jobs = [j for j in self.q_store.list_recent_jobs() if j["job_id"] == job_id]
        self.assertEqual(jobs[0]["status"], "DLQ")
        self.assertIn("Recovered from process termination", jobs[0]["last_error"])

    def test_idempotent_recovery_retry_no_duplicates(self):
        """
        Scenario: Process crashed after Tally created customer record 'Tally Crash Test' but BEFORE middleware saved mapping.
        On recovery retry: middleware target pre-check detects existing record in Tally and adopts ID without duplicate creation!
        """
        cust_id = "555"
        cust_item = [{"id": cust_id, "name": "Tally Crash Test"}]

        mock_ext_client = MagicMock()
        mock_ext_client.ping.return_value = True
        # Simulate Tally already having record 'Tally Crash Test'
        mock_ext_client.check_exists_in_tally.return_value = True

        creation_call_count = 0

        def mock_sync_func(item):
            nonlocal creation_call_count
            creation_call_count += 1
            return f"TALLY-CUST-{item['id']}"

        # Run pipeline after recovery
        stats = run_sync_pipeline(
            entity_type="customer",
            fetch_func=lambda: cust_item,
            sync_func=mock_sync_func,
            store=self.store,
            external_client=mock_ext_client,
        )

        # Creation function MUST NOT be called! Target system pre-check adopts existing Tally ID!
        self.assertEqual(creation_call_count, 0)
        self.assertEqual(stats["skipped"], 1)

        # Mapping is saved cleanly
        int_key = generate_integration_key("default", "customer", cust_id, "forward")
        mapping = self.store.find_by_integration_key(int_key)
        self.assertIsNotNone(mapping)
        self.assertEqual(mapping["target_id"], "Tally Crash Test")


if __name__ == "__main__":
    unittest.main()
