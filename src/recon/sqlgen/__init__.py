"""Compile rules to T-SQL (one generator per check_type)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from recon.config.models import Rule
from recon.sqlgen.common import PARAM_HEADER, GenContext, GeneratedSql, params_for
from recon.sqlgen.control_total import generate_control_total
from recon.sqlgen.mapping import generate_mapping
from recon.sqlgen.quality import generate_quality
from recon.sqlgen.record_match import generate_record_match

GENERATORS: dict[str, Callable[[Any, GenContext], GeneratedSql]] = {
    "record_match": generate_record_match,
    "control_total": generate_control_total,
    "mapping": generate_mapping,
    "quality": generate_quality,
}


def generate(rule: Rule, ctx: GenContext) -> GeneratedSql:
    try:
        generator = GENERATORS[rule.check_type]
    except KeyError:
        raise NotImplementedError(f"no SQL generator for check_type {rule.check_type!r}") from None
    return generator(rule, ctx)


__all__ = ["GENERATORS", "PARAM_HEADER", "GenContext", "GeneratedSql", "generate", "params_for"]
