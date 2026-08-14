import os
import shutil
import tempfile
import unittest

from app.mapping.store import MappingStore
from app.reconciliation.engine import ReconciliationEngine


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


if __name__ == "__main__":
    unittest.main()
