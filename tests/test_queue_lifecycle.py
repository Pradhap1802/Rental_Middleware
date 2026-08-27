import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

from app.queue.queue_store import QueueStore
from app.queue.worker import QueueWorker


class TestQueueJobLifecycle(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_lifecycle.db")
        self.store = QueueStore(self.db_path)

    def tearDown(self):
        if hasattr(self, "store") and self.store:
            self.store.db.close()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_job_enqueueing_and_initial_attributes(self):
        job_id = self.store.enqueue(
            entity_type="invoice",
            payload={"test": "data"},
            entity_id="INV-001",
            company_id="CompanyAlpha",
            direction="forward",
        )
        self.assertIsNotNone(job_id)

        jobs = self.store.list_recent_jobs(limit=1)
        self.assertEqual(len(jobs), 1)
        j = jobs[0]

        self.assertEqual(j["job_id"], job_id)
        self.assertEqual(j["entity_type"], "invoices")
        self.assertEqual(j["entity_id"], "INV-001")
        self.assertEqual(j["company_id"], "CompanyAlpha")
        self.assertEqual(j["direction"], "forward")
        self.assertEqual(j["status"], "PENDING")
        self.assertEqual(j["attempt_count"], 0)
        self.assertIsNone(j["started_at"])
        self.assertIsNone(j["completed_at"])

    def test_job_claiming_and_processing_transition(self):
        job_id = self.store.enqueue(entity_type="customer", entity_id="CUST-10")
        claimed = self.store.claim_next_job()

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["job_id"], job_id)
        self.assertEqual(claimed["status"], "PROCESSING")
        self.assertIsNotNone(claimed["started_at"])
        self.assertIsNone(claimed["completed_at"])

    def test_success_and_partial_success_transitions(self):
        # 1. Full SUCCESS
        job1 = self.store.enqueue(entity_type="customer", entity_id="1")
        self.store.claim_next_job()
        self.store.mark_success(job1, partial=False)

        j1 = [j for j in self.store.list_recent_jobs() if j["job_id"] == job1][0]
        self.assertEqual(j1["status"], "SUCCESS")
        self.assertIsNotNone(j1["completed_at"])

        # 2. PARTIAL_SUCCESS
        job2 = self.store.enqueue(entity_type="equipment", entity_id="2")
        self.store.claim_next_job()
        self.store.mark_success(job2, partial=True)

        j2 = [j for j in self.store.list_recent_jobs() if j["job_id"] == job2][0]
        self.assertEqual(j2["status"], "PARTIAL_SUCCESS")
        self.assertIsNotNone(j2["completed_at"])

    def test_retrying_backoff_and_state(self):
        job_id = self.store.enqueue(entity_type="payment", entity_id="PAY-9")
        self.store.claim_next_job()
        self.store.mark_retrying(job_id, error_msg="Timeout connecting to Tally", delay_seconds=60)

        j = [j for j in self.store.list_recent_jobs() if j["job_id"] == job_id][0]
        self.assertEqual(j["status"], "RETRYING")
        self.assertEqual(j["attempt_count"], 1)
        self.assertEqual(j["last_error"], "Timeout connecting to Tally")
        self.assertIsNotNone(j["next_retry_at"])

    def test_retrying_job_is_not_immediately_reclaimable(self):
        """
        mark_retrying()/mark_waiting_for_dependency() only updated next_retry_at/
        next_attempt_at to the future delay, never the job's original 'scheduled_at' —
        but claim_next_job()'s eligibility check is `scheduled_at <= now OR
        next_retry_at <= now OR next_attempt_at <= now`. Since scheduled_at is set once
        at creation and is always in the past by the time a retry happens, that OR
        condition was always true regardless of the intended backoff — confirmed live: a
        job with a 60-second WAITING_FOR_DEPENDENCY delay was reclaimed and re-run every
        ~7 seconds instead. All three "due" columns must agree so the delay actually
        holds until it elapses.
        """
        job_id = self.store.enqueue(entity_type="payment", entity_id="PAY-10")
        self.store.claim_next_job()
        self.store.mark_retrying(job_id, error_msg="Timeout connecting to Tally", delay_seconds=60)

        self.assertIsNone(self.store.claim_next_job())

    def test_waiting_for_dependency_job_is_not_immediately_reclaimable(self):
        job_id = self.store.enqueue(entity_type="rental_orders", entity_id="21")
        self.store.claim_next_job()
        self.store.mark_waiting_for_dependency(job_id, reason="Missing Equipment dependency mapping", delay_seconds=60)

        self.assertIsNone(self.store.claim_next_job())

    def test_dlq_transition_and_dead_letter_logging(self):
        job_id = self.store.enqueue(entity_type="invoice", entity_id="INV-999")
        self.store.claim_next_job()
        self.store.mark_dlq(job_id, error_msg="Schema validation error")

        j = [j for j in self.store.list_recent_jobs() if j["job_id"] == job_id][0]
        self.assertEqual(j["status"], "DLQ")
        self.assertEqual(j["attempt_count"], 1)
        self.assertIsNotNone(j["completed_at"])

    def test_job_cancellation(self):
        job_id = self.store.enqueue(entity_type="customer", entity_id="CUST-CANCEL")
        cancelled = self.store.cancel_job(job_id)
        self.assertTrue(cancelled)

        j = [j for j in self.store.list_recent_jobs() if j["job_id"] == job_id][0]
        self.assertEqual(j["status"], "CANCELLED")
        self.assertIsNotNone(j["completed_at"])

    def test_full_worker_audit_traceability(self):
        mock_executor = MagicMock()
        mock_executor.return_value = {"processed": 10, "created": 8, "updated": 1, "failed": 1, "skipped": 0}

        worker = QueueWorker(self.store, sync_executor=mock_executor)

        job_id = self.store.enqueue(entity_type="invoice", entity_id="INV-AUDIT")
        claimed_job = self.store.claim_next_job()

        worker._process_job(claimed_job)

        j = [j for j in self.store.list_recent_jobs() if j["job_id"] == job_id][0]
        self.assertEqual(j["status"], "PARTIAL_SUCCESS")
        self.assertIsNotNone(j["created_at"])
        self.assertIsNotNone(j["started_at"])
        self.assertIsNotNone(j["completed_at"])

    def test_total_failure_is_not_recorded_as_success(self):
        """
        If every item in the batch failed (executor caught each error internally and
        never raised), the job must NOT be recorded as SUCCESS just because no exception
        propagated — otherwise a fully-down RentAsst/Tally target is invisible at the
        queue/dashboard level. It should go through the same retryable path as a raised
        exception.
        """
        mock_executor = MagicMock()
        mock_executor.return_value = {"processed": 3, "created": 0, "updated": 0, "failed": 3, "skipped": 0}

        worker = QueueWorker(self.store, sync_executor=mock_executor)

        job_id = self.store.enqueue(entity_type="invoice", entity_id="INV-ALL-FAILED")
        claimed_job = self.store.claim_next_job()

        worker._process_job(claimed_job)

        j = [j for j in self.store.list_recent_jobs() if j["job_id"] == job_id][0]
        self.assertIn(j["status"], ("RETRYING", "DLQ"))
        self.assertNotEqual(j["status"], "SUCCESS")
        self.assertNotEqual(j["status"], "PARTIAL_SUCCESS")
        self.assertIn("3 item(s) failed", j["last_error"])


if __name__ == "__main__":
    unittest.main()
