import os
import shutil
import tempfile
import unittest

from app.sync.ownership import filter_payload_by_ownership, get_field_owner
from app.sync.conflicts import ConflictDetector
from app.mapping.store import MappingStore


class TestOwnershipAndConflicts(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_own_conf.db")
        self.store = MappingStore(self.db_path)
        self.detector = ConflictDetector(self.store)

    def tearDown(self):
        if hasattr(self, "store") and self.store:
            self.store.db.close()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_field_ownership_policy(self):
        self.assertEqual(get_field_owner("customer", "mobile"), "rentasst")
        self.assertEqual(get_field_owner("customer", "closing_balance"), "tally")
        self.assertEqual(get_field_owner("equipment", "rent_price"), "rentasst")
        self.assertEqual(get_field_owner("equipment", "opening_stock"), "tally")

    def test_filter_payload_by_ownership_forward(self):
        # Forward Sync: RentAsst -> Tally. Strips Tally-authoritative fields (e.g. closing_balance)
        raw_payload = {
            "id": 10,
            "name": "Customer X",
            "mobile": "9876543210",
            "closing_balance": "50000.00",
        }
        filtered = filter_payload_by_ownership("customer", "forward", raw_payload)
        self.assertIn("name", filtered)
        self.assertIn("mobile", filtered)
        self.assertNotIn("closing_balance", filtered)

    def test_filter_payload_by_ownership_reverse(self):
        # Reverse Sync: Tally -> RentAsst. Strips RentAsst-authoritative fields (e.g. name, mobile)
        raw_payload = {
            "id": 10,
            "name": "Customer X Modified in Tally",
            "closing_balance": "50000.00",
        }
        filtered = filter_payload_by_ownership("customer", "reverse", raw_payload)
        self.assertIn("closing_balance", filtered)
        self.assertNotIn("name", filtered)

    def test_conflict_detection_and_recording(self):
        ra_data = {"id": 100, "name": "RentAsst Name", "mobile": "9999988888"}
        tally_data = {"id": 100, "name": "Tally Name", "mobile": "9999988888"}

        conflicts = self.detector.detect_and_record_conflicts(
            entity_type="customer",
            entity_id="100",
            rentasst_data=ra_data,
            tally_data=tally_data,
        )

        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["field_name"], "name")

        # Verify recorded in DB
        db_conflicts = self.detector.list_conflicts(status_filter="OPEN")
        self.assertEqual(len(db_conflicts), 1)
        self.assertEqual(db_conflicts[0]["rentasst_value"], "RentAsst Name")
        self.assertEqual(db_conflicts[0]["tally_value"], "Tally Name")

    def test_conflict_resolution(self):
        # Record conflict
        c_entry = self.detector.record_conflict(
            entity_type="customer",
            entity_id="200",
            field_name="email",
            rentasst_value="user@rentasst.com",
            tally_value="user@tally.com",
        )

        cid = c_entry["id"]

        # Resolve conflict choosing RentAsst
        resolved = self.detector.resolve_conflict(cid, "use_rentasst")
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["status"], "RESOLVED_RENTASST")

        # Verify open conflicts query returns empty
        open_conflicts = self.detector.list_conflicts(status_filter="OPEN")
        self.assertEqual(len(open_conflicts), 0)


if __name__ == "__main__":
    unittest.main()
