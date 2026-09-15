from __future__ import annotations

from file_ops import copy_file
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

CATEGORY_COLUMNS = {
    # 1-based COM columns: name, amount, note
    "差旅": (5, 6, 7),
    "酬金": (8, 9, 10),
    "设备资产": (11, 12, 13),
    "市内交通": (14, 15, 16),
    "材料费": (17, 18, 19),
    "运费": (20, 21, 22),
    "印刷费": (23, 24, 25),
    "其他": (26, 27, 28),
}


@dataclass
class WriteResult:
    stats_row: int
    inventory_row: Optional[int]
    stats_path: str
    inventory_path: Optional[str]
    backup_dir: str


@dataclass
class BatchWriteResult:
    rows: list[WriteResult]
    backup_dir: str


class WPSController:
    """Control WPS Spreadsheets through its COM Automation interface.

    This does NOT require Microsoft Excel. WPS Spreadsheet ProgID is KET.Application.
    """

    def __init__(self, visible: bool = False):
        self.visible = visible
        self.app = None

    def start(self):
        try:
            import win32com.client
        except ImportError as exc:
            raise RuntimeError(
                "缺少 pywin32，无法启动 WPS COM。请先运行："
                r"python -m pip install -r C:\ClaimAgent\others\requirements.txt"
            ) from exc
        last_error = None
        for progid in ("ket.Application", "KET.Application"):
            try:
                self.app = win32com.client.DispatchEx(progid)
                self.app.Visible = self.visible
                try:
                    self.app.DisplayAlerts = False
                except Exception:
                    pass
                return self.app
            except Exception as exc:
                last_error = exc
        raise RuntimeError(
            "无法启动 WPS 表格 COM 接口（KET.Application）。\n"
            "请确认：1) Windows 已安装 WPS Office；2) WPS 版本启用了 COM/VBA 组件；"
            "3) Python 与 WPS 位数兼容。\n"
            f"原始错误：{last_error}"
        )

    def quit(self):
        if self.app is not None:
            try:
                self.app.Quit()
            except Exception:
                pass
            self.app = None

    def open_workbook(self, path: Path):
        if self.app is None:
            self.start()
        return self.app.Workbooks.Open(str(path))


def backup_files(paths: list[Path], backup_root: Path) -> Path:
    missing = [str(path) for path in paths if path is None or not path.is_file()]
    if missing:
        raise FileNotFoundError(f"无法备份，文件不存在：{', '.join(missing)}")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    dst = backup_root / stamp
    dst.mkdir(parents=True, exist_ok=True)
    for path in paths:
        if path and path.exists():
            copy_file(path, dst / path.name)
    return dst


def _wps_plan_sort_key(plan) -> tuple:
    """Sort reviewed plans chronologically before any WPS row is written."""
    invoice = plan.invoice
    raw_date = str(getattr(invoice, "invoice_date", "")).strip()
    try:
        invoice_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"发票日期格式无效，无法按日期写入 WPS：{raw_date}") from exc
    invoice_no = str(getattr(invoice, "invoice_no", "")).strip()
    source_name = Path(str(getattr(invoice, "source_pdf", ""))).name.casefold()
    return invoice_date, invoice_no, source_name


def write_batch_with_rollback(
    stats_path: Path,
    inventory_path: Optional[Path],
    backup_root: Path,
    plans: list,
) -> BatchWriteResult:
    """Write a reviewed batch using one backup/rollback boundary.

    Each plan must expose ``invoice``, ``category`` and ``short_name``.
    Callers must perform human confirmation and duplicate checks before calling.
    """
    if not plans:
        raise ValueError("批次为空，没有可写入的发票。")

    # Never rely on upload/GUI order for financial rows. Oldest invoice first;
    # same-day invoices use invoice number and source name for stable ordering.
    ordered_plans = sorted(plans, key=_wps_plan_sort_key)
    needs_inventory = any(plan.category == "材料费" for plan in ordered_plans)
    paths_to_backup = [stats_path]
    if needs_inventory:
        if inventory_path is None:
            raise RuntimeError("批次包含材料费，但未配置 inventory_file。")
        paths_to_backup.append(inventory_path)

    backup_dir = backup_files(paths_to_backup, backup_root)
    controller = WPSController(visible=False)
    stats_wb = None
    inv_wb = None
    results: list[WriteResult] = []
    try:
        controller.start()
        stats_wb = controller.open_workbook(stats_path)
        if needs_inventory:
            inv_wb = controller.open_workbook(inventory_path)

        for plan in ordered_plans:
            invoice = plan.invoice
            stats_row = write_stats(
                stats_wb,
                plan.category,
                plan.short_name,
                invoice.total_amount,
                invoice.invoice_date.replace("-", ""),
            )
            inventory_row = None
            if plan.category == "材料费":
                inventory_row = write_inventory(
                    inv_wb,
                    item_name=getattr(plan, "inventory_name", "") or invoice.item_name,
                    specification=plan.specification,
                    unit=plan.unit,
                    quantity=plan.quantity,
                    amount_without_tax=invoice.amount_without_tax,
                    tax_amount=invoice.tax_amount,
                    remark=getattr(plan, "remark", ""),
                )
            results.append(
                WriteResult(
                    stats_row=stats_row,
                    inventory_row=inventory_row,
                    stats_path=str(stats_path),
                    inventory_path=str(inventory_path) if inventory_path else None,
                    backup_dir=str(backup_dir),
                )
            )

        if stats_wb is not None:
            stats_wb.Close(SaveChanges=True)
            stats_wb = None
        if inv_wb is not None:
            inv_wb.Close(SaveChanges=True)
            inv_wb = None
        return BatchWriteResult(rows=results, backup_dir=str(backup_dir))
    except Exception:
        for wb in (stats_wb, inv_wb):
            if wb is not None:
                try:
                    wb.Close(SaveChanges=False)
                except Exception:
                    pass
        controller.quit()
        restore_from_backup(backup_dir, paths_to_backup)
        raise
    finally:
        controller.quit()


