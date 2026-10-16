# Operations guide

How to install, configure, schedule and operate `recon` on the Windows server, and how to develop
against it locally. For rule syntax see the design spec
([`docs/superpowers/specs/2026-10-15-workday-gp-reconciliation-design.md`](superpowers/specs/2026-10-15-workday-gp-reconciliation-design.md));
for adding a data source see [`connectors.md`](connectors.md).

Contents: [Install](#1-install-on-windows) · [Configuration](#2-configuration) ·
[Database setup and grants](#3-database-setup-and-grants) · [Running](#4-running-recon) ·
[Scheduling](#5-scheduling-with-task-scheduler) · [Exit codes](#6-exit-codes) ·
[Locks](#7-locks-and-concurrent-runs) · [Review, import and sign-off](#8-review-import-and-sign-off) ·
[Folders and permissions](#9-folders-and-permissions) · [Logs](#10-logs) ·
[Behaviours to know when writing rules](#11-behaviours-to-know-when-writing-rules) ·
[Troubleshooting](#12-troubleshooting) · [Local development](#13-local-development)

## 1. Install on Windows

Prerequisites on the server:

1. **Python 3.11 or newer** (python.org installer, "Add python.exe to PATH").
2. **Microsoft ODBC Driver 18 for SQL Server** (`msodbcsql.msi` from Microsoft; the driver name is
   `ODBC Driver 18 for SQL Server`).
3. **Git**, or a copy of the repository.

Install into a virtual environment next to the project:

```bat
git clone <repository-url> C:\recon
cd C:\recon
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\pip install .
.venv\Scripts\recon --help
```

`C:\recon` is the **project root**: the folder that holds `config\` and `rules\`. Every command takes
`--root <folder>` (default: the current directory), so scheduled tasks should always pass
`--root C:\recon`. The SQL migrations ship inside the package, so `pip install .` is all `recon db migrate`
needs. To upgrade, `git pull`, re-run `pip install .`, then `recon db migrate`.

## 2. Configuration

All configuration lives in `config\*.yaml` under the project root; rules live in
`rules\<domain>\<rule_id>.yaml` (the file name stem is the rule id). Validate everything, without a database,
with `recon rules validate --root C:\recon` (exit 0 = valid, 2 = problems, all listed).

| File | Purpose |
|---|---|
| `config\connection.yaml` | `driver`, `server`, `database`, `trusted_connection` (default `true`). No secrets. |
| `config\datasets.yaml` | `sources` (logical source -> connector) and `datasets` (object, `period_column`, optional `filter` and `freshness`). |
| `config\mappings.yaml` | Crosswalk tables (`object`, `from`, `to`, optional `effective: {start, end}`). |
| `config\settings.yaml` | `export_dir`, `evidence_dir`, `log_dir`. Relative paths resolve against the project root; absolute and UNC paths are allowed. |
| `config\signers.yaml` | Sign-off allow-list: domain -> Windows logins exactly as `SUSER_SNAME()` returns them (for example `CORP\hr.lead`; compared case-insensitively). |

The database connection comes from the environment variable `RECON_DB` (a full ODBC connection string) when it
is set, otherwise from `config\connection.yaml`, which produces
`Driver={...};Server=...;Database=...;Trusted_Connection=yes;TrustServerCertificate=yes`. The engine always uses
**Windows authentication** in production; identity everywhere (`run_by`, `changed_by`, `signed_by`,
`imported_by`, comment `author`) is `SUSER_SNAME()` of the connection, so each person runs the CLI under their own
Windows login and the scheduled job runs under a service account.

Staging tables, views and crosswalks are read through the `sqlserver` connector, which reads objects in the
**same database** as the `recon` schema. The engine only ever `SELECT`s from them and writes only to schema `recon`.

## 3. Database setup and grants

1. Create (or choose) the database and run the migrations as a login that may create schemas, tables and roles
   (for example `db_owner`):

   ```bat
   .venv\Scripts\recon db migrate --root C:\recon
   ```

   This creates schema `recon`, the roles `recon_engine` and `recon_reader`, and grants `recon_engine` DML on
   `recon.*` and `recon_reader` SELECT on `recon.*`. Re-running it prints `Up to date`. Two concurrent
   migrations are refused (exit 2).

2. Grant `recon_engine` read access to the staging and crosswalk schemas. These grants are site-specific, so the
   migration does not issue them. Using the schema names from the shipped configuration:

   ```sql
   USE [Finance];
   GRANT SELECT ON SCHEMA::stg_wd TO recon_engine;   -- Workday staging
   GRANT SELECT ON SCHEMA::stg_gp TO recon_engine;   -- Great Plains staging
   GRANT SELECT ON SCHEMA::xref   TO recon_engine;   -- crosswalk tables
   ```

   Add one `GRANT SELECT ON SCHEMA::<name> TO recon_engine;` for every other schema a dataset or mapping uses.
   `sp_getapplock` needs no grant (system procedures are executable by `public`).

3. Add the people and the service account to the roles. Everyone who **runs rules, imports workbooks or signs
   off** writes to `recon.*` and therefore needs `recon_engine`; people who only read results need
   `recon_reader`.

   ```sql
   CREATE USER [CORP\svc-recon] FOR LOGIN [CORP\svc-recon];
   ALTER ROLE recon_engine ADD MEMBER [CORP\svc-recon];

   CREATE USER [CORP\analyst] FOR LOGIN [CORP\analyst];
   ALTER ROLE recon_reader ADD MEMBER [CORP\analyst];
   ```

   (Skip `CREATE USER` when the database user already exists.)

4. Check it end to end with the service account: `recon run --all --period current --no-export` should reach exit
   code 0 or 1, not 2.

## 4. Running recon

```text
recon rules validate [--root R]
recon db migrate [--root R]
recon run (--all | --domain D | --rule ID) --period P [--trigger manual|scheduled] [--no-export] [--root R]
recon export (--all | --domain D) --period P [--run-id N] [--root R]
recon import FILE.xlsx [--dry-run] [--root R]
recon signoff --domain D --period P --comment "..." [--root R]
recon signoff revoke --domain D --period P --reason "..." [--root R]
```

- Exactly one of `--all`, `--domain` or `--rule` is required for `run`. **`--domain` takes a single domain.**
- **`--period` accepts `YYYY-MM`, `current` or `previous`.** `current` and `previous` are calendar months relative
  to the date on the machine running `recon`. The literal `SNAPSHOT` is **not** accepted: rules over datasets
  without a `period_column` are snapshot rules and run inside every period's run (their breaks carry the period
  label `SNAPSHOT` and show up in every period's workbook and sign-off).
- Every `run` writes a review workbook to `<export_dir>\recon_<domain|all>_<period>_<run_id>.xlsx` unless
  `--no-export` is given; a failed export is a warning, never a different exit code. `recon export` names its
  file after the run it shows, `recon_<domain|all>_<period>_<run_id>.xlsx` (the latest run, or `--run-id`), and
  uses `_latest` only when the period has no run yet. **Exports never overwrite a file:** if the name is taken,
  `_<UTC yyyymmddTHHMMSS>` (and, if needed, a counter `_2`, `_3`, ...) is appended.
- Running against a period that is already signed off prints
  `warning: <domain> <period> is signed off; new breaks are flagged post_signoff` and carries on.

### Freshness

A dataset with `freshness: {column, max_age_hours}` is checked before its rule runs: the newest value of `column`
must be at most `max_age_hours` old. The check is measured **over the whole dataset** (the dataset `filter` is
applied, the period predicate is not), so a closed period whose rows were loaded long ago is not reported stale as
long as the dataset as a whole is fresh. Age is compared with the **SQL Server's local time** (`SYSDATETIME()`),
so staging loads must stamp local server time, not UTC. A stale source is an `error` for that rule
(`stale source <dataset>`), not a break.

## 5. Scheduling with Task Scheduler

Schedule two jobs under the service account (`CORP\svc-recon` below; `/RP *` makes `schtasks` prompt for the
password). Run them well apart: snapshot rules lock per domain across periods (see [Locks](#7-locks-and-concurrent-runs)),
so two overlapping runs would refuse each other with exit 2.

**Nightly, after the staging loads finish (example: 03:00):**

```bat
schtasks /Create /TN "Recon\Nightly" /SC DAILY /ST 03:00 /RU CORP\svc-recon /RP * ^
  /TR "C:\recon\.venv\Scripts\recon.exe run --all --period current --trigger scheduled --root C:\recon"
```

**Previous period, on calendar days 1-10 (example: 05:00).** `schtasks` cannot express "days 1-10" with one
trigger, so run a daily task whose script exits early after the 10th. Save as `C:\recon\scripts\previous-period.ps1`:

```powershell
if ((Get-Date).Day -gt 10) { exit 0 }
& C:\recon\.venv\Scripts\recon.exe run --all --period previous --trigger scheduled --root C:\recon
exit $LASTEXITCODE
```

```bat
schtasks /Create /TN "Recon\PreviousPeriod" /SC DAILY /ST 05:00 /RU CORP\svc-recon /RP * ^
  /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\recon\scripts\previous-period.ps1"
```

Operate them with `schtasks /Run /TN "Recon\Nightly"`, `schtasks /Query /TN "Recon\Nightly" /V /FO LIST`
(the *Last Result* field is the exit code, see below) and `schtasks /Delete /TN "Recon\Nightly" /F`.

The service account needs: a SQL Server login in `recon_engine`, read/write on the project's `exports\`, `logs\`
and `evidence\` folders (or the configured paths), and "Log on as a batch job".

## 6. Exit codes

| Code | Meaning |
|---|---|
| `0` | Every check passed (for `import`: all rows applied). |
| `1` | Breaks were found and no check errored (for `import`: some rows were rejected; see the `_rejects.xlsx` file). |
| `2` | Any error or refused command: invalid configuration, database unreachable, a rule error (rule SQL failed, stale source, more than `max_breaks`), lock held elsewhere, refused sign-off, unknown domain or period. |

Alert on a scheduled task's *Last Result* of `2`; `1` is the normal "there is something to review" outcome. A
rule that errors never changes the breaks of that rule (they are left exactly as they were) and the run is
`partial` (or `failed` when every rule errored).

## 7. Locks and concurrent runs

- `recon run` takes an exclusive SQL Server application lock `recon:<domain>:<period>` for every domain in
  scope (sorted order), plus `recon:<domain>:SNAPSHOT` for every domain that has a snapshot rule in scope.
  If any is held by another session the command refuses with `run already in progress for <lock>` and
  **exit 2**, having written nothing. This covers two runs of the same domain and period, and any two runs
  that both include a snapshot rule of the same domain, even for different periods.
- `recon signoff` takes the **same locks** as a run for that domain and period, so a run and a sign-off cannot
  interleave, and it re-checks the latest run and the open-break count after writing the evidence file
  (`state changed during sign-off; retry` means a run or import slipped in; just repeat the command).
- A run that was killed leaves a `running` row; the next run for the same period and domain marks it `failed`.
  Locks are session-owned and vanish with the connection, so no manual cleanup is needed.

## 8. Review, import and sign-off

1. `recon run` (or `recon export`) writes the workbook. Reviewers edit only **Status**, **Owner** and
   **New comment** on the `Breaks` sheet. Rows may be sorted, filtered or deleted: rows are matched by `break_id`,
   never by position.
2. `recon import <file> [--dry-run]` validates every edited row and applies the valid ones in one transaction.
   - A **blank Owner or Status means "no change"**; a row with all three editable cells blank or unchanged is
     not an edit.
   - Status is `open`, `investigating`, `resolved` or `accepted`; `resolved` and `accepted` require a comment.
     A row is rejected when the break changed since the export (`changed since export`), the transition is not
     allowed, or the Owner is longer than 128 characters.
   - Rejected rows are written to `<file>_rejects.xlsx` with a reason column and the command exits `1`; the
     valid rows are still applied. A file can be imported only once (`file already imported`).
3. `recon signoff --domain D --period P --comment "..."` succeeds only when **all** of these hold, otherwise it
   lists every failed reason and exits 2:
   - the latest run covering the domain and period has status `succeeded` and executed every active rule of the
     domain;
   - no break of the domain for the period (including `SNAPSHOT` breaks) is `open` or `investigating`;
   - the connection's `SUSER_SNAME()` is listed for that domain in `config\signers.yaml`;
   - the period is not already signed off.

   It then writes the evidence workbook to `<evidence_dir>\<domain>\<period>\recon_evidence_<domain>_<period>_<UTC timestamp>.xlsx`,
   marks it read-only, stores its SHA-256 and prints the path.
4. `recon signoff revoke --domain D --period P --reason "..."` (signer only) marks the sign-off `revoked`. The
   evidence file is kept. After a revoke the period can be signed off again; each sign-off gets a new timestamped file.

**The evidence and export folders must be on NTFS (or another file system that supports hard links).**
Evidence files and exports are created by writing a private temporary file in the target folder and then
*hard-linking* it to its final name; the link fails if the name already exists, which is how an existing evidence
file or export is guaranteed never to be overwritten. On a share, make sure the volume behind it is NTFS and test it once
(`fsutil hardlink create C:\path\to\evidence\link.test C:\path\to\evidence\some.file` on the file server, or
a trial `recon signoff` on a test period).

## 9. Folders and permissions

| Folder (default) | Who writes | Who reads | Notes |
|---|---|---|---|
| `exports\` | Service account, every analyst who runs `recon run` / `recon export` | Reviewers | Workbooks contain break details (PII only as `***`) but may name employees and accounts; restrict to the finance and HR reviewers. Safe to clean up: workbooks can be regenerated with `recon export`. |
| `evidence\` | The people who sign off (and the service account if it ever does) | Auditors (read-only) | Evidence is frozen: files are read-only and hashed. IT should grant *create* but not *modify/delete* on files, and keep backups. Must be NTFS (see above). |
| `logs\` | Every account that runs `recon` | Operators | See below. |

Set `export_dir` and `evidence_dir` in `config\settings.yaml` to UNC paths if reviewers work from a share.

## 10. Logs

`<log_dir>\recon.log` (default `C:\recon\logs\recon.log`), a rotating file of 10 MB x 10 files, plus the console.
Every line includes `run_id=` and `rule_id=` where they apply, for example
`... INFO recon.executor run_id=42 rule_id=emp_master_match check emp_master_match: fail, 8 break(s) ...`.
PII values are never logged. The authoritative history is in the `recon` schema (`recon.run`, `recon.run_check`
with the exact generated SQL, `recon.audit_log`), not in the log file.

## 11. Behaviours to know when writing rules

- **Float and real columns:** declare them `type: decimal` in the rule. Comparing a `float` column as text uses
  scientific notation (`1E-05`) and will not match the other side. With `type: decimal` both sides are cast to
  `DECIMAL` and tolerances apply.
- **Money as text:** a `varchar` amount compared with `type: decimal` is cast first; a value that cannot be cast is
  a `value_mismatch` showing the raw text.
- **`like` assertions are case-sensitive** and match the *trimmed* value (so a `CHAR` column's padding is
  ignored); write `[A-Z]` or `[a-z]` as intended.
- **Keys and strings** are trimmed and compared case-insensitively by default (`normalize`), so `e001 ` and
  `E001` are the same key.
- **PII columns** (`pii: true`) are compared by SHA-256 hash and stored, exported and logged only as `***`. They
  cannot be used as keys and cannot have a tolerance.
- **Quality break `field`** is `<assertion>:<column>` for `not_null`, `accepted_values`, `references` and `like`
  (e.g. `not_null:DEPRTMNT`, `references:DEPRTMNT`), `unique:<col>,<col>` for `unique`, and the assertion's `name`
  for `custom_sql`, so two assertions on the same column give separate breaks.
- **Duplicate keys** in a `record_match` dataset yield one `quality_violation` for the key and no other break for it.
- **Mappings:** an ambiguous mapping (two valid rows for one value on the as-of date) is an error for every rule
  that uses it. Effective dating uses the end of the run's period for periodic rules and today for snapshot rules.
- **`max_breaks` (default 5000)** protects against a bad load: more differences than that is an `error` and the
  rule's existing breaks are left untouched.
- Free SQL (`filter`, `measures`, `custom_sql`) is trusted code and goes through pull-request review.

## 12. Troubleshooting

| Message | Cause and action |
|---|---|
| `Cannot connect to the database: ...` | Wrong server/database, missing login, or the ODBC driver is not installed. Check `RECON_DB` / `config\connection.yaml`. |
| `run already in progress for recon:payroll:2026-09` | Another run or sign-off holds that lock; wait for it, or check for an overlapping scheduled task. |
| `stale source wd_pay_jrnl` (check error) | The newest `load_ts` is older than `max_age_hours` (server local time). Fix the staging load; the rule's breaks are untouched. |
| `suspected load failure: N > max_breaks M` | A bad load or a wrong rule. Investigate; raise `max_breaks` in the rule only if the volume is real. |
| `ambiguous mapping <name>: value X has 2 valid rows` | Fix overlapping effective dates in the crosswalk table. |
| `rule R: ...: column 'c' not found in dataset d` | A staging column was renamed or dropped. Preflight refuses the whole run (exit 2, nothing written). |
| `file already imported` | The same file bytes were imported before; re-export for a fresh workbook. |
| `X is not an authorized signer for D` | Add the login to `config\signers.yaml` (compare with `SELECT SUSER_SNAME()`). |
| `evidence file already exists: ...; retry` | Two sign-offs in the same second; run it again. |

## 13. Local development

Everything runs on Linux or macOS against a throwaway SQL Server container.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

docker compose -f docker-compose.test.yml up -d          # SQL Server 2022 on localhost:1433
export RECON_TEST_DSN='Driver={ODBC Driver 18 for SQL Server};Server=localhost,1433;UID=sa;PWD=Recon_Test_Pw1!;TrustServerCertificate=yes'

ruff check .
pytest -m "not integration"      # unit tests, no database
pytest -m integration            # needs RECON_TEST_DSN
pytest                           # both
docker compose -f docker-compose.test.yml down
```

You also need the Microsoft ODBC Driver 18 locally (Linux: `msodbcsql18` from Microsoft's apt repository).
`RECON_TEST_DSN` is a *server-level* connection string with no `Database=`; the integration tests create their own
`recon_test_<hex>` / `recon_e2e_<hex>` databases and drop them afterwards. Without `RECON_TEST_DSN` the
integration tests skip. The password above is for the throwaway container only.

**Synthetic end-to-end data.** `tests/fixtures/synthetic.sql` creates the schemas `stg_wd`, `stg_gp` and `xref`
and every table the fixture project (`tests/fixtures/project`) names, with at least one planted instance of each
break type, a PII difference, an effective-dated mapping change, a duplicate key and fresh `load_ts` values.
`synthetic_clean.sql` loads the same tables with data that reconciles (zero breaks), and
`synthetic_teardown.sql` drops everything. All three are idempotent and split into batches with `GO` lines.
`tests/integration/test_end_to_end.py` drives only the CLI through the whole workflow
(migrate, validate, run, export, edit, import, sign off, run again, revoke) against a fresh database.

To try the planted dataset by hand:

```bash
export RECON_DB='...;Database=recon_scratch'      # an empty scratch database
python - <<'PY'
import os
from recon.db import Database
from recon.migrations import split_batches
db = Database(os.environ["RECON_DB"])
for batch in split_batches(open("tests/fixtures/synthetic.sql", encoding="utf-8").read()):
    db.execute(batch)
PY
cp -r tests/fixtures/project /tmp/recon-demo
recon db migrate --root /tmp/recon-demo
recon run --all --period 2026-09 --root /tmp/recon-demo     # exit 1, 21 breaks
```

**CI** (`.github/workflows/ci.yml`) runs on every pull request and on pushes to `main`: job `unit` (Python 3.11
and 3.13: `ruff check .`, `pytest -m "not integration"`, `recon rules validate --root .`) and job `integration`
(SQL Server 2022 service container, ODBC Driver 18 from Microsoft's apt repository, `pytest -m integration`).
