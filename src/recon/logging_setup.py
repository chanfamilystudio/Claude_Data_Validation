"""Logging: rotating file + console, with run_id / rule_id injected from context."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s run_id=%(run_id)s rule_id=%(rule_id)s %(message)s"

_run_id: ContextVar[object] = ContextVar("recon_run_id", default="-")
_rule_id: ContextVar[object] = ContextVar("recon_rule_id", default="-")
_FIELDS = {"run_id": _run_id, "rule_id": _rule_id}

_HANDLER_TAG = "_recon_handler"


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = _run_id.get()
        record.rule_id = _rule_id.get()
        return True


@contextmanager
def log_context(**fields: object) -> Iterator[None]:
    """Set ``run_id`` and/or ``rule_id`` on every record logged inside the block."""
    unknown = set(fields) - set(_FIELDS)
    if unknown:
        raise TypeError(f"unknown log context field(s): {', '.join(sorted(unknown))}")
    tokens = [(_FIELDS[name], _FIELDS[name].set(value)) for name, value in fields.items()]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def configure_logging(log_dir: Path) -> None:
    """Attach file + console handlers to the ``recon`` logger. Safe to call repeatedly."""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("recon")
    logger.setLevel(logging.INFO)

    for handler in list(logger.handlers):
        if getattr(handler, _HANDLER_TAG, False):
            logger.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(LOG_FORMAT)
    file_handler = RotatingFileHandler(
        log_dir / "recon.log", maxBytes=10_000_000, backupCount=10, encoding="utf-8"
    )
    console_handler = logging.StreamHandler(sys.stderr)
    for handler in (file_handler, console_handler):
        handler.setFormatter(formatter)
        handler.addFilter(_ContextFilter())
        setattr(handler, _HANDLER_TAG, True)
        logger.addHandler(handler)
