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
    extracted_parties: dict[str, str] = field(default_factory=dict, repr=False)
    extracted_values: dict = field(default_factory=dict, repr=False)

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
    cleaned = str(text).replace("¥", "").replace("￥", "").replace(",", "").strip()
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


def _parse_parties_from_page(page) -> tuple[str, str, str, str]:
    """Read the two labelled party boxes independently of PDF object order."""
    words = page.get_text("words")
    anchors = {}
    for role, title in (("buyer", "购买方"), ("seller", "销售方")):
        candidates = []
        for word in words:
            token = re.sub(r"\s+", "", str(word[4]))
            if token in (title, title + "信息"):
                candidates.append(word)
            elif token == title[0]:
                # Vertical labels can be stored as five separate PDF objects.
                column = sorted((w for w in words if abs(w[0] - word[0]) < 3
                                 and word[1] <= w[1] <= word[1] + 6 * (word[3] - word[1])),
                                key=lambda w: w[1])
                if "".join(str(w[4]).strip() for w in column).startswith(title):
                    candidates.append(word)
        if len(candidates) == 1:
            anchors[role] = candidates[0]

    # Without both labels the box boundary is uncertain; do not allow a
    # missing buyer value to be filled using the seller's neighbouring box.
    if len(anchors) != 2:
        return "", "", "", ""

    parties = []
    for role in ("buyer", "seller"):
        anchor = anchors.get(role)
        if anchor is None:
            parties.extend(("", ""))
            continue
        left, right = anchor[0], float(page.rect.width)
        top, bottom = anchor[1] - 3, float(page.rect.height)
        for other in anchors.values():
            if other is anchor:
                continue
            if abs(other[1] - anchor[1]) < 10 and other[0] > left:
                right = min(right, other[0])
            elif other[1] > top + 10:
                bottom = min(bottom, other[1] - 3)
        headers = [w[1] for w in words if w[1] > top and
                   re.sub(r"\s+", "", str(w[4])) in ("项目名称", "货物或应税劳务、服务名称")]
        if headers:
            bottom = min(bottom, min(headers))
        # Reconstruct visual rows before matching labels: “名” and “称:”
        # may be separate words, while a tax label and its value may be one.
        rows = []
        box = [w for w in words if left <= w[0] < right and top <= w[1] < bottom
               and not (abs(w[0] - left) < 3 and str(w[4]).strip() in "购买销售方信息")]
        for word in sorted(box, key=lambda w: ((w[1] + w[3]) / 2, w[0])):
            center = (word[1] + word[3]) / 2
            row = next((row for row in rows if abs(row[0] - center) < 5), None)
            if row is None:
                rows.append((center, [word]))
            else:
                row[1].append(word)
        names, tax_ids = [], []
        for _, row in rows:
            text = "".join(str(w[4]).strip() for w in sorted(row, key=lambda w: w[0]))
            if any(abs(w[0] - left) < 3 and str(w[4]).startswith("息统一社会信用代码/") for w in row):
                text = text.removeprefix("息")
            name = re.match(r"^名\s*称\s*[：:]\s*(.*)$", text)
            if name and name.group(1).strip():
                names.append(name.group(1).strip())
            tax = re.fullmatch(r"(?:统一社会信用代码/)?纳税人识别号[：:]([0-9A-Z]{15}|[0-9A-Z]{18}|[0-9A-Z]{20})",
                               _normalise_tax_id(text))
            if tax:
                tax_ids.append(tax.group(1))
        parties.extend((names[0] if len(names) == 1 else "",
                        tax_ids[0] if len(tax_ids) == 1 else ""))
    return tuple(parties)


