# Workday ↔ GP Reconciliation Framework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `recon` Python package: YAML-defined reconciliation rules compiled to T-SQL, executed in SQL Server, with persistent break tracking, Excel round-trip, and period sign-off.

**Architecture:** Pydantic-validated YAML → per-check-type SQL generators (pure functions) → an executor that runs each rule in its own transaction and feeds raw differences to a pure-Python break-lifecycle planner, persisted through a `Repository` (SQL implementation plus an in-memory fake for unit tests). Excel export/import and sign-off sit on the `Repository` only. Break volume per rule is capped by `max_breaks` (default 5000), so the lifecycle merge runs in Python rather than as a SQL `MERGE` — same behaviour as the spec, fully unit-testable.

**Tech Stack:** Python ≥3.11, pyodbc ≥5, pydantic ≥2.6, PyYAML ≥6, typer ≥0.12, openpyxl ≥3.1; pytest ≥8, ruff; SQL Server 2022 (Docker) for integration tests; GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-10-15-workday-gp-reconciliation-design.md`

## Global Constraints

- Package lives in `src/recon/`, built with hatchling from `pyproject.toml`; console script `recon = recon.cli:app`; `recon.__version__ = "0.1.0"`.
- Engine writes only to schema `recon`; staging and mapping tables are only ever `SELECT`ed.
- Left side = Workday, right side = GP in every shipped rule and fixture; the engine is side-agnostic.
- Identifier regex: `^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$`; identifiers are always emitted bracket-quoted (`[schema].[table]`, `[col]`).
- Rule `id`: `^[a-z0-9_]+$` and equal to the YAML file stem. Domains: `^[a-z0-9_]+$` (shipped: `payroll`, `employees`, `gl`, `ap`).
- Defaults: `max_breaks = 5000`, `timeout_s = 300`, tolerance `abs = 0`, `pct = 0`, `normalize = {trim: true, case_insensitive: true}`, type `string`.
- Tolerance: within iff `|diff| <= max(abs, pct × |right|)`; `diff = left − right`.
- Snapshot period label is the literal `SNAPSHOT`.
- PII values are stored, exported and logged only as `***`; compared via `HASHBYTES('SHA2_256', …)`. PII columns may not be keys.
- Break statuses: `open`, `investigating`, `resolved`, `accepted`. Break types: `missing_left`, `missing_right`, `value_mismatch`, `mapping_missing`, `mapping_mismatch`, `quality_violation`, `total_mismatch`.
- `break_key = sha256("|".join([rule_id, period_label, business_key, field_name or "", break_type])).hexdigest()`.
- Exit codes: `0` all pass, `1` breaks and no errors, `2` any error / refused command.
- Identity everywhere = `SUSER_SNAME()` of the DB connection.
- Generated SQL never interpolates run parameters: every batch starts with `PARAM_HEADER` (Task 5) and receives `(period_start, period_end_excl, as_of)` as bound `?` params.
- Logging: `logs/recon.log`, `RotatingFileHandler(maxBytes=10_000_000, backupCount=10)` + console; format includes `run_id` and `rule_id`.
- Integration tests are marked `@pytest.mark.integration` and skip unless env `RECON_TEST_DSN` is set.
- Commit messages end with the session's attribution trailer lines.

## Review Focus

1. **Datetime period columns** — a GP `TRXDATE` of `2026-09-30 14:35` must fall in period `2026-09`. Period predicates use `>= @period_start AND < @period_end_excl` (Task 4 unit test + Task 5 integration test).
2. **Money stored as text or float** — `'100.00'` (varchar) vs `100.004` (float) with `abs: 0.01` is a pass; `'abc'` vs `5` is a `value_mismatch` showing `abc` (Task 5 integration test).
3. **Key whitespace/case** — Workday `e001 ` and GP `E001` are the same employee, not a missing-left + missing-right pair (Task 5 integration test).
4. **Composite keys containing the separator** — keys `('A|B','C')` and `('A','B|C')` must produce different `business_key`s (Task 5 unit test on `business_key_expr`, integration test).
5. **User-rearranged workbooks** — rows sorted, filtered, deleted or blank rows added in Excel must import by `break_id`, not row position; old exports remain importable after a nightly run that merely re-saw the breaks (Task 12 tests).

---

## File Structure

```
pyproject.toml                       # deps, ruff, pytest markers, console script
.gitignore                           # logs/, exports/, evidence/, .venv, __pycache__, *.egg-info
docker-compose.test.yml              # mssql 2022 for local integration tests
config/                              # example project config (shipped)
  connection.yaml  settings.yaml  datasets.yaml  mappings.yaml  signers.yaml
rules/{payroll,employees,gl,ap}/.gitkeep
sql/migrations/V001__init.sql
src/recon/
  __init__.py          # __version__
  periods.py           # RunPeriod, resolve_period, SNAPSHOT
  logging_setup.py     # configure_logging, log context
  config/models.py     # pydantic models for config + rules
  config/loader.py     # load_project, Project, ConfigError, rule_version_hash
  db.py                # Database, LockBusy, connection_string
  migrations.py        # apply_migrations
  connectors/base.py   # Column, Connector protocol
  connectors/sqlserver.py
  connectors/__init__.py   # get_connector registry
  sqlgen/common.py     # PARAM_HEADER, GenContext, GeneratedSql, expression helpers
  sqlgen/record_match.py  sqlgen/control_total.py  sqlgen/mapping.py  sqlgen/quality.py
  sqlgen/__init__.py   # generate() dispatch
  model.py             # dataclasses shared by breaks/repository/excel/signoff
  breaks.py            # break_key, plan_merge, validate_user_transition
  repository.py        # Repository protocol + SqlRepository
  planner.py           # select_rules, rule_period_label, run_status, exit_code
  executor.py          # execute_run
  excel/layout.py      # sheet + column constants shared by export/import
  excel/export.py      # export_workbook
  excel/importer.py    # import_workbook
  signoff.py           # sign_off, revoke
  cli.py               # typer app
tests/
  fakes.py             # InMemoryRepository
  unit/...  integration/...  fixtures/project/...  fixtures/synthetic.sql
docs/connectors.md  docs/operations.md
.github/workflows/ci.yml
```

---

### Task 1: Project skeleton, periods, logging

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `src/recon/__init__.py`, `src/recon/periods.py`, `src/recon/logging_setup.py`
- Test: `tests/unit/test_periods.py`, `tests/unit/test_logging.py`

**Interfaces:**
- Produces: `RunPeriod(label: str, start: date, end: date)` (frozen dataclass; `end` inclusive) with property `end_excl -> date` (`end + 1 day`); `SNAPSHOT = "SNAPSHOT"`; `resolve_period(token: str, today: date) -> RunPeriod`; `configure_logging(log_dir: Path) -> None`; `log_context(**fields) -> ContextManager` that adds `run_id`/`rule_id` to records (default `-`).

- [ ] **Step 1: Write failing tests**

```python
def test_month_token():
    p = resolve_period("2026-09", today=date(2026, 10, 16))
    assert (p.label, p.start, p.end, p.end_excl) == ("2026-09", date(2026,9,1), date(2026,9,30), date(2026,10,1))

