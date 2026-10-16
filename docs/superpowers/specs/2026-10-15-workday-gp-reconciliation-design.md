# Workday ↔ Great Plains Reconciliation Framework — Design Spec

- **Status:** Draft for review
- **Date:** 2026-10-15
- **Repo:** `chanfamilystudio/Claude_Data_Validation`

## 1. Purpose and scope

### 1.1 Goal

A reusable, metadata-driven data validation framework that reconciles Workday against Microsoft Dynamics Great Plains (GP), stored in SQL Server, and that can be extended to validate other sources without changing the comparison engine.

It serves four uses at once:

1. **Financial close / audit** — prove payroll, GL and AP postings agree; produce signed-off evidence per period.
2. **Master data sync** — employees, org structure, chart of accounts, vendors agree across systems.
3. **Integration monitoring** — nightly detection of failed or partial interface loads.
4. **Migration cutover** — on-demand validation of any domain/period.

### 1.2 In scope (v1)

| Area | Decision |
|---|---|
| Domains | `payroll` (payroll journals), `employees` (employees & org), `gl` (chart of accounts / GL), `ap` (vendors / AP / expenses) |
| Check types | `control_total`, `record_match`, `mapping`, `quality` |
| Data access | Both systems already staged in SQL Server; crosswalk (mapping) tables already exist in SQL |
| Engine | Python 3.11+, rules compiled to T-SQL and executed in SQL Server ("push-down") |
| Rule authoring | YAML files in Git, PR-reviewed |
| Results store | New `recon` schema in the same database as the staging tables |
| Break handling | Persistent breaks tracked to resolution with owner, comments, audit trail |
| User interface | Excel export; edited workbook re-imported to update break status |
| Sign-off | Per domain + period, by an authorized signer, with frozen evidence workbook |
| Schedule | Nightly scheduled run + on-demand CLI |
| Runtime | On-prem Windows server, Windows Authentication, Task Scheduler |
| PII | Compared via hash in SQL; masked in all stored values and exports |
| Connectors | SQL Server connector implemented; documented `Connector` interface for future sources |

### 1.3 Out of scope (v1)

- Ingestion/extraction from Workday or GP (staging is loaded by an existing process).
- Connectors for files, APIs, or non-SQL-Server databases (interface only).
- Email/Teams alerting.
- Web UI or BI dashboards.
- Segregated preparer/reviewer sign-off (single authorized signer is sufficient).
- Fiscal calendars (periods are calendar months in v1).
- Data retention/purge (everything is kept).

### 1.4 Assumptions

1. Each human user runs the CLI under their own Windows login; identity is taken from `SUSER_SNAME()`. Scheduled runs use a service account.
2. Python 3.11+ can be installed on the Windows server.
3. Volume per run is < 1M rows for the largest domain.
4. Staging and mapping table DDL will be supplied by the business; v1 production rules are written after it is received. The framework is built and tested independently against a synthetic dataset.

### 1.5 Success criteria

- Every planted break type in the synthetic test dataset is detected; clean records produce zero breaks.
- A full nightly run across all four domains at < 1M rows completes in under 15 minutes.
- An auditor can, from `recon.*` and the evidence workbook alone, see what was checked (exact SQL), what differed, who did what about it, and who signed off.
- Adding a new reconciliation between already-registered datasets requires only a new YAML file — no Python changes.
- Adding a new source type requires only a new `Connector` implementation.

## 2. Architecture

### 2.1 Approach

Rules are declared in YAML. Python loads and validates them, asks a connector for each side's dataset as a SQL-addressable source, generates T-SQL per check type, and executes it inside SQL Server. Results are written directly into `recon.*`. Future non-SQL connectors land data into `recon.landing_*` tables first, then use the same comparison path.

Rejected alternatives: comparing in pandas (moves PII out of SQL, comparisons not reproducible as SQL, totals slower); Great Expectations/Soda (cover single-source quality only; cross-system matching, break workflow, Excel round-trip and sign-off would still be custom).

### 2.2 Components

```
             rules/**/*.yaml, config/*.yaml (Git)
                         │
                         ▼
 ┌──────────────── recon CLI (typer) ────────────────┐
 │  run │ export │ import │ signoff │ rules validate │
 └──┬───────┬────────┬─────────┬──────────┬──────────┘
    ▼       ▼        ▼         ▼          ▼
 RuleLoader → Planner → SqlGenerator → Executor ──► SQL Server
                                        │            ├─ staging (WD/GP)      [read-only]
                                        ▼            ├─ mapping tables        [read-only]
                                   BreakManager      └─ recon.*               [read/write]
 Connector (SqlServer in v1) ─────────────────────►
 ExcelExporter / ExcelImporter / SignOff
```

