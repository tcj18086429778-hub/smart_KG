"""GPKG 成本字段标准化器。

本文件负责读取源 GPKG 图层，统一补齐标准空间字段和成本字段，
并根据规则或源数据回填逻辑为每个实体生成可用于后续栅格化的 `C_*` 字段。

在整体链路中的位置：
1. 将源 GPKG 转换成成本化 GPKG。
2. 为 Neo4j 走线决策图谱提供标准化的 `RoutingFeature` 输入。
3. 为成本栅格构建阶段提供直接可用的实体属性。
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
from pyproj import CRS
from shapely.geometry import base as geom_base

from .cost_rule_loader import CostRuleEntry, load_cost_rules
from .logging_utils import instrument_module_functions


METRIC_CRS_SHANGHAI = "EPSG:4550"

GPKG_SOURCE_LAYERS = [
    "building", "cropland", "dapeng", "road",
    "structures", "tower", "vegetation", "water",
]

FIELD_MAP_SOURCE_TO_STANDARD = {
    "factorLevel1": "S_TYP_NM",
    "factorLevel2": "S_STYP_NM",
    "factorType": "S_TYP_DETAIL",
    "factorTypeCode": "S_TYP_CD",
    "factorTypeKey": "S_STYP_CD",
    "level": "S_LVL",
}
logger = logging.getLogger(__name__)


def resolve_metric_crs(gdf: gpd.GeoDataFrame, fallback: str = METRIC_CRS_SHANGHAI) -> str:
    """从 GeoDataFrame 的经纬度范围推断合适的米制投影 CRS。

    参数：
        gdf: 待解析的 GeoDataFrame。
        fallback: 无法推断时使用的默认 CRS。

    返回：
        米制投影 CRS 字符串（如 EPSG:4550）。
    """
    logger.debug("开始解析米制投影：fallback=%s, crs=%s, empty=%s", fallback, gdf.crs, gdf.empty)
    if gdf.empty or gdf.crs is None:
        return fallback

    crs = CRS.from_user_input(gdf.crs)
    if not crs.is_geographic:
        return crs.to_string()

    minx, _, maxx, _ = gdf.total_bounds
    if not np.isfinite(minx) or not np.isfinite(maxx):
        return fallback

    lon = (float(minx) + float(maxx)) / 2.0
    if lon >= 121.5:
        return "EPSG:4550"
    if lon >= 118.5:
        return "EPSG:4549"
    if lon >= 115.5:
        return "EPSG:4548"
    return "EPSG:4547"


def standardize_gpkg(
    source_gpkg: Path,
    out_gpkg: Path,
    voltage_level: str,
    rules: list[CostRuleEntry] | None = None,
    rules_path: Path | None = None,
    layers: list[str] | None = None,
) -> dict[str, Any]:
    """将源 GPKG 图层标准化为成本化 GPKG。

    对源 GPKG 的各图层逐层处理，补齐 S_* 标准空间字段和 C_* 成本字段，
    输出可直接用于后续栅格化和 Neo4j 图谱构建的标准化 GPKG。

    参数：
        source_gpkg: 源 GPKG 文件路径。
        out_gpkg: 输出 GPKG 文件路径。
        voltage_level: 目标电压等级。
        rules: 成本规则列表（可选，与 rules_path 互斥）。
        rules_path: 成本规则 JSON 文件路径（可选）。
        layers: 要处理的图层名列表，默认使用 GPKG_SOURCE_LAYERS。

    返回：
        各图层统计信息字典（layers、total_features、enriched、rejected）。
    """
    if not voltage_level:
        raise ValueError("voltage_level is required")
    logger.info(
        "开始标准化 GPKG：source_gpkg=%s, out_gpkg=%s, voltage_level=%s, rules_path=%s, layer_override=%s",
        source_gpkg,
        out_gpkg,
        voltage_level,
        rules_path,
        layers,
    )

    if rules is None:
        if rules_path:
            rules = load_cost_rules(rules_path)
        else:
            rules = []

    out_gpkg.parent.mkdir(parents=True, exist_ok=True)
    if out_gpkg.exists():
        out_gpkg.unlink()
        logger.debug("删除已有输出 GPKG：out_gpkg=%s", out_gpkg)

    target_layers = layers or GPKG_SOURCE_LAYERS
    stats: dict[str, Any] = {"layers": {}, "total_features": 0, "enriched": 0, "rejected": 0}

    for layer_name in target_layers:
        try:
            gdf = gpd.read_file(source_gpkg, layer=layer_name)
        except Exception:
            logger.debug("读取图层失败或图层不存在，跳过：layer=%s", layer_name)
            continue
        if gdf.empty:
            logger.debug("图层为空，跳过：layer=%s", layer_name)
            continue

        logger.info("开始处理图层：layer=%s, feature_count=%s", layer_name, len(gdf))
        result_gdf = _process_layer(gdf, layer_name, voltage_level, rules)
        result_gdf.to_file(out_gpkg, layer=layer_name, driver="GPKG", mode="a" if layer_name != target_layers[0] else "w")

        enriched_count = int(result_gdf["C_CALC_MD"].notna().sum())
        stats["layers"][layer_name] = {
            "total": len(result_gdf),
            "enriched": enriched_count,
        }
        stats["total_features"] += len(result_gdf)
        stats["enriched"] += enriched_count
        logger.info("图层处理完成：layer=%s, enriched=%s/%s", layer_name, enriched_count, len(result_gdf))

    stats["rejected"] = stats["total_features"] - stats["enriched"]
    logger.info("GPKG 标准化完成：stats=%s", stats)
    return stats


def _process_layer(
    gdf: gpd.GeoDataFrame,
    layer_name: str,
    voltage_level: str,
    rules: list[CostRuleEntry],
) -> gpd.GeoDataFrame:
    """对单个图层执行字段规范化和成本字段补齐。

    参数：
        gdf: 源图层 GeoDataFrame。
        layer_name: 图层名称。
        voltage_level: 目标电压等级。
        rules: 成本规则列表。

    返回：
        补齐了 S_* 和 C_* 字段的 GeoDataFrame。
    """
    result = gdf.copy()
    logger.debug("开始规范化图层字段：layer=%s, feature_count=%s", layer_name, len(result))

    result["S_ID"] = [
        _generate_feature_id(layer_name, idx, row)
        for idx, row in result.iterrows()
    ]
    result["S_NM"] = result["featureName"]
    result["S_TYP_NM"] = result["factorLevel1"]
    result["S_STYP_NM"] = result["factorLevel2"]
    result["S_TYP_DETAIL"] = result["factorType"]
    result["S_TYP_CD"] = result["factorTypeCode"]
    result["S_STYP_CD"] = result["factorTypeKey"]
    result["S_LVL"] = result["level"]

    metric_crs = resolve_metric_crs(result)
    metric_gdf = result.to_crs(metric_crs)
    result["S_AREA"] = metric_gdf.geometry.apply(_safe_area_mu)
    result["S_LTH"] = metric_gdf.geometry.apply(_safe_length_km)
    result["S_CNT"] = 1
    result["S_METRIC_CRS"] = metric_crs

    result["C_CALC_MD"] = None
    result["C_EFF_TYP"] = None
    result["C_EFF_VAL"] = np.nan
    result["C_EFF_ATTR"] = None
    result["C_RULE_ID"] = None
    result["C_RULE_NM"] = None
    result["C_REASON_CD"] = np.nan
    result["C_BUF_DIST_M"] = np.nan
    result["C_AVOID_MD"] = None
    result["C_MATCH_SC"] = np.nan

    if rules:
        logger.debug("开始按规则补齐成本字段：layer=%s, rule_count=%s", layer_name, len(rules))
        _enrich_cost_fields(result, voltage_level, rules)

    return result


def _enrich_cost_fields(
    gdf: gpd.GeoDataFrame,
    voltage_level: str,
    rules: list[CostRuleEntry],
) -> None:
    """遍历各行，按规则匹配或源数据回填补齐 C_* 成本字段。

    参数：
        gdf: 待补齐的 GeoDataFrame（原地修改）。
        voltage_level: 目标电压等级。
        rules: 成本规则列表。
    """
    for idx in gdf.index:
        row = gdf.loc[idx]
        matched, score = _match_rule(row, voltage_level, rules)
        if matched:
            logger.debug(
                "地物命中规则：feature_id=%s, feature_name=%s, rule_id=%s, calc_mode=%s, score=%s",
                row.get("S_ID"),
                row.get("S_NM"),
                matched.rule_id,
                matched.calc_mode,
                score,
            )
            gdf.at[idx, "C_CALC_MD"] = matched.calc_mode
            gdf.at[idx, "C_EFF_TYP"] = matched.effect_value_status
            gdf.at[idx, "C_EFF_VAL"] = matched.effect_value if matched.effect_value is not None else np.nan
            gdf.at[idx, "C_EFF_ATTR"] = matched.effect_attr
            gdf.at[idx, "C_RULE_ID"] = matched.rule_id
            gdf.at[idx, "C_RULE_NM"] = matched.rule_name
            gdf.at[idx, "C_REASON_CD"] = matched.reason_code
            gdf.at[idx, "C_BUF_DIST_M"] = matched.buffer_distance_m if matched.buffer_distance_m is not None else np.nan
            gdf.at[idx, "C_AVOID_MD"] = matched.avoidance_mode
            gdf.at[idx, "C_MATCH_SC"] = float(score)
        else:
            logger.debug("地物未命中规则，尝试源数据回填：feature_id=%s, feature_name=%s", row.get("S_ID"), row.get("S_NM"))
            _apply_source_cost_fallback(gdf, idx, row)


def _match_rule(
    row: Any,
    voltage_level: str,
    rules: list[CostRuleEntry],
) -> tuple[CostRuleEntry | None, int]:
    """为单个地物行匹配最优的成本规则。

    参数：
        row: 地物行数据。
        voltage_level: 目标电压等级。
        rules: 成本规则列表。

    返回：
        (最优匹配的规则, 匹配分数) 元组，无匹配时规则为 None、分数为 -1。
    """
    feature_key = row.get("S_STYP_CD") or row.get("factorTypeKey")
    feature_detail = row.get("S_TYP_DETAIL") or row.get("factorType")
    feature_category_name = row.get("S_STYP_NM") or row.get("factorLevel2")
    feature_group_name = row.get("S_TYP_NM") or row.get("factorLevel1")
    feature_level = row.get("S_LVL") or row.get("level")
    feature_geom_kind = _infer_geometry_kind(row.get("geometry"))

    best: CostRuleEntry | None = None
    best_rank: tuple[int, int, int, int] | None = None

    for rule in rules:
        if not rule.enabled:
            continue
        if rule.voltage_level and rule.voltage_level != voltage_level:
            continue
        score = _rule_identity_score(
            rule=rule,
            feature_key=feature_key,
            feature_detail=feature_detail,
            feature_category_name=feature_category_name,
            feature_group_name=feature_group_name,
            feature_level=feature_level,
            feature_geom_kind=feature_geom_kind,
        )
        if score < 0:
            score = _condition_match_score(
                rule=rule,
                feature_key=feature_key,
                feature_detail=feature_detail,
                feature_category_name=feature_category_name,
                feature_group_name=feature_group_name,
            )
        if score < 0:
            continue

        rank = (
            score,
            rule.priority,
            1 if rule.voltage_level else 0,
            1 if rule.feature_subtype_code else 0,
        )
        if best_rank is None or rank > best_rank:
            best = rule
            best_rank = rank

    if best is not None:
        logger.debug(
            "规则匹配成功：feature_key=%s, feature_detail=%s, rule_id=%s, score=%s, priority=%s",
            feature_key,
            feature_detail,
            best.rule_id,
            best_rank[0] if best_rank else None,
            best.priority,
        )
    return best, (best_rank[0] if best_rank else -1)


def _rule_identity_score(
    rule: CostRuleEntry,
    feature_key: str | None,
    feature_detail: str | None,
    feature_category_name: str | None,
    feature_group_name: str | None,
    feature_level: str | None,
    feature_geom_kind: str | None,
) -> int:
    """根据地物编码、名称、类别和等级等标识字段计算与规则的精确匹配分数。

    返回 -1 表示不匹配，>=0 表示匹配（分数越高匹配越精确）。
    """
    score = 0

    rule_code = _norm(rule.feature_subtype_code)
    rule_name = _norm(rule.feature_subtype_name)
    rule_category = _norm(rule.feature_type_name)
    rule_level = _norm(rule.feature_level)
    rule_geom_kind = _norm(rule.geometry_kind)

    detail_name = _norm(feature_detail)
    category_name = _norm(feature_category_name)
    group_name = _norm(feature_group_name)
    source_code = _norm(feature_key)
    source_level = _norm(feature_level)
    source_geom_kind = _norm(feature_geom_kind)

    if rule_code:
        if source_code == rule_code:
            score += 100
        elif rule_name and detail_name == rule_name:
            score += 70
        else:
            return -1
    elif rule_name:
        if detail_name == rule_name:
            score += 85
        else:
            return -1
    else:
        return -1

    if rule_category:
        if category_name == rule_category:
            score += 20
        elif group_name == rule_category:
            score += 10
        else:
            return -1

    if rule_level:
        if source_level == rule_level:
            score += 8
        else:
            return -1

    if rule_geom_kind and source_geom_kind == rule_geom_kind:
        score += 3

    return score


def _condition_match_score(
    rule: CostRuleEntry,
    feature_key: str | None,
    feature_detail: str | None,
    feature_category_name: str | None,
    feature_group_name: str | None,
) -> int:
    """递归解析规则的条件树并计算匹配分数。

    返回 -1 表示不匹配，>=0 表示累积匹配分数。
    """
    cond = rule.match_condition_json
    if not cond:
        return -1

    leaves = cond.get("conditions", []) if "logic" in cond else [cond]
    score = 0
    for sub in leaves:
        if sub.get("field") == "voltage_level":
            score += 2
            continue
        leaf_score = _leaf_match_score(
            sub,
            feature_key,
            feature_detail,
            feature_category_name,
            feature_group_name,
        )
        if leaf_score < 0:
            return -1
        score += leaf_score
    return score


def _leaf_match_score(
    cond: dict[str, Any],
    feature_key: str | None,
    feature_detail: str | None,
    feature_category_name: str | None,
    feature_group_name: str | None,
) -> int:
    """对单个叶子条件节点进行字段值匹配。

    返回 -1 表示不匹配，>=0 表示匹配分数。
    """
    field = cond.get("field", "")
    op = cond.get("operator", "eq")
    value = cond.get("value")

    if field == "voltage_level":
        return 2

    candidates: list[str] = []
    if field in ("feature_subtype_code", "S_STYP_CD"):
        candidates = [_norm(feature_key)]
    elif field in ("feature_subtype_name", "S_STYP_NM"):
        candidates = [_norm(feature_detail), _norm(feature_category_name)]
    elif field in ("feature_type_name", "S_TYP_NM"):
        candidates = [_norm(feature_category_name), _norm(feature_group_name)]
    else:
        return -1

    candidates = [item for item in candidates if item]
    if not candidates:
        return -1

    if isinstance(value, list):
        normalized_values = [_norm(item) for item in value]
    else:
        normalized_values = [_norm(value)]

    if op == "eq":
        return 50 if normalized_values[0] in candidates else -1
    if op == "in":
        return 40 if any(item in candidates for item in normalized_values) else -1
    return -1


def _norm(value: Any) -> str | None:
    """将任意值归一化为小写字符串，用于规则匹配的比较基准。"""
    if value is None:
        return None
    text = str(value).strip()
    return text.lower() if text else None


def _apply_source_cost_fallback(gdf: gpd.GeoDataFrame, idx: Any, row: Any) -> None:
    """当规则未命中时，使用源数据的 costValue 字段回填到 C_* 字段。

    参数：
        gdf: 目标 GeoDataFrame（原地修改）。
        idx: 行索引。
        row: 行数据。
    """
    source_cost = row.get("costValue")
    if source_cost is None or (isinstance(source_cost, float) and np.isnan(source_cost)):
        return

    try:
        source_cost_value = float(source_cost)
    except (TypeError, ValueError):
        return
    if source_cost_value <= 0:
        return

    feature_code = row.get("S_STYP_CD") or row.get("factorTypeKey") or "unknown"
    feature_name = row.get("S_TYP_DETAIL") or row.get("factorType") or row.get("S_NM") or "source_feature"
    fallback_rule_id = f"source_fallback:{feature_code}"
    logger.debug(
        "应用源数据成本回填：feature_id=%s, feature_name=%s, fallback_rule_id=%s, source_cost=%s",
        row.get("S_ID"),
        feature_name,
        fallback_rule_id,
        source_cost_value,
    )

    gdf.at[idx, "C_CALC_MD"] = "MAIN_COST_INCREMENT"
    gdf.at[idx, "C_EFF_TYP"] = "NUMERIC"
    gdf.at[idx, "C_EFF_VAL"] = source_cost_value
    gdf.at[idx, "C_EFF_ATTR"] = _fallback_effect_attr(row)
    gdf.at[idx, "C_RULE_ID"] = fallback_rule_id
    gdf.at[idx, "C_RULE_NM"] = f"{feature_name}源数据成本回填"
    gdf.at[idx, "C_REASON_CD"] = _fallback_reason_code(fallback_rule_id)
    gdf.at[idx, "C_AVOID_MD"] = None
    gdf.at[idx, "C_MATCH_SC"] = 0.0


def _fallback_effect_attr(row: Any) -> str:
    """根据几何类型和 costType 字段推断回填所用的计量属性名。"""
    geom_kind = _infer_geometry_kind(row.get("geometry"))
    cost_type = row.get("costType")
    if str(cost_type).strip() == "1":
        return "S_AREA"
    if geom_kind == "line":
        return "S_LTH"
    if geom_kind == "polygon":
        return "S_CNT"
    return "S_CNT"


def _fallback_reason_code(seed: str) -> int:
    """根据种子字符串生成稳定的回退原因码（范围 10000-99999）。"""
    digest = hashlib.md5(seed.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 90000 + 10000


def _infer_geometry_kind(geom: geom_base.BaseGeometry | None) -> str | None:
    """根据 Shapely 几何对象推断其几何类型。

    返回 "point"、"line"、"polygon" 或 None。
    """
    if geom is None or geom.is_empty:
        return None
    geom_type = geom.geom_type
    if "Point" in geom_type:
        return "point"
    if "Line" in geom_type:
        return "line"
    if "Polygon" in geom_type:
        return "polygon"
    return None


def _safe_area_mu(geom: geom_base.BaseGeometry | None) -> float:
    """安全计算几何的亩面积，空几何返回 0.0。"""
    if geom is None or geom.is_empty:
        return 0.0
    area_m2 = geom.area
    return area_m2 / 666.67


def _safe_length_km(geom: geom_base.BaseGeometry | None) -> float:
    """安全计算几何的公里长度，空几何返回 0.0。"""
    if geom is None or geom.is_empty:
        return 0.0
    return geom.length / 1000.0


def read_enriched_gpkg_features(
    gpkg_path: Path,
    exclude_layers: list[str] | None = None,
) -> list[dict[str, Any]]:
    """从已标准化的成本化 GPKG 中读取地物特征列表。

    默认排除 tower 图层（塔位不参与走线决策面语义）。
    每条返回的字典对应 Neo4j 走线决策图谱的一个 RoutingFeature 节点。

    参数：
        gpkg_path: 成本化 GPKG 文件路径。
        exclude_layers: 需排除的图层名列表，默认排除 tower。

    返回：
        地物字典列表，包含 id、layer_name、calc_mode、effect_value、effect_attr、
        rule_id、rule_name、reason_code、buffer_distance_m、avoidance_mode、
        match_score、name、feature_type_code、feature_subtype_code、feature_level、
        geometry_type 等字段。
    """
    import geopandas as gpd

    exclude = [e.lower() for e in (exclude_layers or ["tower"])]
    logger.info("开始读取成本化 GPKG 特征：gpkg_path=%s, exclude_layers=%s", gpkg_path, exclude)
    features: list[dict[str, Any]] = []

    if not gpkg_path.exists():
        raise FileNotFoundError(f"GPKG not found: {gpkg_path}")

    try:
        layers_info = gpd.list_layers(gpkg_path)
    except Exception:
        return features

    for _, layer_row in layers_info.iterrows():
        layer_name = str(layer_row["name"])
        if layer_name.lower() in exclude:
            logger.debug("跳过排除图层：layer=%s", layer_name)
            continue

        try:
            gdf = gpd.read_file(gpkg_path, layer=layer_name)
        except Exception:
            continue

        if gdf.empty or "C_CALC_MD" not in gdf.columns:
            logger.debug("图层未成本化或为空，跳过：layer=%s", layer_name)
            continue

        gdf = gdf[gdf["C_CALC_MD"].notna()].copy()
        if gdf.empty:
            continue

        for _, feat in gdf.iterrows():
            geom = feat.geometry if hasattr(feat, "geometry") else None
            features.append(
                {
                    "id": str(feat.get("S_ID", f"{layer_name}:{_}")),
                    "layer_name": layer_name,
                    "calc_mode": str(feat["C_CALC_MD"]) if feat.get("C_CALC_MD") is not None else None,
                    "effect_value": _safe_float(feat.get("C_EFF_VAL")),
                    "effect_attr": str(feat["C_EFF_ATTR"]) if feat.get("C_EFF_ATTR") is not None else None,
                    "rule_id": str(feat["C_RULE_ID"]) if feat.get("C_RULE_ID") is not None else None,
                    "rule_name": str(feat["C_RULE_NM"]) if feat.get("C_RULE_NM") is not None else None,
                    "reason_code": _safe_int(feat.get("C_REASON_CD")),
                    "buffer_distance_m": _safe_float(feat.get("C_BUF_DIST_M")),
                    "avoidance_mode": str(feat["C_AVOID_MD"]) if feat.get("C_AVOID_MD") is not None else None,
                    "match_score": _safe_float(feat.get("C_MATCH_SC")),
                    "name": str(feat.get("S_NM") or feat.get("S_STYP_NM") or feat.get("S_TYP_NM") or layer_name),
                    "feature_type_code": str(feat["S_TYP_CD"]) if "S_TYP_CD" in gdf.columns and feat.get("S_TYP_CD") is not None else None,
                    "feature_subtype_code": str(feat["S_STYP_CD"]) if "S_STYP_CD" in gdf.columns and feat.get("S_STYP_CD") is not None else None,
                    "feature_level": str(feat["S_LVL"]) if "S_LVL" in gdf.columns and feat.get("S_LVL") is not None else None,
                    "geometry_type": geom.geom_type if geom is not None and not geom.is_empty else None,
                }
            )
        logger.info("已读取成本化图层：layer=%s, feature_count=%s", layer_name, len(gdf))

    logger.info("成本化 GPKG 特征读取完成：count=%s", len(features))
    return features


def _safe_float(val: Any) -> float | None:
    """安全转换为 float，None 和 NaN 均返回 None。"""
    if val is None:
        return None
    try:
        v = float(val)
        return None if np.isnan(v) else v
    except (TypeError, ValueError):
        return None


def _safe_int(val: Any) -> int | None:
    """安全转换为 int，失败返回 None。"""
    if val is None:
        return None
    try:
        v = int(val)
        return v
    except (TypeError, ValueError):
        return None


def _generate_feature_id(layer_name: str, idx: Any, row: Any) -> str:
    """根据图层名、索引和要素属性生成稳定的唯一特征 ID。

    参数：
        layer_name: 图层名称。
        idx: 行索引。
        row: 行数据。

    返回：
        "feat:{layer_name}:{md5截断}" 格式的唯一标识。
    """
    key_parts = [
        layer_name,
        str(idx),
        str(row.get("factorTypeKey", "")),
        str(row.get("factorType", "")),
    ]
    raw = "|".join(key_parts)
    return f"feat:{layer_name}:{hashlib.md5(raw.encode()).hexdigest()[:12]}"


instrument_module_functions(globals(), logger)
