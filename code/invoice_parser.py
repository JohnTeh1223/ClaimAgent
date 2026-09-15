from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Optional


class InvoiceParserUnavailable(RuntimeError):
    """Raised when the local PDF parsing dependency is unavailable."""


class InvoiceStatus(str, Enum):
    PARSED_OK = "PARSED_OK"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAILED = "FAILED"


@dataclass
class InvoiceWarning:
    code: str
    message: str
    field: str = ""


@dataclass
class InvoiceItem:
    description: str = ""
    specification: str = ""
    unit: str = ""
    quantity: Optional[Decimal] = None
    unit_price: Optional[Decimal] = None
    amount: Optional[Decimal] = None
    tax_rate: str = ""
    tax_amount: Optional[Decimal] = None
    raw_text: str = ""
    is_negative: bool = False


@dataclass
class InvoiceData:
    source_pdf: str
    invoice_no: str
    invoice_date: str  # YYYY-MM-DD
    buyer_name: str
    buyer_tax_id: str
    seller_name: str
    seller_tax_id: str
    item_name: str
    short_name: str
    specification: str
    unit_raw: str
    unit_for_inventory: str
    quantity: Decimal
    unit_price: Optional[Decimal]
    amount_without_tax: Decimal
    tax_amount: Decimal
    total_amount: Decimal
    tax_rate: str
    category_guess: str
    items: list[InvoiceItem] = field(default_factory=list)
    status: InvoiceStatus = InvoiceStatus.PARSED_OK
    warnings: list[InvoiceWarning] = field(default_factory=list)
    raw_text: str = ""

    @property
    def invoice_number(self) -> str:
        return self.invoice_no

    @property
    def total_without_tax(self) -> Decimal:
        return self.amount_without_tax

    @property
    def total_tax(self) -> Decimal:
        return self.tax_amount

    @property
    def grand_total(self) -> Decimal:
        return self.total_amount

    def to_dict(self) -> dict:
        def serialise(value):
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, Enum):
                return value.value
            if isinstance(value, list):
                return [serialise(item) for item in value]
            if isinstance(value, dict):
                return {key: serialise(item) for key, item in value.items()}
            return value

        return serialise(asdict(self))


UNIT_MAP = {
    "pcs": "个",
    "pc": "个",
    "piece": "个",
    "pieces": "个",
}

MATERIAL_HINTS = (
    "电子元件", "计算机网络设备", "金属制品", "电线电缆", "橡胶制品", "塑料制品", "轴承",
    "医药", "航空航天设备", "体育用品", "化学试剂", "导航遥控设备",
    "机械设备", "仪器", "耗材", "材料", "模块", "接收机", "开发板", "传感器",
)
TRANSPORT_HINTS = ("出租车", "网约车", "滴滴", "客运", "交通运输服务", "市内交通")
FREIGHT_HINTS = ("快递", "顺丰", "邮政", "物流", "运费")
PRINT_HINTS = ("印刷", "打印", "复印")
TRAVEL_HINTS = ("火车票", "铁路", "航空旅客运输", "住宿", "酒店")


def _clean_lines(text: str) -> list[str]:
    lines = []
    for raw in text.splitlines():
        cleaned = re.sub(r"\s+", " ", raw).strip()
        if cleaned:
            lines.append(cleaned)
    return lines


def _normalise_tax_id(text: str) -> str:
    return re.sub(r"\s+", "", text).upper()


def _shorten_item(item: str) -> str:
    item = item.strip()
    parts = [part.strip() for part in item.split("*") if part.strip()]
    if len(parts) >= 2:
        return parts[-1]
    return item.split("*")[-1].strip() if "*" in item else item


