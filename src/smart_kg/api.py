"""FastAPI 服务入口。

本文件暴露 smart_kg 的 HTTP 接口，包括成本规则标准化、
GPKG 成本字段补齐和成本栅格构建。

保留端点：
- GET  /health
- POST /cost-rules/standardize
- POST /gpkg/cost-fields
- POST /raster/cost-surface
"""

from __future__ import annotations

import logging
from pathlib import Path as _Path
from typing import Optional as _Optional

from fastapi import FastAPI
from pydantic import BaseModel as _BaseModel

from .logging_utils import configure_logging, instrument_module_functions


configure_logging()
logger = logging.getLogger(__name__)
app = FastAPI(title="smart_KG", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    logger.debug("收到健康检查请求。")
    return {"status": "ok"}


class CostRulesRequest(_BaseModel):
    excel_path: str
    out_path: str = "data/standardized/cost_rules.json"
    rule_set_version: _Optional[str] = None


class CostGpkgRequest(_BaseModel):
    source_gpkg: str
    out_gpkg: str
    voltage_level: str
    rules_path: _Optional[str] = None
    rule_set_version: _Optional[str] = None


class CostRasterRequest(_BaseModel):
    gpkg_path: str
    out_dir: str
    voltage_level: str
    resolution: float = 20.0
    calculation_crs: _Optional[str] = None


@app.post("/cost-rules/standardize")
def post_standardize_cost_rules(req: CostRulesRequest) -> dict:
    from .cost_rule_loader import standardize_cost_rules

    logger.info("收到成本规则标准化请求：excel_path=%s, out_path=%s", req.excel_path, req.out_path)
    rows = standardize_cost_rules(
        excel_path=_Path(req.excel_path),
        out_path=_Path(req.out_path),
        rule_set_version=req.rule_set_version,
    )
    logger.info("成本规则标准化完成：count=%s, out_path=%s", len(rows), req.out_path)
    return {"count": len(rows), "out_path": req.out_path}


@app.post("/gpkg/cost-fields")
def post_cost_gpkg(req: CostGpkgRequest) -> dict:
    from .cost_rule_loader import load_cost_rules
    from .gpkg_standardizer import standardize_gpkg

    logger.info(
        "收到 GPKG 成本化请求：source_gpkg=%s, out_gpkg=%s, voltage_level=%s",
        req.source_gpkg,
        req.out_gpkg,
        req.voltage_level,
    )
    rules = load_cost_rules(_Path(req.rules_path)) if req.rules_path else None
    stats = standardize_gpkg(
        source_gpkg=_Path(req.source_gpkg),
        out_gpkg=_Path(req.out_gpkg),
        voltage_level=req.voltage_level,
        rules=rules,
    )
    logger.info("GPKG 成本化完成：source_gpkg=%s, stats=%s", req.source_gpkg, stats)
    return stats


@app.post("/raster/cost-surface")
def post_build_cost_raster(req: CostRasterRequest) -> dict:
    from .raster_executor import build_cost_raster

    logger.info(
        "收到成本栅格构建请求：gpkg_path=%s, out_dir=%s, voltage_level=%s",
        req.gpkg_path,
        req.out_dir,
        req.voltage_level,
    )
    metadata = build_cost_raster(
        gpkg_path=_Path(req.gpkg_path),
        out_dir=_Path(req.out_dir),
        voltage_level=req.voltage_level,
        resolution=req.resolution,
        calculation_crs=req.calculation_crs,
    )
    logger.info("成本栅格构建完成：gpkg_path=%s, cost_surface_path=%s", req.gpkg_path, metadata.get("cost_surface_path"))
    return metadata


instrument_module_functions(globals(), logger, exclude={"app"})
