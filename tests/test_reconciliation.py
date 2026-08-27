import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from app.mapping.store import MappingStore
from app.reconciliation.engine import ReconciliationEngine
from app.models.domain import AppConfig


class TestReconciliationEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_reconciliation.db")
        self.store = MappingStore(self.db_path)
        self.engine = ReconciliationEngine(self.store)

    def tearDown(self):
        if hasattr(self, "store") and self.store:
            self.store.db.close()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_read_only_safety_and_macro_totals(self):
        ra_invoices = [
            {"id": 1, "number": "INV-001", "grand_total": 1000.0, "tax_amount": 180.0, "invoice_date": "2026-08-01"},
            {"id": 2, "number": "INV-002", "grand_total": 2000.0, "tax_amount": 360.0, "invoice_date": "2026-08-02"},
        ]
        tally_invoices = [
            {"voucher_number": "INV-001", "amount": 1000.0, "tax_amount": 180.0, "date": "2026-08-01"},
            {"voucher_number": "INV-003", "amount": 1500.0, "tax_amount": 270.0, "date": "2026-08-03"},  # Missing in RA!
        ]

        # Initial mapping count
        with self.store.db.get_connection() as c:
            init_count = c.execute("SELECT COUNT(*) FROM mapping").fetchone()[0]

        res = self.engine.run_reconciliation(
            ra_invoices=ra_invoices,
            tally_invoices=tally_invoices,
        )

        # 1. Assert Read-Only: Mapping count MUST remain unchanged!
        with self.store.db.get_connection() as c:
            final_count = c.execute("SELECT COUNT(*) FROM mapping").fetchone()[0]
        self.assertEqual(init_count, final_count)

        # 2. Assert Macro Financial Totals
        macro = res["macro_totals"]["invoices"]
        self.assertEqual(macro["rentasst_count"], 2)
        self.assertEqual(macro["tally_count"], 2)
        self.assertEqual(macro["rentasst_amount_total"], 3000.0)
        self.assertEqual(macro["tally_amount_total"], 2500.0)
        self.assertEqual(macro["amount_diff"], 500.0)

        # 3. Assert Mismatch Types Captured
        discrepancies = res["discrepancies"]
        m_types = [d["mismatch_type"] for d in discrepancies]
        self.assertIn("MISSING_IN_TALLY", m_types)   # INV-002 missing in Tally
        self.assertIn("MISSING_IN_RENTASST", m_types) # INV-003 missing in RentAsst

    def test_amount_and_date_mismatch_detection(self):
        ra_invoices = [
            {"id": 10, "number": "INV-100", "grand_total": 5000.0, "invoice_date": "2026-08-10"},
        ]
        tally_invoices = [
            {"voucher_number": "INV-100", "amount": 5500.0, "date": "2026-08-12"}, # Amount & Date Mismatch!
        ]

        res = self.engine.run_reconciliation(
            ra_invoices=ra_invoices,
            tally_invoices=tally_invoices,
        )

        discrepancies = res["discrepancies"]
        self.assertEqual(len(discrepancies), 2)
        m_types = [d["mismatch_type"] for d in discrepancies]
        self.assertIn("AMOUNT_MISMATCH", m_types)
        self.assertIn("DATE_MISMATCH", m_types)

    def test_customer_mismatch_detection(self):
        ra_cust = [{"id": 1, "name": "Client Alpha"}]
        tally_cust = [{"name": "Client Beta"}]

        res = self.engine.run_reconciliation(
            ra_customers=ra_cust,
            tally_customers=tally_cust,
        )

        discrepancies = res["discrepancies"]
        self.assertEqual(len(discrepancies), 2)
        m_types = [d["mismatch_type"] for d in discrepancies]
        self.assertIn("MISSING_IN_TALLY", m_types)
        self.assertIn("MISSING_IN_RENTASST", m_types)

    def test_payment_mismatch_detection(self):
        ra_payments = [{"id": 1, "reference_id": "PAY-001", "amount": 500.0}]
        tally_payments = [{"voucher_number": "PAY-001", "amount": 450.0}]

        res = self.engine.run_reconciliation(ra_payments=ra_payments, tally_payments=tally_payments)

        discrepancies = res["discrepancies"]
        self.assertEqual(len(discrepancies), 1)
        self.assertEqual(discrepancies[0]["mismatch_type"], "AMOUNT_MISMATCH")
        self.assertEqual(discrepancies[0]["entity_type"], "payment")

    def test_equipment_mismatch_detection(self):
        ra_equipment = [{"id": 1, "name": "Generator 500kVA"}]
        tally_equipment = [{"name": "Excavator CAT320"}]

        res = self.engine.run_reconciliation(ra_equipment=ra_equipment, tally_equipment=tally_equipment)

        discrepancies = res["discrepancies"]
        self.assertEqual(len(discrepancies), 2)
        m_types = [d["mismatch_type"] for d in discrepancies]
        self.assertIn("MISSING_IN_TALLY", m_types)
        self.assertIn("MISSING_IN_RENTASST", m_types)

    def test_rental_order_mismatch_detection(self):
        ra_rental_orders = [{"id": 1, "number": "ORD-001"}]
        tally_rental_orders = [{"voucher_number": "ORD-002"}]

        res = self.engine.run_reconciliation(ra_rental_orders=ra_rental_orders, tally_rental_orders=tally_rental_orders)

        discrepancies = res["discrepancies"]
        self.assertEqual(len(discrepancies), 2)
        m_types = [d["mismatch_type"] for d in discrepancies]
        self.assertIn("MISSING_IN_TALLY", m_types)
        self.assertIn("MISSING_IN_RENTASST", m_types)


