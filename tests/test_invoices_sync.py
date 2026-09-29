import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

from app.mapping.store import MappingStore
from app.sync.invoices import sync_invoices


class TestInvoicesForwardSync(unittest.TestCase):
    """
    An invoice generated from a Rent Out (order_type == "rent") carries its line items
    on the underlying rentout's rent_items table, not its own invoice_items — confirmed
    live: GET /invoices/34 (generated from rentout #22) returns "items": [] even though
    get-rent-items/22 has the real Dell Mouse line. Without attaching them, every such
    invoice forward-synced to Tally with zero inventory lines, and Tally then had
    nothing for the reverse sync to read back either.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_invoices.db")
        self.store = MappingStore(self.db_path)

    def tearDown(self):
        if hasattr(self, "store") and self.store:
            self.store.db.close()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_rent_order_invoice_gets_items_attached_from_rent_items(self):
        mock_ra_client = MagicMock()
        mock_ra_client.fetch_invoices.return_value = [
            {"id": 34, "number": "31", "customer_id": 14, "order_id": 22, "order_type": "rent", "subtotal": 2000, "grand_total": 2360, "items": []},
        ]
        mock_ra_client.get_rent_items.return_value = [
            {
                "id": 34, "asset_id": 16, "asset_name": "Dell Mouse",
                "rented_quantity": 20, "price": 100, "total_price": 2000,
                "asset": {"id": 16, "name": "Dell Mouse", "asset_unit": None},
            }
        ]
        mock_ext_client = MagicMock()
        mock_ext_client.check_exists_in_tally.return_value = False
        mock_ext_client.sync_invoice.return_value = "RENTAL-INV-34"

        self.store.save_mapping("customer", "14", "Felix", status="synced")
        self.store.save_mapping("equipment", "16", "TALLY-ID-16", status="synced")

        sync_invoices(
            rentasst_client=mock_ra_client,
            external_client=mock_ext_client,
            store=self.store,
        )

        mock_ra_client.get_rent_items.assert_called_once_with(22)
        pushed_invoice = mock_ext_client.sync_invoice.call_args[0][0]
        self.assertEqual(len(pushed_invoice["items"]), 1)
        self.assertEqual(pushed_invoice["items"][0]["name"], "Dell Mouse")
        self.assertEqual(pushed_invoice["items"][0]["quantity"], 20)
        self.assertEqual(pushed_invoice["items"][0]["total_price"], 2000)

    def test_rent_order_invoice_gets_the_orders_reference_number_attached(self):
        """
        build_sales_invoice_voucher_xml stamps data['rent']['number'] onto the Tally
        invoice voucher as a <REFERENCE> tag so it's traceable back to its order —
        but fetch_invoices() never returns a nested 'rent' object at all (confirmed
        live: GET /invoices/4 returns only a flat order_id), so that reference was
        silently always blank. get_rent_items()'s own item rows already carry the
        parent order's 'rent' relation (including its RentAsst display number, e.g.
        'R100003') — this must be extracted and attached even when 'items' was
        already populated by an earlier sync pass.
        """
        mock_ra_client = MagicMock()
        mock_ra_client.fetch_invoices.return_value = [
            {
                "id": 34, "number": "31", "customer_id": 14, "order_id": 22, "order_type": "rent",
                "subtotal": 2000, "grand_total": 2360,
                "items": [{"name": "Dell Mouse", "asset_id": 16, "quantity": 20}],
            },
        ]
        mock_ra_client.get_rent_items.return_value = [
            {
                "id": 34, "asset_id": 16, "asset_name": "Dell Mouse",
                "rented_quantity": 20, "price": 100, "total_price": 2000,
                "asset": {"id": 16, "name": "Dell Mouse", "asset_unit": None},
                "rent": {"id": 22, "number": "R100003"},
            }
        ]
        mock_ext_client = MagicMock()
        mock_ext_client.check_exists_in_tally.return_value = False
        mock_ext_client.sync_invoice.return_value = "RENTAL-INV-34"

        self.store.save_mapping("customer", "14", "Felix", status="synced")
        self.store.save_mapping("equipment", "16", "TALLY-ID-16", status="synced")

        sync_invoices(
            rentasst_client=mock_ra_client,
            external_client=mock_ext_client,
            store=self.store,
        )

        # Items were already present, but get_rent_items still had to be called to
        # recover the reference.
        mock_ra_client.get_rent_items.assert_called_once_with(22)
        pushed_invoice = mock_ext_client.sync_invoice.call_args[0][0]
        self.assertEqual(pushed_invoice["rent"]["number"], "R100003")

    def test_standalone_invoice_with_own_items_is_left_alone(self):
        """An invoice that already has real items (not order_type=='rent') must not be touched."""
        mock_ra_client = MagicMock()
        mock_ra_client.fetch_invoices.return_value = [
            {
                "id": 26, "number": "20", "customer_id": 10, "order_type": "standalone",
                "subtotal": 20, "grand_total": 20,
                "items": [{"name": "Dell Laptop Bag", "asset_id": 5, "quantity": 2}],
            },
        ]
        mock_ext_client = MagicMock()
        mock_ext_client.check_exists_in_tally.return_value = False
        mock_ext_client.sync_invoice.return_value = "RENTAL-INV-26"

        self.store.save_mapping("customer", "10", "Acme", status="synced")
        self.store.save_mapping("equipment", "5", "TALLY-ID-5", status="synced")

        sync_invoices(
            rentasst_client=mock_ra_client,
            external_client=mock_ext_client,
            store=self.store,
        )

        mock_ra_client.get_rent_items.assert_not_called()
        pushed_invoice = mock_ext_client.sync_invoice.call_args[0][0]
        self.assertEqual(pushed_invoice["items"][0]["name"], "Dell Laptop Bag")

    def test_invoice_without_order_id_is_left_alone(self):
        mock_ra_client = MagicMock()
        mock_ra_client.fetch_invoices.return_value = [
            {"id": 40, "number": "35", "customer_id": 10, "order_type": "rent", "order_id": None, "items": []},
        ]
        mock_ext_client = MagicMock()
        mock_ext_client.check_exists_in_tally.return_value = False
        mock_ext_client.sync_invoice.return_value = "RENTAL-INV-40"

        self.store.save_mapping("customer", "10", "Acme", status="synced")

        sync_invoices(
            rentasst_client=mock_ra_client,
            external_client=mock_ext_client,
            store=self.store,
        )

        mock_ra_client.get_rent_items.assert_not_called()


if __name__ == "__main__":
    unittest.main()
