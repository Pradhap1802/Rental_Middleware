import unittest
from unittest.mock import MagicMock

import requests

from app.models.domain import AppConfig
from app.connectors.tally.xml_builder import sanitize_tally_xml, escape_xml, format_tally_date
from app.connectors.tally.parser import validate_tally_accounting_success
from app.connectors.tally.client import TallyClient
from app.connectors.tally.ledger import build_customer_ledger_xml
from app.connectors.tally.sales_voucher import build_sales_invoice_voucher_xml, build_sales_order_voucher_xml
from app.connectors.tally.receipt_voucher import build_receipt_voucher_xml
from app.connectors.tally.stock_item import build_physical_stock_voucher_xml
from app.connectors.tally.unit_match import resolve_existing_unit_name


class TestTallyConnectorAndValidation(unittest.TestCase):

    def test_xml_sanitization_and_escaping(self):
        dirty_xml = "\ufeff<ENVELOPE xmlns:UDF='TallyUDF'><HEADER><VERSION>1</VERSION></HEADER><BODY><DATA>Sub &amp; Co</DATA></BODY></ENVELOPE>"
        clean = sanitize_tally_xml(dirty_xml)
        self.assertNotIn("\ufeff", clean)
        self.assertNotIn("xmlns:UDF", clean)

        escaped = escape_xml("Rent & Sales Co <Pvt Ltd>")
        self.assertEqual(escaped, "Rent &amp; Sales Co &lt;Pvt Ltd&gt;")

        dt = format_tally_date("2026-08-15")
        self.assertEqual(dt, "20260815")  # real transaction date by default

        dt_edu = format_tally_date("2026-08-15", edu_mode=True)
        self.assertEqual(dt_edu, "20260801")  # explicit EDU mode still forces 01st

    def test_app_config_defaults_edu_mode_off(self):
        """
        tally_edu_mode must default to False: real transaction dates should always reach
        Tally unless an operator explicitly opts into Educational-mode's 1st/2nd/last-day
        date restriction for a demo/test company.
        """
        cfg = AppConfig(external_url="http://localhost:9000", external_system_type="tally")
        self.assertFalse(cfg.tally_edu_mode)

    def test_successful_tally_accounting_response(self):
        success_xml = """<ENVELOPE>
  <HEADER><VERSION>1</VERSION></HEADER>
  <BODY>
    <IMPORTRESULT>
      <CREATED>1</CREATED>
      <ALTERED>0</ALTERED>
      <DELETED>0</DELETED>
      <LASTVOUCHERID>10502</LASTVOUCHERID>
    </IMPORTRESULT>
  </BODY>
</ENVELOPE>"""
        is_success, err_msg, tally_id = validate_tally_accounting_success(success_xml)
        self.assertTrue(is_success)
        self.assertIsNone(err_msg)
        self.assertEqual(tally_id, "TALLY-ID-10502")

    def test_failed_tally_response_with_http_200_and_lineerror(self):
        """
        CRITICAL TEST: Tally returns HTTP 200 but XML contains <LINEERROR> (e.g. Ledger missing).
        Validator MUST return is_success = False and extract the exact error string.
        """
        line_error_xml = """<ENVELOPE>
  <HEADER><VERSION>1</VERSION></HEADER>
  <BODY>
    <IMPORTRESULT>
      <CREATED>0</CREATED>
      <ALTERED>0</ALTERED>
      <LINEERROR>Line 1: Ledger 'Rental Income' does not exist in Tally database.</LINEERROR>
      <LINEERROR>Line 4: GSTIN format is invalid for party 'Customer X'.</LINEERROR>
    </IMPORTRESULT>
  </BODY>
</ENVELOPE>"""
        is_success, err_msg, tally_id = validate_tally_accounting_success(line_error_xml)
        self.assertFalse(is_success)
        self.assertIsNone(tally_id)
        self.assertIsNotNone(err_msg)
        self.assertIn("Ledger 'Rental Income' does not exist", err_msg)
        self.assertIn("GSTIN format is invalid", err_msg)

    def test_zero_creation_response_detection(self):
        """
        Tests response returning HTTP 200 with CREATED=0 and ALTERED=0 and no errors.
        Must be treated as an accounting failure.
        """
        zero_xml = """<ENVELOPE>
  <HEADER><VERSION>1</VERSION></HEADER>
  <BODY>
    <IMPORTRESULT>
      <CREATED>0</CREATED>
      <ALTERED>0</ALTERED>
    </IMPORTRESULT>
  </BODY>
</ENVELOPE>"""
        is_success, err_msg, tally_id = validate_tally_accounting_success(zero_xml)
        self.assertFalse(is_success)
        self.assertIn("0 records created or altered", err_msg)

    def test_tally_client_send_xml_raises_on_lineerror(self):
        cfg = AppConfig(external_url="http://localhost:9000", external_system_type="tally")
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"""<ENVELOPE>
  <HEADER><VERSION>1</VERSION></HEADER>
  <BODY>
    <IMPORTRESULT>
      <CREATED>0</CREATED>
      <LINEERROR>Line 2: Stock Item 'Generator-500' is out of stock.</LINEERROR>
    </IMPORTRESULT>
  </BODY>
</ENVELOPE>"""
        mock_session.post.return_value = mock_resp

        client = TallyClient(cfg, session=mock_session)

        # send_xml MUST raise ValueError on HTTP 200 containing <LINEERROR>
        with self.assertRaises(ValueError) as ctx:
            client.send_xml("<ENVELOPE>Test</ENVELOPE>")

        self.assertIn("Stock Item 'Generator-500' is out of stock", str(ctx.exception))

    def test_edu_mode_is_auto_detected_and_voucher_is_retried(self):
        """
        Confirmed live: a Tally company running under Educational/unlicensed mode
        rejects every correctly-dated voucher with "Voucher date is missing..."
        regardless of payload correctness (a hand-built minimal voucher with a valid
        <DATE> tag was rejected identically), while the exact same voucher re-dated to
        the 1st of the month succeeds outright. Rather than requiring an operator to
        notice this pattern and manually flip tally_edu_mode, the client must detect it
        from Tally's own rejection, retry once with the date forced, and remember the
        setting for the rest of this run instead of re-discovering (and re-failing) it
        on every subsequent voucher.
        """
        import re
        cfg = AppConfig(external_url="http://localhost:9000", external_system_type="tally")
        self.assertFalse(cfg.tally_edu_mode)
        mock_session = MagicMock()

        check_exists_resp = MagicMock()
        check_exists_resp.status_code = 200
        check_exists_resp.content = b"<ENVELOPE><BODY><DATA><COLLECTION></COLLECTION></DATA></BODY></ENVELOPE>"

        date_missing_resp = MagicMock()
        date_missing_resp.status_code = 200
        date_missing_resp.content = b"""<ENVELOPE><BODY><IMPORTRESULT>
            <CREATED>0</CREATED>
            <LINEERROR>Voucher date is missing for: 'Sales' voucher 31.  Verify the data, resolve errors (if any) and retry Split.</LINEERROR>
        </IMPORTRESULT></BODY></ENVELOPE>"""

        success_resp = MagicMock()
        success_resp.status_code = 200
        success_resp.content = b"<ENVELOPE><BODY><IMPORTRESULT><CREATED>1</CREATED><LASTVCHID>9</LASTVCHID></IMPORTRESULT></BODY></ENVELOPE>"

        mock_session.post.side_effect = [check_exists_resp, date_missing_resp, success_resp]
        client = TallyClient(cfg, session=mock_session)

        result = client.sync_invoice({"id": 34, "number": "31", "customer_name": "Felix", "subtotal": 2000, "grand_total": 2360})

        self.assertEqual(result, "RENTAL-INV-34")
        self.assertEqual(mock_session.post.call_count, 3)  # check_exists, failed attempt, retry
        self.assertTrue(cfg.tally_edu_mode)
        self.assertTrue(client.edu_mode_auto_detected)

        retry_call = mock_session.post.call_args_list[2]
        retry_body = retry_call.kwargs.get("data")
        retry_body = retry_body.decode("utf-8") if isinstance(retry_body, bytes) else retry_body
        m = re.search(r"<DATE>(\d{8})</DATE>", retry_body)
        self.assertIsNotNone(m)
        self.assertTrue(m.group(1).endswith("01"), f"retry must be dated the 1st, got {m.group(1)}")

    def test_edu_fallback_does_not_retry_once_already_in_edu_mode(self):
        """A second, still-rejected voucher while already in edu mode must not loop."""
        cfg = AppConfig(external_url="http://localhost:9000", external_system_type="tally", tally_edu_mode=True)
        mock_session = MagicMock()

        check_exists_resp = MagicMock()
        check_exists_resp.status_code = 200
        check_exists_resp.content = b"<ENVELOPE><BODY><DATA><COLLECTION></COLLECTION></DATA></BODY></ENVELOPE>"

        date_missing_resp = MagicMock()
        date_missing_resp.status_code = 200
        date_missing_resp.content = b"""<ENVELOPE><BODY><IMPORTRESULT>
            <CREATED>0</CREATED>
            <LINEERROR>Voucher date is missing for: 'Sales' voucher 31.  Verify the data, resolve errors (if any) and retry Split.</LINEERROR>
        </IMPORTRESULT></BODY></ENVELOPE>"""

        mock_session.post.side_effect = [check_exists_resp, date_missing_resp]
        client = TallyClient(cfg, session=mock_session)

        with self.assertRaises(ValueError):
            client.sync_invoice({"id": 34, "number": "31", "customer_name": "Felix", "subtotal": 2000, "grand_total": 2360})

        self.assertEqual(mock_session.post.call_count, 2)  # no retry loop
        self.assertFalse(client.edu_mode_auto_detected)

    def test_edu_fallback_does_not_mask_unrelated_business_errors(self):
        """An unrelated Tally rejection (e.g. missing stock item) must surface as-is, not be swallowed as an edu-mode retry."""
        cfg = AppConfig(external_url="http://localhost:9000", external_system_type="tally")
        mock_session = MagicMock()

        check_exists_resp = MagicMock()
        check_exists_resp.status_code = 200
        check_exists_resp.content = b"<ENVELOPE><BODY><DATA><COLLECTION></COLLECTION></DATA></BODY></ENVELOPE>"

        other_error_resp = MagicMock()
        other_error_resp.status_code = 200
        other_error_resp.content = b"""<ENVELOPE><BODY><IMPORTRESULT>
            <CREATED>0</CREATED>
            <LINEERROR>Stock Item 'Dell Mouse' does not exist!</LINEERROR>
        </IMPORTRESULT></BODY></ENVELOPE>"""

        mock_session.post.side_effect = [check_exists_resp, other_error_resp]
        client = TallyClient(cfg, session=mock_session)

        with self.assertRaises(ValueError) as ctx:
            client.sync_invoice({"id": 34, "number": "31", "customer_name": "Felix", "subtotal": 2000, "grand_total": 2360})

        self.assertIn("Dell Mouse", str(ctx.exception))
        self.assertEqual(mock_session.post.call_count, 2)  # no retry attempted
        self.assertFalse(cfg.tally_edu_mode)
        self.assertFalse(client.edu_mode_auto_detected)

    def test_check_exists_does_not_false_positive_on_substring(self):
        """
        check_exists() used to do a plain substring search over the whole raw export
        (`identifier.lower() in clean.lower()`) — confirmed live: a "Piece" unit check
        returned True even though no such UNIT master existed, purely because the word
        "Piece" appeared elsewhere in the response (e.g. inside another field's text).
        sync_equipment() then skipped creating the UNIT prerequisite and Tally rejected
        the whole STOCKITEM with "Unit 'Piece' does not exist!". The check must only
        match an exact <NAME> field value, not any substring of the response.
        """
        cfg = AppConfig(external_url="http://localhost:9000", external_system_type="tally")
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        # "Piece" appears in a stock item's description — not as an actual UNIT NAME.
        mock_resp.content = b"""<ENVELOPE>
  <BODY><DATA><COLLECTION>
    <UNIT><NAME>Nos</NAME></UNIT>
    <UNIT><NAME>Box</NAME></UNIT>
    <STOCKITEM><DESCRIPTION>Sold per Piece</DESCRIPTION></STOCKITEM>
  </COLLECTION></DATA></BODY>
</ENVELOPE>"""
        mock_session.post.return_value = mock_resp
        client = TallyClient(cfg, session=mock_session)

        self.assertFalse(client.check_exists("unit", "Piece"))
        self.assertTrue(client.check_exists("unit", "Nos"))
        self.assertTrue(client.check_exists("unit", "box"))  # case-insensitive exact match

    def test_check_exists_is_cached_for_the_life_of_the_client(self):
        cfg = AppConfig(external_url="http://localhost:9000", external_system_type="tally")
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"<ENVELOPE><BODY><DATA><COLLECTION><UNIT><NAME>Nos</NAME></UNIT></COLLECTION></DATA></BODY></ENVELOPE>"
        mock_session.post.return_value = mock_resp
        client = TallyClient(cfg, session=mock_session)

        self.assertTrue(client.check_exists("unit", "Nos"))
        self.assertEqual(mock_session.post.call_count, 1)

        # Same identifier, different case — must hit the cache, not Tally again.
        self.assertTrue(client.check_exists("unit", "nos"))
        self.assertEqual(mock_session.post.call_count, 1)

    def test_check_exists_fails_open_on_connectivity_error(self):
        """
        check_exists() drives Create-vs-Alter for real financial vouchers
        (sync_rental_order/sync_invoice/sync_payment). It used to fail CLOSED (return
        False = "doesn't exist") on any connection error, which would default a
        genuinely-existing voucher to ACTION="Create" on a transient Tally connectivity
        blip — a real duplicate-voucher risk. It must now fail OPEN (assume it exists,
        i.e. ACTION="Alter"): if the record truly doesn't exist yet, Tally rejects an
        Alter cleanly as a business error (dead-lettered, retried next cycle) instead of
        silently creating a duplicate.
        """
        cfg = AppConfig(external_url="http://localhost:9000", external_system_type="tally")
        mock_session = MagicMock()
        mock_session.post.side_effect = requests.exceptions.ConnectionError("refused")
        client = TallyClient(cfg, session=mock_session)

        self.assertTrue(client.check_exists("rental_orders", "RENTAL-ORD-5"))

    def test_stock_item_prerequisites_are_not_recreated_within_the_same_batch(self):
        """
        A fresh TallyClient is created for every equipment-sync cycle, but within that
        one cycle every asset sharing the same unit ("Nos") independently re-checked
        existence and re-sent a fresh <UNIT ACTION="Create"> on every single STOCKITEM
        import — confirmed live: Tally's XML server crashed with a native "Memory Access
        Violation" after exactly this kind of back-to-back burst of STOCKITEM imports
        repeatedly recreating the same prerequisite masters. Once the unit is created for
        the first asset in a batch, a second asset sharing it must skip both the
        existence re-check and the Create block entirely.
        """
        cfg = AppConfig(external_url="http://localhost:9000", external_system_type="tally")
        mock_session = MagicMock()

        check_exists_resp = MagicMock()
        check_exists_resp.status_code = 200
        check_exists_resp.content = b"<ENVELOPE><BODY><DATA><COLLECTION></COLLECTION></DATA></BODY></ENVELOPE>"

        import_resp = MagicMock()
        import_resp.status_code = 200
        import_resp.content = b"<ENVELOPE><HEADER><VERSION>1</VERSION></HEADER><BODY><IMPORTRESULT><CREATED>1</CREATED></IMPORTRESULT></BODY></ENVELOPE>"

        sent_bodies = []

        def fake_post(url, data=None, headers=None, timeout=None):
            body = data.decode("utf-8") if isinstance(data, bytes) else str(data)
            sent_bodies.append(body)
            return check_exists_resp if "<TALLYREQUEST>EXPORT</TALLYREQUEST>" in body else import_resp

        mock_session.post.side_effect = fake_post
        client = TallyClient(cfg, session=mock_session)

        client.sync_equipment({"id": 1, "name": "Dell Mouse", "asset_unit": {"name": "Nos"}})
        client.sync_equipment({"id": 2, "name": "Dell Keyboard", "asset_unit": {"name": "Nos"}})

        unit_check_calls = [b for b in sent_bodies if "<TALLYREQUEST>EXPORT</TALLYREQUEST>" in b and "<ID>CheckExistence</ID>" in b and "<TYPE>UNIT</TYPE>" in b]
        import_calls = [b for b in sent_bodies if "<TALLYREQUEST>Import Data</TALLYREQUEST>" in b]

        self.assertEqual(len(unit_check_calls), 1, "unit existence must only be checked once for the whole batch")
        self.assertIn('<UNIT NAME="Nos" ACTION="Create">', import_calls[0])
        self.assertNotIn('<UNIT NAME="Nos" ACTION="Create">', import_calls[1])

    def test_customer_ledger_xml_builder(self):
        cust_data = {
            "id": 10,
            "name": "Acme Rentals Pvt Ltd",
            "gst_number": "27AAACA1234A1Z5",
            "mobile": "9876543210",
            "email": "info@acme.com",
            "address": [{"address1": "Building 5", "city": "Mumbai", "state": "Maharashtra", "zipcode": "400001"}],
        }
        xml = build_customer_ledger_xml(cust_data, action="Create")
        self.assertIn("<LEDGER NAME=\"Acme Rentals Pvt Ltd\" ACTION=\"Create\">", xml)
        self.assertIn("<PARTYGSTIN>27AAACA1234A1Z5</PARTYGSTIN>", xml)
        self.assertIn("<STATENAME>Maharashtra</STATENAME>", xml)

    def test_sales_invoice_voucher_uses_rentasst_tax_breakdown_when_provided(self):
        """
        When RentAsst supplies real cgst_amount/sgst_amount (not just grand_total and
        subtotal), the voucher must use those authoritative figures directly instead of
        re-deriving a plain 50/50 split from grand_total - subtotal — an 18% GST rate
        applied unevenly (or a discount that changes the effective rate) would otherwise
        get silently flattened into an even split that doesn't match reality.
        """
        inv_data = {
            "id": 777,
            "number": "INV-2026-777",
            "customer_name": "Test Client",
            "subtotal": 1000,
            "grand_total": 1180,
            "cgst_amount": 100,
            "sgst_amount": 80,
            "items": [{"name": "Generator 500kVA", "quantity": 1, "price": 1000, "unit": "Nos"}],
        }
        xml = build_sales_invoice_voucher_xml(inv_data)
        self.assertIn("<LEDGERNAME>CGST</LEDGERNAME>", xml)
        self.assertIn("<AMOUNT>100.00</AMOUNT>", xml)
        self.assertIn("<LEDGERNAME>SGST</LEDGERNAME>", xml)
        self.assertIn("<AMOUNT>80.00</AMOUNT>", xml)

    def test_sales_invoice_voucher_xml_builder(self):
        inv_data = {
            "id": 501,
            "number": "INV-2026-001",
            "customer_name": "Test Client",
            "subtotal": 10000,
            "grand_total": 11800,
            "items": [{"name": "Generator 500kVA", "quantity": 1, "price": 10000, "unit": "Nos"}],
        }
        xml = build_sales_invoice_voucher_xml(inv_data, company_state="Maharashtra")
        self.assertIn("<VOUCHER VTYPE=\"Sales\" ACTION=\"Create\"", xml)
        self.assertIn("<VOUCHERNUMBER>INV-2026-001</VOUCHERNUMBER>", xml)
        self.assertIn("<AMOUNT>-11800.00</AMOUNT>", xml)
        self.assertIn("<LEDGERNAME>CGST</LEDGERNAME>", xml)

    def test_sales_invoice_voucher_escapes_voucher_number(self):
        """
        VOUCHERNUMBER must be escaped like every other interpolated field — otherwise a
        crafted invoice number containing '</VOUCHERNUMBER><VOUCHER ...>' could break out
        of the tag and inject arbitrary sibling XML into the Tally import message.
        """
        inv_data = {
            "id": 999,
            "number": 'INV-1</VOUCHERNUMBER><VOUCHER VTYPE="Injected" ACTION="Create">',
            "customer_name": "Test Client",
            "subtotal": 100,
            "grand_total": 118,
        }
        xml = build_sales_invoice_voucher_xml(inv_data)
        self.assertNotIn('<VOUCHER VTYPE="Injected"', xml)
        self.assertIn("&lt;/VOUCHERNUMBER&gt;", xml)

    def test_receipt_voucher_escapes_voucher_number(self):
        """Same injection risk as the invoice voucher, via reference_id/payment_number."""
        pay_data = {
            "id": 1,
            "reference_id": 'PAY-1</VOUCHERNUMBER><VOUCHER VTYPE="Injected" ACTION="Create">',
            "amount": 500,
        }
        xml = build_receipt_voucher_xml(pay_data)
        self.assertNotIn('<VOUCHER VTYPE="Injected"', xml)
        self.assertIn("&lt;/VOUCHERNUMBER&gt;", xml)

    def test_sales_invoice_voucher_files_a_new_ref_bill(self):
        """
        Without a "New Ref" bill allocation, Tally books the party entry "On Account" —
        confirmed live against a real synced invoice, whose exported XML showed
        <BILLTYPE>On Account</BILLTYPE> with an empty bill <NAME/>. That leaves Tally's
        own bill-wise outstanding/payment-summary report unable to show which invoices
        are paid vs outstanding, and gives build_receipt_voucher_xml's "Agst Ref" nothing
        to reference. The bill name must be the same stable RENTAL-INV-{id} marker used
        for REMOTEID/NARRATION.
        """
        inv_data = {
            "id": 34,
            "number": "31",
            "customer_name": "Felix",
            "subtotal": 2000,
            "grand_total": 2360,
        }
        xml = build_sales_invoice_voucher_xml(inv_data)
        self.assertIn("<NAME>RENTAL-INV-34</NAME>", xml)
        self.assertIn("<BILLTYPE>New Ref</BILLTYPE>", xml)
        self.assertNotIn("On Account", xml)

    def test_receipt_voucher_files_an_agst_ref_bill_when_invoice_linked(self):
        """The Agst Ref bill name must match the exact bill the invoice filed as New Ref."""
        pay_data = {"id": 37, "amount": 2360, "invoice_id": 34, "paid_by": "Felix"}
        xml = build_receipt_voucher_xml(pay_data)
        self.assertIn("<NAME>RENTAL-INV-34</NAME>", xml)
        self.assertIn("<BILLTYPE>Agst Ref</BILLTYPE>", xml)

    def test_receipt_voucher_has_no_bill_allocation_without_invoice_id(self):
        """An advance/on-account payment with no linked invoice keeps the old plain entry."""
        pay_data = {"id": 5, "amount": 500, "paid_by": "Felix"}
        xml = build_receipt_voucher_xml(pay_data)
        self.assertNotIn("BILLALLOCATIONS.LIST", xml)

    def test_send_xml_escapes_company_name_in_static_variables(self):
        """
        send_xml()'s fallback STATICVARIABLES injection must escape the company name like
        build_import_envelope/build_export_collection_envelope already do for the same
        field elsewhere, rather than interpolating it raw.
        """
        cfg = AppConfig(external_url="http://localhost:9000", external_system_type="tally")
        cfg.tally_company_name = 'Rental & Co</SVCURRENTCOMPANY></STATICVARIABLES><INJECTED/>'
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"<ENVELOPE><HEADER><VERSION>1</VERSION></HEADER><BODY><IMPORTRESULT><CREATED>1</CREATED></IMPORTRESULT></BODY></ENVELOPE>"
        mock_session.post.return_value = mock_resp

        client = TallyClient(cfg, session=mock_session)
        client.send_xml("<ENVELOPE><BODY><IMPORTDATA><REQUESTDESC><REPORTNAME>All Masters</REPORTNAME></REQUESTDESC></IMPORTDATA></BODY></ENVELOPE>")

        sent_xml = mock_session.post.call_args.kwargs["data"].decode("utf-8")
        self.assertNotIn("<INJECTED/>", sent_xml)
        self.assertIn("&amp;", sent_xml)

    def test_physical_stock_voucher_xml_builder(self):
        xml = build_physical_stock_voucher_xml(item_name="Dell Laptop 3440", quantity=11, unit="Piece")
        self.assertIn('VCHTYPE="Physical Stock"', xml)
        self.assertIn("<STOCKITEMNAME>Dell Laptop 3440</STOCKITEMNAME>", xml)
        self.assertIn("<ACTUALQTY>11 Piece</ACTUALQTY>", xml)
        self.assertIn("<BILLEDQTY>11 Piece</BILLEDQTY>", xml)

    def test_reconcile_stock_quantity_pushes_physical_stock_voucher(self):
        """
        Confirmed live: re-sending RentAsst's available_quantity as OPENINGBALANCE on the
        STOCKITEM master never corrects drift once Sales vouchers have consumed against
        it (OPENINGBALANCE is a fixed baseline, not a live quantity) — a Physical Stock
        voucher is Tally's actual mechanism for reconciling current stock.
        """
        cfg = AppConfig(external_url="http://localhost:9000", external_system_type="tally")
        client = TallyClient(cfg, session=MagicMock())
        client.send_xml = MagicMock(return_value="TALLY-ID-1")

        client.reconcile_stock_quantity("Standee Banner", 11, unit="Nos")

        client.send_xml.assert_called_once()
        physical_stock_xml = client.send_xml.call_args.args[0]
        self.assertIn('VCHTYPE="Physical Stock"', physical_stock_xml)
        self.assertIn("<STOCKITEMNAME>Standee Banner</STOCKITEMNAME>", physical_stock_xml)
        self.assertIn("<ACTUALQTY>11 Nos</ACTUALQTY>", physical_stock_xml)

    def test_reconcile_stock_quantity_skips_when_quantity_unknown(self):
        cfg = AppConfig(external_url="http://localhost:9000", external_system_type="tally")
        client = TallyClient(cfg, session=MagicMock())
        client.send_xml = MagicMock(return_value="TALLY-ID-1")

        client.reconcile_stock_quantity("Standee Banner", None, unit="Nos")

        client.send_xml.assert_not_called()

    def test_invoice_math_validation_allows_rupee_round_off(self):
        """
        RentAsst rounds grand_total to the nearest whole rupee while subtotal keeps paise
        precision (e.g. subtotal=409.46, grand_total=409) — a standard round-off convention,
        not a data error. The validator must accept this (previously rejected at >0.05).
        """
        from app.validation.validator import PayloadValidator
        inv_data = {
            "id": 28,
            "customer_id": 5,
            "subtotal": 409.46,
            "grand_total": 409,
        }
        is_valid, err = PayloadValidator.validate_invoice(inv_data)
        self.assertTrue(is_valid, err)

    def test_sales_invoice_voucher_balances_with_rupee_round_off(self):
        """
        When grand_total is rounded to the nearest rupee but subtotal/tax carry paise
        precision, the voucher's ledger entries must still sum to zero (Tally requires
        every voucher to balance) via an explicit Round Off ledger entry.
        """
        inv_data = {
            "id": 28,
            "number": "28",
            "customer_name": "Cash Customer",
            "subtotal": 409.46,
            "grand_total": 409,
            "items": [{"name": "Moto G45", "quantity": 1, "price": 409.46, "unit": "Nos", "total_price": 409.46}],
        }
        xml = build_sales_invoice_voucher_xml(inv_data)
        self.assertIn("<LEDGERNAME>Round Off</LEDGERNAME>", xml)
        self.assertIn("<AMOUNT>-0.46</AMOUNT>", xml)

        import re
        # STOCKITEMNAME inventory allocation AMOUNT is nested inside the income ledger entry
        # and must not be double-counted — only ALLLEDGERENTRIES.LIST-level amounts balance.
        ledger_amounts = [float(m) for m in re.findall(
            r"<ALLLEDGERENTRIES\.LIST>\s*<LEDGERNAME>[^<]*</LEDGERNAME>\s*(?:<ISPARTYLEDGER>[^<]*</ISPARTYLEDGER>\s*)?<ISDEEMEDPOSITIVE>[^<]*</ISDEEMEDPOSITIVE>\s*<AMOUNT>(-?\d+\.\d+)</AMOUNT>",
            xml,
        )]
        self.assertEqual(round(sum(ledger_amounts), 2), 0.0)