| Unit | Responsibility | Depends on |
|---|---|---|
| `RuleLoader` | Parse YAML, validate with pydantic models, compute `version_hash` (SHA-256 of canonical rule JSON) | pydantic, PyYAML |
| `Connector` (interface) | `describe(dataset) -> list[Column]`; `as_sql_source(dataset, params) -> SqlSource` (a table, view, or landing table it populated) | DB connection |
| `SqlServerConnector` | v1 implementation: returns the staging object with dataset `filter` and period predicate applied | pyodbc |
| `Planner` | Resolve rules for the requested domain/rule/period, bind run parameters | RuleLoader |
| `SqlGenerator` | Pure functions, one per check type: `(rule, sources, params) -> GeneratedSql`. No DB access | none |
| `Executor` | Open run, execute generated SQL per rule in its own transaction, persist results + SQL text, close run | pyodbc |
| `BreakManager` | Merge a check's raw differences into `recon.break` (open / carry forward / auto-clear / reopen) | SQL `MERGE` |
| `ExcelExporter` / `ExcelImporter` | Produce run workbook; validate and apply edited workbook | openpyxl |
| `SignOff` | Enforce preconditions, record approval, produce frozen evidence workbook | Exporter |

Boundaries: all comparison logic runs in SQL Server; Python plans, generates and orchestrates. The engine never writes outside `recon.*`.

### 2.3 Repository layout

```
src/recon/            # package: cli.py, rules/, connectors/, sqlgen/, executor.py,
                      #          breaks.py, excel/, signoff.py, db.py
sql/migrations/       # versioned DDL for the recon schema (V001__init.sql, ...)
config/
  datasets.yaml       # named datasets
  mappings.yaml       # named crosswalks
  signers.yaml        # authorized signers per domain
  connection.yaml     # connection settings (no secrets); overridable by RECON_DB env var
rules/
  payroll/ employees/ gl/ ap/      # one rule per file
tests/
  unit/ integration/ fixtures/     # synthetic WD/GP dataset with planted breaks
docs/
```

## 3. Data model (`recon` schema)

Created and upgraded by versioned migration scripts in `sql/migrations/`, tracked in `recon.schema_version`.

| Table | Purpose | Key columns |
|---|---|---|
| `recon.rule` | Catalog of every rule version ever run | PK (`rule_id`, `version_hash`); `domain`, `check_type`, `severity`, `owner`, `yaml_text`, `active`, `loaded_at` |
| `recon.run` | One row per execution | `run_id` (identity); `trigger` (scheduled/manual); `domain` (nullable = all); `period_label`, `period_start`, `period_end`; `status` (running/succeeded/partial/failed); `run_by`; `git_commit`; `engine_version`; `started_at`, `finished_at` |
| `recon.run_check` | One row per rule in a run | `run_check_id`; `run_id`; `rule_id`, `version_hash`; `result` (pass/fail/error); `rows_left`, `rows_right`; `break_count`; `generated_sql` NVARCHAR(MAX); `duration_ms`; `error_message` |
| `recon.total_result` | Control-total output | `run_check_id`; `group_key`; `measure`; `left_value`, `right_value`, `diff`; `tolerance_applied`; `within_tolerance` |
| `recon.break` | Persistent break | `break_id`; `break_key` (unique); `rule_id`; `domain`; `period_label`; `business_key`; `break_type`; `field_name`; `left_value`, `right_value`, `diff`; `status`; `owner`; `resolution_type` (auto_cleared/manual, nullable); `post_signoff` bit; `first_seen_run_id`, `last_seen_run_id`, `resolved_run_id`; `version` INT (starts at 1; incremented only on status/owner changes, reopen, auto-clear — not when a nightly run merely re-sees the break, so older exports stay importable); `created_at`, `updated_at` |
| `recon.break_occurrence` | Per-run values of a break | (`break_id`, `run_id`); `left_value`, `right_value`, `diff` |
| `recon.break_comment` | Comments | `comment_id`; `break_id`; `text`; `author`; `created_at`; `import_batch_id` (nullable) |
| `recon.audit_log` | Every change by engine or human | `audit_id`; `entity` (break/signoff); `entity_id`; `field`; `old_value`; `new_value`; `changed_by`; `changed_at`; `source` (engine/import/cli); `run_id`, `import_batch_id` (nullable) |
| `recon.import_batch` | Excel re-imports | `import_batch_id`; `file_name`; `file_sha256` (unique); `imported_by`; `rows_applied`; `rows_rejected`; `imported_at` |
| `recon.signoff` | Period approvals | `signoff_id`; `domain`; `period_label`; `run_id`; `signed_by`; `signed_at`; `comment`; `open_break_count` (always 0); `accepted_break_count`; `evidence_path`; `evidence_sha256`; `status` (signed/revoked); `revoked_by`, `revoked_at`, `revoke_reason` |
| `recon.landing_*` | Reserved for future connectors | Not created in v1 |