def _guess_category(item_name: str, seller_name: str) -> str:
    text = f"{item_name} {seller_name}"
    if any(keyword in text for keyword in TRANSPORT_HINTS):
        return "市内交通"
    if any(keyword in text for keyword in FREIGHT_HINTS):
        return "运费"
    if any(keyword in text for keyword in PRINT_HINTS):
        return "印刷费"
    if any(keyword in text for keyword in TRAVEL_HINTS):
        return "差旅"
    if any(keyword in text for keyword in MATERIAL_HINTS) or "*" in item_name:
        return "材料费"
    return "其他"


def _decimal(text: object) -> Optional[Decimal]:
    if text is None:
        return None
    cleaned = str(text).replace("¥", "").replace(",", "").strip()
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", cleaned):
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _first_decimal(words: list[tuple], x_min: float, x_max: float) -> Optional[Decimal]:
    for word in sorted(words, key=lambda value: (value[1], value[0])):
        if x_min <= word[0] < x_max:
            value = _decimal(word[4])
            if value is not None:
                return value
    return None


def _text_in_column(words: list[tuple], x_min: float, x_max: float) -> str:
    selected = [
        word for word in sorted(words, key=lambda value: (value[1], value[0]))
        if x_min <= word[0] < x_max
    ]
    return "".join(str(word[4]).strip() for word in selected).strip()


def _parse_items_from_page(page) -> list[InvoiceItem]:
    """Parse common Chinese e-invoice detail rows using table geometry.

    The amount column is used as the row anchor, which preserves genuine
    multi-item rows and negative discount/adjustment rows.
    """
    words = page.get_text("words", sort=True)
    if not words:
        return []
    width = float(page.rect.width)
    header_y_values = [word[1] for word in words if str(word[4]).replace(" ", "") == "项目名称"]
    if not header_y_values:
        return []
    header_y = min(header_y_values)
    total_y_values = [
        word[1] for word in words
        if word[1] > header_y + 20 and word[0] < width * 0.25 and str(word[4]).strip() == "合"
    ]
    # Total values are often printed a few points above the visual “合计” label.
    total_y = min(total_y_values) - 5 if total_y_values else max(word[1] for word in words) + 1

    amount_anchors = []
    for word in words:
        if not (header_y < word[1] < total_y):
            continue
        if width * 0.66 <= word[0] < width * 0.76 and _decimal(word[4]) is not None:
            amount_anchors.append(word)
    amount_anchors.sort(key=lambda value: (value[1], value[0]))

    items: list[InvoiceItem] = []
    for index, anchor in enumerate(amount_anchors):
        row_start = anchor[1] - 2.5
        row_end = amount_anchors[index + 1][1] - 2.5 if index + 1 < len(amount_anchors) else total_y
        row_words = [word for word in words if row_start <= word[1] < row_end]
        description = _text_in_column(row_words, 0, width * 0.20)
        specification = _text_in_column(row_words, width * 0.20, width * 0.31)
        unit = _text_in_column(row_words, width * 0.31, width * 0.42)
        quantity = _first_decimal(row_words, width * 0.42, width * 0.49)
        unit_price = _first_decimal(row_words, width * 0.49, width * 0.66)
        amount = _decimal(anchor[4])
        tax_rate = _text_in_column(row_words, width * 0.76, width * 0.90)
        tax_amount = _first_decimal(row_words, width * 0.90, width + 1)
        raw_text = " ".join(
            str(word[4]).strip() for word in sorted(row_words, key=lambda value: (value[1], value[0]))
        )
        if not description and amount is None and tax_amount is None:
            continue
        items.append(
            InvoiceItem(
                description=description,
                specification=specification,
                unit=unit,
                quantity=quantity,
                unit_price=unit_price,
                amount=amount,
                tax_rate=tax_rate,
                tax_amount=tax_amount,
                raw_text=raw_text,
                is_negative=(amount is not None and amount < 0) or (tax_amount is not None and tax_amount < 0),
            )
        )
    return items


