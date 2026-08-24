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
