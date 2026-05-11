from __future__ import annotations

import json
import os
from typing import Any

from .schemas import Condition, EvaluationReport, GeoFeature, LineSegment, Rule, SpatialRelation, TowerSite


def safe_rel_type(value: str) -> str:
    if not value.replace("_", "").isalnum() or not value.isupper():
        raise ValueError(f"Unsafe relationship type: {value}")
    return value


def prepare_node_props(row: dict[str, Any]) -> dict[str, Any]:
    props = dict(row)
    nested = props.pop("properties", None)
    if nested:
        props["properties_json"] = json.dumps(nested, ensure_ascii=False)
    return props


def condition_node_id(rule_id: str, path: str) -> str:
    return f"condition:{rule_id}:{path}"


def append_condition_rows(
    condition: Condition,
    rule_id: str,
    path: str,
    rows: list[dict[str, Any]],
    rels: list[dict[str, Any]],
) -> None:
    node_id = condition_node_id(rule_id, path)
    if condition.logic:
        rows.append(
            {
                "id": node_id,
                "rule_id": rule_id,
                "kind": "group",
                "logic": condition.logic.value,
                "path": path,
            }
        )
        for index, child in enumerate(condition.conditions or []):
            child_path = f"{path}.{index}"
            child_id = condition_node_id(rule_id, child_path)
            rels.append({"parent_id": node_id, "child_id": child_id, "order": index})
            append_condition_rows(child, rule_id, child_path, rows, rels)
        return

    rows.append(
        {
            "id": node_id,
            "rule_id": rule_id,
            "kind": "leaf",
            "field": condition.field,
            "operator": condition.operator.value if condition.operator else None,
            "value_json": json.dumps(condition.value, ensure_ascii=False),
            "path": path,
        }
    )


def collect_condition_fields(condition: Condition) -> set[str]:
    if condition.logic:
        fields: set[str] = set()
        for child in condition.conditions or []:
            fields.update(collect_condition_fields(child))
        return fields
    return {condition.field} if condition.field else set()


def cost_factor_id(rule: Rule) -> str:
    attr = rule.effect_attr or "none"
    return f"cost_factor:{rule.calc_mode.value}:{attr}"