def _previous_meaningful_line(lines: list[str], target_tax: str) -> str:
    for index, line in enumerate(lines):
        if _normalise_tax_id(line) != target_tax:
            continue
        for candidate_index in range(index - 1, -1, -1):
            candidate = lines[candidate_index]
            if candidate in {"名称：", "名称:", "统一社会信用代码/纳税人识别号：", "统一社会信用代码/纳税人识别号:"}:
                continue
            if re.fullmatch(r"20\d{2}年.*日", candidate) or re.fullmatch(r"\d{20}", candidate):
                continue
            return candidate
    return ""


def _warning(warnings: list[InvoiceWarning], code: str, message: str, field: str = "") -> None:
    warnings.append(InvoiceWarning(code=code, message=message, field=field))


def parse_invoice(pdf_path: str | Path) -> InvoiceData:
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        raise InvoiceParserUnavailable(
            "缺少 PyMuPDF，无法读取 PDF。请先运行："
            r"python -m pip install -r C:\ClaimAgent\others\requirements.txt"
        ) from exc

    pdf_path = Path(pdf_path)
    if not pdf_path.is_file():
        raise FileNotFoundError(pdf_path)
    try:
        document = fitz.open(pdf_path)
    except Exception as exc:
        raise ValueError(f"PDF 损坏或无法打开：{exc}") from exc
    try:
        if document.is_encrypted and not document.authenticate(""):
            raise ValueError("PDF 已加密，无法读取。")
        text = "\n".join(page.get_text("text") for page in document)
        items = []
        for page in document:
            items.extend(_parse_items_from_page(page))
    finally:
        document.close()

    lines = _clean_lines(text)
    compact_text = "\n".join(lines)
    if len(compact_text.strip()) < 20:
        raise ValueError("PDF 中没有足够的可读发票文本。")

    warnings: list[InvoiceWarning] = []
    invoice_match = re.search(r"(?<!\d)(\d{20})(?!\d)", compact_text)
    invoice_no = invoice_match.group(1) if invoice_match else ""
    if not invoice_no:
        _warning(warnings, "MISSING_INVOICE_NUMBER", "未识别到20位发票号码。", "invoice_no")

    date_match = re.search(r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日", compact_text)
    invoice_date = ""
    if date_match:
        year, month, day = map(int, date_match.groups())
        invoice_date = f"{year:04d}-{month:02d}-{day:02d}"
    else:
        _warning(warnings, "MISSING_INVOICE_DATE", "未识别到开票日期。", "invoice_date")

    tax_candidates = []
    for line in lines:
        candidate = _normalise_tax_id(line)
        if re.fullmatch(r"[0-9A-Z]{18}", candidate) and candidate not in tax_candidates:
            tax_candidates.append(candidate)
    buyer_tax_id = tax_candidates[0] if tax_candidates else ""
    seller_tax_id = tax_candidates[1] if len(tax_candidates) > 1 else ""
    if not buyer_tax_id:
        _warning(warnings, "MISSING_BUYER_TAX_ID", "未识别到购买方税号。", "buyer_tax_id")
    if not seller_tax_id:
        _warning(warnings, "MISSING_SELLER_TAX_ID", "未识别到销售方税号。", "seller_tax_id")
    buyer_name = _previous_meaningful_line(lines, buyer_tax_id) if buyer_tax_id else ""
    seller_name = _previous_meaningful_line(lines, seller_tax_id) if seller_tax_id else ""
    if not buyer_name:
        _warning(warnings, "MISSING_BUYER_NAME", "未识别到购买方名称。", "buyer_name")
    if not seller_name:
        _warning(warnings, "MISSING_SELLER_NAME", "未识别到销售方名称。", "seller_name")

    money_values = [
        Decimal(value) for value in re.findall(r"¥\s*(-?[0-9]+(?:\.[0-9]+)?)", compact_text)
    ]
    amount_without_tax = money_values[0] if len(money_values) >= 1 else Decimal("0.00")
    tax_amount = money_values[1] if len(money_values) >= 2 else Decimal("0.00")
    total_amount = money_values[-1] if len(money_values) >= 3 else Decimal("0.00")
    if len(money_values) < 3:
        _warning(warnings, "MISSING_TOTALS", f"发票级金额字段不足，当前识别到：{money_values}", "totals")

    if not items:
        fallback_descriptions = [line for line in lines if "*" in line and not line.startswith("注")]
        items = [InvoiceItem(description=description, raw_text=description) for description in fallback_descriptions]
        _warning(warnings, "ITEM_TABLE_UNCERTAIN", "未能可靠解析发票明细表，请人工核对。", "items")
    if len(items) > 1:
        _warning(warnings, "MULTIPLE_ITEMS", f"检测到 {len(items)} 条发票明细，请人工确认。", "items")
    if any(item.is_negative for item in items):
        _warning(warnings, "NEGATIVE_ITEM", "检测到负数折扣或调整明细，请人工确认。", "items")

    tolerance = Decimal("0.02")
    item_amounts = [item.amount for item in items if item.amount is not None]
    item_taxes = [item.tax_amount for item in items if item.tax_amount is not None]
    if items and len(item_amounts) == len(items) and abs(sum(item_amounts, Decimal("0")) - amount_without_tax) > tolerance:
        _warning(warnings, "ITEM_AMOUNT_MISMATCH", "明细金额合计与发票不含税金额不一致。", "amount_without_tax")
    if items and len(item_taxes) == len(items) and abs(sum(item_taxes, Decimal("0")) - tax_amount) > tolerance:
        _warning(warnings, "ITEM_TAX_MISMATCH", "明细税额合计与发票税额不一致。", "tax_amount")
    if len(money_values) >= 3 and abs(amount_without_tax + tax_amount - total_amount) > tolerance:
        _warning(warnings, "INVOICE_TOTAL_MISMATCH", "不含税金额加税额与价税合计不一致。", "total_amount")

    positive_items = [item for item in items if not item.is_negative and item.description]
    name_source = positive_items or [item for item in items if item.description]
    distinct_names = list(dict.fromkeys(item.description for item in name_source))
    item_name = "；".join(distinct_names)
    short_name = _shorten_item(distinct_names[0]) if distinct_names else ""
    if not item_name:
        _warning(warnings, "MISSING_ITEM_NAME", "未识别到发票项目名称。", "item_name")
    if not short_name:
        _warning(warnings, "MISSING_SHORT_NAME", "无法自动生成统计表简称。", "short_name")

    primary_item = positive_items[0] if positive_items else (items[0] if items else InvoiceItem())
    tax_rate = primary_item.tax_rate
    quantity = primary_item.quantity if primary_item.quantity is not None else Decimal("1")
    category_guess = _guess_category(item_name, seller_name)
    status = InvoiceStatus.REVIEW_REQUIRED if warnings else InvoiceStatus.PARSED_OK

    return InvoiceData(
        source_pdf=str(pdf_path),
        invoice_no=invoice_no,
        invoice_date=invoice_date,
        buyer_name=buyer_name,
        buyer_tax_id=buyer_tax_id,
        seller_name=seller_name,
        seller_tax_id=seller_tax_id,
        item_name=item_name,
        short_name=short_name,
        specification=primary_item.specification or "-",
        unit_raw=primary_item.unit,
        unit_for_inventory=UNIT_MAP.get(primary_item.unit.lower(), primary_item.unit or "-"),
        quantity=quantity,
        unit_price=primary_item.unit_price,
        amount_without_tax=amount_without_tax.quantize(Decimal("0.01")),
        tax_amount=tax_amount.quantize(Decimal("0.01")),
        total_amount=total_amount.quantize(Decimal("0.01")),
        tax_rate=tax_rate,
        category_guess=category_guess,
        items=items,
        status=status,
        warnings=warnings,
        raw_text=text,
    )