### 3.1 Enumerations

- **break_type:** `missing_left` (only in right/GP), `missing_right` (only in left/Workday), `value_mismatch`, `mapping_missing`, `mapping_mismatch`, `quality_violation`, `total_mismatch`.
- **break status:** `open`, `investigating`, `resolved`, `accepted`.
- **severity:** `high`, `medium`, `low`.

### 3.2 Conventions

- **Left = Workday, right = GP** by convention in shipped rules (the engine itself is side-agnostic).
- **Periods:** `period_label` (e.g. `2026-09`) + inclusive `period_start`/`period_end` dates. Master-data (non-periodic) checks use `period_label = 'SNAPSHOT'`.
- **Identity:** `run_by`, `changed_by`, `signed_by`, `imported_by`, `author` = `SUSER_SNAME()` of the connection.
- **PII:** columns listed in a dataset's `pii_columns` (and compare fields marked `pii: true`) are masked in every check type: comparison uses `HASHBYTES('SHA2_256', normalized_value)`; `left_value`/`right_value` are stored as `***`. Raw PII is never written to `recon.*`. PII columns may not be used where they become `business_key` — record_match keys, control_total groups, mapping columns, quality keys and unique columns (validation error). `custom_sql` cannot be inspected and must not return a PII column.
- **break_key:** SHA-256 over `rule_id | period_label | business_key | field_name | break_type`. Excludes rule version, so tightening/loosening a rule cleanly clears breaks.

### 3.3 Database roles

- `recon_engine`: SELECT on staging and mapping schemas; full DML on `recon.*`; EXECUTE on `sp_getapplock`.
- `recon_reader`: SELECT on `recon.*`.

## 4. Rule format

### 4.1 Datasets and mappings

```yaml
# config/datasets.yaml
datasets:
  wd_workers:
    source: workday            # logical source name → connector
    object: stg_wd.workers     # schema.table or view
    period_column: null        # null = snapshot (master data)
  gp_employees:
    source: gp
    object: stg_gp.UPR00100
    period_column: null
  wd_pay_jrnl:
    source: workday
    object: stg_wd.payroll_journal
    period_column: pay_date
    freshness: {column: load_ts, max_age_hours: 26}   # optional
  gp_gl_pay:
    source: gp
    object: stg_gp.GL20000
    period_column: TRXDATE
    filter: "SOURCDOC = 'UPRCC'"                       # optional, trusted SQL

sources:
  workday: {connector: sqlserver}
  gp:      {connector: sqlserver}
```

```yaml
# config/mappings.yaml
mappings:
  cc_to_dept:
    object: xref.cost_center_map
    from: wd_cost_center
    to: gp_dept
    effective: {start: eff_start, end: eff_end}        # optional effective dating
```

When `effective` is set, the mapping row valid on the period end date (or today for snapshots) is used. More than one valid row for a `from` value is a rule error.

### 4.2 Common rule fields

```yaml
id: emp_master_match          # unique, [a-z0-9_]+, matches file name
domain: employees             # payroll | employees | gl | ap (extensible)
check_type: record_match      # control_total | record_match | mapping | quality
description: Workday workers vs GP employee master
owner: hris-team              # informational
severity: high
active: true
max_breaks: 5000              # optional; default 5000
timeout_s: 300                # optional; default 300
```

### 4.3 `record_match`

