import re
from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from fakes import InMemoryRepository
from recon import cli
from recon.cli import app, run_exporter
from recon.excel.export import export_path, free_export_path
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
    monkeypatch.setattr(cli, "SqlRepository", lambda db: InMemoryRepository())
    monkeypatch.setattr(cli, "export_workbook", boom)
    res = CliRunner().invoke(app, ["export", "--domain", "payroll", "--period", "2026-09",
                                   "--root", str(fixture_root)])
    assert res.exit_code == 2 and "Export failed: bad cell" in res.output


def test_free_export_path_appends_utc_stamp_then_counter(tmp_path):
    now = datetime(2026, 10, 16, 7, 8, 9, tzinfo=UTC)
    base = tmp_path / "recon_payroll_2026-09_7.xlsx"
    assert free_export_path(tmp_path, "payroll", "2026-09", 7, now=now) == base
    base.write_bytes(b"old")
    stamped = tmp_path / "recon_payroll_2026-09_7_20261016T070809.xlsx"
    assert free_export_path(tmp_path, "payroll", "2026-09", 7, now=now) == stamped
    stamped.write_bytes(b"old")
    assert free_export_path(tmp_path, "payroll", "2026-09", 7, now=now) == (
        tmp_path / "recon_payroll_2026-09_7_20261016T070809_2.xlsx")
    (tmp_path / "recon_payroll_2026-09_7_20261016T070809_2.xlsx").write_bytes(b"old")
    assert free_export_path(tmp_path, "payroll", "2026-09", 7, now=now) == (
        tmp_path / "recon_payroll_2026-09_7_20261016T070809_3.xlsx")


def test_run_exporter_never_overwrites(tmp_path):
    repo = InMemoryRepository()
    add_rule(repo)
    run = open_run(repo)
    export = run_exporter(repo, tmp_path, "employees", "2026-09")
    first = export(run)
    before = first.read_bytes()
    second = export(run)
    assert first == tmp_path / f"recon_employees_2026-09_{run}.xlsx" and first.read_bytes() == before
    assert re.fullmatch(rf"recon_employees_2026-09_{run}_\d{{8}}T\d{{6}}\.xlsx", second.name) and second.is_file()


@pytest.fixture
def wired_export(tmp_fixture, monkeypatch):
    class FakeDb:
        def close(self):
            pass

    repo = InMemoryRepository()
    add_rule(repo)
    monkeypatch.setenv("RECON_DB", "Driver={x};Server=none")
    monkeypatch.setattr(cli, "Database", lambda conn: FakeDb())
    monkeypatch.setattr(cli, "SqlRepository", lambda db: repo)
    return tmp_fixture, repo


def _export_cli(root, *args):
    return CliRunner().invoke(app, ["export", "--period", "2026-09", "--root", str(root), *args])


def test_export_names_file_by_resolved_run_and_never_overwrites(wired_export):
    root, repo = wired_export
    exports = root / "exports"
    res = _export_cli(root, "--domain", "employees")
    assert res.exit_code == 0, res.output
    assert res.output.strip() == str(exports / "recon_employees_2026-09_latest.xlsx")  # no run yet

    run = open_run(repo)
    res = _export_cli(root, "--domain", "employees")
    assert res.exit_code == 0, res.output
    assert res.output.strip() == str(exports / f"recon_employees_2026-09_{run}.xlsx")
    before = (exports / f"recon_employees_2026-09_{run}.xlsx").read_bytes()

    for args in (["--domain", "employees"], ["--domain", "employees", "--run-id", str(run)]):
        res = _export_cli(root, *args)
        assert res.exit_code == 0, res.output
        again = res.output.strip()
        assert re.fullmatch(rf".*recon_employees_2026-09_{run}_\d{{8}}T\d{{6}}(_\d+)?\.xlsx", again), again
    assert (exports / f"recon_employees_2026-09_{run}.xlsx").read_bytes() == before
    assert len(list(exports.glob(f"recon_employees_2026-09_{run}*.xlsx"))) == 3
