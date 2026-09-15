from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "code"))

from wps_backend import write_batch_with_rollback


class FakeWorkbook:
    def __init__(self, path: Path):
        self.path = path
        self.closed = False

    def Close(self, SaveChanges=True):
        self.closed = True


class FakeController:
    instances = []

    def __init__(self, visible=False):
        self.opened = []
        self.quit_called = False
        self.__class__.instances.append(self)

    def start(self):
        return self

    def open_workbook(self, path):
        workbook = FakeWorkbook(Path(path))
        self.opened.append(workbook)
        return workbook

    def quit(self):
        self.quit_called = True


def plan(category="其他", invoice_date="2026-08-05", invoice_no="26444000000000000001", item_name="测试项目"):
    invoice = SimpleNamespace(
        total_amount=10.0,
        invoice_date=invoice_date,
        invoice_no=invoice_no,
        source_pdf=f"{invoice_no}.pdf",
        item_name=item_name,
        amount_without_tax=9.0,
        tax_amount=1.0,
    )
    return SimpleNamespace(
        invoice=invoice,
        category=category,
        short_name=item_name,
        specification="-",
        unit="个",
        quantity=1.0,
    )


class WpsBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stats = self.root / "stats.xlsx"
        self.inventory = self.root / "inventory.xlsx"
        self.backups = self.root / "backup"
        self.stats.write_bytes(b"ORIGINAL STATS")
        self.inventory.write_bytes(b"ORIGINAL INVENTORY")
        FakeController.instances.clear()

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_spreadsheet_stops_before_com(self):
        self.stats.unlink()
        with self.assertRaises(FileNotFoundError):
            write_batch_with_rollback(self.stats, self.inventory, self.backups, [plan()])

    @patch("wps_backend.WPSController", FakeController)
    @patch("wps_backend.write_inventory", return_value=5)
    @patch("wps_backend.write_stats", return_value=74)
    def test_non_material_only_opens_stats(self, _stats, _inventory):
        result = write_batch_with_rollback(self.stats, self.inventory, self.backups, [plan("其他")])
        self.assertEqual(len(FakeController.instances[-1].opened), 1)
        self.assertIsNone(result.rows[0].inventory_row)

    @patch("wps_backend.WPSController", FakeController)
    @patch("wps_backend.write_inventory", return_value=5)
    @patch("wps_backend.write_stats", return_value=74)
    def test_material_opens_and_writes_both_workbooks(self, _stats, mock_inventory):
        result = write_batch_with_rollback(self.stats, self.inventory, self.backups, [plan("材料费")])
        self.assertEqual(len(FakeController.instances[-1].opened), 2)
        self.assertEqual(result.rows[0].inventory_row, 5)
        mock_inventory.assert_called_once()

    @patch("wps_backend.WPSController", FakeController)
    @patch("wps_backend.write_inventory", return_value=5)
    @patch("wps_backend.write_stats", return_value=74)
    def test_batch_writes_oldest_invoice_first(self, mock_stats, mock_inventory):
        newest = plan("材料费", "2026-08-20", "26444000000000000003", "较晚发票")
        oldest = plan("材料费", "2026-07-01", "26444000000000000001", "最早发票")
        middle = plan("材料费", "2026-08-05", "26444000000000000002", "中间发票")

        write_batch_with_rollback(
            self.stats,
            self.inventory,
            self.backups,
            [newest, oldest, middle],
        )

        self.assertEqual(
            [call.args[4] for call in mock_stats.call_args_list],
            ["20260701", "20260805", "20260820"],
        )
        self.assertEqual(
            [call.kwargs["item_name"] for call in mock_inventory.call_args_list],
            ["最早发票", "中间发票", "较晚发票"],
        )

    @patch("wps_backend.WPSController", FakeController)
    @patch("wps_backend.write_inventory", return_value=5)
    @patch("wps_backend.write_stats", return_value=74)
    def test_same_date_uses_invoice_number_for_stable_order(self, mock_stats, _inventory):
        second = plan("其他", "2026-08-05", "26444000000000000002")
        first = plan("其他", "2026-08-05", "26444000000000000001")
        write_batch_with_rollback(self.stats, self.inventory, self.backups, [second, first])
        self.assertEqual(
            [call.args[2] for call in mock_stats.call_args_list],
            [first.short_name, second.short_name],
        )

    def test_invalid_date_stops_before_backup_or_com(self):
        with self.assertRaisesRegex(ValueError, "发票日期格式无效"):
            write_batch_with_rollback(
                self.stats,
                self.inventory,
                self.backups,
                [plan(invoice_date="2026/08/05")],
            )
        self.assertFalse(self.backups.exists())

    @patch("wps_backend.WPSController", FakeController)
    def test_write_failure_restores_original_spreadsheets(self):
        def modify_stats(workbook, *_args):
            workbook.path.write_bytes(b"MODIFIED STATS")
            return 74

        def fail_inventory(workbook, **_kwargs):
            workbook.path.write_bytes(b"MODIFIED INVENTORY")
            raise RuntimeError("inventory failed")

        with patch("wps_backend.write_stats", side_effect=modify_stats), patch(
            "wps_backend.write_inventory", side_effect=fail_inventory
        ):
            with self.assertRaisesRegex(RuntimeError, "inventory failed"):
                write_batch_with_rollback(self.stats, self.inventory, self.backups, [plan("材料费")])
        self.assertEqual(self.stats.read_bytes(), b"ORIGINAL STATS")
        self.assertEqual(self.inventory.read_bytes(), b"ORIGINAL INVENTORY")

    def test_wps_startup_failure_leaves_files_unchanged(self):
        class FailingController(FakeController):
            def start(self):
                raise RuntimeError("KET unavailable")

        with patch("wps_backend.WPSController", FailingController):
            with self.assertRaisesRegex(RuntimeError, "KET unavailable"):
                write_batch_with_rollback(self.stats, self.inventory, self.backups, [plan("其他")])
        self.assertEqual(self.stats.read_bytes(), b"ORIGINAL STATS")
        self.assertEqual(self.inventory.read_bytes(), b"ORIGINAL INVENTORY")


if __name__ == "__main__":
    unittest.main()