class TestUnitNameMatching(unittest.TestCase):
    """
    RentAsst and Tally were confirmed live to disagree on unit spelling both by name
    ("Meter" vs an existing Tally unit named "MTR") and by symbol/name mix
    ("Piece"/"pc" vs an existing Tally unit named "Pieces"). resolve_existing_unit_name
    must find the existing Tally unit either way and return ITS name, so callers reuse
    it instead of creating a second, duplicate UNIT master for the same real unit.
    """

    def test_matches_via_synonym_group_regardless_of_which_field_matched(self):
        existing = [
            {"name": "MTR", "symbol": "Mtr"},
            {"name": "Pieces", "symbol": "Pcs"},
            {"name": "Nos", "symbol": ""},
        ]
        self.assertEqual(resolve_existing_unit_name("Meter", "m", existing), "MTR")
        self.assertEqual(resolve_existing_unit_name("Piece", "pc", existing), "Pieces")
        self.assertIsNone(resolve_existing_unit_name("Kilogram", "kg", existing))

    def test_exact_normalized_match_without_a_synonym_entry(self):
        existing = [{"name": "Box", "symbol": "Bx"}]
        self.assertEqual(resolve_existing_unit_name("box", "", existing), "Box")

    def test_no_match_returns_none_for_an_empty_registry(self):
        self.assertIsNone(resolve_existing_unit_name("Meter", "m", []))


