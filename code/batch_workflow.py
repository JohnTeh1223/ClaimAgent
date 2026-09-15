from __future__ import annotations

import json
import re
import shutil
import traceback
from file_ops import copy_file
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable

from invoice_parser import (
    InvoiceData,
    InvoiceItem,
    InvoiceParserUnavailable,
    InvoiceStatus,
    parse_invoice,
)
from wps_backend import open_in_wps, restore_from_backup, write_batch_with_rollback


CATEGORIES = ["差旅", "酬金", "设备资产", "市内交通", "材料费", "运费", "印刷费", "其他"]
PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class ReviewItem:
    invoice: InvoiceData
    category: str
    short_name: str
    specification: str
    unit: str
    quantity: float
    reimbursement_month: str
    inventory_name: str = ""
    remark: str = ""
    review_acknowledged: bool = False
    included: bool = True
    needs_review: bool = False
    errors: list[str] = field(default_factory=list)

    @classmethod
    def from_invoice(cls, invoice: InvoiceData) -> "ReviewItem":
        review_required = invoice.status == InvoiceStatus.REVIEW_REQUIRED
        return cls(
            invoice=invoice,
            category=invoice.category_guess if invoice.category_guess in CATEGORIES else "其他",
            short_name=invoice.short_name,
            specification=invoice.specification,
            unit=invoice.unit_for_inventory,
            quantity=invoice.quantity,
            reimbursement_month=invoice.invoice_date[:7].replace("-", ""),
            inventory_name=invoice.item_name,
            review_acknowledged=not review_required,
            included=not review_required,
            needs_review=review_required,
        )

    @property
    def source_path(self) -> Path:
        return Path(self.invoice.source_pdf)

    @property
    def target_filename(self) -> str:
        return f"{self.invoice.total_amount:.2f}{sanitize_filename_part(self.short_name)}.pdf"

    @property
    def fingerprint(self) -> tuple[str, str, str]:
        return (
            self.invoice.invoice_no.strip().upper(),
            self.invoice.seller_name.strip(),
            f"{self.invoice.total_amount:.2f}",
        )


@dataclass
class ParseFailure:
    source_pdf: str
    error: str
    review_path: str | None = None


@dataclass
class BatchScan:
    items: list[ReviewItem]
    failures: list[ParseFailure]


@dataclass
class BatchSummary:
    invoice_count: int
    total_amount: float
    totals_by_category: dict[str, float]
    targets: list[str]
    material_count: int
    review_count: int


@dataclass
class BatchExecutionResult:
    completed: list[str]
    official_pdfs: list[str]
    review_files: list[str]
    backup_dir: str
    log_file: str
    open_warning: str | None = None


def load_config(config_path: Path | None = None) -> dict:
    path = config_path or PROJECT_ROOT / "config.json"
    return json.loads(path.read_text(encoding="utf-8"))


def sanitize_filename_part(text: str) -> str:
    text = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "", text)
    text = re.sub(r"\s+", " ", text).strip().rstrip(".")
    return text[:80] or "未命名"


def configured_path(config: dict, key: str, fallback_name: str) -> Path:
    value = config.get(key)
    if value:
        return Path(value)
    return Path(config["input_dir"]).parent / fallback_name


def _money(value: object, label: str, errors: list[str]) -> Decimal | None:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        errors.append(f"{label}不是有效金额：{value}")
        return None


