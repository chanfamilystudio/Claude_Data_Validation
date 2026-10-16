from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from recon.breaks import break_key, plan_merge, validate_user_transition
from recon.model import BreakRecord, ObservedDiff

RULE, DOMAIN, PERIOD = "r1", "payroll", "2026-09"


def obs(bk="E1", bt="value_mismatch", field="amt", left="1", right="2", diff="-1"):
    return ObservedDiff(bk, bt, field, left, right, None if diff is None else Decimal(diff))


def rec(o: ObservedDiff, status="open", break_id=1, **kw):
    base = {
        "break_id": break_id,
        "break_key": break_key(RULE, PERIOD, o.business_key, o.field_name, o.break_type),
        "rule_id": RULE, "domain": DOMAIN, "period_label": PERIOD,
        "business_key": o.business_key, "break_type": o.break_type, "field_name": o.field_name,
        "left_value": o.left_value, "right_value": o.right_value, "diff": o.diff,
        "status": status, "owner": None, "resolution_type": None, "post_signoff": False,
        "first_seen_run_id": 1, "last_seen_run_id": 1, "resolved_run_id": None,
        "version": 1, "severity": "high",
    }
    base.update(kw)
    return BreakRecord(**base)


def merge(existing, observed, signed_off=False):
    return plan_merge(existing, observed, rule_id=RULE, domain=DOMAIN,
                      period_label=PERIOD, signed_off=signed_off)


def test_break_key_formula():
    import hashlib
    expected = hashlib.sha256(b"r1|2026-09|E1|amt|value_mismatch").hexdigest()
    assert break_key("r1", "2026-09", "E1", "amt", "value_mismatch") == expected
    assert break_key("r1", "2026-09", "E1", None, "missing_left") == hashlib.sha256(
        b"r1|2026-09|E1||missing_left").hexdigest()


@pytest.mark.parametrize("status, present, values_changed, expected_kind", [
    (None, True, False, "create"),
    ("open", True, False, "seen"), ("investigating", True, True, "seen"),
    ("open", False, False, "auto_clear"), ("investigating", False, False, "auto_clear"),
    ("resolved", True, False, "reopen"), ("resolved", False, False, None),
    ("accepted", True, False, "seen"), ("accepted", True, True, "reopen"),
    ("accepted", False, False, "auto_clear"),
])
def test_engine_transitions(status, present, values_changed, expected_kind):
    o = obs()
    existing = [] if status is None else [rec(o, status=status)]
    seen_obs = obs(left="9" if values_changed else "1") if present else None
    changes = merge(existing, [seen_obs] if seen_obs else [])
    if expected_kind is None:
        assert changes == []
        return
    assert len(changes) == 1
    c = changes[0]
    assert c.kind == expected_kind
    assert c.break_key == break_key(RULE, PERIOD, "E1", "amt", "value_mismatch")
    assert (c.rule_id, c.domain, c.period_label) == (RULE, DOMAIN, PERIOD)
    if expected_kind == "create":
        assert c.existing is None and c.observed == seen_obs
    elif expected_kind == "auto_clear":
        assert c.observed is None and c.existing == existing[0]
    else:
        assert c.observed == seen_obs and c.existing == existing[0]


def test_rerun_same_data_is_idempotent():
    observed = [obs("E1"), obs("E2"), obs("E3", bt="missing_right", field=None, diff=None)]
    existing = [rec(o, break_id=i + 1) for i, o in enumerate(observed)]
    changes = merge(existing, observed)
    assert [c.kind for c in changes] == ["seen"] * 3


def test_duplicate_observed_collapsed():
    first, second = obs(left="1"), obs(left="2")
    changes = merge([], [first, second])
    assert len(changes) == 1
    assert changes[0].observed == first


def test_post_signoff_only_on_create():
    o_new, o_seen, o_reopen = obs("N"), obs("S"), obs("R")
    existing = [rec(o_seen, break_id=1), rec(o_reopen, status="resolved", break_id=2),
                rec(obs("A"), break_id=3)]
    changes = merge(existing, [o_new, o_seen, o_reopen], signed_off=True)
    by_kind = {c.kind: c for c in changes}
    assert by_kind["create"].post_signoff is True
    assert by_kind["seen"].post_signoff is False
    assert by_kind["reopen"].post_signoff is False
    assert by_kind["auto_clear"].post_signoff is False
    assert merge([], [o_new], signed_off=False)[0].post_signoff is False


def test_diff_compared_as_decimal_value():
    o = obs(diff="1.0")
    existing = [rec(o, status="accepted", diff=Decimal("1.000000"))]
    assert merge(existing, [o])[0].kind == "seen"


@pytest.mark.parametrize("field, value", [
    ("left_value", "x"), ("right_value", "x"), ("diff", Decimal(5)), ("diff", None),
])
def test_accepted_any_value_change_reopens(field, value):
    o = obs()
    existing = [rec(o, status="accepted")]
    changed = dataclasses.replace(o, **{field: value})
    assert merge(existing, [changed])[0].kind == "reopen"


def test_none_values_equal():
    o = obs(bt="missing_right", field=None, left=None, right=None, diff=None)
    assert merge([rec(o, status="accepted")], [o])[0].kind == "seen"


def test_output_order_observed_then_auto_clears_by_break_id():
    a, b, c, d = obs("A"), obs("B"), obs("C"), obs("D")
    existing = [rec(d, break_id=9), rec(c, break_id=3), rec(b, break_id=5),
                rec(a, status="resolved", break_id=1)]
    changes = merge(existing, [b, obs("NEW"), a])
    assert [(x.kind, x.break_key) for x in changes] == [
        ("seen", break_key(RULE, PERIOD, "B", "amt", "value_mismatch")),
        ("create", break_key(RULE, PERIOD, "NEW", "amt", "value_mismatch")),
        ("reopen", break_key(RULE, PERIOD, "A", "amt", "value_mismatch")),
        ("auto_clear", break_key(RULE, PERIOD, "C", "amt", "value_mismatch")),
        ("auto_clear", break_key(RULE, PERIOD, "D", "amt", "value_mismatch")),
    ]


def test_models_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        obs().diff = Decimal(1)  # type: ignore[misc]


@pytest.mark.parametrize("cur, new, comment, reason", [
    ("open", "resolved", None, "comment required for resolved"),
    ("open", "accepted", "", "comment required for accepted"),
    ("open", "accepted", "   ", "comment required for accepted"),
    ("resolved", "accepted", "x", "transition resolved->accepted not allowed"),
    ("accepted", "open", None, None), ("open", "investigating", None, None),
    ("open", "open", None, None),
    ("open", "resolved", "done", None),
    ("investigating", "accepted", "ok", None),
    ("investigating", "open", None, None),
    ("accepted", "investigating", None, None),
    ("accepted", "resolved", "x", "transition accepted->resolved not allowed"),
    ("resolved", "open", None, None),
    ("resolved", "investigating", "x", "transition resolved->investigating not allowed"),
    ("bogus", "open", None, "invalid status 'bogus'"),
    ("open", "bogus", "x", "invalid status 'bogus'"),
    ("bogus", "bogus", None, "invalid status 'bogus'"),
])
def test_user_transitions(cur, new, comment, reason):
    assert validate_user_transition(cur, new, comment) == reason
