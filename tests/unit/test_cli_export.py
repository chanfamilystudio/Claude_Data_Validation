import pytest
from typer.testing import CliRunner

from fakes import InMemoryRepository
from recon import cli
from recon.cli import app, run_exporter
from recon.excel.export import export_path
from repository_contract import add_rule, create_break, open_run


@pytest.mark.parametrize("selection", [[], ["--all", "--domain", "payroll"]])
def test_export_requires_exactly_one_selection(fixture_root, selection):
    res = CliRunner().invoke(app, ["export", "--period", "2026-09", "--root", str(fixture_root), *selection])
    assert res.exit_code == 2
    assert "exactly one of --domain or --all" in res.output


def test_export_rejects_snapshot_and_bad_period(fixture_root):
    res = CliRunner().invoke(app, ["export", "--all", "--period", "SNAPSHOT", "--root", str(fixture_root)])
    assert res.exit_code == 2 and "--period must be YYYY-MM, current or previous" in res.output
    res = CliRunner().invoke(app, ["export", "--all", "--period", "2026-13", "--root", str(fixture_root)])
    assert res.exit_code == 2 and "invalid period" in res.output


def test_export_reports_config_errors(tmp_path):
    res = CliRunner().invoke(app, ["export", "--all", "--period", "current", "--root", str(tmp_path)])
    assert res.exit_code == 2 and "datasets.yaml" in res.output


def test_export_rejects_unknown_domain(fixture_root, monkeypatch):
    monkeypatch.setenv("RECON_DB", "Driver={x};Server=none")
    res = CliRunner().invoke(app, ["export", "--domain", "nope", "--period", "2026-09",
                                   "--root", str(fixture_root)])
    assert res.exit_code == 2 and "unknown domain 'nope'" in res.output


def test_export_path_names(tmp_path):
    assert export_path(tmp_path, "payroll", "2026-09", 7) == tmp_path / "recon_payroll_2026-09_7.xlsx"
    assert export_path(tmp_path, None, "2026-09", None) == tmp_path / "recon_all_2026-09_latest.xlsx"


def test_run_exporter_writes_the_run_workbook(tmp_path):
    repo = InMemoryRepository()
    add_rule(repo)
    run = open_run(repo)
    create_break(repo, run)
    path = run_exporter(repo, tmp_path / "exports", "employees", "2026-09")(run)
    assert path == tmp_path / "exports" / f"recon_employees_2026-09_{run}.xlsx" and path.is_file()


def test_run_exporter_failure_is_a_warning(tmp_path, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(cli, "export_workbook", boom)
    assert run_exporter(InMemoryRepository(), tmp_path, None, "2026-09")(3) is None
    assert "Warning: workbook export failed: disk full" in capsys.readouterr().out


def test_export_any_failure_exits_2(fixture_root, monkeypatch):
    class FakeDb:
        def close(self):
            pass

    def boom(*a, **k):
        raise ValueError("bad cell")

    monkeypatch.setenv("RECON_DB", "Driver={x};Server=none")
    monkeypatch.setattr(cli, "Database", lambda conn: FakeDb())
    monkeypatch.setattr(cli, "SqlRepository", lambda db: object())
    monkeypatch.setattr(cli, "export_workbook", boom)
    res = CliRunner().invoke(app, ["export", "--domain", "payroll", "--period", "2026-09",
                                   "--root", str(fixture_root)])
    assert res.exit_code == 2 and "Export failed: bad cell" in res.output
