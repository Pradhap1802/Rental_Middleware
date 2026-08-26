import unittest
from unittest.mock import MagicMock

from app.models.domain import AppConfig
from app.connectors.tally.xml_builder import sanitize_tally_xml, escape_xml, format_tally_date
from app.connectors.tally.parser import validate_tally_accounting_success
from app.connectors.tally.client import TallyClient
from app.connectors.tally.ledger import build_customer_ledger_xml
from app.connectors.tally.sales_voucher import build_sales_invoice_voucher_xml
from app.connectors.tally.receipt_voucher import build_receipt_voucher_xml
from app.connectors.tally.stock_item import build_physical_stock_voucher_xml


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


if __name__ == "__main__":
    unittest.main()
