import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

from app.mapping.store import MappingStore
from app.sync.tally_to_rentasst import (
    sync_tally_to_rentasst,
    is_tally_voucher_duplicate,
    _synthetic_mobile_number,
    _extract_primary_mobile,
    _extract_address_payload,
    _customer_contact_hash,
    _equipment_change_hash,
)
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

    def test_synthetic_mobile_number_is_stable_across_calls(self):
        """
        Must be deterministic (not Python's per-process-salted hash()) — a retry after a
        crash between push_customer() succeeding and the mapping being saved locally must
        regenerate the exact same placeholder number for the same party name, or the
        customer could get duplicated in RentAsst.
        """
        first = _synthetic_mobile_number("Acme Rentals Pvt Ltd")
        second = _synthetic_mobile_number("Acme Rentals Pvt Ltd")
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("900"))
        self.assertEqual(len(first), 10)

        different = _synthetic_mobile_number("Different Party Name")
        self.assertNotEqual(first, different)

    def test_extract_primary_mobile_prefers_ledgermobile_over_comma_joined_ledgerphone(self):
        """
        LEDGERPHONE can hold multiple comma-joined numbers (the forward sync writes every
        mobile/alternate-mobile RentAsst has into it, e.g. "08056997998, 08056997998") —
        confirmed live. Stripping non-digits from that whole string concatenates every
        number into one garbled value ("0805699799808056997998") instead of the real
        number, which is exactly the "synced a different number" bug. LEDGERMOBILE (a
        single clean field) must be preferred.
        """
        ledger = {"mobile": "08056997998", "phone": "08056997998, 08056997998"}
        self.assertEqual(_extract_primary_mobile(ledger, "Test"), "08056997998")

    def test_extract_primary_mobile_falls_back_to_first_phone_segment_only(self):
        """A ledger with no LEDGERMOBILE at all must take only the FIRST comma-separated
        number from LEDGERPHONE, never the whole joined string."""
        ledger = {"mobile": "", "phone": "9876543210, 9123456789"}
        self.assertEqual(_extract_primary_mobile(ledger, "Test"), "9876543210")

    def test_extract_primary_mobile_falls_back_to_synthetic_when_nothing_usable(self):
        ledger = {"mobile": "", "phone": ""}
        result = _extract_primary_mobile(ledger, "Some Party")
        self.assertEqual(result, _synthetic_mobile_number("Some Party"))

    def test_extract_address_payload_joins_lines_and_keeps_structured_fields(self):
        ledger = {
            "address_lines": ["Hosur", "Near IT Park", "Hosur"],
            "state": "Tamil Nadu",
            "country": "India",
            "pincode": "635109",
        }
        payload = _extract_address_payload(ledger)
        self.assertEqual(payload["address1"], "Hosur, Near IT Park, Hosur")
        self.assertEqual(payload["state"], "Tamil Nadu")
        self.assertEqual(payload["country"], "India")
        self.assertEqual(payload["zipcode"], "635109")
        self.assertEqual(payload["full_address"], "India, Hosur, Near IT Park, Hosur, Tamil Nadu, 635109")

    def test_extract_address_payload_returns_none_when_tally_has_no_address_data(self):
        """Must omit the address field entirely (not push an empty object that would
        overwrite a real RentAsst address with nothing) when Tally has none."""
        self.assertIsNone(_extract_address_payload({"address_lines": [], "state": "", "country": "", "pincode": ""}))

    def test_reverse_sync_creates_customer_with_clean_mobile_and_address(self):
        mock_ra_client = MagicMock()
        mock_ra_client.fetch_customers.return_value = []
        mock_ra_client.push_customer.return_value = {"id": 501}
        mock_ra_client.get_customer.return_value = {"address": []}
        mock_ext_client = MagicMock()
        mock_ext_client.cfg = MagicMock()

        ledger = {
            "name": "Test",
            "alter_id": 225,
            "phone": "08056997998, 08056997998",
            "mobile": "08056997998",
            "email": "pradhapm07836@gmail.com",
            "gstin": "33FDJPP7799K",
            "address_lines": ["Hosur", "Hosur", "Near IT Park", "Hosur"],
            "pincode": "635109",
            "country": "India",
            "state": "Tamil Nadu",
        }

        with unittest.mock.patch("app.sync.tally_to_rentasst.TallyFetcher") as mock_fetcher_cls:
            mock_fetcher = MagicMock()
            mock_fetcher.fetch_ledgers.return_value = [ledger]
            mock_fetcher.fetch_stock_items.return_value = []
            mock_fetcher.fetch_vouchers.return_value = []
            mock_fetcher_cls.return_value = mock_fetcher

            sync_tally_to_rentasst(
                ra_client=mock_ra_client, ext_client=mock_ext_client, store=self.store, force_full_sync=True,
            )

        mock_ra_client.push_customer.assert_called_once()
        payload = mock_ra_client.push_customer.call_args[0][0]
        self.assertEqual(payload["mobile"], "08056997998")
        self.assertEqual(payload["customer_gst_number"], "33FDJPP7799K")
        self.assertNotIn("address", payload)

        # Address is pushed through the dedicated address endpoint, not embedded on the
        # customer payload (confirmed live: RentAsst silently ignores an embedded 'address'
        # key on create).
        mock_ra_client.get_customer.assert_called_once_with("501")
        mock_ra_client.create_customer_address.assert_called_once()
        addr_call = mock_ra_client.create_customer_address.call_args[0]
        self.assertEqual(addr_call[0], "501")
        self.assertEqual(addr_call[1]["address1"], "Hosur, Hosur, Near IT Park, Hosur")
        self.assertEqual(addr_call[1]["state"], "Tamil Nadu")
        mock_ra_client.update_customer_address.assert_not_called()

    def test_reverse_sync_updates_existing_customer_when_tally_contact_details_change(self):
        """
        A customer whose mapping already exists used to be skipped unconditionally, even
        if their mobile/GST/address changed in Tally afterward — confirmed live, none of
        these ever reached RentAsst after the customer's first sync. A real change must
        now push an update instead of being silently ignored forever.
        """
        self.store.save_mapping(
            entity_type="customer", source_id="Test", target_id="CLOUD-CUST-1",
            source_system="tally", target_system="rentasst", status="synced",
            last_synced_hash="stale-hash-from-before-the-gst-number-was-added",
        )
        mock_ra_client = MagicMock()
        mock_ext_client = MagicMock()
        mock_ext_client.cfg = MagicMock()

        ledger = {
            "name": "Test", "alter_id": 226, "phone": "", "mobile": "08056997998",
            "email": "pradhapm07836@gmail.com", "gstin": "33FDJPP7799K",
            "address_lines": [], "pincode": "", "country": "", "state": "",
        }

        with unittest.mock.patch("app.sync.tally_to_rentasst.TallyFetcher") as mock_fetcher_cls:
            mock_fetcher = MagicMock()
            mock_fetcher.fetch_ledgers.return_value = [ledger]
            mock_fetcher.fetch_stock_items.return_value = []
            mock_fetcher.fetch_vouchers.return_value = []
            mock_fetcher_cls.return_value = mock_fetcher

            stats = sync_tally_to_rentasst(
                ra_client=mock_ra_client, ext_client=mock_ext_client, store=self.store, force_full_sync=True,
            )

        mock_ra_client.push_customer.assert_not_called()
        mock_ra_client.update_customer.assert_called_once_with(
            "CLOUD-CUST-1",
            {
                "name": "Test", "mobile": "08056997998",
                "email": "pradhapm07836@gmail.com", "customer_gst_number": "33FDJPP7799K",
            },
        )
        self.assertGreaterEqual(stats["updated"], 1)

    def test_reverse_sync_skips_existing_customer_when_tally_contact_details_unchanged(self):
        """An unrelated Tally edit (or a re-run with nothing changed) must not spam an
        update every single cycle."""
        ledger = {
            "name": "Test", "alter_id": 226, "phone": "", "mobile": "08056997998",
            "email": "pradhapm07836@gmail.com", "gstin": "33FDJPP7799K",
            "address_lines": [], "pincode": "", "country": "", "state": "",
        }
        unchanged_hash = _customer_contact_hash("08056997998", "pradhapm07836@gmail.com", "33FDJPP7799K", None)
        self.store.save_mapping(
            entity_type="customer", source_id="Test", target_id="CLOUD-CUST-1",
            source_system="tally", target_system="rentasst", status="synced",
            last_synced_hash=unchanged_hash,
        )
        mock_ra_client = MagicMock()
        mock_ext_client = MagicMock()
        mock_ext_client.cfg = MagicMock()

        with unittest.mock.patch("app.sync.tally_to_rentasst.TallyFetcher") as mock_fetcher_cls:
            mock_fetcher = MagicMock()
            mock_fetcher.fetch_ledgers.return_value = [ledger]
            mock_fetcher.fetch_stock_items.return_value = []
            mock_fetcher.fetch_vouchers.return_value = []
            mock_fetcher_cls.return_value = mock_fetcher

            sync_tally_to_rentasst(
                ra_client=mock_ra_client, ext_client=mock_ext_client, store=self.store, force_full_sync=True,
            )

        mock_ra_client.update_customer.assert_not_called()
        mock_ra_client.push_customer.assert_not_called()

    def test_reverse_sync_recreates_customer_when_mapped_rentasst_record_no_longer_exists(self):
        """
        A mapped RentAsst customer id can go stale (the record was deleted, or RentAsst's
        DB was reset) — confirmed live: PUT /customer/{id} against a deleted id 404s
        forever with no recovery. The stale mapping must be dropped and the customer
        re-created, not left permanently failing.
        """
        self.store.save_mapping(
            entity_type="customer", source_id="Test-1", target_id="3",
            source_system="tally", target_system="rentasst", status="synced",
            last_synced_hash="some-hash-from-before-the-record-was-deleted",
        )
        mock_ra_client = MagicMock()
        mock_ra_client.check_exists_in_rentasst.return_value = False
        mock_ra_client.push_customer.return_value = {"id": 9}
        mock_ra_client.get_customer.return_value = {"address": []}
        mock_ext_client = MagicMock()
        mock_ext_client.cfg = MagicMock()

        ledger = {
            "name": "Test-1", "alter_id": 227, "phone": "", "mobile": "0987654321",
            "email": "", "gstin": "", "address_lines": [], "pincode": "", "country": "", "state": "",
        }

        with unittest.mock.patch("app.sync.tally_to_rentasst.TallyFetcher") as mock_fetcher_cls:
            mock_fetcher = MagicMock()
            mock_fetcher.fetch_ledgers.return_value = [ledger]
            mock_fetcher.fetch_stock_items.return_value = []
            mock_fetcher.fetch_vouchers.return_value = []
            mock_fetcher_cls.return_value = mock_fetcher

            stats = sync_tally_to_rentasst(
                ra_client=mock_ra_client, ext_client=mock_ext_client, store=self.store, force_full_sync=True,
            )

        mock_ra_client.check_exists_in_rentasst.assert_called_once_with("customer", "3")
        mock_ra_client.update_customer.assert_not_called()
        mock_ra_client.push_customer.assert_called_once()
        self.assertEqual(stats["created"], 1)

        refreshed = self.store.find_mapping("customer", "Test-1")
        self.assertEqual(refreshed["target_id"], "9")

    def test_reverse_sync_updates_customer_address_when_one_already_exists(self):
        """A customer that already has an address record on file must get it UPDATED
        (PUT), not a second duplicate one created (POST)."""
        self.store.save_mapping(
            entity_type="customer", source_id="Test", target_id="1",
            source_system="tally", target_system="rentasst", status="synced",
            last_synced_hash="stale-hash",
        )
        mock_ra_client = MagicMock()
        mock_ra_client.check_exists_in_rentasst.return_value = True
        mock_ra_client.get_customer.return_value = {"address": [{"id": 7, "address1": "Old Line"}]}
        mock_ext_client = MagicMock()
        mock_ext_client.cfg = MagicMock()

        ledger = {
            "name": "Test", "alter_id": 228, "phone": "", "mobile": "08056997998",
            "email": "pradhapm07836@gmail.com", "gstin": "33FDJPP7799K",
            "address_lines": ["New Line"], "pincode": "635109", "country": "India", "state": "Tamil Nadu",
        }

        with unittest.mock.patch("app.sync.tally_to_rentasst.TallyFetcher") as mock_fetcher_cls:
            mock_fetcher = MagicMock()
            mock_fetcher.fetch_ledgers.return_value = [ledger]
            mock_fetcher.fetch_stock_items.return_value = []
            mock_fetcher.fetch_vouchers.return_value = []
            mock_fetcher_cls.return_value = mock_fetcher

            sync_tally_to_rentasst(
                ra_client=mock_ra_client, ext_client=mock_ext_client, store=self.store, force_full_sync=True,
            )

        mock_ra_client.update_customer_address.assert_called_once()
        call_args = mock_ra_client.update_customer_address.call_args[0]
        self.assertEqual(call_args[0], "1")
        self.assertEqual(call_args[1], 7)
        self.assertEqual(call_args[2]["address1"], "New Line")
        mock_ra_client.create_customer_address.assert_not_called()

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

    def test_reverse_sync_derives_invoice_item_price_from_amount_when_rate_is_blank(self):
        """
        Confirmed live: a Tally "Sales" voucher's inventory lines carry AMOUNT but leave
        RATE blank (unlike "Sales Order" lines, which always populate both) — parsing RATE
        alone left price=0 for every item on an invoice created this way. RentAsst's
        InvoiceItem::calculateAllAmounts() always recomputes total_price server-side as
        quantity*price, discarding whatever total_price the client sends, so a zero price
        here becomes a permanently zero item and a zero invoice grand_total regardless of
        what total_price is pushed. price must be derived from amount/quantity when rate
        can't be parsed.
        """
        self.store.save_mapping(
            entity_type="equipment",
            source_id="Dell Keyboard",
            target_id="17",
            source_system="tally",
            target_system="rentasst",
        )

        mock_ra_client = MagicMock()
        mock_ra_client.push_invoice.return_value = {"id": "CLOUD-INV-201"}
        mock_ext_client = MagicMock()
        mock_ext_client.cfg = MagicMock()

        voucher = {
            "tally_guid": "GUID-BLANK-RATE",
            "alter_id": 20,
            "voucher_type": "Sales",
            "voucher_number": "INV-201",
            "party_name": "Acme Corp",
            "date": "2026-08-20",
            "amount": 40.0,
            "items": [{"name": "Dell Keyboard", "quantity": "", "rate": "", "amount": "40.00"}],
        }

        with unittest.mock.patch("app.sync.tally_to_rentasst.TallyFetcher") as mock_fetcher_cls:
            mock_fetcher = MagicMock()
            mock_fetcher.fetch_vouchers.return_value = [voucher]
            mock_fetcher_cls.return_value = mock_fetcher

            sync_tally_to_rentasst(
                ra_client=mock_ra_client,
                ext_client=mock_ext_client,
                store=self.store,
                force_full_sync=True,
            )

        pushed_items = mock_ra_client.push_invoice_items.call_args[0][1]
        self.assertEqual(pushed_items[0]["quantity"], 1)
        self.assertEqual(pushed_items[0]["price"], 40.0)
        self.assertEqual(pushed_items[0]["total_price"], 40.0)

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

    def test_reverse_sync_never_retries_status_update_on_a_locked_invoice(self):
        """
        An invoice that's already 'paid' in RentAsst permanently rejects header edits
        (RentAsst's canEditInvoice() lock) — confirmed live, a 422 on every single cycle.
        Comparing current vs. recomputed target status isn't enough to catch this: if the
        settling receipt isn't in THIS run's fetch batch, invoice_status recomputes as
        'confirmed' even though the invoice is really 'paid', so a naive status-mismatch
        check would retry the doomed update forever. update_invoice must never be called
        once the invoice is in a known-locked status, regardless of the recomputed target.
        """
        tally_guid = "GUID-LOCKED-INV"
        rev_key = generate_integration_key("default", "invoice", tally_guid, "reverse")
        self.store.save_mapping(
            entity_type="invoice",
            source_id=tally_guid,
            target_id="CLOUD-INV-LOCKED",
            source_system="tally",
            target_system="rentasst",
            integration_key=rev_key,
            status="synced",
        )

        mock_ra_client = MagicMock()
        mock_ra_client.get_invoice.return_value = {"id": "CLOUD-INV-LOCKED", "status": "paid", "items": [{"id": 1}]}
        mock_ext_client = MagicMock()
        mock_ext_client.cfg = MagicMock()

        # No settling receipt in this batch — invoice_status recomputes as "confirmed",
        # which differs from the live "paid" status, but the update must still be skipped.
        invoice_voucher = {
            "tally_guid": tally_guid,
            "alter_id": 22,
            "voucher_type": "Sales",
            "voucher_number": "INV-LOCKED",
            "party_name": "Acme Corp",
            "date": "2026-08-20",
            "amount": 500.0,
        }

        with unittest.mock.patch("app.sync.tally_to_rentasst.TallyFetcher") as mock_fetcher_cls:
            mock_fetcher = MagicMock()
            mock_fetcher.fetch_vouchers.return_value = [invoice_voucher]
            mock_fetcher_cls.return_value = mock_fetcher

            sync_tally_to_rentasst(
                ra_client=mock_ra_client,
                ext_client=mock_ext_client,
                store=self.store,
                force_full_sync=True,
            )

        mock_ra_client.update_invoice.assert_not_called()

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
        # These three are RentAsst-side quirks confirmed live (see the comments on
        # DEFAULT_RENTOUT_SETTINGS and the item dict in tally_to_rentasst.py): a null id
        # crashes an availability check, identical rent_from/rent_to fails RentAsst's own
        # duration validation, and a missing discount_is_percentage violates a NOT NULL
        # DB constraint.
        self.assertIsNone(pushed_items[0]["id"])
        self.assertNotEqual(pushed_items[0]["rent_from"], pushed_items[0]["rent_to"])
        self.assertEqual(pushed_items[0]["discount_is_percentage"], False)
        # RentAsst's RentService::calculateStandardPrice() always overwrites total_price
        # server-side as quantity*price*duration, where duration comes from matching
        # calculation_method against 1/2/3 (days/hours/months) and silently defaults to 0
        # for anything else including a missing field — confirmed live: every rentout item
        # landed at total_price=0 (and the whole rentout's grand_total with it) because this
        # was never sent. 4 = AssetCalculationMethods::FLAT_PRICE (quantity*price, no
        # duration multiplier), the correct mode for a one-time Tally sale line.
        self.assertEqual(pushed_items[0]["calculation_method"], 4)

        # The rentout header itself must carry a non-empty settings object — an empty {}
        # round-trips through RentAsst's own request parsing as a PHP array, not an
        # object, and crashes identically (see DEFAULT_RENTOUT_SETTINGS).
        pushed_rentout = mock_ra_client.push_rentout.call_args[0][0]
        self.assertTrue(pushed_rentout["settings"])
        self.assertIn("refund_type", pushed_rentout["settings"])

    def test_reverse_sync_backfill_patches_null_settings_before_pushing_items(self):
        """
        A rentout created before 'settings' was included on create (or created directly
        via this reverse-sync path some other way) still has a null settings column —
        RentAsst's own RentItemsService::updateRentDeposit() reads
        $rent->settings->refund_type unconditionally, and a null settings crashes that
        whole DB transaction, rolling back the item insert too. The backfill path must
        patch settings first whenever it's missing.
        """
        self.store.save_mapping(
            entity_type="equipment",
            source_id="Earphone",
            target_id="4",
            source_system="tally",
            target_system="rentasst",
        )
        tally_guid = "GUID-NULL-SETTINGS-ORD"
        rev_key = generate_integration_key("default", "rental_order", tally_guid, "reverse")
        self.store.save_mapping(
            entity_type="rental_order",
            source_id=tally_guid,
            target_id="17",
            source_system="tally",
            target_system="rentasst",
            integration_key=rev_key,
            status="synced",
        )

        mock_ra_client = MagicMock()
        mock_ra_client.get_rentout.return_value = {"id": "17", "rent_items_count": 0, "settings": None, "status": 1}
        mock_ext_client = MagicMock()
        mock_ext_client.cfg = MagicMock()

        voucher = {
            "tally_guid": tally_guid,
            "alter_id": 44,
            "voucher_type": "Sales Order",
            "voucher_number": "ORD-NULL-SETTINGS",
            "party_name": "Acme Corp",
            "date": "2026-08-20",
            "amount": 50.0,
            "items": [{"name": "Earphone", "quantity": "1 Piece", "rate": "50.00/Piece", "amount": "50.00"}],
        }

        with unittest.mock.patch("app.sync.tally_to_rentasst.TallyFetcher") as mock_fetcher_cls:
            mock_fetcher = MagicMock()
            mock_fetcher.fetch_vouchers.return_value = [voucher]
            mock_fetcher_cls.return_value = mock_fetcher

            sync_tally_to_rentasst(
                ra_client=mock_ra_client,
                ext_client=mock_ext_client,
                store=self.store,
                force_full_sync=True,
            )

        mock_ra_client.update_rentout.assert_called_once()
        patched_id, patched_payload = mock_ra_client.update_rentout.call_args[0]
        self.assertEqual(patched_id, "17")
        self.assertTrue(patched_payload["settings"])
        # The settings patch must happen BEFORE the items push, not after — otherwise the
        # DB transaction the item insert runs inside would still crash on the old null
        # settings.
        self.assertLess(
            mock_ra_client.method_calls.index(unittest.mock.call.update_rentout(patched_id, patched_payload)),
            mock_ra_client.method_calls.index(unittest.mock.call.push_rentout_items("17", mock_ra_client.push_rentout_items.call_args[0][1])),
        )

    def test_reverse_sync_backfill_skips_settings_patch_when_already_present(self):
        """The mirror case: a rentout that already has a non-null settings object must
        not get a needless extra update_rentout call on every sync."""
        self.store.save_mapping(
            entity_type="equipment",
            source_id="Earphone",
            target_id="4",
            source_system="tally",
            target_system="rentasst",
        )
        tally_guid = "GUID-HAS-SETTINGS-ORD"
        rev_key = generate_integration_key("default", "rental_order", tally_guid, "reverse")
        self.store.save_mapping(
            entity_type="rental_order",
            source_id=tally_guid,
            target_id="18",
            source_system="tally",
            target_system="rentasst",
            integration_key=rev_key,
            status="synced",
        )

        mock_ra_client = MagicMock()
        mock_ra_client.get_rentout.return_value = {"id": "18", "rent_items_count": 0, "settings": {"refund_type": 1}, "status": 1}
        mock_ext_client = MagicMock()
        mock_ext_client.cfg = MagicMock()

        voucher = {
            "tally_guid": tally_guid,
            "alter_id": 45,
            "voucher_type": "Sales Order",
            "voucher_number": "ORD-HAS-SETTINGS",
            "party_name": "Acme Corp",
            "date": "2026-08-20",
            "amount": 50.0,
            "items": [{"name": "Earphone", "quantity": "1 Piece", "rate": "50.00/Piece", "amount": "50.00"}],
        }

        with unittest.mock.patch("app.sync.tally_to_rentasst.TallyFetcher") as mock_fetcher_cls:
            mock_fetcher = MagicMock()
            mock_fetcher.fetch_vouchers.return_value = [voucher]
            mock_fetcher_cls.return_value = mock_fetcher

            sync_tally_to_rentasst(
                ra_client=mock_ra_client,
                ext_client=mock_ext_client,
                store=self.store,
                force_full_sync=True,
            )

        mock_ra_client.update_rentout.assert_not_called()
        mock_ra_client.push_rentout_items.assert_called_once()

    def test_reverse_sync_backfills_missing_invoice_items_on_existing_invoice(self):
        """
        An invoice synced before push_invoice_items() existed (or whose item push
        failed) is stuck at zero items forever under the plain "update status only"
        path — reverse sync must notice the live RentAsst record still has no items and
        backfill them, without re-pushing on invoices that already have items.
        """
        self.store.save_mapping(
            entity_type="equipment",
            source_id="Moto G45",
            target_id="17",
            source_system="tally",
            target_system="rentasst",
        )
        tally_guid = "GUID-BACKFILL-INV"
        rev_key = generate_integration_key("default", "invoice", tally_guid, "reverse")
        self.store.save_mapping(
            entity_type="invoice",
            source_id=tally_guid,
            target_id="CLOUD-INV-BACKFILL",
            source_system="tally",
            target_system="rentasst",
            integration_key=rev_key,
            status="synced",
        )

        mock_ra_client = MagicMock()
        mock_ra_client.get_invoice.return_value = {"id": "CLOUD-INV-BACKFILL", "items": []}
        mock_ext_client = MagicMock()
        mock_ext_client.cfg = MagicMock()

        voucher = {
            "tally_guid": tally_guid,
            "alter_id": 40,
            "voucher_type": "Sales",
            "voucher_number": "INV-BACKFILL",
            "party_name": "Acme Corp",
            "date": "2026-08-20",
            "amount": 97.0,
            "items": [{"name": "Moto G45", "quantity": "1 Piece", "rate": "97.00/Piece", "amount": "97.00"}],
        }

        with unittest.mock.patch("app.sync.tally_to_rentasst.TallyFetcher") as mock_fetcher_cls:
            mock_fetcher = MagicMock()
            mock_fetcher.fetch_vouchers.return_value = [voucher]
            mock_fetcher_cls.return_value = mock_fetcher

            sync_tally_to_rentasst(
                ra_client=mock_ra_client,
                ext_client=mock_ext_client,
                store=self.store,
                force_full_sync=True,
            )

        mock_ra_client.push_invoice.assert_not_called()
        mock_ra_client.get_invoice.assert_called_once_with("CLOUD-INV-BACKFILL")
        mock_ra_client.push_invoice_items.assert_called_once()
        pushed_items = mock_ra_client.push_invoice_items.call_args[0][1]
        self.assertEqual(pushed_items[0]["asset_id"], 17)

    def test_reverse_sync_does_not_duplicate_invoice_items_when_already_present(self):
        """The mirror case: an existing invoice that already has items must not get a
        second, duplicate set pushed just because reverse sync ran again."""
        tally_guid = "GUID-HAS-ITEMS-INV"
        rev_key = generate_integration_key("default", "invoice", tally_guid, "reverse")
        self.store.save_mapping(
            entity_type="invoice",
            source_id=tally_guid,
            target_id="CLOUD-INV-HAS-ITEMS",
            source_system="tally",
            target_system="rentasst",
            integration_key=rev_key,
            status="synced",
        )

        mock_ra_client = MagicMock()
        mock_ra_client.get_invoice.return_value = {"id": "CLOUD-INV-HAS-ITEMS", "items": [{"id": 1, "name": "Moto G45"}]}
        mock_ext_client = MagicMock()
        mock_ext_client.cfg = MagicMock()

        voucher = {
            "tally_guid": tally_guid,
            "alter_id": 41,
            "voucher_type": "Sales",
            "voucher_number": "INV-HAS-ITEMS",
            "party_name": "Acme Corp",
            "date": "2026-08-20",
            "amount": 97.0,
            "items": [{"name": "Moto G45", "quantity": "1 Piece", "rate": "97.00/Piece", "amount": "97.00"}],
        }

        with unittest.mock.patch("app.sync.tally_to_rentasst.TallyFetcher") as mock_fetcher_cls:
            mock_fetcher = MagicMock()
            mock_fetcher.fetch_vouchers.return_value = [voucher]
            mock_fetcher_cls.return_value = mock_fetcher

            sync_tally_to_rentasst(
                ra_client=mock_ra_client,
                ext_client=mock_ext_client,
                store=self.store,
                force_full_sync=True,
            )

        mock_ra_client.push_invoice_items.assert_not_called()

    def test_reverse_sync_backfills_missing_rentout_items_on_existing_rentout(self):
        """Same backfill behavior for rentouts: an existing rentout stuck at zero rent
        items gets them added via push_rentout_items() instead of staying empty forever."""
        self.store.save_mapping(
            entity_type="equipment",
            source_id="Moto G45",
            target_id="17",
            source_system="tally",
            target_system="rentasst",
        )
        tally_guid = "GUID-BACKFILL-ORD"
        rev_key = generate_integration_key("default", "rental_order", tally_guid, "reverse")
        self.store.save_mapping(
            entity_type="rental_order",
            source_id=tally_guid,
            target_id="CLOUD-ORD-BACKFILL",
            source_system="tally",
            target_system="rentasst",
            integration_key=rev_key,
            status="synced",
        )

        mock_ra_client = MagicMock()
        mock_ra_client.get_rentout.return_value = {"id": "CLOUD-ORD-BACKFILL", "rent_items_count": 0}
        mock_ext_client = MagicMock()
        mock_ext_client.cfg = MagicMock()

        voucher = {
            "tally_guid": tally_guid,
            "alter_id": 42,
            "voucher_type": "Sales Order",
            "voucher_number": "ORD-BACKFILL",
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

        mock_ra_client.push_rentout.assert_not_called()
        mock_ra_client.get_rentout.assert_called_once_with("CLOUD-ORD-BACKFILL")
        mock_ra_client.push_rentout_items.assert_called_once()
        pushed_items = mock_ra_client.push_rentout_items.call_args[0][1]
        self.assertEqual(pushed_items[0]["asset_id"], 17)
        self.assertGreaterEqual(stats["updated"], 1)

    def test_reverse_sync_does_not_duplicate_rentout_items_when_already_present(self):
        """The mirror case for rentouts: an existing rentout that already has items
        must not get a second, duplicate set pushed."""
        tally_guid = "GUID-HAS-ITEMS-ORD"
        rev_key = generate_integration_key("default", "rental_order", tally_guid, "reverse")
        self.store.save_mapping(
            entity_type="rental_order",
            source_id=tally_guid,
            target_id="CLOUD-ORD-HAS-ITEMS",
            source_system="tally",
            target_system="rentasst",
            integration_key=rev_key,
            status="synced",
        )

        mock_ra_client = MagicMock()
        mock_ra_client.get_rentout.return_value = {"id": "CLOUD-ORD-HAS-ITEMS", "rent_items_count": 2}
        mock_ext_client = MagicMock()
        mock_ext_client.cfg = MagicMock()

        voucher = {
            "tally_guid": tally_guid,
            "alter_id": 43,
            "voucher_type": "Sales Order",
            "voucher_number": "ORD-HAS-ITEMS",
            "party_name": "Acme Corp",
            "date": "2026-08-20",
            "amount": 97.0,
            "items": [{"name": "Moto G45", "quantity": "1 Piece", "rate": "97.00/Piece", "amount": "97.00"}],
        }

        with unittest.mock.patch("app.sync.tally_to_rentasst.TallyFetcher") as mock_fetcher_cls:
            mock_fetcher = MagicMock()
            mock_fetcher.fetch_vouchers.return_value = [voucher]
            mock_fetcher_cls.return_value = mock_fetcher

            sync_tally_to_rentasst(
                ra_client=mock_ra_client,
                ext_client=mock_ext_client,
                store=self.store,
                force_full_sync=True,
            )

        mock_ra_client.push_rentout_items.assert_not_called()


class TestEquipmentReverseSync(unittest.TestCase):
    """
    Reverse sync (Tally -> RentAsst) of stock items into RentAsst equipment. Confirmed
    live: a brand-new Tally-only stock item always landed in RentAsst with
    available_quantity=0 (CLOSINGBALANCE was fetched by Tally's own STOCKITEM export but
    never parsed or pushed), an already reverse-synced item's HSN/GST/quantity changes in
    Tally never reached RentAsst (the lookup used to find its RentAsst mapping never
    actually matched — same root cause as the equivalent customer bug), and — the most
    dangerous gap — reverse sync could find a RentAsst-native (forward-sync-owned) asset by
    a live name match and push Tally-derived changes onto it, which RentAsst itself
    rejects once that asset has real rental history ("Asset has inventory history. Archive
    stock first before disabling inventory tracking.").
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_equipment_reverse_sync.db")
        self.store = MappingStore(self.db_path)

    def tearDown(self):
        if hasattr(self, "store") and self.store:
            self.store.db.close()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run_sync(self, ra_client, stock_item):
        ext_client = MagicMock()
        ext_client.cfg = MagicMock()
        with unittest.mock.patch("app.sync.tally_to_rentasst.TallyFetcher") as mock_fetcher_cls:
            mock_fetcher = MagicMock()
            mock_fetcher.fetch_ledgers.return_value = []
            mock_fetcher.fetch_stock_items.return_value = [stock_item]
            mock_fetcher.fetch_vouchers.return_value = []
            mock_fetcher_cls.return_value = mock_fetcher
            return sync_tally_to_rentasst(
                ra_client=ra_client, ext_client=ext_client, store=self.store, force_full_sync=True,
            )

    def test_creates_new_asset_with_quantity_gst_and_rent_price(self):
        mock_ra_client = MagicMock()
        mock_ra_client.fetch_equipment.return_value = []
        mock_ra_client.push_equipment.return_value = {"id": 10}

        stock_item = {
            "name": "New Tally Asset", "parent": "", "unit": "pc",
            "hsn_code": "998877", "gst_rate": 18.0, "quantity": 6.0,
            "rent_price": 150.0, "alter_id": 300,
        }

        stats = self._run_sync(mock_ra_client, stock_item)

        mock_ra_client.push_equipment.assert_called_once()
        payload = mock_ra_client.push_equipment.call_args[0][0]
        self.assertEqual(payload["available_quantity"], 6)
        self.assertEqual(payload["branch"], [{"branch_id": 1, "quantity": 6}])
        self.assertEqual(payload["rent_price"], "150.00")
        self.assertEqual(payload["day_based_rent_price"], "150.00")
        self.assertEqual(stats["created"], 1)

    def test_never_pushes_updates_onto_a_forward_owned_asset_with_matching_name(self):
        """
        The critical safety boundary: an asset found only via a live name match (no
        reverse-sync mapping) must never get update_equipment called on it, regardless of
        how different its Tally-side HSN/GST/quantity looks. Also must NOT persist any
        mapping row for this case — confirmed live this was a second, worse bug: a
        mapping saved here as source_system="tally" is the exact shape
        run_sync_pipeline's forward-sync guard (app/sync/base.py) reads as "this record
        originated in Tally, never forward-sync it again", which silently blackholed ALL
        future forward-sync GST/price/quantity updates for a real RentAsst-native asset
        ('Dell Laptop') the moment reverse sync ever looked at it once.
        """
        mock_ra_client = MagicMock()
        mock_ra_client.fetch_equipment.return_value = [{"id": 1, "name": "Dell Laptop"}]

        stock_item = {
            "name": "Dell Laptop", "parent": "Laptop", "unit": "pc",
            "hsn_code": "256341", "gst_rate": 18.0, "quantity": 10.0, "alter_id": 233,
        }

        stats = self._run_sync(mock_ra_client, stock_item)

        mock_ra_client.push_equipment.assert_not_called()
        mock_ra_client.update_equipment.assert_not_called()
        self.assertEqual(stats["skipped"], 1)
        self.assertIsNone(self.store.find_mapping("equipment", "Dell Laptop"))

        # Second run must ALSO never touch it — re-scanning by name every cycle (since
        # nothing is cached) must keep resolving to "forward-owned, skip".
        mock_ra_client.reset_mock()
        mock_ra_client.fetch_equipment.return_value = [{"id": 1, "name": "Dell Laptop"}]
        stats2 = self._run_sync(mock_ra_client, stock_item)
        mock_ra_client.update_equipment.assert_not_called()
        mock_ra_client.push_equipment.assert_not_called()
        self.assertEqual(stats2["skipped"], 1)
        self.assertIsNone(self.store.find_mapping("equipment", "Dell Laptop"))

    def test_updates_a_reverse_owned_asset_when_hsn_gst_quantity_or_rent_price_changes(self):
        self.store.save_mapping(
            entity_type="equipment", source_id="Diag Reverse Asset", target_id="20",
            source_system="tally", target_system="rentasst",
            last_synced_hash="stale-hash-from-before-the-hsn-code-was-added",
        )
        mock_ra_client = MagicMock()
        mock_ra_client.check_exists_in_rentasst.return_value = True

        stock_item = {
            "name": "Diag Reverse Asset", "parent": "", "unit": "pc",
            "hsn_code": "998877", "gst_rate": 18.0, "quantity": 7.0,
            "rent_price": 200.0, "alter_id": 234,
        }

        stats = self._run_sync(mock_ra_client, stock_item)

        mock_ra_client.push_equipment.assert_not_called()
        mock_ra_client.update_equipment.assert_called_once()
        call_args = mock_ra_client.update_equipment.call_args
        self.assertEqual(call_args[0][0], "20")
        payload = call_args[0][1]
        self.assertEqual(payload["name"], "Diag Reverse Asset")
        self.assertEqual(payload["hsn_code"], "998877")
        self.assertEqual(payload["gst_rate"], 18.0)
        self.assertEqual(payload["available_quantity"], 7)
        self.assertEqual(payload["branch"], [{"branch_id": 1, "quantity": 7}])
        self.assertEqual(payload["rent_price"], "200.00")
        self.assertEqual(payload["day_based_rent_price"], "200.00")
        self.assertGreaterEqual(stats["updated"], 1)

    def test_skips_reverse_owned_asset_when_nothing_changed(self):
        unchanged_hash = _equipment_change_hash("998877", 0.0, 7.0, "", "pc")
        self.store.save_mapping(
            entity_type="equipment", source_id="Diag Reverse Asset", target_id="20",
            source_system="tally", target_system="rentasst",
            last_synced_hash=unchanged_hash,
        )
        mock_ra_client = MagicMock()
        mock_ra_client.check_exists_in_rentasst.return_value = True

        stock_item = {
            "name": "Diag Reverse Asset", "parent": "", "unit": "pc",
            "hsn_code": "998877", "gst_rate": 0.0, "quantity": 7.0, "alter_id": 234,
        }

        self._run_sync(mock_ra_client, stock_item)

        mock_ra_client.update_equipment.assert_not_called()
        mock_ra_client.push_equipment.assert_not_called()

    def test_recreates_when_reverse_owned_mapping_target_no_longer_exists(self):
        self.store.save_mapping(
            entity_type="equipment", source_id="Diag Reverse Asset", target_id="99",
            source_system="tally", target_system="rentasst",
            last_synced_hash="some-hash-from-before-the-record-was-deleted",
        )
        mock_ra_client = MagicMock()
        mock_ra_client.check_exists_in_rentasst.return_value = False
        mock_ra_client.fetch_equipment.return_value = []
        mock_ra_client.push_equipment.return_value = {"id": 55}

        stock_item = {
            "name": "Diag Reverse Asset", "parent": "", "unit": "pc",
            "hsn_code": "998877", "gst_rate": 0.0, "quantity": 7.0, "alter_id": 234,
        }

        stats = self._run_sync(mock_ra_client, stock_item)

        mock_ra_client.check_exists_in_rentasst.assert_called_once_with("equipment", "99")
        mock_ra_client.update_equipment.assert_not_called()
        mock_ra_client.push_equipment.assert_called_once()
        self.assertEqual(stats["created"], 1)

        refreshed = self.store.find_mapping("equipment", "Diag Reverse Asset")
        self.assertEqual(refreshed["target_id"], "55")


if __name__ == "__main__":
    unittest.main()
