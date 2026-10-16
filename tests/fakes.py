"""In-memory ``Repository`` used by executor/importer/sign-off unit tests.

Mirrors the SQL implementation's observable behaviour (types, ordering, constraint failures);
both run the same suite in ``tests/repository_contract.py``.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, localcontext

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
from recon.repository import VersionConflict


def utcnow() -> datetime:
    """Naive UTC truncated to milliseconds, like ``SYSUTCDATETIME()`` into DATETIME2(3)."""
    now = datetime.now(UTC).replace(tzinfo=None)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def dec6(value: Decimal | None) -> Decimal | None:
    """Round like SQL Server's conversion to DECIMAL(38,6): half away from zero, 6 places."""
    if value is None:
        return None
    with localcontext() as ctx:
        ctx.prec = 60
        return Decimal(value).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


class InMemoryRepository:
    def __init__(self, user: str = "TEST\\user"):
        self._user = user
        self.rules: list[dict] = []
        self.runs: dict[int, dict] = {}
        self.checks: dict[int, dict] = {}
        self.totals: list[dict] = []
        self.breaks: dict[int, dict] = {}
        self.occurrences: list[dict] = []
        self.comments: list[dict] = []
        self.audit: list[dict] = []
        self.batches: list[dict] = []
        self.signoffs: dict[int, dict] = {}
        self._rule_seq = 0
        self._ids = {"run": 0, "check": 0, "break": 0, "comment": 0, "audit": 0, "batch": 0, "signoff": 0}

    # -- helpers -------------------------------------------------------------------------------
    def _next(self, name: str) -> int:
        self._ids[name] += 1
        return self._ids[name]

    def _audit(self, entity, entity_id, field, old, new, source, run_id=None, import_batch_id=None) -> None:
        self.audit.append({
            "audit_id": self._next("audit"), "entity": entity, "entity_id": entity_id, "field": field,
            "old_value": old, "new_value": new, "changed_by": self._user, "changed_at": utcnow(),
            "source": source, "run_id": run_id, "import_batch_id": import_batch_id,
        })

    def _severity(self, rule_id: str) -> str:
        rows = [r for r in self.rules if r["rule_id"] == rule_id]
        if not rows:
            return "medium"
        return max(rows, key=lambda r: (r["loaded_at"], r["seq"], r["version_hash"]))["severity"]

    def _record(self, b: dict) -> BreakRecord:
        return BreakRecord(
            break_id=b["break_id"], break_key=b["break_key"], rule_id=b["rule_id"], domain=b["domain"],
            period_label=b["period_label"], business_key=b["business_key"], break_type=b["break_type"],
            field_name=b["field_name"], left_value=b["left_value"], right_value=b["right_value"],
            diff=b["diff"], status=b["status"], owner=b["owner"], resolution_type=b["resolution_type"],
            post_signoff=b["post_signoff"], first_seen_run_id=b["first_seen_run_id"],
            last_seen_run_id=b["last_seen_run_id"], resolved_run_id=b["resolved_run_id"],
            version=b["version"], severity=self._severity(b["rule_id"]),
        )

    def _break_by_key(self, key: str) -> dict:
        for b in self.breaks.values():
            if b["break_key"] == key:
                return b
        raise LookupError(f"break not found for key {key}")

    # -- identity and rules --------------------------------------------------------------------
    def current_user(self) -> str:
        return self._user

    def upsert_rule(self, rule_id, version_hash, domain, check_type, severity, owner, yaml_text, active) -> None:
        self._rule_seq += 1
        fields = {
            "domain": domain, "check_type": check_type, "severity": severity, "owner": owner,
            "yaml_text": yaml_text, "active": active, "loaded_at": utcnow(), "seq": self._rule_seq,
        }
        for r in self.rules:
            if r["rule_id"] == rule_id and r["version_hash"] == version_hash:
                r.update(fields)  # re-loading an existing version makes it the latest again
                return
        self.rules.append({"rule_id": rule_id, "version_hash": version_hash, **fields})

    # -- runs ----------------------------------------------------------------------------------
    def open_run(self, trigger, domain, period_label, period_start: date, period_end: date,
                 git_commit, engine_version) -> int:
        if period_start is None or period_end is None:
            raise ValueError("open_run requires period_start and period_end")
        run_id = self._next("run")
        self.runs[run_id] = {
            "run_id": run_id, "trigger": trigger, "domain": domain, "period_label": period_label,
            "period_start": period_start, "period_end": period_end, "status": "running",
            "run_by": self._user, "git_commit": git_commit, "engine_version": engine_version,
            "started_at": utcnow(), "finished_at": None,
        }
        return run_id

    def close_run(self, run_id: int, status: str) -> None:
        run = self.runs[run_id]
        run["status"] = status
        run["finished_at"] = utcnow()

    def fail_orphan_runs(self, period_label: str, domains: list[str]) -> int:
        count = 0
        for run in self.runs.values():
            if (run["status"] == "running" and run["period_label"] == period_label
                    and (run["domain"] is None or run["domain"] in domains)):
                run["status"] = "failed"
                run["finished_at"] = utcnow()
                count += 1
        return count

    def latest_run(self, domain: str | None, period_label: str) -> RunRow | None:
        wanted = (None,) if domain is None else (domain, None)
        match = [r for r in self.runs.values()
                 if r["period_label"] == period_label and r["domain"] in wanted]
        return self._run_row(max(match, key=lambda x: x["run_id"])) if match else None

    def get_run(self, run_id: int) -> RunRow | None:
        r = self.runs.get(run_id)
        return self._run_row(r) if r else None

    @staticmethod
    def _run_row(r: dict) -> RunRow:
        return RunRow(r["run_id"], r["trigger"], r["domain"], r["period_label"], r["status"],
                      r["run_by"], r["started_at"], r["finished_at"])

    # -- checks and totals ---------------------------------------------------------------------
    def record_check(self, rec: CheckRecord) -> int:
        if rec.run_id not in self.runs:
            raise LookupError(f"unknown run {rec.run_id}")
        if not any(r["rule_id"] == rec.rule_id and r["version_hash"] == rec.version_hash for r in self.rules):
            raise LookupError(f"unknown rule {rec.rule_id}@{rec.version_hash}")
        check_id = self._next("check")
        self.checks[check_id] = {"run_check_id": check_id, "rec": rec}
        return check_id

    def insert_totals(self, run_check_id: int, rows: list[TotalRow]) -> None:
        if run_check_id not in self.checks:
            raise LookupError(f"unknown run_check {run_check_id}")
        for row in rows:
            q = dataclasses.replace(row, left_value=dec6(row.left_value), right_value=dec6(row.right_value),
                                    diff=dec6(row.diff), tolerance_applied=dec6(row.tolerance_applied))
            self.totals.append({"run_check_id": run_check_id, "row": q})

    def checks_for_run(self, run_id: int, domain: str | None = None) -> list[CheckRecord]:
        out = []
        for _, c in sorted(self.checks.items()):
            rec = c["rec"]
            if rec.run_id != run_id:
                continue
            rule = next(r for r in self.rules if r["rule_id"] == rec.rule_id and r["version_hash"] == rec.version_hash)
            if domain is None or rule["domain"] == domain:
                out.append(dataclasses.replace(rec, domain=rule["domain"], severity=rule["severity"]))
        return out

    def totals_for_run(self, run_id: int, domain: str | None) -> list[TotalRow]:
        out = []
        for t in self.totals:
            rec = self.checks[t["run_check_id"]]["rec"]
            if rec.run_id != run_id:
                continue
            if domain is not None:
                rule = next((r for r in self.rules
                             if r["rule_id"] == rec.rule_id and r["version_hash"] == rec.version_hash), None)
                if rule is None or rule["domain"] != domain:
                    continue
            out.append(t["row"])
        return out

    # -- breaks --------------------------------------------------------------------------------
    def load_breaks(self, rule_id: str, period_label: str) -> list[BreakRecord]:
        return [self._record(b) for _, b in sorted(self.breaks.items())
                if b["rule_id"] == rule_id and b["period_label"] == period_label]

    def get_break(self, break_id: int) -> BreakRecord | None:
        b = self.breaks.get(break_id)
        return self._record(b) if b else None

    def apply_engine_changes(self, changes: list[BreakChange], run_id: int) -> None:
        for ch in changes:
            obs = ch.observed
            if ch.kind != "auto_clear" and obs is None:
                raise ValueError(f"{ch.kind} change for {ch.break_key} has no observed diff")
            if ch.kind == "create":
                if any(b["break_key"] == ch.break_key for b in self.breaks.values()):
                    raise ValueError(f"duplicate break_key {ch.break_key}")
                break_id = self._next("break")
                now = utcnow()
                self.breaks[break_id] = {
                    "break_id": break_id, "break_key": ch.break_key, "rule_id": ch.rule_id,
                    "domain": ch.domain, "period_label": ch.period_label,
                    "business_key": obs.business_key, "break_type": obs.break_type,
                    "field_name": obs.field_name, "left_value": obs.left_value,
                    "right_value": obs.right_value, "diff": dec6(obs.diff), "status": "open", "owner": None,
                    "resolution_type": None, "post_signoff": ch.post_signoff,
                    "first_seen_run_id": run_id, "last_seen_run_id": run_id, "resolved_run_id": None,
                    "version": 1, "created_at": now, "updated_at": now,
                }
                self._occurrence(break_id, run_id, obs)
                self._audit("break", break_id, "status", None, "open", "engine", run_id=run_id)
                continue
            b = self._break_by_key(ch.break_key)
            old_status = b["status"]
            b["updated_at"] = utcnow()
            if ch.kind == "seen":
                self._values(b, obs, run_id)
                self._occurrence(b["break_id"], run_id, obs)
            elif ch.kind == "reopen":
                self._values(b, obs, run_id)
                b.update(status="open", resolution_type=None, resolved_run_id=None, version=b["version"] + 1)
                self._occurrence(b["break_id"], run_id, obs)
                self._audit("break", b["break_id"], "status", old_status, "open", "engine", run_id=run_id)
            elif ch.kind == "auto_clear":
                b.update(status="resolved", resolution_type="auto_cleared", resolved_run_id=run_id,
                         version=b["version"] + 1)
                self._audit("break", b["break_id"], "status", old_status, "resolved", "engine", run_id=run_id)
            else:
                raise ValueError(f"unknown change kind {ch.kind!r}")

    @staticmethod
    def _values(b: dict, obs, run_id: int) -> None:
        b.update(left_value=obs.left_value, right_value=obs.right_value, diff=dec6(obs.diff),
                 last_seen_run_id=run_id)

    def _occurrence(self, break_id: int, run_id: int, obs) -> None:
        self.occurrences.append({"break_id": break_id, "run_id": run_id, "left_value": obs.left_value,
                                 "right_value": obs.right_value, "diff": dec6(obs.diff)})

    def breaks_for(self, domain, period_label, statuses=None, resolved_in_run=None) -> list[BreakRecord]:
        out = []
        for _, b in sorted(self.breaks.items()):
            if domain is not None and b["domain"] != domain:
                continue
            if b["period_label"] not in (period_label, "SNAPSHOT"):
                continue
            keep = (statuses is None or b["status"] in statuses) or (
                resolved_in_run is not None and b["resolved_run_id"] == resolved_in_run)
            if keep:
                out.append(self._record(b))
        return out

    # -- imports and comments ------------------------------------------------------------------
    def import_seen(self, file_sha256: str) -> bool:
        return any(b["file_sha256"] == file_sha256 for b in self.batches)

    def apply_user_edits(self, edits: list[UserEdit], file_name: str, file_sha256: str,
                         rows_rejected: int) -> int:
        # validate everything first so a failure leaves the fake untouched (transaction rollback)
        if self.import_seen(file_sha256):
            raise ValueError(f"file already imported: {file_sha256}")
        for e in edits:
            if e.break_id not in self.breaks:
                raise LookupError(f"break {e.break_id} does not exist")
            if e.expected_version != self.breaks[e.break_id]["version"]:
                raise VersionConflict(e.break_id)
        batch_id = self._next("batch")
        self.batches.append({
            "import_batch_id": batch_id, "file_name": file_name, "file_sha256": file_sha256,
            "imported_by": self._user, "rows_applied": len(edits), "rows_rejected": rows_rejected,
            "imported_at": utcnow(),
        })
        bumped: set[int] = set()
        for e in edits:
            b = self.breaks[e.break_id]
            changed = False
            if e.new_status is not None and e.new_status != b["status"]:
                old = b["status"]
                b["status"] = e.new_status
                if e.new_status == "resolved":
                    b["resolution_type"] = "manual"
                elif old == "resolved":
                    b["resolution_type"] = None
                    b["resolved_run_id"] = None
                self._audit("break", b["break_id"], "status", old, e.new_status, "import",
                            import_batch_id=batch_id)
                changed = True
            if e.new_owner is not None and e.new_owner != b["owner"]:
                old_owner = b["owner"]
                b["owner"] = e.new_owner
                self._audit("break", b["break_id"], "owner", old_owner, e.new_owner, "import",
                            import_batch_id=batch_id)
                changed = True
            if e.comment is not None:
                self.comments.append({
                    "comment_id": self._next("comment"), "break_id": b["break_id"], "text": e.comment,
                    "author": self._user, "created_at": utcnow(), "import_batch_id": batch_id,
                })
            if changed and b["break_id"] not in bumped:  # comment-only edits keep the version
                bumped.add(b["break_id"])
                b["version"] += 1
            b["updated_at"] = utcnow()
        return batch_id

    def comments_for(self, break_ids: list[int]) -> dict[int, list[Comment]]:
        wanted = set(break_ids)
        out: dict[int, list[Comment]] = {}
        for c in sorted(self.comments, key=lambda c: (c["created_at"], c["comment_id"])):
            if c["break_id"] in wanted:
                out.setdefault(c["break_id"], []).append(
                    Comment(c["break_id"], c["text"], c["author"], c["created_at"]))
        return out

    # -- audit and sign-off --------------------------------------------------------------------
    def audit_for(self, domain: str, period_label: str) -> list[AuditEntry]:
        break_ids = {i for i, b in self.breaks.items()
                     if b["domain"] == domain and b["period_label"] in (period_label, "SNAPSHOT")}
        signoff_ids = {i for i, s in self.signoffs.items()
                       if s["domain"] == domain and s["period_label"] == period_label}
        out = []
        for a in self.audit:  # already ascending by audit_id
            if (a["entity"] == "break" and a["entity_id"] in break_ids) or (
                    a["entity"] == "signoff" and a["entity_id"] in signoff_ids):
                out.append(AuditEntry(a["entity"], str(a["entity_id"]), a["field"], a["old_value"],
                                      a["new_value"], a["changed_by"], a["changed_at"], a["source"]))
        return out

    def active_signoff(self, domain: str, period_label: str) -> SignoffRow | None:
        for i, s in sorted(self.signoffs.items()):
            if s["domain"] == domain and s["period_label"] == period_label and s["status"] == "signed":
                return dataclasses.replace(s["row"], signoff_id=i, status="signed")
        return None

    def insert_signoff(self, row: SignoffRow) -> int:
        if row.open_break_count != 0:
            raise ValueError("open_break_count must be 0 to sign off")
        if row.status == "signed" and self.active_signoff(row.domain, row.period_label) is not None:
            raise ValueError(f"active sign-off already exists for {row.domain} {row.period_label}")
        if row.run_id not in self.runs:
            raise LookupError(f"unknown run {row.run_id}")
        signoff_id = self._next("signoff")
        self.signoffs[signoff_id] = {
            "domain": row.domain, "period_label": row.period_label, "status": row.status,
            # identity and time are stamped here, never taken from the caller (as in SQL)
            "row": dataclasses.replace(row, signoff_id=signoff_id, signed_by=self._user, signed_at=utcnow()),
            "revoked_by": None,
            "revoked_at": None, "revoke_reason": None,
        }
        self._audit("signoff", signoff_id, "status", None, row.status, "cli")
        return signoff_id

    def revoke_signoff(self, signoff_id: int, reason: str) -> None:
        s = self.signoffs.get(signoff_id)
        if s is None or s["status"] != "signed":
            raise LookupError(f"no active sign-off with id {signoff_id}")
        s.update(status="revoked", revoked_by=self._user, revoked_at=utcnow(), revoke_reason=reason)
        self._audit("signoff", signoff_id, "status", "signed", "revoked", "cli")
