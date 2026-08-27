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


class TestPostWithFallbackAllEndpointsFail(unittest.TestCase):
    """
    _post_with_fallback previously fell through to a fabricated {"id": "RA-MOCK-ID",
    "status": "success"} response when every candidate endpoint 404/405'd (e.g. a wrong
    base_url, or a route not yet released on this RentAsst deployment) — the caller then
    saved a "synced" mapping for a record that was never actually created in RentAsst,
    with no error, no dead letter, and no retry. It must now raise instead.
    """

    def setUp(self):
        self.cfg = AppConfig(rentasst_url="http://localhost:8000/api", rentasst_api_key="test-key")
        self.client = RentAsstClient(self.cfg)

    def test_all_endpoints_404_raises_instead_of_fabricating_success(self):
        mock_response = MagicMock()
        mock_response.status_code = 404
        self.client.session.post = MagicMock(return_value=mock_response)

        with self.assertRaises(Exception):
            self.client.push_customer({"name": "Test Customer", "mobile": "9000000000"})

        self.assertEqual(self.client.session.post.call_count, 2)  # "customer", "customers"

    def test_mixed_exception_then_404_raises_the_real_exception(self):
        mock_404 = MagicMock()
        mock_404.status_code = 404
        self.client.session.post = MagicMock(
            side_effect=[requests.exceptions.ConnectionError("refused"), mock_404]
        )

        with self.assertRaises(requests.exceptions.ConnectionError):
            self.client.push_customer({"name": "Test Customer", "mobile": "9000000000"})


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
        mock_response.headers = {"content-type": "application/json"}
        mock_response.text = '[{"id": 1}]'
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

    def _json_response(self, payload):
        r = MagicMock()
        r.status_code = 200
        r.headers = {"content-type": "application/json"}
        r.text = "response body"
        r.raise_for_status.return_value = None
        r.json.return_value = payload
        return r

    def test_enriches_payment_date_and_customer_name_via_invoice(self):
        list_response = self._json_response(
            [{"id": 37, "invoice_id": 34, "rent_id": None, "amount": 2360, "paid_by": None}]
        )
        detail_response = self._json_response({
            "id": 37, "invoice_id": 34, "rent_id": None, "amount": 2360,
            "paid_by": None, "payment_date": "25.08.2026 10:57", "rent": None,
        })
        invoice_response = self._json_response({"id": 34, "customer": {"id": 14, "name": "Felix"}})

        def fake_get(url, **kwargs):
            if url.endswith("/payment") or url.endswith("/payments") or url.endswith("/payment-list"):
                return list_response
            if url.endswith("/payment/37"):
                return detail_response
            if "invoice" in url and url.endswith("/34"):
                return invoice_response
            raise AssertionError(f"unexpected URL: {url}")

        # _request_with_fallback tries a POST DataTable endpoint first (discovery
        # order) — no server is actually listening in this test, so make that
        # attempt fail fast and cleanly instead of hitting a real socket.
        failed_post = MagicMock()
        failed_post.status_code = 404
        self.client.session.post = MagicMock(return_value=failed_post)
        self.client.session.get = MagicMock(side_effect=fake_get)

        result = self.client.fetch_payments()

        self.assertEqual(result[0]["payment_date"], "25.08.2026 10:57")
        self.assertEqual(result[0]["paid_by"], "Felix")


class TestCheckExistsInRentasst(unittest.TestCase):
    """
    check_exists_in_rentasst previously trusted a raw HTTP 200 as proof a record exists.
    Confirmed live against the real RentAsst API: several of the fallback paths it tries
    aren't real routes for that entity (e.g. 'customers/{id}' plural, 'invoice/{id}'
    singular for an invoice lookup) — an invalid path falls through to the SPA's
    catch-all route, which returns HTTP 200 with a generic HTML shell instead of a 404.
    That false "exists" masked a genuinely deleted RentAsst customer forever, blocking
    the reverse-sync self-healing (re-create) path keyed off this check.
    """

    def setUp(self):
        self.cfg = AppConfig(rentasst_url="http://localhost:8000/api", rentasst_api_key="test-key")
        self.client = RentAsstClient(self.cfg)

    def test_html_shell_on_200_is_not_treated_as_existing(self):
        html_response = MagicMock()
        html_response.status_code = 200
        html_response.json.side_effect = ValueError("No JSON object could be decoded")

        real_404 = MagicMock()
        real_404.status_code = 404

        def fake_get(url, **kwargs):
            if url.endswith("/customer/3"):
                return real_404
            if url.endswith("/customers/3"):
                return html_response
            raise AssertionError(f"unexpected URL: {url}")

        self.client.session.get = MagicMock(side_effect=fake_get)

        self.assertFalse(self.client.check_exists_in_rentasst("customer", "3"))

    def test_real_json_record_on_200_is_treated_as_existing(self):
        json_response = MagicMock()
        json_response.status_code = 200
        json_response.json.return_value = {"id": 1, "name": "Test"}

        self.client.session.get = MagicMock(return_value=json_response)

        self.assertTrue(self.client.check_exists_in_rentasst("customer", "1"))

    def test_equipment_html_shell_on_200_is_not_treated_as_existing(self):
        """Same SPA-catch-all trap confirmed live for equipment: 'equipment/{id}' isn't a
        real route (only 'asset/{id}' is) and returns HTML with status 200 for ANY id,
        including one that was never created."""
        real_404 = MagicMock()
        real_404.status_code = 404

        html_response = MagicMock()
        html_response.status_code = 200
        html_response.json.side_effect = ValueError("No JSON object could be decoded")

        def fake_get(url, **kwargs):
            if url.endswith("/asset/99"):
                return real_404
            if url.endswith("/equipment/99"):
                return html_response
            raise AssertionError(f"unexpected URL: {url}")

        self.client.session.get = MagicMock(side_effect=fake_get)

        self.assertFalse(self.client.check_exists_in_rentasst("equipment", "99"))

    def test_equipment_real_json_record_on_200_is_treated_as_existing(self):
        json_response = MagicMock()
        json_response.status_code = 200
        json_response.json.return_value = {"id": 1, "name": "Dell Laptop"}

        self.client.session.get = MagicMock(return_value=json_response)

        self.assertTrue(self.client.check_exists_in_rentasst("equipment", "1"))

    def test_total_connection_failure_fails_open_not_treated_as_deleted(self):
        """
        Confirmed live: RentAsst's local API has intermittent outages. If every
        endpoint attempt raises (timeout/connection error) rather than returning a
        real HTTP response, that must NOT be read as "record deleted" — reverse
        sync's self-heal path drops the mapping and creates a genuine duplicate asset
        the instant that happens, which is exactly what occurred live to both
        'Dell Laptop' and 'Dell Mouse'.
        """
        import requests as requests_module

        self.client.session.get = MagicMock(side_effect=requests_module.exceptions.ReadTimeout("timed out"))

        self.assertTrue(self.client.check_exists_in_rentasst("equipment", "1"))

    def test_real_404_response_still_treated_as_not_existing(self):
        # A genuine HTTP response (even a 404) is a real signal from the server, unlike
        # a network-level failure -- this must still resolve to "doesn't exist".
        real_404 = MagicMock()
        real_404.status_code = 404

        self.client.session.get = MagicMock(return_value=real_404)

        self.assertFalse(self.client.check_exists_in_rentasst("equipment", "1"))


if __name__ == "__main__":
    unittest.main()