```yaml
left: wd_workers
right: gp_employees
keys: [{left: employee_id, right: EMPLOYID}]
normalize: {trim: true, case_insensitive: true}       # defaults shown
compare:
  - {left: cost_center,   right: DEPRTMNT, via_mapping: cc_to_dept}
  - {left: hire_date,     right: STRTDATE, type: date}
  - {left: annual_salary, right: SALARY,   type: decimal, tolerance: {abs: 0.01}}
  - {left: national_id,   right: SOCSCNUM, pii: true}
```

Produces `missing_left`, `missing_right`, `value_mismatch` (one break per differing field), and `mapping_missing` when `via_mapping` finds no crosswalk row. Duplicate keys on either side are reported as `quality_violation` breaks on that rule (field `__key__`) and those keys are excluded from field comparison.

### 4.4 `control_total`

```yaml
left: wd_pay_jrnl
right: gp_gl_pay
group_by: [{left: ledger_account, right: ACTINDX, via_mapping: wd_acct_to_gp}]   # optional
measures:
  - {name: net_amount, left: "SUM(debit - credit)",    right: "SUM(DEBITAMT - CRDTAMNT)", tolerance: {abs: 0.01}}
  - {name: line_count, left: "COUNT(*)",               right: "COUNT(*)"}
```

Writes every group × measure to `recon.total_result`; out-of-tolerance rows become `total_mismatch` breaks (`business_key` = group key, `field_name` = measure). Groups present on one side only are compared against 0.

### 4.5 `mapping`

```yaml
dataset: wd_workers
column: cost_center
mapping: cc_to_dept
target: {dataset: gp_departments, column: DEPRTMNT}
```

Distinct non-null source values with no crosswalk row → `mapping_missing`; translated values not found in the target → `mapping_mismatch`. `business_key` = source value.

### 4.6 `quality`

```yaml
dataset: gp_employees
key: [EMPLOYID]               # used as business_key in breaks
assertions:
  - not_null: [EMPLOYID, DEPRTMNT]
  - unique: [EMPLOYID]
  - accepted_values: {column: INACTIVE, values: [0, 1]}
  - references: {column: DEPRTMNT, dataset: gp_departments, ref_column: DEPRTMNT}
  - like: {column: EMPLOYID, pattern: "[0-9][0-9][0-9][0-9][0-9][0-9]"}
  - custom_sql:
      name: future_hire_dates
      sql: "SELECT EMPLOYID AS business_key FROM {dataset} WHERE STRTDATE > GETDATE()"
```

Each violating row → `quality_violation` break with `field_name` = assertion name/column. `custom_sql` must return a `business_key` column; `{dataset}` is replaced with the connector's SQL source.

### 4.7 Semantics

- **Tolerance:** within tolerance iff `|diff| <= max(abs, pct × |right|)`; `abs` and `pct` default to 0 (exact).
- **Types:** `string` (default), `decimal`, `int`, `date`, `datetime`, `bool`. Values are cast to the declared type on both sides before comparing; a failed cast is a `value_mismatch` with the raw value shown.
- **Nulls:** null = null; null vs non-null is a mismatch. Empty string is treated as null when `trim` is on.
- **Normalization:** `trim` and `case_insensitive` apply to string comparisons and keys.
- **Periods:** the run's period filters every dataset with a `period_column`; snapshot datasets are compared whole.

### 4.8 Validation and SQL safety

- Rules and configs are validated by pydantic; unknown fields are errors.
- Identifiers (`object`, column names) must match `^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$`, are bracket-quoted in generated SQL, and are checked against `Connector.describe()` in preflight.
- Free SQL is allowed only in `measures`, `filter` and `custom_sql`. It is trusted code: changes go through PR review, and the engine login cannot write outside `recon.*`.
- Run parameters (dates, labels) are always passed as bound parameters, never interpolated.

## 5. Run flow

`recon run [--domain D | --all] [--rule ID] --period {YYYY-MM|current|previous} [--trigger scheduled|manual]`