class Neo4jWriter:
    def __init__(self) -> None:
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ModuleNotFoundError:
            pass
        from neo4j import GraphDatabase

        uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
        username = os.getenv("NEO4J_USERNAME", "neo4j")
        password = os.getenv("NEO4J_PASSWORD", "change-me")
        self.database = os.getenv("NEO4J_DATABASE", "neo4j")
        self.driver = GraphDatabase.driver(uri, auth=(username, password))

    def close(self) -> None:
        self.driver.close()

    def write_all(
        self,
        features: dict[str, GeoFeature],
        tower_sites: dict[str, TowerSite],
        line_segments: dict[str, LineSegment],
        spatial_relations: list[SpatialRelation],
        rules: list[Rule],
        report: EvaluationReport,
        base_cost_rules: list[dict[str, Any]] | None = None,
    ) -> None:
        base_cost_rules = base_cost_rules or []
        with self.driver.session(database=self.database) as session:
            session.execute_write(self._create_constraints)
            session.execute_write(self._cleanup_rule_artifacts, [rule.rule_id for rule in rules])
            session.execute_write(self._write_tower_sites, list(tower_sites.values()))
            session.execute_write(self._write_line_segments, list(line_segments.values()))
            session.execute_write(self._write_features, list(features.values()))
            session.execute_write(self._write_rules, rules)
            session.execute_write(self._write_rule_conditions, rules)
            session.execute_write(self._write_rule_metadata_relationships, rules)
            session.execute_write(self._write_base_cost_rules, base_cost_rules)
            session.execute_write(self._write_segment_links, list(line_segments.values()))
            session.execute_write(self._write_spatial_relations, spatial_relations)
            session.execute_write(self._write_feature_rule_matches, report)
            session.execute_write(self._write_triggers, report)

    @staticmethod
    def _create_constraints(tx: Any) -> None:
        statements = [
            "CREATE CONSTRAINT tower_site_id IF NOT EXISTS FOR (n:TowerSite) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT line_segment_id IF NOT EXISTS FOR (n:LineSegment) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT geo_feature_id IF NOT EXISTS FOR (n:GeoFeature) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT rule_id IF NOT EXISTS FOR (n:Rule) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT condition_id IF NOT EXISTS FOR (n:Condition) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT effect_target_id IF NOT EXISTS FOR (n:EffectTarget) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT field_id IF NOT EXISTS FOR (n:Field) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT cost_factor_id IF NOT EXISTS FOR (n:CostFactor) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT voltage_level_id IF NOT EXISTS FOR (n:VoltageLevel) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT base_cost_rule_id IF NOT EXISTS FOR (n:BaseCostRule) REQUIRE n.id IS UNIQUE",
        ]
        for statement in statements:
            tx.run(statement)

    @staticmethod
    def _cleanup_rule_artifacts(tx: Any, rule_ids: list[str]) -> None:
        tx.run(
            """
            MATCH ()-[r:MATCHES_RULE|TRIGGERS_CONSTRAINT|TRIGGERS_COST_RULE|AFFECTS_TARGET|USES_FIELD|HAS_COST_FACTOR]->()
            DELETE r
            """
        )
        tx.run("MATCH ()-[r:HAS_BASE_COST]->() DELETE r")
        tx.run("MATCH (c:Condition) DETACH DELETE c")
        tx.run("MATCH (n) WHERE n:EffectTarget OR n:Field OR n:CostFactor DETACH DELETE n")
        tx.run("MATCH (n) WHERE n:VoltageLevel OR n:BaseCostRule DETACH DELETE n")
        tx.run(
            """
            MATCH (r:Rule)
            WHERE NOT r.id IN $rule_ids
            DETACH DELETE r
            """,
            rule_ids=rule_ids,
        )

    @staticmethod
    def _write_tower_sites(tx: Any, sites: list[TowerSite]) -> None:
        tx.run(
            """
            UNWIND $rows AS row
            MERGE (n:TowerSite {id: row.id})
            SET n += row
            """,
            rows=[prepare_node_props(site.model_dump()) for site in sites],
        )

    @staticmethod
    def _write_line_segments(tx: Any, segments: list[LineSegment]) -> None:
        tx.run(
            """
            UNWIND $rows AS row
            MERGE (n:LineSegment {id: row.id})
            SET n += row
            """,
            rows=[prepare_node_props(segment.model_dump()) for segment in segments],
        )

    @staticmethod
    def _write_features(tx: Any, features: list[GeoFeature]) -> None:
        tx.run(
            """
            UNWIND $rows AS row
            MERGE (n:GeoFeature {id: row.id})
            SET n += row
            """,
            rows=[prepare_node_props(feature.model_dump()) for feature in features],
        )

    @staticmethod
    def _write_rules(tx: Any, rules: list[Rule]) -> None:
        rows = []
        for rule in rules:
            label = "ConstraintRule" if rule.is_constraint else "CostRule"
            row = rule.model_dump(mode="json")
            row["id"] = row.pop("rule_id")
            row["name"] = row.get("rule_name")
            row["match_condition_json"] = json.dumps(row["match_condition_json"], ensure_ascii=False)
            row["_label"] = label
            rows.append(row)
        for row in rows:
            label = row.pop("_label")
            tx.run(
                f"""
                MERGE (n:Rule:{label} {{id: $row.id}})
                SET n += $row
                """,
                row=row,
            )

    @staticmethod
    def _write_rule_conditions(tx: Any, rules: list[Rule]) -> None:
        condition_rows: list[dict[str, Any]] = []
        condition_rels: list[dict[str, Any]] = []
        root_rels: list[dict[str, str]] = []
        for rule in rules:
            root_id = condition_node_id(rule.rule_id, "root")
            root_rels.append({"rule_id": rule.rule_id, "condition_id": root_id})
            append_condition_rows(
                condition=rule.match_condition_json,
                rule_id=rule.rule_id,
                path="root",
                rows=condition_rows,
                rels=condition_rels,
            )
        tx.run(
            """
            UNWIND $rows AS row
            MERGE (c:Condition {id: row.id})
            SET c += row
            """,
            rows=condition_rows,
        )
        tx.run(
            """
            UNWIND $rows AS row
            MATCH (r:Rule {id: row.rule_id})
            MATCH (c:Condition {id: row.condition_id})
            MERGE (r)-[:HAS_CONDITION]->(c)
            """,
            rows=root_rels,
        )
        tx.run(
            """
            UNWIND $rows AS row
            MATCH (parent:Condition {id: row.parent_id})
            MATCH (child:Condition {id: row.child_id})
            MERGE (parent)-[rel:HAS_SUB_CONDITION]->(child)
            SET rel.order = row.order
            """,
            rows=condition_rels,
        )

    @staticmethod
    def _write_rule_metadata_relationships(tx: Any, rules: list[Rule]) -> None:
        target_rows = sorted({rule.effect_target.value for rule in rules})
        field_rows = sorted({field for rule in rules for field in collect_condition_fields(rule.match_condition_json)})
        cost_factor_rows = []
        rule_target_rels = []
        rule_field_rels = []
        rule_factor_rels = []
        seen_factors: set[str] = set()
        for rule in rules:
            rule_target_rels.append({"rule_id": rule.rule_id, "target_id": rule.effect_target.value})
            for field in collect_condition_fields(rule.match_condition_json):
                rule_field_rels.append({"rule_id": rule.rule_id, "field_id": field})
            if not rule.is_constraint:
                factor_id = cost_factor_id(rule)
                if factor_id not in seen_factors:
                    seen_factors.add(factor_id)
                    cost_factor_rows.append(
                        {
                            "id": factor_id,
                            "calc_mode": rule.calc_mode.value,
                            "effect_attr": rule.effect_attr,
                            "name": f"{rule.calc_mode.value}:{rule.effect_attr or 'none'}",
                        }
                    )
                rule_factor_rels.append({"rule_id": rule.rule_id, "factor_id": factor_id})
        tx.run(
            """
            UNWIND $rows AS id
            MERGE (target:EffectTarget {id: id})
            SET target.name = id
            """,
            rows=target_rows,
        )
        tx.run(
            """
            UNWIND $rows AS id
            MERGE (field:Field {id: id})
            SET field.name = id
            """,
            rows=field_rows,
        )
        tx.run(
            """
            UNWIND $rows AS row
            MERGE (factor:CostFactor {id: row.id})
            SET factor += row
            """,
            rows=cost_factor_rows,
        )
        tx.run(
            """
            UNWIND $rows AS row
            MATCH (rule:Rule {id: row.rule_id})
            MATCH (target:EffectTarget {id: row.target_id})
            MERGE (rule)-[:AFFECTS_TARGET]->(target)
            """,
            rows=rule_target_rels,
        )
        tx.run(
            """
            UNWIND $rows AS row
            MATCH (rule:Rule {id: row.rule_id})
            MATCH (field:Field {id: row.field_id})
            MERGE (rule)-[:USES_FIELD]->(field)
            """,
            rows=rule_field_rels,
        )
        tx.run(
            """
            UNWIND $rows AS row
            MATCH (rule:Rule {id: row.rule_id})
            MATCH (factor:CostFactor {id: row.factor_id})
            MERGE (rule)-[:HAS_COST_FACTOR]->(factor)
            """,
            rows=rule_factor_rels,
        )

    @staticmethod
    def _write_base_cost_rules(tx: Any, base_cost_rules: list[dict[str, Any]]) -> None:
        if not base_cost_rules:
            return
        tx.run(
            """
            UNWIND $rows AS row
            MERGE (v:VoltageLevel {id: 'voltage_level:' + row.voltage_level})
            SET v.name = row.voltage_level
            MERGE (b:BaseCostRule {id: row.id})
            SET b += row
            MERGE (v)-[:HAS_BASE_COST]->(b)
            """,
            rows=base_cost_rules,
        )

    @staticmethod
    def _write_segment_links(tx: Any, segments: list[LineSegment]) -> None:
        tx.run(
            """
            UNWIND $rows AS row
            MATCH (seg:LineSegment {id: row.id})
            MATCH (start:TowerSite {id: row.start_site_id})
            MATCH (end:TowerSite {id: row.end_site_id})
            MERGE (seg)-[:STARTS_FROM]->(start)
            MERGE (seg)-[:ENDS_AT]->(end)
            MERGE (start)-[:CONNECTS_TO]->(end)
            """,
            rows=[segment.model_dump() for segment in segments],
        )

    @staticmethod
    def _write_spatial_relations(tx: Any, relations: list[SpatialRelation]) -> None:
        for relation in relations:
            rel_type = safe_rel_type(relation.relation.value)
            props = dict(relation.properties)
            props.update(relation.metadata)
            tx.run(
                f"""
                MATCH (source {{id: $source_id}})
                MATCH (target {{id: $target_id}})
                MERGE (source)-[r:{rel_type}]->(target)
                SET r += $props
                """,
                source_id=relation.source_id,
                target_id=relation.target_id,
                props=props,
            )

    @staticmethod
    def _write_feature_rule_matches(tx: Any, report: EvaluationReport) -> None:
        rows_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for trigger in report.triggers:
            key = (trigger.feature_id, trigger.rule_id)
            row = rows_by_key.setdefault(
                key,
                {
                    "feature_id": trigger.feature_id,
                    "rule_id": trigger.rule_id,
                    "relations": [],
                    "subjects": [],
                },
            )
            if trigger.relation.value not in row["relations"]:
                row["relations"].append(trigger.relation.value)
            if trigger.subject_id not in row["subjects"]:
                row["subjects"].append(trigger.subject_id)
        tx.run(
            """
            UNWIND $rows AS row
            MATCH (feature:GeoFeature {id: row.feature_id})
            MATCH (rule:Rule {id: row.rule_id})
            MERGE (feature)-[r:MATCHES_RULE]->(rule)
            SET r.relations = row.relations,
                r.subjects = row.subjects,
                r.match_count = size(row.subjects)
            """,
            rows=list(rows_by_key.values()),
        )

    @staticmethod
    def _write_triggers(tx: Any, report: EvaluationReport) -> None:
        for trigger in report.triggers:
            rel_type = "TRIGGERS_CONSTRAINT" if trigger.calc_mode.value == "FORBIDDEN" else "TRIGGERS_COST_RULE"
            row = trigger.model_dump(mode="json")
            tx.run(
                f"""
                MATCH (subject {{id: $subject_id}})
                MATCH (rule:Rule {{id: $rule_id}})
                MERGE (subject)-[r:{rel_type}]->(rule)
                SET r += $props
                """,
                subject_id=trigger.subject_id,
                rule_id=trigger.rule_id,
                props=row,
            )
