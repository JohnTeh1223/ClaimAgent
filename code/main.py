from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

from invoice_parser import parse_invoice
from wps_backend import write_with_rollback, open_in_wps, restore_from_backup


CATEGORIES = ["差旅", "酬金", "设备资产", "市内交通", "材料费", "运费", "印刷费", "其他"]
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_config() -> dict:
    config_path = PROJECT_ROOT / "config.json"
    return json.loads(config_path.read_text(encoding="utf-8"))


def sanitize_filename_part(text: str) -> str:
    text = re.sub(r'[<>:"/\\|?*]', "", text)
    text = re.sub(r"\s+", " ", text).strip().rstrip(".")
    return text[:80] or "未命名"


def choose_category(guess: str) -> str:
    print("\n报销类别：")
    for i, c in enumerate(CATEGORIES, 1):
        marker = "  <自动建议>" if c == guess else ""
        print(f"  {i}. {c}{marker}")
    default_index = CATEGORIES.index(guess) + 1 if guess in CATEGORIES else len(CATEGORIES)
    raw = input(f"选择类别 [直接回车={default_index}]：").strip()
    if not raw:
        return CATEGORIES[default_index - 1]
    try:
        idx = int(raw)
        return CATEGORIES[idx - 1]
    except Exception:
        raise ValueError("类别输入无效。")


def ask_edit(label: str, current: str) -> str:
    raw = input(f"{label} [{current}]：").strip()
    return raw if raw else current


def validate_invoice(invoice, config: dict) -> list[str]:
    errors = []
    if invoice.buyer_name != config["expected_buyer_name"]:
        errors.append(f"购买方应为 {config['expected_buyer_name']}，实际识别为 {invoice.buyer_name}")
    if invoice.buyer_tax_id.upper() != config["expected_buyer_tax_id"].upper():
        errors.append(
            f"购买方税号应为 {config['expected_buyer_tax_id']}，实际识别为 {invoice.buyer_tax_id}"
        )
    if round(invoice.amount_without_tax + invoice.tax_amount, 2) != round(invoice.total_amount, 2):
        errors.append(
            f"金额校验失败：不含税 {invoice.amount_without_tax:.2f} + 税额 {invoice.tax_amount:.2f} != 合计 {invoice.total_amount:.2f}"
        )
    return errors


def print_preview(invoice, category: str, short_name: str, month: str, target_pdf: Path, stats_path: Path, inventory_path: Path | None):
    print("\n" + "=" * 66)
    print("                     报销写入预览（尚未写入）")
    print("=" * 66)
    print(f"发票号码       : {invoice.invoice_no}")
    print(f"开票日期       : {invoice.invoice_date}")
    print(f"购买方         : {invoice.buyer_name}")
    print(f"购买方税号     : {invoice.buyer_tax_id}")
    print(f"销售方         : {invoice.seller_name}")
    print(f"发票项目       : {invoice.item_name}")
    print(f"统计表简称     : {short_name}")
    print(f"报销类别       : {category}")
    print(f"数量/单位      : {invoice.quantity:g} {invoice.unit_for_inventory}")
    print(f"不含税金额     : ¥{invoice.amount_without_tax:.2f}")
    print(f"税额           : ¥{invoice.tax_amount:.2f}")
    print(f"价税合计       : ¥{invoice.total_amount:.2f}")
    print(f"月份文件夹     : {month}")
    print(f"PDF目标        : {target_pdf}")
    print(f"待报销统计     : {stats_path}")
    if category == "材料费":
        print(f"入库单         : {inventory_path}")
        print(f"入库品名       : {invoice.item_name}")
        print(f"入库型号       : {invoice.specification}")
        print(f"入库单位       : {invoice.unit_for_inventory}")
    print("=" * 66)
    print("注意：到这里为止，程序没有修改任何 WPS 文件。")


