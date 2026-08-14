import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

from app.mapping.store import MappingStore
from app.services.backup_service import BackupService
from app.sync.base import run_sync_pipeline


class TestBackupAndRecovery(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "state.db")
        self.store = MappingStore(self.db_path)
        self.backup_svc = BackupService(self.temp_dir)

    def tearDown(self):
        if hasattr(self, "store") and self.store:
            self.store.db.close()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_manual_and_scheduled_backup_creation(self):
        # Save sample mapping
        self.store.save("customer", "CUST-100", "TALLY-CUST-100")

        res = self.backup_svc.trigger_backup()
        self.assertEqual(res["status"], "success")
        self.assertTrue(res["verified"])
        self.assertIsNotNone(res["db_backup"])

        backups = self.backup_svc.list_backups()
        self.assertGreater(len(backups), 0)

    def test_backup_integrity_verification(self):
        # 1. Valid backup verification
        res = self.backup_svc.trigger_backup()
        b_name = res["db_backup"]
        b_path = os.path.join(self.backup_svc.backup_dir, b_name)
        self.assertTrue(self.backup_svc.verify_backup(b_path))

        # 2. Corrupt backup verification -> MUST FAIL
        corrupt_path = os.path.join(self.backup_svc.backup_dir, "corrupt.db")
        with open(corrupt_path, "w") as f:
            f.write("Corrupted Junk File Contents")
        self.assertFalse(self.backup_svc.verify_backup(corrupt_path))

    def test_backup_restore_procedure(self):
        # 1. State 1: Save Customer 1
        self.store.save("customer", "CUST-1", "TALLY-CUST-1")
        res1 = self.backup_svc.trigger_backup()
        b1_file = res1["db_backup"]

        # 2. State 2: Save Customer 2
        self.store.save("customer", "CUST-2", "TALLY-CUST-2")
        self.assertTrue(self.store.exists("customer", "CUST-2"))

        # 3. Restore State 1 Backup
        self.store.db.close()  # Close active connection before file overwrite
        restore_res = self.backup_svc.restore_backup(b1_file)
        self.assertEqual(restore_res["status"], "success")
        self.assertIsNotNone(restore_res["prerestore_snapshot"])

        # Reopen connection after restore
        self.store = MappingStore(self.db_path)
        self.assertTrue(self.store.exists("customer", "CUST-1"))
        # Customer 2 should NOT exist in restored State 1!
        self.assertFalse(self.store.exists("customer", "CUST-2"))

    def test_backup_retention_purge(self):
        # Create 12 dummy backup files
        for i in range(12):
            b_path = os.path.join(self.backup_svc.backup_dir, f"state_backup_20260812_{i:02d}0000.db")
            shutil.copy2(self.db_path, b_path)

        self.backup_svc.purge_old_backups(days=30, max_backups=5)
        backups = [f for f in os.listdir(self.backup_svc.backup_dir) if f.startswith("state_backup_")]
        self.assertLessEqual(len(backups), 5)

    def test_post_restore_idempotency_prevents_duplicate_vouchers(self):
        """
        Proof of Task 25: Restoring an older state database snapshot before invoice sync
        does NOT create duplicate vouchers in Tally Prime, because target system pre-checks
        discover existing vouchers in Tally and adopt target IDs without creating duplicates!
        """
        mock_ext = MagicMock()
        mock_ext.ping.return_value = True

        invoice_payload = [{
            "id": "INV-RESTORE-1",
            "number": "INV-RESTORE-1",
            "customer_id": "CUST-100",
            "subtotal": 1000.0,
            "tax_amount": 180.0,
            "grand_total": 1180.0,
        }]

        # 1. State 1: Initial Backup (Invoice not synced yet)
        self.store.save("customer", "CUST-100", "TALLY-CUST-100")
        backup_res = self.backup_svc.trigger_backup()
        old_backup_file = backup_res["db_backup"]

        # 2. State 2: Invoice syncs to Tally Prime successfully
        mock_ext.check_exists_in_tally.return_value = False
        stats_1 = run_sync_pipeline(
            entity_type="invoice",
            fetch_func=lambda: invoice_payload,
            sync_func=lambda i: "TALLY-VOUCHER-RESTORE-1",
            store=self.store,
            external_client=mock_ext,
        )
        self.assertEqual(stats_1["created"], 1)

        # 3. Disaster Recovery: Database is restored to State 1 (Old Snapshot where Invoice mapping is lost!)
        self.store.db.close()
        self.backup_svc.restore_backup(old_backup_file)
        self.store = MappingStore(self.db_path)

        # Confirm local SQLite mapping is missing in restored DB
        self.assertFalse(self.store.exists("invoice", "INV-RESTORE-1"))

        # 4. Post-Restore Sync Pipeline Execution:
        # Pre-flight check discovers that voucher 'INV-RESTORE-1' ALREADY EXISTS in Tally Prime!
        mock_ext.check_exists_in_tally.return_value = True

        stats_2 = run_sync_pipeline(
            entity_type="invoice",
            fetch_func=lambda: invoice_payload,
            sync_func=lambda i: "TALLY-VOUCHER-RESTORE-1",
            store=self.store,
            external_client=mock_ext,
        )

        # ZERO DUPLICATES CREATED! Target ID adopted and local mapping restored!
        self.assertEqual(stats_2["created"], 0)
        self.assertEqual(stats_2["skipped"], 1)
        self.assertTrue(self.store.exists("invoice", "INV-RESTORE-1"))


if __name__ == "__main__":
    unittest.main()
