"""命令行入口。

本文件负责注册 smart_kg 项目的命令行命令，将用户输入分发到
成本规则标准化、GPKG 成本化、图谱导入和栅格构建等模块。

当前主链路：
1. standardize-cost-rules：Excel → 标准化成本规则 JSON
2. cost-gpkg / cost-gpkg-from-graph：GPKG 成本字段填充
3. build-cost-raster / build-cost-raster-from-graph：成本栅格构建
4. import-cost-rules-neo4j / import-rule-set-neo4j：Neo4j 图谱写入
5. list-rule-sets / run-route-pipeline-from-graph：图谱查询与全链路管道
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .cost_rule_loader import standardize_cost_rules as _standardize_cost_rules
from .logging_utils import configure_logging, instrument_module_functions


logger = logging.getLogger(__name__)


def _log_command_start(command_name: str, **kwargs: object) -> None:
    logger.info("开始执行命令：%s", command_name)
    if kwargs:
        logger.debug("命令入参：%s -> %s", command_name, kwargs)


def run_serve(args: argparse.Namespace) -> None:
    """启动 FastAPI 服务。"""
    import uvicorn

    _log_command_start("serve", host=args.host, port=args.port, reload=args.reload)
    uvicorn.run("smart_kg.api:app", host=args.host, port=args.port, reload=args.reload)


def run_standardize_cost_rules(args: argparse.Namespace) -> None:
    """将 Excel 成本分类规则表抽取为标准 JSON。

    从成本分类体系 Excel 中提取禁建规则和成本规则，
    输出为可被 GPKG 成本化和 Neo4j 入库直接使用的 CostRuleEntry 列表。
    """
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows = _standardize_cost_rules(
        excel_path=Path(args.excel),
        out_path=out_path,
        rule_set_version=args.rule_set_version,
    )
    print(f"Standardized {len(rows)} cost rules to: {out_path}")


def run_import_cost_rules_neo4j(args: argparse.Namespace) -> None:
    """从成本规则 + 成本化 GPKG 导入走线决策图谱到 Neo4j。

    加载标准化成本规则和 GPKG 图层要素，构建 RouteDecision 节点、
    CostRule 节点、RoutingFeature 节点及其 TRIGGERED_BY_RULE 关联，
    可选关联栅格成本面元数据。
    """
    from .neo4j_writer import Neo4jWriter
    from .cost_rule_loader import load_cost_rules
    from .gpkg_standardizer import read_enriched_gpkg_features

    _log_command_start(
        "import-cost-rules-neo4j",
        rules=args.rules,
        gpkg=args.gpkg,
        voltage_level=args.voltage_level,
        metadata=args.metadata,
    )
    rules_path = Path(args.rules)
    entries = load_cost_rules(rules_path)

    gpkg_path = Path(args.gpkg)
    features = read_enriched_gpkg_features(gpkg_path)

    metadata = None
    if args.metadata:
        metadata_path = Path(args.metadata)
        if metadata_path.exists():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["_metadata_path"] = str(metadata_path)

    voltage_level = args.voltage_level

    writer = Neo4jWriter()
    try:
        logger.info(
            "开始写入走线决策图谱：rule_count=%s, feature_count=%s, voltage_level=%s",
            len(entries),
            len(features),
            voltage_level,
        )
        result = writer.write_route_decision_graph(
            entries=[e.model_dump(mode="json") for e in entries],
            features=features,
            voltage_level=voltage_level,
            raster_metadata=metadata,
        )
    finally:
        writer.close()

    logger.info("走线决策图谱写入完成：decision_id=%s", result["decision_id"])
    print("Imported route-decision graph into Neo4j.")
    print(f"  decision_id: {result['decision_id']}")
    print(f"  cost rules: {result['rule_count']}")
    print(f"  routing features: {result['feature_count']}")
    print(f"  forbidden: {result['forbidden_count']}")
    print(f"  cost-affected: {result['cost_count']}")
    if metadata:
        print(f"  cost surface stats: {json.dumps(metadata.get('stats', {}), ensure_ascii=False)}")


def run_cost_gpkg(args: argparse.Namespace) -> None:
    """对原始 GPKG 执行标准化和成本字段填充。

    读取成本规则 JSON，对源 GPKG 中各图层的要素进行规则匹配，
    将 C_CALC_MD、C_EFF_VAL、C_REASON_CD 等成本相关字段写入输出 GPKG。
    """
    from .gpkg_standardizer import standardize_gpkg
    from .cost_rule_loader import load_cost_rules

    _log_command_start(
        "cost-gpkg",
        source_gpkg=args.source_gpkg,
        out_gpkg=args.out_gpkg,
        voltage_level=args.voltage_level,
        rules=args.rules,
    )
    source = Path(args.source_gpkg)
    out = Path(args.out_gpkg)
    rules_path = Path(args.rules) if args.rules else None
    rules = load_cost_rules(rules_path) if rules_path else None
    stats = standardize_gpkg(
        source_gpkg=source,
        out_gpkg=out,
        voltage_level=args.voltage_level,
        rules=rules,
    )
    logger.info("GPKG 成本化完成：source_gpkg=%s, out_gpkg=%s, stats=%s", source, out, stats)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    print(f"\nCosted GPKG written to: {out}")


def run_cost_gpkg_from_graph(args: argparse.Namespace) -> None:
    """使用 Neo4j 图谱中的活跃规则对 GPKG 进行成本化。

    从 Neo4j 图谱中读取指定电压等级的活跃 CostRule 和 RasterSpec，
    再对源 GPKG 执行规则匹配与成本字段填充，全程由图谱驱动。
    """
    from .graph_rule_source import standardize_gpkg_from_graph

    _log_command_start(
        "cost-gpkg-from-graph",
        source_gpkg=args.source_gpkg,
        out_gpkg=args.out_gpkg,
        voltage_level=args.voltage_level,
        rule_set_version=args.rule_set_version,
    )
    stats, bundle = standardize_gpkg_from_graph(
        source_gpkg=Path(args.source_gpkg),
        out_gpkg=Path(args.out_gpkg),
        voltage_level=args.voltage_level,
        rule_set_version=args.rule_set_version,
    )
    logger.info(
        "图谱驱动 GPKG 成本化完成：rule_set_id=%s, out_gpkg=%s, enriched=%s/%s",
        bundle.rule_set_id,
        args.out_gpkg,
        stats.get("enriched"),
        stats.get("total_features"),
    )
    print(json.dumps(
        {
            "graph_rule_set_id": bundle.rule_set_id,
            "graph_rule_set_version": bundle.rule_set_version,
            "stats": stats,
        },
        ensure_ascii=False,
        indent=2,
    ))
    print(f"\nGraph-driven costed GPKG written to: {args.out_gpkg}")


def run_build_cost_raster(args: argparse.Namespace) -> None:
    """根据成本化 GPKG 构建栅格成本面。

    将成本化 GPKG 中的矢量要素栅格化，输出 cost_surface.tif、
    blocked_mask.tif、reason_code.tif 和成本预览图等栅格文件。
    """
    from .raster_executor import build_cost_raster

    _log_command_start(
        "build-cost-raster",
        gpkg=args.gpkg,
        out_dir=args.out_dir,
        voltage_level=args.voltage_level,
        resolution=args.resolution,
        calculation_crs=args.calculation_crs,
        base_cost=args.base_cost,
    )
    gpkg = Path(args.gpkg)
    out_dir = Path(args.out_dir)
    metadata = build_cost_raster(
        gpkg_path=gpkg,
        out_dir=out_dir,
        voltage_level=args.voltage_level,
        resolution=args.resolution,
        calculation_crs=args.calculation_crs,
        base_cost=args.base_cost,
    )
    logger.info("成本栅格构建完成：gpkg=%s, out_dir=%s, cost_surface=%s", gpkg, out_dir, metadata.get("cost_surface_path"))
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    print(f"\nRaster outputs written to: {out_dir}")


def run_build_cost_raster_from_graph(args: argparse.Namespace) -> None:
    """使用 Neo4j 图谱中的 RasterSpec 参数构建栅格成本面。

    从 Neo4j 图谱中读取栅格规格（分辨率、CRS、基础成本、图层过滤），
    然后将成本化 GPKG 栅格化为成本面及禁建遮罩等输出文件。
    """
    from .graph_rule_source import build_cost_raster_from_graph

    _log_command_start(
        "build-cost-raster-from-graph",
        gpkg=args.gpkg,
        out_dir=args.out_dir,
        voltage_level=args.voltage_level,
        rule_set_version=args.rule_set_version,
        resolution=args.resolution,
        calculation_crs=args.calculation_crs,
        base_cost=args.base_cost,
    )
    metadata, bundle = build_cost_raster_from_graph(
        gpkg_path=Path(args.gpkg),
        out_dir=Path(args.out_dir),
        voltage_level=args.voltage_level,
        rule_set_version=args.rule_set_version,
        resolution=args.resolution,
        calculation_crs=args.calculation_crs,
        base_cost=args.base_cost,
    )
    logger.info(
        "图谱驱动栅格构建完成：rule_set_id=%s, out_dir=%s, blocked_pixels=%s",
        bundle.rule_set_id,
        args.out_dir,
        metadata.get("stats", {}).get("total_blocked_pixels"),
    )
    print(json.dumps(
        {
            "graph_rule_set_id": bundle.rule_set_id,
            "graph_rule_set_version": bundle.rule_set_version,
            "metadata": metadata,
        },
        ensure_ascii=False,
        indent=2,
    ))
    print(f"\nGraph-driven raster outputs written to: {args.out_dir}")


def run_import_rule_set_neo4j(args: argparse.Namespace) -> None:
    """将 RuleSet 目录与 RasterSpec 导入 Neo4j 图谱目录层。

    写入 RuleSet 节点、RasterSpec 节点、CostRule 节点的目录索引，
    并建立 CONTAINS_RULE、USES_RASTER_SPEC、INCLUDES_LAYER、
    EXCLUDES_LAYER 等图谱目录关系。
    """
    from .neo4j_writer import Neo4jWriter
    from .cost_rule_loader import load_cost_rules

    _log_command_start(
        "import-rule-set-neo4j",
        rules=args.rules,
        voltage_level=args.voltage_level,
        rule_set_version=args.rule_set_version,
        metadata=args.metadata,
    )
    rules_path = Path(args.rules)
    entries = load_cost_rules(rules_path)

    metadata = None
    if args.metadata:
        metadata_path = Path(args.metadata)
        if metadata_path.exists():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    included = [layer.strip() for layer in args.included_layers.split(",")] if args.included_layers else None
    excluded = [layer.strip() for layer in args.excluded_layers.split(",")] if args.excluded_layers else None
    resolution = args.resolution
    calculation_crs = args.calculation_crs
    base_cost = args.base_cost

    if metadata:
        if included is None:
            included = list(metadata.get("included_layers") or [])
        if excluded is None:
            excluded = list(metadata.get("excluded_layers") or [])
        if resolution is None:
            resolution = metadata.get("resolution")
        if calculation_crs is None:
            calculation_crs = metadata.get("crs")
        if base_cost is None:
            base_cost = metadata.get("base_cost")

    writer = Neo4jWriter()
    try:
        logger.info(
            "开始写入 RuleSet 图谱目录：voltage_level=%s, rule_set_version=%s, rule_count=%s",
            args.voltage_level,
            args.rule_set_version,
            len(entries),
        )
        result = writer.write_rule_set_catalog(
            entries=[e.model_dump(mode="json") for e in entries],
            voltage_level=args.voltage_level,
            rule_set_version=args.rule_set_version,
            resolution=resolution,
            calculation_crs=calculation_crs,
            base_cost=base_cost,
            included_layers=included,
            excluded_layers=excluded,
        )
    finally:
        writer.close()

    logger.info("RuleSet 图谱目录写入完成：rule_set_id=%s, raster_spec_id=%s", result["rule_set_id"], result["raster_spec_id"])
    print("Imported RuleSet catalog into Neo4j.")
    print(f"  rule_set_id: {result['rule_set_id']}")
    print(f"  raster_spec_id: {result['raster_spec_id']}")
    print(f"  voltage_level: {result['voltage_level']}")
    print(f"  rule_set_version: {result['rule_set_version']}")
    print(f"  rule_count: {result['rule_count']}")
    if included:
        print(f"  included_layers: {included}")
    if excluded:
        print(f"  excluded_layers: {excluded}")


def run_list_rule_sets(args: argparse.Namespace) -> None:
    """从 Neo4j 查询并列出所有 RuleSet 目录条目。

    连接 Neo4j 数据库，查询指定电压等级的 RuleSet 节点及其关联的
    CostRule 数量和 RasterSpec 参数，逐条输出 JSON 摘要。
    """
    import json
    import os

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ModuleNotFoundError:
        pass
    from neo4j import GraphDatabase

    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    username = os.getenv("NEO4J_USERNAME", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "change-me")
    database = os.getenv("NEO4J_DATABASE", "neo4j")

    _log_command_start("list-rule-sets", voltage_level=args.voltage_level, database=database)
    with GraphDatabase.driver(uri, auth=(username, password)) as driver:
        with driver.session(database=database) as session:
            rows = list(
                session.run(
                    """
                    MATCH (rs:RuleSet)
                    WHERE $voltage_level IS NULL OR rs.voltage_level = $voltage_level
                    WITH rs
                    ORDER BY rs.generated_at DESC
                    OPTIONAL MATCH (rs)-[:CONTAINS_RULE]->(cr:CostRule)
                    OPTIONAL MATCH (rs)-[:USES_RASTER_SPEC]->(spec:RasterSpec)
                    RETURN rs.id AS rule_set_id,
                           rs.voltage_level AS voltage_level,
                           rs.rule_set_version AS rule_set_version,
                           rs.status AS status,
                           rs.total_rules AS total_rules,
                           rs.generated_at AS generated_at,
                           collect(DISTINCT cr.id) AS cost_rules,
                           spec.id AS raster_spec_id,
                           spec.resolution AS resolution,
                           spec.calculation_crs AS crs,
                           spec.base_cost AS base_cost
                    ORDER BY generated_at DESC
                    """,
                    voltage_level=args.voltage_level,
                )
            )
            if not rows:
                logger.info("Neo4j 中没有 RuleSet：voltage_level=%s", args.voltage_level)
                print("No RuleSets found in Neo4j.")
                return
            logger.info("查询到 RuleSet：count=%s, voltage_level=%s", len(rows), args.voltage_level)
            for row in rows:
                spec = {}
                if row.get("raster_spec_id"):
                    spec = {
                        "raster_spec_id": row["raster_spec_id"],
                        "resolution": row.get("resolution"),
                        "crs": row.get("crs"),
                        "base_cost": row.get("base_cost"),
                    }
                result = {
                    "rule_set_id": row["rule_set_id"],
                    "voltage_level": row["voltage_level"],
                    "rule_set_version": row["rule_set_version"],
                    "status": row["status"],
                    "total_rules": row["total_rules"],
                    "actual_rule_count": len(row.get("cost_rules") or []),
                    "raster_spec": spec or None,
                }
                print(json.dumps(result, ensure_ascii=False, indent=2))
                print("---")


def run_route_pipeline_from_graph(args: argparse.Namespace) -> None:
    """执行图谱驱动的全链路走线管道。

    从 Neo4j 图谱中读取规则和栅格规格，依次完成 GPKG 成本化和栅格构建，
    是 "cost-gpkg-from-graph + build-cost-raster-from-graph" 的组合命令。
    """
    from .graph_rule_source import run_route_pipeline_from_graph

    _log_command_start(
        "run-route-pipeline-from-graph",
        source_gpkg=args.source_gpkg,
        out_gpkg=args.out_gpkg,
        out_dir=args.out_dir,
        voltage_level=args.voltage_level,
        rule_set_version=args.rule_set_version,
    )
    result = run_route_pipeline_from_graph(
        source_gpkg=Path(args.source_gpkg),
        out_gpkg=Path(args.out_gpkg),
        raster_out_dir=Path(args.out_dir),
        voltage_level=args.voltage_level,
        rule_set_version=args.rule_set_version,
        resolution=args.resolution,
        calculation_crs=args.calculation_crs,
        base_cost=args.base_cost,
    )
    logger.info(
        "图谱驱动全链路完成：rule_set_id=%s, out_gpkg=%s, out_dir=%s",
        result.get("graph_rule_set_id"),
        args.out_gpkg,
        args.out_dir,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\nGraph-driven route pipeline completed. GPKG: {args.out_gpkg}, raster dir: {args.out_dir}")


def run_cleanup_legacy_neo4j(args: argparse.Namespace) -> None:
    """清理 Neo4j 中旧版通用图谱的节点和关系。

    仅删除旧版 write_all 工作流产生的标签（TowerSite、LineSegment、
    GeoFeature、Rule、Condition、EffectTarget、Field、CostFactor、
    VoltageLevel、BaseCostRule、CalculationRun、RulePackage 等）及其关系。
    不会触碰当前主链路使用的 RuleSet、RouteDecision、RasterSpec、
    CostRule、RoutingFeature、RoutingLayer、CostSurface 节点。
    """
    import os

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ModuleNotFoundError:
        pass
    from neo4j import GraphDatabase

    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    username = os.getenv("NEO4J_USERNAME", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "change-me")
    database = os.getenv("NEO4J_DATABASE", "neo4j")

    legacy_labels = [
        "TowerSite",
        "LineSegment",
        "GeoFeature",
        "Rule",
        "Condition",
        "EffectTarget",
        "Field",
        "CostFactor",
        "VoltageLevel",
        "BaseCostRule",
        "CalculationRun",
        "RulePackage",
    ]

    _log_command_start("cleanup-legacy-neo4j", database=database, dry_run=args.dry_run)

    with GraphDatabase.driver(uri, auth=(username, password)) as driver:
        with driver.session(database=database) as session:
            if args.dry_run:
                print("Dry-run mode: showing legacy node counts without deleting.")
                for label in legacy_labels:
                    result = session.run(f"MATCH (n:{label}) RETURN count(n) AS cnt")
                    cnt = result.single()["cnt"]
                    if cnt > 0:
                        print(f"  {label}: {cnt} nodes")
                return

            total = 0
            for label in legacy_labels:
                result = session.run(
                    f"MATCH (n:{label}) DETACH DELETE n RETURN count(*) AS deleted"
                )
                deleted = result.single()["deleted"]
                if deleted > 0:
                    total += deleted
                    print(f"  Deleted {deleted} {label} nodes")

            if total == 0:
                print("No legacy graph nodes found. Nothing to clean.")
            else:
                print(f"\nTotal legacy nodes removed: {total}")


def build_parser() -> argparse.ArgumentParser:
    """构建 smart-kg 命令行参数解析器。"""
    parser = argparse.ArgumentParser(prog="smart-kg")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Start the FastAPI service.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8000, type=int)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=run_serve)

    cost_rules_parser = subparsers.add_parser("standardize-cost-rules", help="Extract cost rules from Excel to JSON.")
    cost_rules_parser.add_argument("--excel", required=True, help="Path to the cost-rules classification Excel workbook.")
    cost_rules_parser.add_argument("--out", default="data/standardized/cost_rules.json")
    cost_rules_parser.add_argument("--rule-set-version", default=None)
    cost_rules_parser.set_defaults(func=run_standardize_cost_rules)

    import_rules_neo4j_parser = subparsers.add_parser("import-cost-rules-neo4j", help="Import route-decision graph from cost rules, GPKG, and raster metadata into Neo4j.")
    import_rules_neo4j_parser.add_argument("--rules", required=True, help="Cost rules JSON path from standardize-cost-rules.")
    import_rules_neo4j_parser.add_argument("--gpkg", required=True, help="Costed GPKG path from cost-gpkg.")
    import_rules_neo4j_parser.add_argument("--voltage-level", required=True, help="Voltage level (e.g. 110kV).")
    import_rules_neo4j_parser.add_argument("--metadata", default=None, help="Raster metadata JSON path from build-cost-raster (optional).")
    import_rules_neo4j_parser.add_argument("--rule-set-version", default=None)
    import_rules_neo4j_parser.set_defaults(func=run_import_cost_rules_neo4j)

    cost_gpkg_parser = subparsers.add_parser("cost-gpkg", help="Standardize GPKG and enrich with cost fields.")
    cost_gpkg_parser.add_argument("--source-gpkg", required=True, help="Path to source GPKG.")
    cost_gpkg_parser.add_argument("--out-gpkg", required=True, help="Output costed GPKG path.")
    cost_gpkg_parser.add_argument("--voltage-level", required=True, help="Voltage level (e.g. 110kV).")
    cost_gpkg_parser.add_argument("--rules", default=None, help="Cost rules JSON path.")
    cost_gpkg_parser.add_argument("--rule-set-version", default=None)
    cost_gpkg_parser.set_defaults(func=run_cost_gpkg)

    graph_cost_gpkg_parser = subparsers.add_parser("cost-gpkg-from-graph", help="Standardize GPKG and enrich cost fields using active Neo4j graph rules.")
    graph_cost_gpkg_parser.add_argument("--source-gpkg", required=True, help="Path to source GPKG.")
    graph_cost_gpkg_parser.add_argument("--out-gpkg", required=True, help="Output costed GPKG path.")
    graph_cost_gpkg_parser.add_argument("--voltage-level", required=True, help="Voltage level (e.g. 110kV).")
    graph_cost_gpkg_parser.add_argument("--rule-set-version", default=None, help="Optional active RuleSet version override.")
    graph_cost_gpkg_parser.set_defaults(func=run_cost_gpkg_from_graph)

    raster_parser = subparsers.add_parser("build-cost-raster", help="Generate cost raster from costed GPKG.")
    raster_parser.add_argument("--gpkg", required=True, help="Path to costed GPKG.")
    raster_parser.add_argument("--out-dir", required=True, help="Output directory for raster files.")
    raster_parser.add_argument("--voltage-level", required=True, help="Voltage level (e.g. 110kV).")
    raster_parser.add_argument("--resolution", type=float, default=20.0, help="Raster resolution in meters.")
    raster_parser.add_argument("--calculation-crs", default=None, help="Target CRS for rasterization.")
    raster_parser.add_argument("--base-cost", type=float, default=1.0, help="Baseline routing cost applied to all traversable pixels.")
    raster_parser.set_defaults(func=run_build_cost_raster)

    graph_raster_parser = subparsers.add_parser("build-cost-raster-from-graph", help="Generate cost raster using raster execution settings from Neo4j graph.")
    graph_raster_parser.add_argument("--gpkg", required=True, help="Path to costed GPKG.")
    graph_raster_parser.add_argument("--out-dir", required=True, help="Output directory for raster files.")
    graph_raster_parser.add_argument("--voltage-level", required=True, help="Voltage level (e.g. 110kV).")
    graph_raster_parser.add_argument("--rule-set-version", default=None, help="Optional RuleSet version override.")
    graph_raster_parser.add_argument("--resolution", type=float, default=None, help="Optional override for raster resolution in meters.")
    graph_raster_parser.add_argument("--calculation-crs", default=None, help="Optional override for target CRS.")
    graph_raster_parser.add_argument("--base-cost", type=float, default=None, help="Optional override for baseline routing cost.")
    graph_raster_parser.set_defaults(func=run_build_cost_raster_from_graph)

    graph_pipeline_parser = subparsers.add_parser("run-route-pipeline-from-graph", help="Run the full graph-driven pipeline from source GPKG to costed GPKG and grayscale raster.")
    graph_pipeline_parser.add_argument("--source-gpkg", required=True, help="Path to source GPKG.")
    graph_pipeline_parser.add_argument("--out-gpkg", required=True, help="Output costed GPKG path.")
    graph_pipeline_parser.add_argument("--out-dir", required=True, help="Output directory for raster files.")
    graph_pipeline_parser.add_argument("--voltage-level", required=True, help="Voltage level (e.g. 110kV).")
    graph_pipeline_parser.add_argument("--rule-set-version", default=None, help="Optional RuleSet version override.")
    graph_pipeline_parser.add_argument("--resolution", type=float, default=None, help="Optional override for raster resolution in meters.")
    graph_pipeline_parser.add_argument("--calculation-crs", default=None, help="Optional override for target CRS.")
    graph_pipeline_parser.add_argument("--base-cost", type=float, default=None, help="Optional override for baseline routing cost.")
    graph_pipeline_parser.set_defaults(func=run_route_pipeline_from_graph)

    # RuleSet / RasterSpec graph catalog commands.

    import_ruleset_parser = subparsers.add_parser("import-rule-set-neo4j", help="Import a RuleSet catalog with RasterSpec into Neo4j (graph catalog layer).")
    import_ruleset_parser.add_argument("--rules", required=True, help="Cost rules JSON path from standardize-cost-rules.")
    import_ruleset_parser.add_argument("--voltage-level", required=True, help="Voltage level (e.g. 110kV).")
    import_ruleset_parser.add_argument("--rule-set-version", default=None, help="RuleSet version tag. Defaults to current timestamp.")
    import_ruleset_parser.add_argument("--resolution", type=float, default=None, help="Raster resolution in meters.")
    import_ruleset_parser.add_argument("--calculation-crs", default=None, help="Target CRS for rasterization (e.g. EPSG:4547).")
    import_ruleset_parser.add_argument("--base-cost", type=float, default=None, help="Baseline routing cost applied to all traversable pixels.")
    import_ruleset_parser.add_argument("--included-layers", default=None, help="Comma-separated list of layer names included in cost surface.")
    import_ruleset_parser.add_argument("--excluded-layers", default=None, help="Comma-separated list of layer names excluded from cost surface.")
    import_ruleset_parser.add_argument("--metadata", default=None, help="Optional raster metadata JSON used to populate RasterSpec defaults.")
    import_ruleset_parser.set_defaults(func=run_import_rule_set_neo4j)

    list_rulesets_parser = subparsers.add_parser("list-rule-sets", help="List RuleSet catalog entries from Neo4j.")
    list_rulesets_parser.add_argument("--voltage-level", default=None, help="Optional filter by voltage level.")
    list_rulesets_parser.set_defaults(func=run_list_rule_sets)

    cleanup_parser = subparsers.add_parser("cleanup-legacy-neo4j", help="Remove legacy universal-graph nodes from Neo4j (does NOT touch RuleSet/RouteDecision).")
    cleanup_parser.add_argument("--dry-run", action="store_true", help="Show counts without deleting.")
    cleanup_parser.set_defaults(func=run_cleanup_legacy_neo4j)

    return parser


def main() -> None:
    """smart-kg 命令行主入口。

    配置日志、解析参数、记录命令启动事件，然后分发到对应的子命令处理函数。
    """
    configure_logging()
    parser = build_parser()
    args = parser.parse_args()
    logger.info("解析命令行完成：command=%s", getattr(args, "command", None))
    args.func(args)


instrument_module_functions(globals(), logger, exclude={"main", "build_parser"})


if __name__ == "__main__":
    main()
