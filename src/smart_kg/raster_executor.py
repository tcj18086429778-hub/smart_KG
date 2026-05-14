"""成本面栅格构建器。

本文件负责将成本化 GPKG 中的规则命中结果转换为可直接用于路径搜索的成本栅格。
这是"选线规则 -> 成本面"链路的关键输出环节。

主要处理链路：
1. 按图层筛选规则命中的要素。
2. 将禁建要素栅格化为 blocked_mask。
3. 将成本增量要素按面积/长度/处数分摊到像元，叠加到 cost_surface。
4. 将缩放系数要素按比例调整对应像元。
5. 输出 cost_surface.tif、blocked_mask.tif、reason_code.tif、cost_preview.png 和 metadata.json。

核心日志覆盖：
- 输入参数摘要（gpkg_path、out_dir、voltage_level、resolution、calculation_crs）。
- 每个图层的处理情况（layer_name、feature_count、是否跳过）。
- 栅格尺寸（width、height、transform）和像元面积/长度。
- 禁建/增量/缩放三类要素的处理进度和像元统计。
- 各输出文件的写入路径。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.features import rasterize
from rasterio.transform import from_bounds
from PIL import Image
from shapely.geometry import base as geom_base

from .gpkg_standardizer import METRIC_CRS_SHANGHAI, resolve_metric_crs
from .logging_utils import instrument_module_functions

logger = logging.getLogger(__name__)

BASE_REASON_CODE = 1


def build_cost_raster(
    gpkg_path: Path,
    out_dir: Path,
    voltage_level: str,
    resolution: float = 20.0,
    calculation_crs: str | None = None,
    base_cost: float = 1.0,
    included_layers: list[str] | None = None,
    excluded_layers: list[str] | None = None,
) -> dict[str, Any]:
    """从成本化 GPKG 构建用于路径搜索的成本栅格。

    参数：
        gpkg_path: 成本化后的 GPKG 文件路径。
        out_dir: 栅格输出目录。
        voltage_level: 电压等级（如 "110kV"）。
        resolution: 栅格分辨率（米），默认 20.0m。
        calculation_crs: 目标投影坐标系，为空时自动推断。
        base_cost: 基础走线成本，应用于所有可通行像元，必须为正数。
        included_layers: 显式指定参与栅格化的图层名列表。
        excluded_layers: 需要排除的图层名列表，默认为 ["tower"]。

    返回：
        包含 cost_surface_path、blocked_mask_path、reason_code_path、
        metadata_path、stats 等信息的元数据字典。
    """
    if not voltage_level:
        raise ValueError("voltage_level is required")
    if base_cost <= 0:
        raise ValueError("base_cost must be positive")

    logger.info(
        "开始构建成本栅格：gpkg_path=%s, out_dir=%s, voltage_level=%s, resolution=%s, calculation_crs=%s, base_cost=%s",
        gpkg_path,
        out_dir,
        voltage_level,
        resolution,
        calculation_crs,
        base_cost,
    )

    out_dir.mkdir(parents=True, exist_ok=True)

    # ── 加载并筛选图层 ──
    all_gdfs: list[gpd.GeoDataFrame] = []
    included_layer_names: list[str] = []
    excluded_layer_names: list[str] = []
    include_filter = {name.lower() for name in included_layers} if included_layers else None
    exclude_filter = {name.lower() for name in (excluded_layers or ["tower"])}

    layers = gpd.list_layers(gpkg_path)
    logger.debug("GPKG 图层列表：layer_count=%s", len(layers))
    for _, row in layers.iterrows():
        layer_name = row["name"]
        layer_key = layer_name.lower()
        if layer_key in exclude_filter:
            if layer_name not in excluded_layer_names:
                excluded_layer_names.append(layer_name)
            logger.debug("排除图层：layer=%s", layer_name)
            continue
        if include_filter is not None and layer_key not in include_filter:
            logger.debug("图层不在包含列表中，跳过：layer=%s", layer_name)
            continue
        gdf = gpd.read_file(gpkg_path, layer=layer_name)
        if gdf.empty or "C_CALC_MD" not in gdf.columns:
            logger.debug("图层为空或未成本化，跳过：layer=%s", layer_name)
            continue
        gdf = gdf[gdf["C_CALC_MD"].notna()].copy()
        if not gdf.empty:
            all_gdfs.append(gdf)
            included_layer_names.append(layer_name)
            logger.info("已加载成本化图层：layer=%s, feature_count=%s", layer_name, len(gdf))

    if not all_gdfs:
        raise ValueError("No enriched features found in GPKG")

    # ── 统一投影并计算栅格参数 ──
    crs = calculation_crs or resolve_metric_crs(all_gdfs[0], fallback=METRIC_CRS_SHANGHAI)
    logger.info("栅格目标投影：crs=%s", crs)
    merged = gpd.GeoDataFrame(pd.concat(all_gdfs, ignore_index=True), geometry="geometry", crs=all_gdfs[0].crs)
    merged = merged.to_crs(crs)

    bounds = merged.total_bounds
    transform, width, height = _compute_grid(bounds, resolution)
    pixel_width_m = abs(transform.a)
    pixel_height_m = abs(transform.e)
    pixel_area_mu = (pixel_width_m * pixel_height_m) / 666.67
    pixel_length_km = ((pixel_width_m + pixel_height_m) / 2.0) / 1000.0

    logger.info(
        "栅格参数：width=%s, height=%s, pixel_width_m=%s, pixel_height_m=%s, pixel_area_mu=%s, pixel_length_km=%s",
        width,
        height,
        pixel_width_m,
        pixel_height_m,
        pixel_area_mu,
        pixel_length_km,
    )

    # ── 初始化栅格数组 ──
    cost_surface = np.full((height, width), float(base_cost), dtype=np.float64)
    blocked_mask = np.zeros((height, width), dtype=np.uint8)
    reason_code = np.zeros((height, width), dtype=np.int32)
    reason_strength = np.zeros((height, width), dtype=np.float64)
    scaling_surface = np.ones((height, width), dtype=np.float64)

    reason_map: dict[int, dict[str, Any]] = {
        BASE_REASON_CODE: {
            "rule_id": "base_routing_cost",
            "rule_name": "基础走线成本",
            "calc_mode": "BASE_COST",
            "feature_id": "",
            "feature_name": "",
        },
    }

    # ── 按 calc_mode 分组 ──
    forbidden_features = merged[merged["C_CALC_MD"] == "FORBIDDEN"]
    scaling_features = merged[merged["C_CALC_MD"] == "MAIN_COST_SCALING"]
    additive_features = merged[merged["C_CALC_MD"].isin(["MAIN_COST_INCREMENT", "SPATIAL_INTERSECT", "CROSS_EVENT"])]

    logger.info(
        "要素分组统计：forbidden=%s, scaling=%s, additive=%s",
        len(forbidden_features),
        len(scaling_features),
        len(additive_features),
    )

    # ── 处理禁建要素 ──
    if not forbidden_features.empty:
        logger.info("开始处理禁建要素：count=%s", len(forbidden_features))
        shapes = _feature_shapes(forbidden_features, value=1)
        forbidden_raster = rasterize(
            shapes, out_shape=(height, width), transform=transform,
            fill=0, dtype=np.uint8, merge_alg=rasterio.enums.MergeAlg.replace,
        )
        blocked_mask = np.maximum(blocked_mask, forbidden_raster)
        for _, feat in forbidden_features.iterrows():
            rc = _get_reason_code(feat)
            feature_id = feat.get("S_ID", "?")
            rule_id = feat.get("C_RULE_ID", "?")
            reason_map[rc] = {
                "rule_id": rule_id,
                "rule_name": feat.get("C_RULE_NM", ""),
                "calc_mode": "FORBIDDEN",
                "feature_id": feature_id,
                "feature_name": feat.get("S_NM", ""),
            }
            logger.debug(
                "禁建要素栅格化：feature_id=%s, rule_id=%s, reason_code=%s",
                feature_id,
                rule_id,
                rc,
            )

        reason_shapes = _feature_shapes_with_reason(forbidden_features)
        if reason_shapes:
            reason_raster = rasterize(
                reason_shapes, out_shape=(height, width), transform=transform,
                fill=0, dtype=np.int32, merge_alg=rasterio.enums.MergeAlg.replace,
            )
            reason_code = np.where(reason_raster > reason_code, reason_raster, reason_code)
        blocked_pixels = int(blocked_mask.sum())
        logger.info("禁建要素处理完成：blocked_pixels=%s", blocked_pixels)

    # ── 处理增量/相交事件成本要素 ──
    total_additive_cost = 0.0
    if not additive_features.empty:
        logger.info("开始处理增量成本要素：count=%s", len(additive_features))
        for _, feat in additive_features.iterrows():
            feature_id = feat.get("S_ID", "?")
            rule_id = feat.get("C_RULE_ID", "?")
            geom = _effective_geometry(feat)
            if geom is None or geom.is_empty:
                logger.debug("增量要素几何为空，跳过：feature_id=%s, rule_id=%s", feature_id, rule_id)
                continue
            mask = rasterize(
                [(geom, 1)], out_shape=(height, width), transform=transform,
                fill=0, dtype=np.uint8, merge_alg=rasterio.enums.MergeAlg.replace,
            )
            covered_pixels = int(mask.sum())
            if covered_pixels <= 0:
                logger.debug("增量要素覆盖像元数为 0，跳过：feature_id=%s, rule_id=%s", feature_id, rule_id)
                continue
            pixel_cost = _compute_feature_pixel_cost(
                feat=feat,
                covered_pixels=covered_pixels,
                pixel_area_mu=pixel_area_mu,
                pixel_length_km=pixel_length_km,
            )
            if pixel_cost <= 0:
                logger.debug("增量要素像元成本为 0，跳过：feature_id=%s, rule_id=%s", feature_id, rule_id)
                continue

            layer = mask.astype(np.float64) * pixel_cost
            cost_surface += layer
            total_additive_cost += float(layer.sum())
            rc = _get_reason_code(feat)
            reason_map[rc] = {
                "rule_id": rule_id,
                "rule_name": feat.get("C_RULE_NM", ""),
                "calc_mode": feat.get("C_CALC_MD", ""),
                "feature_id": feature_id,
                "feature_name": feat.get("S_NM", ""),
            }
            logger.debug(
                "增量成本：feature_id=%s, rule_id=%s, covered_pixels=%s, pixel_cost=%s, total_layer_cost=%s",
                feature_id,
                rule_id,
                covered_pixels,
                pixel_cost,
                layer.sum(),
            )
            stronger = (blocked_mask == 0) & (layer > reason_strength)
            reason_code = np.where(stronger, rc, reason_code)
            reason_strength = np.where(stronger, layer, reason_strength)
        logger.info("增量成本要素处理完成：total_additive_cost=%s", total_additive_cost)

    # ── 处理缩放系数要素 ──
    if not scaling_features.empty:
        logger.info("开始处理缩放系数要素：count=%s", len(scaling_features))
        for _, feat in scaling_features.iterrows():
            feature_id = feat.get("S_ID", "?")
            rule_id = feat.get("C_RULE_ID", "?")
            scale_val = feat.get("C_EFF_VAL")
            geom = _effective_geometry(feat)
            if not scale_val or np.isnan(scale_val) or geom is None or geom.is_empty:
                logger.debug("缩放要素无效，跳过：feature_id=%s, rule_id=%s", feature_id, rule_id)
                continue
            shapes = [(geom, scale_val)]
            layer = rasterize(
                shapes, out_shape=(height, width), transform=transform,
                fill=1.0, dtype=np.float64, merge_alg=rasterio.enums.MergeAlg.replace,
            )
            scaling_surface *= layer
            logger.debug(
                "缩放系数：feature_id=%s, rule_id=%s, scale_val=%s",
                feature_id,
                rule_id,
                scale_val,
            )
        logger.info("缩放系数要素处理完成")

    # ── 汇总与输出 ──
    cost_surface *= scaling_surface
    traversable_max_cost = float(cost_surface.max()) if cost_surface.size else 0.0
    blocked_fill_cost = max(traversable_max_cost, 1.0) * 10.0
    cost_surface[blocked_mask > 0] = blocked_fill_cost
    reason_code = np.where((blocked_mask == 0) & (reason_code == 0), BASE_REASON_CODE, reason_code)

    logger.info(
        "栅格汇总完成：traversable_max_cost=%s, blocked_fill_cost=%s, blocked_pixels=%s",
        traversable_max_cost,
        blocked_fill_cost,
        int(blocked_mask.sum()),
    )

    cost_path = out_dir / "cost_surface.tif"
    blocked_path = out_dir / "blocked_mask.tif"
    reason_path = out_dir / "reason_code.tif"
    preview_path = out_dir / "cost_preview.png"
    metadata_path = out_dir / "metadata.json"

    profile = {
        "driver": "GTiff",
        "crs": crs,
        "transform": transform,
        "width": width,
        "height": height,
        "count": 1,
        "compress": "deflate",
    }

    logger.info("写出成本面栅格：cost_surface_path=%s", cost_path)
    _write_tif(cost_path, cost_surface, {**profile, "dtype": "float64"})
    logger.info("写出禁建遮罩栅格：blocked_mask_path=%s", blocked_path)
    _write_tif(blocked_path, blocked_mask, {**profile, "dtype": "uint8"})
    logger.info("写出原因编码栅格：reason_code_path=%s", reason_path)
    _write_tif(reason_path, reason_code, {**profile, "dtype": "int32"})
    logger.info("写出成本预览图：preview_path=%s", preview_path)
    _write_preview(preview_path, cost_surface, blocked_mask)

    stats = {
        "total_cost_pixels": int((cost_surface > 0).sum()),
        "traversable_cost_pixels": int(((cost_surface > 0) & (blocked_mask == 0)).sum()),
        "total_blocked_pixels": int((blocked_mask > 0).sum()),
        "min_traversable_cost": float(cost_surface[blocked_mask == 0].min()) if np.any(blocked_mask == 0) else 0.0,
        "max_cost": float(cost_surface.max()) if cost_surface.max() > 0 else 0.0,
        "blocked_fill_cost": blocked_fill_cost,
    }
    metadata = {
        "voltage_level": voltage_level,
        "resolution": resolution,
        "resolution_m": resolution,
        "base_cost": base_cost,
        "crs": crs,
        "bounds": bounds.tolist(),
        "width": width,
        "height": height,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_gpkg": str(gpkg_path),
        "included_layers": included_layer_names,
        "excluded_layers": excluded_layer_names,
        "cost_surface_path": str(cost_path),
        "blocked_mask_path": str(blocked_path),
        "reason_code_path": str(reason_path),
        "preview_path": str(preview_path),
        "outputs": {
            "cost_surface": str(cost_path),
            "blocked_mask": str(blocked_path),
            "reason_code": str(reason_path),
            "preview": str(preview_path),
        },
        "reason_code_mapping": {str(k): v for k, v in reason_map.items()},
        "stats": stats,
    }
    logger.info(
        "成本栅格构建完成：voltage_level=%s, resolution=%s, total_pixels=%s, blocked_pixels=%s, max_cost=%s",
        voltage_level,
        resolution,
        stats["total_cost_pixels"],
        stats["total_blocked_pixels"],
        stats["max_cost"],
    )
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return metadata


def _compute_grid(bounds: np.ndarray, resolution: float) -> tuple[Any, int, int]:
    """根据边界和分辨率计算栅格变换矩阵、宽度和高度。

    参数：
        bounds: [minx, miny, maxx, maxy] 边界数组。
        resolution: 像元分辨率（米）。

    返回：
        (仿射变换矩阵, 宽度, 高度) 三元组。
    """
    minx, miny, maxx, maxy = bounds
    width = max(1, int(np.ceil((maxx - minx) / resolution)))
    height = max(1, int(np.ceil((maxy - miny) / resolution)))
    transform = from_bounds(minx, miny, maxx, maxy, width, height)
    return transform, width, height


def _compute_feature_pixel_cost(
    feat: Any,
    covered_pixels: int,
    pixel_area_mu: float,
    pixel_length_km: float,
) -> float:
    """计算单个要素分配到每个覆盖像元上的成本。

    参数：
        feat: 要素行（GeoDataFrame 的迭代行）。
        covered_pixels: 该要素覆盖的像元数。
        pixel_area_mu: 单个像元对应的亩数。
        pixel_length_km: 单个像元对应的公里数。

    返回：
        每个像元的成本值；非法值返回 0。
    """
    eff_val = feat.get("C_EFF_VAL")
    if eff_val is None or (isinstance(eff_val, float) and np.isnan(eff_val)):
        return 0.0

    eff_attr = feat.get("C_EFF_ATTR")
    if eff_attr == "S_AREA":
        return float(eff_val) * pixel_area_mu
    if eff_attr == "S_LTH":
        return float(eff_val) * pixel_length_km

    total_cost = float(eff_val)
    if eff_attr == "S_CNT":
        cnt = feat.get("S_CNT", 1)
        total_cost = float(eff_val) * float(cnt)

    return total_cost / covered_pixels if covered_pixels > 0 else 0.0


def _get_reason_code(feat: Any) -> int:
    """从要素中提取原因编码，缺失时根据 rule_id 生成稳定编码。

    参数：
        feat: 要素行。

    返回：
        1~99999 范围内的整数原因编码。
    """
    reason_code = feat.get("C_REASON_CD")
    if reason_code is not None and not pd.isna(reason_code):
        return int(reason_code)
    rule_id = feat.get("C_RULE_ID") or feat.get("S_ID") or ""
    digest = hashlib.md5(str(rule_id).encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 90000 + 10000


def _feature_shapes(gdf: gpd.GeoDataFrame, value: float = 1.0) -> list[tuple[Any, float]]:
    """从 GeoDataFrame 中提取几何与值配对列表，用于栅格化。

    参数：
        gdf: 要素 GeoDataFrame。
        value: 填充值。

    返回：
        (几何对象, 值) 的列表。
    """
    shapes: list[tuple[Any, float]] = []
    for _, feat in gdf.iterrows():
        geom = _effective_geometry(feat)
        if geom is None or geom.is_empty:
            continue
        shapes.append((geom, value))
    return shapes


def _feature_shapes_with_reason(gdf: gpd.GeoDataFrame) -> list[tuple[Any, int]]:
    """从禁建 GeoDataFrame 中提取 (几何, 原因编码) 配对列表。

    参数：
        gdf: 禁建要素 GeoDataFrame。

    返回：
        (几何对象, 原因编码) 的列表。
    """
    shapes = []
    for _, feat in gdf.iterrows():
        geom = _effective_geometry(feat)
        if geom is None or geom.is_empty:
            continue
        shapes.append((geom, _get_reason_code(feat)))
    return shapes


def _effective_geometry(feat: Any) -> geom_base.BaseGeometry | None:
    """计算要素的有效几何，对禁建且需要缓冲的要素应用 buffer。

    参数：
        feat: 要素行。

    返回：
        有效几何对象，几何为空或无效时返回 None。
    """
    geom = feat.geometry if hasattr(feat, "geometry") else feat.get("geometry")
    if geom is None or geom.is_empty:
        return None

    if feat.get("C_CALC_MD") == "FORBIDDEN" and str(feat.get("C_AVOID_MD") or "").upper() == "BUFFER":
        distance = feat.get("C_BUF_DIST_M")
        if distance is not None and not pd.isna(distance) and float(distance) > 0:
            return geom.buffer(float(distance))
    return geom


def _write_tif(path: Path, data: np.ndarray, profile: dict[str, Any]) -> None:
    """将 numpy 数组写出为单波段 GeoTIFF。

    参数：
        path: 输出路径。
        data: 待写出的二维数组。
        profile: rasterio 写入配置字典。
    """
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)


def _write_preview(path: Path, cost_surface: np.ndarray, blocked_mask: np.ndarray) -> None:
    """生成成本面的灰度预览 PNG 图像。

    参数：
        path: 输出 PNG 路径。
        cost_surface: 成本面数组。
        blocked_mask: 禁建遮罩数组（在预览图中以白色标注）。
    """
    preview = cost_surface.copy()
    max_val = preview.max()
    if max_val > 0:
        preview = (preview / max_val * 254).astype(np.uint8)
    else:
        preview = np.zeros_like(preview, dtype=np.uint8)
    preview[blocked_mask > 0] = 255
    img = Image.fromarray(preview, mode="L")
    img.save(path)


instrument_module_functions(globals(), logger)
