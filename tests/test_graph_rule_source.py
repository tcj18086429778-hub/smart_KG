"""图谱规则源读取单元测试。

验证从 Neo4j 图谱读取 CostRuleEntry、GraphRasterSpec 和 GraphRuleBundle 的完整流程。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


def test_cost_rule_entry_from_props_parses_graph_payload() -> None:
    """验证从 Neo4j 节点属性解析 CostRuleEntry 时字段映射正确。"""
    from smart_kg.graph_rule_source import _cost_rule_entry_from_props

    entry = _cost_rule_entry_from_props(
        {
            "id": "cost_rule:test:road",
            "rule_name": "市道成本",
            "source_table": "22道路",
            "source_row": 7,
            "feature_type_name": "道路",
            "feature_subtype_code": "3005",
            "feature_subtype_name": "市道",
            "calc_mode": "MAIN_COST_INCREMENT",
            "effect_target": "ALL",
            "effect_value": 100.0,
            "effect_value_status": "NUMERIC",
            "effect_attr": "S_LTH",
            "effect_unit": "万元/公里",
            "voltage_level": "110kV",
            "priority": 300,
            "reason_code": 2101,
            "match_condition_json": '{"field":"feature_subtype_code","operator":"eq","value":"3005"}',
            "enabled": True,
        }
    )

    assert entry.rule_id == "cost_rule:test:road"
    assert entry.rule_name == "市道成本"
    assert entry.effect_attr == "S_LTH"
    assert entry.match_condition_json["value"] == "3005"


def test_raster_spec_from_graph_preserves_layers() -> None:
    """验证从图谱读取的 RasterSpec 保留去重后的图层列表。"""
    from smart_kg.graph_rule_source import _raster_spec_from_graph

    spec = _raster_spec_from_graph(
        {
            "props": {"id": "spec:110kV", "resolution": 30.0, "base_cost": 1.5, "calculation_crs": "EPSG:4550"},
            "included_layers": ["building", "road", "building"],
            "excluded_layers": ["tower"],
        }
    )

    assert spec.spec_id == "spec:110kV"
    assert spec.resolution == 30.0
    assert spec.base_cost == 1.5
    assert spec.calculation_crs == "EPSG:4550"
    assert spec.included_layers == ["building", "road"]
    assert spec.excluded_layers == ["tower"]


class _FakeResult:
    """模拟 Neo4j 查询结果对象。"""

    def __init__(self, rows):
        """保存预置结果行。"""
        self._rows = rows

    def single(self):
        """返回首条结果，模拟 Neo4j `single()` 行为。"""
        return self._rows[0] if self._rows else None

    def __iter__(self):
        """支持将结果对象当作可迭代行集合使用。"""
        return iter(self._rows)


class _FakeSession:
    """模拟 Neo4j Session，用于断言查询路由。"""

    def __init__(self):
        """初始化调用记录容器。"""
        self.calls = []

    def run(self, query, **params):
        """按查询内容返回预置结果。"""
        self.calls.append((query, params))
        compact = " ".join(query.split())
        if "MATCH (rs:RuleSet)" in compact:
            return _FakeResult([])
        if "MATCH (cr:CostRule)" in compact and "NOT cr:Rule" in compact:
            return _FakeResult(
                [
                    {
                        "props": {
                            "id": "cost_rule:test:building",
                            "rule_name": "建筑成本",
                            "source_table": "8通道清理",
                            "source_row": 20,
                            "feature_type_name": "通道清理",
                            "feature_subtype_name": "民房",
                            "calc_mode": "MAIN_COST_INCREMENT",
                            "effect_target": "ALL",
                            "effect_value": 300.0,
                            "effect_value_status": "NUMERIC",
                            "effect_attr": "S_CNT",
                            "voltage_level": "110kV",
                            "priority": 300,
                            "reason_code": 2036,
                            "enabled": True,
                        }
                    }
                ]
            )
        if "MATCH (rd:RouteDecision {voltage_level: $voltage_level})" in compact:
            return _FakeResult(
                [
                    {
                        "props": {"id": "cost_surface:test", "resolution": 25.0, "crs": "EPSG:4550"},
                        "included_layers": ["building", "water"],
                        "excluded_layers": ["tower"],
                    }
                ]
            )
        raise AssertionError(f"Unexpected query: {query}")

    def __enter__(self):
        """支持 `with session` 语法。"""
        return self

    def __exit__(self, exc_type, exc, tb):
        """结束上下文时不吞掉异常。"""
        return False


class _FakeDriver:
    """模拟 Neo4j Driver。"""

    def __init__(self):
        """预置单一 FakeSession。"""
        self.session_obj = _FakeSession()

    def session(self, database=None):
        """返回伪造的 Session。"""
        return self.session_obj

    def __enter__(self):
        """支持 `with driver` 语法。"""
        return self

    def __exit__(self, exc_type, exc, tb):
        """结束上下文时不吞掉异常。"""
        return False


def test_load_graph_rule_bundle_falls_back_to_route_decision_graph() -> None:
    """验证无 RuleSet 时回退到从 RouteDecision 图谱读取规则和参数。"""
    from smart_kg.graph_rule_source import load_graph_rule_bundle

    fake_driver = _FakeDriver()
    with patch("neo4j.GraphDatabase.driver", return_value=fake_driver):
        bundle = load_graph_rule_bundle(voltage_level="110kV")

    assert bundle.rule_set_id is None
    assert bundle.raster_spec.source == "route_decision_fallback"
    assert bundle.raster_spec.resolution == 25.0
    assert bundle.raster_spec.included_layers == ["building", "water"]
    assert bundle.raster_spec.excluded_layers == ["tower"]
    assert len(bundle.rules) == 1
    assert bundle.rules[0].rule_id == "cost_rule:test:building"


def test_run_route_pipeline_from_graph_passes_graph_spec(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """验证 run_route_pipeline_from_graph 将图谱规格参数正确传递给标准化和栅格化步骤。"""
    from smart_kg.cost_rule_loader import CostRuleEntry
    from smart_kg.graph_rule_source import GraphRasterSpec, GraphRuleBundle, run_route_pipeline_from_graph

    captured: dict[str, object] = {}

    bundle = GraphRuleBundle(
        voltage_level="110kV",
        rule_set_id="ruleset:test",
        rule_set_version="20260422",
        rules=[
            CostRuleEntry(
                rule_id="cost_rule:test:1",
                rule_name="测试成本",
                source_table="table",
                source_row=1,
                calc_mode="MAIN_COST_INCREMENT",
                effect_value_status="NUMERIC",
                effect_value=10.0,
                reason_code=2001,
            )
        ],
        raster_spec=GraphRasterSpec(
            resolution=18.0,
            base_cost=2.0,
            calculation_crs="EPSG:4550",
            included_layers=["building"],
            excluded_layers=["tower"],
            source="ruleset",
        ),
    )

    def fake_load_graph_rule_bundle(voltage_level, rule_set_version=None):
        """伪造图谱规则集加载结果。"""
        assert voltage_level == "110kV"
        assert rule_set_version == "20260422"
        return bundle

    def fake_standardize_gpkg(source_gpkg, out_gpkg, voltage_level, rules, layers=None):
        """伪造 GPKG 成本化过程并记录调用参数。"""
        captured["standardize"] = {
            "source_gpkg": source_gpkg,
            "out_gpkg": out_gpkg,
            "voltage_level": voltage_level,
            "rule_count": len(rules),
            "layers": layers,
        }
        return {"total_features": 1, "enriched": 1}

    def fake_build_cost_raster(gpkg_path, out_dir, voltage_level, resolution, calculation_crs, base_cost, included_layers, excluded_layers):
        """伪造栅格构建过程并记录执行规格。"""
        captured["raster"] = {
            "gpkg_path": gpkg_path,
            "out_dir": out_dir,
            "voltage_level": voltage_level,
            "resolution": resolution,
            "calculation_crs": calculation_crs,
            "base_cost": base_cost,
            "included_layers": included_layers,
            "excluded_layers": excluded_layers,
        }
        return {"cost_surface_path": str(out_dir / "cost_surface.tif")}

    monkeypatch.setattr("smart_kg.graph_rule_source.load_graph_rule_bundle", fake_load_graph_rule_bundle)
    monkeypatch.setattr("smart_kg.graph_rule_source.standardize_gpkg", fake_standardize_gpkg)
    monkeypatch.setattr("smart_kg.graph_rule_source.build_cost_raster", fake_build_cost_raster)

    result = run_route_pipeline_from_graph(
        source_gpkg=tmp_path / "source.gpkg",
        out_gpkg=tmp_path / "costed.gpkg",
        raster_out_dir=tmp_path / "raster",
        voltage_level="110kV",
        rule_set_version="20260422",
    )

    assert result["graph_rule_set_id"] == "ruleset:test"
    assert captured["standardize"]["rule_count"] == 1
    assert captured["raster"]["resolution"] == 18.0
    assert captured["raster"]["base_cost"] == 2.0
    assert captured["raster"]["included_layers"] == ["building"]
    assert captured["raster"]["excluded_layers"] == ["tower"]
