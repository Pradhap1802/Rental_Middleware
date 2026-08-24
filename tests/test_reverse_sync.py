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

    def test_reverse_sync_pushes_invoice_line_items_after_creation(self):
        """
        RentAsst's invoice create endpoint silently drops an 'items' field (InvoiceItem is
        a separate resource) — line items must be pushed via push_invoice_items() after the
        invoice itself is created, with each Tally stock item resolved to a RentAsst
        asset_id via the equipment reverse-mapping.
        """
        self.store.save_mapping(
            entity_type="equipment",
            source_id="Moto G45",
            target_id="17",
            source_system="tally",
            target_system="rentasst",
        )

        mock_ra_client = MagicMock()
        mock_ra_client.push_invoice.return_value = {"id": "CLOUD-INV-200"}
        mock_ext_client = MagicMock()
        mock_ext_client.cfg = MagicMock()

        voucher = {
            "tally_guid": "GUID-WITH-ITEMS",
            "alter_id": 20,
            "voucher_type": "Sales",
            "voucher_number": "INV-200",
            "party_name": "Acme Corp",
            "date": "2026-08-20",
            "amount": 97.0,
            "items": [{"name": "Moto G45", "quantity": "1 Piece", "rate": "97.00/Piece", "amount": "97.00"}],
        }

        with unittest.mock.patch("app.sync.tally_to_rentasst.TallyFetcher") as mock_fetcher_cls:
            mock_fetcher = MagicMock()
            mock_fetcher.fetch_vouchers.return_value = [voucher]
            mock_fetcher_cls.return_value = mock_fetcher

            stats = sync_tally_to_rentasst(
                ra_client=mock_ra_client,
                ext_client=mock_ext_client,
                store=self.store,
                force_full_sync=True,
            )

        self.assertEqual(stats["created"], 1)
        mock_ra_client.push_invoice_items.assert_called_once()
        call_args = mock_ra_client.push_invoice_items.call_args
        self.assertEqual(call_args[0][0], "CLOUD-INV-200")
        pushed_items = call_args[0][1]
        self.assertEqual(len(pushed_items), 1)
        self.assertEqual(pushed_items[0]["asset_id"], 17)
        self.assertEqual(pushed_items[0]["quantity"], 1)
        self.assertEqual(pushed_items[0]["price"], 97.0)
        self.assertEqual(pushed_items[0]["total_price"], 97.0)

    def test_reverse_sync_updates_existing_invoice_instead_of_skipping(self):
        """
        A previously reverse-synced invoice must be refreshed (status/amount) on later
        runs, not silently skipped forever — and must NOT have push_invoice or
        push_invoice_items called again (create-once, items-bulk-create appends and would
        duplicate rows if repeated).
        """
        tally_guid = "GUID-EXISTING-INV"
        rev_key = generate_integration_key("default", "invoice", tally_guid, "reverse")
        self.store.save_mapping(
            entity_type="invoice",
            source_id=tally_guid,
            target_id="CLOUD-INV-EXISTING",
            source_system="tally",
            target_system="rentasst",
            integration_key=rev_key,
            status="synced",
        )

        mock_ra_client = MagicMock()
        mock_ext_client = MagicMock()
        mock_ext_client.cfg = MagicMock()

        # A matching receipt in the same batch, fully settling the invoice -> status=paid
        receipt = {
            "tally_guid": "GUID-RECEIPT-1",
            "alter_id": 21,
            "voucher_type": "Receipt",
            "voucher_number": "RCPT-1",
            "party_name": "Acme Corp",
            "date": "2026-08-20",
            "amount": 500.0,
            "bill_ref": "INV-EXISTING",
        }
        invoice_voucher = {
            "tally_guid": tally_guid,
            "alter_id": 22,
            "voucher_type": "Sales",
            "voucher_number": "INV-EXISTING",
            "party_name": "Acme Corp",
            "date": "2026-08-20",
            "amount": 500.0,
        }

        with unittest.mock.patch("app.sync.tally_to_rentasst.TallyFetcher") as mock_fetcher_cls:
            mock_fetcher = MagicMock()
            mock_fetcher.fetch_vouchers.return_value = [receipt, invoice_voucher]
            mock_fetcher_cls.return_value = mock_fetcher

            stats = sync_tally_to_rentasst(
                ra_client=mock_ra_client,
                ext_client=mock_ext_client,
                store=self.store,
                force_full_sync=True,
            )

        mock_ra_client.push_invoice.assert_not_called()
        mock_ra_client.push_invoice_items.assert_not_called()
        mock_ra_client.update_invoice.assert_called_once()
        update_args = mock_ra_client.update_invoice.call_args
        self.assertEqual(update_args[0][0], "CLOUD-INV-EXISTING")
        self.assertEqual(update_args[0][1]["status"], "paid")
        self.assertGreaterEqual(stats["updated"], 1)

    def test_reverse_sync_sales_order_uses_valid_rentasst_date_and_status_format(self):
        """
        RentAsst's create-rent-details endpoint (RentDetailsRequest) requires rent_from/
        rent_to as full 'Y-m-d H:i:s' timestamps and 'status' as numeric (0-10) — a
        date-only string or a string status like "confirmed" fails that validation with a
        422. Confirmed against RentAsst's own RentDetailsRequest::rules() and
        RentStatuses::UPCOMING source.
        """
        mock_ra_client = MagicMock()
        mock_ra_client.fetch_customers.return_value = []
        mock_ra_client.push_customer.return_value = {"id": 9}
        mock_ra_client.push_rentout.return_value = {"id": "CLOUD-ORD-1"}
        mock_ext_client = MagicMock()
        mock_ext_client.cfg = MagicMock()

        voucher = {
            "tally_guid": "GUID-SALES-ORDER-1",
            "alter_id": 30,
            "voucher_type": "Sales Order",
            "voucher_number": "ORD-1",
            "party_name": "Acme Corp",
            "date": "2026-08-20",
            "amount": 2500.0,
        }

        with unittest.mock.patch("app.sync.tally_to_rentasst.TallyFetcher") as mock_fetcher_cls:
            mock_fetcher = MagicMock()
            mock_fetcher.fetch_vouchers.return_value = [voucher]
            mock_fetcher_cls.return_value = mock_fetcher

            stats = sync_tally_to_rentasst(
                ra_client=mock_ra_client,
                ext_client=mock_ext_client,
                store=self.store,
                force_full_sync=True,
            )

        mock_ra_client.push_rentout.assert_called_once()
        pushed = mock_ra_client.push_rentout.call_args[0][0]
        self.assertEqual(pushed["rent_from"], "2026-08-20 00:00:00")
        self.assertEqual(pushed["rent_to"], "2026-08-20 00:00:00")
        self.assertEqual(pushed["order_booking_date"], "2026-08-20 00:00:00")
        self.assertIsInstance(pushed["status"], int)
        self.assertEqual(pushed["status"], 1)
        self.assertEqual(stats["created"], 1)
        mock_ra_client.push_rentout_items.assert_not_called()

    def test_reverse_sync_pushes_rentout_asset_lines_after_creation(self):
        """
        RentAsst's create-rent-details endpoint silently drops an 'items' field on the
        rentout payload (RentItem is a separate rent_items table, not a Rent column) —
        each Tally inventory line must be pushed via push_rentout_items() after the
        rentout exists, with the Tally stock item resolved to a RentAsst asset_id via the
        equipment reverse-mapping.
        """
        self.store.save_mapping(
            entity_type="equipment",
            source_id="Moto G45",
            target_id="17",
            source_system="tally",
            target_system="rentasst",
        )

        mock_ra_client = MagicMock()
        mock_ra_client.fetch_customers.return_value = []
        mock_ra_client.push_customer.return_value = {"id": 9}
        mock_ra_client.push_rentout.return_value = {"id": "CLOUD-ORD-2"}
        mock_ext_client = MagicMock()
        mock_ext_client.cfg = MagicMock()

        voucher = {
            "tally_guid": "GUID-SALES-ORDER-2",
            "alter_id": 31,
            "voucher_type": "Sales Order",
            "voucher_number": "ORD-2",
            "party_name": "Acme Corp",
            "date": "2026-08-20",
            "amount": 97.0,
            "items": [{"name": "Moto G45", "quantity": "1 Piece", "rate": "97.00/Piece", "amount": "97.00"}],
        }

        with unittest.mock.patch("app.sync.tally_to_rentasst.TallyFetcher") as mock_fetcher_cls:
            mock_fetcher = MagicMock()
            mock_fetcher.fetch_vouchers.return_value = [voucher]
            mock_fetcher_cls.return_value = mock_fetcher

            stats = sync_tally_to_rentasst(
                ra_client=mock_ra_client,
                ext_client=mock_ext_client,
                store=self.store,
                force_full_sync=True,
            )

        self.assertEqual(stats["created"], 1)
        mock_ra_client.push_rentout_items.assert_called_once()
        call_args = mock_ra_client.push_rentout_items.call_args
        self.assertEqual(call_args[0][0], "CLOUD-ORD-2")
        pushed_items = call_args[0][1]
        self.assertEqual(len(pushed_items), 1)
        self.assertEqual(pushed_items[0]["asset_id"], 17)
        self.assertEqual(pushed_items[0]["rented_quantity"], 1)
        self.assertEqual(pushed_items[0]["price"], 97.0)
        self.assertEqual(pushed_items[0]["total_price"], 97.0)


if __name__ == "__main__":
    unittest.main()
