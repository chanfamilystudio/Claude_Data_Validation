# Connectors

A connector tells the engine how to read a dataset. Rules never talk to a connector directly:
the SQL generator asks it (1) what columns the dataset has, and (2) for a SQL expression it can
`SELECT` from. All reconciliation then runs as T-SQL on the server.

## The protocol

Defined in `src/recon/connectors/base.py`:

```python
@dataclass(frozen=True)
class Column:
    name: str
    data_type: str

class Connector(Protocol):
    name: str
    def describe(self, dataset: DatasetConfig) -> list[Column]: ...
    def as_sql_source(self, dataset: DatasetConfig) -> str: ...
```

- `describe` returns the dataset's columns in ordinal order; preflight uses it to validate that
  every column a rule references exists. When the object does not exist it raises
  `ConfigError(["dataset '<name>': object '<object>' not found"])`.
- `as_sql_source` returns a **parenthesised derived table without an alias**; the generator
  supplies the alias. Identifiers are always bracket-quoted. The expression may reference
  `@period_start` and `@period_end_excl`, which every generated batch declares from bound
  parameters (run parameters are never interpolated into SQL text).

## Built-in: `sqlserver`

`SqlServerConnector(db)` reads staging tables or views already in the engine's database.

- `describe` queries `INFORMATION_SCHEMA.COLUMNS` (tables and views). A one-part object name
  uses schema `dbo`.
- `as_sql_source` for `stg_gp.GL20000` with `period_column: TRXDATE` and `filter: SOURCDOC = 'UPRCC'`:

  ```sql
  (SELECT * FROM [stg_gp].[GL20000] WHERE (SOURCDOC = 'UPRCC')
   AND [TRXDATE] >= @period_start AND [TRXDATE] < @period_end_excl)
  ```

  Predicates are the optional `(<filter>)` plus, for periodic datasets, the half-open range
  `col >= @period_start AND col < @period_end_excl`. The half-open range keeps datetime values
  such as `2026-09-30 14:35` inside their period. A snapshot dataset (no `period_column`) with no
  filter has no `WHERE`.

## Registering a connector

Connectors are registered by name in the `CONNECTORS` dict in `src/recon/connectors/__init__.py`;
`sources.<name>.connector` in configuration selects one, and `get_connector(name, db)` builds it.
An unknown name raises `ConfigError(["unknown connector '<name>'"])`.

## Adding a connector: the landing-table pattern

For data that is not already in SQL Server (a file, an API, another database), implement the
protocol so that `as_sql_source` **lands the rows in the engine's database and returns that
table as the source**:

1. Read the rows from the external system.
2. Within the run, truncate and bulk-insert them into `recon.landing_<dataset>`
   (the engine writes only to schema `recon`; the source system is only read).
3. Return a derived table over the landing table, with the same filter and period predicates
   the SQL Server connector applies, e.g.
   `(SELECT * FROM [recon].[landing_wd_workers] WHERE ...)`.
4. Implement `describe` from the external schema, or from the landing table's columns.
5. Add the class to `CONNECTORS`.

Because the generator only sees the returned expression, nothing downstream (check types,
tolerances, break handling) changes for the new connector.
