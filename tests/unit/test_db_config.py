from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

import recon.db as dbmod
from recon.cli import app
from recon.config.loader import ConfigError
from recon.db import Database, LockBusy, connection_string
from recon.migrations import apply_migrations, list_migrations, split_batches


def write_connection_yaml(root: Path, **overrides) -> None:
    data = {
        "driver": "ODBC Driver 18 for SQL Server",
        "server": "localhost",
        "database": "Finance",
        "trusted_connection": True,
    }
    data.update(overrides)
    (root / "config").mkdir(exist_ok=True)
    (root / "config" / "connection.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def test_env_overrides_file(tmp_path):
    write_connection_yaml(tmp_path, server="sql01", database="Fin")
    assert connection_string(tmp_path, {"RECON_DB": "DSN=x"}) == "DSN=x"
    cs = connection_string(tmp_path, {})
    assert "Server=sql01" in cs and "Database=Fin" in cs and "Trusted_Connection=yes" in cs
    assert "Driver={ODBC Driver 18 for SQL Server}" in cs and "TrustServerCertificate=yes" in cs


def test_blank_env_falls_back_to_file(tmp_path):
    write_connection_yaml(tmp_path)
    assert "Database=Finance" in connection_string(tmp_path, {"RECON_DB": "  "})


def test_missing_connection_file_is_config_error(tmp_path):
    with pytest.raises(ConfigError) as exc:
        connection_string(tmp_path, {})
    assert "connection.yaml" in str(exc.value) and "RECON_DB" in str(exc.value)


def test_connection_file_missing_keys_is_config_error(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "connection.yaml").write_text("server: x\n", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        connection_string(tmp_path, {})
    assert "database" in str(exc.value)


def test_split_batches_on_go_lines():
    sql = "SELECT 1;\nGO\n  go  \nSELECT 2;\nSELECT 'GO now';\nGO\n"
    assert split_batches(sql) == ["SELECT 1;", "SELECT 2;\nSELECT 'GO now';"]


def test_list_migrations_orders_numerically_and_ignores_others(tmp_path):
    for name in ("V010__later.sql", "V002__b.sql", "V001__a.sql", "notes.txt", "V1_bad.sql"):
        (tmp_path / name).write_text("SELECT 1;", encoding="utf-8")
    assert [v for v, _ in list_migrations(tmp_path)] == ["V001", "V002", "V010"]


def test_packaged_migrations_include_v001():
    assert "V001" in [v for v, _ in list_migrations(None)]


class FakeCursor:
    def __init__(self, conn):
        self.conn, self.description, self.rowcount, self._rows = conn, None, 0, []

    def execute(self, sql, *params):
        self.conn.log.append(("execute", sql, params))
        if sql.startswith("BOOM"):
            raise RuntimeError("boom")
        if sql.startswith("SELECT"):
            self.description = [("a",), ("b",)]
            self._rows = [(1, "x"), (2, "y")]
        else:
            self.rowcount = 3
        return self

    def fetchall(self):
        return self._rows

    def nextset(self):
        return False

    def close(self):
        pass


class FakeConn:
    def __init__(self):
        self.log, self.timeout, self.autocommit = [], 0, None

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.log.append("commit")

    def rollback(self):
        self.log.append("rollback")

    def close(self):
        self.log.append("close")


@pytest.fixture
def fake_db(monkeypatch):
    conn = FakeConn()

    def connect(cs, autocommit=True, **kw):
        conn.autocommit = autocommit
        return conn

    monkeypatch.setattr(dbmod.pyodbc, "connect", connect)
    return Database("DSN=fake"), conn


def test_database_connects_without_autocommit(fake_db):
    _, conn = fake_db
    assert conn.autocommit is False


def test_simple_calls_commit_immediately(fake_db):
    db, conn = fake_db
    assert db.query("SELECT 1", 5) == [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
    assert db.scalar("SELECT 1") == 1
    assert db.execute("UPDATE t") == 3
    assert conn.log.count("commit") == 3


def test_transaction_commits_once_on_success(fake_db):
    db, conn = fake_db
    with db.transaction():
        db.execute("UPDATE a")
        db.execute("UPDATE b")
        assert "commit" not in conn.log
    assert conn.log.count("commit") == 1 and "rollback" not in conn.log


def test_transaction_rolls_back_on_error(fake_db):
    db, conn = fake_db
    with pytest.raises(RuntimeError), db.transaction():
        db.execute("UPDATE a")
        db.execute("BOOM")
    assert "commit" not in conn.log and conn.log.count("rollback") == 1
    db.execute("UPDATE c")  # usable again, not stuck in a transaction
    assert conn.log[-1] == "commit"


def test_nested_transaction_is_an_error(fake_db):
    db, _ = fake_db
    with db.transaction(), pytest.raises(RuntimeError, match="nested"), db.transaction():
        pass


def test_failed_simple_call_rolls_back(fake_db):
    db, conn = fake_db
    with pytest.raises(RuntimeError):
        db.execute("BOOM")
    assert conn.log[-1] == "rollback"


def test_set_timeout(fake_db):
    db, conn = fake_db
    db.set_timeout(7)
    assert conn.timeout == 7


def test_lockbusy_carries_resource():
    assert LockBusy("r").resource == "r" and "r" in str(LockBusy("r"))


def test_apply_migrations_signature_defaults_to_package():
    import inspect

    assert inspect.signature(apply_migrations).parameters["migrations_dir"].default is None


def test_cli_db_migrate_connection_failure(tmp_path, monkeypatch):
    write_connection_yaml(tmp_path)
    monkeypatch.delenv("RECON_DB", raising=False)

    def refuse(cs, **kw):
        raise dbmod.pyodbc.Error("08001", "cannot connect")

    monkeypatch.setattr(dbmod.pyodbc, "connect", refuse)
    res = CliRunner().invoke(app, ["db", "migrate", "--root", str(tmp_path)])
    assert res.exit_code == 2 and "connect" in res.output.lower()


def test_cli_db_migrate_missing_config(tmp_path, monkeypatch):
    monkeypatch.delenv("RECON_DB", raising=False)
    res = CliRunner().invoke(app, ["db", "migrate", "--root", str(tmp_path)])
    assert res.exit_code == 2 and "connection.yaml" in res.output