class TestTallyClientUnitResolution(unittest.TestCase):
    """
    End-to-end (against a mocked Tally session): a RentAsst unit that Tally already
    represents under a different spelling must resolve to Tally's existing unit at
    every point that names a unit — STOCKITEM's BASEUNITS, unit pre-creation, and a
    Physical Stock reconciliation voucher's ACTUALQTY/BILLEDQTY — not just one of them,
    or the STOCKITEM master and its own reconciliation voucher would end up naming two
    different Tally units for the same asset.
    """

    def test_sync_equipment_reuses_existing_tally_unit_with_different_spelling(self):
        cfg = AppConfig(external_url="http://localhost:9000", external_system_type="tally")
        mock_session = MagicMock()

        list_units_resp = MagicMock()
        list_units_resp.status_code = 200
        list_units_resp.content = b"""<ENVELOPE><BODY><DATA><COLLECTION>
            <UNIT><NAME>MTR</NAME><SYMBOL>Mtr</SYMBOL></UNIT>
        </COLLECTION></DATA></BODY></ENVELOPE>"""

        equip_check_resp = MagicMock()
        equip_check_resp.status_code = 200
        equip_check_resp.content = b"<ENVELOPE><BODY><DATA><COLLECTION></COLLECTION></DATA></BODY></ENVELOPE>"

        import_resp = MagicMock()
        import_resp.status_code = 200
        import_resp.content = b"<ENVELOPE><HEADER><VERSION>1</VERSION></HEADER><BODY><IMPORTRESULT><CREATED>1</CREATED></IMPORTRESULT></BODY></ENVELOPE>"

        sent_bodies = []

        def fake_post(url, data=None, headers=None, timeout=None):
            body = data.decode("utf-8") if isinstance(data, bytes) else str(data)
            sent_bodies.append(body)
            if "<ID>ListUnits</ID>" in body:
                return list_units_resp
            if "<TALLYREQUEST>EXPORT</TALLYREQUEST>" in body:
                return equip_check_resp
            return import_resp

        mock_session.post.side_effect = fake_post
        client = TallyClient(cfg, session=mock_session)

        client.sync_equipment({
            "id": 5, "name": "Measuring Tape",
            "asset_unit": {"name": "Meter", "symbol": "m"},
        })

        import_calls = [b for b in sent_bodies if "<TALLYREQUEST>Import Data</TALLYREQUEST>" in b]
        self.assertEqual(len(import_calls), 1)
        self.assertIn("<BASEUNITS>MTR</BASEUNITS>", import_calls[0])
        self.assertNotIn("ACTION=\"Create\">\n            <NAME>Meter</NAME>", import_calls[0])
        self.assertNotIn('<UNIT NAME="MTR" ACTION="Create">', import_calls[0])

        unit_check_calls = [b for b in sent_bodies if "<ID>CheckExistence</ID>" in b and "<TYPE>UNIT</TYPE>" in b]
        self.assertEqual(len(unit_check_calls), 0, "a matched existing unit must skip the CheckExistence round-trip entirely")

    def test_sync_unit_skips_creating_a_duplicate_when_an_equivalent_unit_exists(self):
        cfg = AppConfig(external_url="http://localhost:9000", external_system_type="tally")
        mock_session = MagicMock()

        list_units_resp = MagicMock()
        list_units_resp.status_code = 200
        list_units_resp.content = b"""<ENVELOPE><BODY><DATA><COLLECTION>
            <UNIT><NAME>Pieces</NAME><SYMBOL>Pcs</SYMBOL></UNIT>
        </COLLECTION></DATA></BODY></ENVELOPE>"""
        mock_session.post.return_value = list_units_resp
        client = TallyClient(cfg, session=mock_session)

        created = client.sync_unit("Piece", symbol="pc")

        self.assertFalse(created)
        import_calls = [
            c for c in mock_session.post.call_args_list
            if "<TALLYREQUEST>Import Data</TALLYREQUEST>" in (c.kwargs.get("data") or b"").decode("utf-8", "ignore")
        ]
        self.assertEqual(len(import_calls), 0)

    def test_reconcile_stock_quantity_uses_the_resolved_tally_unit_name(self):
        cfg = AppConfig(external_url="http://localhost:9000", external_system_type="tally")
        mock_session = MagicMock()

        list_units_resp = MagicMock()
        list_units_resp.status_code = 200
        list_units_resp.content = b"""<ENVELOPE><BODY><DATA><COLLECTION>
            <UNIT><NAME>MTR</NAME><SYMBOL>Mtr</SYMBOL></UNIT>
        </COLLECTION></DATA></BODY></ENVELOPE>"""

        import_resp = MagicMock()
        import_resp.status_code = 200
        import_resp.content = b"<ENVELOPE><HEADER><VERSION>1</VERSION></HEADER><BODY><IMPORTRESULT><CREATED>1</CREATED><LASTVCHID>9</LASTVCHID></IMPORTRESULT></BODY></ENVELOPE>"

        sent_bodies = []

        def fake_post(url, data=None, headers=None, timeout=None):
            body = data.decode("utf-8") if isinstance(data, bytes) else str(data)
            sent_bodies.append(body)
            if "<ID>ListUnits</ID>" in body:
                return list_units_resp
            return import_resp

        mock_session.post.side_effect = fake_post
        client = TallyClient(cfg, session=mock_session)

        client.reconcile_stock_quantity("Measuring Tape", 25, unit="Meter")

        voucher_calls = [b for b in sent_bodies if "Physical Stock" in b]
        self.assertEqual(len(voucher_calls), 1)
        self.assertIn("25 MTR", voucher_calls[0])


