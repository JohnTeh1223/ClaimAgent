from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "code"))

from invoice_parser import InvoiceStatus, parse_invoice


class RealInvoiceRegressionTests(unittest.TestCase):
    @staticmethod
    def find_invoice(filename: str) -> Path | None:
        for folder in ("01-待处理", "02-待复核", "03-已完成"):
            candidate = PROJECT_ROOT / folder / filename
            if candidate.is_file():
                return candidate
        return None

    def test_supplied_discount_invoice_preserves_header_totals_and_rows(self):
        path = self.find_invoice("OSTB_52344122717Z53Eq8Y5m3538m.pdf")
        if path is None:
            self.skipTest("用户提供的双明细发票当前不在工作目录")
        invoice = parse_invoice(path)
        self.assertEqual(invoice.invoice_number, "26952000003499796116")
        self.assertEqual(invoice.invoice_date, "2026-08-18")
        self.assertEqual(invoice.buyer_name, "浙江大学")
        self.assertEqual(invoice.buyer_tax_id, "12100000470095016Q")
        self.assertEqual(invoice.seller_name, "广东品胜电子股份有限公司")
        self.assertEqual(invoice.seller_tax_id, "91440300757617352R")
        self.assertEqual(invoice.total_without_tax, Decimal("95.68"))
        self.assertEqual(invoice.total_tax, Decimal("12.44"))
        self.assertEqual(invoice.grand_total, Decimal("108.12"))
        self.assertEqual(len(invoice.items), 2)
        self.assertEqual(invoice.items[0].amount, Decimal("100.78"))
        self.assertEqual(invoice.items[1].amount, Decimal("-5.10"))
        self.assertTrue(invoice.items[1].is_negative)
        self.assertEqual(invoice.status, InvoiceStatus.REVIEW_REQUIRED)

    def test_existing_normal_invoice_remains_parsed_ok(self):
        path = self.find_invoice("dzfp_26444000000227502481_浙江大学_20260805163803 (1).pdf")
        if path is None:
            self.skipTest("正常发票回归样本当前不在工作目录")
        invoice = parse_invoice(path)
        self.assertEqual(invoice.status, InvoiceStatus.PARSED_OK)
        self.assertEqual(len(invoice.items), 1)
        self.assertEqual(invoice.grand_total, Decimal("16.49"))
        self.assertEqual(invoice.warnings, [])


if __name__ == "__main__":
    unittest.main()
