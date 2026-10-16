"""Pydantic models for datasets, mappings and the four rule check types (spec section 4)."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    StringConstraints,
    Tag,
    model_validator,
)

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")


def _check_identifier(value: str) -> str:
    if not IDENTIFIER_RE.match(value):
        raise ValueError(f"invalid identifier {value!r}")
    return value


Identifier = Annotated[str, AfterValidator(_check_identifier)]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]+$")]
Name = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- config


class SourceConfig(_Model):
    connector: str


class Freshness(_Model):
    column: Identifier
    max_age_hours: int = Field(gt=0)


class DatasetConfig(_Model):
    name: Name
    source: str
    object: Identifier
    period_column: Identifier | None = None
    filter: str | None = None
    freshness: Freshness | None = None


class Effective(_Model):
    start: Identifier
    end: Identifier


class MappingConfig(_Model):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: Name
    object: Identifier
    from_: Identifier = Field(alias="from")
    to: Identifier
    effective: Effective | None = None


# --------------------------------------------------------------------------- rule parts


class Tolerance(_Model):
    abs: Decimal = Field(default=Decimal(0), ge=0)
    pct: Decimal = Field(default=Decimal(0), ge=0)


class KeyPair(_Model):
    left: Identifier
    right: Identifier


class CompareField(_Model):
    left: Identifier
    right: Identifier
    type: Literal["string", "decimal", "int", "date", "datetime", "bool"] = "string"
    tolerance: Tolerance | None = None
    via_mapping: str | None = None
    pii: bool = False

    @model_validator(mode="after")
    def _tolerance_only_on_numeric(self) -> CompareField:
        if self.tolerance is None:
            return self
        if self.type not in ("decimal", "int"):
            raise ValueError(f"tolerance requires type decimal or int (field {self.left!r} is {self.type})")
        if self.pii:
            raise ValueError(
                f"tolerance is not allowed on pii field {self.left!r} (pii values are compared by hash)"
            )
        return self


class Normalize(_Model):
    trim: bool = True
    case_insensitive: bool = True


class Measure(_Model):
    name: Name
    left: str  # trusted SQL aggregate
    right: str  # trusted SQL aggregate
    tolerance: Tolerance | None = None


class GroupBy(_Model):
    left: Identifier
    right: Identifier
    via_mapping: str | None = None


class Target(_Model):
    dataset: str
    column: Identifier


# --------------------------------------------------------------------------- quality assertions
# YAML uses a one-key short form, e.g. ``- not_null: [A, B]`` or ``- unique: [A]``; each model
# unwraps its own key.


class _Assertion(_Model):
    @model_validator(mode="before")
    @classmethod
    def _unwrap(cls, data: Any) -> Any:
        key = cls._yaml_key()
        if isinstance(data, dict) and set(data) == {key}:
            return cls._expand(data[key])
        return data

    @classmethod
    def _yaml_key(cls) -> str:
        raise NotImplementedError

    @classmethod
    def _expand(cls, payload: Any) -> Any:
        return payload


class NotNull(_Assertion):
    columns: list[Identifier] = Field(min_length=1)

    @classmethod
    def _yaml_key(cls) -> str:
        return "not_null"

    @classmethod
    def _expand(cls, payload: Any) -> Any:
        return {"columns": payload}


class Unique(_Assertion):
    columns: list[Identifier] = Field(min_length=1)

    @classmethod
    def _yaml_key(cls) -> str:
        return "unique"

    @classmethod
    def _expand(cls, payload: Any) -> Any:
        return {"columns": payload}


class AcceptedValues(_Assertion):
    column: Identifier
    values: list[str | int | float | bool] = Field(min_length=1)

    @classmethod
    def _yaml_key(cls) -> str:
        return "accepted_values"


class References(_Assertion):
    column: Identifier
    dataset: str
    ref_column: Identifier

    @classmethod
    def _yaml_key(cls) -> str:
        return "references"


class Like(_Assertion):
    column: Identifier
    pattern: str

    @classmethod
    def _yaml_key(cls) -> str:
        return "like"


class CustomSql(_Assertion):
    name: Name
    sql: str

    @classmethod
    def _yaml_key(cls) -> str:
        return "custom_sql"


_ASSERTION_TAGS: dict[str, type[_Assertion]] = {
    "not_null": NotNull,
    "unique": Unique,
    "accepted_values": AcceptedValues,
    "references": References,
    "like": Like,
    "custom_sql": CustomSql,
}
_TAG_BY_CLASS = {cls: tag for tag, cls in _ASSERTION_TAGS.items()}


def _assertion_tag(value: Any) -> str | None:
    if isinstance(value, _Assertion):
        return _TAG_BY_CLASS[type(value)]
    if isinstance(value, dict) and len(value) == 1:
        key = next(iter(value))
        return key if key in _ASSERTION_TAGS else None
    return None


Assertion = Annotated[
    Annotated[NotNull, Tag("not_null")] | Annotated[Unique, Tag("unique")] | Annotated[AcceptedValues, Tag("accepted_values")] | Annotated[References, Tag("references")] | Annotated[Like, Tag("like")] | Annotated[CustomSql, Tag("custom_sql")],
    Discriminator(
        _assertion_tag,
        custom_error_type="invalid_assertion",
        custom_error_message="assertion must be a single-key mapping: one of " + ", ".join(_ASSERTION_TAGS),
    ),
]


# --------------------------------------------------------------------------- rules


class _BaseRule(_Model):
    id: Slug
    domain: Slug
    description: str = ""
    owner: str = ""
    severity: Literal["high", "medium", "low"]
    active: bool = True
    max_breaks: int = Field(default=5000, gt=0)
    timeout_s: int = Field(default=300, gt=0)

    def dataset_refs(self) -> list[tuple[str, str]]:
        """(location, dataset name) pairs this rule references."""
        return []

    def mapping_refs(self) -> list[tuple[str, str]]:
        """(location, mapping name) pairs this rule references."""
        return []

    def dataset_names(self) -> list[str]:
        seen: list[str] = []
        for _, name in self.dataset_refs():
            if name not in seen:
                seen.append(name)
        return seen


class RecordMatchRule(_BaseRule):
    check_type: Literal["record_match"]
    left: str
    right: str
    keys: list[KeyPair] = Field(min_length=1)
    normalize: Normalize = Field(default_factory=Normalize)
    compare: list[CompareField] = Field(min_length=1)

    @model_validator(mode="after")
    def _no_pii_keys(self) -> RecordMatchRule:
        pii_left = {c.left.lower() for c in self.compare if c.pii}
        pii_right = {c.right.lower() for c in self.compare if c.pii}
        for key in self.keys:
            if key.left.lower() in pii_left or key.right.lower() in pii_right:
                raise ValueError(
                    f"key {key.left!r}/{key.right!r} is a pii column; pii columns cannot be keys"
                )
        return self

    @model_validator(mode="after")
    def _unique_compare_columns(self) -> RecordMatchRule:
        # the left column is the break's field_name (part of break_key), compared case-insensitively
        seen: dict[str, str] = {}
        for c in self.compare:
            folded = c.left.lower()
            if folded in seen:
                raise ValueError(f"duplicate compare column {c.left!r} (same as {seen[folded]!r}, ignoring case)")
            seen[folded] = c.left
        return self

    def dataset_refs(self) -> list[tuple[str, str]]:
        return [("left", self.left), ("right", self.right)]

    def mapping_refs(self) -> list[tuple[str, str]]:
        return [(f"compare.{i}.via_mapping", c.via_mapping) for i, c in enumerate(self.compare) if c.via_mapping]


class ControlTotalRule(_BaseRule):
    check_type: Literal["control_total"]
    left: str
    right: str
    group_by: list[GroupBy] = Field(default_factory=list)
    measures: list[Measure] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_measure_names(self) -> ControlTotalRule:
        # the measure name is the break's field_name (part of break_key); SQL Server compares it
        # case-insensitively under the usual collations, so case-only differences are duplicates too
        seen: dict[str, str] = {}
        for m in self.measures:
            folded = m.name.lower()
            if folded in seen:
                raise ValueError(f"duplicate measure name {m.name!r} (same as {seen[folded]!r}, ignoring case)")
            seen[folded] = m.name
        return self

    def dataset_refs(self) -> list[tuple[str, str]]:
        return [("left", self.left), ("right", self.right)]

    def mapping_refs(self) -> list[tuple[str, str]]:
        return [(f"group_by.{i}.via_mapping", g.via_mapping) for i, g in enumerate(self.group_by) if g.via_mapping]


class MappingRule(_BaseRule):
    check_type: Literal["mapping"]
    dataset: str
    column: Identifier
    mapping: str
    target: Target

    def dataset_refs(self) -> list[tuple[str, str]]:
        return [("dataset", self.dataset), ("target.dataset", self.target.dataset)]

    def mapping_refs(self) -> list[tuple[str, str]]:
        return [("mapping", self.mapping)]


class QualityRule(_BaseRule):
    check_type: Literal["quality"]
    dataset: str
    key: list[Identifier] = Field(min_length=1)
    assertions: list[Assertion] = Field(min_length=1)

    def dataset_refs(self) -> list[tuple[str, str]]:
        refs = [("dataset", self.dataset)]
        refs += [
            (f"assertions.{i}.references.dataset", a.dataset)
            for i, a in enumerate(self.assertions)
            if isinstance(a, References)
        ]
        return refs


Rule = Annotated[
    RecordMatchRule | ControlTotalRule | MappingRule | QualityRule,
    Field(discriminator="check_type"),
]