def validate_invoice(invoice: InvoiceData, config: dict) -> list[str]:
    errors: list[str] = []
    required = {
        "发票号码": invoice.invoice_no,
        "开票日期": invoice.invoice_date,
        "购买方": invoice.buyer_name,
        "购买方税号": invoice.buyer_tax_id,
        "销售方": invoice.seller_name,
        "发票项目": invoice.item_name,
    }
    for label, value in required.items():
        if not str(value).strip():
            errors.append(f"{label}缺失。")
    if invoice.buyer_name.strip() != config["expected_buyer_name"].strip():
        errors.append(f"购买方应为 {config['expected_buyer_name']}，实际为 {invoice.buyer_name or '空'}。")
    if invoice.buyer_tax_id.strip().upper() != config["expected_buyer_tax_id"].strip().upper():
        errors.append(f"购买方税号应为 {config['expected_buyer_tax_id']}，实际为 {invoice.buyer_tax_id or '空'}。")
    if invoice.invoice_no and not re.fullmatch(r"\d{20}", invoice.invoice_no.strip()):
        errors.append("发票号码必须为20位数字。")
    if invoice.invoice_date:
        try:
            datetime.strptime(invoice.invoice_date, "%Y-%m-%d")
        except ValueError:
            errors.append("开票日期必须是有效的 YYYY-MM-DD 日期。")

    amount = _money(invoice.amount_without_tax, "不含税金额", errors)
    tax = _money(invoice.tax_amount, "税额", errors)
    total = _money(invoice.total_amount, "价税合计", errors)
    if amount is not None and tax is not None and total is not None and amount + tax != total:
        errors.append(f"金额校验失败：不含税 {amount:.2f} + 税额 {tax:.2f} != 合计 {total:.2f}。")
    if total is not None and total <= 0:
        errors.append("价税合计必须大于 0。")
    tolerance = Decimal("0.02")
    items = getattr(invoice, "items", [])
    if items and all(item.amount is not None for item in items) and amount is not None:
        item_amount = sum((item.amount for item in items), Decimal("0"))
        if abs(item_amount - amount) > tolerance:
            errors.append(f"明细金额合计 {item_amount:.2f} 与不含税金额 {amount:.2f} 不一致。")
    if items and all(item.tax_amount is not None for item in items) and tax is not None:
        item_tax = sum((item.tax_amount for item in items), Decimal("0"))
        if abs(item_tax - tax) > tolerance:
            errors.append(f"明细税额合计 {item_tax:.2f} 与发票税额 {tax:.2f} 不一致。")
    return errors


def validate_review_item(item: ReviewItem, config: dict) -> list[str]:
    errors = validate_invoice(item.invoice, config)
    warnings = getattr(item.invoice, "warnings", [])
    if warnings and not item.review_acknowledged:
        errors.extend(f"待人工确认：{warning.message}" for warning in warnings)
    if item.category not in CATEGORIES:
        errors.append(f"未知报销类别：{item.category}")
    if not item.short_name.strip():
        errors.append("统计表简称不能为空。")
    if not re.fullmatch(r"20\d{4}", item.reimbursement_month.strip()):
        errors.append("报销月份必须为 YYYYMM，例如 202608。")
    if item.category == "材料费":
        if not item.inventory_name.strip():
            errors.append("材料费的入库品名不能为空。")
        if not item.specification.strip():
            errors.append("材料费的型号/规格不能为空。")
        if not item.unit.strip():
            errors.append("材料费的单位不能为空。")
        try:
            if float(item.quantity) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            errors.append("材料费数量必须为大于 0 的数字。")
    return errors


def append_log(config: dict, record: dict) -> Path:
    log_path = configured_path(config, "log_file", "logs/processing.jsonl")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"timestamp": datetime.now().astimezone().isoformat(timespec="seconds"), **record}
    with log_path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
    return log_path


def _successful_log_items(config: dict) -> list[dict]:
    log_path = configured_path(config, "log_file", "logs/processing.jsonl")
    if not log_path.is_file():
        return []
    results: list[dict] = []
    for line_number, line in enumerate(log_path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"处理日志第 {line_number} 行损坏，无法安全完成重复检测：{log_path}"
            ) from exc
        if record.get("event") == "batch_write" and record.get("result") == "success":
            items = record.get("items")
            if not isinstance(items, list):
                raise ValueError(f"处理日志第 {line_number} 行 items 格式无效：{log_path}")
            results.extend(items)
    return results


