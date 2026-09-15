# PROJECT_CONTEXT.md — Zhejiang University Reimbursement Agent

## 1. Project goal

Build a Windows-local reimbursement agent that reduces repetitive lab reimbursement work while keeping the user in control of every official write.

The user should be able to put invoice PDFs into a local working directory, manually start the agent, review extracted/derived reimbursement information, correct anything necessary, and then explicitly approve writing to the WPS-synced reimbursement files.

The system is not intended to be a fully autonomous financial bot. Human review before submission is a deliberate product requirement.

## 2. Source reimbursement rules supplied by the user

The lab's current reimbursement instructions establish these rules:

1. Invoice buyer/header must not be an individual. It must be `浙江大学`.
2. Zhejiang University's unified social credit code is `12100000470095016Q`.
3. Invoice files are named using `金额+名称.pdf`.
4. For online purchases (Taobao, JD, etc.), an online-order screenshot is generally required; its amount must correspond to the invoice and its naming convention should match the invoice.
5. Electronic invoices, except special cases such as train tickets, require the purchaser's electronic signature image to be placed on the invoice.
6. Reimbursement materials are stored under the WPS team reimbursement folder; each person has an individual folder.
7. Each reimbursement upload creates/uses a separate `YYYYMM` folder, for example `202501`.
8. The user's `待报销统计–姓名.xlsx` must be updated.
9. Material expenses must additionally be entered into `入库单-姓名.xlsx` based on invoice information.

The current implementation covers only part of this full policy. Signature insertion and online-order screenshot pairing are future features.

## 3. User's current local WPS environment

WPS is already synchronizing the team cloud drive to a normal Windows filesystem directory.

Current reimbursement root:

`C:\Users\user\Documents\wps离线\1643291118\WPS企业云盘\浙江大学\团队文档\课题组共享\报销\TEH MU HAN`

Because this folder is locally synchronized, the application should treat it as a filesystem target rather than automating the WPS website.

Current configured spreadsheet names:

- `待报销统计 -郑沐涵.xlsx`
- `入库单-20260602-郑沐涵.xlsx`

These filenames may change in the future. They must remain configurable.

## 4. Desired interaction model

The desired behavior is manual start, not an always-running service.

Normal use:

```text
User copies PDFs into C:\ClaimAgent\01-待处理
        ↓
User manually starts run.bat / program
        ↓
Agent reads PDFs
        ↓
Agent validates and extracts fields
        ↓
Agent proposes category + short name + spreadsheet entries
        ↓
User reviews/edits everything
        ↓
Agent shows final batch summary
        ↓
User explicitly confirms
        ↓
Only now:
  - backup official xlsx files
  - sort the confirmed batch by invoice date (oldest first)
  - write reimbursement statistics
  - if material expense, write inventory form
  - rename/copy PDF into YYYYMM reimbursement folder
        ↓
Open modified spreadsheets in WPS Office for user review
```

The user specifically does NOT currently want:

- automatic watchdog folder triggers
- start-on-boot
- background always-running agent

Manual start is intentional for now.

## 5. Example invoice used to build V1

A real test invoice supplied by the user has these values:

- invoice type: electronic ordinary invoice
- invoice number: `26444000000227502481`
- invoice date: `2026-08-05`
- buyer: `浙江大学`
- buyer tax ID: `12100000470095016Q`
- seller: `中山宏蓝电子科技有限公司`
- item: `*电子元件*无线发射模块`
- invoice unit: `pcs`
- quantity: `1`
- amount excluding tax: `16.33`
- tax: `0.16`
- total: `16.49`
- tax rate: `1%`

The V1 desired interpretation is:

- reimbursement category: `材料费`
- short reimbursement name: `无线发射模块`
- renamed PDF: `16.49无线发射模块.pdf`
- reimbursement-statistics name: `无线发射模块`
- reimbursement-statistics amount: `16.49`
- reimbursement-statistics note/date: `20260805`
- inventory item name: preserve the full invoice item description, e.g. `*电子元件*无线发射模块`
- inventory amount excluding tax: `16.33`
- inventory tax: `0.16`
- inventory subtotal: `16.49`

This example is a development fixture, not a universal assumption for all invoices.

## 6. Data model concept

Keep invoice source data separate from user-facing reimbursement-derived data.

Suggested conceptual structure:

```text
Invoice source fields
- source_pdf
- invoice_no
- invoice_date
- buyer_name
- buyer_tax_id
- seller_name
- seller_tax_id
- item_name_full
- specification
- unit_raw
- quantity
- unit_price
- amount_without_tax
- tax_amount
- total_amount
- tax_rate

Derived/reviewable reimbursement fields
- short_name
- category
- inventory_unit
- reimbursement_month
- target_filename
- target_folder
```

Derived fields are suggestions and must remain editable before final confirmation.

## 7. Reimbursement categories currently supported

Current category list:

1. `差旅`
2. `酬金`
3. `设备资产`
4. `市内交通`
5. `材料费`
6. `运费`
7. `印刷费`
8. `其他`

The parser currently uses keyword heuristics to suggest a category. These heuristics are not authoritative.

The user must be able to override the suggestion before submission.

## 8. `待报销统计` mapping

Current workbook mapping used by V1:

| Category | Name | Amount | Note/Date |
|---|---:|---:|---:|
| 差旅 | E | F | G |
| 酬金 | H | I | J |
| 设备资产 | K | L | M |
| 市内交通 | N | O | P |
| 材料费 | Q | R | S |
| 运费 | T | U | V |
| 印刷费 | W | X | Y |
| 其他 | Z | AA | AB |

