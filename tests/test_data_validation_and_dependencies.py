import os
import shutil
import tempfile
import unittest

from app.validation.validator import PayloadValidator
from app.sync.dependencies import DependencyResolver, MissingDependencyException
from app.mapping.store import MappingStore
from app.queue.queue_store import QueueStore
from app.queue.worker import QueueWorker
from app.sync.base import run_sync_pipeline


class TestDataValidationAndDependencies(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_val_deps.db")
        self.store = MappingStore(self.db_path)
        self.q_store = QueueStore(self.db_path)

    def tearDown(self):
        if hasattr(self, "store") and self.store:
            self.store.db.close()
        if hasattr(self, "q_store") and self.q_store:
            self.q_store.db.close()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_invoice_math_validation(self):
        # 1. Valid Invoice Math
        valid_inv = {
            "id": 101,
            "number": "INV-101",
            "customer_id": 50,
            "subtotal": 1000.0,
            "tax_amount": 180.0,
            "extra_charges": 20.0,
            "discount": 50.0,
            "grand_total": 1150.0,  # 1000 + 180 + 20 - 50 = 1150
        }
        is_val, err = PayloadValidator.validate_invoice(valid_inv)
        self.assertTrue(is_val)
        self.assertIsNone(err)

        # 2. Invalid Invoice Math (subtotal 1000 + tax 180 = 1180 != grand_total 2500)
        invalid_inv = {
            "id": 102,
            "number": "INV-102",
            "customer_id": 50,
            "subtotal": 1000.0,
            "tax_amount": 180.0,
            "grand_total": 2500.0,
        }
        is_val, err = PayloadValidator.validate_invoice(invalid_inv)
        self.assertFalse(is_val)
        self.assertIn("Invoice math validation failure", err)

    def test_preflight_validation_routes_invalid_payload_to_dlq(self):
        """
        Invalid payload must be caught by pre-flight validation and routed directly to DLQ
        without invoking creation sync_func.
        """
        invalid_invoices = [{
            "id": 999,
            "number": "INV-BAD",
            "customer_id": 1,
            "subtotal": 500,
            "tax_amount": 90,
            "grand_total": 9999,  # Mismatch!
        }]

        creation_call_count = 0

        def mock_sync_func(item):
            nonlocal creation_call_count
            creation_call_count += 1
            return f"TALLY-INV-{item['id']}"

        stats = run_sync_pipeline(
            entity_type="invoice",
            fetch_func=lambda: invalid_invoices,
            sync_func=mock_sync_func,
            store=self.store,
        )

        # Creation func MUST NOT be called
        self.assertEqual(creation_call_count, 0)
        self.assertEqual(stats["failed"], 1)

        # Item MUST be stored in Dead-Letter Queue
        dlqs = self.store.list_dead_letters(entity_type="invoice")
        self.assertEqual(len(dlqs), 1)
        self.assertIn("Validation Failure", dlqs[0]["error_message"])

    def test_missing_customer_dependency_for_invoice(self):
        # Invoice for Customer ID 505 when Customer 505 mapping is NOT present
        inv_data = {"id": 1, "number": "INV-1", "customer_id": "505"}

        has_deps, reason, missing_ent, missing_id = DependencyResolver.check_dependencies(
            entity_type="invoice",
            data=inv_data,
            store=self.store,
            source_company_id="default",
        )
        self.assertFalse(has_deps)
        self.assertEqual(missing_ent, "customer")
        self.assertEqual(missing_id, "505")

        # Now save Customer 505 mapping in SQLite
        self.store.save_mapping("customer", "505", "TALLY-CUST-505", status="synced")

        # Re-check dependency -> must pass now!
        has_deps_now, _, _, _ = DependencyResolver.check_dependencies(
            entity_type="invoice",
            data=inv_data,
            store=self.store,
            source_company_id="default",
        )
        self.assertTrue(has_deps_now)

    def test_missing_equipment_dependency_for_rental_order(self):
        """
        Confirmed live: the scheduler enqueues equipment and rental_orders sync jobs
        concurrently (SyncScheduler._sync_job), so a rental order can be forward-synced
        before its rent item's own equipment has finished syncing to Tally, producing a
        permanent "Stock Item does not exist!" dead-letter for what is really just a
        timing race. The dependency check must catch this before sync_func runs.
        """
        self.store.save_mapping("customer", "14", "Felix", status="synced")
        order_data = {
            "id": 22,
            "customer_id": 14,
            "items": [{"name": "Dell Mouse", "asset_id": 16, "quantity": 20}],
        }

        has_deps, reason, missing_ent, missing_id = DependencyResolver.check_dependencies(
            entity_type="rental_order",
            data=order_data,
            store=self.store,
            source_company_id="default",
        )
        self.assertFalse(has_deps)
        self.assertEqual(missing_ent, "equipment")
        self.assertEqual(missing_id, "16")

        self.store.save_mapping("equipment", "16", "TALLY-ID-16", status="synced")

        has_deps_now, _, _, _ = DependencyResolver.check_dependencies(
            entity_type="rental_order",
            data=order_data,
            store=self.store,
            source_company_id="default",
        )
        self.assertTrue(has_deps_now)

    def test_missing_equipment_dependency_for_invoice(self):
        self.store.save_mapping("customer", "14", "Felix", status="synced")
        invoice_data = {
            "id": 34,
            "customer_id": 14,
            "items": [{"name": "Dell Mouse", "asset_id": 16, "quantity": 20}],
        }

        has_deps, reason, missing_ent, missing_id = DependencyResolver.check_dependencies(
            entity_type="invoice",
            data=invoice_data,
            store=self.store,
            source_company_id="default",
        )
        self.assertFalse(has_deps)
        self.assertEqual(missing_ent, "equipment")
        self.assertEqual(missing_id, "16")

    def test_worker_transitions_missing_dependency_to_waiting_state(self):
        """
        When a worker executes a job whose dependency is missing,
        it raises MissingDependencyException and transitions job to WAITING_FOR_DEPENDENCY state.
        """
        def mock_executor(entity_type):
            # Simulate invoice execution with missing dependency
            raise MissingDependencyException(
                "Missing Customer dependency mapping (Customer ID: '900') for Invoice sync",
                missing_entity="customer",
                missing_id="900",
            )

        worker = QueueWorker(self.q_store, sync_executor=mock_executor)

        job_id = self.q_store.enqueue(entity_type="invoice", entity_id="INV-DEP-TEST")
        claimed = self.q_store.claim_next_job()

        worker._process_job(claimed)

        # Job must be in WAITING_FOR_DEPENDENCY state (NOT DLQ or FAILED!)
        j = [j for j in self.q_store.list_recent_jobs() if j["job_id"] == job_id][0]
        self.assertEqual(j["status"], "WAITING_FOR_DEPENDENCY")
        self.assertIn("Missing Customer dependency", j["last_error"])

        # DLQ must remain empty!
        dlqs = self.store.list_dead_letters()
        self.assertEqual(len(dlqs), 0)


if __name__ == "__main__":
    unittest.main()
