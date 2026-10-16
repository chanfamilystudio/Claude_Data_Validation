"""SQL Server access: connection string resolution and a small pyodbc wrapper."""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pyodbc
import yaml

from recon.config.loader import ConfigError

log = logging.getLogger(__name__)

ENV_VAR = "RECON_DB"
CONNECTION_FILE = Path("config") / "connection.yaml"


class LockBusy(Exception):
    """Another session holds the application lock for ``resource``."""

    def __init__(self, resource: str):
        self.resource = resource
        super().__init__(f"lock busy: {resource}")


def connection_string(root: Path, env: Mapping[str, str]) -> str:
    """``RECON_DB`` wins; otherwise build an ODBC string from ``config/connection.yaml``."""
    override = (env.get(ENV_VAR) or "").strip()
    if override:
        return override
    path = Path(root) / CONNECTION_FILE
    hint = f"set {ENV_VAR} or provide {CONNECTION_FILE.as_posix()}"
    if not path.is_file():
        raise ConfigError([f"{CONNECTION_FILE.as_posix()}: file not found ({hint})"])
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError([f"{CONNECTION_FILE.as_posix()}: invalid YAML ({exc})"]) from exc
    if not isinstance(data, dict):
        raise ConfigError([f"{CONNECTION_FILE.as_posix()}: expected a mapping"])
    missing = [k for k in ("driver", "server", "database") if not data.get(k)]
    if missing:
        raise ConfigError([f"{CONNECTION_FILE.as_posix()}: missing {', '.join(missing)}"])
    parts = [
        f"Driver={{{data['driver']}}}",
        f"Server={data['server']}",
        f"Database={data['database']}",
    ]
    if data.get("trusted_connection", True):
        parts.append("Trusted_Connection=yes")
    parts.append("TrustServerCertificate=yes")
    return ";".join(parts)


class Database:
    """One pyodbc connection (``autocommit=False``).

    Outside :meth:`transaction`, each call commits immediately (rolling back on error) so
    simple calls never leave a transaction open. Inside, the surrounding block decides.
    """

    def __init__(self, conn_str: str):
        self.conn_str = conn_str
        self._conn = pyodbc.connect(conn_str, autocommit=False)
        self._in_tx = False

    # -- lifecycle -----------------------------------------------------------------------------
    def close(self) -> None:
        try:
            self._conn.close()
        except pyodbc.Error:  # pragma: no cover - best effort
            pass

    def set_timeout(self, seconds: int) -> None:
        """Query timeout (seconds, 0 = none) for statements executed from now on."""
        self._conn.timeout = seconds

    # -- statements ----------------------------------------------------------------------------
    def _run(self, sql: str, params: tuple, fetch: bool) -> tuple[list[dict[str, Any]], int]:
        try:
            cur = self._conn.cursor()
            try:
                cur.execute(sql, *params)
                rows: list[dict[str, Any]] = []
                rowcount = cur.rowcount
                if fetch:
                    # skip leading row-count-only results (e.g. INSERT before SELECT)
                    while cur.description is None and cur.nextset():
                        pass
                    if cur.description is not None:
                        cols = [c[0] for c in cur.description]
                        rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
                return rows, rowcount
            finally:
                cur.close()
        except BaseException:
            if not self._in_tx:
                self._conn.rollback()
            raise

    def _finish(self) -> None:
        if not self._in_tx:
            self._conn.commit()

    def query(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        rows, _ = self._run(sql, params, fetch=True)
        self._finish()
        return rows

    def scalar(self, sql: str, *params: Any) -> Any:
        rows = self.query(sql, *params)
        if not rows:
            return None
        return next(iter(rows[0].values()))

    def execute(self, sql: str, *params: Any) -> int:
        _, rowcount = self._run(sql, params, fetch=False)
        self._finish()
        return rowcount

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Commit on success, roll back on any exception. Not re-entrant."""
        if self._in_tx:
            raise RuntimeError("nested transaction: Database.transaction() is not re-entrant")
        self._in_tx = True
        try:
            yield
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        finally:
            self._in_tx = False

    # -- identity and locking ------------------------------------------------------------------
    def current_user(self) -> str:
        return self.scalar("SELECT SUSER_SNAME()")

    @contextmanager
    def applock(self, resource: str) -> Iterator[None]:
        """Exclusive session-owned application lock on this connection; fail fast if held."""
        result = self.scalar(
            "SET NOCOUNT ON; DECLARE @r INT;"
            " EXEC @r = sp_getapplock @Resource = ?, @LockMode = 'Exclusive',"
            " @LockOwner = 'Session', @LockTimeout = 0; SELECT @r;",
            resource,
        )
        if result is None or result < 0:
            raise LockBusy(resource)
        try:
            yield
        finally:
            try:
                self.execute(
                    "EXEC sp_releaseapplock @Resource = ?, @LockOwner = 'Session'", resource
                )
            except pyodbc.Error:
                log.warning("could not release applock %s (session lock ends with the connection)", resource)
