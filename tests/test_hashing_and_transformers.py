import unittest

from app.sync.base import compute_payload_hash, extract_identifier
from app.sync.ownership import filter_payload_by_ownership, FIELD_OWNERSHIP_POLICY


class TestHashingAndTransformers(unittest.TestCase):
    def test_compute_payload_hash(self):
        payload_a = {"name": "Test Customer", "phone": "9876543210", "gstin": "33AAAAA0000A1Z5"}
        payload_b = {"gstin": "33AAAAA0000A1Z5", "name": "Test Customer", "phone": "9876543210"}

        hash_a = compute_payload_hash(payload_a)
        hash_b = compute_payload_hash(payload_b)

        # Hashes MUST be identical regardless of key order!
        self.assertEqual(hash_a, hash_b)
        self.assertEqual(len(hash_a), 64)  # SHA-256 hex string length

    def test_extract_identifier(self):
        cust = {"name": "Acme Rentals"}
        self.assertEqual(extract_identifier("customer", cust), "Acme Rentals")

        inv = {"id": 55, "number": "INV-2026-99"}
        self.assertEqual(extract_identifier("invoice", inv), "RENTAL-INV-55")

        pay = {"id": 88, "reference_id": "PAY-REF-777"}
        self.assertEqual(extract_identifier("payment", pay), "RENTAL-PAY-88")

    def test_filter_payload_by_ownership_forward(self):
        # Forward Sync: RentAsst -> Tally
        cust_payload = {
            "name": "RentAsst Customer Name",
            "phone": "9876543210",
            "opening_balance": 5000.0,
            "credit_limit": 100000.0,
        }

        filtered = filter_payload_by_ownership("customer", direction="forward", payload=cust_payload)

        # RentAsst authoritative fields MUST be preserved
        self.assertEqual(filtered.get("name"), "RentAsst Customer Name")
        self.assertEqual(filtered.get("phone"), "9876543210")

        # Tally authoritative fields MUST NOT be sent in forward direction
        self.assertNotIn("opening_balance", filtered)
        self.assertNotIn("credit_limit", filtered)

    def test_filter_payload_by_ownership_reverse(self):
        # Reverse Sync: Tally -> RentAsst
        cust_payload = {
            "name": "Tally Overwriting Name Attempt",
            "opening_balance": 5000.0,
            "credit_limit": 100000.0,
        }

        filtered = filter_payload_by_ownership("customer", direction="reverse", payload=cust_payload)

        # Tally authoritative fields MUST be preserved in reverse sync
        self.assertEqual(filtered.get("opening_balance"), 5000.0)
        self.assertEqual(filtered.get("credit_limit"), 100000.0)

        # RentAsst authoritative fields MUST NOT be overwritten by Tally reverse sync
        self.assertNotIn("name", filtered)


if __name__ == "__main__":
    unittest.main()
