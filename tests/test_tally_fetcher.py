import unittest
from unittest.mock import MagicMock, patch

from app.connectors.tally_fetcher import TallyFetcher


# Trimmed but structurally real fixture, captured live from Tally Prime's actual XML
# collection export for a "Sales Order" voucher. The item line lives under
# ALLINVENTORYENTRIES.LIST — there is no bare INVENTORYENTRIES.LIST tag anywhere in a
# real Tally response, for any voucher type.
REAL_SALES_ORDER_XML = """<ENVELOPE>
  <VOUCHER REMOTEID="" VCHTYPE="Sales Order" OBJVIEW="Invoice Voucher View">
    <DATE TYPE="Date">20260801</DATE>
    <GUID>43cd646b-f7a5-4e9b-87dc-ae276a3ef875-0000002e</GUID>
    <NARRATION TYPE="String"></NARRATION>
    <VOUCHERTYPENAME>Sales Order</VOUCHERTYPENAME>
    <PARTYNAME TYPE="String">Vishal Krishnan</PARTYNAME>
    <PARTYLEDGERNAME TYPE="String">Vishal Krishnan</PARTYLEDGERNAME>
    <VOUCHERNUMBER>9</VOUCHERNUMBER>
    <REFERENCE TYPE="String">10</REFERENCE>
    <ALTERID TYPE="Number"> 441</ALTERID>
    <AMOUNT TYPE="Amount">-50.00</AMOUNT>
    <ALLINVENTORYENTRIES.LIST>
      <STOCKITEMNAME TYPE="String">Earphone</STOCKITEMNAME>
      <ISDEEMEDPOSITIVE TYPE="Logical">No</ISDEEMEDPOSITIVE>
      <RATE TYPE="Rate">50.00/Piece</RATE>
      <AMOUNT TYPE="Amount">50.00</AMOUNT>
      <ACTUALQTY TYPE="Quantity"> 1 Piece</ACTUALQTY>
      <BILLEDQTY TYPE="Quantity"> 1 Piece</BILLEDQTY>
    </ALLINVENTORYENTRIES.LIST>
    <ALLLEDGERENTRIES.LIST>
      <LEDGERNAME TYPE="String">Vishal Krishnan</LEDGERNAME>
      <ISDEEMEDPOSITIVE TYPE="Logical">Yes</ISDEEMEDPOSITIVE>
      <AMOUNT TYPE="Amount">-50.00</AMOUNT>
    </ALLLEDGERENTRIES.LIST>
    <ALLLEDGERENTRIES.LIST>
      <LEDGERNAME TYPE="String">Rental Income</LEDGERNAME>
      <ISDEEMEDPOSITIVE TYPE="Logical">No</ISDEEMEDPOSITIVE>
      <AMOUNT TYPE="Amount">50.00</AMOUNT>
    </ALLLEDGERENTRIES.LIST>
  </VOUCHER>
</ENVELOPE>"""


class TestTallyFetcherSharedHttpLock(unittest.TestCase):
    """
    _post_xml() used to call bare requests.post() directly, completely bypassing
    TallyClient's _tally_post()/_TALLY_HTTP_LOCK — QueueWorker runs up to 4 jobs
    concurrently, and tally_to_rentasst (which uses this fetcher) is enqueued every
    cycle alongside equipment/invoices/payments/rental_orders (which use TallyClient),
    so this fetcher's requests could race against TallyClient's, unserialized, from
    within a single process. Confirmed live: a burst of "Could not set
    'SVCurrentCompany'" errors on equipment sync while reverse sync was also running —
    the exact concurrency signature this codebase's own comments document as capable of
    corrupting or crashing Tally. _post_xml must route through the same shared lock.
    """

    def test_post_xml_routes_through_the_shared_tally_post_lock(self):
        from app.models.domain import AppConfig

        cfg = AppConfig(external_url="http://localhost:9000", external_system_type="tally")
        fetcher = TallyFetcher(cfg)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"<ENVELOPE>OK</ENVELOPE>"
        mock_resp.raise_for_status.return_value = None

        with patch("app.connectors.tally_fetcher._tally_post", return_value=mock_resp) as mock_tally_post:
            result = fetcher._post_xml("<ENVELOPE>test</ENVELOPE>")

        mock_tally_post.assert_called_once()
        called_session = mock_tally_post.call_args[0][0]
        self.assertIs(called_session, fetcher.session)
        self.assertIn("OK", result)


class TestTallyFetcherInventoryParsing(unittest.TestCase):
    """
    fetch_vouchers()/​_parse_vouchers_xml() previously searched for a bare
    "INVENTORYENTRIES.LIST" tag that does not exist in Tally's real XML — every voucher's
    line items came back as an empty list regardless of voucher type, which silently
    defeated push_invoice_items()/push_rentout_items() (they always received nothing to
    push). Confirmed live against a real "Sales Order" voucher export.
    """

    def test_fetch_vouchers_extracts_items_from_real_tally_xml_shape(self):
        cfg = MagicMock()
        cfg.external_url = "http://localhost:9000"
        fetcher = TallyFetcher(cfg)

        with patch.object(fetcher, "_post_xml", return_value=REAL_SALES_ORDER_XML):
            vouchers = fetcher.fetch_vouchers(last_alter_id=0)

        self.assertEqual(len(vouchers), 1)
        items = vouchers[0]["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "Earphone")
        self.assertEqual(items[0]["rate"], "50.00/Piece")
        self.assertEqual(items[0]["amount"], "50.00")


if __name__ == "__main__":
    unittest.main()
