from __future__ import annotations

from typing import Any

from .condition_evaluator import evaluate_condition
from .schemas import CalcMode, EffectTarget, EvaluationReport, GeoFeature, LineSegment, Rule, RuleTrigger, SpatialRelation, TowerSite


SUBJECT_TYPE_TO_TARGET = {
    "TowerSite": EffectTarget.TOWER_SITE,
    "LineSegment": EffectTarget.LINE_SEGMENT,
}


EFFECT_ATTR_TO_RELATION_FIELD = {
    "S_AREA": "intersect_area_mu",
    "S_LTH": "length_km",
    "S_CNT": "cross_count",
    "S_COUNT": "cross_count",
}


class RuleEngine:
    def __init__(
        self,
        features: dict[str, GeoFeature],
        tower_sites: dict[str, TowerSite],
        line_segments: dict[str, LineSegment],
        spatial_relations: list[SpatialRelation],
        rules: list[Rule],
    ) -> None:
        self.features = features
        self.tower_sites = tower_sites
        self.line_segments = line_segments
        self.spatial_relations = spatial_relations
        self.rules = rules

    def evaluate(self) -> EvaluationReport:
        triggers: list[RuleTrigger] = []
        for relation in self.spatial_relations:
            if relation.target_type != "GeoFeature":
                continue
            feature = self.features.get(relation.target_id)
            if feature is None:
                continue
            subject_target = SUBJECT_TYPE_TO_TARGET.get(relation.source_type)
            if subject_target is None:
                continue
            context = self._build_context(feature, relation)
            for rule in self.rules:
                if not rule.enabled:
                    continue
                if not self._target_applies(rule.effect_target, subject_target):
                    continue
                if evaluate_condition(rule.match_condition_json, context):
                    triggers.append(self._make_trigger(rule, relation, feature, context))
        return EvaluationReport(status_by_subject=self._status_by_subject(triggers), triggers=triggers)

    def explain_subject(self, subject_id: str) -> dict[str, Any]:
        report = self.evaluate()
        triggers = [item for item in report.triggers if item.subject_id == subject_id]
        status = report.status_by_subject.get(subject_id, "available")
        return {
            "subject_id": subject_id,
            "status": status,
            "constraints": [item.model_dump(mode="json") for item in triggers if item.calc_mode == CalcMode.FORBIDDEN],
            "cost_rules": [item.model_dump(mode="json") for item in triggers if item.calc_mode != CalcMode.FORBIDDEN],
        }

    def _build_context(self, feature: GeoFeature, relation: SpatialRelation) -> dict[str, Any]:
        context = feature.to_context()
        context.update(relation.to_context())
        subject = self._subject_context(relation.source_type, relation.source_id)
        context.update(subject)
        context["source_type"] = relation.source_type
        context["source_id"] = relation.source_id
        context["target_type"] = relation.target_type
        context["target_id"] = relation.target_id
        return context

    def _subject_context(self, subject_type: str, subject_id: str) -> dict[str, Any]:
        if subject_type == "TowerSite":
            subject = self.tower_sites.get(subject_id)
        elif subject_type == "LineSegment":
            subject = self.line_segments.get(subject_id)
        else:
            subject = None
        if subject is None:
            return {}
        data = subject.model_dump(exclude={"properties"})
        data.update(subject.properties)
        return data

    def _target_applies(self, rule_target: EffectTarget, subject_target: EffectTarget) -> bool:
        return rule_target == EffectTarget.BOTH or rule_target == subject_target

    def _make_trigger(
        self,
        rule: Rule,
        relation: SpatialRelation,
        feature: GeoFeature,
        context: dict[str, Any],
    ) -> RuleTrigger:
        subject_name = self._subject_name(relation.source_type, relation.source_id)
        attr_field = EFFECT_ATTR_TO_RELATION_FIELD.get(rule.effect_attr or "")
        attr_value = context.get(attr_field) if attr_field else None
        status = "blocked" if rule.calc_mode == CalcMode.FORBIDDEN else "cost_affected"
        explanation = self._explain(rule, relation, feature, attr_value)
        return RuleTrigger(
            subject_type=relation.source_type,
            subject_id=relation.source_id,
            subject_name=subject_name,
            feature_id=feature.id,
            feature_name=feature.name,
            relation=relation.relation,
            rule_id=rule.rule_id,
            rule_name=rule.rule_name,
            calc_mode=rule.calc_mode,
            effect_target=rule.effect_target,
            status=status,
            effect_value=rule.effect_value,
            effect_attr=rule.effect_attr,
            effect_unit=rule.effect_unit,
            attr_value=attr_value,
            source_table=rule.source_table,
            source_row=rule.source_row,
            explanation=explanation,
        )

    def _subject_name(self, subject_type: str, subject_id: str) -> str | None:
        if subject_type == "TowerSite":
            subject = self.tower_sites.get(subject_id)
            return subject.name if subject else None
        if subject_type == "LineSegment":
            subject = self.line_segments.get(subject_id)
            return subject.name if subject else None
        return None

    def _explain(self, rule: Rule, relation: SpatialRelation, feature: GeoFeature, attr_value: Any) -> str:
        subject_name = self._subject_name(relation.source_type, relation.source_id) or relation.source_id
        if rule.calc_mode == CalcMode.FORBIDDEN:
            return f"{subject_name} 与 {feature.name} 发生 {relation.relation.value}，命中“{rule.rule_name}”，因此对象不可用。"
        if attr_value is not None and rule.effect_unit:
            return f"{subject_name} 与 {feature.name} 发生 {relation.relation.value}，命中“{rule.rule_name}”，计量值为 {attr_value}，成本影响为 {rule.effect_value}{rule.effect_unit}。"
        if rule.effect_unit:
            return f"{subject_name} 与 {feature.name} 发生 {relation.relation.value}，命中“{rule.rule_name}”，成本影响为 {rule.effect_value}{rule.effect_unit}。"
        return f"{subject_name} 与 {feature.name} 发生 {relation.relation.value}，命中“{rule.rule_name}”。"

    def _status_by_subject(self, triggers: list[RuleTrigger]) -> dict[str, str]:
        status: dict[str, str] = {}
        for trigger in triggers:
            current = status.get(trigger.subject_id)
            if trigger.calc_mode == CalcMode.FORBIDDEN:
                status[trigger.subject_id] = "blocked"
            elif current != "blocked":
                status[trigger.subject_id] = "cost_affected"
        for site_id in self.tower_sites:
            status.setdefault(site_id, "available")
        for segment_id in self.line_segments:
            status.setdefault(segment_id, "available")
        return status
