import unittest
from unittest.mock import MagicMock

import requests

from app.clients.rentasst_client import RentAsstClient
from app.models.domain import AppConfig


class TestPushRentout(unittest.TestCase):
    """
    push_rentout previously fell back through ['create-rent-details', 'rent', 'rents',
    'rental-orders', 'invoice', 'invoices'] whenever a response wasn't a plain success —
    _post_with_fallback only treats 404/405 as "try the next endpoint", so a genuine 422
    validation failure at the real 'create-rent-details' endpoint cascaded all the way to
    posting the rentout payload at the invoice-create endpoint instead, producing a
    misleading "422 at /invoices" error for what was actually a rejected rent-order payload.
    """

    def setUp(self):
        self.cfg = AppConfig(rentasst_url="http://localhost:8000/api", rentasst_api_key="test-key")
        self.client = RentAsstClient(self.cfg)

    def test_push_rentout_only_posts_to_create_rent_details(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"id": 55}
        self.client.session.post = MagicMock(return_value=mock_response)

        self.client.push_rentout({"customer_id": 1, "status": 1})

        self.client.session.post.assert_called_once()
        called_url = self.client.session.post.call_args[0][0]
        self.assertTrue(called_url.endswith("/create-rent-details"), called_url)

    def test_push_rentout_surfaces_real_error_without_falling_back_to_invoices(self):
        mock_response = MagicMock()
        mock_response.status_code = 422
        mock_response.text = '{"message":"The rent from field must match the format Y-m-d H:i:s."}'
        mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError(response=mock_response)
        self.client.session.post = MagicMock(return_value=mock_response)

        with self.assertRaises(Exception):
            self.client.push_rentout({"customer_id": 1, "status": 1})

        # Must fail at create-rent-details itself — never silently retried against the
        # unrelated invoice-create endpoint.
        self.client.session.post.assert_called_once()
        called_url = self.client.session.post.call_args[0][0]
        self.assertTrue(called_url.endswith("/create-rent-details"), called_url)


class TestFetchBusinesses(unittest.TestCase):
    """
    fetch_businesses previously only tried ['user/businesses', 'business', 'tenants',
    'businesses'] — none of these are real RentAsst routes (confirmed against
    routes/api.php), so every call 404'd through the whole list and surfaced as a 400 on
    GET /api/companies/rentasst. The real route is 'get_user_business_list'
    (UserController@getUserActiveBusinesses).
    """

    def setUp(self):
        self.cfg = AppConfig(rentasst_url="http://localhost:8000/api", rentasst_api_key="test-key")
        self.client = RentAsstClient(self.cfg)

    def test_fetch_businesses_uses_real_endpoint(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = [{"id": 1, "business_name": "Acme Rentals", "business_code": "acme"}]
        self.client.session.get = MagicMock(return_value=mock_response)

        result = self.client.fetch_businesses()

        self.client.session.get.assert_called_once()
        called_url = self.client.session.get.call_args[0][0]
        self.assertTrue(called_url.endswith("/get_user_business_list"), called_url)
        self.assertEqual(result, [{"id": 1, "business_name": "Acme Rentals", "business_code": "acme"}])


class TestPushRentoutItems(unittest.TestCase):
    """
    RentAsst's create-rent-details endpoint silently drops an 'items' field on the
    rentout payload — RentItem is a separate rent_items table, not a Rent column
    (confirmed against RentService::createNewRent()'s Rent::create($requestData) and
    RentItem::$fillable in RentAsst's own source). Line items must be pushed via the
    bulk /store/rent_items/{rent_id} endpoint instead, as a plain top-level JSON array
    (RentItem::arrayRules() validates '*.rented_quantity' etc, not an 'items' envelope).
    """

    def setUp(self):
        self.cfg = AppConfig(rentasst_url="http://localhost:8000/api", rentasst_api_key="test-key")
        self.client = RentAsstClient(self.cfg)

    def test_push_rentout_items_posts_plain_array_to_bulk_endpoint(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"data": {"id": 5}}
        self.client.session.post = MagicMock(return_value=mock_response)

        self.client.push_rentout_items("42", [{"asset_id": 17, "rented_quantity": 1, "price": 97.0, "total_price": 97.0}])

        self.client.session.post.assert_called_once()
        call = self.client.session.post.call_args
        self.assertTrue(call[0][0].endswith("/store/rent_items/42"), call[0][0])
        posted_body = call[1]["json"]
        self.assertIsInstance(posted_body, list)
        self.assertEqual(posted_body[0]["asset_id"], 17)
        self.assertEqual(posted_body[0]["rent_id"], 42)


class TestFetchPaymentsEnrichment(unittest.TestCase):
    """
    fetch_payments()'s list endpoint has no 'payment_date' field at all (only
    'created_at') and no 'paid_by'/customer name — confirmed live against a real
    invoice-linked payment. build_receipt_voucher_xml then falls back to today's date
    and a generic "Cash Customer" ledger instead of the real customer.
    """

    def setUp(self):
        self.cfg = AppConfig(rentasst_url="http://localhost:8000/api", rentasst_api_key="test-key")
        self.client = RentAsstClient(self.cfg)

    def test_enriches_payment_date_and_customer_name_via_invoice(self):
        list_response = MagicMock()
        list_response.status_code = 200
        list_response.raise_for_status.return_value = None
        list_response.json.return_value = [
            {"id": 37, "invoice_id": 34, "rent_id": None, "amount": 2360, "paid_by": None}
        ]

        detail_response = MagicMock()
        detail_response.status_code = 200
        detail_response.raise_for_status.return_value = None
        detail_response.json.return_value = {
            "id": 37, "invoice_id": 34, "rent_id": None, "amount": 2360,
            "paid_by": None, "payment_date": "25.08.2026 10:57", "rent": None,
        }

        invoice_response = MagicMock()
        invoice_response.status_code = 200
        invoice_response.raise_for_status.return_value = None
        invoice_response.json.return_value = {"id": 34, "customer": {"id": 14, "name": "Felix"}}

        def fake_get(url, **kwargs):
            if url.endswith("/payment") or url.endswith("/payments"):
                return list_response
            if url.endswith("/payment/37"):
                return detail_response
            if "invoice" in url and url.endswith("/34"):
                return invoice_response
            raise AssertionError(f"unexpected URL: {url}")

        self.client.session.get = MagicMock(side_effect=fake_get)

        result = self.client.fetch_payments()

        self.assertEqual(result[0]["payment_date"], "25.08.2026 10:57")
        self.assertEqual(result[0]["paid_by"], "Felix")


if __name__ == "__main__":
    unittest.main()
