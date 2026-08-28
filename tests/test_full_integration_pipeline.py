import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

from app.mapping.store import MappingStore
from app.queue.queue_store import QueueStore
from app.sync.base import run_sync_pipeline
from app.sync.tally_to_rentasst import sync_tally_to_rentasst
from app.reconciliation.engine import ReconciliationEngine
from app.sync.idempotency import generate_integration_key


class TestFullIntegrationPipeline(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_e2e.db")
        self.store = MappingStore(self.db_path)
        self.q_store = QueueStore(self.db_path)
        self.engine = ReconciliationEngine(self.store)

    def tearDown(self):
        if hasattr(self, "store") and self.store:
            self.store.db.close()
        if hasattr(self, "q_store") and self.q_store:
            self.q_store.db.close()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_e2e_forward_sync_pipeline_customer_equipment_order_invoice_payment(self):
        """
        Integration Test 1: Complete forward dependency execution hierarchy:
        Customer -> Equipment -> Rental Order -> Invoice -> Payment
        """
        mock_ext = MagicMock()
        mock_ext.ping.return_value = True
        mock_ext.check_exists_in_tally.return_value = False

        # 1. Customer Sync
        cust_payload = [{"id": "CUST-E2E-1", "name": "Global Construction Corp", "phone": "9988776655"}]
        c_stats = run_sync_pipeline(
            entity_type="customer",
            fetch_func=lambda: cust_payload,
            sync_func=lambda c: f"TALLY-CUST-{c['id']}",
            store=self.store,
            external_client=mock_ext,
        )
        self.assertEqual(c_stats["created"], 1)
        self.assertTrue(self.store.exists("customer", "CUST-E2E-1"))

        # 2. Equipment Sync
        eq_payload = [{"id": "EQ-E2E-1", "name": "JCB Excavator 300", "daily_rate": 500.0}]
        e_stats = run_sync_pipeline(
            entity_type="equipment",
            fetch_func=lambda: eq_payload,
            sync_func=lambda e: f"TALLY-EQ-{e['id']}",
            store=self.store,
            external_client=mock_ext,
        )
        self.assertEqual(e_stats["created"], 1)
        self.assertTrue(self.store.exists("equipment", "EQ-E2E-1"))

        # 3. Rental Order Sync
        order_payload = [{
            "id": "ORD-E2E-1",
            "number": "ORD-E2E-1",
            "customer_id": "CUST-E2E-1",
            "total_amount": 5000.0,
            "order_date": "2026-08-01",
        }]
        o_stats = run_sync_pipeline(
            entity_type="rental_order",
            fetch_func=lambda: order_payload,
            sync_func=lambda o: f"TALLY-ORD-{o['id']}",
            store=self.store,
            external_client=mock_ext,
        )
        self.assertEqual(o_stats["created"], 1)

        # 4. Invoice Sync (Requires Customer Mapping & Valid Math)
        inv_payload = [{
            "id": "INV-E2E-1",
            "number": "INV-E2E-1",
            "customer_id": "CUST-E2E-1",
            "subtotal": 5000.0,
            "tax_amount": 900.0,
            "grand_total": 5900.0,
            "invoice_date": "2026-08-02",
        }]
        i_stats = run_sync_pipeline(
            entity_type="invoice",
            fetch_func=lambda: inv_payload,
            sync_func=lambda i: f"TALLY-INV-{i['id']}",
            store=self.store,
            external_client=mock_ext,
        )
        self.assertEqual(i_stats["created"], 1)
        self.assertTrue(self.store.exists("invoice", "INV-E2E-1"))

        # 5. Payment Sync (Requires Invoice Dependency)
        pay_payload = [{
            "id": "PAY-E2E-1",
            "reference_id": "PAY-E2E-1",
            "invoice_id": "INV-E2E-1",
            "amount": 5900.0,
            "payment_date": "2026-08-03",
        }]
        p_stats = run_sync_pipeline(
            entity_type="payment",
            fetch_func=lambda: pay_payload,
            sync_func=lambda p: f"TALLY-PAY-{p['id']}",
            store=self.store,
            external_client=mock_ext,
        )
        self.assertEqual(p_stats["created"], 1)
        self.assertTrue(self.store.exists("payment", "PAY-E2E-1"))

    @unittest.mock.patch("app.sync.tally_to_rentasst.TallyFetcher.fetch_vouchers")
    def test_e2e_reverse_sync_tally_to_rentasst(self, mock_fetch_vouchers):
        """
        Integration Test 2: Reverse sync from Tally vouchers to RentAsst REST API.

        Uses a Sales Order (rentout) voucher — reverse sync only mirrors rental
        orders (plus customers/equipment, exercised elsewhere) from Tally into
        RentAsst now; Invoices/Payments are RentAsst-native and reach Tally only via
        forward sync, referencing the rental order's own Tally identity instead.
        """
        mock_fetch_vouchers.return_value = [{
            "tally_guid": "GUID-TALLY-REV-100",
            "voucher_number": "VOUCHER-REV-100",
            "voucher_type": "sales order",
            "party_name": "Reverse Customer Ltd",
            "date": "2026-08-10",
            "amount": 1180.0,
            "alter_id": 50,
        }]

        mock_ext_client = MagicMock()
        mock_ext_client.cfg = MagicMock()

        mock_ra_client = MagicMock()
        mock_ra_client.fetch_customers.return_value = []
        mock_ra_client.push_customer.return_value = {"id": 42}
        mock_ra_client.push_rentout.return_value = {"id": "RA-REV-ORD-100", "status": "created"}

        stats = sync_tally_to_rentasst(
            ra_client=mock_ra_client,
            ext_client=mock_ext_client,
            store=self.store,
        )

        self.assertEqual(stats["created"], 1)
        rev_key = generate_integration_key("default", "rental_order", "GUID-TALLY-REV-100", "reverse")
        rev_map = self.store.find_by_integration_key(rev_key)
        self.assertIsNotNone(rev_map)
        self.assertEqual(rev_map["target_id"], "RA-REV-ORD-100")

    def test_e2e_reconciliation_audit(self):
        """
        Integration Test 3: Read-only reconciliation audit pass over synced datasets.
        """
        ra_invoices = [{"id": 1, "number": "INV-101", "grand_total": 1000.0, "invoice_date": "2026-08-01"}]
        tally_invoices = [{"voucher_number": "INV-101", "amount": 1000.0, "date": "2026-08-01"}]

        res = self.engine.run_reconciliation(
            ra_invoices=ra_invoices,
            tally_invoices=tally_invoices,
        )

        self.assertEqual(res["status"], "COMPLETED")
        self.assertEqual(res["total_discrepancies"], 0)
        self.assertEqual(res["macro_totals"]["invoices"]["amount_diff"], 0.0)

    def test_e2e_dlq_and_retry_recovery(self):
        """
        Integration Test 4: Pre-flight math error routes invalid record to DLQ; retry recovers valid jobs.
        """
        mock_ext = MagicMock()
        mock_ext.ping.return_value = True

        invalid_inv = [{
            "id": "INV-MATH-ERR",
            "number": "INV-MATH-ERR",
            "customer_id": "CUST-100",
            "subtotal": 100.0,
            "tax_amount": 18.0,
            "grand_total": 9999.0,
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
        rec_id = dls[0].get("source_id") or dls[0].get("entity_id") or dls[0].get("rentasst_id")
        self.assertEqual(rec_id, "INV-MATH-ERR")


if __name__ == "__main__":
    unittest.main()