1. **Preflight** — load/validate all YAML; upsert new versions into `recon.rule`; `describe()` every referenced dataset and verify columns. Any failure → exit 2, nothing written to `recon.run`.
2. **Lock** — `sp_getapplock` on `recon:{domain}:{period}` (exclusive, session-owned) for every domain in scope (`--all` takes all of them, in sorted order). If any is held → exit 2 with "run already in progress".
3. **Stale-run cleanup** — any `recon.run` still `running` whose lock is not held is marked `failed`.
4. **Open run** — insert `recon.run` (`running`).
5. **Per rule** (own transaction):
   1. Freshness check for datasets that declare it; stale → `run_check.result = error` ("stale source: …"); skip.
   2. Generate SQL; execute into a temp table of raw differences (with `timeout_s`).
   3. If difference count > `max_breaks` → `error` ("suspected load failure: N > max_breaks"); breaks untouched.
   4. Otherwise `BreakManager.merge(...)`; write `run_check` (`pass` if 0 breaks, else `fail`) with `generated_sql`.
   5. Any exception → rollback that rule's transaction; record `error` with message.
6. **Close run** — `succeeded` (no errors), `partial` (some errors), `failed` (all errors or unexpected abort).
7. **Export** — write the run workbook (§7.1).
8. **Exit code** — `0` all pass; `1` breaks present, no errors; `2` any error.

`current`/`previous` resolve to calendar months relative to the server date. Running against a signed-off period prints a warning.

**Scheduling:** Task Scheduler runs `recon run --all --period current --trigger scheduled` nightly after staging loads, plus `--period previous` on calendar days 1–10.

## 6. Break lifecycle

```
            new key seen
                │
                ▼
   ┌──────►  open ◄──────────────────────┐
   │          │  user                    │ reappears (engine)
   │          ▼                          │
   │     investigating                   │
   │       │       │ user                │
   │  user ▼       ▼                     │
   │   accepted   resolved ──────────────┘
   │       │       ▲
   │       │       │ engine: absent from a successful run of same rule+period
   └───────┘  (auto_cleared)
  values changed (engine)
```

