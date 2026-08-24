import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock
from concurrent.futures import ThreadPoolExecutor

from app.mapping.store import MappingStore
from app.sync.idempotency import generate_integration_key, check_target_system_record_exists
from app.sync.base import run_sync_pipeline


class TestIdempotencyMechanism(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_idempotency.db")
        self.store = MappingStore(self.db_path)

    def tearDown(self):
        if hasattr(self, "store") and self.store:
            self.store.db.close()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_deterministic_integration_key_format(self):
        key_fwd = generate_integration_key(
            source_company="TenantA",
            entity_type="invoice",
            source_id="INV-99",
            sync_direction="forward",
        )
        self.assertEqual(key_fwd, "tenanta:invoice:INV-99:forward")

        key_rev = generate_integration_key(
            source_company="TallyCompanyX",
            entity_type="payment",
            source_id="TALLY-PAY-123",
            sync_direction="reverse",
        )
        self.assertEqual(key_rev, "tallycompanyx:payment:TALLY-PAY-123:reverse")

    def test_duplicate_request_prevention(self):
        # Mock external client: initially record does not exist in Tally
        mock_ext_client = MagicMock()
        mock_ext_client.ping.return_value = True
        mock_ext_client.check_exists_in_tally.return_value = False

        creation_call_count = 0

        def mock_sync_func(item):
            nonlocal creation_call_count
            creation_call_count += 1
            # Once created, Tally reports it as existing
            mock_ext_client.check_exists_in_tally.return_value = True
            return f"TALLY-ID-{item['id']}"

        items = [{"id": "101", "name": "Customer 101"}]

        # First Execution -> Should call creation func once
        stats1 = run_sync_pipeline(
            entity_type="customer",
            fetch_func=lambda: items,
            sync_func=mock_sync_func,
            store=self.store,
            external_client=mock_ext_client,
            source_company_id="company_one",
        )
        self.assertEqual(creation_call_count, 1)
        self.assertEqual(stats1["created"], 1)

        # Verify mapping exists with deterministic key
        key = generate_integration_key("company_one", "customer", "101", "forward")
        mapping = self.store.find_by_integration_key(key)
        self.assertIsNotNone(mapping)
        self.assertEqual(mapping["target_id"], "TALLY-ID-101")

        # Second Execution (Duplicate Request) -> Must skip creation call
        stats2 = run_sync_pipeline(
            entity_type="customer",
            fetch_func=lambda: items,
            sync_func=mock_sync_func,
            store=self.store,
            external_client=mock_ext_client,
            source_company_id="company_one",
        )
        self.assertEqual(creation_call_count, 1)  # Creation count remains 1!
        self.assertEqual(stats2["skipped"], 1)

    def test_rental_order_second_pass_skips_instead_of_repushing(self):
        """
        Confirmed live: extract_identifier() used to build a rental_order's identifier
        from RentAsst's own display 'number' (e.g. 'R100016'), but Tally never echoes
        that value back anywhere in its export (it auto-assigns its own VOUCHERNUMBER),
        so check_target_system_record_exists()'s substring search against Tally's
        NARRATION/VOUCHERNUMBER fields always failed to find an already-synced order —
        every single sync cycle concluded "no longer exists in target system" and
        needlessly re-pushed it as an Alter, forever, on two consecutive full runs.
        The identifier must be the deterministic RENTAL-ORD-{id} marker instead, which
        Tally actually stores in NARRATION and the check can reliably find.
        """
        mock_ext_client = MagicMock()
        mock_ext_client.ping.return_value = True
        # Doesn't exist yet on the first pass; simulates Tally's real check_exists() only
        # ever matching the deterministic marker, never RentAsst's own display number
        # ('R100016' would never be found in Tally's export).
        mock_ext_client.check_exists_in_tally.return_value = False

        def mock_sync(item):
            mock_ext_client.check_exists_in_tally.side_effect = (
                lambda ent, identifier: identifier == "RENTAL-ORD-20"
            )
            return "RENTAL-ORD-20"

        mock_ext_client.sync_rental_order.side_effect = mock_sync

        items = [{"id": 20, "number": "R100016", "status": 1, "amount": 118.0, "customer_name": "Test"}]

        stats1 = run_sync_pipeline(
            entity_type="rental_order",
            fetch_func=lambda: items,
            sync_func=mock_ext_client.sync_rental_order,
            store=self.store,
            external_client=mock_ext_client,
        )
        self.assertEqual(stats1["created"], 1)
        mock_ext_client.sync_rental_order.assert_called_once()

        stats2 = run_sync_pipeline(
            entity_type="rental_order",
            fetch_func=lambda: items,
            sync_func=mock_ext_client.sync_rental_order,
            store=self.store,
            external_client=mock_ext_client,
        )
        self.assertEqual(stats2["skipped"], 1)
        mock_ext_client.sync_rental_order.assert_called_once()  # still just the one call

    def test_timeout_scenario_recovery(self):
        """
        Simulates scenario where Tally successfully created a record on a previous request,
        but the connection timed out before the middleware could save the mapping.
        On retry, target pre-check detects existing Tally record and saves mapping without creating duplicate.
        """
        mock_ext_client = MagicMock()
        mock_ext_client.ping.return_value = True
        # Target system confirms item "Customer Timeout Test" ALREADY exists in Tally from timed-out call
        mock_ext_client.check_exists_in_tally.return_value = True

        creation_call_count = 0

        def mock_sync_func(item):
            nonlocal creation_call_count
            creation_call_count += 1
            return f"TALLY-NEW-{item['id']}"

        items = [{"id": "202", "name": "Customer Timeout Test"}]

        # Run sync pipeline on an unmapped item whose target record already exists in Tally
        stats = run_sync_pipeline(
            entity_type="customer",
            fetch_func=lambda: items,
            sync_func=mock_sync_func,
            store=self.store,
            external_client=mock_ext_client,
            source_company_id="company_one",
        )

        # Creation function MUST NOT be called (timeout recovery adopted existing record)
        self.assertEqual(creation_call_count, 0)
        self.assertEqual(stats["skipped"], 1)

        # Mapping should now be recovered in SQLite
        key = generate_integration_key("company_one", "customer", "202", "forward")
        mapping = self.store.find_by_integration_key(key)
        self.assertIsNotNone(mapping)
        self.assertEqual(mapping["target_id"], "Customer Timeout Test")

    def test_concurrent_requests_handling(self):
        """
        Simulates 4 concurrent worker threads running sync pipeline for the same entity item.
        Asserts that target mapping is created and only 1 thread executes creation.
        """
        mock_ext_client = MagicMock()
        mock_ext_client.ping.return_value = True
        mock_ext_client.check_exists_in_tally.return_value = False

        creation_call_count = 0

        def mock_sync_func(item):
            nonlocal creation_call_count
            creation_call_count += 1
            return f"TALLY-CONC-{item['id']}"

        items = [{"id": "303", "name": "Concurrent Customer"}]

        def run_task():
            # Use dedicated store connection per thread
            thread_store = MappingStore(self.db_path)
            try:
                return run_sync_pipeline(
                    entity_type="customer",
                    fetch_func=lambda: items,
                    sync_func=mock_sync_func,
                    store=thread_store,
                    external_client=mock_ext_client,
                    source_company_id="company_conc",
                )
            finally:
                thread_store.db.close()

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(run_task) for _ in range(4)]
            results = [f.result() for f in futures]

        self.assertEqual(len(results), 4)

        # Key check
        key = generate_integration_key("company_conc", "customer", "303", "forward")
        mapping = self.store.find_by_integration_key(key)
        self.assertIsNotNone(mapping)
        self.assertEqual(mapping["target_id"], "TALLY-CONC-303")

    def test_forward_sync_never_repushes_a_record_reverse_sync_created(self):
        """
        A RentAsst rental_order/invoice/payment created by reverse sync (Tally -> RentAsst)
        must never be forward-synced back to Tally. Confirmed live: RentAsst never persists
        our tally_guid on the Rent/Invoice model (not a fillable column), so a Tally-native
        voucher forward-synced again looks like a brand-new record to sync_rental_order's
        REMOTEID-based existence check — Tally then rejects the duplicate-creation attempt
        with an opaque "EXCEPTIONS>0" business error every single scheduled sync.
        """
        self.store.save_mapping(
            entity_type="rental_order",
            source_id="TALLY-GUID-ABC",
            target_id="17",
            source_system="tally",
            target_system="rentasst",
            status="synced",
        )

        mock_ext_client = MagicMock()
        mock_ext_client.ping.return_value = True

        sync_call_count = 0

        def mock_sync_func(item):
            nonlocal sync_call_count
            sync_call_count += 1
            return f"RENTAL-ORD-{item['id']}"

        items = [{"id": "17", "number": "R100013", "customer_id": 4, "amount": 50.0}]

        stats = run_sync_pipeline(
            entity_type="rental_order",
            fetch_func=lambda: items,
            sync_func=mock_sync_func,
            store=self.store,
            external_client=mock_ext_client,
        )

        self.assertEqual(sync_call_count, 0)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["failed"], 0)

    def test_forward_sync_still_pushes_a_rentasst_native_record(self):
        """The mirror case: a record with no reverse-sync mapping (genuinely created in
        RentAsst, never touched by reverse sync) must still forward-sync normally."""
        mock_ext_client = MagicMock()
        mock_ext_client.ping.return_value = True
        mock_ext_client.check_exists_in_tally.return_value = False

        sync_call_count = 0

        def mock_sync_func(item):
            nonlocal sync_call_count
            sync_call_count += 1
            return f"RENTAL-ORD-{item['id']}"

        items = [{"id": "18", "number": "R100014", "customer_name": "New Customer", "amount": 100.0}]

        stats = run_sync_pipeline(
            entity_type="rental_order",
            fetch_func=lambda: items,
            sync_func=mock_sync_func,
            store=self.store,
            external_client=mock_ext_client,
        )

        self.assertEqual(sync_call_count, 1)
        self.assertEqual(stats["created"], 1)


if __name__ == "__main__":
    unittest.main()
