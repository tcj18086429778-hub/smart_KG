"""Neo4j 图谱写入器。

本文件负责将成本规则和 GPKG 要素以统一的图谱模型写入 Neo4j 数据库。
提供两套图谱写入能力：

1. **走线决策图谱**（write_route_decision_graph）：
   RouteDecision -> RoutingFeature -> CostRule 的三层结构，
   支持成本栅格元数据的绑定和图层级别的 include/exclude 关系。
2. **RuleSet 目录图谱**（write_rule_set_catalog）：
   RuleSet -> CostRule、RuleSet -> RasterSpec、RasterSpec -> RoutingLayer
   的图式结构，供 graph_rule_source 在运行时查询。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from .logging_utils import instrument_class_methods

logger = logging.getLogger(__name__)


class Neo4jWriter:
    """Neo4j 图谱写入器。

    负责将 smart_KG 的运行结果以规范化的图模型写入 Neo4j，
    支持走线决策图谱和 RuleSet 目录图谱两种写入模式。
    """

    def __init__(self) -> None:
        """初始化 Neo4j 写入器。

        自动从环境变量 (.env) 读取连接参数，连接参数：
        - NEO4J_URI（默认 bolt://localhost:7687）
        - NEO4J_USERNAME（默认 neo4j）
        - NEO4J_PASSWORD（默认 change-me）
        - NEO4J_DATABASE（默认 neo4j）
        """
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
        logger.info("Neo4j 连接已建立：uri=%s, database=%s", uri, self.database)

    def close(self) -> None:
        """关闭 Neo4j 驱动连接。"""
        logger.info("关闭 Neo4j 连接")
        self.driver.close()

    # ── 走线决策图谱 ────────────────────────────────────────────────────────

    def write_route_decision_graph(
        self,
        entries: list[dict[str, Any]],
        features: list[dict[str, Any]],
        voltage_level: str,
        raster_metadata: dict[str, Any] | None = None,
        decision_id: str | None = None,
    ) -> dict[str, Any]:
        """将完整的走线决策图谱写入 Neo4j。

        图谱结构：
          RouteDecision -[:HAS_ROUTING_FEATURE]-> RoutingFeature
          RoutingFeature -[:TRIGGERED_BY_RULE]-> CostRule
          RouteDecision -[:INCLUDES_LAYER|EXCLUDES_LAYER]-> RoutingLayer
          RouteDecision -[:GENERATES_COST_SURFACE]-> CostSurface

        参数：
            entries: CostRuleEntry 字典列表。
            features: 成本化后的 GPKG 要素字典列表（不含塔位）。
            voltage_level: 电压等级（如 "110kV"）。
            raster_metadata: 可选的成本栅格元数据字典。
            decision_id: 可选的显式决策 ID。

        返回：
            包含 decision_id、rule_count、feature_count、forbidden_count、cost_count 的字典。
        """
        from .cost_rule_loader import CostRuleEntry

        decision_id = decision_id or (
            f"route_decision:{voltage_level}:"
            f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        )

        forbidden_count = sum(1 for f in features if f.get("calc_mode") == "FORBIDDEN")
        cost_count = sum(
            1
            for f in features
            if f.get("calc_mode") and f.get("calc_mode") != "FORBIDDEN"
        )

        prep_entries = [
            e if isinstance(e, dict)
            else CostRuleEntry.model_validate(e).model_dump(mode="json")
            for e in entries
        ]

        logger.info(
            "开始写入走线决策图谱：decision_id=%s, voltage_level=%s, rule_count=%s, feature_count=%s, forbidden=%s, cost_affected=%s",
            decision_id,
            voltage_level,
            len(prep_entries),
            len(features),
            forbidden_count,
            cost_count,
        )

        with self.driver.session(database=self.database) as session:
            session.execute_write(self._write_route_decision_constraints)
            session.execute_write(self._cleanup_route_decision_artifacts, decision_id)
            session.execute_write(
                self._write_route_decision_node, decision_id, voltage_level,
                {"total_features": len(features), "forbidden_count": forbidden_count,
                 "cost_count": cost_count, "rule_count": len(prep_entries)},
            )
            session.execute_write(self._write_cost_rule_entries_from_dicts, prep_entries)
            session.execute_write(self._write_routing_features, features, decision_id)
            if raster_metadata:
                session.execute_write(
                    self._write_route_cost_surface, decision_id, raster_metadata,
                )

        logger.info(
            "走线决策图谱写入完成：decision_id=%s, rule_count=%s, feature_count=%s",
            decision_id,
            len(prep_entries),
            len(features),
        )
        return {
            "decision_id": decision_id,
            "rule_count": len(prep_entries),
            "feature_count": len(features),
            "forbidden_count": forbidden_count,
            "cost_count": cost_count,
        }

    @staticmethod
    def _write_route_decision_constraints(tx: Any) -> None:
        """创建走线决策图谱的节点唯一性约束。"""
        for stmt in [
            "CREATE CONSTRAINT route_decision_id IF NOT EXISTS "
            "FOR (n:RouteDecision) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT routing_feature_id IF NOT EXISTS "
            "FOR (n:RoutingFeature) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT cost_rule_id IF NOT EXISTS "
            "FOR (n:CostRule) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT routing_layer_id IF NOT EXISTS "
            "FOR (n:RoutingLayer) REQUIRE n.id IS UNIQUE",
        ]:
            tx.run(stmt)

    @staticmethod
    def _cleanup_route_decision_artifacts(tx: Any, decision_id: str) -> None:
        """清理指定决策 ID 的已有图谱数据，支持重复导入。"""
        tx.run(
            """
            MATCH (rd:RouteDecision {id: $decision_id})
            OPTIONAL MATCH (rd)-[:HAS_ROUTING_FEATURE]->(rf:RoutingFeature)
            OPTIONAL MATCH (rd)-[:INCLUDES_LAYER|EXCLUDES_LAYER]->(rl:RoutingLayer)
            OPTIONAL MATCH (rd)-[:GENERATES_COST_SURFACE]->(cs:CostSurface)
            DETACH DELETE rd, rf, rl, cs
            """,
            decision_id=decision_id,
        )

    @staticmethod
    def _write_route_decision_node(
        tx: Any, decision_id: str, voltage_level: str,
        stats: dict[str, Any],
    ) -> None:
        """写入走线决策节点。"""
        now = datetime.now(timezone.utc).isoformat()
        tx.run(
            """
            MERGE (rd:RouteDecision {id: $row.id})
            SET rd.voltage_level = $row.voltage_level,
                rd.total_features = $row.total_features,
                rd.forbidden_count = $row.forbidden_count,
                rd.cost_count = $row.cost_count,
                rd.rule_count = $row.rule_count,
                rd.created_at = $row.created_at
            """,
            row={
                "id": decision_id,
                "voltage_level": voltage_level,
                "total_features": stats["total_features"],
                "forbidden_count": stats["forbidden_count"],
                "cost_count": stats["cost_count"],
                "rule_count": stats["rule_count"],
                "created_at": now,
            },
        )

    @staticmethod
    def _write_cost_rule_entries_from_dicts(
        tx: Any, entries: list[dict[str, Any]],
    ) -> None:
        """从字典列表写入 CostRule 节点。"""
        for entry in entries:
            row = dict(entry)
            match_json = row.pop("match_condition_json", {})
            row["match_condition_json"] = (
                json.dumps(match_json, ensure_ascii=False) if match_json else None
            )
            raw = row.pop("raw_value", None)
            if raw is not None:
                row["raw_value"] = str(raw)
            row["id"] = row.pop("rule_id")
            tx.run(
                """
                MERGE (cr:CostRule {id: $row.id})
                SET cr += $row
                """,
                row=row,
            )

    @staticmethod
    def _write_routing_features(
        tx: Any, features: list[dict[str, Any]], decision_id: str,
    ) -> None:
        """写入 RoutingFeature 节点及其与 CostRule 的关系。"""
        for feat in features:
            row = dict(feat)
            source_feat_id = row.pop("id")
            route_feature_id = f"{decision_id}:{source_feat_id}"
            rule_id = row.get("rule_id")

            tx.run(
                """
                MERGE (rf:RoutingFeature {id: $route_feature_id})
                SET rf += $props,
                    rf.source_feature_id = $source_feat_id,
                    rf.decision_id = $decision_id
                """,
                route_feature_id=route_feature_id,
                source_feat_id=source_feat_id,
                decision_id=decision_id,
                props=row,
            )

            if rule_id:
                tx.run(
                    """
                    MERGE (cr:CostRule {id: $rule_id})
                    ON CREATE SET cr.rule_origin = 'source_fallback',
                                 cr.enabled = false,
                                 cr.rule_name = $row_rule_name,
                                 cr.calc_mode = $row_calc_mode
                    WITH cr
                    MATCH (rf:RoutingFeature {id: $route_feature_id})
                    MERGE (rf)-[:TRIGGERED_BY_RULE]->(cr)
                    """,
                    route_feature_id=route_feature_id,
                    rule_id=rule_id,
                    row_rule_name=row.get("rule_name", ""),
                    row_calc_mode=row.get("calc_mode", ""),
                )

            tx.run(
                """
                MATCH (rd:RouteDecision {id: $decision_id})
                MATCH (rf:RoutingFeature {id: $route_feature_id})
                MERGE (rd)-[:HAS_ROUTING_FEATURE]->(rf)
                """,
                decision_id=decision_id,
                route_feature_id=route_feature_id,
            )

    @staticmethod
    def _write_route_cost_surface(
        tx: Any, decision_id: str, metadata: dict[str, Any],
    ) -> None:
        """写入走线决策关联的成本面节点和图层关系。"""
        surface_id = f"cost_surface:{decision_id}"
        included = metadata.get("included_layers", [])
        excluded = metadata.get("excluded_layers", [])
        m_stats = metadata.get("stats", {})
        row = {
            "id": surface_id,
            "run_id": decision_id,
            "cost_surface_path": metadata.get("cost_surface_path", ""),
            "blocked_mask_path": metadata.get("blocked_mask_path"),
            "reason_code_path": metadata.get("reason_code_path"),
            "metadata_path": metadata.get("_metadata_path", ""),
            "resolution": metadata.get("resolution", 0.0),
            "crs": metadata.get("crs", ""),
            "generated_at": metadata.get(
                "generated_at", datetime.now(timezone.utc).isoformat(),
            ),
        }
        for key, val in m_stats.items():
            row[f"stat_{key}"] = val
        tx.run(
            """
            MERGE (cs:CostSurface {id: $row.id})
            SET cs += $row
            """,
            row=row,
        )
        tx.run(
            """
            MATCH (rd:RouteDecision {id: $decision_id})
            MATCH (cs:CostSurface {id: $surface_id})
            MERGE (rd)-[:GENERATES_COST_SURFACE]->(cs)
            """,
            decision_id=decision_id,
            surface_id=surface_id,
        )
        Neo4jWriter._write_route_routing_layers(
            tx, decision_id, included, excluded,
        )

    # ── RuleSet 目录图谱层 ──────────────────────────────────────────────────

    def write_rule_set_catalog(
        self,
        entries: list[dict[str, Any]],
        voltage_level: str,
        rule_set_version: str | None = None,
        resolution: float | None = None,
        calculation_crs: str | None = None,
        base_cost: float | None = None,
        included_layers: list[str] | None = None,
        excluded_layers: list[str] | None = None,
        rule_set_id: str | None = None,
        raster_spec_id: str | None = None,
    ) -> dict[str, Any]:
        """写入 RuleSet 目录及关联的 RasterSpec 到 Neo4j。

        图谱结构：
          RuleSet -[:CONTAINS_RULE]-> CostRule
          RuleSet -[:USES_RASTER_SPEC]-> RasterSpec
          RasterSpec -[:INCLUDES_LAYER]-> RoutingLayer
          RasterSpec -[:EXCLUDES_LAYER]-> RoutingLayer

        参数：
            entries: CostRuleEntry 字典列表。
            voltage_level: 电压等级（如 "110kV"）。
            rule_set_version: 规则集版本标签。
            resolution: 栅格分辨率（米）。
            calculation_crs: 目标投影坐标系。
            base_cost: 基础走线成本。
            included_layers: 参与栅格计算的图层名列表。
            excluded_layers: 排除的图层名列表。
            rule_set_id: 可选显式 RuleSet ID。
            raster_spec_id: 可选显式 RasterSpec ID。

        返回：
            包含 rule_set_id、raster_spec_id、rule_count、voltage_level、rule_set_version 的字典。
        """
        from .cost_rule_loader import CostRuleEntry

        now_iso = datetime.now(timezone.utc).isoformat()
        version_tag = rule_set_version or datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")

        rule_set_id = rule_set_id or f"ruleset:{voltage_level}:{version_tag}"
        raster_spec_id = raster_spec_id or f"raster_spec:{voltage_level}:{version_tag}"

        prep_entries = [
            e if isinstance(e, dict)
            else CostRuleEntry.model_validate(e).model_dump(mode="json")
            for e in entries
        ]

        seen_ids: set[str] = set()
        unique_entries: list[dict[str, Any]] = []
        for e in prep_entries:
            rid = e.get("rule_id", "")
            if rid not in seen_ids:
                seen_ids.add(rid)
                unique_entries.append(e)

        rule_ids = [e["rule_id"] for e in unique_entries]

        logger.info(
            "开始写入 RuleSet 目录图谱：rule_set_id=%s, raster_spec_id=%s, voltage_level=%s, version=%s, rule_count=%s",
            rule_set_id,
            raster_spec_id,
            voltage_level,
            version_tag,
            len(unique_entries),
        )

        with self.driver.session(database=self.database) as session:
            session.execute_write(self._write_rule_set_constraints)
            session.execute_write(
                self._write_rule_set_catalog_tx,
                rule_set_id, voltage_level, version_tag,
                raster_spec_id, resolution, calculation_crs, base_cost,
                included_layers or [], excluded_layers or [],
                unique_entries, rule_ids, now_iso,
            )

        logger.info(
            "RuleSet 目录图谱写入完成：rule_set_id=%s, raster_spec_id=%s, rule_count=%s",
            rule_set_id,
            raster_spec_id,
            len(unique_entries),
        )
        return {
            "rule_set_id": rule_set_id,
            "raster_spec_id": raster_spec_id,
            "rule_count": len(unique_entries),
            "voltage_level": voltage_level,
            "rule_set_version": version_tag,
        }

    @staticmethod
    def _write_rule_set_constraints(tx: Any) -> None:
        """创建 RuleSet 目录图层的节点唯一性约束。"""
        for stmt in [
            "CREATE CONSTRAINT ruleset_id IF NOT EXISTS "
            "FOR (n:RuleSet) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT rasterspec_id IF NOT EXISTS "
            "FOR (n:RasterSpec) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT costrule_rid IF NOT EXISTS "
            "FOR (n:CostRule) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT routing_layer_id IF NOT EXISTS "
            "FOR (n:RoutingLayer) REQUIRE n.id IS UNIQUE",
        ]:
            tx.run(stmt)

    @staticmethod
    def _write_rule_set_catalog_tx(
        tx: Any,
        rule_set_id: str,
        voltage_level: str,
        version_tag: str,
        raster_spec_id: str,
        resolution: float | None,
        calculation_crs: str | None,
        base_cost: float | None,
        included_layers: list[str],
        excluded_layers: list[str],
        entries: list[dict[str, Any]],
        rule_ids: list[str],
        now_iso: str,
    ) -> None:
        """在单事务内完成 RuleSet 目录的完整写入。"""
        rs_props: dict[str, Any] = {
            "id": rule_set_id,
            "voltage_level": voltage_level,
            "rule_set_version": version_tag,
            "status": "active",
            "total_rules": len(entries),
            "generated_at": now_iso,
        }
        tx.run(
            """
            MERGE (rs:RuleSet {id: $props.id})
            SET rs += $props
            """,
            props=rs_props,
        )

        for entry in entries:
            row = dict(entry)
            match_json = row.pop("match_condition_json", {})
            row["match_condition_json"] = (
                json.dumps(match_json, ensure_ascii=False) if match_json else None
            )
            raw = row.pop("raw_value", None)
            if raw is not None:
                row["raw_value"] = str(raw)
            row["id"] = row.pop("rule_id")
            tx.run(
                """
                MERGE (cr:CostRule {id: $row.id})
                SET cr += $row
                """,
                row=row,
            )
            tx.run(
                """
                MATCH (rs:RuleSet {id: $rule_set_id})
                MATCH (cr:CostRule {id: $rule_id})
                MERGE (rs)-[:CONTAINS_RULE]->(cr)
                """,
                rule_set_id=rule_set_id,
                rule_id=row["id"],
            )

        spec_props: dict[str, Any] = {
            "id": raster_spec_id,
            "resolution": resolution,
            "base_cost": base_cost,
            "calculation_crs": calculation_crs,
            "crs": calculation_crs,
            "voltage_level": voltage_level,
            "generated_at": now_iso,
        }
        tx.run(
            """
            MERGE (spec:RasterSpec {id: $props.id})
            SET spec += $props
            """,
            props=spec_props,
        )
        tx.run(
            """
            MATCH (rs:RuleSet {id: $rule_set_id})
            MATCH (spec:RasterSpec {id: $raster_spec_id})
            MERGE (rs)-[:USES_RASTER_SPEC]->(spec)
            """,
            rule_set_id=rule_set_id,
            raster_spec_id=raster_spec_id,
        )

        for layer_name in included_layers:
            layer_id = f"routing_layer:{layer_name}:{raster_spec_id}"
            tx.run(
                """
                MERGE (rl:RoutingLayer {id: $layer_id})
                SET rl.name = $layer_name
                """,
                layer_id=layer_id, layer_name=layer_name,
            )
            tx.run(
                """
                MATCH (spec:RasterSpec {id: $raster_spec_id})
                MATCH (rl:RoutingLayer {id: $layer_id})
                MERGE (spec)-[:INCLUDES_LAYER]->(rl)
                """,
                raster_spec_id=raster_spec_id, layer_id=layer_id,
            )
        for layer_name in excluded_layers:
            layer_id = f"routing_layer:{layer_name}:{raster_spec_id}"
            tx.run(
                """
                MERGE (rl:RoutingLayer {id: $layer_id})
                SET rl.name = $layer_name
                """,
                layer_id=layer_id, layer_name=layer_name,
            )
            tx.run(
                """
                MATCH (spec:RasterSpec {id: $raster_spec_id})
                MATCH (rl:RoutingLayer {id: $layer_id})
                MERGE (spec)-[:EXCLUDES_LAYER]->(rl)
                """,
                raster_spec_id=raster_spec_id, layer_id=layer_id,
            )

    @staticmethod
    def _write_route_routing_layers(
        tx: Any, decision_id: str,
        included: list[str], excluded: list[str],
    ) -> None:
        """为走线决策创建 RoutingLayer 节点和 INCLUDES_LAYER / EXCLUDES_LAYER 关系。"""
        for layer_name in included:
            layer_id = f"routing_layer:{layer_name}:{decision_id}"
            tx.run(
                """
                MERGE (rl:RoutingLayer {id: $layer_id})
                SET rl.name = $layer_name
                """,
                layer_id=layer_id, layer_name=layer_name,
            )
            tx.run(
                """
                MATCH (rd:RouteDecision {id: $decision_id})
                MATCH (rl:RoutingLayer {id: $layer_id})
                MERGE (rd)-[:INCLUDES_LAYER]->(rl)
                """,
                decision_id=decision_id, layer_id=layer_id,
            )
        for layer_name in excluded:
            layer_id = f"routing_layer:{layer_name}:{decision_id}"
            tx.run(
                """
                MERGE (rl:RoutingLayer {id: $layer_id})
                SET rl.name = $layer_name
                """,
                layer_id=layer_id, layer_name=layer_name,
            )
            tx.run(
                """
                MATCH (rd:RouteDecision {id: $decision_id})
                MATCH (rl:RoutingLayer {id: $layer_id})
                MERGE (rd)-[:EXCLUDES_LAYER]->(rl)
                """,
                decision_id=decision_id, layer_id=layer_id,
            )


instrument_class_methods(Neo4jWriter, logger)