class TestSalesOrderVoucherInventoryShape(unittest.TestCase):
    """
    build_sales_order_voucher_xml books Rent Outs as a real "Sales" voucher, not
    "Sales Order" — confirmed live against this company's real Tally server that
    "Sales Order" (and "Delivery Note") reject EVERY import attempt with EXCEPTIONS>0
    regardless of XML shape (top-level ALLINVENTORYENTRIES.LIST, nested
    INVENTORYALLOCATIONS.LIST, with/without ORDERALLOCATIONS.LIST, with/without
    BILLALLOCATIONS.LIST, with/without an explicit VOUCHERNUMBER), most surfacing "Bad
    Order Number in Voucher!" — while a plain "Sales" voucher with the identical
    party/items succeeds immediately. See the function's own docstring for the full
    live-verification history.

    Items go in a nested INVENTORYALLOCATIONS.LIST inside the "Sales Account"
    ALLLEDGERENTRIES.LIST entry — the same accounting-invoice shape
    build_sales_invoice_voucher_xml uses. That ledger entry's own AMOUNT must equal the
    sum of its nested item lines (the item subtotal, not the GST-inclusive total) or
    Tally rejects the whole voucher — confirmed live, this is what broke the first
    version of this fix.
    """

    def test_item_lines_use_nested_inventoryallocations_inside_sales_ledger(self):
        xml = build_sales_order_voucher_xml({
            "id": 2, "number": "R100001", "customer_name": "Test", "grand_total": 114,
            "items": [{"name": "Moto G45", "quantity": 1, "price": 97, "total_price": 97, "unit": "Piece"}],
        })
        self.assertIn("<VOUCHER VTYPE=\"RentAsst Sales\" ACTION=\"Create\"", xml)
        self.assertIn("<INVENTORYALLOCATIONS.LIST>", xml)
        self.assertIn("<STOCKITEMNAME>Moto G45</STOCKITEMNAME>", xml)
        self.assertNotIn("ALLINVENTORYENTRIES.LIST>", xml)
        self.assertNotIn("ORDERALLOCATIONS.LIST", xml)
        self.assertNotIn("BATCHALLOCATIONS.LIST", xml)

        # INVENTORYALLOCATIONS.LIST must be nested inside the Sales Account
        # ALLLEDGERENTRIES.LIST entry, not a sibling of it.
        ledger_idx = xml.index("<LEDGERNAME>Sales Account</LEDGERNAME>")
        inv_idx = xml.index("<INVENTORYALLOCATIONS.LIST>")
        close_idx = xml.index("</ALLLEDGERENTRIES.LIST>", ledger_idx)
        self.assertLess(ledger_idx, inv_idx)
        self.assertLess(inv_idx, close_idx)

        # The Sales Account ledger's own AMOUNT is the item subtotal (97), not the
        # GST-inclusive grand_total (114) — the mismatch is what Tally rejected live.
        self.assertIn("<AMOUNT>97.00</AMOUNT>", xml)

    def test_multiple_items_produce_multiple_nested_inventory_entries(self):
        xml = build_sales_order_voucher_xml({
            "id": 7, "number": "R100004", "customer_name": "Test", "grand_total": 560,
            "items": [
                {"name": "Dell Laptop Bag", "quantity": 1, "price": 10, "total_price": 20, "unit": "Piece"},
                {"name": "Dell Keyboard", "quantity": 1, "price": 20, "total_price": 40, "unit": "Piece"},
                {"name": "Dell Laptop 3440", "quantity": 1, "price": 250, "total_price": 500, "unit": "Piece"},
            ],
        })
        self.assertEqual(xml.count("<INVENTORYALLOCATIONS.LIST>"), 3)
        self.assertIn("Dell Laptop Bag", xml)
        self.assertIn("Dell Keyboard", xml)
        self.assertIn("Dell Laptop 3440", xml)

    def test_order_with_no_items_has_no_inventory_block_at_all(self):
        xml = build_sales_order_voucher_xml({
            "id": 13, "number": "R100009", "customer_name": "Test", "grand_total": 270,
        })
        self.assertNotIn("INVENTORYALLOCATIONS.LIST", xml)
        self.assertIn("<ALLLEDGERENTRIES.LIST>", xml)

    def test_leftover_amount_over_item_subtotal_is_booked_as_gst_when_no_gst_percent_given(self):
        # No `gst` field on this order -> falls back to deriving tax from the gap
        # between grand_total (236) and the item subtotal (200): the 36 leftover must
        # appear as CGST/SGST, and the Sales Account ledger's own amount must stay at
        # 200 (not the mismatched 236) to keep the voucher's ledger entries balanced.
        xml = build_sales_order_voucher_xml({
            "id": 2, "number": "R100001", "customer_name": "Test", "grand_total": 236,
            "items": [
                {"name": "Dell Laptop", "quantity": 1, "price": 150, "total_price": 150, "unit": "Piece"},
                {"name": "Dell Mouse", "quantity": 1, "price": 50, "total_price": 50, "unit": "Piece"},
            ],
        })
        self.assertIn("<AMOUNT>200.00</AMOUNT>", xml)
        self.assertIn("<LEDGERNAME>CGST</LEDGERNAME>", xml)
        self.assertIn("<LEDGERNAME>SGST</LEDGERNAME>", xml)
        self.assertIn("<AMOUNT>18.00</AMOUNT>", xml)

    def test_amount_understated_vs_items_keeps_ledger_balanced_with_nested_sum(self):
        """
        When the order's own amount/grand_total field is SMALLER than the real sum of
        its item lines (a data-entry inconsistency: no gst% given, and grand_total is
        missing/understated relative to items), the code used to reassign
        item_subtotal = amount without rebuilding the already-rendered
        INVENTORYALLOCATIONS.LIST lines (which still summed to the original, larger
        item total) — desyncing the Sales Account ledger's own AMOUNT from its nested
        inventory sum, which Tally rejects the whole voucher for. The item lines are
        the source of truth (they're what INVENTORYALLOCATIONS.LIST actually sums to);
        the ledger AMOUNT and party amount must be corrected to match them instead.
        """
        xml = build_sales_order_voucher_xml({
            "id": 9, "number": "R100005", "customer_name": "Test", "grand_total": 50,
            "items": [
                {"name": "Dell Laptop", "quantity": 1, "price": 150, "total_price": 150, "unit": "Piece"},
                {"name": "Dell Mouse", "quantity": 1, "price": 50, "total_price": 50, "unit": "Piece"},
            ],
        })
        # Sales Account ledger AMOUNT must equal the real item subtotal (200), matching
        # what the nested INVENTORYALLOCATIONS.LIST lines actually sum to (150 + 50).
        ledger_idx = xml.index("<LEDGERNAME>Sales Account</LEDGERNAME>")
        close_idx = xml.index("</ALLLEDGERENTRIES.LIST>", ledger_idx)
        sales_block = xml[ledger_idx:close_idx]
        # The ledger's own AMOUNT (immediately after ISDEEMEDPOSITIVE, before the
        # nested per-item INVENTORYALLOCATIONS.LIST lines) must be 200.00 — the real
        # item subtotal — not the understated grand_total of 50.
        ledger_amount_line = sales_block.split("<INVENTORYALLOCATIONS.LIST>")[0]
        self.assertIn("<AMOUNT>200.00</AMOUNT>", ledger_amount_line)
        # No tax booked (no gst% given, and there's no positive leftover to book).
        self.assertNotIn("<LEDGERNAME>CGST</LEDGERNAME>", xml)
        # Party ledger entry must balance against the corrected total (200), not the
        # original understated grand_total (50).
        self.assertIn("<AMOUNT>-200.00</AMOUNT>", xml)

    def test_gst_computed_from_real_percentage_field_not_derived_from_total(self):
        """
        Confirmed live: rental_order payloads carry a real `gst` percentage field
        (e.g. "gst": 18) but grand_total can include non-taxable extras (shipping,
        labour, deposit) the item lines don't cover — deriving tax as
        (amount - item_subtotal) silently misbooked those extras as GST. When `gst`
        is present, tax must instead be item_subtotal * gst% exactly, and the party/
        bill amount recomputed from that (item_subtotal + tax), not trusted from
        grand_total.
        """
        xml = build_sales_order_voucher_xml({
            "id": 2, "number": "R100001", "customer_name": "Test", "grand_total": 300,
            "gst": 18, "cgst": 18, "sgst": 18,  # RentAsst mirrors the same 18% into all three
            "items": [
                {"name": "Dell Laptop", "quantity": 1, "price": 150, "total_price": 150, "unit": "Piece"},
                {"name": "Dell Mouse", "quantity": 1, "price": 50, "total_price": 50, "unit": "Piece"},
            ],
        })
        # item_subtotal=200, 18% of 200 = 36 (NOT 300-200=100, which would double-count
        # grand_total's non-taxable extras as GST).
        self.assertIn("<AMOUNT>200.00</AMOUNT>", xml)
        self.assertIn("<AMOUNT>18.00</AMOUNT>", xml)
        self.assertIn("<AMOUNT>-236.00</AMOUNT>", xml)
        self.assertNotIn("<AMOUNT>-300.00</AMOUNT>", xml)