def test_current_and_previous_cross_year():
    assert resolve_period("current", today=date(2026, 1, 5)).label == "2026-01"
    assert resolve_period("previous", today=date(2026, 1, 5)).label == "2025-12"

@pytest.mark.parametrize("bad", ["2026-13", "2026-9", "sept", ""])
def test_invalid_tokens(bad):
    with pytest.raises(ValueError):
        resolve_period(bad, today=date(2026, 10, 16))

def test_log_context_fields(tmp_path):
    configure_logging(tmp_path)
    with log_context(run_id=7, rule_id="emp_master_match"):
        logging.getLogger("recon").info("hello")
    assert "run_id=7" in (tmp_path / "recon.log").read_text()
```

- [ ] **Step 2: Run** `pytest tests/unit -v` — expect FAIL (`ModuleNotFoundError: recon`).
- [ ] **Step 3: Implement.** `pyproject.toml` declares deps from Tech Stack, `[project.optional-dependencies] dev = ["pytest>=8", "ruff"]`, `[tool.pytest.ini_options] markers = ["integration: needs SQL Server via RECON_TEST_DSN"]`, `pythonpath = ["src"]`, ruff line-length 110. Logging per Global Constraints (contextvar-based filter).
- [ ] **Step 4: Run** `pip install -e '.[dev]' && pytest tests/unit -v && ruff check .` — expect PASS.
- [ ] **Step 5: Commit** `feat: project skeleton, period resolution, logging`

---

### Task 2: Config and rule models, project loader, `recon rules validate`

**Files:**
- Create: `src/recon/config/__init__.py`, `src/recon/config/models.py`, `src/recon/config/loader.py`, `src/recon/cli.py`
- Create: `config/*.yaml` (shipped examples; `connection.yaml` with `driver`, `server`, `database`, `trusted_connection: true`; `settings.yaml` with `export_dir: exports`, `evidence_dir: evidence`, `log_dir: logs`)
- Create fixture: `tests/fixtures/project/` with `config/` (datasets `wd_workers`, `gp_employees`, `gp_departments`, `wd_pay_jrnl`, `gp_gl_pay`; mappings `cc_to_dept` with `effective`, `wd_acct_to_gp`; signers `{employees: ["TEST\\signer"], payroll: ["TEST\\signer"]}`) and one rule per check type: `rules/employees/emp_master_match.yaml` (spec §4.3 example), `rules/payroll/pay_jrnl_totals.yaml` (§4.4), `rules/employees/emp_cost_center_map.yaml` (§4.5), `rules/employees/gp_emp_quality.yaml` (§4.6)
- Test: `tests/unit/test_config.py`, `tests/unit/test_cli_rules.py`

**Interfaces:**
- Produces (models.py): `SourceConfig(connector: str)`; `DatasetConfig(name, source, object, period_column: str|None, filter: str|None, freshness: Freshness|None)`; `Freshness(column, max_age_hours: int)`; `MappingConfig(name, object, from_: str (alias "from"), to: str, effective: Effective|None)`; `Effective(start, end)`; `Tolerance(abs: Decimal=0, pct: Decimal=0)`; `KeyPair(left, right)`; `CompareField(left, right, type: Literal[string,decimal,int,date,datetime,bool]="string", tolerance: Tolerance|None, via_mapping: str|None, pii: bool=False)`; `Normalize(trim=True, case_insensitive=True)`; `Measure(name, left, right, tolerance: Tolerance|None)`; `GroupBy(left, right, via_mapping: str|None)`; assertion models `NotNull(columns)`, `Unique(columns)`, `AcceptedValues(column, values: list)`, `References(column, dataset, ref_column)`, `Like(column, pattern)`, `CustomSql(name, sql)`; rules `RecordMatchRule`, `ControlTotalRule`, `MappingRule`, `QualityRule` sharing base fields (`id, domain, check_type, description="", owner="", severity: Literal[high,medium,low], active=True, max_breaks=5000, timeout_s=300`) and a discriminated union `Rule` on `check_type`. Rule-level helper: `Rule.dataset_names() -> list[str]`. All models `extra="forbid"`.
- Produces (loader.py): `Settings(export_dir: Path, evidence_dir: Path, log_dir: Path)` (relative paths resolved against project root); `Project(root, sources, datasets, mappings, rules: dict[str, Rule], rule_text: dict[str, str], signers: dict[str, list[str]], settings)`; `ConfigError(Exception)` with `.issues: list[str]`, each formatted `"<relative file>: <location>: <message>"`; `load_project(root: Path) -> Project`; `rule_version_hash(rule: Rule) -> str` (sha256 of `rule.model_dump_json()` with sorted keys).
- Produces (cli.py): `app = typer.Typer()`; command `rules validate --root PATH` → prints `OK: N rules` exit 0, or each issue and exit 2.

- [ ] **Step 1: Write failing tests**

```python
def test_fixture_loads(fixture_root):
    p = load_project(fixture_root)
    assert sorted(p.rules) == ["emp_cost_center_map", "emp_master_match", "gp_emp_quality", "pay_jrnl_totals"]
    r = p.rules["emp_master_match"]
    assert r.max_breaks == 5000 and r.compare[0].tolerance is None and r.normalize.trim is True

def test_quality_assertion_short_form_parses(fixture_root):
    q = load_project(fixture_root).rules["gp_emp_quality"]
    assert [type(a).__name__ for a in q.assertions][:2] == ["NotNull", "Unique"]

@pytest.mark.parametrize("mutation, expected", [
    ({"surprise": 1}, "Extra inputs are not permitted"),
    ({"left": "no_such_dataset"}, "unknown dataset 'no_such_dataset'"),
    ({"keys": [{"left": "employee_id;drop", "right": "EMPLOYID"}]}, "invalid identifier"),
    ({"id": "other_name"}, "id 'other_name' must match file name 'emp_master_match'"),
])
def test_rule_errors_name_file(tmp_fixture, mutation, expected):
    edit_rule(tmp_fixture, "employees/emp_master_match.yaml", mutation)
    with pytest.raises(ConfigError) as e:
        load_project(tmp_fixture)
    assert any("rules/employees/emp_master_match.yaml" in i and expected in i for i in e.value.issues)

def test_pii_key_rejected(tmp_fixture):  # compare field national_id is pii; using it as a key is an error
    edit_rule(tmp_fixture, "employees/emp_master_match.yaml", {"keys": [{"left": "national_id", "right": "SOCSCNUM"}]})
    with pytest.raises(ConfigError, match="pii"): load_project(tmp_fixture)

def test_unknown_mapping_and_duplicate_ids(tmp_fixture): ...   # "unknown mapping 'x'", "duplicate rule id"

def test_version_hash_stable_and_sensitive(fixture_root):
    r = load_project(fixture_root).rules["emp_master_match"]
    assert rule_version_hash(r) == rule_version_hash(r.model_copy(deep=True))
    changed = r.model_copy(update={"max_breaks": 10})
    assert rule_version_hash(changed) != rule_version_hash(r)

def test_cli_validate(fixture_root, tmp_fixture):
    assert CliRunner().invoke(app, ["rules", "validate", "--root", str(fixture_root)]).exit_code == 0
    edit_rule(tmp_fixture, "employees/emp_master_match.yaml", {"surprise": 1})
    res = CliRunner().invoke(app, ["rules", "validate", "--root", str(tmp_fixture)])
    assert res.exit_code == 2 and "emp_master_match.yaml" in res.output
```

`fixture_root`, `tmp_fixture` (copy into `tmp_path`) and `edit_rule(root, relpath, updates)` (load YAML, `dict.update`, dump) live in `tests/conftest.py`.

- [ ] **Step 2: Run** `pytest tests/unit/test_config.py tests/unit/test_cli_rules.py -v` — expect FAIL.
- [ ] **Step 3: Implement** models, loader (collect *all* issues before raising; cross-reference checks: datasets, mappings, `References.dataset`, `target.dataset`, pii-in-keys; rule YAML text kept verbatim in `rule_text`), and the CLI command.
- [ ] **Step 4: Run** the same tests — expect PASS. Also `recon rules validate --root config/..` on the shipped (empty-rules) config → `OK: 0 rules`.
- [ ] **Step 5: Commit** `feat: config/rule models, project loader, rules validate`

---

### Task 3: Database access, `recon` schema migration, integration harness

**Files:**
- Create: `src/recon/db.py`, `src/recon/migrations.py`, `sql/migrations/V001__init.sql`, `docker-compose.test.yml`, `tests/integration/conftest.py`
- Modify: `src/recon/cli.py` (add `db migrate --root PATH`)
- Test: `tests/unit/test_db_config.py`, `tests/integration/test_migrations.py`

**Interfaces:**
- Produces: `connection_string(root: Path, env: Mapping[str, str]) -> str` — env `RECON_DB` wins; otherwise built from `config/connection.yaml` (`Driver={...};Server=...;Database=...;Trusted_Connection=yes;TrustServerCertificate=yes`).
- Produces: `class Database` with `__init__(conn_str: str)`, `query(sql: str, *params) -> list[dict]`, `scalar(sql, *params)`, `execute(sql, *params) -> int`, `transaction() -> ContextManager` (commit on success, rollback on exception; `autocommit=False` connection), `set_timeout(seconds: int)`, `current_user() -> str` (`SELECT SUSER_SNAME()`), `applock(resource: str) -> ContextManager` (`sp_getapplock @LockMode='Exclusive', @LockOwner='Session', @LockTimeout=0`; raises `LockBusy(resource)` when result < 0; releases on exit).
- Produces: `apply_migrations(db: Database, migrations_dir: Path) -> list[str]` — applies `V###__*.sql` files in order not yet in `recon.schema_version(version VARCHAR(20) PK, applied_at DATETIME2)`; splits files on lines matching `^\s*GO\s*$`.
- Produces (DDL, `V001__init.sql`): schema `recon` and every table in spec §3 with these concrete choices: identity `BIGINT` PKs; `business_key NVARCHAR(400)`, `field_name NVARCHAR(128)`, `left_value`/`right_value NVARCHAR(4000)`, `diff DECIMAL(38,6)`, `generated_sql NVARCHAR(MAX)`, `yaml_text NVARCHAR(MAX)`, all timestamps `DATETIME2(3) DEFAULT SYSUTCDATETIME()`; `recon.break.break_key CHAR(64) UNIQUE`; `recon.break.version INT NOT NULL DEFAULT 1`; `recon.import_batch.file_sha256 CHAR(64) UNIQUE`; filtered unique index on `recon.signoff(domain, period_label) WHERE status = 'signed'`; CHECK constraints for status/break_type/result/run status enumerations; roles `recon_engine` and `recon_reader` per spec §3.3 (role creation guarded with `IF DATABASE_PRINCIPAL_ID(...) IS NULL`; staging-schema grants are documented in `docs/operations.md`, not hard-coded).
- Produces (tests/integration/conftest.py): session fixtures `dsn` (connection string of the per-session test database) and `db` — skips unless `RECON_TEST_DSN`; creates database `recon_test_<8 hex>`, applies migrations, yields `Database`, drops it afterwards; helper `run_sql_file(db, path)`.

- [ ] **Step 1: Write failing tests**

```python
def test_env_overrides_file(tmp_path):
    write_connection_yaml(tmp_path, server="sql01", database="Fin")
    assert connection_string(tmp_path, {"RECON_DB": "DSN=x"}) == "DSN=x"
    cs = connection_string(tmp_path, {})
    assert "Server=sql01" in cs and "Database=Fin" in cs and "Trusted_Connection=yes" in cs

@pytest.mark.integration
def test_migrations_idempotent(db):
    assert apply_migrations(db, MIGRATIONS) == []          # fixture already applied V001
    tables = {r["name"] for r in db.query("SELECT name FROM sys.tables WHERE schema_id = SCHEMA_ID('recon')")}
    assert {"rule","run","run_check","total_result","break","break_occurrence","break_comment",
            "audit_log","import_batch","signoff","schema_version"} <= tables

@pytest.mark.integration
def test_applock_is_exclusive(db, dsn):
    other = Database(dsn)
    with db.applock("recon:payroll:2026-09"):
        with pytest.raises(LockBusy):
            with other.applock("recon:payroll:2026-09"): pass
```

- [ ] **Step 2: Run** `pytest tests/unit/test_db_config.py -v` — FAIL. (Integration: `docker compose -f docker-compose.test.yml up -d`, install ODBC Driver 18, `export RECON_TEST_DSN=...`; documented in `docs/operations.md` § Development.)
- [ ] **Step 3: Implement** `db.py`, `migrations.py`, DDL, compose file (image `mcr.microsoft.com/mssql/server:2022-latest`, `ACCEPT_EULA=Y`, port 1433), `recon db migrate`.
- [ ] **Step 4: Run** unit tests — PASS; integration tests — PASS when a DSN is available (record in the task report whether they ran).
- [ ] **Step 5: Commit** `feat: database layer and recon schema migration`

---

### Task 4: Connector interface and SQL Server connector

**Files:**
- Create: `src/recon/connectors/base.py`, `src/recon/connectors/sqlserver.py`, `src/recon/connectors/__init__.py`, `docs/connectors.md`
- Test: `tests/unit/test_connectors.py`, `tests/integration/test_sqlserver_connector.py`

**Interfaces:**
- Consumes: `DatasetConfig` (Task 2), `Database` (Task 3).
- Produces: `Column(name: str, data_type: str)`; `class Connector(Protocol): name: str; describe(dataset: DatasetConfig) -> list[Column]; as_sql_source(dataset: DatasetConfig) -> str`; `SqlServerConnector(db: Database)`; `get_connector(name: str, db: Database) -> Connector` (registry dict `{"sqlserver": SqlServerConnector}`; unknown → `ConfigError`).
- `as_sql_source` returns a parenthesised derived table **without alias**: `(SELECT * FROM [schema].[table] WHERE <predicates>)`; predicates are `(<filter>)` if set and, if `period_column` set, `[col] >= @period_start AND [col] < @period_end_excl`; no `WHERE` when no predicates.
- `describe` queries `INFORMATION_SCHEMA.COLUMNS`; raises `ConfigError(["dataset '<name>': object '<object>' not found"])` when no columns.

- [ ] **Step 1: Write failing tests**

```python
def test_snapshot_source():
    ds = DatasetConfig(name="wd_workers", source="workday", object="stg_wd.workers")
    assert SqlServerConnector(None).as_sql_source(ds) == "(SELECT * FROM [stg_wd].[workers])"

def test_periodic_source_uses_half_open_range_and_filter():
    ds = DatasetConfig(name="gp_gl_pay", source="gp", object="stg_gp.GL20000",
                       period_column="TRXDATE", filter="SOURCDOC = 'UPRCC'")
    assert SqlServerConnector(None).as_sql_source(ds) == (
        "(SELECT * FROM [stg_gp].[GL20000] WHERE (SOURCDOC = 'UPRCC') "
        "AND [TRXDATE] >= @period_start AND [TRXDATE] < @period_end_excl)")

@pytest.mark.integration
def test_describe(db):
    db.execute("CREATE SCHEMA t_conn"); db.execute("CREATE TABLE t_conn.x (a INT, b NVARCHAR(10))")
    cols = SqlServerConnector(db).describe(DatasetConfig(name="x", source="s", object="t_conn.x"))
    assert [(c.name, c.data_type) for c in cols] == [("a", "int"), ("b", "nvarchar")]
```

- [ ] **Step 2: Run** — FAIL. **Step 3: Implement.** `docs/connectors.md` explains the protocol and the landing-table pattern (spec §9).
- [ ] **Step 4: Run** — PASS. **Step 5: Commit** `feat: connector interface and SQL Server connector`

---

### Task 5: SQL generation core and `record_match`

**Files:**
- Create: `src/recon/sqlgen/__init__.py`, `src/recon/sqlgen/common.py`, `src/recon/sqlgen/record_match.py`
- Test: `tests/unit/sqlgen/conftest.py` (golden helper), `tests/unit/sqlgen/test_common.py`, `tests/unit/sqlgen/test_record_match.py`, `tests/unit/sqlgen/golden/*.sql`, `tests/integration/test_record_match_sql.py`

**Interfaces:**
- Produces (common.py):
  - `PARAM_HEADER = "DECLARE @period_start DATE = ?, @period_end_excl DATE = ?, @as_of DATE = ?;\n"`; `params_for(period: RunPeriod | None, today: date) -> tuple` (snapshot: `(None, None, today)`; else `(start, end_excl, end)`).
  - `GenContext(datasets: dict[str, DatasetConfig], sources: dict[str, str], mappings: dict[str, MappingConfig])`.
  - `GeneratedSql(checks_sql: str | None, counts_sql: str, diff_sql: str, totals_sql: str | None = None)` with `full_text() -> str` (all non-null parts, each prefixed by `PARAM_HEADER`, joined with `\nGO\n` — this is what is stored as `generated_sql`).
  - **Diff contract:** `diff_sql` is a single `SELECT` returning exactly `business_key NVARCHAR(400), break_type VARCHAR(30), field_name NVARCHAR(128) NULL, left_value NVARCHAR(4000) NULL, right_value NVARCHAR(4000) NULL, diff DECIMAL(38,6) NULL`. `counts_sql` returns one row `rows_left BIGINT, rows_right BIGINT`. `checks_sql` contains `THROW 50001, '<message>', 1` guards.
  - Helpers: `qi(identifier) -> str` (bracket-quote, validates regex); `norm_string(expr, normalize) -> str` (`NULLIF(LTRIM(RTRIM(x)), '')`, `UPPER(...)` when case-insensitive; `CONVERT(NVARCHAR(4000), x)` first); `typed(expr, type) -> str` (`TRY_CAST(... AS DECIMAL(38,6) | BIGINT | DATE | DATETIME2(3) | BIT)`; string → `norm_string`); `business_key_expr(exprs: list[str]) -> str` (each part `REPLACE(REPLACE(ISNULL(p, N'∅'), N'\', N'\\'), N'|', N'\|')`, joined with `N'|'`); `pii_hash(expr) -> str` (`HASHBYTES('SHA2_256', expr)`); `mapping_source(m: MappingConfig) -> str` (with `effective`: `WHERE [start] <= @as_of AND ([end] IS NULL OR [end] >= @as_of)`); `mapping_ambiguity_check(m) -> str` (THROW `'ambiguous mapping <name>: value <v> has N valid rows'` when any `from` value has >1 valid row).
- Produces (record_match.py): `generate_record_match(rule: RecordMatchRule, ctx: GenContext) -> GeneratedSql`. Algorithm (not implied by the signature):
  1. CTEs `L`/`R`: `business_key_expr` over normalized keys, plus per compare field the typed value, the raw value (`CONVERT(NVARCHAR(4000), col)`), and for `via_mapping` the mapped value (LEFT JOIN mapping on normalized `from`) and a `mapped` flag.
  2. `dup` CTE: keys with `COUNT(*) > 1` on either side → `quality_violation`, `field_name = '__key__'`, `left_value`/`right_value` = counts.
  3. Non-duplicate keys FULL OUTER JOIN: missing on left → `missing_left`; missing on right → `missing_right`.
  4. Matched keys `CROSS APPLY (VALUES ...)` one row per compare field; emit `mapping_missing` when the left value is non-null and unmapped; otherwise `value_mismatch` when not equal: null-safe equality; a failed `TRY_CAST` with non-null raw value counts as unequal and shows the raw value; decimal fields use the tolerance rule with `diff = left − right`; PII fields compare `pii_hash` and output `'***'` for both values.
  5. `counts_sql`: `SELECT (SELECT COUNT(*) FROM <left>) AS rows_left, (SELECT COUNT(*) FROM <right>) AS rows_right`.
- Produces (`sqlgen/__init__.py`): `generate(rule: Rule, ctx: GenContext) -> GeneratedSql` dispatching on `check_type` (other types added in Tasks 6–7).

- [ ] **Step 1: Write failing tests**

```python
def test_business_key_escapes_separator():
    sql = business_key_expr(["a", "b"])
    assert "N'\\|'" in sql and sql.count("N'|'") == 1

def test_record_match_golden(fixture_project, golden):
    rule = fixture_project.rules["emp_master_match"]
    golden("record_match_emp_master", generate(rule, ctx_for(fixture_project)).full_text())

def test_identifiers_quoted_and_params_not_interpolated(fixture_project):
    text = generate(fixture_project.rules["emp_master_match"], ctx_for(fixture_project)).full_text()
    assert "[EMPLOYID]" in text and "2026" not in text and text.startswith(PARAM_HEADER)
```

`golden(name, text)` compares with `golden/<name>.sql`, writing it when `UPDATE_GOLDEN=1`. The reviewer reads the golden file as part of this task's review.

Integration (`test_record_match_sql.py`): create `t_wd.workers(employee_id, cost_center, hire_date, annual_salary, national_id)` and `t_gp.employees(EMPLOYID, DEPRTMNT, STRTDATE, SALARY, SOCSCNUM)` with one row per scenario below, run `PARAM_HEADER + diff_sql` with `params_for(...)`, and assert the exact diff set:

| Scenario | Expected diff |
|---|---|
| identical row | none |
| `'e001 '` vs `'E001'` keys | none (Review Focus 3) |
| salary `'100.00'` (nvarchar) vs `100.004` (float), tol 0.01 | none (Review Focus 2) |
| salary `'100.00'` vs `100.02` | `value_mismatch` `annual_salary`, diff `-0.020000` |
| salary `'abc'` vs `5` | `value_mismatch`, `left_value='abc'` |
| only in GP | `missing_left` |
| only in Workday | `missing_right` |
| national_id differs | `value_mismatch`, values `***` |
| cost center with no crosswalk row | `mapping_missing` |
| cost center mapped to a different dept | `value_mismatch` `cost_center` |
| duplicate GP key | `quality_violation` `__key__`, no other diffs for that key |
| composite-key variant rule with `('A|B','C')` and `('A','B|C')` on both sides | none (each matches itself) (Review Focus 4) |
| effective-dated mapping with two valid rows | executing `checks_sql` raises with "ambiguous mapping cc_to_dept" |

Also add `test_period_datetime_boundary`: a periodic source with `TRXDATE = '2026-09-30T14:35'` is included for period `2026-09` and excluded for `2026-10` (Review Focus 1).

- [ ] **Step 2: Run** `pytest tests/unit/sqlgen -v` — FAIL. **Step 3: Implement.** **Step 4: Run** unit (PASS, golden generated and reviewed) and integration (PASS when DSN available).
- [ ] **Step 5: Commit** `feat: SQL generation core and record_match`

---

### Task 6: `control_total` generator

**Files:**
- Create: `src/recon/sqlgen/control_total.py`; Modify: `src/recon/sqlgen/__init__.py`
- Test: `tests/unit/sqlgen/test_control_total.py`, golden `control_total_pay_jrnl.sql`, `tests/integration/test_control_total_sql.py`

**Interfaces:**
- Consumes: Task 5 helpers and contract.
- Produces: `generate_control_total(rule: ControlTotalRule, ctx: GenContext) -> GeneratedSql` where `totals_sql` is a `SELECT` returning `group_key NVARCHAR(400), measure NVARCHAR(128), left_value DECIMAL(38,6), right_value DECIMAL(38,6), diff DECIMAL(38,6), tolerance_applied DECIMAL(38,6), within_tolerance BIT`, and `diff_sql` reads from temp table `#totals` (filled by the executor): `SELECT group_key AS business_key, 'total_mismatch', measure, CONVERT(NVARCHAR(4000), left_value), CONVERT(NVARCHAR(4000), right_value), diff FROM #totals WHERE within_tolerance = 0`.
- Rules: groups FULL OUTER JOINed on `business_key_expr` of normalized group expressions (left side through `via_mapping` when set); a group missing on one side uses 0; no `group_by` → single group `'ALL'`; `tolerance_applied = max(abs, pct × |right|)`.

- [ ] **Step 1: Tests.** Golden test for the fixture rule. Integration: journal lines for accounts 6000/6100 vs GP lines; assert `#totals` rows for `('6000','net_amount')` within tolerance at 0.004 difference, `('6100','net_amount')` out of tolerance, account present only in GP compared against 0 with `left_value = 0`, and `line_count` rows exact.
- [ ] **Step 2–4:** Run (FAIL) → implement → run (PASS).
- [ ] **Step 5: Commit** `feat: control_total SQL generator`

---

### Task 7: `mapping` and `quality` generators

**Files:**
- Create: `src/recon/sqlgen/mapping.py`, `src/recon/sqlgen/quality.py`; Modify: `src/recon/sqlgen/__init__.py`
- Test: `tests/unit/sqlgen/test_mapping.py`, `tests/unit/sqlgen/test_quality.py`, goldens, `tests/integration/test_mapping_quality_sql.py`

**Interfaces:**
- Produces: `generate_mapping(rule: MappingRule, ctx) -> GeneratedSql` — distinct normalized non-null source values; unmapped → `mapping_missing`; mapped value not in normalized target column → `mapping_mismatch` (`left_value` = source value, `right_value` = mapped value); `business_key` = source value; `counts_sql` → distinct source values / distinct target values.
- Produces: `generate_quality(rule: QualityRule, ctx) -> GeneratedSql` — `UNION ALL` of one `SELECT` per assertion, all `break_type = 'quality_violation'`, `business_key = business_key_expr(rule.key)`:
  - `not_null`: one row per (row, column) where null/blank; `field_name` = column.
  - `unique`: one row per duplicated value group; `business_key` = the unique columns' key expr; `field_name = 'unique:' + ','.join(columns)`; `left_value` = count.
  - `accepted_values`: `field_name` = column, `left_value` = actual value (values compared as normalized strings).
  - `references`: rows whose non-null value has no match in the referenced dataset column; `field_name` = column.
  - `like`: `NOT LIKE` pattern on non-null values; `field_name` = column.
  - `custom_sql`: `{dataset}` replaced with the source text; wrapped as `SELECT CONVERT(NVARCHAR(400), business_key), 'quality_violation', N'<name>', NULL, NULL, NULL FROM (<sql>) c`.
  - `counts_sql`: `rows_left` = dataset row count, `rows_right` = 0.

- [ ] **Step 1: Tests.** Golden per rule. Integration: one planted violation per assertion type plus clean rows; assert exact `(business_key, field_name)` set; `custom_sql` missing a `business_key` column raises a SQL error (surfaced later as a check error).
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat: mapping and quality SQL generators`

---

### Task 8: Break lifecycle (pure)

**Files:**
- Create: `src/recon/model.py`, `src/recon/breaks.py`
- Test: `tests/unit/test_breaks.py`

**Interfaces:**
- Produces (model.py, frozen dataclasses unless noted):
  - `ObservedDiff(business_key, break_type, field_name: str|None, left_value: str|None, right_value: str|None, diff: Decimal|None)`
  - `BreakRecord(break_id: int, break_key, rule_id, domain, period_label, business_key, break_type, field_name, left_value, right_value, diff, status, owner: str|None, resolution_type: str|None, post_signoff: bool, first_seen_run_id: int, last_seen_run_id: int, resolved_run_id: int|None, version: int, severity: str)`
  - `BreakChange(kind: Literal["create","seen","reopen","auto_clear"], break_key: str, rule_id, domain, period_label, observed: ObservedDiff|None, existing: BreakRecord|None, post_signoff: bool)`
  - `CheckRecord(run_id, rule_id, version_hash, result: Literal["pass","fail","error"], rows_left: int|None, rows_right: int|None, break_count: int, generated_sql: str, duration_ms: int, error_message: str|None)`
  - `TotalRow(group_key, measure, left_value: Decimal, right_value: Decimal, diff: Decimal, tolerance_applied: Decimal, within_tolerance: bool)`
  - `RunRow(run_id, trigger, domain: str|None, period_label, status, run_by, started_at: datetime, finished_at: datetime|None)`
  - `UserEdit(break_id: int, expected_version: int, new_status: str|None, new_owner: str|None, comment: str|None)`
  - `Comment(break_id, text, author, created_at: datetime)`; `AuditEntry(entity, entity_id, field, old_value, new_value, changed_by, changed_at, source)`
  - `SignoffRow(signoff_id: int|None, domain, period_label, run_id, signed_by, signed_at: datetime, comment, open_break_count: int, accepted_break_count: int, evidence_path: str, evidence_sha256: str, status: str)`
- Produces (breaks.py):
  - `break_key(rule_id, period_label, business_key, field_name, break_type) -> str` (Global Constraints formula).
  - `plan_merge(existing: list[BreakRecord], observed: list[ObservedDiff], *, rule_id: str, domain: str, period_label: str, signed_off: bool) -> list[BreakChange]` — implements spec §6 engine table exactly; observed duplicates with the same key are collapsed (first wins); `post_signoff = signed_off` on `create` only.
  - `validate_user_transition(current: str, new: str, comment: str|None) -> str | None` — returns the reject reason string (`"transition open->open not allowed"` style, `"comment required for accepted"`), or `None`; same-status is not a transition (callers skip it).

- [ ] **Step 1: Write failing tests** — table-driven over spec §6:

```python
@pytest.mark.parametrize("status, present, values_changed, expected_kind", [
    (None, True, False, "create"),
    ("open", True, False, "seen"), ("investigating", True, True, "seen"),
    ("open", False, False, "auto_clear"), ("investigating", False, False, "auto_clear"),
    ("resolved", True, False, "reopen"), ("resolved", False, False, None),
    ("accepted", True, False, "seen"), ("accepted", True, True, "reopen"),
    ("accepted", False, False, "auto_clear"),
])
def test_engine_transitions(status, present, values_changed, expected_kind): ...

def test_rerun_same_data_is_idempotent():   # seen-only changes, no creates
def test_duplicate_observed_collapsed():
def test_post_signoff_only_on_create():

@pytest.mark.parametrize("cur, new, comment, reason", [
    ("open", "resolved", None, "comment required for resolved"),
    ("open", "accepted", "", "comment required for accepted"),
    ("resolved", "accepted", "x", "transition resolved->accepted not allowed"),
    ("accepted", "open", None, None), ("open", "investigating", None, None),
])
def test_user_transitions(cur, new, comment, reason):
    assert validate_user_transition(cur, new, comment) == reason
```

- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5: Commit** `feat: break lifecycle planning`

---

### Task 9: Repository (SQL implementation + in-memory fake)

**Files:**
- Create: `src/recon/repository.py`, `tests/fakes.py`
- Test: `tests/unit/test_fake_repository.py`, `tests/integration/test_sql_repository.py` (the same contract suite run against both via a parametrized `repo` fixture)

**Interfaces:**
- Consumes: Task 8 models, `Database`.
- Produces: `class Repository(Protocol)` and `SqlRepository(db: Database)`, `InMemoryRepository(user: str = "TEST\\user")`:

```python
def current_user(self) -> str
def upsert_rule(self, rule_id: str, version_hash: str, domain: str, check_type: str, severity: str, owner: str, yaml_text: str, active: bool) -> None
def open_run(self, trigger: str, domain: str | None, period_label: str, period_start: date | None, period_end: date | None, git_commit: str | None, engine_version: str) -> int
def close_run(self, run_id: int, status: str) -> None
def fail_orphan_runs(self, period_label: str, domains: list[str]) -> int   # status running, same period, domain in list or NULL
def record_check(self, rec: CheckRecord) -> int
def insert_totals(self, run_check_id: int, rows: list[TotalRow]) -> None
def latest_run(self, domain: str, period_label: str) -> RunRow | None      # domain = D or NULL (all-domain run)
def checks_for_run(self, run_id: int) -> list[CheckRecord]
def totals_for_run(self, run_id: int, domain: str | None) -> list[TotalRow]
def load_breaks(self, rule_id: str, period_label: str) -> list[BreakRecord]
def apply_engine_changes(self, changes: list[BreakChange], run_id: int) -> None
def breaks_for(self, domain: str | None, period_label: str, statuses: set[str] | None = None, resolved_in_run: int | None = None) -> list[BreakRecord]  # includes SNAPSHOT breaks of the domain
def get_break(self, break_id: int) -> BreakRecord | None
def import_seen(self, file_sha256: str) -> bool
def apply_user_edits(self, edits: list[UserEdit], file_name: str, file_sha256: str, rows_rejected: int) -> int  # returns import_batch_id; one transaction
def comments_for(self, break_ids: list[int]) -> dict[int, list[Comment]]
def audit_for(self, domain: str, period_label: str) -> list[AuditEntry]
def active_signoff(self, domain: str, period_label: str) -> SignoffRow | None
def insert_signoff(self, row: SignoffRow) -> int
def revoke_signoff(self, signoff_id: int, reason: str) -> None
```

- Semantics both implementations must honour (pinned by the contract suite): `apply_engine_changes` inserts a `break_occurrence` for `create`/`seen`/`reopen`; bumps `version` only for `reopen`/`auto_clear`; `seen` updates values and `last_seen_run_id` without bumping `version`; `auto_clear` sets `resolution_type='auto_cleared'`, `resolved_run_id`; status changes write `audit_log` (`source='engine'`). `apply_user_edits` bumps `version` per edited break, sets `resolution_type='manual'` when status becomes `resolved`, inserts comments, writes audit (`source='import'`, `import_batch_id`), records the batch with `imported_by = current_user()`. `severity` on `BreakRecord` comes from the latest `recon.rule` row for the rule.

- [ ] **Step 1: Write the contract tests** (parametrized over fake and, when DSN set, SQL): create → seen (version unchanged, 2 occurrences) → auto_clear (version 2, resolved) → reopen (version 3); user edit with comment writes comment + audit + batch; `import_seen` true after a batch; `breaks_for("employees", "2026-09")` includes `SNAPSHOT` employee breaks; `fail_orphan_runs` only touches matching running runs; `active_signoff` ignores revoked.
- [ ] **Step 2–4:** FAIL → implement both → PASS.
- [ ] **Step 5: Commit** `feat: repository with SQL and in-memory implementations`

---

### Task 10: Executor and `recon run`

**Files:**
- Create: `src/recon/planner.py`, `src/recon/executor.py`
- Modify: `src/recon/cli.py` (add `run`, `db migrate` already present)
- Test: `tests/unit/test_planner.py`, `tests/integration/test_executor.py`

**Interfaces:**
- Consumes: Tasks 2–9.
- Produces (planner.py): `select_rules(project, domains: list[str] | None, rule_id: str | None) -> list[Rule]` (active only, sorted by `id`; unknown rule/domain → `ConfigError`); `rule_period_label(rule, project, period: RunPeriod) -> str` (`SNAPSHOT` iff every dataset the rule reads has `period_column` null); `run_status(results: list[str]) -> str` (`succeeded` no errors, `failed` all errors or empty, else `partial`); `exit_code(results: list[str], aborted: bool) -> int`.
- Produces (executor.py): `RunOutcome(run_id: int | None, status: str, exit_code: int, workbook: Path | None, messages: list[str])`; `execute_run(project: Project, db: Database, repo: Repository, *, domains: list[str] | None, rule_id: str | None, period: RunPeriod, trigger: Literal["scheduled","manual"], export: Callable[[int], Path] | None, today: date) -> RunOutcome`.
- Flow (spec §5), with these concrete choices: lock resources `recon:<domain>:<period.label>` for every domain of the selected rules, acquired in sorted order (`LockBusy` → exit 2, message `run already in progress for <resource>`); `fail_orphan_runs` after locking; `upsert_rule` for every selected rule; preflight `describe()` every referenced dataset and check each column the rule names exists (case-insensitive) — failures abort before `open_run`; per rule inside `db.transaction()` with `db.set_timeout(rule.timeout_s)`: freshness (`SELECT DATEDIFF(hour, MAX([col]), SYSUTCDATETIME()) FROM <src> s`; NULL or > max → `error: stale source <dataset>`), `checks_sql`, `counts_sql`, `totals_sql` → `SELECT * INTO #totals FROM (<totals_sql>) t` + `insert_totals`, `SELECT * INTO #diffs FROM (<diff_sql>) d`, count > `max_breaks` → `error: suspected load failure: <n> > max_breaks <m>` (breaks untouched), else fetch → `plan_merge` (with `signed_off = repo.active_signoff(domain, label) is not None`) → `apply_engine_changes`; `DROP TABLE` temp tables; any exception → rollback, `CheckRecord(result="error", error_message=str(exc)[:4000])` written in a fresh transaction. `run_by` comes from the connection. `git_commit` from `git rev-parse HEAD` in project root (None if unavailable). Warn (`messages`) when the period is signed off.
- CLI: `recon run [--domain D]... [--all] [--rule ID] --period P [--trigger manual|scheduled] [--root PATH] [--no-export]`; requires exactly one of `--domain`/`--all`/`--rule`; prints one line per check and the summary; exits with `exit_code`.

- [ ] **Step 1: Tests.** Unit: `run_status`/`exit_code` tables; `rule_period_label` for snapshot vs periodic rule; `select_rules` errors. Integration (fixture project + small staging tables created by the test): first run → `fail`, breaks created, exit 1; fix the data, rerun → breaks `resolved/auto_cleared`, exit 0; break the data again → reopened; a rule whose SQL fails (custom_sql without `business_key`) → `partial`, exit 2, other rules still recorded; `max_breaks: 2` with 3 diffs → `error`, no breaks written; stale `load_ts` → `error: stale source`; concurrent `applock` held by another connection → exit 2, no run row.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat: run executor and recon run command`

---

### Task 11: Excel export

**Files:**
- Create: `src/recon/excel/__init__.py`, `src/recon/excel/layout.py`, `src/recon/excel/export.py`
- Modify: `src/recon/cli.py` (add `export`; `run` passes an `export` callable to `execute_run` unless `--no-export`)
- Test: `tests/unit/test_export.py`

**Interfaces:**
- Produces (layout.py): `SCHEMA_VERSION = "1"`; sheet names `SUMMARY="Summary"`, `TOTALS="Totals"`, `BREAKS="Breaks"`, `META="_meta"`, evidence-only `COMMENTS="Comments"`, `AUDIT="Audit"`, `SQL="SQL"`; `BREAK_COLUMNS = ["break_id","rule_id","severity","domain","period","business_key","break_type","field","left","right","diff","first_seen_run","last_seen_run","post_signoff","comment_history","Status","Owner","New comment"]`; `EDITABLE = ["Status","Owner","New comment"]`; `STATUSES = ["open","investigating","resolved","accepted"]`; `SEVERITY_ORDER = {"high":0,"medium":1,"low":2}`.
- Produces (export.py): `export_workbook(repo: Repository, *, domain: str | None, period_label: str, run_id: int | None, out_path: Path, evidence: bool = False) -> Path`.
  - `Breaks`: statuses open/investigating/accepted + resolved in `run_id`; evidence mode = every break for the period. Sorted by severity order, `break_type`, `business_key`. `comment_history` = latest 5 comments `"<author> <yyyy-mm-dd>: <text>"` joined by newline.
  - Editable columns unlocked, data-validation list on `Status`; all sheets protected (no password); evidence mode: nothing unlocked, plus `Comments`, `Audit`, `SQL` (`rule_id`, `generated_sql` per check of the run).
  - `_meta`: `sheet_state = "veryHidden"`; rows `export_id` (uuid4), `run_id`, `schema_version`, `exported_by`, `exported_at` (ISO-8601 UTC) as key/value in A1:B5, header `break_id | version` at row 7, one row per exported break.
  - `recon export --domain D|--all --period P [--run-id N] [--root PATH]` → writes `<export_dir>/recon_<domain|all>_<period>_<run_id or "latest">.xlsx`.

- [ ] **Step 1: Tests** (InMemoryRepository seeded with breaks, comments, totals): sheet names; header row equals `BREAK_COLUMNS`; resolved-not-this-run excluded, resolved-this-run included; PII `***` passes through unchanged; `_meta` veryHidden with version per break; Status validation list equals `STATUSES`; `Status` cell unlocked and `business_key` cell locked; evidence mode has `SQL` sheet and no unlocked cells; sort order high before low.
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5: Commit** `feat: Excel export`

---

### Task 12: Excel import

**Files:**
- Create: `src/recon/excel/importer.py`; Modify: `src/recon/cli.py` (add `import`)
- Test: `tests/unit/test_import.py`

**Interfaces:**
- Consumes: `layout.py`, `validate_user_transition`, `Repository`.
- Produces: `ImportResult(applied: int, rejected: int, rejects_path: Path | None, batch_id: int | None)`; `WorkbookError(Exception)`; `import_workbook(repo: Repository, path: Path, *, dry_run: bool = False) -> ImportResult`.
  - File-level `WorkbookError` messages: `"not a recon export workbook"` (no `_meta`/`Breaks`, or header mismatch), `"unsupported schema version <v>"`, `"file already imported"` (sha256 of file bytes).
  - Rows are keyed by `break_id` (blank → skipped); a row is an edit when `Status` ≠ current status, or `Owner` (blank = None) ≠ current owner, or `New comment` non-blank.
  - Reject reasons (exact): `"unknown break_id"`, `"invalid status '<x>'"`, `"changed since export"` (`_meta` version ≠ current `version`), validate_user_transition's reason, `"owner longer than 128 characters"`.
  - Valid edits applied in one `apply_user_edits` call (skipped when `dry_run`); rejects written to `<stem>_rejects.xlsx` beside the input with the original columns plus `Reason`; no rejects file when nothing rejected.
  - CLI: `recon import FILE [--dry-run] [--root PATH]` prints `applied N, rejected M` (+ rejects path); exit 0 if no rejects, 1 if rejects, 2 on `WorkbookError`.

- [ ] **Step 1: Tests** (export with Task 11 → edit with openpyxl → import):

```python
def test_valid_edit_applied(repo, exported):        # status investigating + owner → applied 1, version bumped, audit source import
def test_comment_required(repo, exported):          # resolved without comment → rejected, reason "comment required for resolved"
def test_changed_since_export(repo, exported):      # repo applies a user edit first → "changed since export"
def test_nightly_reseen_does_not_block(repo, exported):   # apply_engine_changes(kind="seen") after export → edit still applied (Review Focus 5)
def test_rows_sorted_deleted_and_blank(repo, exported):   # reverse rows, delete one, insert blank row → only real edits applied (Review Focus 5)
def test_same_file_twice(repo, exported):           # second import → WorkbookError("file already imported")
def test_dry_run_applies_nothing(repo, exported):
def test_rejects_file_contents(repo, exported):     # rejects workbook has Reason column with exact strings
def test_not_a_recon_workbook(tmp_path, repo):
```

- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5: Commit** `feat: Excel import with validation and rejects file`

---

### Task 13: Sign-off, evidence, revoke

**Files:**
- Create: `src/recon/signoff.py`; Modify: `src/recon/cli.py` (typer sub-app `signoff` with `invoke_without_command=True` for sign-off and subcommand `revoke`)
- Test: `tests/unit/test_signoff.py`

**Interfaces:**
- Consumes: `Project` (signers, active rules per domain, `settings.evidence_dir`), `Repository`, `export_workbook(..., evidence=True)`.
- Produces: `SignoffRefused(Exception)` with `.reasons: list[str]`; `sign_off(project, repo, *, domain: str, period_label: str, comment: str, now: datetime) -> SignoffRow`; `revoke(project, repo, *, domain: str, period_label: str, reason: str) -> None`.
  - Refusal reasons (exact, all collected): `"no run found for <D> <P>"`, `"latest run <id> status is <status>"`, `"latest run <id> did not execute rules: <a, b>"`, `"<n> breaks are open or investigating"`, `"<user> is not an authorized signer for <D>"`, `"<D> <P> is already signed off"`. Revoke reasons: `"no active sign-off for <D> <P>"`, the signer reason.
  - On success: evidence written to `<evidence_dir>/<D>/<P>/recon_evidence_<D>_<P>_<yyyymmddTHHMMSSZ>.xlsx`, set read-only (`os.chmod(path, stat.S_IREAD)`), sha256 recorded; `insert_signoff` with `accepted_break_count`; if insertion fails the file is deleted.
  - CLI: `recon signoff --domain D --period P --comment TEXT`, `recon signoff revoke --domain D --period P --reason TEXT`; refusal prints each reason, exit 2.

- [ ] **Step 1: Tests:** each refusal reason individually; all reasons collected together; success writes a read-only file whose sha256 matches the stored value and records `accepted_break_count`; second sign-off refused; revoke then sign again succeeds; non-signer cannot revoke; breaks created after sign-off are flagged `post_signoff` (via `plan_merge` with `signed_off=True` through the fake repo).
- [ ] **Step 2–4:** FAIL → implement → PASS. **Step 5: Commit** `feat: period sign-off with evidence workbook`

---

### Task 14: Synthetic end-to-end suite, CI, operations docs

**Files:**
- Create: `tests/fixtures/synthetic.sql` (schemas `stg_wd`, `stg_gp`, `xref` matching the fixture project datasets/mappings, with at least one planted instance of every break type, PII column, effective-dated mapping, duplicate key, and a `load_ts` column), `tests/integration/test_end_to_end.py`, `.github/workflows/ci.yml`, `docs/operations.md`
- Modify: `README.md` (Quick start: install, configure, migrate, validate, run)

**Interfaces:**
- Consumes: everything above, through the CLI only (`typer.testing.CliRunner` with `RECON_DB` set to the test database).

- [ ] **Step 1: Write `test_end_to_end`**: `db migrate` → `rules validate` → `run --all --period 2026-09` (exit 1; every break type present in `recon.break`) → `export` → edit workbook (accept one with comment, resolve the rest with comments) → `import` (exit 0) → `signoff` (exit 0, evidence exists) → `run` again (exit 1 or 0; warning about signed period printed) → `signoff revoke` (exit 0). Also: clean-data variant produces zero breaks.
- [ ] **Step 2: Run** with DSN — iterate until PASS.
- [ ] **Step 3: CI** `.github/workflows/ci.yml`: on `pull_request` and `push` to `main`; job `unit` (matrix Python 3.11, 3.13: `pip install -e '.[dev]'`, `ruff check .`, `pytest -m "not integration"`, `recon rules validate --root .`); job `integration` (service `mcr.microsoft.com/mssql/server:2022-latest` with `ACCEPT_EULA=Y`, `MSSQL_SA_PASSWORD` from a workflow env literal used only for the throwaway container, health-check via port; install `msodbcsql18` from Microsoft's apt repo; `RECON_TEST_DSN="Driver={ODBC Driver 18 for SQL Server};Server=localhost,1433;UID=sa;PWD=$MSSQL_SA_PASSWORD;TrustServerCertificate=yes"`; `pytest -m integration`).
- [ ] **Step 4: `docs/operations.md`**: Windows install (Python, ODBC Driver 18, `pip install`), config files, granting `recon_engine` SELECT on staging/xref schemas, `schtasks` commands for the nightly job (`recon run --all --period current --trigger scheduled`) and the days-1–10 previous-period job, exit codes, log location, export/evidence folder permissions, local development with `docker-compose.test.yml`.
- [ ] **Step 5: Run** full suite locally (`pytest`), push, confirm CI green. **Commit** `test: end-to-end suite, CI workflow, operations docs`

---

## Not in this plan

Production rules for `payroll`, `employees`, `gl`, `ap` (spec §13 step 9) get their own short plan once the staging/mapping DDL is supplied: they are YAML files plus acceptance runs, no engine changes.
