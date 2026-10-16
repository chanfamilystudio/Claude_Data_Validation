"""Persistence for runs, checks, breaks, comments, audit, imports and sign-offs (schema ``recon``).

Transaction ownership: the engine-path methods (``upsert_rule`` .. ``apply_engine_changes``) never
open a transaction; the executor wraps them in ``Database.transaction()``. The human-path methods
(``apply_user_edits``, ``insert_signoff``, ``revoke_signoff``) each run in their own transaction,
so they must not be called from inside one (``Database.transaction()`` is not re-entrant).
"""

from __future__ import annotations

from datetime import date
from typing import Any, Protocol

from recon.db import Database
from recon.model import (
    AuditEntry,
    BreakChange,
    BreakRecord,
    CheckRecord,
    Comment,
    RunRow,
    SignoffRow,
    TotalRow,
    UserEdit,
)

SNAPSHOT = "SNAPSHOT"
_DEFAULT_SEVERITY = "medium"  # only if a break outlives every rule row (cannot happen via the engine)


class VersionConflict(Exception):
    """A break's version no longer matches the exported one; the whole import batch was rolled back."""

    def __init__(self, break_id: int):
        super().__init__(f"changed since export (break {break_id}); re-run import")
        self.break_id = break_id


class Repository(Protocol):
    def current_user(self) -> str: ...

    def upsert_rule(self, rule_id: str, version_hash: str, domain: str, check_type: str, severity: str,
                    owner: str, yaml_text: str, active: bool) -> None: ...

    def open_run(self, trigger: str, domain: str | None, period_label: str, period_start: date,
                 period_end: date, git_commit: str | None, engine_version: str) -> int: ...

    def close_run(self, run_id: int, status: str) -> None: ...

    def fail_orphan_runs(self, period_label: str, domains: list[str]) -> int: ...

    def record_check(self, rec: CheckRecord) -> int: ...

    def insert_totals(self, run_check_id: int, rows: list[TotalRow]) -> None: ...

    def latest_run(self, domain: str | None, period_label: str) -> RunRow | None: ...

    def get_run(self, run_id: int) -> RunRow | None: ...

    def checks_for_run(self, run_id: int, domain: str | None = None) -> list[CheckRecord]: ...

    def totals_for_run(self, run_id: int, domain: str | None) -> list[TotalRow]: ...

    def load_breaks(self, rule_id: str, period_label: str) -> list[BreakRecord]: ...

    def apply_engine_changes(self, changes: list[BreakChange], run_id: int) -> None: ...

    def breaks_for(self, domain: str | None, period_label: str, statuses: set[str] | None = None,
                   resolved_in_run: int | None = None) -> list[BreakRecord]: ...

    def get_break(self, break_id: int) -> BreakRecord | None: ...

    def import_seen(self, file_sha256: str) -> bool: ...

    def apply_user_edits(self, edits: list[UserEdit], file_name: str, file_sha256: str,
                         rows_rejected: int) -> int: ...

    def comments_for(self, break_ids: list[int]) -> dict[int, list[Comment]]: ...

    def audit_for(self, domain: str, period_label: str) -> list[AuditEntry]: ...

    def active_signoff(self, domain: str, period_label: str) -> SignoffRow | None: ...

    def insert_signoff(self, row: SignoffRow) -> int: ...

    def revoke_signoff(self, signoff_id: int, reason: str) -> None: ...


# `recon.[break]` columns plus the severity of the most recent rule row (loaded_at, then version_hash).
_BREAK_SELECT = """
SELECT b.break_id, b.break_key, b.rule_id, b.domain, b.period_label, b.business_key, b.break_type,
       b.field_name, b.left_value, b.right_value, b.diff, b.status, b.owner, b.resolution_type,
       b.post_signoff, b.first_seen_run_id, b.last_seen_run_id, b.resolved_run_id, b.version,
       COALESCE(s.severity, ?) AS severity
FROM recon.[break] b
OUTER APPLY (SELECT TOP 1 r.severity FROM recon.[rule] r WHERE r.rule_id = b.rule_id
             ORDER BY r.loaded_at DESC, r.version_hash DESC) s
"""

_RUN_COLS = "run_id, [trigger], domain, period_label, status, run_by, started_at, finished_at"

