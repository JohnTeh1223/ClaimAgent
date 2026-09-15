from __future__ import annotations

import copy
import tkinter as tk
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tkinter import messagebox, ttk

from batch_workflow import (
    CATEGORIES,
    ReviewItem,
    execute_batch,
    load_config,
    preflight_batch,
    route_to_review,
    scan_input_directory,
    sort_review_items_by_date,
    summarize,
    validate_review_item,
    write_review_metadata,
)
from invoice_parser import InvoiceItem, InvoiceStatus


class ReimbursementApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("浙江大学报销助手 — 批量复核")
        self.geometry("1320x820")
        self.minsize(1080, 700)
        self.config_data = load_config()
        self.items: list[ReviewItem] = []
        self.current_index: int | None = None
        self._build_ui()
        self.after(50, self.reload_queue)

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self, padding=10)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="重新扫描待处理目录", command=self.reload_queue).pack(side="left")
        ttk.Button(toolbar, text="保存当前修改", command=self.save_current).pack(side="left", padx=8)
        ttk.Button(toolbar, text="将当前文件移至待复核", command=self.move_current_to_review).pack(side="left")
        ttk.Button(toolbar, text="批次汇总并确认写入", command=self.confirm_batch).pack(side="right")

        self.status_var = tk.StringVar(value="准备扫描……")
        ttk.Label(toolbar, textvariable=self.status_var).pack(side="right", padx=18)

        pane = ttk.Panedwindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        queue_frame = ttk.Labelframe(pane, text="复核队列", padding=6)
        form_frame = ttk.Labelframe(pane, text="发票详情与可编辑报销字段", padding=10)
        pane.add(queue_frame, weight=3)
        pane.add(form_frame, weight=4)

        columns = ("include", "file", "seller", "amount", "category", "status")
        self.tree = ttk.Treeview(queue_frame, columns=columns, show="headings", selectmode="browse")
        headings = {
            "include": "纳入",
            "file": "源文件",
            "seller": "销售方",
            "amount": "合计",
            "category": "类别",
            "status": "状态",
        }
        widths = {"include": 45, "file": 190, "seller": 180, "amount": 70, "category": 75, "status": 110}
        for key in columns:
            self.tree.heading(key, text=headings[key])
            self.tree.column(key, width=widths[key], anchor="w")
        scroll = ttk.Scrollbar(queue_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._select_item)

        self.vars = {name: tk.StringVar() for name in (
            "source", "invoice_no", "invoice_date", "buyer", "buyer_tax_id", "seller", "seller_tax_id",
            "item_name", "amount_without_tax", "tax", "total", "category", "short_name",
            "inventory_name", "specification", "unit", "quantity", "month", "remark", "target",
        )}
        self.field_entries: dict[str, ttk.Entry] = {}
        style = ttk.Style(self)
        style.configure("Warning.TEntry", fieldbackground="#fff4cc")

        row = 0
        ttk.Label(form_frame, text="源文件").grid(row=row, column=0, sticky="e", padx=5, pady=3)
        source_entry = ttk.Entry(form_frame, textvariable=self.vars["source"], state="readonly")
        source_entry.grid(row=row, column=1, columnspan=3, sticky="ew", padx=5, pady=3)
        row += 1

        invoice_fields = [
            ("发票号码", "invoice_no"), ("开票日期", "invoice_date"),
            ("购买方", "buyer"), ("购买方税号", "buyer_tax_id"),
            ("销售方", "seller"), ("销售方税号", "seller_tax_id"),
            ("发票项目汇总", "item_name"), ("不含税金额", "amount_without_tax"),
            ("税额", "tax"), ("价税合计", "total"),
        ]
        for index in range(0, len(invoice_fields), 2):
            for pair_index, (label, key) in enumerate(invoice_fields[index:index + 2]):
                column = pair_index * 2
                ttk.Label(form_frame, text=label).grid(row=row, column=column, sticky="e", padx=5, pady=3)
                entry = ttk.Entry(form_frame, textvariable=self.vars[key], width=30)
                entry.grid(row=row, column=column + 1, sticky="ew", padx=5, pady=3)
                self.field_entries[key] = entry
            row += 1

        ttk.Separator(form_frame).grid(row=row, column=0, columnspan=4, sticky="ew", pady=7)
        row += 1
        reimbursement_fields = [
            ("统计表简称 / PDF名称", "short_name"), ("入库品名", "inventory_name"),
            ("型号/规格", "specification"), ("入库单位", "unit"),
            ("数量", "quantity"), ("报销月份 (YYYYMM)", "month"),
            ("备注", "remark"),
        ]
        ttk.Label(form_frame, text="报销类别").grid(row=row, column=0, sticky="e", padx=5, pady=3)
        ttk.Combobox(
            form_frame, textvariable=self.vars["category"], values=CATEGORIES, state="readonly", width=27
        ).grid(row=row, column=1, sticky="ew", padx=5, pady=3)
        label, key = reimbursement_fields[0]
        ttk.Label(form_frame, text=label).grid(row=row, column=2, sticky="e", padx=5, pady=3)
        self.field_entries[key] = ttk.Entry(form_frame, textvariable=self.vars[key])
        self.field_entries[key].grid(row=row, column=3, sticky="ew", padx=5, pady=3)
        row += 1
        for index in range(1, len(reimbursement_fields), 2):
            for pair_index, (label, key) in enumerate(reimbursement_fields[index:index + 2]):
                column = pair_index * 2
                ttk.Label(form_frame, text=label).grid(row=row, column=column, sticky="e", padx=5, pady=3)
                entry = ttk.Entry(form_frame, textvariable=self.vars[key])
                entry.grid(row=row, column=column + 1, sticky="ew", padx=5, pady=3)
                self.field_entries[key] = entry
            row += 1

        self.item_button_var = tk.StringVar(value="查看/编辑发票明细")
        ttk.Button(form_frame, textvariable=self.item_button_var, command=self.edit_invoice_items).grid(
            row=row, column=0, columnspan=4, sticky="ew", padx=5, pady=6
        )
        row += 1

        self.include_var = tk.BooleanVar(value=True)
        self.review_var = tk.BooleanVar(value=False)
        self.acknowledged_var = tk.BooleanVar(value=False)
        flags = ttk.Frame(form_frame)
        flags.grid(row=row, column=0, columnspan=4, sticky="w", pady=6)
        ttk.Checkbutton(flags, text="纳入本次批量写入", variable=self.include_var).pack(side="left")
        ttk.Checkbutton(flags, text="标记为待复核（不会写入）", variable=self.review_var).pack(side="left", padx=15)
        ttk.Checkbutton(flags, text="我已人工核对异常字段和明细", variable=self.acknowledged_var).pack(side="left")
        row += 1

        ttk.Label(form_frame, text="目标 PDF").grid(row=row, column=0, sticky="ne", padx=5, pady=3)
        ttk.Entry(form_frame, textvariable=self.vars["target"], state="readonly", width=58).grid(
            row=row, column=1, columnspan=3, sticky="ew", padx=5, pady=3
        )
        row += 1
        ttk.Label(form_frame, text="校验信息").grid(row=row, column=0, sticky="ne", padx=5, pady=3)
        self.validation_text = tk.Text(form_frame, height=5, wrap="word", state="disabled")
        self.validation_text.grid(row=row, column=1, columnspan=3, sticky="nsew", padx=5, pady=3)
        form_frame.columnconfigure(1, weight=1)
        form_frame.columnconfigure(3, weight=1)
        form_frame.rowconfigure(row, weight=1)

    def reload_queue(self) -> None:
        if self.current_index is not None:
            self.save_current(show_message=False)
        try:
            scan = scan_input_directory(self.config_data)
        except Exception as exc:
            messagebox.showerror("扫描失败", str(exc), parent=self)
            return
        self.items = scan.items
        self.current_index = None
        self._refresh_tree()
        message = f"待复核队列 {len(scan.items)} 张"
        if scan.failures:
            message += f"；解析失败 {len(scan.failures)} 张，已移入待复核目录"
        self.status_var.set(message)
        if self.items:
            first = self.tree.get_children()[0]
            self.tree.selection_set(first)
            self.tree.focus(first)
            self._select_item()
        else:
            self._clear_form()

    def _refresh_tree(self) -> None:
        selected = self.current_index
        self.tree.delete(*self.tree.get_children())
        for index, item in enumerate(self.items):
            item.errors = validate_review_item(item, self.config_data)
            if item.needs_review:
                status = "待复核"
            elif item.errors:
                status = "校验失败"
            elif item.invoice.warnings:
                status = "已人工确认"
            else:
                status = "可写入"
            self.tree.insert(
                "", "end", iid=str(index),
                values=("是" if item.included else "否", item.source_path.name, item.invoice.seller_name,
                        f"¥{item.invoice.total_amount:.2f}", item.category, status),
            )
        if selected is not None and str(selected) in self.tree.get_children():
            self.tree.selection_set(str(selected))

    def _update_tree_row(self, index: int) -> None:
        item = self.items[index]
        if item.needs_review:
            status = "待复核"
        elif item.errors:
            status = "校验失败"
        elif item.invoice.warnings:
            status = "已人工确认"
        else:
            status = "可写入"
        if self.tree.exists(str(index)):
            self.tree.item(
                str(index),
                values=("是" if item.included else "否", item.source_path.name, item.invoice.seller_name,
                        f"¥{item.invoice.total_amount:.2f}", item.category, status),
            )

    def _select_item(self, _event=None) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        new_index = int(selected[0])
        if self.current_index is not None and self.current_index != new_index:
            self.save_current(show_message=False)
        self.current_index = new_index
        self._load_form(self.items[new_index])

    def _load_form(self, item: ReviewItem) -> None:
        inv = item.invoice
        values = {
            "source": str(item.source_path), "invoice_no": inv.invoice_no, "invoice_date": inv.invoice_date,
            "buyer": inv.buyer_name, "buyer_tax_id": inv.buyer_tax_id, "seller": inv.seller_name,
            "seller_tax_id": inv.seller_tax_id,
            "item_name": inv.item_name, "amount_without_tax": f"{inv.amount_without_tax:.2f}",
            "tax": f"{inv.tax_amount:.2f}", "total": f"{inv.total_amount:.2f}",
            "category": item.category, "short_name": item.short_name, "specification": item.specification,
            "unit": item.unit, "quantity": f"{item.quantity:g}", "month": item.reimbursement_month,
            "inventory_name": item.inventory_name, "remark": item.remark,
            "target": f"{item.reimbursement_month}/{item.target_filename}",
        }
        for key, value in values.items():
            self.vars[key].set(value)
        self.include_var.set(item.included)
        self.review_var.set(item.needs_review)
        self.acknowledged_var.set(item.review_acknowledged)
        item_warning = any(warning.field == "items" for warning in inv.warnings) and not item.review_acknowledged
        prefix = "⚠ " if item_warning else ""
        self.item_button_var.set(f"{prefix}查看/编辑发票明细（{len(inv.items)} 条）")
        self._show_item_messages(item)
        self._apply_warning_styles(item)

    def _clear_form(self) -> None:
        for var in self.vars.values():
            var.set("")
        self._show_errors([])

    def _show_errors(self, errors: list[str]) -> None:
        self.validation_text.configure(state="normal")
        self.validation_text.delete("1.0", "end")
        self.validation_text.insert("1.0", "\n".join(f"• {error}" for error in errors) if errors else "✓ 校验通过")
        self.validation_text.configure(state="disabled")

    def _show_item_messages(self, item: ReviewItem) -> None:
        messages = [f"❌ {error}" for error in item.errors]
        if item.invoice.warnings and item.review_acknowledged:
            messages.extend(f"⚠ 已人工确认：{warning.message}" for warning in item.invoice.warnings)
        if not messages:
            messages = ["✓ 校验通过"]
        self.validation_text.configure(state="normal")
        self.validation_text.delete("1.0", "end")
        self.validation_text.insert("1.0", "\n".join(messages))
        self.validation_text.configure(state="disabled")

    def _apply_warning_styles(self, item: ReviewItem) -> None:
        warning_fields = {warning.field for warning in item.invoice.warnings if warning.field}
        aliases = {
            "buyer": "buyer_name", "seller": "seller_name",
            "tax": "tax_amount", "total": "total_amount",
        }
        for key, entry in self.field_entries.items():
            warning_key = aliases.get(key, key)
            entry.configure(style="Warning.TEntry" if warning_key in warning_fields else "TEntry")

    @staticmethod
    def _optional_decimal(text: str) -> Decimal | None:
        value = text.strip()
        if not value:
            return None
        try:
            return Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"不是有效数字：{text}") from exc

    def edit_invoice_items(self) -> None:
        if self.current_index is None:
            return
        item = self.items[self.current_index]
        working: list[InvoiceItem] = copy.deepcopy(item.invoice.items)
        dialog = tk.Toplevel(self)
        dialog.title(f"发票明细复核 — {item.source_path.name}")
        dialog.geometry("980x610")
        dialog.transient(self)
        dialog.grab_set()

        columns = ("description", "quantity", "amount", "tax", "type")
        tree = ttk.Treeview(dialog, columns=columns, show="headings", height=9)
        for key, label, width in (
            ("description", "名称", 500), ("quantity", "数量", 80),
            ("amount", "不含税金额", 100), ("tax", "税额", 90), ("type", "类型", 70),
        ):
            tree.heading(key, text=label)
            tree.column(key, width=width, anchor="w")
        tree.pack(fill="x", padx=10, pady=10)

        editor = ttk.Labelframe(dialog, text="选中明细（均可修改）", padding=8)
        editor.pack(fill="both", expand=True, padx=10)
        detail_vars = {key: tk.StringVar() for key in (
            "description", "specification", "unit", "quantity", "unit_price",
            "amount", "tax_rate", "tax_amount",
        )}
        detail_fields = [
            ("名称", "description"), ("规格型号", "specification"),
            ("单位", "unit"), ("数量", "quantity"),
            ("单价", "unit_price"), ("不含税金额", "amount"),
            ("税率", "tax_rate"), ("税额", "tax_amount"),
        ]
        for index, (label, key) in enumerate(detail_fields):
            row, pair = divmod(index, 2)
            ttk.Label(editor, text=label).grid(row=row, column=pair * 2, sticky="e", padx=4, pady=4)
            ttk.Entry(editor, textvariable=detail_vars[key], width=42).grid(
                row=row, column=pair * 2 + 1, sticky="ew", padx=4, pady=4
            )
        editor.columnconfigure(1, weight=1)
        editor.columnconfigure(3, weight=1)
        selected_index: list[int | None] = [None]

        def refresh_tree(select_index: int | None = None) -> None:
            tree.delete(*tree.get_children())
            for index, detail in enumerate(working):
                tree.insert("", "end", iid=str(index), values=(
                    detail.description,
                    "" if detail.quantity is None else str(detail.quantity),
                    "" if detail.amount is None else str(detail.amount),
                    "" if detail.tax_amount is None else str(detail.tax_amount),
                    "折扣/负数" if detail.is_negative else "普通",
                ))
            if select_index is not None and tree.exists(str(select_index)):
                tree.selection_set(str(select_index))
                tree.focus(str(select_index))

        def save_selected() -> bool:
            index = selected_index[0]
            if index is None or index >= len(working):
                return True
            try:
                detail = working[index]
                detail.description = detail_vars["description"].get().strip()
                detail.specification = detail_vars["specification"].get().strip()
                detail.unit = detail_vars["unit"].get().strip()
                detail.quantity = self._optional_decimal(detail_vars["quantity"].get())
                detail.unit_price = self._optional_decimal(detail_vars["unit_price"].get())
                detail.amount = self._optional_decimal(detail_vars["amount"].get())
                detail.tax_rate = detail_vars["tax_rate"].get().strip()
                detail.tax_amount = self._optional_decimal(detail_vars["tax_amount"].get())
                detail.is_negative = (
                    detail.amount is not None and detail.amount < 0
                ) or (detail.tax_amount is not None and detail.tax_amount < 0)
            except ValueError as exc:
                messagebox.showerror("明细数字无效", str(exc), parent=dialog)
                return False
            refresh_tree(index)
            return True

        def load_selected(_event=None) -> None:
            selection = tree.selection()
            if not selection:
                return
            new_index = int(selection[0])
            if selected_index[0] is not None and selected_index[0] != new_index and not save_selected():
                return
            selected_index[0] = new_index
            detail = working[new_index]
            values = {
                "description": detail.description, "specification": detail.specification,
                "unit": detail.unit, "quantity": "" if detail.quantity is None else str(detail.quantity),
                "unit_price": "" if detail.unit_price is None else str(detail.unit_price),
                "amount": "" if detail.amount is None else str(detail.amount), "tax_rate": detail.tax_rate,
                "tax_amount": "" if detail.tax_amount is None else str(detail.tax_amount),
            }
            for key, value in values.items():
                detail_vars[key].set(value)

        def add_detail() -> None:
            if not save_selected():
                return
            working.append(InvoiceItem())
            selected_index[0] = len(working) - 1
            refresh_tree(selected_index[0])
            load_selected()

        def delete_detail() -> None:
            index = selected_index[0]
            if index is None:
                return
            working.pop(index)
            selected_index[0] = None
            for variable in detail_vars.values():
                variable.set("")
            refresh_tree(0 if working else None)
            if working:
                load_selected()

        tree.bind("<<TreeviewSelect>>", load_selected)
        buttons = ttk.Frame(dialog, padding=10)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="添加明细", command=add_detail).pack(side="left")
        ttk.Button(buttons, text="删除选中明细", command=delete_detail).pack(side="left", padx=8)
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="right")

        def commit_details() -> None:
            if not save_selected():
                return
            item.invoice.items = working
            self.item_button_var.set(f"查看/编辑发票明细（{len(working)} 条）")
            dialog.destroy()
            self.save_current(show_message=False)

        ttk.Button(buttons, text="保存全部明细", command=commit_details).pack(side="right", padx=8)
        refresh_tree(0 if working else None)
        if working:
            load_selected()

    def save_current(self, *, show_message: bool = True) -> bool:
        if self.current_index is None:
            return True
        item = self.items[self.current_index]
        input_errors: list[str] = []
        try:
            quantity = Decimal(self.vars["quantity"].get().strip())
        except InvalidOperation:
            quantity = Decimal("0")
            input_errors.append("数量不是有效数字。")
        invoice = item.invoice
        invoice.invoice_no = self.vars["invoice_no"].get().strip()
        invoice.invoice_date = self.vars["invoice_date"].get().strip()
        invoice.buyer_name = self.vars["buyer"].get().strip()
        invoice.buyer_tax_id = self.vars["buyer_tax_id"].get().strip()
        invoice.seller_name = self.vars["seller"].get().strip()
        invoice.seller_tax_id = self.vars["seller_tax_id"].get().strip()
        invoice.item_name = self.vars["item_name"].get().strip()
        for key, attribute, label in (
            ("amount_without_tax", "amount_without_tax", "不含税金额"),
            ("tax", "tax_amount", "税额"),
            ("total", "total_amount", "价税合计"),
        ):
            try:
                setattr(invoice, attribute, Decimal(self.vars[key].get().strip()).quantize(Decimal("0.01")))
            except InvalidOperation:
                setattr(invoice, attribute, Decimal("0.00"))
                input_errors.append(f"{label}不是有效金额。")
        item.category = self.vars["category"].get().strip()
        item.short_name = self.vars["short_name"].get().strip()
        item.inventory_name = self.vars["inventory_name"].get().strip()
        item.specification = self.vars["specification"].get().strip()
        item.unit = self.vars["unit"].get().strip()
        item.quantity = quantity
        item.reimbursement_month = self.vars["month"].get().strip()
        item.remark = self.vars["remark"].get().strip()
        item.included = self.include_var.get()
        item.needs_review = self.review_var.get()
        item.review_acknowledged = self.acknowledged_var.get()
        invoice.short_name = item.short_name
        invoice.specification = item.specification
        invoice.unit_for_inventory = item.unit
        invoice.quantity = item.quantity
        if item.review_acknowledged and invoice.warnings:
            item.needs_review = False
            self.review_var.set(False)
        if item.needs_review:
            item.included = False
            self.include_var.set(False)
        item.errors = input_errors + validate_review_item(item, self.config_data)
        if item.errors:
            item.needs_review = True
            item.included = False
            self.review_var.set(True)
            self.include_var.set(False)
            invoice.status = InvoiceStatus.REVIEW_REQUIRED
        elif not self.review_var.get():
            item.needs_review = False
            invoice.status = InvoiceStatus.PARSED_OK
        if invoice.warnings or item.needs_review:
            try:
                write_review_metadata(invoice, self.config_data, item)
            except Exception as exc:
                item.errors.append(f"无法保存待复核元数据：{exc}")
                item.needs_review = True
                item.included = False
        self.vars["target"].set(f"{item.reimbursement_month}/{item.target_filename}")
        self._show_item_messages(item)
        self._apply_warning_styles(item)
        self._update_tree_row(self.current_index)
        if show_message:
            self.status_var.set("当前修改已保存在本次复核会话中（尚未写入任何 WPS 文件）")
        return not item.errors

    def move_current_to_review(self) -> None:
        if self.current_index is None:
            return
        self.save_current(show_message=False)
        item = self.items[self.current_index]
        reason = "用户在 GUI 中标记为待复核"
        if item.errors:
            reason += "：" + "；".join(item.errors)
        if not messagebox.askyesno(
            "移至待复核", f"将 {item.source_path.name} 移出待处理目录并放入待复核目录？\n\n{reason}", parent=self
        ):
            return
        try:
            target = route_to_review(item.source_path, self.config_data, reason, move=True)
        except Exception as exc:
            messagebox.showerror("移动失败", str(exc), parent=self)
            return
        self.items.pop(self.current_index)
        self.current_index = None
        self._refresh_tree()
        self._clear_form()
        self.status_var.set(f"已移至待复核：{target}")

    def confirm_batch(self) -> None:
        self.save_current(show_message=False)
        active = sort_review_items_by_date(
            item for item in self.items if item.included and not item.needs_review
        )
        if not active:
            messagebox.showwarning("没有可写入项目", "请先勾选至少一张通过校验的发票。", parent=self)
            return
        errors = preflight_batch(active, self.config_data)
        if errors:
            detail = "\n".join(f"{Path(key).name}:\n  - " + "\n  - ".join(value) for key, value in errors.items())
            messagebox.showerror("最终检查未通过", detail, parent=self)
            return
        summary = summarize(self.items)
        self._show_confirmation_dialog(summary, active)

    def _show_confirmation_dialog(self, summary, active: list[ReviewItem]) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("最终批次确认")
        dialog.geometry("720x560")
        dialog.transient(self)
        dialog.grab_set()
        text = tk.Text(dialog, wrap="word", padx=12, pady=12)
        text.pack(fill="both", expand=True, padx=10, pady=10)
        lines = [
            "报销写入预览（当前尚未修改任何 WPS 文件）",
            "",
            f"发票数量：{summary.invoice_count}",
            f"价税合计：¥{summary.total_amount:.2f}",
            f"其中材料费：{summary.material_count} 张（会同时修改入库单）",
            "",
            "分类汇总：",
        ]
        lines.extend(f"  {category}: ¥{amount:.2f}" for category, amount in summary.totals_by_category.items())
        lines.extend(["", "目标 PDF（也是 WPS 日期升序写入顺序）："])
        for item, target in zip(active, summary.targets):
            destination = "统计表 + 入库单" if item.category == "材料费" else "仅统计表"
            lines.append(f"  {target}  |  {destination}")
        lines.extend([
            "", "确认后将一次性：备份表格、写入整个批次、复制报销 PDF、",
            "再把源 PDF 移至已完成目录并记录结构化日志。任何中途失败都会尝试整体回滚。",
        ])
        text.insert("1.0", "\n".join(lines))
        text.configure(state="disabled")

        accepted = tk.BooleanVar(value=False)
        check = ttk.Checkbutton(dialog, text="我已逐项复核，并明确同意执行上述整个批次", variable=accepted)
        check.pack(anchor="w", padx=18, pady=(0, 8))
        buttons = ttk.Frame(dialog)
        buttons.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="right")

        def commit() -> None:
            if not accepted.get():
                messagebox.showwarning("需要明确确认", "请先勾选确认框。", parent=dialog)
                return
            dialog.destroy()
            self._execute_confirmed_batch(active)

        ttk.Button(buttons, text="确认并执行批次写入", command=commit).pack(side="right", padx=8)

    def _execute_confirmed_batch(self, active: list[ReviewItem]) -> None:
        self.status_var.set("正在执行已确认批次，请勿关闭程序……")
        self.update_idletasks()
        try:
            result = execute_batch(active, self.config_data, confirmed=True)
        except Exception as exc:
            messagebox.showerror("批次失败并已尝试回滚", str(exc), parent=self)
            self.status_var.set("批次失败；请检查错误与处理日志")
            return
        message = (
            f"批次完成：{len(result.completed)} 张发票。\n"
            f"备份：{result.backup_dir}\n日志：{result.log_file}"
        )
        if result.open_warning:
            message += f"\n\n{result.open_warning}"
        messagebox.showinfo("写入完成", message, parent=self)
        self.reload_queue()


def main() -> None:
    app = ReimbursementApp()
    app.mainloop()


if __name__ == "__main__":
    main()
