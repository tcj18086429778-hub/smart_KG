"""图谱规则读取与图谱驱动执行入口。

本文件负责从 Neo4j 中读取当前激活的 `RuleSet`、`CostRule` 与 `RasterSpec`，
并将其转成现有文件驱动执行器可以直接消费的对象，从而实现
“GPKG -> 图谱查规则 -> 成本化 -> 栅格化”的闭环。

主要对象：
1. `GraphRasterSpec`：图谱中栅格执行配置的本地表示。
2. `GraphRuleBundle`：图谱规则集与执行配置的打包对象。
3. `load_graph_rule_bundle`：从 Neo4j 拉取规则与栅格配置。
4. `run_route_pipeline_from_graph`：图谱驱动的完整工作流入口。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cost_rule_loader import CostRuleEntry
from .gpkg_standardizer import standardize_gpkg
from .logging_utils import instrument_module_functions, summarize_for_log
from .raster_executor import build_cost_raster


logger = logging.getLogger(__name__)


@dataclass
class GraphRasterSpec:
    """图谱中栅格执行配置的本地映射对象。"""

    spec_id: str | None = None
    resolution: float | None = None
    base_cost: float | None = None
    calculation_crs: str | None = None
    included_layers: list[str] = field(default_factory=list)
    excluded_layers: list[str] = field(default_factory=list)
    source: str = "default"


@dataclass
class GraphRuleBundle:
    """图谱规则集与执行配置的打包结果。"""

    voltage_level: str
    rules: list[CostRuleEntry]
    raster_spec: GraphRasterSpec
    rule_set_id: str | None = None
    rule_set_version: str | None = None


def load_graph_rule_bundle(
    voltage_level: str,
    rule_set_version: str | None = None,
) -> GraphRuleBundle:
    """从 Neo4j 读取指定电压等级的规则集与栅格配置。"""

    if not voltage_level:
        raise ValueError("voltage_level is required")

    logger.info("开始从图谱读取规则集：voltage_level=%s, rule_set_version=%s", voltage_level, rule_set_version)
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ModuleNotFoundError:
        logger.debug("未安装 python-dotenv，跳过 .env 加载。")
    from neo4j import GraphDatabase

    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    username = os.getenv("NEO4J_USERNAME", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "change-me")
    database = os.getenv("NEO4J_DATABASE", "neo4j")
    logger.debug("Neo4j 连接参数：uri=%s, username=%s, database=%s", uri, username, database)

    with GraphDatabase.driver(uri, auth=(username, password)) as driver:
        with driver.session(database=database) as session:
            rule_set_row = session.run(
                """
                MATCH (rs:RuleSet)
                WHERE rs.voltage_level = $voltage_level
                  AND coalesce(rs.status, 'active') = 'active'
                  AND ($rule_set_version IS NULL OR rs.rule_set_version = $rule_set_version)
                RETURN properties(rs) AS props
                ORDER BY coalesce(rs.generated_at, rs.created_at, '') DESC
                LIMIT 1
                """,
                voltage_level=voltage_level,
                rule_set_version=rule_set_version,
            ).single()

            if rule_set_row:
                rule_set_props = dict(rule_set_row["props"])
                rule_set_id = str(rule_set_props.get("id", ""))
                logger.info("命中 RuleSet：rule_set_id=%s, props=%s", rule_set_id, summarize_for_log(rule_set_props))
                rule_rows = list(
                    session.run(
                        """
                        MATCH (rs:RuleSet {id: $rule_set_id})-[:CONTAINS_RULE]->(cr:CostRule)
                        RETURN properties(cr) AS props
                        ORDER BY coalesce(cr.priority, 0) DESC, cr.id ASC
                        """,
                        rule_set_id=rule_set_id,
                    )
                )
                logger.debug("RuleSet 关联规则条数：rule_set_id=%s, count=%s", rule_set_id, len(rule_rows))
                spec_row = session.run(
                    """
                    MATCH (rs:RuleSet {id: $rule_set_id})-[:USES_RASTER_SPEC]->(spec:RasterSpec)
                    OPTIONAL MATCH (spec)-[:INCLUDES_LAYER]->(inc:RoutingLayer)
                    OPTIONAL MATCH (spec)-[:EXCLUDES_LAYER]->(exc:RoutingLayer)
                    RETURN properties(spec) AS props,
                           collect(DISTINCT inc.name) AS included_layers,
                           collect(DISTINCT exc.name) AS excluded_layers
                    LIMIT 1
                    """,
                    rule_set_id=rule_set_id,
                ).single()
                spec = _raster_spec_from_graph(spec_row)
                logger.info(
                    "RuleSet 栅格配置已加载：rule_set_id=%s, spec_id=%s, included_layers=%s, excluded_layers=%s",
                    rule_set_id,
                    spec.spec_id,
                    spec.included_layers,
                    spec.excluded_layers,
                )
                return GraphRuleBundle(
                    voltage_level=voltage_level,
                    rules=[_cost_rule_entry_from_props(dict(row["props"])) for row in rule_rows],
                    raster_spec=spec,
                    rule_set_id=rule_set_id,
                    rule_set_version=_str_or_none(rule_set_props.get("rule_set_version")),
                )

            logger.warning("未命中 RuleSet，开始回退到 RouteDecision 图谱：voltage_level=%s", voltage_level)
            rule_rows = list(
                session.run(
                    """
                    MATCH (cr:CostRule)
                    WHERE NOT cr:Rule
                      AND coalesce(cr.enabled, true) = true
                      AND (cr.voltage_level IS NULL OR cr.voltage_level = $voltage_level)
                    RETURN properties(cr) AS props
                    ORDER BY coalesce(cr.priority, 0) DESC, cr.id ASC
                    """,
                    voltage_level=voltage_level,
                )
            )
            spec_row = session.run(
                """
                MATCH (rd:RouteDecision {voltage_level: $voltage_level})
                WITH rd
                ORDER BY rd.created_at DESC
                LIMIT 1
                OPTIONAL MATCH (rd)-[:GENERATES_COST_SURFACE]->(cs:CostSurface)
                OPTIONAL MATCH (rd)-[:INCLUDES_LAYER]->(inc:RoutingLayer)
                OPTIONAL MATCH (rd)-[:EXCLUDES_LAYER]->(exc:RoutingLayer)
                RETURN properties(cs) AS props,
                       collect(DISTINCT inc.name) AS included_layers,
                       collect(DISTINCT exc.name) AS excluded_layers
                """,
                voltage_level=voltage_level,
            ).single()
            spec = _raster_spec_from_graph(spec_row, source="route_decision_fallback")
            logger.info(
                "RouteDecision 回退加载完成：voltage_level=%s, rule_count=%s, spec=%s",
                voltage_level,
                len(rule_rows),
                summarize_for_log(spec),
            )
            return GraphRuleBundle(
                voltage_level=voltage_level,
                rules=[_cost_rule_entry_from_props(dict(row["props"])) for row in rule_rows],
                raster_spec=spec,
                rule_set_id=None,
                rule_set_version=rule_set_version,
            )


def standardize_gpkg_from_graph(
    source_gpkg: Path,
    out_gpkg: Path,
    voltage_level: str,
    rule_set_version: str | None = None,
    layers: list[str] | None = None,
) -> tuple[dict[str, Any], GraphRuleBundle]:
    """使用图谱规则对输入 GPKG 补齐成本字段。"""

    logger.info(
        "开始执行图谱驱动 GPKG 成本化：source_gpkg=%s, out_gpkg=%s, voltage_level=%s, rule_set_version=%s, layers=%s",
        source_gpkg,
        out_gpkg,
        voltage_level,
        rule_set_version,
        layers,
    )
    bundle = load_graph_rule_bundle(voltage_level=voltage_level, rule_set_version=rule_set_version)
    stats = standardize_gpkg(
        source_gpkg=source_gpkg,
        out_gpkg=out_gpkg,
        voltage_level=voltage_level,
        rules=bundle.rules,
        layers=layers,
    )
    logger.info(
        "图谱驱动 GPKG 成本化完成：rule_set_id=%s, enriched=%s/%s",
        bundle.rule_set_id,
        stats.get("enriched"),
        stats.get("total_features"),
    )
    return stats, bundle


def build_cost_raster_from_graph(
    gpkg_path: Path,
    out_dir: Path,
    voltage_level: str,
    rule_set_version: str | None = None,
    resolution: float | None = None,
    calculation_crs: str | None = None,
    base_cost: float | None = None,
) -> tuple[dict[str, Any], GraphRuleBundle]:
    """使用图谱中的 `RasterSpec` 对成本化 GPKG 构建栅格。"""

    logger.info(
        "开始执行图谱驱动栅格构建：gpkg_path=%s, out_dir=%s, voltage_level=%s, rule_set_version=%s",
        gpkg_path,
        out_dir,
        voltage_level,
        rule_set_version,
    )
    bundle = load_graph_rule_bundle(voltage_level=voltage_level, rule_set_version=rule_set_version)
    spec = bundle.raster_spec
    logger.debug("图谱栅格配置：%s", summarize_for_log(spec))
    metadata = build_cost_raster(
        gpkg_path=gpkg_path,
        out_dir=out_dir,
        voltage_level=voltage_level,
        resolution=resolution or spec.resolution or 20.0,
        calculation_crs=calculation_crs or spec.calculation_crs,
        base_cost=base_cost or spec.base_cost or 1.0,
        included_layers=spec.included_layers or None,
        excluded_layers=spec.excluded_layers or None,
    )
    metadata["graph_rule_set_id"] = bundle.rule_set_id
    metadata["graph_rule_set_version"] = bundle.rule_set_version
    metadata["graph_raster_spec_source"] = spec.source
    logger.info(
        "图谱驱动栅格构建完成：rule_set_id=%s, cost_surface_path=%s, blocked_pixels=%s",
        bundle.rule_set_id,
        metadata.get("cost_surface_path"),
        metadata.get("stats", {}).get("total_blocked_pixels"),
    )
    return metadata, bundle


def run_route_pipeline_from_graph(
    source_gpkg: Path,
    out_gpkg: Path,
    raster_out_dir: Path,
    voltage_level: str,
    rule_set_version: str | None = None,
    resolution: float | None = None,
    calculation_crs: str | None = None,
    base_cost: float | None = None,
) -> dict[str, Any]:
    """执行图谱驱动的全链路工作流。"""

    logger.info(
        "开始执行图谱驱动全链路：source_gpkg=%s, out_gpkg=%s, raster_out_dir=%s, voltage_level=%s, rule_set_version=%s",
        source_gpkg,
        out_gpkg,
        raster_out_dir,
        voltage_level,
        rule_set_version,
    )
    stats, bundle = standardize_gpkg_from_graph(
        source_gpkg=source_gpkg,
        out_gpkg=out_gpkg,
        voltage_level=voltage_level,
        rule_set_version=rule_set_version,
    )
    metadata, _ = build_cost_raster_from_graph(
        gpkg_path=out_gpkg,
        out_dir=raster_out_dir,
        voltage_level=voltage_level,
        rule_set_version=rule_set_version,
        resolution=resolution,
        calculation_crs=calculation_crs,
        base_cost=base_cost,
    )
    result = {
        "voltage_level": voltage_level,
        "graph_rule_set_id": bundle.rule_set_id,
        "graph_rule_set_version": bundle.rule_set_version,
        "costed_gpkg_path": str(out_gpkg),
        "raster_out_dir": str(raster_out_dir),
        "gpkg_stats": stats,
        "raster_metadata": metadata,
    }
    logger.info("图谱驱动全链路结束：result=%s", summarize_for_log(result))
    return result


def _raster_spec_from_graph(row: Any, source: str = "ruleset") -> GraphRasterSpec:
    """将 Neo4j 查询结果转换为本地栅格配置对象。"""

    if not row:
        logger.debug("图谱未返回 RasterSpec，回退到默认配置。")
        return GraphRasterSpec(source="default")

    props = dict(row.get("props") or {})
    resolution = _float_or_none(props.get("resolution"))
    base_cost = _float_or_none(props.get("base_cost"))
    calculation_crs = _str_or_none(props.get("calculation_crs") or props.get("crs"))
    spec = GraphRasterSpec(
        spec_id=_str_or_none(props.get("id")),
        resolution=resolution,
        base_cost=base_cost,
        calculation_crs=calculation_crs,
        included_layers=_clean_layer_names(row.get("included_layers", [])),
        excluded_layers=_clean_layer_names(row.get("excluded_layers", [])),
        source=source,
    )
    logger.debug("已解析 RasterSpec：%s", summarize_for_log(spec))
    return spec


def _cost_rule_entry_from_props(props: dict[str, Any]) -> CostRuleEntry:
    """将图谱中的 `CostRule` 属性转换成 `CostRuleEntry`。"""

    payload = dict(props)
    payload["rule_id"] = _str_or_none(payload.pop("id")) or ""
    payload["rule_name"] = _str_or_none(payload.get("rule_name")) or payload["rule_id"]
    payload["source_table"] = _str_or_none(payload.get("source_table")) or "neo4j"
    payload["source_row"] = _int_or_default(payload.get("source_row"), 0)
    payload["effect_target"] = _str_or_none(payload.get("effect_target")) or "ALL"
    payload["enabled"] = bool(payload.get("enabled", True))
    payload["priority"] = _int_or_default(payload.get("priority"), 100)
    payload["reason_code"] = _int_or_default(
        payload.get("reason_code"),
        _fallback_reason_code(payload["rule_id"], payload.get("calc_mode")),
    )
    payload["match_condition_json"] = _load_match_condition(payload.get("match_condition_json"))
    payload["effect_value"] = _float_or_none(payload.get("effect_value"))
    payload["buffer_distance_m"] = _float_or_none(payload.get("buffer_distance_m"))
    payload["raw_value"] = payload.get("raw_value")
    logger.debug(
        "开始构造 CostRuleEntry：rule_id=%s, rule_name=%s, calc_mode=%s, reason_code=%s",
        payload["rule_id"],
        payload["rule_name"],
        payload.get("calc_mode"),
        payload["reason_code"],
    )
    return CostRuleEntry.model_validate(payload)


def _load_match_condition(value: Any) -> dict[str, Any]:
    """解析图谱中保存的匹配条件 JSON。"""

    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("匹配条件 JSON 解析失败，返回空条件：value=%s", value)
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _fallback_reason_code(rule_id: str, calc_mode: Any) -> int:
    """在图谱中未显式提供原因编码时生成稳定回退值。"""

    seed = f"{rule_id}:{calc_mode or ''}"
    digest = hashlib.md5(seed.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 90000 + (1000 if str(calc_mode) == "FORBIDDEN" else 2000)


def _clean_layer_names(values: list[Any]) -> list[str]:
    """清洗图谱返回的图层名列表并去重。"""

    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _str_or_none(value)
        if not text:
            continue
        if text.lower() in seen:
            continue
        seen.add(text.lower())
        cleaned.append(text)
    return cleaned


def _str_or_none(value: Any) -> str | None:
    """将任意值转成去空白字符串；空字符串转成 `None`。"""

    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _float_or_none(value: Any) -> float | None:
    """将任意值转成浮点数；失败时返回 `None`。"""

    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


def _int_or_default(value: Any, default: int) -> int:
    """将任意值转成整数；失败时返回默认值。"""

    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


instrument_module_functions(globals(), logger)