def find_first_blank_row(ws, name_col: int, start_row: int = 74, end_row: int = 365) -> int:
    for row in range(start_row, end_row + 1):
        value = ws.Cells(row, name_col).Value
        if value is None or str(value).strip() == "":
            return row
    raise RuntimeError(f"待报销区域 {start_row}-{end_row} 已满。")


def write_stats(wb, category: str, short_name: str, total_amount: float, note: str) -> int:
    if category not in CATEGORY_COLUMNS:
        raise ValueError(f"未知报销类别：{category}")
    ws = wb.Worksheets(1)
    name_col, amount_col, note_col = CATEGORY_COLUMNS[category]
    row = find_first_blank_row(ws, name_col)
    ws.Cells(row, name_col).Value = short_name
    ws.Cells(row, amount_col).Value = float(total_amount)
    ws.Cells(row, note_col).Value = note
    wb.Save()
    return row


def _find_inventory_total_row(ws, max_row: int = 500) -> int:
    for row in range(1, max_row + 1):
        value = ws.Cells(row, 1).Value
        if value is not None and "金额合计" in str(value):
            return row
    raise RuntimeError("入库单中未找到“金额合计”行。")


def write_inventory(
    wb,
    item_name: str,
    specification: str,
    unit: str,
    quantity: float,
    amount_without_tax: float,
    tax_amount: float,
    remark: str = "",
) -> int:
    ws = wb.Worksheets(1)
    total_row = _find_inventory_total_row(ws)
    previous_item_row = total_row - 1

    # Insert one row before the total line; WPS/Excel-compatible formulas below should shift automatically.
    ws.Rows(total_row).Insert()

    # Copy the previous material row to preserve borders, fonts, row height, and number formats.
    try:
        ws.Range(f"A{previous_item_row}:I{previous_item_row}").Copy(
            ws.Range(f"A{total_row}:I{total_row}")
        )
    except Exception:
        # If direct destination copy is unavailable in a particular WPS build, continue with plain cells.
        pass

    row = total_row
    ws.Cells(row, 1).Formula = "=ROW()-4"
    ws.Cells(row, 2).Value = item_name
    ws.Cells(row, 3).Value = specification or "-"
    ws.Cells(row, 4).Value = unit or "-"
    ws.Cells(row, 5).Value = float(quantity)
    ws.Cells(row, 6).Value = float(amount_without_tax)
    ws.Cells(row, 7).Value = float(tax_amount)
    ws.Cells(row, 8).Formula = f"=F{row}+G{row}"
    ws.Cells(row, 9).Value = remark

    # The total row has moved down by one. Explicitly refresh its sum formula for safety.
    new_total_row = row + 1
    ws.Cells(new_total_row, 8).Formula = f"=SUM(H5:H{row})"

    wb.Save()
    return row


def write_with_rollback(
    stats_path: Path,
    inventory_path: Optional[Path],
    backup_root: Path,
    category: str,
    short_name: str,
    note: str,
    invoice,
) -> WriteResult:
    paths_to_backup = [stats_path]
    if category == "材料费" and inventory_path is not None:
        paths_to_backup.append(inventory_path)
    backup_dir = backup_files(paths_to_backup, backup_root)

    controller = WPSController(visible=False)
    stats_wb = None
    inv_wb = None
    try:
        controller.start()
        stats_wb = controller.open_workbook(stats_path)
        stats_row = write_stats(stats_wb, category, short_name, invoice.total_amount, note)
        stats_wb.Close(SaveChanges=True)
        stats_wb = None

        inventory_row = None
        if category == "材料费":
            if inventory_path is None:
                raise RuntimeError("材料费需要配置 inventory_file。")
            inv_wb = controller.open_workbook(inventory_path)
            inventory_row = write_inventory(
                inv_wb,
                item_name=invoice.item_name,
                specification=invoice.specification,
                unit=invoice.unit_for_inventory,
                quantity=invoice.quantity,
                amount_without_tax=invoice.amount_without_tax,
                tax_amount=invoice.tax_amount,
                remark="",
            )
            inv_wb.Close(SaveChanges=True)
            inv_wb = None

        return WriteResult(
            stats_row=stats_row,
            inventory_row=inventory_row,
            stats_path=str(stats_path),
            inventory_path=str(inventory_path) if inventory_path else None,
            backup_dir=str(backup_dir),
        )
    except Exception:
        # Close workbooks first, then restore exact pre-write backups.
        for wb in (stats_wb, inv_wb):
            if wb is not None:
                try:
                    wb.Close(SaveChanges=False)
                except Exception:
                    pass
        controller.quit()
        for original in paths_to_backup:
            backup = backup_dir / original.name
            if backup.exists():
                copy_file(backup, original)
        raise
    finally:
        controller.quit()



def restore_from_backup(backup_dir: Path, originals: list[Path]):
    for original in originals:
        if original is None:
            continue
        backup = backup_dir / original.name
        if backup.exists():
            copy_file(backup, original)


def open_in_wps(paths: list[Path]):
    """Open the modified xlsx files visibly in WPS Spreadsheet, not Excel."""
    controller = WPSController(visible=True)
    controller.start()
    for path in paths:
        if path and path.exists():
            controller.open_workbook(path)
    # Deliberately do NOT call Quit(): leave WPS open for the user to inspect.
    return controller
