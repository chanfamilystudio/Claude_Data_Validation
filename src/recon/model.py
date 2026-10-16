"""Plain data records shared across the engine (no behaviour, no I/O)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True)
class ObservedDiff:
    business_key: str
    break_type: str
    field_name: str | None
    left_value: str | None
    right_value: str | None
    diff: Decimal | None


@dataclass(frozen=True)
class BreakRecord:
    break_id: int
    break_key: str
    rule_id: str
    domain: str
    period_label: str
    business_key: str
    break_type: str
    field_name: str | None
    left_value: str | None
    right_value: str | None
    diff: Decimal | None
    status: str
    owner: str | None
    resolution_type: str | None
    post_signoff: bool
    first_seen_run_id: int
    last_seen_run_id: int
    resolved_run_id: int | None
    version: int
    severity: str


@dataclass(frozen=True)
class BreakChange:
    kind: Literal["create", "seen", "reopen", "auto_clear"]
    break_key: str
    rule_id: str
    domain: str
    period_label: str
    observed: ObservedDiff | None
    existing: BreakRecord | None
    post_signoff: bool


@dataclass(frozen=True)
class CheckRecord:
    run_id: int
    rule_id: str
    version_hash: str
    result: Literal["pass", "fail", "error"]
    rows_left: int | None
    rows_right: int | None
    break_count: int
    generated_sql: str
    duration_ms: int
    error_message: str | None
    # filled on reads (from the rule row the check ran); ignored when recording a check
    domain: str | None = None
    severity: str | None = None


@dataclass(frozen=True)
class TotalRow:
    group_key: str
    measure: str
    left_value: Decimal
    right_value: Decimal
    diff: Decimal
    tolerance_applied: Decimal
    within_tolerance: bool
    # filled on reads (from the run_check the total belongs to); ignored when inserting totals
    rule_id: str | None = None


@dataclass(frozen=True)
class RunRow:
    run_id: int
    trigger: str
    domain: str | None
    period_label: str
    status: str
    run_by: str
    started_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class UserEdit:
    break_id: int
    expected_version: int
    new_status: str | None
    new_owner: str | None
    comment: str | None


@dataclass(frozen=True)
class Comment:
    break_id: int
    text: str
    author: str
    created_at: datetime


@dataclass(frozen=True)
class AuditEntry:
    entity: str
    entity_id: str
    field: str
    old_value: str | None
    new_value: str | None
    changed_by: str
    changed_at: datetime
    source: str


@dataclass(frozen=True)
class SignoffRow:
    signoff_id: int | None
    domain: str
    period_label: str
    run_id: int
    signed_by: str
    signed_at: datetime
    comment: str | None
    open_break_count: int
    accepted_break_count: int
    evidence_path: str
    evidence_sha256: str
    status: str
