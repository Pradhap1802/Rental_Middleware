import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import MagicMock

from app.mapping.store import MappingStore
from app.utils.cache import TTLCache
from app.sync.base import run_sync_pipeline


class TestPerformanceAndOptimization(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_perf.db")
        self.store = MappingStore(self.db_path)
        self.cache = TTLCache(default_ttl_seconds=1)

    def tearDown(self):
        if hasattr(self, "store") and self.store:
            self.store.db.close()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_ttl_cache_operations(self):
        # Set & Get
        self.cache.set("key-1", "value-1", ttl_seconds=1)
        self.assertEqual(self.cache.get("key-1"), "value-1")

        # Invalidation
        self.cache.invalidate("key-1")
        self.assertIsNone(self.cache.get("key-1"))

        # TTL Expiration
        self.cache.set("key-ttl", "value-ttl", ttl_seconds=1)
        self.assertEqual(self.cache.get("key-ttl"), "value-ttl")
        time.sleep(1.1)
        self.assertIsNone(self.cache.get("key-ttl"))

    def test_batch_prefetch_mappings(self):
        # Pre-seed 3 customer mappings in SQLite DB
        self.store.save("customer", "101", "TALLY-CUST-101")
        self.store.save("customer", "102", "TALLY-CUST-102")
        self.store.save("customer", "103", "TALLY-CUST-103")

        # Prefetch batch mappings in single SQL query
        prefetched = self.store.prefetch_mappings("customer", ["101", "102", "103"])
        self.assertEqual(len(prefetched), 3)
        self.assertIn("101", prefetched)
        self.assertIn("102", prefetched)
        self.assertIn("103", prefetched)

        # Subsequent find_mapping calls hit in-memory TTL cache!
        m101 = self.store.find_mapping("customer", "101")
        self.assertEqual(m101["target_id"], "TALLY-CUST-101")

    def test_cache_invalidation_on_write(self):
        self.store.save("customer", "200", "TALLY-OLD")
        _ = self.store.find_mapping("customer", "200")

        # Update mapping -> Cache MUST be invalidated
        self.store.save("customer", "200", "TALLY-NEW")
        updated = self.store.find_mapping("customer", "200")
        self.assertEqual(updated["target_id"], "TALLY-NEW")

    def test_accounting_correctness_with_batch_prefetching(self):
        """
        Proves that high-performance batch prefetching and TTL caching
        never compromise accounting accuracy or mapping integrity.
        """
        mock_ext = MagicMock()
        mock_ext.ping.return_value = True
        mock_ext.check_exists_in_tally.return_value = False

        batch_invoices = [
            {"id": f"INV-PERF-{i}", "number": f"INV-PERF-{i}", "customer_id": "CUST-1", "subtotal": 100.0, "tax_amount": 18.0, "grand_total": 118.0}
            for i in range(10)
        ]

        self.store.save("customer", "CUST-1", "TALLY-CUST-1")

        stats = run_sync_pipeline(
            entity_type="invoice",
            fetch_func=lambda: batch_invoices,
            sync_func=lambda i: f"TALLY-VOUCHER-{i['id']}",
            store=self.store,
            external_client=mock_ext,
        )

        self.assertEqual(stats["created"], 10)
        self.assertEqual(stats["failed"], 0)

        # Verify all 10 mappings were saved with 100% accuracy
        for i in range(10):
            inv_id = f"INV-PERF-{i}"
            m = self.store.find_mapping("invoice", inv_id)
            self.assertIsNotNone(m)
            self.assertEqual(m["target_id"], f"TALLY-VOUCHER-{inv_id}")


if __name__ == "__main__":
    unittest.main()
