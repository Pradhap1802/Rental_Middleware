import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.mapping.store import MappingStore
from app.queue.queue_store import QueueStore


class TestProductionDashboard(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_dash.db")
        self.store = MappingStore(self.db_path)
        self.q_store = QueueStore(self.db_path)

        app.state.data_dir = self.temp_dir
        app.state.db_path = self.db_path
        app.state.mapping_store = self.store

        mock_ra = MagicMock()
        mock_ra.ping.return_value = True
        app.state.ra_client = mock_ra

        mock_ext = MagicMock()
        mock_ext.ping.return_value = True
        app.state.ext_client = mock_ext

        mock_worker = MagicMock()
        mock_worker.is_running = True
        mock_worker.current_job_info = "Syncing Invoices"
        app.state.worker = mock_worker

        mock_sched = MagicMock()
        mock_sched.is_running = True
        mock_sched.is_paused = False
        app.state.scheduler = mock_sched

        self.client = TestClient(app, headers={"X-Middleware-Key": app.state.api_key})

    def tearDown(self):
        if hasattr(self, "store") and self.store:
            self.store.db.close()
        if hasattr(self, "q_store") and self.q_store:
            self.q_store.db.close()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_production_status_api_structure(self):
        # Insert sample mapping data
        self.store.save_mapping("customer", "100", "TALLY-CUST-100")
        self.store.save_mapping("invoice", "200", "TALLY-INV-200")

        # Insert sample jobs in sync_queue
        self.q_store.enqueue(entity_type="customer", entity_id="100")
        self.q_store.enqueue(entity_type="invoice", entity_id="200")

        res = self.client.get("/api/status")
        self.assertEqual(res.status_code, 200)
        data = res.json()

        # 1. System Health
        sys_h = data["system_health"]
        self.assertTrue(sys_h["rentasst_status"])
        self.assertTrue(sys_h["tally_status"])
        self.assertTrue(sys_h["database_status"])
        self.assertEqual(sys_h["scheduler_status"], "active")
        self.assertEqual(sys_h["worker_status"], "UP")
        self.assertEqual(sys_h["running_job"], "Syncing Invoices")

        # 2. Entity Sync Status
        es = data["entity_sync_status"]
        self.assertIn("customers", es)
        self.assertIn("equipment", es)
        self.assertIn("invoices", es)
        self.assertIn("payments", es)
        self.assertIn("reverse_sync", es)
        self.assertEqual(es["customers"]["synced_count"], 1)
        self.assertEqual(es["invoices"]["synced_count"], 1)

        # 3. Job Status Breakdown
        jb = data["job_status_breakdown"]
        self.assertIn("PENDING", jb)
        self.assertIn("PROCESSING", jb)
        self.assertIn("SUCCESS", jb)
        self.assertIn("FAILED", jb)
        self.assertIn("DLQ", jb)

        # 4. Reconciliation Metrics
        rm = data["reconciliation_metrics"]
        self.assertIn("matched", rm)
        self.assertIn("missing", rm)
        self.assertIn("mismatched", rm)
        self.assertIn("unresolved_conflicts", rm)

        # 5. Performance Metrics
        pm = data["performance_metrics"]
        self.assertIn("records_processed", pm)
        self.assertIn("failure_rate_percent", pm)


if __name__ == "__main__":
    unittest.main()
