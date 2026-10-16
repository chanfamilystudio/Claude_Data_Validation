# Production rules

`rules/` holds 19 rules across the four domains. They are written against an **assumed** staging layout
(`docs/ddl/assumed_staging.sql`), because the real staging DDL was not available. Everything they need from
the database is named in `config/datasets.yaml` and `config/mappings.yaml`. When the real DDL arrives, change
those two files and the column names in the affected rules, then re-run
`recon rules validate` and a period against real data.

`tests/integration/test_production_rules.py` loads the assumed layout plus
`tests/fixtures/production_sample.sql` and asserts that the shipped rules find exactly the planted breaks.
Update both when the layout changes.

## Rules

| Domain | Rule | Type | What it proves |
|---|---|---|---|
| employees | `emp_master_match` | record_match | Each worker exists in GP with the same name, department, job title, hire date, active status and national ID (PII, hashed) |
| employees | `emp_cost_center_map` | mapping | Every worker's cost center has a crosswalk row to an existing GP department |
| employees | `emp_job_profile_map` | mapping | Every worker's job profile maps to an existing GP job title |
| employees | `org_cost_center_map` | mapping | Every *active* Workday cost center maps to an existing GP department |
| employees | `gp_employee_quality` | quality | GP employee IDs present/unique; INACTIVE is 0/1; department and job title exist |
| employees | `wd_worker_quality` | quality | Workday worker IDs present/unique; known worker status; no future hire dates |
| payroll | `pay_jrnl_by_account` | control_total | Period payroll debits, credits and net agree per GP natural account |
| payroll | `pay_jrnl_by_department` | control_total | Period payroll net agrees per GP department segment |
| payroll | `pay_jrnl_grand_total` | control_total | Period payroll debits, credits and net agree in total |
| payroll | `pay_jrnl_account_map` | mapping | Every ledger account on the period's journal maps to an active GP natural account |
| payroll | `wd_pay_jrnl_quality` | quality | Journal lines complete and unique; every Workday journal balances |
| gl | `gl_account_map` | mapping | Every active Workday ledger account maps to an active GP natural account |
| gl | `gp_account_segment_quality` | quality | GL00100 account index unique; segment combination unique; ACTIVE is 0/1 |
| gl | `gp_account_string_quality` | quality | GL00105 account strings present and unique; every index exists in GL00100 |
| ap | `vendor_master_match` | record_match | Each Workday supplier exists in GP with the same name, status, tax ID (PII, hashed), city, state, ZIP, currency |
| ap | `ap_invoice_match` | record_match | Each period invoice exists in GP (open or paid) with the same amount (±0.01) and date |
| ap | `ap_invoice_totals` | control_total | Period invoice amount and count agree per vendor |
| ap | `gp_vendor_quality` | quality | GP vendor IDs present/unique; VENDSTTS is 1/2/3 |
| ap | `wd_invoice_quality` | quality | Workday invoices complete and unique; every invoice's supplier exists in the Workday supplier extract |

Employees, GL account and vendor checks are snapshot (master data). Payroll and invoice checks are periodic.

## Assumptions to confirm or change

**Great Plains (straight copies, GP names and types)**
- Tables are copied as-is into schema `stg_gp`: `UPR00100`, `UPR40300`, `UPR40301`, `GL00100`, `GL00105`,
  `GL20000`, `PM00200`, `PM20000`, `PM30200`. Only open-year GL (`GL20000`) is used; closing a prior year needs
  `GL30000` added to the `gl_detail` view.
- Three helper views are needed on the GP side (definitions in the DDL file):
  - `stg_gp.gl_detail`: GL lines plus natural account (`ACTNUMBR_2`) and department (`ACTNUMBR_3`) segments.
  - `stg_gp.natural_accounts`: active natural accounts.
  - `stg_gp.ap_invoices`: open and paid, non-voided invoices (`DOCTYPE = 1`).
- **Account segments:** segment 2 is the natural account and segment 3 the department. Change the views if your
  account format differs.
- **Payroll postings** are GL lines with `SOURCDOC = 'UPRCC'` (dataset filter in `config/datasets.yaml`).
- **No freshness checks on GP data:** straight copies carry no load timestamp. Add a `load_ts` column and a
  `freshness:` entry if your copy job stamps one.

**Workday staging (schema `stg_wd`)**
- One table per extract with a `load_ts` column; freshness allows 26 hours.
- **Workday IDs equal GP IDs:** worker `employee_id` = GP `EMPLOYID`; supplier `supplier_id` = GP `VENDORID`;
  `invoice_number` = GP `DOCNUMBR`. If they differ, add the GP ID to the extract or a crosswalk.
- **Identical formats:** national IDs and tax IDs are formatted the same way in both systems (e.g. dashes kept).
  Normalise them in staging if not, otherwise every row breaks.
- **Payroll journal:** one row per journal line, with `debit`/`credit`, `ledger_account` and `cost_center`, dated by
  `pay_date`.

**Crosswalks (schema `xref`)**
- `cost_center_map` is effective-dated (`eff_start`/`eff_end`). The other four crosswalks are not.
- Status crosswalks translate Workday text statuses to the GP codes:
  - `worker_status_map`: Active/Leave → 0, Terminated → 1.
  - `supplier_status_map`: Active → 1, Inactive → 2, On Hold → 3.

**Tolerances**
- All money comparisons allow ±0.01. Counts, dates and text are exact.
- Text comparison ignores case and surrounding spaces. That covers GP's upper-case, fixed-width `CHAR` columns.

**Sign-off**
- `config/signers.yaml` is empty. Add the Windows logins allowed to sign off each domain before the first close.

## Not covered yet
- Expense reports, if they post to GP through a route other than payables invoices.
- Pay-code level payroll detail (GP `UPR30300`), and benefit and deduction reconciliation.
- Trial-balance comparison beyond payroll postings. Workday is not the GL book of record in this design.
