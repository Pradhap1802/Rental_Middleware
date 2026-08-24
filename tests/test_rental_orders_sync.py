import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

from app.mapping.store import MappingStore
from app.sync.rental_orders import sync_rental_orders


class TestRentalOrdersForwardSync(unittest.TestCase):
    """
    Cancelled RentAsst rentouts (RentStatuses::CANCELLED = 7) always have amount=None,
    so forward-syncing them fails PayloadValidator's zero-amount check and writes a fresh
    dead-letter entry every single 10-minute cycle forever — there's nothing to fix, since
    a cancelled order legitimately has no business being pushed to Tally. Confirmed live
    against real rentouts #9-#12, all status=7, re-dead-lettered on every scheduled run.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_rental_orders.db")
        self.store = MappingStore(self.db_path)

    def tearDown(self):
        if hasattr(self, "store") and self.store:
            self.store.db.close()
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_cancelled_orders_are_excluded_from_forward_sync(self):
        mock_ra_client = MagicMock()
        mock_ra_client.fetch_rental_orders.return_value = [
            {"id": 9, "number": "R100005", "status": 7, "amount": None, "customer_name": "Acme"},
            {"id": 10, "number": "R100006", "status": 7, "amount": None, "customer_name": "Acme"},
        ]
        mock_ext_client = MagicMock()

        stats = sync_rental_orders(
            rentasst_client=mock_ra_client,
            external_client=mock_ext_client,
            store=self.store,
        )

        mock_ext_client.sync_rental_order.assert_not_called()
        self.assertEqual(stats["processed"], 0)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(len(self.store.list_dead_letters(entity_type="rental_order")), 0)

    def test_non_cancelled_orders_still_sync_normally(self):
        mock_ra_client = MagicMock()
        mock_ra_client.fetch_rental_orders.return_value = [
            {"id": 20, "number": "R100020", "status": 1, "amount": 500.0, "customer_name": "Acme"},
        ]
        mock_ext_client = MagicMock()
        mock_ext_client.check_exists_in_tally.return_value = False
        mock_ext_client.sync_rental_order.return_value = "RENTAL-ORD-20"

        stats = sync_rental_orders(
            rentasst_client=mock_ra_client,
            external_client=mock_ext_client,
            store=self.store,
        )

        mock_ext_client.sync_rental_order.assert_called_once()
        self.assertEqual(stats["created"], 1)

    def test_rent_items_are_attached_before_forward_sync(self):
        """
        fetch_rental_orders()'s list view only returns a bare 'rent_items_count' integer,
        no item detail — build_sales_order_voucher_xml's item lookup always found nothing
        without this, so every forward-synced Sales Order voucher landed in Tally
        header-only regardless of how many real rent items the order had. Confirmed live
        against rentout R100016 (id 20). get_rent_items() must be called for any order
        with a non-zero rent_items_count, and its result mapped into the item shape
        build_sales_order_voucher_xml actually reads (name/quantity/price/total_price/unit).
        """
        mock_ra_client = MagicMock()
        mock_ra_client.fetch_rental_orders.return_value = [
            {"id": 20, "number": "R100016", "status": 1, "amount": 118.0, "customer_name": "Acme", "rent_items_count": 1},
        ]
        mock_ra_client.get_rent_items.return_value = [
            {
                "id": 28, "asset_id": 8, "asset_name": "Standee Banner",
                "rented_quantity": 1, "price": 100, "total_price": 100,
                "asset": {"asset_unit": {"name": "Nos"}},
            }
        ]
        mock_ext_client = MagicMock()
        mock_ext_client.check_exists_in_tally.return_value = False
        mock_ext_client.sync_rental_order.return_value = "RENTAL-ORD-20"

        sync_rental_orders(
            rentasst_client=mock_ra_client,
            external_client=mock_ext_client,
            store=self.store,
        )

        mock_ra_client.get_rent_items.assert_called_once_with(20)
        pushed_order = mock_ext_client.sync_rental_order.call_args[0][0]
        self.assertEqual(len(pushed_order["items"]), 1)
        self.assertEqual(pushed_order["items"][0]["name"], "Standee Banner")
        self.assertEqual(pushed_order["items"][0]["quantity"], 1)
        self.assertEqual(pushed_order["items"][0]["price"], 100)
        self.assertEqual(pushed_order["items"][0]["total_price"], 100)
        self.assertEqual(pushed_order["items"][0]["unit"], "Nos")

    def test_rent_items_not_fetched_when_order_has_none(self):
        """No wasted GET when rent_items_count is 0/absent."""
        mock_ra_client = MagicMock()
        mock_ra_client.fetch_rental_orders.return_value = [
            {"id": 21, "number": "R100017", "status": 1, "amount": 50.0, "customer_name": "Acme", "rent_items_count": 0},
        ]
        mock_ext_client = MagicMock()
        mock_ext_client.check_exists_in_tally.return_value = False
        mock_ext_client.sync_rental_order.return_value = "RENTAL-ORD-21"

        sync_rental_orders(
            rentasst_client=mock_ra_client,
            external_client=mock_ext_client,
            store=self.store,
        )

        mock_ra_client.get_rent_items.assert_not_called()


if __name__ == "__main__":
    unittest.main()