def process_one(pdf_path: Path, config: dict):
    invoice = parse_invoice(pdf_path)

    errors = validate_invoice(invoice, config)
    if errors:
        print("\n❌ 发票合规检查未通过：")
        for e in errors:
            print(" -", e)
        print("程序已停止，未写入 WPS。")
        return

    print("\n✓ 发票抬头与税号校验通过")
    category = choose_category(invoice.category_guess)
    short_name = ask_edit("统计表简称", invoice.short_name)
    invoice.short_name = short_name

    if category == "材料费":
        invoice.specification = ask_edit("入库型号/规格", invoice.specification)
        invoice.unit_for_inventory = ask_edit("入库单位", invoice.unit_for_inventory)
        qty_raw = input(f"入库数量 [{invoice.quantity:g}]：").strip()
        if qty_raw:
            invoice.quantity = float(qty_raw)

    default_month = invoice.invoice_date[:7].replace("-", "")
    month = ask_edit("报销月份文件夹", default_month)
    if not re.fullmatch(r"20\d{4}", month):
        raise ValueError("月份必须为 YYYYMM，例如 202608。")

    wps_root = Path(config["wps_root"])
    stats_path = wps_root / config["stats_file"]
    inventory_path = wps_root / config["inventory_file"] if config.get("inventory_file") else None
    month_dir = wps_root / month
    new_filename = f"{invoice.total_amount:.2f}{sanitize_filename_part(short_name)}.pdf"
    target_pdf = month_dir / new_filename

    if not stats_path.exists():
        raise FileNotFoundError(f"找不到待报销统计表：{stats_path}")
    if category == "材料费" and (inventory_path is None or not inventory_path.exists()):
        raise FileNotFoundError(f"找不到入库单：{inventory_path}")
    if target_pdf.exists():
        raise FileExistsError(f"目标 PDF 已存在，为避免重复报销已停止：{target_pdf}")

    print_preview(invoice, category, short_name, month, target_pdf, stats_path, inventory_path)

    print("\n这一步会真正修改 WPS 文件并复制发票。")
    confirm = input("如确认无误，请完整输入 YES：").strip()
    if confirm != "YES":
        print("已取消。未写入 WPS，未复制 PDF。")
        return

    # All destructive/official writes start only AFTER confirmation.
    month_dir.mkdir(parents=True, exist_ok=True)
    backup_root = Path(config["backup_dir"])
    backup_root.mkdir(parents=True, exist_ok=True)

    note = invoice.invoice_date.replace("-", "")
    result = write_with_rollback(
        stats_path=stats_path,
        inventory_path=inventory_path,
        backup_root=backup_root,
        category=category,
        short_name=short_name,
        note=note,
        invoice=invoice,
    )

    try:
        shutil.copy2(pdf_path, target_pdf)
    except Exception:
        originals = [stats_path]
        if category == "材料费" and inventory_path:
            originals.append(inventory_path)
        restore_from_backup(Path(result.backup_dir), originals)
        if target_pdf.exists():
            try:
                target_pdf.unlink()
            except Exception:
                pass
        print("⚠ PDF 复制失败，已尝试把两个 WPS 表格恢复到写入前状态。")
        raise

    print("\n✓ 写入完成")
    print(f"待报销统计写入行：{result.stats_row}")
    if result.inventory_row is not None:
        print(f"入库单写入行：{result.inventory_row}")
    print(f"发票已复制为：{target_pdf.name}")
    print("正在使用 WPS 表格打开修改后的文件供你复核……")

    open_paths = [stats_path]
    if category == "材料费" and inventory_path:
        open_paths.append(inventory_path)
    open_in_wps(open_paths)


def choose_pdf(config: dict) -> Path:
    if len(sys.argv) >= 2:
        return Path(sys.argv[1].strip('"'))

    input_dir = Path(config["input_dir"])
    input_dir.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(input_dir.glob("*.pdf"))
    if not pdfs:
        print(f"待处理目录没有 PDF：{input_dir}")
        raw = input("也可以直接粘贴一个 PDF 完整路径：").strip().strip('"')
        return Path(raw)
    if len(pdfs) == 1:
        print(f"发现 1 个 PDF：{pdfs[0].name}")
        return pdfs[0]

    print("发现多个 PDF：")
    for i, p in enumerate(pdfs, 1):
        print(f"  {i}. {p.name}")
    idx = int(input("先选择一个进行 V1 测试：").strip())
    return pdfs[idx - 1]


def main():
    config = load_config()
    pdf_path = choose_pdf(config)
    if not pdf_path.exists():
        raise FileNotFoundError(pdf_path)
    process_one(pdf_path, config)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("\n❌ 运行失败：", exc)
        print("没有把错误静默吞掉；请把这段报错完整发给我，我继续修。")
        input("按回车退出……")
        raise
