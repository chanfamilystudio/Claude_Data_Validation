"""Workbook layout shared by the exporter and the importer: sheet names, columns, enumerations."""

from __future__ import annotations

SCHEMA_VERSION = "1"

SUMMARY = "Summary"
TOTALS = "Totals"
BREAKS = "Breaks"
META = "_meta"
# evidence workbooks only
COMMENTS = "Comments"
AUDIT = "Audit"
SQL = "SQL"

BREAK_COLUMNS = [
    "break_id", "rule_id", "severity", "domain", "period", "business_key", "break_type", "field",
    "left", "right", "diff", "first_seen_run", "last_seen_run", "post_signoff", "comment_history",
    "Status", "Owner", "New comment",
]
EDITABLE = ["Status", "Owner", "New comment"]
STATUSES = ["open", "investigating", "resolved", "accepted"]
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

# Summary: run metadata as key/value rows, then the per-rule check table.
SUMMARY_RUN_KEYS = ["run_id", "domain", "period", "status", "run_by", "started_at", "finished_at"]
SUMMARY_CHECK_COLUMNS = ["rule_id", "severity", "result", "rows_left", "rows_right", "break_count",
                         "error_message"]
NO_RUN = "no run"

TOTALS_COLUMNS = ["rule_id", "group_key", "measure", "left_value", "right_value", "diff", "tolerance_applied",
                  "within_tolerance"]
COMMENT_COLUMNS = ["break_id", "author", "created_at", "text"]
AUDIT_COLUMNS = ["entity", "entity_id", "field", "old_value", "new_value", "changed_by", "changed_at",
                 "source"]
SQL_COLUMNS = ["rule_id", "part", "generated_sql"]  # long SQL spans several parts (rows)

# _meta: key/value rows in A1:B5, the per-break table header at row 7 (data from row 8).
META_KEYS = ["export_id", "run_id", "schema_version", "exported_by", "exported_at"]
META_HEADER_ROW = 7
META_HEADER = ["break_id", "version"]