def _table_columns(page) -> dict[str, tuple[float, float]]:
    """Locate columns from the visible header, including split label words."""
    words = page.get_text("words")
    headers = [w for w in words if w[4] == "项目名称"]
    if not headers:
        return {}
    header = min(headers, key=lambda w: w[1])
    names = {"项目名称", "规格型号", "单位", "数量", "单价", "金额", "税率/征收率", "税额"}
    labels = []
    pending = []
    for word in sorted((w for w in words if abs(w[1] - header[1]) < 3), key=lambda w: w[0]):
        pending.append(word)
        name = "".join(re.sub(r"\s+", "", str(w[4])) for w in pending)
        if name in names:
            labels.append((name, (pending[0][0] + pending[-1][2]) / 2))
            pending = []
    order = [name for name, _ in labels]
    if pending or order not in (
        ["项目名称", "规格型号", "单位", "数量", "单价", "金额", "税率/征收率", "税额"],
        ["项目名称", "单价", "数量", "金额", "税率/征收率", "税额"],
    ):
        return {}
    boundaries = [0.0] + [(a[1] + b[1]) / 2 for a, b in zip(labels, labels[1:])] + [float(page.rect.width) + 1]
    return {name: (boundaries[i], boundaries[i + 1]) for i, (name, _) in enumerate(labels)}


def _column_words(words: list[tuple], bounds: tuple[float, float]) -> list[tuple]:
    # Numeric values are right-aligned; long unit prices can cross the left
    # boundary while still ending in their own column.
    return [w for w in words if bounds[0] <= (w[2] if _decimal(w[4]) is not None else w[0]) < bounds[1]]


def _parse_totals_from_page(page) -> list[Decimal]:
    words = page.get_text("words")
    width = float(page.rect.width)
    total_labels = [w for w in words if w[0] < width * .25 and w[4] in ("合", "合计")]
    small_labels = [w for w in words if "小写" in w[4]]
    if not total_labels or not small_labels:
        return []
    total_y = max(total_labels, key=lambda w: w[1])[1]
    small = max(small_labels, key=lambda w: w[1])
    total_words = [w for w in words if abs(w[1] - total_y) < 6]
    small_words = [w for w in words if abs(w[1] - small[1]) < 6]
    columns = _table_columns(page)
    amount_words = _column_words(total_words, columns.get("金额", (width * .65, width * .76)))
    tax_words = _column_words(total_words, columns.get("税额", (width * .90, width + 1)))
    values = [_first_decimal(amount_words, 0, width),
              _first_decimal(tax_words, 0, width),
              _first_decimal(small_words, small[2] - 5, width)]
    return values if all(v is not None for v in values) else []


