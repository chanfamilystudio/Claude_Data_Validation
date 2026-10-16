"""Load and cross-validate a reconciliation project (config/*.yaml + rules/**/*.yaml)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError, field_validator

from recon.config.models import DatasetConfig, MappingConfig, Rule, SourceConfig
from recon.config.pii import pii_issues

_RULE_ADAPTER: TypeAdapter[Rule] = TypeAdapter(Rule)


class ConfigError(Exception):
    """Raised when a project fails validation; ``issues`` lists every problem found."""

    def __init__(self, issues: list[str]):
        self.issues = list(issues)
        super().__init__("\n".join(self.issues))


@dataclass(frozen=True)
class Settings:
    export_dir: Path
    evidence_dir: Path
    log_dir: Path


@dataclass
class Project:
    root: Path
    sources: dict[str, SourceConfig]
    datasets: dict[str, DatasetConfig]
    mappings: dict[str, MappingConfig]
    rules: dict[str, Rule]
    rule_text: dict[str, str]
    signers: dict[str, list[str]]
    settings: Settings


# --------------------------------------------------------------------------- file models


def _inject_names(kind: str):
    def inject(value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        out = {}
        for key, body in value.items():
            if isinstance(body, dict):
                if "name" in body:
                    raise ValueError(f"{kind} {key!r}: 'name' must not be set; it is taken from the key")
                body = {**body, "name": key}
            out[key] = body
        return out

    return inject


class _DatasetsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sources: dict[str, SourceConfig] = {}
    datasets: dict[str, DatasetConfig] = {}

    _name_datasets = field_validator("datasets", mode="before")(_inject_names("dataset"))


class _MappingsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mappings: dict[str, MappingConfig] = {}

    _name_mappings = field_validator("mappings", mode="before")(_inject_names("mapping"))


class _SignersFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signers: dict[str, list[str]] = {}


class _SettingsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    export_dir: str = "exports"
    evidence_dir: str = "evidence"
    log_dir: str = "logs"


# --------------------------------------------------------------------------- helpers


def _format_loc(loc: tuple[Any, ...]) -> str:
    return ".".join(str(p) for p in loc) if loc else "<root>"


_RULE_TAGS = {"record_match", "control_total", "mapping", "quality"}


def _validation_issues(rel: str, exc: ValidationError, *, strip_tag: bool = False) -> list[str]:
    issues = []
    for err in exc.errors():
        loc = err["loc"]
        if strip_tag and loc and loc[0] in _RULE_TAGS:  # drop the discriminated-union tag segment
            loc = loc[1:]
        issues.append(f"{rel}: {_format_loc(loc)}: {err['msg']}")
    return issues


def _read_yaml(path: Path, rel: str, issues: list[str], *, required: bool) -> tuple[Any, str | None]:
    """Return (parsed data, raw text). Appends to issues on failure; data is None then."""
    if not path.is_file():
        if required:
            issues.append(f"{rel}: <root>: file not found")
        return None, None
    text = path.read_text(encoding="utf-8")
    try:
        return yaml.safe_load(text), text
    except yaml.YAMLError as exc:
        detail = " ".join(str(exc).split())
        issues.append(f"{rel}: <root>: invalid YAML: {detail}")
        return None, text


def _load_config_file(root: Path, name: str, model: type[BaseModel], issues: list[str], *, required: bool):
    rel = f"config/{name}"
    before = len(issues)
    data, _ = _read_yaml(root / rel, rel, issues, required=required)
    if len(issues) > before:
        return None
    if data is None:
        data = {}
    if not isinstance(data, dict):
        issues.append(f"{rel}: <root>: expected a mapping at the top level")
        return None
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        issues.extend(_validation_issues(rel, exc))
        return None


# --------------------------------------------------------------------------- public API


def load_project(root: Path) -> Project:
    root = Path(root)
    issues: list[str] = []

    ds_file = _load_config_file(root, "datasets.yaml", _DatasetsFile, issues, required=True)
    map_file = _load_config_file(root, "mappings.yaml", _MappingsFile, issues, required=False)
    sig_file = _load_config_file(root, "signers.yaml", _SignersFile, issues, required=False)
    set_file = _load_config_file(root, "settings.yaml", _SettingsFile, issues, required=False)

    sources = ds_file.sources if ds_file else {}
    datasets = ds_file.datasets if ds_file else {}
    mappings = map_file.mappings if map_file else {}

    for name, ds in datasets.items():
        if ds.source not in sources:
            issues.append(f"config/datasets.yaml: datasets.{name}.source: unknown source {ds.source!r}")

    set_cfg = set_file or _SettingsFile()
    settings = Settings(
        export_dir=root / set_cfg.export_dir,
        evidence_dir=root / set_cfg.evidence_dir,
        log_dir=root / set_cfg.log_dir,
    )

    # Cross-references can only be judged when the files that define the names loaded cleanly.
    check_datasets = ds_file is not None
    check_mappings = map_file is not None or not (root / "config/mappings.yaml").is_file()

    rules: dict[str, Rule] = {}
    rule_text: dict[str, str] = {}
    seen_in: dict[str, str] = {}
    rules_dir = root / "rules"
    for path in sorted(rules_dir.rglob("*.yaml")) if rules_dir.is_dir() else []:
        rel = path.relative_to(root).as_posix()
        before = len(issues)
        data, text = _read_yaml(path, rel, issues, required=True)
        if len(issues) > before:
            continue
        if not isinstance(data, dict):
            issues.append(f"{rel}: <root>: rule file must contain a mapping")
            continue
        try:
            rule = _RULE_ADAPTER.validate_python(data)
        except ValidationError as exc:
            issues.extend(_validation_issues(rel, exc, strip_tag=True))
            continue

        problems: list[str] = []
        if rule.id != path.stem:
            problems.append(f"id: id {rule.id!r} must match file name {path.stem!r}")
        if rule.id in seen_in:
            problems.append(f"id: duplicate rule id {rule.id!r} (also defined in {seen_in[rule.id]})")
        if check_datasets:
            problems += [
                f"{loc}: unknown dataset {name!r}" for loc, name in rule.dataset_refs() if name not in datasets
            ]
        if check_datasets:
            problems += pii_issues(rule, datasets)
        if check_mappings:
            problems += [
                f"{loc}: unknown mapping {name!r}" for loc, name in rule.mapping_refs() if name not in mappings
            ]
        issues.extend(f"{rel}: {p}" for p in problems)
        seen_in.setdefault(rule.id, rel)
        if not problems:
            rules[rule.id] = rule
            rule_text[rule.id] = text

    if issues:
        raise ConfigError(issues)

    return Project(
        root=root,
        sources=sources,
        datasets=datasets,
        mappings=mappings,
        rules=rules,
        rule_text=rule_text,
        signers=sig_file.signers if sig_file else {},
        settings=settings,
    )


def rule_version_hash(rule: Rule) -> str:
    """sha256 of the rule's canonical (sorted-key) JSON; changes whenever any rule field changes."""
    canonical = json.dumps(json.loads(rule.model_dump_json()), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
