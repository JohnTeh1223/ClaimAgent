# AGENTS.md — Reimbursement Agent repository instructions

## 1. Purpose

This repository is a Windows-local reimbursement assistant for Zhejiang University lab reimbursement workflows.

The software reads invoice PDFs, validates mandatory invoice fields, prepares a reimbursement preview, and only after explicit human confirmation writes to the user's WPS-synced local files.

This is a financial-document workflow. Correctness, traceability, rollback, and human confirmation are more important than autonomy.

## 2. Read before changing code

Before making changes, read these files in order:

1. `others/PROJECT_CONTEXT.md` — business rules, current workflow, data mapping, roadmap.
2. `README.md` — current behavior and usage.
3. `config.json` — local paths and expected buyer information.
4. `code/invoice_parser.py` — PDF field extraction and category suggestion.
5. `code/wps_backend.py` — WPS COM writes, backup, rollback, and opening spreadsheets.
6. `code/main.py` — legacy command-line interaction and confirmation flow.
7. `code/batch_workflow.py` and `code/gui.py` — current batch workflow and GUI.

Do not start coding based only on a single task prompt. Preserve the existing business rules unless the user explicitly changes them.

## 3. Non-negotiable safety rules

### Human confirmation

- NEVER modify official WPS files before explicit user confirmation.
- NEVER copy/rename a PDF into the official reimbursement folder before confirmation.
- A preview must show all important extracted and derived values before the write step.
- If any mandatory field is ambiguous, missing, inconsistent, or invalid, stop and ask the user instead of guessing.

### Invoice compliance

Current mandatory buyer validation:

- Buyer name: `浙江大学`
- Buyer unified social credit code / tax ID: `12100000470095016Q`

If either check fails, do not write to WPS.

Always verify, at minimum:

- invoice number
- invoice date
- buyer name
- buyer tax ID
- seller name
- item description
- amount excluding tax
- tax amount
- total amount
- arithmetic consistency: amount excluding tax + tax = total, allowing ordinary 2-decimal rounding

### File naming

The reimbursement PDF naming convention is:

`金额+名称.pdf`

Example:

`16.49无线发射模块.pdf`

The filename's short name may differ from the full invoice item description. Preserve both values in the internal data model.

### Backups and rollback

- Before modifying any reimbursement spreadsheet, create a timestamped backup.
- If any part of the official write sequence fails, restore all already-modified spreadsheet files where possible.
- Do not silently swallow exceptions.
- Do not overwrite an existing target PDF. Treat an existing same-name target as a possible duplicate reimbursement and stop.

## 4. WPS requirements

The workflow uses the local WPS Enterprise Cloud Drive sync directory. It is not necessary to upload through the WPS website.

Current configured root:

`C:\Users\user\Documents\wps离线\1643291118\WPS企业云盘\浙江大学\团队文档\课题组共享\报销\TEH MU HAN`

Do not hard-code this path in Python source. Keep it configurable through `config.json` or a future settings UI.

The program must NOT depend on Microsoft Excel.

Current spreadsheet automation route:

- WPS Spreadsheet COM automation
- ProgID: `KET.Application` / `ket.Application`
- Python package: `pywin32`

After a successful write, opening the modified `.xlsx` files in WPS for manual review is desirable.

If WPS COM is unavailable on a particular machine, do not automatically replace it with Microsoft Excel automation. Investigate a WPS-compatible fallback and clearly report the limitation.

## 5. Current spreadsheet mapping

### `待报销统计 -郑沐涵.xlsx`

Current category-to-column mapping in `wps_backend.py` is authoritative unless the spreadsheet structure is re-verified:

- 差旅: E:G
- 酬金: H:J
- 设备资产: K:M
- 市内交通: N:P
- 材料费: Q:S
- 运费: T:V
- 印刷费: W:Y
- 其他: Z:AB

For each category, the three values are conceptually:

`名称 | 金额 | 备注/日期`

The current V1 searches for the first blank row starting at row 74.

Do not change these positions casually. Any refactor should first verify the actual workbook structure using a safe test copy.

### `入库单-20260602-郑沐涵.xlsx`

Only material expenses (`材料费`) are currently written to the inventory form.

Current write behavior:

