import os
import shutil
import tempfile
import unittest
from app.mapping.store import MappingStore


class TestMappingStore(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_state.db")
        self.store = MappingStore(self.db_path)

    def tearDown(self):
        if hasattr(self, "store") and self.store:
            self.store.db.close()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_mapping_creation_and_lookup(self):
        self.store.save_mapping(
            entity_type="customer",
            source_id="CUST-100",
            target_id="TALLY-LEDGER-100",
            source_system="rentasst",
            source_company_id="comp_a",
            target_system="tally",
            target_company_id="tally_a",
            last_synced_hash="sha256_hash_123",
            status="synced",
        )

        mapping = self.store.find_mapping(
            entity_type="customer",
            source_id="CUST-100",
            source_system="rentasst",
            source_company_id="comp_a",
        )
        self.assertIsNotNone(mapping)
        self.assertEqual(mapping["entity_type"], "customer")
        self.assertEqual(mapping["source_id"], "CUST-100")
        self.assertEqual(mapping["target_id"], "TALLY-LEDGER-100")
        self.assertEqual(mapping["source_system"], "rentasst")
        self.assertEqual(mapping["source_company_id"], "comp_a")
        self.assertEqual(mapping["target_system"], "tally")
        self.assertEqual(mapping["target_company_id"], "tally_a")
        self.assertEqual(mapping["last_synced_hash"], "sha256_hash_123")
        self.assertEqual(mapping["integration_key"], "rentasst:comp_a:customer:CUST-100")
        self.assertEqual(mapping["status"], "synced")

    def test_mapping_update_and_versioning(self):
        self.store.save_mapping(
            entity_type="invoice",
            source_id="INV-500",
            target_id="TALLY-INV-500",
            source_company_id="comp_a",
            last_synced_hash="hash_v1",
        )

        initial = self.store.find_mapping("invoice", "INV-500", source_company_id="comp_a")
        self.assertEqual(initial["sync_version"], 1)
        self.assertEqual(initial["last_synced_hash"], "hash_v1")

        # Update mapping with new hash
        self.store.save_mapping(
            entity_type="invoice",
            source_id="INV-500",
            target_id="TALLY-INV-500-ALT",
            source_company_id="comp_a",
            last_synced_hash="hash_v2",
        )

        updated = self.store.find_mapping("invoice", "INV-500", source_company_id="comp_a")
        self.assertEqual(updated["sync_version"], 2)
        self.assertEqual(updated["target_id"], "TALLY-INV-500-ALT")
        self.assertEqual(updated["last_synced_hash"], "hash_v2")

    def test_duplicate_prevention(self):
        self.store.save_mapping(
            entity_type="payment",
            source_id="PAY-99",
            target_id="TALLY-PAY-99",
            source_company_id="comp_a",
            last_synced_hash="payload_hash_abc",
            status="synced",
        )

        # Same hash should return True for duplicate
        is_dup = self.store.is_duplicate("payment", "PAY-99", current_hash="payload_hash_abc", source_company_id="comp_a")
        self.assertTrue(is_dup)

        # Different hash should return False
        is_dup_new = self.store.is_duplicate("payment", "PAY-99", current_hash="payload_hash_xyz", source_company_id="comp_a")
        self.assertFalse(is_dup_new)

    def test_multi_company_isolation(self):
        # Save same source_id ("1001") for two different companies
        self.store.save_mapping(
            entity_type="customer",
            source_id="1001",
            target_id="TALLY-COMP-A-1001",
            source_company_id="company_alpha",
        )
        self.store.save_mapping(
            entity_type="customer",
            source_id="1001",
            target_id="TALLY-COMP-B-1001",
            source_company_id="company_beta",
        )

        map_alpha = self.store.find_mapping("customer", "1001", source_company_id="company_alpha")
        map_beta = self.store.find_mapping("customer", "1001", source_company_id="company_beta")

        self.assertIsNotNone(map_alpha)
        self.assertIsNotNone(map_beta)
        self.assertEqual(map_alpha["target_id"], "TALLY-COMP-A-1001")
        self.assertEqual(map_beta["target_id"], "TALLY-COMP-B-1001")

    def test_cross_entity_isolation(self):
        # Save ID "777" for customer and invoice
        self.store.save_mapping(
            entity_type="customer",
            source_id="777",
            target_id="LEDGER-777",
        )
        self.store.save_mapping(
            entity_type="invoice",
            source_id="777",
            target_id="VOUCHER-777",
        )

        cust_map = self.store.find_mapping("customer", "777")
        inv_map = self.store.find_mapping("invoice", "777")

        self.assertEqual(cust_map["target_id"], "LEDGER-777")
        self.assertEqual(inv_map["target_id"], "VOUCHER-777")

    def test_backward_compatibility_legacy_adapter(self):
        # Legacy save call
        self.store.save(entity_type="equipment", rentasst_id="EQ-5", external_id="TALLY-ITEM-5", last_hash="hash_5")

        ext_id = self.store.get_external_id("equipment", "EQ-5")
        ra_id = self.store.get_rentasst_id("equipment", "TALLY-ITEM-5")

        self.assertEqual(ext_id, "TALLY-ITEM-5")
        self.assertEqual(ra_id, "EQ-5")

    def test_dead_letters_and_checkpoints(self):
        self.store.set_checkpoint("tally_alter_id", "1500")
        ckpt = self.store.get_checkpoint("tally_alter_id")
        self.assertEqual(ckpt, "1500")

        self.store.add_dead_letter("invoice", "INV-ERROR-1", "Validation Failed", '{"key": "val"}')
        dead_letters = self.store.list_dead_letters("invoice")
        self.assertTrue(len(dead_letters) >= 1)
        self.assertEqual(dead_letters[0]["source_id"], "INV-ERROR-1")

        cleared = self.store.clear_dead_letters("invoice")
        self.assertTrue(cleared >= 1)


if __name__ == "__main__":
    unittest.main()
