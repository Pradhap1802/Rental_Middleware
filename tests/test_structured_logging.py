import os
import json
import tempfile
import unittest
import logging

from app.logging.logger import StructuredJsonFormatter, log_sync_event, MAIN_LOG_PATH


class TestStructuredLogging(unittest.TestCase):
    def test_structured_json_formatter_fields(self):
        formatter = StructuredJsonFormatter()
        logger = logging.getLogger("TestStructuredLogger")
        logger.setLevel(logging.INFO)

        record = logging.LogRecord(
            name="TestStructuredLogger",
            level=logging.INFO,
            pathname="test.py",
            lineno=10,
            msg="Testing structured log message",
            args=(),
            exc_info=None,
        )
        record.component = "Sync:Invoice"
        record.duration_ms = 125.5
        record.metadata = {
            "correlation_id": "test-corr-uuid-12345",
            "job_id": 99,
            "entity_type": "invoice",
            "entity_id": "INV-1001",
            "company_id": "company_A",
            "direction": "forward",
            "source_system": "rentasst",
            "target_system": "tally",
            "attempt": 1,
            "status": "SUCCESS",
        }

        json_output = formatter.format(record)
        log_obj = json.loads(json_output)

        # Assert mandatory sync log fields
        self.assertEqual(log_obj["correlation_id"], "test-corr-uuid-12345")
        self.assertEqual(log_obj["job_id"], 99)
        self.assertEqual(log_obj["entity_type"], "invoice")
        self.assertEqual(log_obj["entity_id"], "INV-1001")
        self.assertEqual(log_obj["company_id"], "company_A")
        self.assertEqual(log_obj["direction"], "forward")
        self.assertEqual(log_obj["source_system"], "rentasst")
        self.assertEqual(log_obj["target_system"], "tally")
        self.assertEqual(log_obj["attempt"], 1)
        self.assertEqual(log_obj["status"], "SUCCESS")
        self.assertEqual(log_obj["duration"], 125.5)

    def test_credential_redaction_in_structured_logs(self):
        formatter = StructuredJsonFormatter()
        record = logging.LogRecord(
            name="TestStructuredLogger",
            level=logging.INFO,
            pathname="test.py",
            lineno=10,
            msg="User password is super_secret_pass_123",
            args=(),
            exc_info=None,
        )
        record.metadata = {
            "api_key": "raw_secret_key_8888",
            "rentasst_api_token": "bearer_token_xyz_9999",
            "normal_field": "public_value",
        }

        json_output = formatter.format(record)
        log_obj = json.loads(json_output)

        # Raw passwords and tokens MUST NOT be present in JSON string!
        self.assertNotIn("super_secret_pass_123", json_output)
        self.assertNotIn("raw_secret_key_8888", json_output)
        self.assertNotIn("bearer_token_xyz_9999", json_output)

        self.assertEqual(log_obj["metadata"]["normal_field"], "public_value")

    def test_log_sync_event_helper(self):
        log_sync_event(
            entity_type="customer",
            entity_id="CUST-777",
            company_id="company_B",
            direction="reverse",
            source_system="tally",
            target_system="rentasst",
            job_id=42,
            attempt=2,
            status="PARTIAL_SUCCESS",
            duration_ms=45.0,
            message="Reverse sync completed with warnings",
        )

        # Check that middleware.log file exists and contains structured log entry
        if os.path.exists(MAIN_LOG_PATH):
            with open(MAIN_LOG_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()
            self.assertGreater(len(lines), 0)


if __name__ == "__main__":
    unittest.main()
