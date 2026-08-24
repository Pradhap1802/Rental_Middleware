import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

from app.mapping.store import MappingStore
from app.sync.equipment import sync_equipment


class TestEquipmentStockReconciliation(unittest.TestCase):
    """
    Tally-side stock drift (from Sales vouchers consuming inventory there) is
    independent of whether an item's RentAsst record changed — confirmed live, a stock
    item drifted to a negative CLOSINGBALANCE in Tally while its RentAsst
    available_quantity stayed the same the whole time. Reconciliation must therefore run
    every cycle for every item with a known quantity, regardless of run_sync_pipeline's
    content-hash dedup on the master-data push.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_equipment.db")
        self.store = MappingStore(self.db_path)

    def tearDown(self):
        if hasattr(self, "store") and self.store:
            self.store.db.close()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_reconciliation_runs_even_when_master_data_sync_is_skipped(self):
        mock_ra_client = MagicMock()
        mock_ra_client.fetch_equipment.return_value = [
            {"id": 8, "name": "Standee Banner", "available_quantity": 11},
        ]
        mock_ext_client = MagicMock()
        mock_ext_client.check_exists_in_tally.return_value = True  # already synced, hash unchanged -> skip

        stats = sync_equipment(
            rentasst_client=mock_ra_client,
            external_client=mock_ext_client,
            store=self.store,
        )

        self.assertEqual(stats["skipped"], 1)
        mock_ext_client.sync_equipment.assert_not_called()
        # Reconciliation still fires, independent of the master-data skip above.
        mock_ext_client.reconcile_equipment_stock.assert_called_once_with("Standee Banner", 11, unit="Nos")

    def test_reconciliation_skips_items_with_no_known_quantity(self):
        mock_ra_client = MagicMock()
        mock_ra_client.fetch_equipment.return_value = [
            {"id": 9, "name": "Untracked Item", "available_quantity": None},
        ]
        mock_ext_client = MagicMock()
        mock_ext_client.check_exists_in_tally.return_value = False

        sync_equipment(
            rentasst_client=mock_ra_client,
            external_client=mock_ext_client,
            store=self.store,
        )

        mock_ext_client.reconcile_equipment_stock.assert_not_called()

    def test_reconciliation_uses_asset_unit_name(self):
        mock_ra_client = MagicMock()
        mock_ra_client.fetch_equipment.return_value = [
            {"id": 8, "name": "Dell Laptop 3440", "available_quantity": 5, "asset_unit": {"name": "Piece"}},
        ]
        mock_ext_client = MagicMock()
        mock_ext_client.check_exists_in_tally.return_value = True

        sync_equipment(
            rentasst_client=mock_ra_client,
            external_client=mock_ext_client,
            store=self.store,
        )

        mock_ext_client.reconcile_equipment_stock.assert_called_once_with("Dell Laptop 3440", 5, unit="Piece")


if __name__ == "__main__":
    unittest.main()