# Both create paths and the occurrence / audit side effects are one batch per change.
_CREATE_SQL = """
SET NOCOUNT ON;
DECLARE @id BIGINT;
INSERT INTO recon.[break]
    (break_key, rule_id, domain, period_label, business_key, break_type, field_name,
     left_value, right_value, diff, status, post_signoff, first_seen_run_id, last_seen_run_id, version)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, 1);
SET @id = SCOPE_IDENTITY();
INSERT INTO recon.break_occurrence (break_id, run_id, left_value, right_value, diff)
VALUES (@id, ?, ?, ?, ?);
INSERT INTO recon.audit_log (entity, entity_id, field, old_value, new_value, changed_by, source, run_id)
VALUES ('break', @id, 'status', NULL, 'open', SUSER_SNAME(), 'engine', ?);
"""

_SEEN_SQL = """
SET NOCOUNT ON;
DECLARE @t TABLE (break_id BIGINT);
UPDATE recon.[break]
   SET left_value = ?, right_value = ?, diff = ?, last_seen_run_id = ?, updated_at = SYSUTCDATETIME()
OUTPUT inserted.break_id INTO @t
 WHERE break_key = ?;
IF @@ROWCOUNT = 0 THROW 50010, 'break not found', 1;
INSERT INTO recon.break_occurrence (break_id, run_id, left_value, right_value, diff)
SELECT break_id, ?, ?, ?, ? FROM @t;
"""

_REOPEN_SQL = """
SET NOCOUNT ON;
DECLARE @t TABLE (break_id BIGINT, old_status VARCHAR(20));
UPDATE recon.[break]
   SET status = 'open', resolution_type = NULL, resolved_run_id = NULL, version = version + 1,
       left_value = ?, right_value = ?, diff = ?, last_seen_run_id = ?, updated_at = SYSUTCDATETIME()
OUTPUT inserted.break_id, deleted.status INTO @t
 WHERE break_key = ?;
IF @@ROWCOUNT = 0 THROW 50010, 'break not found', 1;
INSERT INTO recon.break_occurrence (break_id, run_id, left_value, right_value, diff)
SELECT break_id, ?, ?, ?, ? FROM @t;
INSERT INTO recon.audit_log (entity, entity_id, field, old_value, new_value, changed_by, source, run_id)
SELECT 'break', break_id, 'status', old_status, 'open', SUSER_SNAME(), 'engine', ? FROM @t;
"""

_AUTO_CLEAR_SQL = """
SET NOCOUNT ON;
DECLARE @t TABLE (break_id BIGINT, old_status VARCHAR(20));
UPDATE recon.[break]
   SET status = 'resolved', resolution_type = 'auto_cleared', resolved_run_id = ?,
       version = version + 1, updated_at = SYSUTCDATETIME()
OUTPUT inserted.break_id, deleted.status INTO @t
 WHERE break_key = ?;
IF @@ROWCOUNT = 0 THROW 50010, 'break not found', 1;
INSERT INTO recon.audit_log (entity, entity_id, field, old_value, new_value, changed_by, source, run_id)
SELECT 'break', break_id, 'status', old_status, 'resolved', SUSER_SNAME(), 'engine', ? FROM @t;
"""


