from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "code"))

from batch_workflow import (
    ReviewItem,
    execute_batch,
    preflight_batch,
    scan_input_directory,
    summarize,
    validate_invoice,
    validate_review_item,
    write_review_metadata,
)
from invoice_parser import InvoiceData, InvoiceParserUnavailable, InvoiceStatus, InvoiceWarning
from wps_backend import BatchWriteResult, WriteResult


def make_invoice(source: Path, **overrides) -> InvoiceData:
    values = {
        "source_pdf": str(source),
        "invoice_no": "26444000000227502481",
        "invoice_date": "2026-08-05",
        "buyer_name": "浙江大学",
        "buyer_tax_id": "12100000470095016Q",
        "seller_name": "中山宏蓝电子科技有限公司",
        "seller_tax_id": "91442000TEST000001",
        "item_name": "*电子元件*无线发射模块",
        "short_name": "无线发射模块",
        "specification": "-",
        "unit_raw": "pcs",
        "unit_for_inventory": "个",
        "quantity": 1.0,
        "unit_price": 16.33,
        "amount_without_tax": 16.33,
        "tax_amount": 0.16,
        "total_amount": 16.49,
        "tax_rate": "1%",
        "category_guess": "材料费",
    }
    values.update(overrides)
    return InvoiceData(**values)


class BatchWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.input_dir = self.root / "待处理"
        self.wps_root = self.root / "wps_test_copy"
        self.input_dir.mkdir()
        self.wps_root.mkdir()
        (self.wps_root / "stats.xlsx").write_bytes(b"TEST STATS COPY")
        (self.wps_root / "inventory.xlsx").write_bytes(b"TEST INVENTORY COPY")
        self.config = {
            "wps_root": str(self.wps_root),
            "stats_file": "stats.xlsx",
            "inventory_file": "inventory.xlsx",
            "input_dir": str(self.input_dir),
            "completed_dir": str(self.root / "已完成"),
            "review_dir": str(self.root / "待复核"),
            "backup_dir": str(self.root / "backup"),
            "log_file": str(self.root / "logs" / "processing.jsonl"),
            "expected_buyer_name": "浙江大学",
            "expected_buyer_tax_id": "12100000470095016Q",
        }

    def tearDown(self):
        self.temp.cleanup()

    def make_item(self, filename="invoice.pdf", **overrides):
        source = self.input_dir / filename
        source.write_bytes(b"PDF TEST COPY")
        invoice = make_invoice(source, **overrides)
        return ReviewItem.from_invoice(invoice)

    def test_valid_invoice_and_arithmetic(self):
        item = self.make_item()
        self.assertEqual(validate_invoice(item.invoice, self.config), [])

    def test_invalid_buyer_name_and_tax_id(self):
        item = self.make_item(buyer_name="个人", buyer_tax_id="BAD")
        errors = validate_invoice(item.invoice, self.config)
        self.assertTrue(any("购买方应为" in error for error in errors))
        self.assertTrue(any("购买方税号应为" in error for error in errors))

    def test_total_arithmetic_mismatch(self):
        item = self.make_item(total_amount=99.0)
        self.assertTrue(any("金额校验失败" in error for error in validate_invoice(item.invoice, self.config)))

    def test_duplicate_target_pdf_is_blocked(self):
        item = self.make_item()
        month = self.wps_root / item.reimbursement_month
        month.mkdir()
        (month / item.target_filename).write_bytes(b"EXISTING")
        errors = preflight_batch([item], self.config)
        self.assertTrue(any("目标 PDF 已存在" in error for values in errors.values() for error in values))

    def test_duplicate_invoice_inside_batch_is_blocked(self):
        first = self.make_item("first.pdf")
        second = self.make_item("second.pdf")
        errors = preflight_batch([first, second], self.config)
        self.assertTrue(any("本批次发票号码重复" in error for values in errors.values() for error in values))

    def test_duplicate_invoice_in_log_is_blocked(self):
        item = self.make_item()
        log = Path(self.config["log_file"])
        log.parent.mkdir()
        log.write_text(json.dumps({
            "event": "batch_write", "result": "success",
            "items": [{"invoice_no": item.invoice.invoice_no}],
        }) + "\n", encoding="utf-8")
        errors = preflight_batch([item], self.config)
        self.assertTrue(any("处理日志中已有发票号码" in error for values in errors.values() for error in values))

    def test_corrupt_log_fails_closed(self):
        item = self.make_item()
        log = Path(self.config["log_file"])
        log.parent.mkdir()
        log.write_text("{not valid json}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "处理日志第 1 行损坏"):
            preflight_batch([item], self.config)

    def test_summary_reports_category_and_inventory_effect(self):
        material = self.make_item("material.pdf")
        other = self.make_item(
            "other.pdf", invoice_no="26444000000227502482", total_amount=20.0,
            amount_without_tax=19.0, tax_amount=1.0, category_guess="其他",
        )
        summary = summarize([material, other])
        self.assertEqual(summary.invoice_count, 2)
        self.assertEqual(summary.total_amount, 36.49)
        self.assertEqual(summary.material_count, 1)

    def test_summary_targets_follow_invoice_date_order(self):
        later = self.make_item(
            "later.pdf", invoice_no="26444000000227502482", invoice_date="2026-08-20"
        )
        earlier = self.make_item(
            "earlier.pdf", invoice_no="26444000000227502483", invoice_date="2026-07-01"
        )
        later.short_name = "较晚发票"
        earlier.short_name = "较早发票"
        summary = summarize([later, earlier])
        self.assertIn(earlier.target_filename, summary.targets[0])
        self.assertIn(later.target_filename, summary.targets[1])

    @patch("batch_workflow.parse_invoice", side_effect=ValueError("未识别到发票号码"))
    def test_parse_failure_is_moved_to_review_with_reason_and_log(self, _parse):
        source = self.input_dir / "bad.pdf"
        source.write_bytes(b"BAD PDF")
        scan = scan_input_directory(self.config)
        self.assertEqual(len(scan.failures), 1)
        self.assertFalse(source.exists())
        review = Path(scan.failures[0].review_path)
        self.assertTrue(review.exists())
        self.assertTrue(review.with_suffix(".pdf.error.json").exists())
        record = json.loads(Path(self.config["log_file"]).read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(record["result"], "review_needed")

    @patch("batch_workflow.parse_invoice", side_effect=InvoiceParserUnavailable("缺少 PyMuPDF"))
    def test_missing_parser_dependency_keeps_source_in_pending(self, _parse):
        source = self.input_dir / "invoice.pdf"
        source.write_bytes(b"PDF")
        with self.assertRaises(InvoiceParserUnavailable):
            scan_input_directory(self.config)
        self.assertTrue(source.exists())
        self.assertEqual(list(Path(self.config["review_dir"]).glob("*.pdf")), [])

    def test_review_warning_requires_manual_acknowledgement(self):
        item = self.make_item()
        item.invoice.status = InvoiceStatus.REVIEW_REQUIRED
        item.invoice.warnings = [InvoiceWarning("MULTIPLE_ITEMS", "检测到2条明细", "items")]
        review = ReviewItem.from_invoice(item.invoice)
        self.assertTrue(any("待人工确认" in error for error in validate_review_item(review, self.config)))
        review.review_acknowledged = True
        self.assertEqual(validate_review_item(review, self.config), [])

    @patch("batch_workflow.parse_invoice")
    def test_pdf_in_review_directory_still_appears_in_queue(self, mock_parse):
        review_dir = Path(self.config["review_dir"])
        review_dir.mkdir()
        source = review_dir / "review.pdf"
        source.write_bytes(b"PDF")
        invoice = make_invoice(source)
        invoice.status = InvoiceStatus.REVIEW_REQUIRED
        invoice.warnings = [InvoiceWarning("ITEM_TABLE_UNCERTAIN", "请核对明细", "items")]
        mock_parse.return_value = invoice
        scan = scan_input_directory(self.config)
        self.assertEqual(len(scan.items), 1)
        self.assertEqual(scan.items[0].source_path, source)
        self.assertTrue(scan.items[0].needs_review)

    @patch("batch_workflow.parse_invoice")
    def test_manual_review_metadata_is_restored_on_next_scan(self, mock_parse):
        source = self.input_dir / "complex.pdf"
        source.write_bytes(b"PDF")
        first_invoice = make_invoice(source)
        first_invoice.status = InvoiceStatus.REVIEW_REQUIRED
        first_invoice.warnings = [InvoiceWarning("MULTIPLE_ITEMS", "请核对明细", "items")]
        mock_parse.return_value = first_invoice
        first_scan = scan_input_directory(self.config)
        review = first_scan.items[0]
        review.short_name = "人工简称"
        review.remark = "已核对折扣行"
        review.review_acknowledged = True
        write_review_metadata(review.invoice, self.config, review)

        fresh_invoice = make_invoice(source)
        fresh_invoice.status = InvoiceStatus.REVIEW_REQUIRED
        fresh_invoice.warnings = [InvoiceWarning("MULTIPLE_ITEMS", "请核对明细", "items")]
        mock_parse.return_value = fresh_invoice
        restored = scan_input_directory(self.config).items[0]
        self.assertEqual(restored.short_name, "人工简称")
        self.assertEqual(restored.remark, "已核对折扣行")
        self.assertTrue(restored.review_acknowledged)

    def test_cancel_before_confirmation_changes_nothing(self):
        item = self.make_item()
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        with self.assertRaises(PermissionError):
            execute_batch([item], self.config, confirmed=False)
        after = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    @patch("batch_workflow.open_in_wps")
    @patch("batch_workflow.write_batch_with_rollback")
    def test_success_moves_source_writes_log_and_copies_pdf(self, mock_write, mock_open):
        item = self.make_item()
        backup = self.root / "backup" / "test"
        backup.mkdir(parents=True)
        shutil.copy2(self.wps_root / "stats.xlsx", backup / "stats.xlsx")
        shutil.copy2(self.wps_root / "inventory.xlsx", backup / "inventory.xlsx")
        mock_write.return_value = BatchWriteResult(
            rows=[WriteResult(74, 5, str(self.wps_root / "stats.xlsx"),
                              str(self.wps_root / "inventory.xlsx"), str(backup))],
            backup_dir=str(backup),
        )
        result = execute_batch([item], self.config, confirmed=True)
        self.assertFalse(item.source_path.exists())
        self.assertTrue(Path(result.completed[0]).exists())
        self.assertTrue(Path(result.official_pdfs[0]).exists())
        record = json.loads(Path(result.log_file).read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(record["result"], "success")
        self.assertEqual(record["items"][0]["invoice_no"], item.invoice.invoice_no)
        mock_open.assert_called_once()

    @patch("batch_workflow.open_in_wps")
    @patch("batch_workflow._copy_official_pdf", side_effect=OSError("copy failed"))
    @patch("batch_workflow.write_batch_with_rollback")
    def test_pdf_copy_failure_restores_workbooks(self, mock_write, _mock_copy, _mock_open):
        item = self.make_item()
        backup = self.root / "backup" / "test"
        backup.mkdir(parents=True)
        (backup / "stats.xlsx").write_bytes(b"ORIGINAL STATS")
        (backup / "inventory.xlsx").write_bytes(b"ORIGINAL INVENTORY")
        (self.wps_root / "stats.xlsx").write_bytes(b"MODIFIED STATS")
        (self.wps_root / "inventory.xlsx").write_bytes(b"MODIFIED INVENTORY")
        mock_write.return_value = BatchWriteResult([], str(backup))
        with self.assertRaisesRegex(OSError, "copy failed"):
            execute_batch([item], self.config, confirmed=True)
        self.assertEqual((self.wps_root / "stats.xlsx").read_bytes(), b"ORIGINAL STATS")
        self.assertEqual((self.wps_root / "inventory.xlsx").read_bytes(), b"ORIGINAL INVENTORY")
        self.assertTrue(item.source_path.exists())


if __name__ == "__main__":
    unittest.main()