class TestSalesOrderVoucherTypeAndNumbering(unittest.TestCase):
    """
    Confirmed live: the reserved "Sales" voucher type's NUMBERINGMETHOD is "Default"
    (Tally's built-in automatic numbering) — every custom VOUCHERNUMBER sent to it was
    silently discarded and replaced with Tally's own sequential number (sent
    "R1-CUSTOM-99", Tally stored "5"). build_sales_order_voucher_xml self-heals a
    dedicated "RentAsst Sales" voucher type (NUMBERINGMETHOD="Manual") the same way it
    self-heals prereq ledgers, so RentAsst's real order number is actually preserved
    (confirmed live: the same custom number came back unchanged under this type).
    """

    def test_creates_dedicated_manual_numbering_voucher_type(self):
        xml = build_sales_order_voucher_xml({
            "id": 2, "number": "R100001", "customer_name": "Test", "grand_total": 236,
        })
        self.assertIn('<VOUCHERTYPE NAME="RentAsst Sales" ACTION="Create">', xml)
        self.assertIn("<PARENT>Sales</PARENT>", xml)
        self.assertIn("<NUMBERINGMETHOD>Manual</NUMBERINGMETHOD>", xml)

    def test_voucher_uses_the_dedicated_type_not_the_shared_reserved_sales_type(self):
        xml = build_sales_order_voucher_xml({
            "id": 2, "number": "R100001", "customer_name": "Test", "grand_total": 236,
        })
        self.assertIn('<VOUCHER VTYPE="RentAsst Sales" ACTION="Create"', xml)
        self.assertIn("<VOUCHERTYPENAME>RentAsst Sales</VOUCHERTYPENAME>", xml)
        self.assertNotIn('VTYPE="Sales"', xml)

    def test_rentasst_order_number_is_sent_as_the_voucher_number(self):
        xml = build_sales_order_voucher_xml({
            "id": 2, "number": "R100001-CUSTOM", "customer_name": "Test", "grand_total": 236,
        })
        self.assertIn("<VOUCHERNUMBER>R100001-CUSTOM</VOUCHERNUMBER>", xml)


