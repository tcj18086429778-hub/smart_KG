from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CalcMode(str, Enum):
    FORBIDDEN = "FORBIDDEN"
    CROSS_EVENT = "CROSS_EVENT"
    MAIN_COST_SCALING = "MAIN_COST_SCALING"
    MAIN_COST_INCREMENT = "MAIN_COST_INCREMENT"
    SPATIAL_INTERSECT = "SPATIAL_INTERSECT"


class EffectTarget(str, Enum):
    TOWER_SITE = "TOWER_SITE"
    LINE_SEGMENT = "LINE_SEGMENT"
    BOTH = "BOTH"


class SpatialRelationType(str, Enum):
    LOCATED_IN = "LOCATED_IN"
    INTERSECTS = "INTERSECTS"
    CROSSES = "CROSSES"
    DISTANCE_TO = "DISTANCE_TO"
    WITHIN_BUFFER = "WITHIN_BUFFER"
    OVERLAPS = "OVERLAPS"
    NEAR = "NEAR"


class Operator(str, Enum):
    EQ = "eq"
    NEQ = "neq"
    IN = "in"
    NOT_IN = "not_in"
    IS_NULL = "is_null"
    EXISTS = "exists"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"


class Logic(str, Enum):
    AND = "AND"
    OR = "OR"


class Condition(BaseModel):
    field: str | None = None
    operator: Operator | None = None
    value: Any = None
    logic: Logic | None = None
    conditions: list["Condition"] | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "Condition":
        is_leaf = self.field is not None or self.operator is not None
        is_group = self.logic is not None or self.conditions is not None
        if is_leaf and is_group:
            raise ValueError("Condition must be either a leaf or a group, not both.")
        if is_leaf:
            if not self.field or not self.operator:
                raise ValueError("Leaf condition requires field and operator.")
        elif is_group:
            if self.logic is None or not self.conditions:
                raise ValueError("Group condition requires logic and non-empty conditions.")
        else:
            raise ValueError("Condition cannot be empty.")
        return self


class GeoFeature(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    feature_type_code: str | None = None
    feature_type_name: str | None = None
    feature_subtype_code: str | None = None
    feature_subtype_name: str | None = None
    feature_level: str | None = None
    source_file: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)

    def to_context(self) -> dict[str, Any]:
        data = self.model_dump(exclude={"properties"})
        data.update(self.properties)
        return data


class TowerSite(BaseModel):
    id: str
    name: str
    project_id: str | None = None
    site_code: str | None = None
    voltage_level: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)


class LineSegment(BaseModel):
    id: str
    name: str
    project_id: str | None = None
    start_site_id: str
    end_site_id: str
    length_km: float | None = None
    voltage_level: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)


class SpatialRelation(BaseModel):
    source_type: str
    source_id: str
    relation: SpatialRelationType
    target_type: str
    target_id: str
    properties: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_context(self) -> dict[str, Any]:
        data = {"relation": self.relation.value}
        data.update(self.properties)
        data.update({f"metadata.{k}": v for k, v in self.metadata.items()})
        return data


class Rule(BaseModel):
    rule_id: str
    rule_name: str
    calc_mode: CalcMode
    effect_target: EffectTarget
    match_condition_json: Condition
    match_condition_raw: str | None = None
    effect_value: Any = None
    effect_value_status: str | None = None
    effect_attr: str | None = None
    effect_unit: str | None = None
    voltage_level: str | None = None
    rule_category: str | None = None
    source_table: str | None = None
    source_row: int | None = None
    enabled: bool = True

    @property
    def is_constraint(self) -> bool:
        return self.calc_mode == CalcMode.FORBIDDEN


class RuleTrigger(BaseModel):
    subject_type: str
    subject_id: str
    subject_name: str | None = None
    feature_id: str
    feature_name: str | None = None
    relation: SpatialRelationType
    rule_id: str
    rule_name: str
    calc_mode: CalcMode
    effect_target: EffectTarget
    status: str
    effect_value: Any = None
    effect_attr: str | None = None
    effect_unit: str | None = None
    attr_value: Any = None
    source_table: str | None = None
    source_row: int | None = None
    explanation: str


class EvaluationReport(BaseModel):
    status_by_subject: dict[str, str]
    triggers: list[RuleTrigger]