Engine transitions (performed in `BreakManager.merge` for one rule + period, only when that rule's check did not error):

| Current status | Key present this run | Key absent this run |
|---|---|---|
| (none) | create `open` | — |
| `open` / `investigating` | update values, `last_seen_run_id`; add occurrence | → `resolved`, `resolution_type = auto_cleared` |
| `resolved` | → `open` (reopen); audit entry | no change |
| `accepted` | if left/right values unchanged: update `last_seen`; else → `open` | → `resolved`, `auto_cleared` |

User transitions (via import or CLI):

| From | Allowed to | Requires comment |
|---|---|---|
| `open` | `investigating`, `resolved`, `accepted` | yes for `resolved`/`accepted` |
| `investigating` | `open`, `resolved`, `accepted` | yes for `resolved`/`accepted` |
| `accepted` | `open`, `investigating` | no |
| `resolved` | `open` | no |

Every transition writes `recon.audit_log`. Breaks created in a signed-off period get `post_signoff = 1`.

## 7. Excel round-trip

### 7.1 Export

`recon export --domain D --period P` (also runs automatically at end of each run). File: `<export_dir>/recon_<domain|all>_<period>_<run_id>.xlsx`.

| Sheet | Content | Editable |
|---|---|---|
| `Summary` | Run metadata; per-rule result, row counts, break count, severity, error message | no |
| `Totals` | `recon.total_result` rows; out-of-tolerance highlighted | no |
| `Breaks` | Breaks with status `open`/`investigating`/`accepted`, plus those resolved in this run. Columns: break_id, rule_id, severity, domain, period, business_key, break_type, field, left, right, diff, first_seen, last_seen, post_signoff, comment history (latest 5) | `Status` (dropdown), `Owner`, `New comment` |
| `_meta` (very hidden) | export_id, run_id, schema version, exported_by, exported_at; `break_id → version` | no |

Non-editable cells are locked with sheet protection (convenience only; enforcement happens on import). Rows are sorted by severity, then break_type, then business_key.

### 7.2 Import

`recon import <file.xlsx> [--dry-run]`

1. Reject if `_meta` missing, schema version unsupported, or `file_sha256` already in `recon.import_batch`.
2. For each `Breaks` row where `Status`, `Owner` or `New comment` differs from the database:
   - break_id exists; `version` matches current (else reject: "changed since export");
   - transition allowed per §6; comment present where required;
   - `Owner` ≤ 128 chars.
3. Apply all valid rows in one transaction: update `recon.break`, insert `recon.break_comment`, write `recon.audit_log` (source = `import`), insert `recon.import_batch`.
4. Write `<file>_rejects.xlsx` with rejected rows and a `Reason` column. `--dry-run` performs validation and writes the rejects file without applying.

## 8. Sign-off

`recon signoff --domain D --period P --comment "..."`

Preconditions (all must hold, else refuse with the reasons):

1. The latest run covering D/P has status `succeeded` and executed every active rule in D.
2. No breaks for D/P (including `SNAPSHOT` breaks of D) are `open` or `investigating`.
3. The current user is listed for D in `config/signers.yaml`.
4. No existing `signed` sign-off for D/P.

On success: insert `recon.signoff`; generate the evidence workbook (Summary, Totals, all breaks for D/P including resolved, full comment and audit history, a `SQL` sheet with each check's `generated_sql`) into `<evidence_dir>/<domain>/<period>/`; set the file read-only; store its SHA-256.

`recon signoff revoke --domain D --period P --reason "..."` → status `revoked` with audit entry; signer permission required.

## 9. Connector interface

```python
class Connector(Protocol):
    name: str
    def describe(self, dataset: DatasetConfig) -> list[Column]: ...
    def as_sql_source(self, dataset: DatasetConfig, params: RunParams) -> SqlSource: ...
```

`SqlSource` is a SQL expression the generator can select from (bracket-quoted object with filter and period predicate applied, as a derived table). A future file/API/other-DB connector implements `as_sql_source` by loading rows into `recon.landing_<dataset>` (truncate + bulk insert within the run) and returning that table. Connectors are registered by name in `src/recon/connectors/__init__.py`; `sources.<name>.connector` selects one. `docs/connectors.md` documents how to add one.

## 10. Error handling

| Failure | Behavior |
|---|---|
| Invalid YAML / unknown column / unknown dataset | Preflight aborts with all errors (file + path); exit 2; nothing written |
| DB unreachable / login failure | Exit 2 with message; nothing written |
| Rule SQL error or timeout | That `run_check` = `error`; other rules continue; run `partial` |
| Stale source / `max_breaks` exceeded | `run_check` = `error`; breaks for that rule untouched |
| Process crash mid-run | Per-rule transactions keep data consistent; next run marks the orphan `failed` |
| Import validation failures | Row-level rejects file; valid rows applied |

Logging: rotating file (`logs/recon.log`, 10 MB × 10) plus console; every line includes `run_id` and `rule_id` where applicable. No PII values are logged.

## 11. Security

- Engine uses `recon_engine` role; analysts get `recon_reader`.
- Windows Authentication (`Trusted_Connection=yes`); connection settings in `config/connection.yaml` or `RECON_DB`; no secrets in Git.
- PII hashed in SQL; `***` in `recon.*`, exports and logs.
- Evidence folder ACLs managed by IT; files set read-only and hashed.
- SQL injection controls per §4.8.

## 12. Testing

- **Unit (pytest, no DB):** pydantic validation (valid/invalid fixtures); SQL generator golden-file tests per check type and option; tolerance/null/type semantics; break transition table; Excel export → edit → import round-trip using an in-memory fake repository.
- **Integration:** SQL Server 2022 in Docker (`mcr.microsoft.com/mssql/server:2022-latest`); migrations applied; synthetic WD/GP fixture dataset containing at least one planted instance of every `break_type`, PII fields, effective-dated mappings, duplicate keys and a stale-source case. Scenarios: run → expected breaks; fix data → auto-clear; regress → reopen; accept → unchanged values stay accepted; import with valid, invalid and conflicting rows; sign-off blocked then permitted; revoke.
- **CI:** GitHub Actions on every PR — lint (ruff), unit tests, integration tests with the SQL Server service container, and `recon rules validate` over `rules/` and `config/`.
- **Acceptance (after DDL is received):** production rules for all four domains run against a sample extract; business confirms planted/known breaks are found and clean data produces none.

## 13. Delivery order

1. Project skeleton, `recon` schema migrations, DB access layer, CLI shell.
2. Rule/config models and loader; `rules validate`.
3. `record_match` and `control_total` generators + Executor + run flow.
4. `mapping` and `quality` generators.
5. BreakManager lifecycle.
6. Excel export and import.
7. Sign-off and evidence workbook.
8. Synthetic integration suite + CI.
9. Production rules for payroll, employees, gl, ap (blocked on DDL).

## 14. Open items

- Staging and mapping table DDL / samples for the four domains (blocks step 9 only).
- Export and evidence folder paths (UNC) and the Windows service account name for scheduled runs (deployment configuration; defaults are local folders).
