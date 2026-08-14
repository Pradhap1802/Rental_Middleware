import os
import shutil
import tempfile
import unittest
import requests

from app.retry.engine import (
    RetryConfig,
    get_backoff_delay_seconds,
    is_retryable_exception,
    RetryableException,
    NonRetryableException,
)
from app.mapping.store import MappingStore
from app.queue.queue_store import QueueStore


class TestRetrySystemAndDLQ(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_retry_dlq.db")
        self.store = MappingStore(self.db_path)
        self.q_store = QueueStore(self.db_path)

    def tearDown(self):
        if hasattr(self, "store") and self.store:
            self.store.db.close()
        if hasattr(self, "q_store") and self.q_store:
            self.q_store.db.close()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_exponential_backoff_and_jitter(self):
        cfg = RetryConfig(max_attempts=5, base_delay=5, max_delay=100, jitter=0.0)

        # Without jitter
        d1 = get_backoff_delay_seconds(1, config=cfg)
        d2 = get_backoff_delay_seconds(2, config=cfg)
        d3 = get_backoff_delay_seconds(3, config=cfg)
        d4 = get_backoff_delay_seconds(4, config=cfg)
        d5 = get_backoff_delay_seconds(5, config=cfg)
        d6 = get_backoff_delay_seconds(6, config=cfg)

        self.assertEqual(d1, 5)   # 5 * 2^0
        self.assertEqual(d2, 10)  # 5 * 2^1
        self.assertEqual(d3, 20)  # 5 * 2^2
        self.assertEqual(d4, 40)  # 5 * 2^3
        self.assertEqual(d5, 80)  # 5 * 2^4
        self.assertIsNone(d6)     # Max attempts (5) exhausted

        # With Jitter (±20%)
        cfg_j = RetryConfig(max_attempts=5, base_delay=10, max_delay=100, jitter=0.2)
        d_j = get_backoff_delay_seconds(2, config=cfg_j)  # Expected base: 20 -> [16, 24]
        self.assertTrue(16 <= d_j <= 24)

    def test_error_classification(self):
        # 1. Retryable errors
        self.assertTrue(is_retryable_exception(TimeoutError("Connection timed out")))
        self.assertTrue(is_retryable_exception(ConnectionRefusedError("Connection refused on localhost:9000")))
        self.assertTrue(is_retryable_exception(RetryableException("Tally temporary overload")))

        # HTTP 5xx & 429
        resp_503 = requests.Response()
        resp_503.status_code = 503
        self.assertTrue(is_retryable_exception(requests.exceptions.HTTPError(response=resp_503)))

        # 2. Non-Retryable errors
        self.assertFalse(is_retryable_exception(ValueError("Invalid payload format")))
        self.assertFalse(is_retryable_exception(KeyError("missing ledger Customer A")))
        self.assertFalse(is_retryable_exception(NonRetryableException("Malformed XML syntax")))

        # HTTP 400 & 422
        resp_400 = requests.Response()
        resp_400.status_code = 400
        self.assertFalse(is_retryable_exception(requests.exceptions.HTTPError(response=resp_400)))

        # Specific Tally business validation strings
        self.assertFalse(is_retryable_exception(Exception("Ledger 'Cash' does not exist in Tally")))
        self.assertFalse(is_retryable_exception(Exception("Invalid GSTIN number format")))

    def test_dlq_persistence_and_metadata(self):
        dl_id = self.store.add_dead_letter(
            entity_type="invoice",
            source_id="INV-1001",
            error="Ledger 'Sales Tax' does not exist",
            payload='{"amount": 5000, "customer": "Test"}',
            job_id=42,
            entity_id="INV-1001",
            company_id="CompanyBeta",
            source_system="rentasst",
            target_system="tally",
            error_type="LedgerNotFoundError",
            stack_trace="Traceback: line 45 in sync_invoice",
            attempt_count=3,
        )

        item = self.store.get_dead_letter(dl_id)
        self.assertIsNotNone(item)
        self.assertEqual(item["job_id"], 42)
        self.assertEqual(item["entity_type"], "invoice")
        self.assertEqual(item["entity_id"], "INV-1001")
        self.assertEqual(item["company_id"], "CompanyBeta")
        self.assertEqual(item["source_system"], "rentasst")
        self.assertEqual(item["target_system"], "tally")
        self.assertEqual(item["error_type"], "LedgerNotFoundError")
        self.assertEqual(item["error_message"], "Ledger 'Sales Tax' does not exist")
        self.assertEqual(item["stack_trace"], "Traceback: line 45 in sync_invoice")
        self.assertEqual(item["attempt_count"], 3)
        self.assertEqual(item["status"], "PENDING")
        self.assertIn('"amount": 5000', item["payload"])

    def test_dlq_actions_retry_ignore_resolve(self):
        id1 = self.store.add_dead_letter(entity_type="customer", source_id="C-1", error="Err1", payload='{"id": 1}')
        id2 = self.store.add_dead_letter(entity_type="customer", source_id="C-2", error="Err2", payload='{"id": 2}')
        id3 = self.store.add_dead_letter(entity_type="customer", source_id="C-3", error="Err3", payload='{"id": 3}')

        # 1. Mark status IGNORED
        self.store.mark_dead_letter_status(id1, "IGNORED")
        item1 = self.store.get_dead_letter(id1)
        self.assertEqual(item1["status"], "IGNORED")

        # 2. Mark status RESOLVED
        self.store.mark_dead_letter_status(id2, "RESOLVED")
        item2 = self.store.get_dead_letter(id2)
        self.assertEqual(item2["status"], "RESOLVED")

        # 3. Requeue single DLQ item -> re-enqueues into sync_queue as PENDING and updates DLQ status to RESOLVED
        ok = self.store.requeue_dead_letter(id3)
        self.assertTrue(ok)
        item3 = self.store.get_dead_letter(id3)
        self.assertEqual(item3["status"], "RESOLVED")

        # Assert queue now has the requeued PENDING job
        q_jobs = self.q_store.list_recent_jobs()
        self.assertTrue(any(j["entity_type"] == "customers" for j in q_jobs))


if __name__ == "__main__":
    unittest.main()