class TestReconciliationRouteWiring(unittest.TestCase):
    """
    Regression coverage for app/api/reconciliation_routes.py's trigger_reconciliation:
    it previously fetched ext_client.tally.fetch_companies() for "tally_customers" — a
    list of Tally COMPANY names, not customer ledgers — so every RentAsst customer was
    reported MISSING_IN_TALLY regardless of actual sync state, and Tally-side
    invoices/payments/rental orders were never fetched at all (always empty). This
    verifies the fix pulls real data via TallyFetcher instead.
    """

    def setUp(self):
        from app.main import app
        self.app = app
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_reconciliation_route.db")
        self.store = MappingStore(self.db_path)

        app.state.data_dir = self.temp_dir
        app.state.db_path = self.db_path
        app.state.mapping_store = self.store

        self.mock_ra = MagicMock()
        self.mock_ra.fetch_customers.return_value = [{"id": 1, "name": "Acme Rentals"}]
        self.mock_ra.fetch_invoices.return_value = []
        self.mock_ra.fetch_payments.return_value = []
        self.mock_ra.fetch_equipment.return_value = []
        self.mock_ra.fetch_rental_orders.return_value = []
        app.state.ra_client = self.mock_ra

        self.mock_ext = MagicMock()
        self.mock_ext.cfg = AppConfig(external_url="http://localhost:9000", external_system_type="tally")
        app.state.ext_client = self.mock_ext

        self.client = TestClient(app, headers={"X-Middleware-Key": app.state.api_key})

    def tearDown(self):
        if hasattr(self, "store") and self.store:
            self.store.db.close()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_route_uses_real_tally_ledgers_not_companies(self):
        mock_fetcher = MagicMock()
        mock_fetcher.fetch_ledgers.return_value = [{"name": "Acme Rentals"}]
        mock_fetcher.fetch_stock_items.return_value = []
        mock_fetcher.fetch_vouchers.return_value = []

        with patch("app.api.reconciliation_routes.TallyFetcher", return_value=mock_fetcher):
            resp = self.client.post("/api/reconciliation/run")

        self.assertEqual(resp.status_code, 200)
        mock_fetcher.fetch_ledgers.assert_called_once()
        self.mock_ext.tally.fetch_companies.assert_not_called()

        data = resp.json()
        cust_discrepancies = [d for d in data["discrepancies"] if d["entity_type"] == "customer"]
        self.assertEqual(cust_discrepancies, [])

    def test_route_partitions_vouchers_by_type_for_invoices_payments_and_rental_orders(self):
        mock_fetcher = MagicMock()
        mock_fetcher.fetch_ledgers.return_value = []
        mock_fetcher.fetch_stock_items.return_value = []
        mock_fetcher.fetch_vouchers.return_value = [
            {"voucher_number": "INV-1", "voucher_type": "Sales", "amount": 100},
            {"voucher_number": "PAY-1", "voucher_type": "Receipt", "amount": 50},
            {"voucher_number": "ORD-1", "voucher_type": "Sales Order", "amount": 100},
            {"voucher_number": "STK-1", "voucher_type": "Physical Stock", "amount": 0},
        ]

        with patch("app.api.reconciliation_routes.TallyFetcher", return_value=mock_fetcher):
            resp = self.client.post("/api/reconciliation/run")

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        # Every voucher is "MISSING_IN_RENTASST" here since ra_* fetches are all empty —
        # this only checks each voucher landed in the right bucket, not amount matching.
        by_entity = {}
        for d in data["discrepancies"]:
            by_entity.setdefault(d["entity_type"], []).append(d["tally_value"])
        self.assertTrue(any("INV-1" in str(v) for v in by_entity.get("invoice", [])))
        self.assertTrue(any("PAY-1" in str(v) for v in by_entity.get("payment", [])))
        self.assertTrue(any("ORD-1" in str(v) for v in by_entity.get("rental_order", [])))
        # The Physical Stock voucher isn't any of invoice/payment/rental_order/equipment
        all_values = [v for vals in by_entity.values() for v in vals]
        self.assertFalse(any("STK-1" in str(v) for v in all_values))


if __name__ == "__main__":
    unittest.main()
