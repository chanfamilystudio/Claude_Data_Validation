import shutil
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from conftest import edit_rule
from recon.config.loader import ConfigError, load_project, rule_version_hash
from recon.config.models import AcceptedValues, CompareField, NotNull, Unique


def test_fixture_loads(fixture_root):
    p = load_project(fixture_root)
    assert sorted(p.rules) == ["emp_cost_center_map", "emp_master_match", "gp_emp_quality", "pay_jrnl_totals"]
    r = p.rules["emp_master_match"]
    assert r.max_breaks == 5000 and r.compare[0].tolerance is None and r.normalize.trim is True
    assert r.timeout_s == 300 and r.active is True


def test_fixture_config_details(fixture_root):
    p = load_project(fixture_root)
    assert p.datasets["wd_pay_jrnl"].name == "wd_pay_jrnl"
    assert p.datasets["wd_pay_jrnl"].freshness.max_age_hours == 26
    assert p.datasets["gp_gl_pay"].filter == "SOURCDOC = 'UPRCC'"
    assert p.datasets["gp_departments"].period_column is None
    assert p.mappings["cc_to_dept"].from_ == "wd_cost_center"
    assert p.mappings["cc_to_dept"].effective.start == "eff_start"
    assert p.mappings["wd_acct_to_gp"].effective is None
    assert p.signers == {"employees": ["TEST\\signer"], "payroll": ["TEST\\signer"]}
    assert p.sources["gp"].connector == "sqlserver"
    assert p.settings.export_dir == fixture_root / "exports"
    assert p.settings.log_dir == fixture_root / "logs"
    assert p.rule_text["emp_master_match"] == (
        fixture_root / "rules/employees/emp_master_match.yaml"
    ).read_text(encoding="utf-8")


def test_rule_field_parsing(fixture_root):
    p = load_project(fixture_root)
    r = p.rules["emp_master_match"]
    assert str(r.compare[2].tolerance.abs) == "0.01" and r.compare[2].type == "decimal"
    assert r.compare[3].pii is True and r.compare[0].via_mapping == "cc_to_dept"
    ct = p.rules["pay_jrnl_totals"]
    assert ct.group_by[0].via_mapping == "wd_acct_to_gp" and ct.measures[1].tolerance is None
    assert p.rules["emp_cost_center_map"].target.dataset == "gp_departments"


def test_quality_assertion_short_form_parses(fixture_root):
    q = load_project(fixture_root).rules["gp_emp_quality"]
    assert [type(a).__name__ for a in q.assertions][:2] == ["NotNull", "Unique"]
    assert isinstance(q.assertions[0], NotNull) and q.assertions[0].columns == ["EMPLOYID", "DEPRTMNT"]
    assert isinstance(q.assertions[1], Unique)
    assert isinstance(q.assertions[2], AcceptedValues) and q.assertions[2].values == [0, 1]
    assert [type(a).__name__ for a in q.assertions][2:] == [
        "AcceptedValues", "References", "Like", "CustomSql",
    ]


def test_dataset_and_mapping_names(fixture_root):
    p = load_project(fixture_root)
    assert p.rules["emp_master_match"].dataset_names() == ["wd_workers", "gp_employees"]
    assert p.rules["gp_emp_quality"].dataset_names() == ["gp_employees", "gp_departments"]
    assert p.rules["emp_cost_center_map"].dataset_names() == ["wd_workers", "gp_departments"]


@pytest.mark.parametrize("mutation, expected", [
    ({"surprise": 1}, "Extra inputs are not permitted"),
    ({"left": "no_such_dataset"}, "unknown dataset 'no_such_dataset'"),
    ({"keys": [{"left": "employee_id;drop", "right": "EMPLOYID"}]}, "invalid identifier"),
    ({"id": "other_name"}, "id 'other_name' must match file name 'emp_master_match'"),
])
def test_rule_errors_name_file(tmp_fixture, mutation, expected):
    edit_rule(tmp_fixture, "employees/emp_master_match.yaml", mutation)
    with pytest.raises(ConfigError) as e:
        load_project(tmp_fixture)
    assert any("rules/employees/emp_master_match.yaml" in i and expected in i for i in e.value.issues)