- find the row containing `金额合计`
- insert a row immediately above it
- preserve prior row formatting when possible
- write item information
- refresh the subtotal formula

Current columns:

- A: sequence number
- B: item name
- C: specification/model
- D: unit
- E: quantity
- F: amount excluding tax
- G: tax
- H: subtotal
- I: remark

For the inventory form, prefer the full invoice item description rather than the shortened reimbursement name.

## 6. Current invoice parsing behavior

V1 uses PyMuPDF (`fitz`) to read PDF text directly.

Do not add OCR as the default path. OCR should only be a fallback for scanned/image-only invoices because direct PDF text extraction is more reliable when available.

Multi-item and negative/discount rows must be preserved when readable. Mark uncertain structures as `REVIEW_REQUIRED`, expose them for human correction, and continue to block WPS writes until deterministic validation and explicit confirmation pass.

Category suggestions are heuristics only. The user must be able to correct the category before writing.

## 7. Current user workflow

Current manual-start workflow:

1. User places one or more PDF invoices in the configured `01-待处理` directory, or passes a PDF path to `code/main.py`.
2. User manually starts the program using `run.bat` (GUI) or `python code/main.py` (legacy CLI).
3. Program parses and validates a selected invoice.
4. Program shows/asks for editable reimbursement fields.
5. Program displays a full preview.
6. Only after explicit confirmation does the program modify WPS files and copy the renamed PDF.
7. Modified spreadsheets are opened in WPS for review.

IMPORTANT: do not add automatic folder watching or Windows startup/background services unless the user later explicitly asks for them.

## 8. Approved next-stage roadmap

The next-stage work should focus on usability and batch handling, while retaining manual program startup.

Priority tasks:

1. Replace or supplement the command-line confirmation flow with a simple Windows GUI.
2. Support multiple invoices in one session as a review queue.
3. Allow the user to edit category, short name, specification/model, unit, quantity, and reimbursement month before final submission.
4. Show a consolidated batch summary and require one explicit final confirmation before any official WPS write.
5. Sort each confirmed batch by invoice date ascending before writing WPS rows; do not use upload order.
6. After successful processing, move source PDFs out of `待处理` into a processed/completed directory.
7. On parse/validation failure, preserve the source and move/copy it into a clearly identified failed/review directory with an error reason.
8. Add duplicate detection beyond filename collision where practical, preferably using invoice number + seller + total amount.
9. Add structured processing logs so each write can be audited.
10. Later: support adding the purchaser's electronic signature to eligible electronic invoices.
11. Later: support matching online-order screenshots to invoices by amount and other available signals.
12. Later: deliberately design and test multi-item invoice support.

Explicitly out of scope for the current requested roadmap:

- watchdog / automatic folder monitoring
- start-on-boot behavior
- always-running background service

## 9. Coding principles

- Prefer small, testable functions.
- Keep parsing, business rules, UI, and WPS I/O separated.
- Use `pathlib.Path` for filesystem paths.
- Use dataclasses or typed models for invoice and write-plan data.
- Avoid magic spreadsheet coordinates outside a dedicated mapping/config layer.
- Never let an LLM response directly trigger a financial write. AI/heuristics may suggest values; deterministic validation + human confirmation control the write.
- Preserve Chinese field/category names used in the actual reimbursement documents.
- Avoid destructive migrations of user files.

## 10. Testing expectations

Do not test write operations against the user's only official copy.

Use a copied test directory first.

At minimum, test:

- valid single-item invoice
- invalid buyer name
- invalid buyer tax ID
- total arithmetic mismatch
- duplicate target PDF
- WPS COM startup failure
- spreadsheet file missing
- material expense writes both spreadsheets
- non-material expense writes only reimbursement statistics
- write failure after backup restores original spreadsheets
- user cancels before confirmation: zero official changes

After modifying spreadsheet logic, manually open the resulting files in WPS and inspect formatting, formulas, inserted rows, and totals.

## 11. Definition of done for a change

A change is not done merely because the code runs.

For any feature that can write official reimbursement files, completion requires:

- no write before confirmation
- valid backup creation
- rollback behavior checked
- duplicate protection retained
- output opened/readable in WPS
- no Microsoft Excel dependency introduced
- existing reimbursement rules preserved
- clear error message for failure cases
