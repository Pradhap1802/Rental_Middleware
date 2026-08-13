import unittest
from unittest.mock import MagicMock
import requests

from app.models.domain import AppConfig
from app.connectors.tally.xml_builder import sanitize_tally_xml, escape_xml, format_tally_date
from app.connectors.tally.parser import validate_tally_accounting_success, extract_tally_errors
from app.connectors.tally.client import TallyClient
from app.connectors.tally.ledger import build_customer_ledger_xml
from app.connectors.tally.sales_voucher import build_sales_invoice_voucher_xml


class TestTallyConnectorAndValidation(unittest.TestCase):

    def test_xml_sanitization_and_escaping(self):
        dirty_xml = "\ufeff<ENVELOPE xmlns:UDF='TallyUDF'><HEADER><VERSION>1</VERSION></HEADER><BODY><DATA>Sub &amp; Co</DATA></BODY></ENVELOPE>"
        clean = sanitize_tally_xml(dirty_xml)
        self.assertNotIn("\ufeff", clean)
        self.assertNotIn("xmlns:UDF", clean)

        escaped = escape_xml("Rent & Sales Co <Pvt Ltd>")
        self.assertEqual(escaped, "Rent &amp; Sales Co &lt;Pvt Ltd&gt;")

        dt = format_tally_date("2026-08-15")
        self.assertEqual(dt, "20260801")  # EDU mode forces 01st

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


if __name__ == "__main__":
    unittest.main()
