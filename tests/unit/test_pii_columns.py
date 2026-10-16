"""Dataset-level ``pii_columns``: masked wherever a value is shown, refused wherever it would be an identifier."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from conftest import edit_rule
from recon.config.loader import ConfigError, load_project
from recon.connectors.sqlserver import SqlServerConnector
from recon.sqlgen import generate
from recon.sqlgen.common import GenContext


def mark_pii(root: Path, dataset: str, *columns: str) -> None:
    path = root / "config" / "datasets.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["datasets"][dataset]["pii_columns"] = list(columns)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def ctx(project) -> GenContext:
    conn = SqlServerConnector(None)
    return GenContext(
        datasets=project.datasets,
        sources={n: conn.as_sql_source(d) for n, d in project.datasets.items()},
        mappings=project.mappings,
    )


def issues(root: Path) -> list[str]:
    with pytest.raises(ConfigError) as e:
        load_project(root)
    return e.value.issues


def test_is_pii_is_case_insensitive(tmp_fixture):
    mark_pii(tmp_fixture, "gp_employees", "SOCSCNUM")
    ds = load_project(tmp_fixture).datasets["gp_employees"]
    assert ds.is_pii("socscnum") and not ds.is_pii("EMPLOYID")


def test_record_match_key_refused(tmp_fixture):
    mark_pii(tmp_fixture, "gp_employees", "EMPLOYID")
    found = issues(tmp_fixture)
    assert any(
        "rules/employees/emp_master_match.yaml: keys.0.right: column 'EMPLOYID' of dataset 'gp_employees'"
        " is listed in pii_columns and cannot be a key" in i for i in found
    )


def test_quality_key_and_unique_refused(tmp_fixture):
    mark_pii(tmp_fixture, "gp_employees", "EMPLOYID")
    found = [i for i in issues(tmp_fixture) if "gp_emp_quality.yaml" in i]
    assert any("key.0: column 'EMPLOYID'" in i and "cannot be a key" in i for i in found)
    assert any("assertions.1.unique: column 'EMPLOYID'" in i and "cannot be a unique column" in i for i in found)


def test_mapping_column_refused(tmp_fixture):
    mark_pii(tmp_fixture, "wd_workers", "cost_center")
    found = issues(tmp_fixture)
    assert any(
        "emp_cost_center_map.yaml: column: column 'cost_center' of dataset 'wd_workers'"
        " is listed in pii_columns and cannot be checked by a mapping rule" in i for i in found
    )


def test_control_total_group_refused(tmp_fixture):
    mark_pii(tmp_fixture, "wd_pay_jrnl", "ledger_account")
    found = issues(tmp_fixture)
    assert any("pay_jrnl_totals.yaml: group_by.0.left: column 'ledger_account'" in i for i in found)


def test_tolerance_on_dataset_pii_field_refused(tmp_fixture):
    mark_pii(tmp_fixture, "gp_employees", "SALARY")
    found = issues(tmp_fixture)
    assert any("emp_master_match.yaml: compare.2: tolerance is not allowed on pii field 'annual_salary'" in i
               for i in found)


def test_record_match_hashes_and_masks_dataset_pii_field(tmp_fixture):
    edit_rule(tmp_fixture, "employees/emp_master_match.yaml", {
        "compare": [{"left": "cost_center", "right": "DEPRTMNT", "via_mapping": "cc_to_dept"},
                    {"left": "national_id", "right": "SOCSCNUM"}],          # no pii: true on the field
    })
    mark_pii(tmp_fixture, "gp_employees", "SOCSCNUM")
    project = load_project(tmp_fixture)
    sql = generate(project.rules["emp_master_match"], ctx(project)).diff_sql
    assert "HASHBYTES('SHA2_256'" in sql and "N'***'" in sql


def test_quality_masks_shown_values_of_pii_columns(tmp_fixture):
    edit_rule(tmp_fixture, "employees/gp_emp_quality.yaml", {
        "assertions": [
            {"accepted_values": {"column": "SOCSCNUM", "values": ["x"]}},
            {"like": {"column": "SOCSCNUM", "pattern": "[0-9]%"}},
            {"references": {"column": "SOCSCNUM", "dataset": "gp_departments", "ref_column": "DEPRTMNT"}},
            {"like": {"column": "EMPLOYID", "pattern": "[0-9]%"}},
        ],
    })
    mark_pii(tmp_fixture, "gp_employees", "SOCSCNUM")
    project = load_project(tmp_fixture)
    sql = generate(project.rules["gp_emp_quality"], ctx(project)).diff_sql
    branches = sql.split("UNION ALL")
    assert len(branches) == 4
    for branch in branches[:3]:                                    # the three SOCSCNUM assertions
        assert "N'***' AS left_value" in branch
    assert "N'***'" not in branches[3]                            # EMPLOYID is not pii: raw value shown


def test_production_config_marks_identity_numbers():
    project = load_project(Path(__file__).resolve().parents[2])
    marked = {name: ds.pii_columns for name, ds in project.datasets.items() if ds.pii_columns}
    assert marked == {
        "wd_workers": ["national_id"],
        "gp_employees": ["SOCSCNUM"],
        "wd_suppliers": ["tax_id"],
        "gp_vendors": ["TXIDNMBR"],
    }