def _parse_items_from_page(page) -> list[InvoiceItem]:
    """Parse common Chinese e-invoice detail rows using table geometry.

    The amount column is used as the row anchor, which preserves genuine
    multi-item rows and negative discount/adjustment rows.
    """
    words = page.get_text("words", sort=True)
    if not words:
        return []
    width = float(page.rect.width)
    columns = _table_columns(page)
    header_y_values = [word[1] for word in words if str(word[4]).replace(" ", "") == "项目名称"]
    if not header_y_values:
        return []
    header_y = min(header_y_values)
    description_header = next(w for w in words if w[4] == "项目名称" and w[1] == header_y)
    specification_headers = [w for w in words if w[4] == "规格型号" and abs(w[1] - header_y) < 3]
    description_end = ((description_header[2] + specification_headers[0][0]) / 2
                       if specification_headers else width * .18)
    total_y_values = [
        word[1] for word in words
        if word[1] > header_y + 20 and word[0] < width * 0.25 and str(word[4]).strip() in {"合", "合计"}
    ]
    # Total values are often printed a few points above the visual “合计” label.
    total_y = min(total_y_values) - 5 if total_y_values else max(word[1] for word in words) + 1

    amount_anchors = []
    for word in words:
        if not (header_y < word[1] < total_y):
            continue
        if _decimal(word[4]) is not None and _column_words([word], columns.get("金额", (width * .66, width * .76))):
            amount_anchors.append(word)
    amount_anchors.sort(key=lambda value: (value[1], value[0]))

    items: list[InvoiceItem] = []
    for index, anchor in enumerate(amount_anchors):
        row_start = anchor[1] - 2.5
        row_end = amount_anchors[index + 1][1] - 2.5 if index + 1 < len(amount_anchors) else total_y
        row_words = [word for word in words if row_start <= word[1] < row_end]
        def column(name, fallback):
            bounds = columns.get(name) if columns else fallback
            return _column_words(row_words, bounds) if bounds else []

        description = _text_in_column(column("项目名称", (0, description_end)), 0, width + 1)
        specification = _text_in_column(column("规格型号", (description_end, width * .31)), 0, width + 1)
        unit = _text_in_column(column("单位", (width * .31, width * .42)), 0, width + 1)
        quantity = _first_decimal(column("数量", (width * .42, width * .49)), 0, width + 1)
        unit_price = _first_decimal(column("单价", (width * .49, width * .66)), 0, width + 1)
        amount = _decimal(anchor[4])
        tax_rate = _text_in_column(column("税率/征收率", (width * .76, width * .90)), 0, width + 1)
        tax_amount = _first_decimal(column("税额", (width * .90, width + 1)), 0, width + 1)
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
        parties = _parse_parties_from_page(document[0]) if len(document) else ("", "", "", "")
        printed_totals = []
        items = []
        for page in document:
            items.extend(_parse_items_from_page(page))
            printed_totals = _parse_totals_from_page(page) or printed_totals
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

    buyer_name, buyer_tax_id, seller_name, seller_tax_id = parties
    if not buyer_tax_id:
        _warning(warnings, "MISSING_BUYER_TAX_ID", "未识别到购买方税号。", "buyer_tax_id")
    if not seller_tax_id:
        _warning(warnings, "MISSING_SELLER_TAX_ID", "未识别到销售方税号。", "seller_tax_id")
    if not buyer_name:
        _warning(warnings, "MISSING_BUYER_NAME", "未识别到购买方名称。", "buyer_name")
    if not seller_name:
        _warning(warnings, "MISSING_SELLER_NAME", "未识别到销售方名称。", "seller_name")

    money_values = printed_totals or [
        Decimal(value.replace(",", "")) for value in re.findall(r"[¥￥]\s*(-?[0-9,]+(?:\.[0-9]+)?)", compact_text)
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
    short_name = "、".join(dict.fromkeys(_shorten_item(name) for name in distinct_names))
    if not item_name:
        _warning(warnings, "MISSING_ITEM_NAME", "未识别到发票项目名称。", "item_name")
    if not short_name:
        _warning(warnings, "MISSING_SHORT_NAME", "无法自动生成统计表简称。", "short_name")

    primary_item = positive_items[0] if positive_items else (items[0] if items else InvoiceItem())
    # The inventory currently writes one consolidated row per invoice. Do not
    # assign the first product's quantity/unit to several different products.
    consolidated = len(positive_items) > 1
    tax_rate = primary_item.tax_rate
    quantity = Decimal("1") if consolidated else (primary_item.quantity if primary_item.quantity is not None else Decimal("1"))
    category_guess = _guess_category(item_name, seller_name)
    status = InvoiceStatus.REVIEW_REQUIRED if warnings else InvoiceStatus.PARSED_OK

    invoice = InvoiceData(
        source_pdf=str(pdf_path),
        invoice_no=invoice_no,
        invoice_date=invoice_date,
        buyer_name=buyer_name,
        buyer_tax_id=buyer_tax_id,
        seller_name=seller_name,
        seller_tax_id=seller_tax_id,
        item_name=item_name,
        short_name=short_name,
        specification="详见发票明细" if consolidated else (primary_item.specification or "-"),
        unit_raw=primary_item.unit,
        unit_for_inventory="批" if consolidated else UNIT_MAP.get(primary_item.unit.lower(), primary_item.unit or "-"),
        quantity=quantity,
        unit_price=None if consolidated else primary_item.unit_price,
        amount_without_tax=amount_without_tax.quantize(Decimal("0.01")),
        tax_amount=tax_amount.quantize(Decimal("0.01")),
        total_amount=total_amount.quantize(Decimal("0.01")),
        tax_rate=tax_rate,
        category_guess=category_guess,
        items=items,
        status=status,
        warnings=warnings,
        raw_text=text,
        extracted_parties={
            "buyer_name": parties[0], "buyer_tax_id": parties[1],
            "seller_name": parties[2], "seller_tax_id": parties[3],
        },
    )
    snapshot = invoice.to_dict()
    invoice.extracted_values = {key: snapshot[key] for key in (
        "amount_without_tax", "tax_amount", "total_amount", "items",
    )}
    return invoice