class TestRentalOrderNativeVsFallbackVoucherType(unittest.TestCase):
    """
    TallyClient.sync_rental_order tries a real "Sales Order" voucher first (correct
    when Order Processing works) and falls back to the "Sales" shape
    (build_sales_order_voucher_xml) only when that's rejected — confirmed live that
    Order Processing is unavailable on some Tally installations (unlicensed/
    Educational mode, or the feature just disabled) while a plain Sales voucher works
    fine. cfg.tally_order_processing_available remembers the outcome so it isn't
    re-discovered (and re-failed) on every single rental_order sync.
    """

    def _check_exists_resp(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b"<ENVELOPE><BODY><DATA><COLLECTION></COLLECTION></DATA></BODY></ENVELOPE>"
        return resp

    def _rejected_resp(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b"""<ENVELOPE><BODY><IMPORTRESULT>
            <CREATED>0</CREATED>
            <LINEERROR>Bad Order Number in Voucher!</LINEERROR>
        </IMPORTRESULT></BODY></ENVELOPE>"""
        return resp

    def _success_resp(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b"<ENVELOPE><BODY><IMPORTRESULT><CREATED>1</CREATED><LASTVCHID>9</LASTVCHID></IMPORTRESULT></BODY></ENVELOPE>"
        return resp

    def test_unknown_mode_tries_native_first_and_never_double_attempts_the_same_order(self):
        """
        Confirmed live: a failed import under a given REMOTEID/bill-name can leave
        Tally rejecting a SECOND attempt under that identical identifier too, even
        with a totally different voucher type. So when Order Processing is unknown, a
        rejected native attempt must raise (dead-lettering just this one order) rather
        than immediately retrying the SAME order with the fallback shape.
        """
        cfg = AppConfig(external_url="http://localhost:9000", external_system_type="tally")
        self.assertIsNone(cfg.tally_order_processing_available)
        mock_session = MagicMock()
        mock_session.post.side_effect = [self._check_exists_resp(), self._rejected_resp()]
        client = TallyClient(cfg, session=mock_session)

        with self.assertRaises(ValueError):
            client.sync_rental_order({"id": 2, "number": "R100001", "customer_name": "Test", "grand_total": 118})

        self.assertEqual(mock_session.post.call_count, 2)  # check_exists + one rejected attempt, no retry
        self.assertFalse(cfg.tally_order_processing_available)
        self.assertTrue(client.order_processing_auto_detected)

        native_call_body = mock_session.post.call_args_list[1].kwargs.get("data")
        native_call_body = native_call_body.decode("utf-8") if isinstance(native_call_body, bytes) else native_call_body
        self.assertIn('VTYPE="Sales Order"', native_call_body)

    def test_known_unavailable_goes_straight_to_fallback(self):
        cfg = AppConfig(external_url="http://localhost:9000", external_system_type="tally", tally_order_processing_available=False)
        mock_session = MagicMock()
        mock_session.post.side_effect = [self._check_exists_resp(), self._success_resp()]
        client = TallyClient(cfg, session=mock_session)

        result = client.sync_rental_order({"id": 3, "number": "R100002", "customer_name": "Test", "grand_total": 118})

        self.assertEqual(result, "RENTAL-ORD-3")
        self.assertEqual(mock_session.post.call_count, 2)  # check_exists + one successful fallback attempt
        fallback_body = mock_session.post.call_args_list[1].kwargs.get("data")
        fallback_body = fallback_body.decode("utf-8") if isinstance(fallback_body, bytes) else fallback_body
        self.assertIn('VTYPE="RentAsst Sales"', fallback_body)

    def test_known_available_uses_native_and_does_not_fall_back_on_a_real_error(self):
        """Once confirmed working, a fresh rejection is a real data problem with THIS
        order, not evidence Order Processing vanished — it must surface, not be masked."""
        cfg = AppConfig(external_url="http://localhost:9000", external_system_type="tally", tally_order_processing_available=True)
        mock_session = MagicMock()
        mock_session.post.side_effect = [self._check_exists_resp(), self._rejected_resp()]
        client = TallyClient(cfg, session=mock_session)

        with self.assertRaises(ValueError):
            client.sync_rental_order({"id": 4, "number": "R100003", "customer_name": "Test", "grand_total": 118})

        self.assertTrue(cfg.tally_order_processing_available)  # unchanged
        self.assertFalse(client.order_processing_auto_detected)


if __name__ == "__main__":
    unittest.main()
