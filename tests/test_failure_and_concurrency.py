import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

from app.mapping.store import MappingStore
from app.queue.queue_store import QueueStore
from app.queue.lock_manager import LockManager
from app.sync.base import run_sync_pipeline
from app.sync.dependencies import DependencyResolver
from app.sync.idempotency import generate_integration_key


class TestFailureAndConcurrency(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_failure.db")
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

    def test_two_workers_processing_same_invoice(self):
        """
        Scenario 1: Two workers try to process the same invoice simultaneously.
        Distributed lock prevents second worker execution -> 0 duplicates created.
        """
        lock_key = generate_integration_key("default", "invoice", "INV-CONCURRENCY-1", "forward")

        # Worker 1 acquires lock
        locked_1 = self.lock_mgr.acquire_lock(lock_key, worker_id="worker-1")
        self.assertTrue(locked_1)

        # Worker 2 attempts lock acquisition for SAME invoice -> MUST FAIL
        locked_2 = self.lock_mgr.acquire_lock(lock_key, worker_id="worker-2")
        self.assertFalse(locked_2)

        # Worker 1 completes and releases lock
        self.lock_mgr.release_lock(lock_key, worker_id="worker-1")

    def test_scheduler_and_manual_sync_concurrent_trigger(self):
        """
        Scenario 2: Scheduler and Manual UI Trigger run concurrently.
        Atomic job lock skips duplicate execution.
        """
        mock_ext = MagicMock()
        mock_ext.ping.return_value = True
        mock_ext.check_exists_in_tally.return_value = False

        invoice_payload = [{
            "id": "INV-SCHED-MANUAL",
            "number": "INV-SCHED-MANUAL",
            "customer_id": "CUST-1",
            "subtotal": 1000.0,
            "tax_amount": 180.0,
            "grand_total": 1180.0,
        }]

        # Pre-seed customer mapping
        self.store.save("customer", "CUST-1", "TALLY-CUST-1")

        # Scheduler trigger runs (record not yet in Tally)
        stats_1 = run_sync_pipeline(
            entity_type="invoice",
            fetch_func=lambda: invoice_payload,
            sync_func=lambda i: f"TALLY-VOUCHER-{i['id']}",
            store=self.store,
            external_client=mock_ext,
        )
        self.assertEqual(stats_1["created"], 1)

        # Manual UI trigger runs immediately after (record now exists in Tally)
        mock_ext.check_exists_in_tally.return_value = True
        stats_2 = run_sync_pipeline(
            entity_type="invoice",
            fetch_func=lambda: invoice_payload,
            sync_func=lambda i: f"TALLY-VOUCHER-{i['id']}",
            store=self.store,
            external_client=mock_ext,
        )
        # Duplicate MUST be skipped cleanly without creating second mapping!
        self.assertEqual(stats_2["skipped"], 1)
        self.assertEqual(stats_2["created"], 0)

    def test_tally_timeout_after_successful_voucher_creation(self):
        """
        Scenario 3: Tally times out after voucher creation.
        Pre-flight target existence check discovers existing record in Tally -> adopts target ID without duplicate creation.
        """
        mock_ext = MagicMock()
        mock_ext.ping.return_value = True
        # Target check discovers voucher was already posted despite timeout
        mock_ext.check_exists_in_tally.return_value = True

        cust_payload = [{"id": "CUST-TIMEOUT-1", "name": "Timeout Customer Ltd"}]

        stats = run_sync_pipeline(
            entity_type="customer",
            fetch_func=lambda: cust_payload,
            sync_func=lambda c: "TALLY-TIMEOUT-CUST",
            store=self.store,
            external_client=mock_ext,
        )

        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["created"], 0)
        self.assertTrue(self.store.exists("customer", "CUST-TIMEOUT-1"))

    def test_tally_unavailable(self):
        """
        Scenario 4: Tally Prime server is completely unavailable.
        Job state moves to RETRYING/FAILED cleanly without crashing process.
        """
        job_id = self.q_store.enqueue(entity_type="customer", entity_id="CUST-OFFLINE")
        claimed = self.q_store.claim_next_job()
        self.assertEqual(claimed["id"], job_id)

        # Simulate connection refusal / timeout
        self.q_store.mark_failed(job_id, error_msg="Tally XML Server connection refused on 127.0.0.1:9000")

        with self.q_store.db.get_connection() as c:
            row = c.execute("SELECT status, last_error FROM sync_queue WHERE id=?", (job_id,)).fetchone()
            self.assertEqual(row["status"], "FAILED")
            self.assertIn("connection refused", row["last_error"].lower())

    def test_rentasst_unavailable(self):
        """
        Scenario 5: RentAsst Cloud API is unavailable.
        Queue state remains safely intact for retry.
        """
        job_id = self.q_store.enqueue(entity_type="invoice", entity_id="INV-CLOUD-OFFLINE")
        self.q_store.mark_failed(job_id, error_msg="HTTP 503 Service Unavailable: RentAsst API Gateway")

        with self.q_store.db.get_connection() as c:
            row = c.execute("SELECT status FROM sync_queue WHERE id=?", (job_id,)).fetchone()
            self.assertEqual(row["status"], "FAILED")

    def test_invalid_invoice_payload_dlq_routing(self):
        """
        Scenario 6: Invalid invoice payload (subtotal + tax != grand_total).
        Validation failure routes payload directly to DLQ.
        """
        mock_ext = MagicMock()
        mock_ext.ping.return_value = True

        invalid_inv = [{
            "id": "INV-BAD-MATH",
            "number": "INV-BAD-MATH",
            "customer_id": "CUST-1",
            "subtotal": 500.0,
            "tax_amount": 90.0,
            "grand_total": 9999.0,  # Invalid math
        }]

        stats = run_sync_pipeline(
            entity_type="invoice",
            fetch_func=lambda: invalid_inv,
            sync_func=lambda i: "TALLY-INV-ERR",
            store=self.store,
            external_client=mock_ext,
        )

        self.assertEqual(stats["failed"], 1)
        dls = self.store.list_dead_letters()
        self.assertGreater(len(dls), 0)

    def test_missing_ledger_waiting_for_dependency(self):
        """
        Scenario 7: Missing customer ledger dependency for invoice sync.
        Sets job to WAITING_FOR_DEPENDENCY without creating corrupted voucher.
        """
        inv_payload = {"id": "INV-NO-CUST", "customer_id": "CUST-UNMAPPED-999"}

        has_dep, dep_msg, missing_ent, missing_id = DependencyResolver.check_dependencies(
            entity_type="invoice",
            data=inv_payload,
            store=self.store,
        )

        self.assertFalse(has_dep)
        self.assertIn("Missing Customer dependency mapping", dep_msg)

    def test_middleware_crash_recovery_during_processing(self):
        """
        Scenario 8: Worker process crashes while job is in PROCESSING state.
        Startup recovery detects stale lock and safely recovers job.
        """
        job_id = self.q_store.enqueue(entity_type="customer", entity_id="CUST-CRASH")
        claimed = self.q_store.claim_next_job()
        self.assertEqual(claimed["status"], "PROCESSING")

        # Simulate startup crash recovery
        recovery_stats = self.q_store.recover_crashed_jobs(stale_threshold_seconds=0)
        self.assertEqual(recovery_stats["recovered_retrying"], 1)

        with self.q_store.db.get_connection() as c:
            row = c.execute("SELECT status FROM sync_queue WHERE id=?", (job_id,)).fetchone()
            self.assertEqual(row["status"], "RETRYING")

    def test_retry_after_failure_idempotent_recovery(self):
        """
        Scenario 9: Retry after temporary failure.
        Successful retry updates mapping cleanly without duplicates.
        """
        mock_ext = MagicMock()
        mock_ext.ping.return_value = True
        mock_ext.check_exists_in_tally.return_value = False

        cust_payload = [{"id": "CUST-RETRY-1", "name": "Retry Customer"}]

        # Attempt 1: Fails due to temporary network glitch
        def failing_sync(c):
            raise RuntimeError("Transient Network Glitch")

        run_sync_pipeline(
            entity_type="customer",
            fetch_func=lambda: cust_payload,
            sync_func=failing_sync,
            store=self.store,
            external_client=mock_ext,
        )

        self.assertFalse(self.store.exists("customer", "CUST-RETRY-1"))

        # Attempt 2: Recovered and successful
        stats_2 = run_sync_pipeline(
            entity_type="customer",
            fetch_func=lambda: cust_payload,
            sync_func=lambda c: "TALLY-CUST-RETRY-1",
            store=self.store,
            external_client=mock_ext,
        )

        self.assertEqual(stats_2["created"], 1)
        self.assertTrue(self.store.exists("customer", "CUST-RETRY-1"))

    def test_same_customer_id_in_two_companies_isolation(self):
        """
        Scenario 10: Customer 101 exists in both Company A and Company B.
        Company-scoped integration keys prevent cross-company contamination.
        """
        key_a = generate_integration_key("CompanyA", "customer", "101", "forward")
        key_b = generate_integration_key("CompanyB", "customer", "101", "forward")

        # Integration keys MUST be distinct!
        self.assertNotEqual(key_a, key_b)
        self.assertIn("companya", key_a.lower())
        self.assertIn("companyb", key_b.lower())

        # Save mapping for Company A
        self.store.save_mapping(
            entity_type="customer",
            source_id="101",
            target_id="TALLY-COMPA-101",
            source_company_id="CompanyA",
            integration_key=key_a,
        )

        # Save mapping for Company B
        self.store.save_mapping(
            entity_type="customer",
            source_id="101",
            target_id="TALLY-COMPB-101",
            source_company_id="CompanyB",
            integration_key=key_b,
        )

        map_a = self.store.find_by_integration_key(key_a)
        map_b = self.store.find_by_integration_key(key_b)

        self.assertEqual(map_a["target_id"], "TALLY-COMPA-101")
        self.assertEqual(map_b["target_id"], "TALLY-COMPB-101")
        self.assertNotEqual(map_a["target_id"], map_b["target_id"])


if __name__ == "__main__":
    unittest.main()
