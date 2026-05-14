"""GPKG 标准化和成本规则提取的集成测试。

覆盖成本规则标准化、GPKG 图层处理、规则匹配和米制投影等核心流程。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

EXCEL_PATH = Path(__file__).resolve().parents[2] / "选线数据分类体系20260422.xlsx"
SOURCE_GPKG = Path(__file__).resolve().parents[2] / "上海_source.gpkg"
OUT_GPKG = Path(__file__).resolve().parents[1] / "data" / "test_output" / "上海_costed_test.gpkg"
RULES_OUT = Path(__file__).resolve().parents[1] / "data" / "test_output" / "cost_rules_test.json"


@pytest.fixture(autouse=True)
def clean_output():
    """自动清理测试输出文件。"""
    OUT_GPKG.parent.mkdir(parents=True, exist_ok=True)
    yield
    if OUT_GPKG.exists():
        OUT_GPKG.unlink()
    if RULES_OUT.exists():
        RULES_OUT.unlink()


@pytest.mark.skipif(not EXCEL_PATH.exists(), reason="Excel file not available")
def test_standardize_cost_rules_extracts_entries():
    """验证成本规则标准化能正确提取禁建和数值型规则条目。"""
    from smart_kg.cost_rule_loader import standardize_cost_rules

    rows = standardize_cost_rules(EXCEL_PATH, RULES_OUT, rule_set_version="test")
    assert len(rows) > 0
    assert RULES_OUT.exists()

    data = json.loads(RULES_OUT.read_text(encoding="utf-8"))
    assert len(data) == len(rows)

    has_forbidden = any(r["calc_mode"] == "FORBIDDEN" for r in data)
    has_numeric = any(r["effect_value_status"] == "NUMERIC" for r in data)
    assert has_forbidden, "Should extract FORBIDDEN rules from max values"
    assert has_numeric, "Should extract NUMERIC cost rules"


@pytest.mark.skipif(not EXCEL_PATH.exists(), reason="Excel file not available")
def test_negotiable_and_not_considered_handling():
    """验证 NOT_CONSIDERED 状态被过滤，NEGOTIABLE 规则的计量值为 None。"""
    from smart_kg.cost_rule_loader import standardize_cost_rules

    rows = standardize_cost_rules(EXCEL_PATH, None, rule_set_version="test")
    statuses = {r["effect_value_status"] for r in rows}
    assert "NOT_CONSIDERED" not in statuses, "NOT_CONSIDERED should be filtered out"
    if "NEGOTIABLE" in statuses:
        negotiable = [r for r in rows if r["effect_value_status"] == "NEGOTIABLE"]
        for r in negotiable:
            assert r["effect_value"] is None


@pytest.mark.skipif(not SOURCE_GPKG.exists(), reason="Source GPKG not available")
@pytest.mark.skipif(not EXCEL_PATH.exists(), reason="Excel file not available")
def test_gpkg_standardize_produces_output():
    """验证 GPKG 标准化输出包含完整的 S_* 和 C_* 字段。"""
    from smart_kg.cost_rule_loader import standardize_cost_rules, load_cost_rules
    from smart_kg.gpkg_standardizer import standardize_gpkg

    standardize_cost_rules(EXCEL_PATH, RULES_OUT, rule_set_version="test")
    rules = load_cost_rules(RULES_OUT)

    stats = standardize_gpkg(
        source_gpkg=SOURCE_GPKG,
        out_gpkg=OUT_GPKG,
        voltage_level="110kV",
        rules=rules,
    )

    assert OUT_GPKG.exists()
    assert stats["total_features"] > 0
    assert "layers" in stats

    import geopandas as gpd
    layers = gpd.list_layers(OUT_GPKG)
    assert len(layers) > 0

    gdf = gpd.read_file(OUT_GPKG, layer=layers.iloc[0]["name"])
    assert "S_ID" in gdf.columns
    assert "S_NM" in gdf.columns
    assert "S_AREA" in gdf.columns
    assert "C_CALC_MD" in gdf.columns


@pytest.mark.skipif(not SOURCE_GPKG.exists(), reason="Source GPKG not available")
def test_gpkg_standardize_without_rules():
    """验证无规则时标准化仍可完成，但 enriched 计数为 0。"""
    from smart_kg.gpkg_standardizer import standardize_gpkg

    stats = standardize_gpkg(
        source_gpkg=SOURCE_GPKG,
        out_gpkg=OUT_GPKG,
        voltage_level="110kV",
        rules=[],
    )

    assert OUT_GPKG.exists()
    assert stats["total_features"] > 0
    assert stats["enriched"] == 0


def test_voltage_level_required():
    """验证电压等级为空时抛出 ValueError。"""
    from smart_kg.gpkg_standardizer import standardize_gpkg

    with pytest.raises(ValueError, match="voltage_level"):
        standardize_gpkg(
            source_gpkg=SOURCE_GPKG,
            out_gpkg=OUT_GPKG,
            voltage_level="",
        )


def test_match_rule_falls_back_to_name_when_code_system_differs():
    """验证编码体系不同时规则匹配能退回到名称匹配并成功命中。"""
    from shapely.geometry import box

    from smart_kg.cost_rule_loader import CostRuleEntry
    from smart_kg.gpkg_standardizer import _match_rule

    row = {
        "S_STYP_CD": "SD",
        "S_TYP_DETAIL": "市道",
        "S_STYP_NM": "道路",
        "S_TYP_NM": "施工与运维条件",
        "S_LVL": None,
        "geometry": box(0, 0, 10, 10),
    }
    rule = CostRuleEntry(
        rule_id="cost_rule:test:road",
        rule_name="市道110kV成本",
        source_table="22道路",
        source_row=4,
        feature_type_code="3000",
        feature_type_name="道路",
        feature_subtype_code="3005",
        feature_subtype_name="市道",
        geometry_kind="line",
        calc_mode="MAIN_COST_INCREMENT",
        effect_value=5.0,
        effect_value_status="NUMERIC",
        effect_attr="S_LTH",
        voltage_level="110kV",
        raw_value=5.0,
        reason_code=3001,
        priority=200,
        match_condition_json={"field": "feature_subtype_code", "operator": "eq", "value": "3005"},
        enabled=True,
    )

    matched, score = _match_rule(row, "110kV", [rule])

    assert matched is not None
    assert matched.rule_id == rule.rule_id
    assert score > 0


def test_resolve_metric_crs_uses_shanghai_zone():
    """验证上海经度范围（121.5+）的数据使用 EPSG:4550 投影。"""
    import geopandas as gpd
    from shapely.geometry import box

    from smart_kg.gpkg_standardizer import resolve_metric_crs

    gdf = gpd.GeoDataFrame(geometry=[box(121.8, 30.9, 121.9, 31.0)], crs="EPSG:4490")

    assert resolve_metric_crs(gdf) == "EPSG:4550"


def test_fallback_reason_code_is_stable():
    """验证同一种子字符串生成的回退原因码具有确定性。"""
    from smart_kg.gpkg_standardizer import _fallback_reason_code

    assert _fallback_reason_code("source_fallback:NT") == _fallback_reason_code("source_fallback:NT")
