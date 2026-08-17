import unittest
from app.models.domain import AppConfig
from app.clients.rentasst_client import RentAsstClient


class TestCustomerDeduplication(unittest.TestCase):
    def setUp(self):
        self.cfg = AppConfig(
            rentasst_url="http://localhost:8000/api",
            rentasst_api_key="test_key",
            rentasst_tenant_id="test_tenant",
        )
        self.client = RentAsstClient(self.cfg)

    def test_deduplicate_identical_customer_records(self):
        """Tests that genuine duplicate records (same ID or same name+phone) are deduplicated."""
        raw_customers = [
            {"id": "1", "name": "Alice Smith", "mobile": "9876543210", "email": "alice@example.com"},
            {"id": "1", "name": "Alice Smith", "mobile": "9876543210", "email": "alice@example.com"}, # exact duplicate
            {"id": "2", "name": "Bob Jones", "mobile": "9123456780"},
            {"id": "3", "name": "Charlie Brown", "mobile": "9988776655"},
        ]
        result = self.client._deduplicate_customers(raw_customers)
        self.assertEqual(len(result), 3)
        ids = [c["id"] for c in result]
        self.assertEqual(ids, ["1", "2", "3"])

    def test_disambiguate_name_collisions_for_distinct_customers(self):
        """Tests that two different customers sharing the same name are disambiguated for Tally."""
        raw_customers = [
            {"id": "1", "name": "David Miller", "mobile": "9876543210"},
            {"id": "2", "name": "David Miller", "mobile": "9123456789"}, # same name, different person
            {"id": "3", "name": "Eva Green", "mobile": "9000000001"},
            {"id": "4", "name": "Frank White", "mobile": "9000000002"},
        ]
        result = self.client._deduplicate_customers(raw_customers)
        self.assertEqual(len(result), 4)
        tally_names = [c["tally_name"] for c in result]
        self.assertIn("David Miller (9876543210)", tally_names)
        self.assertIn("David Miller (9123456789)", tally_names)
        self.assertIn("Eva Green", tally_names)
        self.assertIn("Frank White", tally_names)


if __name__ == "__main__":
    unittest.main()