class SqlRepository:
    def __init__(self, db: Database):
        self._db = db

    # -- identity and rules --------------------------------------------------------------------
    def current_user(self) -> str:
        return self._db.current_user()

    def upsert_rule(self, rule_id, version_hash, domain, check_type, severity, owner, yaml_text, active) -> None:
        # Re-loading an existing version refreshes loaded_at so it becomes the latest row again.
        self._db.execute(
            """
            SET NOCOUNT ON;
            UPDATE recon.[rule]
               SET domain = ?, check_type = ?, severity = ?, owner = ?, yaml_text = ?, active = ?,
                   loaded_at = SYSUTCDATETIME()
             WHERE rule_id = ? AND version_hash = ?;
            IF @@ROWCOUNT = 0
                INSERT INTO recon.[rule] (rule_id, version_hash, domain, check_type, severity, owner,
                                          yaml_text, active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """,
            domain, check_type, severity, owner, yaml_text, 1 if active else 0, rule_id, version_hash,
            rule_id, version_hash, domain, check_type, severity, owner, yaml_text, 1 if active else 0,
        )

    # -- runs ----------------------------------------------------------------------------------
    def open_run(self, trigger, domain, period_label, period_start, period_end, git_commit, engine_version) -> int:
        if period_start is None or period_end is None:
            raise ValueError("open_run requires period_start and period_end")
        return int(self._db.scalar(
            """
            SET NOCOUNT ON;
            INSERT INTO recon.run ([trigger], domain, period_label, period_start, period_end, status,
                                   run_by, git_commit, engine_version)
            OUTPUT inserted.run_id
            VALUES (?, ?, ?, ?, ?, 'running', SUSER_SNAME(), ?, ?);
            """,
            trigger, domain, period_label, period_start, period_end, git_commit, engine_version,
        ))

    def close_run(self, run_id: int, status: str) -> None:
        self._db.execute(
            "UPDATE recon.run SET status = ?, finished_at = SYSUTCDATETIME() WHERE run_id = ?",
            status, run_id,
        )

    def fail_orphan_runs(self, period_label: str, domains: list[str]) -> int:
        domain_sql = "domain IS NULL"
        if domains:
            domain_sql = f"(domain IS NULL OR domain IN ({', '.join('?' * len(domains))}))"
        return self._db.execute(
            "UPDATE recon.run SET status = 'failed', finished_at = SYSUTCDATETIME()"
            f" WHERE status = 'running' AND period_label = ? AND {domain_sql}",
            period_label, *domains,
        )

    def latest_run(self, domain: str | None, period_label: str) -> RunRow | None:
        """Latest run covering ``domain`` (its own or an all-domain run); ``None`` = latest all-domain run."""
        if domain is None:
            rows = self._db.query(
                f"SELECT TOP 1 {_RUN_COLS} FROM recon.run WHERE period_label = ? AND domain IS NULL"
                " ORDER BY run_id DESC", period_label)
        else:
            rows = self._db.query(
                f"SELECT TOP 1 {_RUN_COLS} FROM recon.run"
                " WHERE period_label = ? AND (domain = ? OR domain IS NULL) ORDER BY run_id DESC",
                period_label, domain,
            )
        return self._run_row(rows)

    def get_run(self, run_id: int) -> RunRow | None:
        return self._run_row(self._db.query(f"SELECT {_RUN_COLS} FROM recon.run WHERE run_id = ?", run_id))

    @staticmethod
    def _run_row(rows) -> RunRow | None:
        if not rows:
            return None
        r = rows[0]
        return RunRow(r["run_id"], r["trigger"], r["domain"], r["period_label"], r["status"],
                      r["run_by"], r["started_at"], r["finished_at"])

    # -- checks and totals ---------------------------------------------------------------------
    def record_check(self, rec: CheckRecord) -> int:
        return int(self._db.scalar(
            """
            SET NOCOUNT ON;
            INSERT INTO recon.run_check (run_id, rule_id, version_hash, result, rows_left, rows_right,
                                         break_count, generated_sql, duration_ms, error_message)
            OUTPUT inserted.run_check_id
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            rec.run_id, rec.rule_id, rec.version_hash, rec.result, rec.rows_left, rec.rows_right,
            rec.break_count, rec.generated_sql, rec.duration_ms, rec.error_message,
        ))

    def insert_totals(self, run_check_id: int, rows: list[TotalRow]) -> None:
        for t in rows:
            self._db.execute(
                "INSERT INTO recon.total_result (run_check_id, group_key, measure, left_value, right_value,"
                " diff, tolerance_applied, within_tolerance) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                run_check_id, t.group_key, t.measure, t.left_value, t.right_value, t.diff,
                t.tolerance_applied, 1 if t.within_tolerance else 0,
            )

    def checks_for_run(self, run_id: int, domain: str | None = None) -> list[CheckRecord]:
        sql = (
            "SELECT c.run_id, c.rule_id, c.version_hash, c.result, c.rows_left, c.rows_right, c.break_count,"
            " c.generated_sql, c.duration_ms, c.error_message, r.domain, r.severity"
            " FROM recon.run_check c"
            " JOIN recon.[rule] r ON r.rule_id = c.rule_id AND r.version_hash = c.version_hash"
            " WHERE c.run_id = ?"
        )
        params: list[Any] = [run_id]
        if domain is not None:
            sql += " AND r.domain = ?"
            params.append(domain)
        return [CheckRecord(**r) for r in self._db.query(sql + " ORDER BY c.run_check_id", *params)]

    def totals_for_run(self, run_id: int, domain: str | None) -> list[TotalRow]:
        sql = (
            "SELECT c.rule_id, t.group_key, t.measure, t.left_value, t.right_value, t.diff, t.tolerance_applied,"
            " t.within_tolerance FROM recon.total_result t"
            " JOIN recon.run_check c ON c.run_check_id = t.run_check_id"
            " JOIN recon.[rule] r ON r.rule_id = c.rule_id AND r.version_hash = c.version_hash"
            " WHERE c.run_id = ?"
        )
        params: list[Any] = [run_id]
        if domain is not None:
            sql += " AND r.domain = ?"
            params.append(domain)
        rows = self._db.query(sql + " ORDER BY t.total_result_id", *params)
        return [TotalRow(r["group_key"], r["measure"], r["left_value"], r["right_value"], r["diff"],
                         r["tolerance_applied"], bool(r["within_tolerance"]), rule_id=r["rule_id"]) for r in rows]

    # -- breaks --------------------------------------------------------------------------------
    @staticmethod
    def _break(r: dict) -> BreakRecord:
        return BreakRecord(
            break_id=r["break_id"], break_key=r["break_key"], rule_id=r["rule_id"],
            domain=r["domain"], period_label=r["period_label"], business_key=r["business_key"],
            break_type=r["break_type"], field_name=r["field_name"], left_value=r["left_value"],
            right_value=r["right_value"], diff=r["diff"], status=r["status"], owner=r["owner"],
            resolution_type=r["resolution_type"], post_signoff=bool(r["post_signoff"]),
            first_seen_run_id=r["first_seen_run_id"], last_seen_run_id=r["last_seen_run_id"],
            resolved_run_id=r["resolved_run_id"], version=r["version"], severity=r["severity"],
        )

    def load_breaks(self, rule_id: str, period_label: str) -> list[BreakRecord]:
        rows = self._db.query(
            _BREAK_SELECT + " WHERE b.rule_id = ? AND b.period_label = ? ORDER BY b.break_id",
            _DEFAULT_SEVERITY, rule_id, period_label)
        return [self._break(r) for r in rows]

    def get_break(self, break_id: int) -> BreakRecord | None:
        rows = self._db.query(_BREAK_SELECT + " WHERE b.break_id = ?", _DEFAULT_SEVERITY, break_id)
        return self._break(rows[0]) if rows else None

    def apply_engine_changes(self, changes: list[BreakChange], run_id: int) -> None:
        for ch in changes:
            obs = ch.observed
            if ch.kind != "auto_clear" and obs is None:
                raise ValueError(f"{ch.kind} change for {ch.break_key} has no observed diff")
            if ch.kind == "create":
                self._db.execute(
                    _CREATE_SQL,
                    ch.break_key, ch.rule_id, ch.domain, ch.period_label, obs.business_key, obs.break_type,
                    obs.field_name, obs.left_value, obs.right_value, obs.diff, 1 if ch.post_signoff else 0,
                    run_id, run_id,
                    run_id, obs.left_value, obs.right_value, obs.diff,
                    run_id,
                )
            elif ch.kind == "seen":
                self._db.execute(
                    _SEEN_SQL,
                    obs.left_value, obs.right_value, obs.diff, run_id, ch.break_key,
                    run_id, obs.left_value, obs.right_value, obs.diff,
                )
            elif ch.kind == "reopen":
                self._db.execute(
                    _REOPEN_SQL,
                    obs.left_value, obs.right_value, obs.diff, run_id, ch.break_key,
                    run_id, obs.left_value, obs.right_value, obs.diff,
                    run_id,
                )
            elif ch.kind == "auto_clear":
                self._db.execute(_AUTO_CLEAR_SQL, run_id, ch.break_key, run_id)
            else:
                raise ValueError(f"unknown change kind {ch.kind!r}")

    def breaks_for(self, domain, period_label, statuses=None, resolved_in_run=None) -> list[BreakRecord]:
        sql = _BREAK_SELECT + " WHERE b.period_label IN (?, ?)"
        params: list[Any] = [_DEFAULT_SEVERITY, period_label, SNAPSHOT]
        if domain is not None:
            sql += " AND b.domain = ?"
            params.append(domain)
        if statuses is not None:
            clauses = []
            if statuses:
                clauses.append(f"b.status IN ({', '.join('?' * len(statuses))})")
                params.extend(sorted(statuses))
            if resolved_in_run is not None:
                clauses.append("b.resolved_run_id = ?")
                params.append(resolved_in_run)
            sql += f" AND ({' OR '.join(clauses)})" if clauses else " AND 1 = 0"
        rows = self._db.query(sql + " ORDER BY b.break_id", *params)
        return [self._break(r) for r in rows]

    # -- imports and comments ------------------------------------------------------------------
    def import_seen(self, file_sha256: str) -> bool:
        return self._db.scalar(
            "SELECT COUNT(*) FROM recon.import_batch WHERE file_sha256 = ?", file_sha256) > 0

    def apply_user_edits(self, edits: list[UserEdit], file_name: str, file_sha256: str,
                         rows_rejected: int) -> int:
        with self._db.transaction():
            batch_id = int(self._db.scalar(
                "SET NOCOUNT ON; INSERT INTO recon.import_batch (file_name, file_sha256, imported_by,"
                " rows_applied, rows_rejected) OUTPUT inserted.import_batch_id"
                " VALUES (?, ?, SUSER_SNAME(), ?, ?)",
                file_name, file_sha256, len(edits), rows_rejected))
            start_versions: dict[int, int] = {}  # version of each break before this batch touched it
            bumped: set[int] = set()
            for e in edits:
                self._apply_edit(e, batch_id, start_versions, bumped)
        return batch_id

    def _apply_edit(self, e: UserEdit, batch_id: int, start_versions: dict[int, int], bumped: set[int]) -> None:
        rows = self._db.query(
            "SELECT status, owner, version FROM recon.[break] WITH (UPDLOCK, ROWLOCK) WHERE break_id = ?",
            e.break_id)
        if not rows:
            raise LookupError(f"break {e.break_id} does not exist")
        start_version = start_versions.setdefault(e.break_id, rows[0]["version"])
        if e.expected_version != start_version:
            raise VersionConflict(e.break_id)  # rolls back the whole batch (caller's transaction)
        old_status, old_owner = rows[0]["status"], rows[0]["owner"]

        sets = ["updated_at = SYSUTCDATETIME()"]
        params: list[Any] = []
        audits: list[tuple[str, str | None, str]] = []
        if e.new_status is not None and e.new_status != old_status:
            sets.append("status = ?")
            params.append(e.new_status)
            if e.new_status == "resolved":
                sets.append("resolution_type = 'manual'")
            elif old_status == "resolved":
                sets.append("resolution_type = NULL")
                sets.append("resolved_run_id = NULL")
            audits.append(("status", old_status, e.new_status))
        if e.new_owner is not None and e.new_owner != old_owner:
            sets.append("owner = ?")
            params.append(e.new_owner)
            audits.append(("owner", old_owner, e.new_owner))
        if audits and e.break_id not in bumped:  # spec 3: only status/owner changes bump the version
            bumped.add(e.break_id)
            sets.append("version = version + 1")
        self._db.execute(f"UPDATE recon.[break] SET {', '.join(sets)} WHERE break_id = ?", *params, e.break_id)

        for field, old, new in audits:
            self._db.execute(
                "INSERT INTO recon.audit_log (entity, entity_id, field, old_value, new_value, changed_by,"
                " source, import_batch_id) VALUES ('break', ?, ?, ?, ?, SUSER_SNAME(), 'import', ?)",
                e.break_id, field, old, new, batch_id)
        if e.comment is not None:
            self._db.execute(
                "INSERT INTO recon.break_comment (break_id, [text], author, import_batch_id)"
                " VALUES (?, ?, SUSER_SNAME(), ?)", e.break_id, e.comment, batch_id)

    def comments_for(self, break_ids: list[int]) -> dict[int, list[Comment]]:
        out: dict[int, list[Comment]] = {}
        ids = list(dict.fromkeys(break_ids))
        for start in range(0, len(ids), 1000):  # stay far below the 2100-parameter limit
            chunk = ids[start:start + 1000]
            rows = self._db.query(
                "SELECT break_id, [text], author, created_at, comment_id FROM recon.break_comment"
                f" WHERE break_id IN ({', '.join('?' * len(chunk))}) ORDER BY created_at, comment_id", *chunk)
            for r in rows:
                out.setdefault(r["break_id"], []).append(
                    Comment(r["break_id"], r["text"], r["author"], r["created_at"]))
        return out

    # -- audit and sign-off --------------------------------------------------------------------
    def audit_for(self, domain: str, period_label: str) -> list[AuditEntry]:
        rows = self._db.query(
            """
            SELECT entity, entity_id, field, old_value, new_value, changed_by, changed_at, source
            FROM recon.audit_log
            WHERE (entity = 'break' AND entity_id IN (
                       SELECT break_id FROM recon.[break] WHERE domain = ? AND period_label IN (?, ?)))
               OR (entity = 'signoff' AND entity_id IN (
                       SELECT signoff_id FROM recon.signoff WHERE domain = ? AND period_label = ?))
            ORDER BY audit_id
            """,
            domain, period_label, SNAPSHOT, domain, period_label)
        return [AuditEntry(r["entity"], str(r["entity_id"]), r["field"], r["old_value"], r["new_value"],
                           r["changed_by"], r["changed_at"], r["source"]) for r in rows]

    def active_signoff(self, domain: str, period_label: str) -> SignoffRow | None:
        rows = self._db.query(
            "SELECT TOP 1 signoff_id, domain, period_label, run_id, signed_by, signed_at, comment,"
            " open_break_count, accepted_break_count, evidence_path, evidence_sha256, status"
            " FROM recon.signoff WHERE domain = ? AND period_label = ? AND status = 'signed'"
            " ORDER BY signoff_id DESC", domain, period_label)
        if not rows:
            return None
        r = rows[0]
        return SignoffRow(
            r["signoff_id"], r["domain"], r["period_label"], r["run_id"], r["signed_by"], r["signed_at"],
            r["comment"], r["open_break_count"], r["accepted_break_count"], r["evidence_path"],
            r["evidence_sha256"], r["status"])

    def insert_signoff(self, row: SignoffRow) -> int:
        with self._db.transaction():
            signoff_id = int(self._db.scalar(
                "SET NOCOUNT ON; INSERT INTO recon.signoff (domain, period_label, run_id, signed_by,"
                " signed_at, comment, open_break_count, accepted_break_count, evidence_path,"
                " evidence_sha256, status) OUTPUT inserted.signoff_id"
                " VALUES (?, ?, ?, SUSER_SNAME(), SYSUTCDATETIME(), ?, ?, ?, ?, ?, ?)",
                row.domain, row.period_label, row.run_id, row.comment,
                row.open_break_count, row.accepted_break_count, row.evidence_path, row.evidence_sha256,
                row.status))
            self._db.execute(
                "INSERT INTO recon.audit_log (entity, entity_id, field, old_value, new_value, changed_by,"
                " source) VALUES ('signoff', ?, 'status', NULL, ?, SUSER_SNAME(), 'cli')",
                signoff_id, row.status)
        return signoff_id

    def revoke_signoff(self, signoff_id: int, reason: str) -> None:
        with self._db.transaction():
            rows = self._db.query(
                "SELECT status FROM recon.signoff WITH (UPDLOCK, ROWLOCK) WHERE signoff_id = ?", signoff_id)
            if not rows or rows[0]["status"] != "signed":
                raise LookupError(f"no active sign-off with id {signoff_id}")
            self._db.execute(
                "UPDATE recon.signoff SET status = 'revoked', revoked_by = SUSER_SNAME(),"
                " revoked_at = SYSUTCDATETIME(), revoke_reason = ? WHERE signoff_id = ?",
                reason, signoff_id)
            self._db.execute(
                "INSERT INTO recon.audit_log (entity, entity_id, field, old_value, new_value, changed_by,"
                " source) VALUES ('signoff', ?, 'status', 'signed', 'revoked', SUSER_SNAME(), 'cli')",
                signoff_id)
