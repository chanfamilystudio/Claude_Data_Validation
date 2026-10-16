import pytest
from typer.testing import CliRunner

from recon.cli import app


@pytest.mark.parametrize(
    "selection",
    [
        [],
        ["--all", "--domain", "payroll"],
        ["--all", "--rule", "pay_jrnl_totals"],
        ["--domain", "payroll", "--rule", "pay_jrnl_totals"],
    ],
)
def test_run_requires_exactly_one_selection(fixture_root, selection):
    res = CliRunner().invoke(app, ["run", "--period", "2026-09", "--root", str(fixture_root), *selection])
    assert res.exit_code == 2
    assert "exactly one of --domain (repeatable), --all or --rule" in res.output


def test_run_rejects_bad_period(fixture_root):
    res = CliRunner().invoke(app, ["run", "--all", "--period", "2026-13", "--root", str(fixture_root)])
    assert res.exit_code == 2 and "invalid period" in res.output


def test_run_reports_config_errors(tmp_path):
    res = CliRunner().invoke(app, ["run", "--all", "--period", "current", "--root", str(tmp_path)])
    assert res.exit_code == 2 and "datasets.yaml" in res.output


def test_run_rejects_unknown_trigger(fixture_root):
    res = CliRunner().invoke(app, ["run", "--all", "--period", "current", "--trigger", "nightly",
                                   "--root", str(fixture_root)])
    assert res.exit_code == 2
