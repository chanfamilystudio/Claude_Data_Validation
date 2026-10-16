"""``recon signoff`` end to end against SQL Server: run -> accept breaks -> sign off -> revoke."""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from recon.cli import app
from recon.db import Database
from recon.model import UserEdit
from recon.repository import SqlRepository

from .test_executor import _reset_recon_logger, staging  # noqa: F401  (fixtures)

pytestmark = pytest.mark.integration


def test_cli_signoff_round_trip(staging, dsn, tmp_fixture, monkeypatch, _reset_recon_logger):  # noqa: F811
    db = staging
    repo = SqlRepository(db)
    monkeypatch.setenv("RECON_DB", dsn)
    (tmp_fixture / "config" / "signers.yaml").write_text(
        yaml.safe_dump({"signers": {"employees": [db.current_user()]}}), encoding="utf-8")
    db.execute("UPDATE stg_gp.UPR00100 SET SALARY = 1 WHERE EMPLOYID = '000002'")
    root = ["--root", str(tmp_fixture)]
    runner = CliRunner()
    assert runner.invoke(app, ["run", "--domain", "employees", "--period", "2026-09", *root]).exit_code == 1

    # breaks still open: refused, nothing written
    res = runner.invoke(app, ["signoff", "--domain", "employees", "--period", "2026-09",
                              "--comment", "too early", *root])
    assert res.exit_code == 2 and "breaks are open or investigating" in res.output, res.output
    assert db.scalar("SELECT COUNT(*) FROM recon.signoff") == 0
    assert not (tmp_fixture / "evidence").exists()

    open_breaks = repo.breaks_for("employees", "2026-09", statuses={"open", "investigating"})
    assert open_breaks
    repo.apply_user_edits([UserEdit(b.break_id, b.version, "accepted", None, "known difference")
                           for b in open_breaks], "accept.xlsx", "a" * 64, 0)

    res = runner.invoke(app, ["signoff", "--domain", "employees", "--period", "2026-09",
                              "--comment", "September reviewed", *root])
    assert res.exit_code == 0, res.output
    row = db.query("SELECT * FROM recon.signoff")[0]
    assert (row["domain"], row["period_label"], row["status"], row["comment"]) == (
        "employees", "2026-09", "signed", "September reviewed")
    assert row["signed_by"] == db.current_user()
    assert (row["open_break_count"], row["accepted_break_count"]) == (0, len(open_breaks))
    evidence = Path(row["evidence_path"])
    assert evidence.is_file() and evidence.parent == tmp_fixture / "evidence" / "employees" / "2026-09"
    assert stat.S_IMODE(evidence.stat().st_mode) == stat.S_IREAD
    assert hashlib.sha256(evidence.read_bytes()).hexdigest() == row["evidence_sha256"]
    assert res.output.strip() == f"Signed off employees 2026-09 (sign-off {row['signoff_id']}): {evidence}"

    res = runner.invoke(app, ["signoff", "--domain", "employees", "--period", "2026-09",
                              "--comment", "again", *root])
    assert res.exit_code == 2 and "employees 2026-09 is already signed off" in res.output

    res = runner.invoke(app, ["signoff", "revoke", "--domain", "employees", "--period", "2026-09",
                              "--reason", "late adjustment", *root])
    assert res.exit_code == 0 and res.output.strip() == "Revoked employees 2026-09", res.output
    assert db.scalar("SELECT status FROM recon.signoff WHERE signoff_id = ?", row["signoff_id"]) == "revoked"
    assert repo.active_signoff("employees", "2026-09") is None
    assert evidence.is_file()


@pytest.mark.parametrize("resource", ["recon:employees:2026-09", "recon:employees:SNAPSHOT"])
def test_cli_signoff_refused_while_a_run_holds_the_lock(staging, dsn, tmp_fixture, monkeypatch,  # noqa: F811
                                                        _reset_recon_logger, resource):  # noqa: F811
    db = staging
    monkeypatch.setenv("RECON_DB", dsn)
    (tmp_fixture / "config" / "signers.yaml").write_text(
        yaml.safe_dump({"signers": {"employees": [db.current_user()]}}), encoding="utf-8")
    db.execute("UPDATE stg_gp.UPR00100 SET SALARY = 1 WHERE EMPLOYID = '000002'")
    runner = CliRunner()
    root = ["--root", str(tmp_fixture)]
    assert runner.invoke(app, ["run", "--domain", "employees", "--period", "2026-09", *root]).exit_code == 1
    SqlRepository(db).apply_user_edits(
        [UserEdit(b.break_id, b.version, "accepted", None, "known")
         for b in SqlRepository(db).breaks_for("employees", "2026-09", statuses={"open", "investigating"})],
        "accept.xlsx", "b" * 64, 0)
    other = Database(dsn)
    try:
        with other.applock(resource):
            res = runner.invoke(app, ["signoff", "--domain", "employees", "--period", "2026-09",
                                      "--comment", "x", *root])
    finally:
        other.close()
    assert res.exit_code == 2 and res.output.strip() == f"run already in progress for {resource}", res.output
    assert db.scalar("SELECT COUNT(*) FROM recon.signoff") == 0
    assert not (tmp_fixture / "evidence").exists()
