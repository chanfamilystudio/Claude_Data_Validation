"""Runs the shared repository contract against SQL Server."""

import pyodbc
import pytest

from recon.repository import SqlRepository
from repository_contract import RepositoryContract

pytestmark = pytest.mark.integration


class TestSqlRepository(RepositoryContract):
    failures = (pyodbc.Error, LookupError)

    @pytest.fixture
    def repo(self, db):
        self.db = db
        return SqlRepository(db)

    def occurrence_rows(self, repo, break_id):
        return self.db.query(
            "SELECT run_id, left_value, right_value, diff FROM recon.break_occurrence"
            " WHERE break_id = ? ORDER BY run_id", break_id)

    def audit_rows(self, repo):
        return self.db.query(
            "SELECT entity, entity_id, field, old_value, new_value, changed_by, source, run_id,"
            " import_batch_id FROM recon.audit_log ORDER BY audit_id")

    def batch_rows(self, repo):
        return self.db.query(
            "SELECT import_batch_id, file_name, file_sha256, imported_by, rows_applied, rows_rejected"
            " FROM recon.import_batch ORDER BY import_batch_id")

    def run_state(self, repo, run_id):
        row = self.db.query("SELECT status, finished_at FROM recon.run WHERE run_id = ?", run_id)[0]
        return row["status"], row["finished_at"]


def test_engine_path_methods_join_the_callers_transaction(db):
    from repository_contract import add_rule, create_break, open_run

    repo = SqlRepository(db)
    # the executor's wrapper: engine-path methods must not open their own transaction
    with pytest.raises(RuntimeError, match="boom"), db.transaction():
        add_rule(repo)
        create_break(repo, open_run(repo))
        raise RuntimeError("boom")
    assert db.scalar("SELECT COUNT(*) FROM recon.run") == 0
    assert db.scalar("SELECT COUNT(*) FROM recon.[break]") == 0
    assert db.scalar("SELECT COUNT(*) FROM recon.audit_log") == 0


def test_human_path_methods_own_their_transaction(db):
    from repository_contract import add_rule, create_break, edit, open_run

    repo = SqlRepository(db)
    add_rule(repo)
    rec = create_break(repo, open_run(repo))
    with db.transaction(), pytest.raises(RuntimeError, match="nested transaction"):
        repo.apply_user_edits([edit(rec.break_id, owner="bob")], "a.xlsx", "1" * 64, 0)
