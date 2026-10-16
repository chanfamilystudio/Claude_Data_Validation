from typer.testing import CliRunner

from conftest import edit_rule
from recon.cli import app


def test_cli_validate(fixture_root, tmp_fixture):
    ok = CliRunner().invoke(app, ["rules", "validate", "--root", str(fixture_root)])
    assert ok.exit_code == 0 and "OK: 4 rules" in ok.output
    edit_rule(tmp_fixture, "employees/emp_master_match.yaml", {"surprise": 1})
    res = CliRunner().invoke(app, ["rules", "validate", "--root", str(tmp_fixture)])
    assert res.exit_code == 2 and "emp_master_match.yaml" in res.output


def test_cli_validate_missing_config(tmp_path):
    res = CliRunner().invoke(app, ["rules", "validate", "--root", str(tmp_path)])
    assert res.exit_code == 2 and "datasets.yaml" in res.output


def test_cli_no_args_shows_help():
    res = CliRunner().invoke(app, [])
    assert "rules" in res.output
