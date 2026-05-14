"""栅格执行器集成测试。

验证 build_cost_raster() 的完整流程：输入验证、图层过滤、像素分布、缓冲禁建和输出文件生成。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import numpy as np

SOURCE_GPKG = Path(__file__).resolve().parents[2] / "上海_source.gpkg"
EXCEL_PATH = Path(__file__).resolve().parents[2] / "选线数据分类体系20260422.xlsx"
OUT_GPKG = Path(__file__).resolve().parents[1] / "data" / "test_output" / "上海_costed_raster_test.gpkg"
RASTER_OUT = Path(__file__).resolve().parents[1] / "data" / "test_output" / "raster_test"
RULES_OUT = Path(__file__).resolve().parents[1] / "data" / "test_output" / "cost_rules_raster_test.json"


@pytest.fixture(autouse=True)
def clean_output():
    """自动清理测试生成的 GPKG、栅格和规则文件。"""
    import shutil
    for p in [OUT_GPKG, RULES_OUT]:
        if p.exists():
            p.unlink()
    if Path(RASTER_OUT).exists():
        shutil.rmtree(RASTER_OUT)
    OUT_GPKG.parent.mkdir(parents=True, exist_ok=True)
    yield
    for p in [OUT_GPKG, RULES_OUT]:
        if p.exists():
            p.unlink()
    if Path(RASTER_OUT).exists():
        shutil.rmtree(RASTER_OUT)


@pytest.mark.skipif(not SOURCE_GPKG.exists(), reason="Source GPKG not available")
@pytest.mark.skipif(not EXCEL_PATH.exists(), reason="Excel file not available")
def test_build_cost_raster_produces_outputs():
    """验证 build_cost_raster 生成全部输出文件（tif、png、metadata）。"""
    from smart_kg.cost_rule_loader import standardize_cost_rules, load_cost_rules
    from smart_kg.gpkg_standardizer import standardize_gpkg
    from smart_kg.raster_executor import build_cost_raster

    standardize_cost_rules(EXCEL_PATH, RULES_OUT, rule_set_version="test")
    rules = load_cost_rules(RULES_OUT)

    standardize_gpkg(
        source_gpkg=SOURCE_GPKG,
        out_gpkg=OUT_GPKG,
        voltage_level="110kV",
        rules=rules,
    )

    metadata = build_cost_raster(
        gpkg_path=OUT_GPKG,
        out_dir=Path(RASTER_OUT),
        voltage_level="110kV",
        resolution=20.0,
    )

    assert Path(RASTER_OUT, "cost_surface.tif").exists()
    assert Path(RASTER_OUT, "blocked_mask.tif").exists()
    assert Path(RASTER_OUT, "reason_code.tif").exists()
    assert Path(RASTER_OUT, "cost_preview.png").exists()
    assert Path(RASTER_OUT, "metadata.json").exists()

    import rasterio
    with rasterio.open(Path(RASTER_OUT, "cost_surface.tif")) as src:
        assert src.width > 0
        assert src.height > 0
        data = src.read(1)
        assert data.shape == (src.height, src.width)

    with rasterio.open(Path(RASTER_OUT, "blocked_mask.tif")) as src:
        data = src.read(1)
        assert data.dtype.name == "uint8"

    meta = json.loads(Path(RASTER_OUT, "metadata.json").read_text(encoding="utf-8"))
    assert "voltage_level" in meta
    assert meta["voltage_level"] == "110kV"
    assert "reason_code_mapping" in meta
    assert meta["resolution_m"] == 20.0

    from PIL import Image
    img = Image.open(Path(RASTER_OUT, "cost_preview.png"))
    assert img.mode == "L"


@pytest.mark.skipif(not SOURCE_GPKG.exists(), reason="Source GPKG not available")
def test_raster_requires_voltage_level():
    """验证电压等级为空时抛出 ValueError。"""
    from smart_kg.raster_executor import build_cost_raster

    with pytest.raises(ValueError, match="voltage_level"):
        build_cost_raster(
            gpkg_path=SOURCE_GPKG,
            out_dir=Path(RASTER_OUT),
            voltage_level="",
        )


def test_build_cost_raster_distributes_total_cost_and_applies_buffer(tmp_path):
    """验证合成数据下成本像素正确分配、禁建缓冲生效、基础成本正确叠加。"""
    import geopandas as gpd
    import rasterio
    from shapely.geometry import Point, box

    from smart_kg.raster_executor import build_cost_raster

    gpkg_path = tmp_path / "synthetic_costed.gpkg"
    out_dir = tmp_path / "synthetic_raster"

    gdf = gpd.GeoDataFrame(
        {
            "featureName": ["建筑A", "河流A"],
            "C_CALC_MD": ["MAIN_COST_INCREMENT", "FORBIDDEN"],
            "C_EFF_VAL": [100.0, np.nan],
            "C_EFF_ATTR": ["S_CNT", None],
            "C_RULE_ID": ["cost_rule:test:building", "constraint_rule:test:river"],
            "C_RULE_NM": ["建筑A成本", "河流A禁建"],
            "C_REASON_CD": [2001, 1001],
            "C_BUF_DIST_M": [np.nan, 10.0],
            "C_AVOID_MD": [None, "BUFFER"],
            "S_ID": ["feat:building:1", "feat:river:1"],
            "S_NM": ["建筑A", "河流A"],
        },
        geometry=[box(0, 0, 20, 20), Point(40, 40)],
        crs="EPSG:4550",
    )
    tower_gdf = gpd.GeoDataFrame(
        {
            "featureName": ["铁塔A"],
            "C_CALC_MD": ["MAIN_COST_INCREMENT"],
            "C_EFF_VAL": [500.0],
            "C_EFF_ATTR": ["S_CNT"],
            "C_RULE_ID": ["source_fallback:TT"],
            "C_RULE_NM": ["铁塔源数据成本回填"],
            "C_REASON_CD": [4001],
            "C_BUF_DIST_M": [np.nan],
            "C_AVOID_MD": [None],
            "S_ID": ["feat:tower:1"],
            "S_NM": ["铁塔A"],
        },
        geometry=[box(60, 60, 70, 70)],
        crs="EPSG:4550",
    )
    gdf.to_file(gpkg_path, layer="features", driver="GPKG")
    tower_gdf.to_file(gpkg_path, layer="tower", driver="GPKG", mode="a")

    metadata = build_cost_raster(
        gpkg_path=gpkg_path,
        out_dir=out_dir,
        voltage_level="110kV",
        resolution=10.0,
        calculation_crs="EPSG:4550",
    )

    with rasterio.open(out_dir / "cost_surface.tif") as src:
        cost_data = src.read(1)
    with rasterio.open(out_dir / "blocked_mask.tif") as src:
        blocked_data = src.read(1)
    with rasterio.open(out_dir / "reason_code.tif") as src:
        reason_data = src.read(1)

    traversable = cost_data[blocked_data == 0]
    traversable_count = int((blocked_data == 0).sum())
    assert traversable.min() >= 1.0
    assert np.isclose(traversable.sum(), traversable_count + 100.0, atol=1e-6)
    assert int(blocked_data.sum()) > 0
    assert float(cost_data[blocked_data > 0].max()) > float(cost_data[blocked_data == 0].max())
    assert 1001 in set(int(v) for v in np.unique(reason_data))
    assert metadata["cost_surface_path"].endswith("cost_surface.tif")
    assert metadata["blocked_mask_path"].endswith("blocked_mask.tif")
    assert metadata["base_cost"] == 1.0
    assert metadata["excluded_layers"] == ["tower"]
    assert "tower" not in metadata["included_layers"]


def test_build_cost_raster_requires_positive_base_cost(tmp_path):
    """验证基础成本为零或负数时抛出 ValueError。"""
    import geopandas as gpd
    from shapely.geometry import box

    from smart_kg.raster_executor import build_cost_raster

    gpkg_path = tmp_path / "invalid_base_cost.gpkg"
    out_dir = tmp_path / "invalid_base_cost_out"
    gdf = gpd.GeoDataFrame(
        {
            "featureName": ["建筑A"],
            "C_CALC_MD": ["MAIN_COST_INCREMENT"],
            "C_EFF_VAL": [10.0],
            "C_EFF_ATTR": ["S_CNT"],
            "S_ID": ["feat:building:1"],
            "S_NM": ["建筑A"],
        },
        geometry=[box(0, 0, 20, 20)],
        crs="EPSG:4550",
    )
    gdf.to_file(gpkg_path, layer="features", driver="GPKG")

    with pytest.raises(ValueError, match="base_cost"):
        build_cost_raster(
            gpkg_path=gpkg_path,
            out_dir=out_dir,
            voltage_level="110kV",
            base_cost=0.0,
        )


def test_build_cost_raster_respects_included_and_excluded_layers(tmp_path):
    """验证 included_layers 和 excluded_layers 过滤器正确生效。"""
    import geopandas as gpd
    import rasterio
    from shapely.geometry import box

    from smart_kg.raster_executor import build_cost_raster

    gpkg_path = tmp_path / "layer_filter.gpkg"
    out_dir = tmp_path / "layer_filter_out"

    building_gdf = gpd.GeoDataFrame(
        {
            "featureName": ["建筑A"],
            "C_CALC_MD": ["MAIN_COST_INCREMENT"],
            "C_EFF_VAL": [40.0],
            "C_EFF_ATTR": ["S_CNT"],
            "C_RULE_ID": ["cost_rule:test:building"],
            "C_RULE_NM": ["建筑成本"],
            "C_REASON_CD": [2001],
            "S_ID": ["feat:building:1"],
            "S_NM": ["建筑A"],
        },
        geometry=[box(0, 0, 10, 10)],
        crs="EPSG:4550",
    )
    road_gdf = gpd.GeoDataFrame(
        {
            "featureName": ["道路A"],
            "C_CALC_MD": ["FORBIDDEN"],
            "C_EFF_VAL": [np.nan],
            "C_EFF_ATTR": [None],
            "C_RULE_ID": ["constraint_rule:test:road"],
            "C_RULE_NM": ["道路禁建"],
            "C_REASON_CD": [1001],
            "C_BUF_DIST_M": [5.0],
            "C_AVOID_MD": ["BUFFER"],
            "S_ID": ["feat:road:1"],
            "S_NM": ["道路A"],
        },
        geometry=[box(20, 0, 30, 10)],
        crs="EPSG:4550",
    )
    tower_gdf = gpd.GeoDataFrame(
        {
            "featureName": ["铁塔A"],
            "C_CALC_MD": ["MAIN_COST_INCREMENT"],
            "C_EFF_VAL": [100.0],
            "C_EFF_ATTR": ["S_CNT"],
            "C_RULE_ID": ["source_fallback:TT"],
            "C_RULE_NM": ["铁塔成本"],
            "C_REASON_CD": [4001],
            "S_ID": ["feat:tower:1"],
            "S_NM": ["铁塔A"],
        },
        geometry=[box(40, 0, 50, 10)],
        crs="EPSG:4550",
    )

    building_gdf.to_file(gpkg_path, layer="building", driver="GPKG")
    road_gdf.to_file(gpkg_path, layer="road", driver="GPKG", mode="a")
    tower_gdf.to_file(gpkg_path, layer="tower", driver="GPKG", mode="a")

    metadata = build_cost_raster(
        gpkg_path=gpkg_path,
        out_dir=out_dir,
        voltage_level="110kV",
        resolution=10.0,
        calculation_crs="EPSG:4550",
        included_layers=["building"],
        excluded_layers=["tower", "road"],
    )

    with rasterio.open(out_dir / "blocked_mask.tif") as src:
        blocked = src.read(1)

    assert metadata["included_layers"] == ["building"]
    assert sorted(metadata["excluded_layers"]) == ["road", "tower"]
    assert int(blocked.sum()) == 0