def route_to_review(source: Path, config: dict, reason: str, *, move: bool) -> Path:
    review_dir = configured_path(config, "review_dir", "待复核")
    review_dir.mkdir(parents=True, exist_ok=True)
    target = review_dir / source.name
    if target.exists() and source.resolve() != target.resolve():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        target = review_dir / f"{source.stem}_{stamp}{source.suffix}"
    if source.resolve() != target.resolve():
        if move:
            shutil.move(str(source), str(target))
        else:
            shutil.copy2(source, target)
    reason_path = target.with_suffix(target.suffix + ".error.json")
    reason_path.write_text(
        json.dumps(
            {
                "source": str(source),
                "status": InvoiceStatus.FAILED.value,
                "warnings": [{"code": "PARSE_FAILED", "message": reason}],
                "extracted": {},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return target


def _review_metadata_path(source: Path, config: dict) -> Path:
    review_dir = configured_path(config, "review_dir", "待复核")
    review_dir.mkdir(parents=True, exist_ok=True)
    legacy_path = review_dir / f"{source.name}.error.json"
    return legacy_path if legacy_path.exists() else review_dir / f"{source.name}.review.json"


def write_review_metadata(invoice: InvoiceData, config: dict, item: ReviewItem | None = None) -> Path:
    source = Path(invoice.source_pdf)
    metadata_path = _review_metadata_path(source, config)
    preserved_manual = {}
    if metadata_path.is_file() and item is None:
        try:
            preserved_manual = json.loads(metadata_path.read_text(encoding="utf-8")).get("manual", {})
        except (json.JSONDecodeError, OSError):
            preserved_manual = {}
    payload = {
        "source": str(source),
        "status": invoice.status.value,
        "warnings": [
            {"code": warning.code, "message": warning.message, "field": warning.field}
            for warning in invoice.warnings
        ],
        "extracted": {
            "invoice_number": invoice.invoice_no,
            "invoice_date": invoice.invoice_date,
            "buyer_name": invoice.buyer_name,
            "buyer_tax_id": invoice.buyer_tax_id,
            "seller_name": invoice.seller_name,
            "seller_tax_id": invoice.seller_tax_id,
            "total_without_tax": str(invoice.amount_without_tax),
            "total_tax": str(invoice.tax_amount),
            "grand_total": str(invoice.total_amount),
            "items": [
                {
                    "description": detail.description,
                    "specification": detail.specification,
                    "unit": detail.unit,
                    "quantity": str(detail.quantity) if detail.quantity is not None else None,
                    "unit_price": str(detail.unit_price) if detail.unit_price is not None else None,
                    "amount": str(detail.amount) if detail.amount is not None else None,
                    "tax_rate": detail.tax_rate,
                    "tax_amount": str(detail.tax_amount) if detail.tax_amount is not None else None,
                    "is_negative": detail.is_negative,
                }
                for detail in invoice.items
            ],
        },
        "manual": preserved_manual if item is None else {
            "invoice_no": invoice.invoice_no,
            "invoice_date": invoice.invoice_date,
            "buyer_name": invoice.buyer_name,
            "buyer_tax_id": invoice.buyer_tax_id,
            "seller_name": invoice.seller_name,
            "seller_tax_id": invoice.seller_tax_id,
            "item_name": invoice.item_name,
            "amount_without_tax": str(invoice.amount_without_tax),
            "tax_amount": str(invoice.tax_amount),
            "total_amount": str(invoice.total_amount),
            "category": item.category,
            "short_name": item.short_name,
            "inventory_name": item.inventory_name,
            "specification": item.specification,
            "unit": item.unit,
            "quantity": str(item.quantity),
            "reimbursement_month": item.reimbursement_month,
            "remark": item.remark,
            "review_acknowledged": item.review_acknowledged,
            "items": [
                {
                    "description": detail.description,
                    "specification": detail.specification,
                    "unit": detail.unit,
                    "quantity": str(detail.quantity) if detail.quantity is not None else None,
                    "unit_price": str(detail.unit_price) if detail.unit_price is not None else None,
                    "amount": str(detail.amount) if detail.amount is not None else None,
                    "tax_rate": detail.tax_rate,
                    "tax_amount": str(detail.tax_amount) if detail.tax_amount is not None else None,
                    "raw_text": detail.raw_text,
                    "is_negative": detail.is_negative,
                }
                for detail in invoice.items
            ],
        },
    }
    metadata_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return metadata_path


def apply_review_metadata(invoice: InvoiceData, config: dict) -> dict:
    metadata_path = _review_metadata_path(Path(invoice.source_pdf), config)
    if not metadata_path.is_file():
        return {}
    try:
        manual = json.loads(metadata_path.read_text(encoding="utf-8")).get("manual", {})
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(manual, dict) or not manual:
        return {}
    for key in ("invoice_no", "invoice_date", "buyer_name", "buyer_tax_id", "seller_name", "seller_tax_id", "item_name"):
        if key in manual:
            setattr(invoice, key, str(manual[key]))
    for key in ("amount_without_tax", "tax_amount", "total_amount"):
        if manual.get(key) not in (None, ""):
            try:
                setattr(invoice, key, Decimal(str(manual[key])).quantize(Decimal("0.01")))
            except InvalidOperation:
                pass
    restored_items = []
    for detail in manual.get("items", []):
        if not isinstance(detail, dict):
            continue
        def optional_decimal(key: str):
            value = detail.get(key)
            return Decimal(str(value)) if value not in (None, "") else None
        try:
            restored_items.append(InvoiceItem(
                description=str(detail.get("description", "")),
                specification=str(detail.get("specification", "")),
                unit=str(detail.get("unit", "")),
                quantity=optional_decimal("quantity"),
                unit_price=optional_decimal("unit_price"),
                amount=optional_decimal("amount"),
                tax_rate=str(detail.get("tax_rate", "")),
                tax_amount=optional_decimal("tax_amount"),
                raw_text=str(detail.get("raw_text", "")),
                is_negative=bool(detail.get("is_negative", False)),
            ))
        except (InvalidOperation, ValueError):
            continue
    if restored_items:
        invoice.items = restored_items
    return manual


def scan_input_directory(config: dict, *, route_parse_failures: bool = True) -> BatchScan:
    input_dir = Path(config["input_dir"])
    review_dir = configured_path(config, "review_dir", "待复核")
    input_dir.mkdir(parents=True, exist_ok=True)
    review_dir.mkdir(parents=True, exist_ok=True)
    items: list[ReviewItem] = []
    failures: list[ParseFailure] = []
    candidates = list(sorted(input_dir.glob("*.pdf"))) + list(sorted(review_dir.glob("*.pdf")))
    seen_names: set[str] = set()
    for pdf in candidates:
        name_key = pdf.name.casefold()
        if name_key in seen_names:
            continue
        seen_names.add(name_key)
        try:
            invoice = parse_invoice(pdf)
            manual = apply_review_metadata(invoice, config)
            item = ReviewItem.from_invoice(invoice)
            if manual:
                for key in ("category", "short_name", "inventory_name", "specification", "unit", "reimbursement_month", "remark"):
                    if key in manual:
                        setattr(item, key, str(manual[key]))
                try:
                    item.quantity = Decimal(str(manual.get("quantity", item.quantity)))
                except InvalidOperation:
                    pass
                item.review_acknowledged = bool(manual.get("review_acknowledged", False))
                item.needs_review = not item.review_acknowledged
                item.included = False
            if pdf.parent.resolve() == review_dir.resolve() and not manual:
                item.included = False
                item.needs_review = True
                item.review_acknowledged = False
            item.errors = validate_review_item(item, config)
            if item.errors:
                item.included = False
                item.needs_review = True
            if invoice.status == InvoiceStatus.REVIEW_REQUIRED:
                write_review_metadata(invoice, config, item)
            items.append(item)
        except InvoiceParserUnavailable:
            # This is a setup problem, not a bad invoice. Leave every source PDF
            # in the pending directory so installing the dependency is sufficient.
            raise
        except Exception as exc:
            review_path = None
            if route_parse_failures:
                try:
                    review_path = str(route_to_review(pdf, config, str(exc), move=pdf.parent != review_dir))
                except Exception as route_exc:
                    exc = RuntimeError(f"{exc}；移入待复核目录也失败：{route_exc}")
            failure = ParseFailure(str(pdf), str(exc), review_path)
            failures.append(failure)
            append_log(
                config,
                {
                    "event": "parse",
                    "result": "review_needed",
                    "source_pdf": str(pdf),
                    "review_path": review_path,
                    "error": str(exc),
                },
            )
    return BatchScan(items, failures)


def preflight_batch(items: Iterable[ReviewItem], config: dict) -> dict[str, list[str]]:
    active = [item for item in items if item.included and not item.needs_review]
    errors: dict[str, list[str]] = {str(item.source_path): validate_review_item(item, config) for item in active}
    stats_path = Path(config["wps_root"]) / config["stats_file"]
    if not stats_path.is_file():
        errors.setdefault("批次", []).append(f"找不到待报销统计表：{stats_path}")
    if any(item.category == "材料费" for item in active):
        inventory_name = config.get("inventory_file")
        inventory_path = Path(config["wps_root"]) / inventory_name if inventory_name else None
        if inventory_path is None or not inventory_path.is_file():
            errors.setdefault("批次", []).append(f"找不到入库单：{inventory_path}")

    by_invoice: dict[str, list[ReviewItem]] = {}
    by_target: dict[Path, list[ReviewItem]] = {}
    for item in active:
        if not item.source_path.is_file():
            errors[str(item.source_path)].append(f"源 PDF 不存在：{item.source_path}")
        by_invoice.setdefault(item.invoice.invoice_no.strip().upper(), []).append(item)
        target = Path(config["wps_root"]) / item.reimbursement_month / item.target_filename
        by_target.setdefault(target, []).append(item)
        if target.exists():
            errors[str(item.source_path)].append(f"目标 PDF 已存在，疑似重复报销：{target}")
        completed = configured_path(config, "completed_dir", "已完成") / item.source_path.name
        if completed.exists():
            errors[str(item.source_path)].append(f"完成目录已有同名源文件：{completed}")
    for invoice_no, matches in by_invoice.items():
        if invoice_no and len(matches) > 1:
            for item in matches:
                errors[str(item.source_path)].append(f"本批次发票号码重复：{invoice_no}")
    for target, matches in by_target.items():
        if len(matches) > 1:
            for item in matches:
                errors[str(item.source_path)].append(f"本批次目标文件名冲突：{target.name}")

    prior = _successful_log_items(config)
    prior_invoice_nos = {str(row.get("invoice_no", "")).strip().upper() for row in prior}
    for item in active:
        if item.invoice.invoice_no.strip().upper() in prior_invoice_nos:
            errors[str(item.source_path)].append(
                f"处理日志中已有发票号码 {item.invoice.invoice_no}，疑似重复报销。"
            )
    return {key: value for key, value in errors.items() if value}


def sort_review_items_by_date(items: Iterable[ReviewItem]) -> list[ReviewItem]:
    """Return a stable chronological order matching the WPS write order."""
    return sorted(
        items,
        key=lambda item: (
            item.invoice.invoice_date,
            item.invoice.invoice_no.strip(),
            item.source_path.name.casefold(),
        ),
    )


def summarize(items: Iterable[ReviewItem]) -> BatchSummary:
    all_items = list(items)
    active = sort_review_items_by_date(
        item for item in all_items if item.included and not item.needs_review
    )
    totals: dict[str, float] = {}
    for item in active:
        totals[item.category] = round(totals.get(item.category, 0.0) + float(item.invoice.total_amount), 2)
    return BatchSummary(
        invoice_count=len(active),
        total_amount=round(sum(float(item.invoice.total_amount) for item in active), 2),
        totals_by_category=totals,
        targets=[f"{item.reimbursement_month}/{item.target_filename}" for item in active],
        material_count=sum(item.category == "材料费" for item in active),
        review_count=sum(item.needs_review for item in all_items),
    )


def _log_item(item: ReviewItem, target: Path, completed: Path) -> dict:
    return {
        "invoice_no": item.invoice.invoice_no,
        "invoice_date": item.invoice.invoice_date,
        "seller_name": item.invoice.seller_name,
        "seller_tax_id": item.invoice.seller_tax_id,
        "category": item.category,
        "amount": round(float(item.invoice.total_amount), 2),
        "month": item.reimbursement_month,
        "remark": item.remark,
        "output_filename": item.target_filename,
        "official_pdf": str(target),
        "completed_source": str(completed),
    }


def _copy_official_pdf(source: Path, target: Path) -> None:
    copy_file(source, target)


def execute_batch(items: list[ReviewItem], config: dict, *, confirmed: bool) -> BatchExecutionResult:
    if not confirmed:
        raise PermissionError("没有收到明确的批次确认，未执行任何正式写入。")
    active = sort_review_items_by_date(
        item for item in items if item.included and not item.needs_review
    )
    if not active:
        raise ValueError("没有已勾选且通过校验的发票。")
    errors = preflight_batch(active, config)
    if errors:
        detail = "；".join(f"{key}: {' | '.join(value)}" for key, value in errors.items())
        raise ValueError(f"最终写入前检查未通过：{detail}")

    wps_root = Path(config["wps_root"])
    stats_path = wps_root / config["stats_file"]
    inventory_path = wps_root / config["inventory_file"] if config.get("inventory_file") else None
    backup_root = Path(config["backup_dir"])
    completed_dir = configured_path(config, "completed_dir", "已完成")
    backup_root.mkdir(parents=True, exist_ok=True)
    completed_dir.mkdir(parents=True, exist_ok=True)

    try:
        write_result = write_batch_with_rollback(stats_path, inventory_path, backup_root, active)
    except Exception as exc:
        try:
            append_log(config, {"event": "batch_write", "result": "failed",
                                "stage": "backup_or_wps", "error": str(exc),
                                "traceback": traceback.format_exc()})
        except Exception:
            pass
        raise
    backup_dir = Path(write_result.backup_dir)
    official_created: list[Path] = []
    moved_sources: list[tuple[Path, Path]] = []
    log_items: list[dict] = []
    try:
        for item in active:
            target_dir = wps_root / item.reimbursement_month
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / item.target_filename
            official_created.append(target)
            _copy_official_pdf(item.source_path, target)

        for item, target in zip(active, official_created):
            completed = completed_dir / item.source_path.name
            source = item.source_path
            shutil.move(str(source), str(completed))
            moved_sources.append((source, completed))
            log_items.append(_log_item(item, target, completed))

        log_path = append_log(
            config,
            {
                "event": "batch_write",
                "result": "success",
                "backup_dir": str(backup_dir),
                "items": log_items,
            },
        )
    except Exception as exc:
        for source, completed in reversed(moved_sources):
            if completed.exists() and not source.exists():
                shutil.move(str(completed), str(source))
        for target in reversed(official_created):
            if target.exists():
                target.unlink()
        originals = [stats_path]
        if any(item.category == "材料费" for item in active) and inventory_path is not None:
            originals.append(inventory_path)
        restore_from_backup(backup_dir, originals)
        try:
            append_log(
                config,
                {
                    "event": "batch_write",
                    "result": "rolled_back",
                    "backup_dir": str(backup_dir),
                    "error": str(exc),
                    "items": [
                        {
                            "invoice_no": item.invoice.invoice_no,
                            "source_pdf": str(item.source_path),
                        }
                        for item in active
                    ],
                },
            )
        except Exception:
            pass
        raise

    open_warning = None
    try:
        open_paths = [stats_path]
        if any(item.category == "材料费" for item in active) and inventory_path is not None:
            open_paths.append(inventory_path)
        open_in_wps(open_paths)
    except Exception as exc:
        open_warning = f"写入已完成，但无法自动打开 WPS 供复核：{exc}"
    return BatchExecutionResult(
        completed=[str(path) for _, path in moved_sources],
        official_pdfs=[str(path) for path in official_created],
        review_files=[],
        backup_dir=str(backup_dir),
        log_file=str(log_path),
        open_warning=open_warning,
    )
