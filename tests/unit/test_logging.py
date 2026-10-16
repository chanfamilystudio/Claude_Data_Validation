import logging

import pytest

from recon.logging_setup import configure_logging, log_context


@pytest.fixture(autouse=True)
def _reset_recon_logger():
    yield
    logger = logging.getLogger("recon")
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()


def test_log_context_fields(tmp_path):
    configure_logging(tmp_path)
    with log_context(run_id=7, rule_id="emp_master_match"):
        logging.getLogger("recon").info("hello")
    text = (tmp_path / "recon.log").read_text()
    assert "run_id=7" in text
    assert "rule_id=emp_master_match" in text


def test_child_logger_and_defaults(tmp_path):
    configure_logging(tmp_path)
    logging.getLogger("recon.engine").info("outside")
    with log_context(run_id=1):
        pass
    logging.getLogger("recon.engine").info("after")
    lines = (tmp_path / "recon.log").read_text().splitlines()
    assert all("run_id=- rule_id=-" in line for line in lines)


def test_context_restored_after_exit_and_nesting(tmp_path):
    configure_logging(tmp_path)
    log = logging.getLogger("recon")
    with log_context(run_id=1, rule_id="a"):
        with log_context(rule_id="b"):
            log.info("inner")
        log.info("outer")
    log.info("none")
    inner, outer, none = (tmp_path / "recon.log").read_text().splitlines()
    assert "run_id=1 rule_id=b" in inner
    assert "run_id=1 rule_id=a" in outer
    assert "run_id=- rule_id=-" in none


def test_configure_logging_is_idempotent(tmp_path):
    configure_logging(tmp_path)
    configure_logging(tmp_path)
    assert len(logging.getLogger("recon").handlers) == 2  # rotating file + console
    logging.getLogger("recon").info("once")
    assert (tmp_path / "recon.log").read_text().count("once") == 1


def test_reconfigure_moves_to_new_dir(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    configure_logging(first)
    configure_logging(second)
    logging.getLogger("recon").info("moved")
    assert "moved" in (second / "recon.log").read_text()
    assert "moved" not in (first / "recon.log").read_text()


def test_rotation_settings(tmp_path):
    from logging.handlers import RotatingFileHandler

    configure_logging(tmp_path)
    (fh,) = [h for h in logging.getLogger("recon").handlers if isinstance(h, RotatingFileHandler)]
    assert (fh.maxBytes, fh.backupCount) == (10_000_000, 10)