def test_pii_key_rejected(tmp_fixture):  # compare field national_id is pii; using it as a key is an error
    edit_rule(tmp_fixture, "employees/emp_master_match.yaml", {"keys": [{"left": "national_id", "right": "SOCSCNUM"}]})
    with pytest.raises(ConfigError, match="pii"):
        load_project(tmp_fixture)


def test_unknown_mapping_and_duplicate_ids(tmp_fixture):
    edit_rule(tmp_fixture, "employees/emp_cost_center_map.yaml", {"mapping": "x"})
    shutil.copy(
        tmp_fixture / "rules/employees/gp_emp_quality.yaml",
        tmp_fixture / "rules/payroll/gp_emp_quality.yaml",
    )
    with pytest.raises(ConfigError) as e:
        load_project(tmp_fixture)
    issues = e.value.issues
    assert any("emp_cost_center_map.yaml" in i and "unknown mapping 'x'" in i for i in issues)
    assert any("duplicate rule id 'gp_emp_quality'" in i for i in issues)


def test_nested_cross_references_checked(tmp_fixture):
    q = yaml.safe_load((tmp_fixture / "rules/employees/gp_emp_quality.yaml").read_text())
    q["assertions"][3]["references"]["dataset"] = "nope"
    edit_rule(tmp_fixture, "employees/gp_emp_quality.yaml", {"assertions": q["assertions"]})
    edit_rule(tmp_fixture, "employees/emp_cost_center_map.yaml", {"target": {"dataset": "nada", "column": "X"}})
    with pytest.raises(ConfigError) as e:
        load_project(tmp_fixture)
    assert any("gp_emp_quality.yaml" in i and "unknown dataset 'nope'" in i for i in e.value.issues)
    assert any("emp_cost_center_map.yaml" in i and "unknown dataset 'nada'" in i for i in e.value.issues)


def test_all_issues_collected_and_issue_format(tmp_fixture):
    edit_rule(tmp_fixture, "employees/emp_master_match.yaml", {"surprise": 1})
    edit_rule(tmp_fixture, "payroll/pay_jrnl_totals.yaml", {"left": "ghost"})
    with pytest.raises(ConfigError) as e:
        load_project(tmp_fixture)
    issues = e.value.issues
    assert len(issues) == 2
    assert issues[0].startswith("rules/employees/emp_master_match.yaml: surprise: ")
    assert "rules/payroll/pay_jrnl_totals.yaml: left: unknown dataset 'ghost'" in issues[1]


def test_unknown_source_and_dataset_name_rejected(tmp_fixture):
    path = tmp_fixture / "config/datasets.yaml"
    data = yaml.safe_load(path.read_text())
    data["datasets"]["wd_workers"]["source"] = "nowhere"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError) as e:
        load_project(tmp_fixture)
    assert any("config/datasets.yaml" in i and "unknown source 'nowhere'" in i for i in e.value.issues)
    data["datasets"]["wd_workers"]["source"] = "workday"
    data["datasets"]["gp_employees"]["name"] = "x"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError) as e:
        load_project(tmp_fixture)
    assert any("config/datasets.yaml" in i and "name" in i for i in e.value.issues)


def test_bad_identifier_in_config_and_bad_yaml(tmp_fixture):
    path = tmp_fixture / "config/mappings.yaml"
    path.write_text(path.read_text().replace("xref.account_map", "xref.account map"))
    (tmp_fixture / "rules/payroll/broken.yaml").write_text("id: [unclosed\n")
    (tmp_fixture / "rules/payroll/scalar.yaml").write_text("just text\n")
    with pytest.raises(ConfigError) as e:
        load_project(tmp_fixture)
    issues = e.value.issues
    assert any("config/mappings.yaml" in i and "invalid identifier" in i for i in issues)
    assert any("rules/payroll/broken.yaml" in i for i in issues)
    assert any("rules/payroll/scalar.yaml" in i and "mapping" in i for i in issues)


def test_missing_datasets_file_and_optional_files(tmp_fixture):
    (tmp_fixture / "config/signers.yaml").unlink()
    (tmp_fixture / "config/settings.yaml").unlink()
    (tmp_fixture / "config/mappings.yaml").unlink()
    (tmp_fixture / "config/datasets.yaml").unlink()
    (tmp_fixture / "config/connection.yaml").write_text("server: x\n")
    with pytest.raises(ConfigError) as e:
        load_project(tmp_fixture)
    assert any("config/datasets.yaml" in i and "not found" in i for i in e.value.issues)