V1 searches for the first blank category-name cell beginning at row 74.

This structure was derived from the user's actual workbook. If the workbook is replaced or reformatted, re-verify these coordinates rather than assuming they remain valid.

## 9. `入库单` mapping

Material expenses require an inventory-form entry.

Current V1 detects the `金额合计` row, inserts a new material row immediately above it, and attempts to copy the previous row's formatting.

Current fields:

| Column | Meaning |
|---|---|
| A | 序号 |
| B | 品名 |
| C | 型号/规格 |
| D | 单位 |
| E | 数量 |
| F | 不含税金额 |
| G | 税额 |
| H | 金额小计 |
| I | 备注 |

The inventory form should retain more exact source information than the summary spreadsheet. In particular, item name should normally preserve the invoice's full item description rather than the shortened filename label.

## 10. Current implementation

### `invoice_parser.py`

- uses PyMuPDF (`fitz`)
- extracts text directly from PDF
- parses 20-digit invoice number
- parses Chinese invoice date
- identifies two tax IDs
- infers buyer/seller names
- preserves one or more item rows, including negative discount/adjustment rows
- extracts monetary values
- maps common units such as `pcs` → `个`
- suggests a reimbursement category using keywords
- marks ambiguous or multi-row item structures for GUI review instead of discarding the invoice

### `main.py`

- loads `config.json`
- lets the user select one PDF in V1
- validates buyer name/tax ID and arithmetic
- allows editing of category and key fields
- computes `YYYYMM`
- generates `金额+名称.pdf`
- stops on an existing target filename
- shows a full preview
- requires the user to type `YES` before official writes
- calls WPS write backend
- copies renamed PDF
- opens modified spreadsheets in WPS

### `wps_backend.py`

- automates WPS Spreadsheet through COM using `KET.Application`
- does not require Microsoft Excel
- backs up official spreadsheet files
- writes reimbursement statistics
- writes inventory form for material expenses
- restores backups after write failures where possible
- opens the resulting spreadsheets visibly in WPS for inspection

## 11. Known V1 limitations

1. Only one invoice is processed per V1 interaction even if multiple PDFs are present.
2. Multi-item invoices are retained for manual review; automatic interpretation remains conservative.
3. Scanned/image-only invoices are not OCRed yet.
4. Electronic signature insertion is not implemented.
5. Online-order screenshot pairing is not implemented.
6. No structured GUI yet; interaction is primarily terminal-based.
7. WPS COM compatibility must be validated on the user's exact WPS/Windows installation.
8. Category classification is heuristic and relatively simple.
9. Duplicate detection currently relies primarily on the target filename collision.
10. The source PDF is not yet automatically moved to a completed/review directory after success.

## 12. Next development phase requested by the user

Focus on turning the working V1 logic into a convenient manually launched desktop workflow.

### Priority A — GUI review interface

Create a simple Windows GUI (technology choice can be evaluated; keep dependencies modest) that displays each invoice as a review card/table row with editable fields such as:

- source filename
- invoice number
- invoice date
- seller
- full item name
- category
- short name
- specification/model
- unit
- quantity
- amount excluding tax
- tax
- total
- reimbursement month
- proposed target filename
- validation status/errors

The user should not need to type category numbers or `YES` in the final UX.

### Priority B — Batch review

Allow the manually launched application to load all PDFs currently in `待处理` and create a review queue.

Important distinction:

- loading/parsing all PDFs is safe before confirmation
- official WPS writes are NOT allowed before final confirmation

The user should be able to exclude an invoice from the batch, edit its suggested fields, or mark it for later review.

### Priority C — Final consolidated confirmation

Before any official write, show a batch summary such as:

- number of invoices
- total reimbursement amount
- totals by category
- list of target PDF names
- whether each invoice will modify only the statistics sheet or also the inventory sheet
- validation warnings/errors

Require one deliberate final confirmation action.

### Priority D — Post-processing organization

After a successful write:

- keep/move processed source PDFs in a clearly named completed directory
- preserve failed/unresolved invoices separately
- store a simple transaction log including timestamp, invoice number, output filename, category, amount, target month, and write result

### Priority E — stronger duplicate protection

Add duplicate checks using invoice number as the primary identifier where possible.

Potential additional signals:

- seller tax ID
- total amount
- invoice date
- existing processing log

Duplicate checks should err on the side of stopping and asking the user.

### Later features

After the core GUI/batch workflow is stable:

- insert purchaser electronic signature image into eligible electronic invoices
- pair online-order screenshots with invoices
- deliberately implement multi-item invoice support
- add OCR fallback for scanned/image-only invoices

Do not implement automatic directory monitoring, autostart, or background resident behavior in this phase.

## 13. Product philosophy

This project's value is not that it is maximally autonomous. Its value is that it removes repetitive work while making incorrect financial writes difficult.

Preferred architecture:

```text
PDF parsing
    ↓
deterministic validation
    ↓
heuristic/AI suggestions
    ↓
human review/edit
    ↓
final explicit confirmation
    ↓
backup
    ↓
WPS write + PDF copy
    ↓
verification / WPS review
    ↓
processing log
```

Any future AI/LLM component should stay on the suggestion side of this boundary. It must not bypass deterministic validation, explicit confirmation, duplicate protection, backup, or rollback.
