import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

from app.main import app
from app.mapping.store import MappingStore


class TestHealthMonitoring(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_health.db")
        self.store = MappingStore(self.db_path)
        
        # Attach state to FastAPI app
        app.state.data_dir = self.temp_dir
        app.state.db_path = self.db_path
        app.state.mapping_store = self.store

        mock_ra = MagicMock()
        mock_ra.ping.return_value = True
        mock_ra.config.rentasst_url = "http://localhost:8000/api"
        mock_ra.config.rentasst_tenant_id = "default"
        app.state.ra_client = mock_ra

        mock_ext = MagicMock()
        mock_ext.ping.return_value = True
        mock_ext.config.external_url = "http://localhost:9000"
        mock_ext.config.external_system_type = "tally"
        app.state.ext_client = mock_ext

        mock_worker = MagicMock()
        mock_worker.is_running = True
        mock_worker.current_job_info = "Idle"
        mock_worker.max_workers = 2
        app.state.worker = mock_worker

        mock_sched = MagicMock()
        mock_sched.is_running = True
        mock_sched.interval_minutes = 10
        app.state.scheduler = mock_sched

        self.client = TestClient(app)

    def tearDown(self):
        if hasattr(self, "store") and self.store:
            self.store.db.close()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_liveness_probe(self):
        res = self.client.get("/health/live")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "UP")

    def test_readiness_probe(self):
        res = self.client.get("/health/ready")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "READY")
        self.assertEqual(data["database"]["status"], "UP")

    def test_comprehensive_health_overview(self):
        res = self.client.get("/health")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "UP")

        comps = data["components"]
        self.assertEqual(comps["database"]["status"], "UP")
        self.assertEqual(comps["rentasst_api"]["status"], "UP")
        self.assertEqual(comps["tally_prime"]["status"], "UP")
        self.assertEqual(comps["worker"]["status"], "UP")
        self.assertEqual(comps["scheduler"]["status"], "UP")

    def test_rentasst_and_tally_health_probes(self):
        ra_res = self.client.get("/health/rentasst")
        self.assertEqual(ra_res.status_code, 200)
        self.assertEqual(ra_res.json()["status"], "UP")

        tally_res = self.client.get("/health/tally")
        self.assertEqual(tally_res.status_code, 200)
        self.assertEqual(tally_res.json()["status"], "UP")

    def test_health_payloads_do_not_expose_credentials(self):
        res = self.client.get("/health")
        raw_text = res.text.lower()
        self.assertNotIn("password", raw_text)
        self.assertNotIn("api_key", raw_text)
        self.assertNotIn("token", raw_text)


if __name__ == "__main__":
    unittest.main()