def test_defaults_when_optional_config_absent(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/datasets.yaml").write_text("sources: {}\ndatasets: {}\n")
    p = load_project(tmp_path)
    assert p.rules == {} and p.signers == {} and p.mappings == {}
    assert p.settings.export_dir == Path(tmp_path) / "exports"
    assert p.settings.evidence_dir == Path(tmp_path) / "evidence"
    assert p.settings.log_dir == Path(tmp_path) / "logs"


def test_version_hash_stable_and_sensitive(fixture_root):
    r = load_project(fixture_root).rules["emp_master_match"]
    assert rule_version_hash(r) == rule_version_hash(r.model_copy(deep=True))
    changed = r.model_copy(update={"max_breaks": 10})
    assert rule_version_hash(changed) != rule_version_hash(r)
    assert len(rule_version_hash(r)) == 64


@pytest.mark.parametrize("type_", ["string", "date", "datetime", "bool"])
def test_tolerance_rejected_on_non_numeric_field(type_):
    with pytest.raises(ValidationError, match=f"tolerance requires type decimal or int.*'x' is {type_}"):
        CompareField(left="x", right="X", type=type_, tolerance={"abs": "0.01"})


def test_tolerance_rejected_on_pii_field():
    with pytest.raises(ValidationError, match="tolerance is not allowed on pii field 'x'"):
        CompareField(left="x", right="X", type="decimal", pii=True, tolerance={"abs": "0.01"})


@pytest.mark.parametrize("type_", ["decimal", "int"])
def test_tolerance_allowed_on_numeric_field(type_):
    assert CompareField(left="x", right="X", type=type_, tolerance={"pct": "0.05"}).tolerance.pct == Decimal("0.05")


def test_tolerance_on_date_field_is_a_loader_issue(tmp_fixture):
    rule = yaml.safe_load((tmp_fixture / "rules/employees/emp_master_match.yaml").read_text(encoding="utf-8"))
    rule["compare"][1]["tolerance"] = {"abs": 1}  # hire_date, type date
    edit_rule(tmp_fixture, "employees/emp_master_match.yaml", {"compare": rule["compare"]})
    with pytest.raises(ConfigError) as e:
        load_project(tmp_fixture)
    assert len(e.value.issues) == 1
    issue = e.value.issues[0]
    assert issue.startswith("rules/employees/emp_master_match.yaml: compare.1: ")
    assert "tolerance requires type decimal or int (field 'hire_date' is date)" in issue


def test_duplicate_measure_names_are_a_loader_issue(tmp_fixture):
    measures = [
        {"name": "net_amount", "left": "SUM(a)", "right": "SUM(b)"},
        {"name": "NET_AMOUNT", "left": "COUNT(*)", "right": "COUNT(*)"},
    ]
    edit_rule(tmp_fixture, "payroll/pay_jrnl_totals.yaml", {"measures": measures})
    with pytest.raises(ConfigError) as e:
        load_project(tmp_fixture)
    assert len(e.value.issues) == 1
    assert e.value.issues[0].startswith("rules/payroll/pay_jrnl_totals.yaml: ")
    assert "duplicate measure name 'NET_AMOUNT'" in e.value.issues[0]


def test_duplicate_compare_left_columns_are_a_loader_issue(tmp_fixture):
    path = tmp_fixture / "rules" / "employees" / "emp_master_match.yaml"
    compare = yaml.safe_load(path.read_text(encoding="utf-8"))["compare"]
    dup = dict(compare[0], left=compare[0]["left"].upper(), right=compare[1]["right"])
    edit_rule(tmp_fixture, "employees/emp_master_match.yaml", {"compare": [*compare, dup]})
    with pytest.raises(ConfigError) as e:
        load_project(tmp_fixture)
    assert len(e.value.issues) == 1
    assert e.value.issues[0].startswith("rules/employees/emp_master_match.yaml: ")
    assert f"duplicate compare column {dup['left']!r}" in e.value.issues[0]
