import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

from app.mapping.store import MappingStore
from app.sync.tally_to_rentasst import sync_tally_to_rentasst, is_tally_voucher_duplicate
from app.sync.idempotency import generate_integration_key


class TestReverseSyncHardening(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_reverse_sync.db")
        self.store = MappingStore(self.db_path)

    def tearDown(self):
        if hasattr(self, "store") and self.store:
            self.store.db.close()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_reverse_deduplication_by_integration_key(self):
        tally_guid = "TALLY-VOUCHER-GUID-999"
        rev_key = generate_integration_key("default", "invoice", tally_guid, "reverse")

        # Save mapping
        self.store.save_mapping(
            entity_type="invoice",
            source_id=tally_guid,
            target_id="101",
            integration_key=rev_key,
            status="synced",
        )

        voucher = {"tally_guid": tally_guid, "voucher_number": "INV-999", "voucher_type": "Sales"}
        is_dup = is_tally_voucher_duplicate(voucher, self.store)
        self.assertTrue(is_dup)

    def test_reverse_sync_preflight_validation_failure_dlq(self):
        mock_ra_client = MagicMock()
        mock_ext_client = MagicMock()
        mock_ext_client.cfg = MagicMock()

        # Mock fetcher returning voucher with invalid math (grand_total = -50)
        invalid_voucher = {
            "tally_guid": "GUID-INVALID-MATH",
            "alter_id": 5,
            "voucher_type": "Sales",
            "voucher_number": "INV-BAD",
            "party_name": "Test Party",
            "date": "2026-08-15",
            "amount": -50.0,  # Invalid negative amount!
        }

        with unittest.mock.patch("app.sync.tally_to_rentasst.TallyFetcher") as mock_fetcher_cls:
            mock_fetcher = MagicMock()
            mock_fetcher.fetch_vouchers.return_value = [invalid_voucher]
            mock_fetcher_cls.return_value = mock_fetcher

            stats = sync_tally_to_rentasst(
                ra_client=mock_ra_client,
                ext_client=mock_ext_client,
                store=self.store,
                force_full_sync=True,
            )

        # RentAsst API push_invoice MUST NOT be called!
        mock_ra_client.push_invoice.assert_not_called()
        self.assertEqual(stats["failed"], 1)

        # Must be recorded in Dead-Letter Queue
        dlqs = self.store.list_dead_letters(entity_type="invoice")
        self.assertEqual(len(dlqs), 1)
        self.assertIn("Validation Failure", dlqs[0]["error_message"])

    def test_reverse_sync_saves_mapping_only_after_confirmed_api_response(self):
        mock_ra_client = MagicMock()
        mock_ra_client.push_invoice.return_value = {"id": "CLOUD-INV-888"}
        mock_ext_client = MagicMock()
        mock_ext_client.cfg = MagicMock()

        valid_voucher = {
            "tally_guid": "GUID-SUCCESS-100",
            "alter_id": 10,
            "voucher_type": "Sales",
            "voucher_number": "INV-100",
            "party_name": "Acme Corp",
            "date": "2026-08-15",
            "amount": 15000.0,
        }

        with unittest.mock.patch("app.sync.tally_to_rentasst.TallyFetcher") as mock_fetcher_cls:
            mock_fetcher = MagicMock()
            mock_fetcher.fetch_vouchers.return_value = [valid_voucher]
            mock_fetcher_cls.return_value = mock_fetcher

            stats = sync_tally_to_rentasst(
                ra_client=mock_ra_client,
                ext_client=mock_ext_client,
                store=self.store,
                force_full_sync=True,
            )

        # RentAsst API push_invoice MUST be called once
        mock_ra_client.push_invoice.assert_called_once()
        self.assertEqual(stats["created"], 1)

        # SQLite mapping MUST be written with CLOUD-INV-888
        rev_key = generate_integration_key("default", "invoice", "GUID-SUCCESS-100", "reverse")
        mapping = self.store.find_by_integration_key(rev_key)
        self.assertIsNotNone(mapping)
        self.assertEqual(mapping["target_id"], "CLOUD-INV-888")


if __name__ == "__main__":
    unittest.main()
