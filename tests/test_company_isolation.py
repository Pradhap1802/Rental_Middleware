import os
import shutil
import tempfile
import unittest

from app.mapping.store import MappingStore
from app.queue.queue_store import QueueStore
from app.queue.lock_manager import LockManager
from app.sync.idempotency import generate_integration_key


class TestCompanyIsolation(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_comp_iso.db")
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

    def test_mapping_company_isolation(self):
        # Company A Customer 101 -> TALLY-COMPA-101
        self.store.save_mapping(
            entity_type="customer",
            source_id="101",
            target_id="TALLY-COMPA-101",
            source_company_id="company_A",
            target_company_id="company_A",
        )

        # Company B Customer 101 -> TALLY-COMPB-101
        self.store.save_mapping(
            entity_type="customer",
            source_id="101",
            target_id="TALLY-COMPB-101",
            source_company_id="company_B",
            target_company_id="company_B",
        )

        # Lookup for Company A MUST return TALLY-COMPA-101
        ext_id_a = self.store.get_external_id("customer", "101", source_company_id="company_A")
        self.assertEqual(ext_id_a, "TALLY-COMPA-101")

        # Lookup for Company B MUST return TALLY-COMPB-101
        ext_id_b = self.store.get_external_id("customer", "101", source_company_id="company_B")
        self.assertEqual(ext_id_b, "TALLY-COMPB-101")

    def test_lock_manager_company_isolation(self):
        lock_key_a = self.lock_mgr.generate_lock_key("company_A", "customer", "forward", "101")
        lock_key_b = self.lock_mgr.generate_lock_key("company_B", "customer", "forward", "101")

        # Lock keys MUST be distinct
        self.assertNotEqual(lock_key_a, lock_key_b)

        # Worker 1 acquires lock for Company A
        acquired_a = self.lock_mgr.acquire_lock(lock_key_a, "worker-1")
        self.assertTrue(acquired_a)

        # Worker 2 acquires lock for Company B (Customer 101) -> MUST succeed and not be blocked!
        acquired_b = self.lock_mgr.acquire_lock(lock_key_b, "worker-2")
        self.assertTrue(acquired_b)

    def test_queue_deduplication_company_isolation(self):
        job_a = self.q_store.enqueue(entity_type="customer", entity_id="101", company_id="company_A")
        self.assertIsNotNone(job_a)

        # Enqueue identical entity_type/entity_id for Company B -> MUST succeed and not be deduplicated!
        job_b = self.q_store.enqueue(entity_type="customer", entity_id="101", company_id="company_B")
        self.assertIsNotNone(job_b)
        self.assertNotEqual(job_a, job_b)

    def test_integration_key_company_isolation(self):
        key_a = generate_integration_key("company_A", "customer", "101", "forward")
        key_b = generate_integration_key("company_B", "customer", "101", "forward")

        self.assertEqual(key_a, "company_a:customer:101:forward")
        self.assertEqual(key_b, "company_b:customer:101:forward")
        self.assertNotEqual(key_a, key_b)


if __name__ == "__main__":
    unittest.main()
