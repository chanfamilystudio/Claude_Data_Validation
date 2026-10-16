"""Break lifecycle planning (spec section 6). Pure functions, no I/O."""

from __future__ import annotations

import hashlib

from recon.model import BreakChange, BreakRecord, ObservedDiff

_ACTIVE = frozenset({"open", "investigating"})
_STATUSES = frozenset({"open", "investigating", "resolved", "accepted"})

# User transition table (spec section 6).
_ALLOWED: dict[str, frozenset[str]] = {
    "open": frozenset({"investigating", "resolved", "accepted"}),
    "investigating": frozenset({"open", "resolved", "accepted"}),
    "accepted": frozenset({"open", "investigating"}),
    "resolved": frozenset({"open"}),
}
_COMMENT_REQUIRED = frozenset({"resolved", "accepted"})


def break_key(
    rule_id: str,
    period_label: str,
    business_key: str,
    field_name: str | None,
    break_type: str,
) -> str:
    raw = "|".join([rule_id, period_label, business_key, field_name or "", break_type])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _values_changed(existing: BreakRecord, observed: ObservedDiff) -> bool:
    # Decimal equality is value-based, so Decimal("1.0") == Decimal("1.000000").
    return (
        existing.left_value != observed.left_value
        or existing.right_value != observed.right_value
        or existing.diff != observed.diff
    )


def plan_merge(
    existing: list[BreakRecord],
    observed: list[ObservedDiff],
    *,
    rule_id: str,
    domain: str,
    period_label: str,
    signed_off: bool,
) -> list[BreakChange]:
    """Decide how this run's diffs change the persistent breaks of one rule+period.

    Changes for observed rows come first (in observed order, duplicates collapsed,
    first wins), followed by auto-clears ordered by existing break_id.
    """
    by_key = {b.break_key: b for b in existing}
    changes: list[BreakChange] = []
    seen_keys: set[str] = set()

    def change(kind, key, obs, ex, post_signoff=False) -> BreakChange:
        return BreakChange(
            kind=kind, break_key=key, rule_id=rule_id, domain=domain,
            period_label=period_label, observed=obs, existing=ex,
            post_signoff=post_signoff,
        )

    for o in observed:
        key = break_key(rule_id, period_label, o.business_key, o.field_name, o.break_type)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        ex = by_key.get(key)
        if ex is None:
            changes.append(change("create", key, o, None, signed_off))
        elif ex.status in _ACTIVE:
            changes.append(change("seen", key, o, ex))
        elif ex.status == "resolved":
            changes.append(change("reopen", key, o, ex))
        elif ex.status == "accepted":
            kind = "reopen" if _values_changed(ex, o) else "seen"
            changes.append(change(kind, key, o, ex))
        else:
            raise ValueError(f"unknown break status {ex.status!r} (break_id={ex.break_id})")

    absent = sorted(
        (b for b in existing if b.break_key not in seen_keys),
        key=lambda b: b.break_id,
    )
    for b in absent:
        if b.status in _ACTIVE or b.status == "accepted":
            changes.append(change("auto_clear", b.break_key, None, b))
    return changes


def validate_user_transition(current: str, new: str, comment: str | None) -> str | None:
    """Return the rejection reason for a user status change, or None if allowed.

    Same-status is not a transition: callers skip it (returns None).
    """
    for name in (current, new):
        if name not in _STATUSES:
            return f"invalid status '{name}'"
    if current == new:
        return None
    if new not in _ALLOWED[current]:
        return f"transition {current}->{new} not allowed"
    if new in _COMMENT_REQUIRED and (comment is None or not comment.strip()):
        return f"comment required for {new}"
    return None
