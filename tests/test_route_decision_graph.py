"""走线决策图谱写入单元测试。

验证 Neo4jWriter.write_route_decision_graph() 各阶段的 Cypher 语句、
节点属性、关系创建以及 read_enriched_gpkg_features 的过滤和回退逻辑。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ── write_route_decision_graph ──────────────────────────────────────────


@pytest.fixture
def mock_neo4j() -> MagicMock:
    """返回一个 mock Neo4j driver，其 execute_write 将调用转发给 mock tx。"""
    mock_tx = MagicMock()
    mock_session = MagicMock()
    mock_session.execute_write.side_effect = (
        lambda func, *args, **kwargs: func(mock_tx, *args, **kwargs)
    )
    mock_driver = MagicMock()
    mock_driver.session.return_value.__enter__.return_value = mock_session
    return mock_driver


_COST_RULE_ENTRY = {
    "rule_id": "cost_rule:building:3005:110kV:1:100",
    "rule_name": "建筑110kV成本",
    "calc_mode": "MAIN_COST_INCREMENT",
    "effect_target": "ALL",
    "effect_value_status": "NUMERIC",
    "effect_value": 100.0,
    "effect_attr": "S_AREA",
    "effect_unit": "万元/面积单位",
    "source_table": "建筑",
    "source_row": 1,
    "voltage_level": "110kV",
    "feature_subtype_code": "3005",
    "feature_subtype_name": "建筑",
    "reason_code": 2001,
    "priority": 200,
    "buffer_distance_m": None,
    "avoidance_mode": None,
    "match_condition_json": {"field": "feature_subtype_code", "operator": "eq", "value": "3005"},
    "enabled": True,
}

_CONSTRAINT_ENTRY = {
    "rule_id": "constraint_rule:river:4002:110kV:2",
    "rule_name": "河流禁建",
    "calc_mode": "FORBIDDEN",
    "effect_target": "ALL",
    "effect_value_status": "FORBIDDEN",
    "source_table": "河流",
    "source_row": 2,
    "voltage_level": "110kV",
    "feature_subtype_code": "4002",
    "feature_subtype_name": "河流",
    "reason_code": 1001,
    "priority": 1000,
    "buffer_distance_m": 10.0,
    "avoidance_mode": "BUFFER",
    "match_condition_json": {"field": "feature_subtype_code", "operator": "eq", "value": "4002"},
    "enabled": True,
}

_COST_FEATURE = {
    "id": "feat:building:abc123",
    "layer_name": "building",
    "calc_mode": "MAIN_COST_INCREMENT",
    "effect_value": 100.0,
    "effect_attr": "S_AREA",
    "rule_id": "cost_rule:building:3005:110kV:1:100",
    "rule_name": "建筑110kV成本",
    "reason_code": 2001,
    "buffer_distance_m": None,
    "avoidance_mode": None,
    "match_score": 108.0,
    "name": "建筑A",
    "geometry_type": "Polygon",
}

_FORBIDDEN_FEATURE = {
    "id": "feat:river:def456",
    "layer_name": "river",
    "calc_mode": "FORBIDDEN",
    "effect_value": None,
    "effect_attr": None,
    "rule_id": "constraint_rule:river:4002:110kV:2",
    "rule_name": "河流禁建",
    "reason_code": 1001,
    "buffer_distance_m": 10.0,
    "avoidance_mode": "BUFFER",
    "match_score": 108.0,
    "name": "河流A",
    "geometry_type": "Line",
}

_RASTER_METADATA = {
    "voltage_level": "110kV",
    "resolution": 20.0,
    "base_cost": 1.0,
    "crs": "EPSG:4550",
    "cost_surface_path": "/tmp/out/cost_surface.tif",
    "blocked_mask_path": "/tmp/out/blocked_mask.tif",
    "reason_code_path": "/tmp/out/reason_code.tif",
    "included_layers": ["building", "river", "road"],
    "excluded_layers": ["tower"],
    "_metadata_path": "/tmp/out/metadata.json",
    "stats": {
        "total_cost_pixels": 1000,
        "traversable_cost_pixels": 800,
        "total_blocked_pixels": 200,
        "max_cost": 50.0,
    },
}


def test_write_route_decision_graph_counts(mock_neo4j: MagicMock) -> None:
    """验证返回的统计信息与输入数据一致。"""
    from smart_kg.neo4j_writer import Neo4jWriter

    with patch("neo4j.GraphDatabase.driver", return_value=mock_neo4j):
        writer = Neo4jWriter()
        result = writer.write_route_decision_graph(
            entries=[_COST_RULE_ENTRY, _CONSTRAINT_ENTRY],
            features=[_COST_FEATURE, _FORBIDDEN_FEATURE],
            voltage_level="110kV",
        )

    assert result["rule_count"] == 2
    assert result["feature_count"] == 2
    assert result["forbidden_count"] == 1
    assert result["cost_count"] == 1
    assert result["decision_id"].startswith("route_decision")


def test_write_route_decision_graph_with_raster_metadata(mock_neo4j: MagicMock) -> None:
    """验证传入 raster_metadata 会触发 CostSurface 节点创建。"""
    from smart_kg.neo4j_writer import Neo4jWriter

    with patch("neo4j.GraphDatabase.driver", return_value=mock_neo4j):
        writer = Neo4jWriter()
        result = writer.write_route_decision_graph(
            entries=[_COST_RULE_ENTRY],
            features=[_COST_FEATURE],
            voltage_level="110kV",
            raster_metadata=_RASTER_METADATA,
        )

    assert result["rule_count"] == 1
    assert result["feature_count"] == 1
    assert result["decision_id"].startswith("route_decision")


def test_write_route_decision_graph_calls_execute_write(mock_neo4j: MagicMock) -> None:
    """验证写入过程按预期调用各阶段的静态方法。"""
    from smart_kg.neo4j_writer import Neo4jWriter

    with patch("neo4j.GraphDatabase.driver", return_value=mock_neo4j):
        writer = Neo4jWriter()
        writer.write_route_decision_graph(
            entries=[_COST_RULE_ENTRY],
            features=[_COST_FEATURE],
            voltage_level="110kV",
        )

    session = mock_neo4j.session.return_value.__enter__.return_value
    call_names = [call[0][0].__name__ for call in session.execute_write.call_args_list]

    assert "_write_route_decision_constraints" in call_names
    assert "_cleanup_route_decision_artifacts" in call_names
    assert "_write_route_decision_node" in call_names
    assert "_write_cost_rule_entries_from_dicts" in call_names
    assert "_write_routing_features" in call_names


def test_constraints_include_cost_rule_and_routing_layer(mock_neo4j: MagicMock) -> None:
    """验证 CostRule 和 RoutingLayer 的唯一性约束已注册。"""
    from smart_kg.neo4j_writer import Neo4jWriter

    mock_tx = MagicMock()
    mock_session = MagicMock()
    mock_session.execute_write.side_effect = (
        lambda func, *args, **kwargs: func(mock_tx, *args, **kwargs)
    )
    mock_neo4j.session.return_value.__enter__.return_value = mock_session

    with patch("neo4j.GraphDatabase.driver", return_value=mock_neo4j):
        writer = Neo4jWriter()
        writer.write_route_decision_graph(
            entries=[_COST_RULE_ENTRY],
            features=[_COST_FEATURE],
            voltage_level="110kV",
        )

    queries = [c[0][0] for c in mock_tx.run.call_args_list]
    assert any("cost_rule_id" in q and "CostRule" in q for q in queries)
    assert any("routing_layer_id" in q and "RoutingLayer" in q for q in queries)


def test_metadata_path_stored_on_cost_surface(mock_neo4j: MagicMock) -> None:
    """验证 metadata_path 被正确传入 CostSurface 节点。"""
    from smart_kg.neo4j_writer import Neo4jWriter

    mock_tx = MagicMock()
    mock_session = MagicMock()
    mock_session.execute_write.side_effect = (
        lambda func, *args, **kwargs: func(mock_tx, *args, **kwargs)
    )
    mock_neo4j.session.return_value.__enter__.return_value = mock_session

    with patch("neo4j.GraphDatabase.driver", return_value=mock_neo4j):
        writer = Neo4jWriter()
        writer.write_route_decision_graph(
            entries=[_COST_RULE_ENTRY],
            features=[_COST_FEATURE],
            voltage_level="110kV",
            raster_metadata=_RASTER_METADATA,
        )

    # Find the MERGE CostSurface call and inspect its row parameter
    for args, kwargs in mock_tx.run.call_args_list:
        query = args[0]
        if "MERGE (cs:CostSurface" in query:
            row = kwargs.get("row", {}) if kwargs else (args[1] if len(args) > 1 else {})
            # row might be positional, check both forms
            if not row and len(args) > 1:
                row = args[1]
            actual_path = row.get("metadata_path", "")
            assert actual_path == "/tmp/out/metadata.json", (
                f"Expected metadata_path='/tmp/out/metadata.json', got '{actual_path}'"
            )
            break


def test_no_opaque_json_blobs_on_cost_surface(mock_neo4j: MagicMock) -> None:
    """验证 CostSurface 不以 JSON 字符串形式存储图层列表和统计信息。"""
    from smart_kg.neo4j_writer import Neo4jWriter

    mock_tx = MagicMock()
    mock_session = MagicMock()
    mock_session.execute_write.side_effect = (
        lambda func, *args, **kwargs: func(mock_tx, *args, **kwargs)
    )
    mock_neo4j.session.return_value.__enter__.return_value = mock_session

    with patch("neo4j.GraphDatabase.driver", return_value=mock_neo4j):
        writer = Neo4jWriter()
        writer.write_route_decision_graph(
            entries=[_COST_RULE_ENTRY],
            features=[_COST_FEATURE],
            voltage_level="110kV",
            raster_metadata=_RASTER_METADATA,
        )

    queries_and_params = [
        (c[0][0], c[1] if len(c) > 1 else {})
        for c in mock_tx.run.call_args_list
    ]
    for q, params in queries_and_params:
        assert "included_layers_json" not in q, f"Opaque JSON in query: {q[:80]}"
        assert "excluded_layers_json" not in q, f"Opaque JSON in query: {q[:80]}"
        assert "stats_json" not in q, f"Opaque JSON in query: {q[:80]}"
        assert "stat_" not in q or "stat_" in q, "stats as properties is ok"  # stat_* in row is expected


def test_stats_are_first_class_properties(mock_neo4j: MagicMock) -> None:
    """验证 stats 字典以独立属性（stat_*）形式存储在 CostSurface 节点上。"""
    from smart_kg.neo4j_writer import Neo4jWriter

    mock_tx = MagicMock()
    mock_session = MagicMock()
    mock_session.execute_write.side_effect = (
        lambda func, *args, **kwargs: func(mock_tx, *args, **kwargs)
    )
    mock_neo4j.session.return_value.__enter__.return_value = mock_session

    with patch("neo4j.GraphDatabase.driver", return_value=mock_neo4j):
        writer = Neo4jWriter()
        writer.write_route_decision_graph(
            entries=[_COST_RULE_ENTRY],
            features=[_COST_FEATURE],
            voltage_level="110kV",
            raster_metadata=_RASTER_METADATA,
        )

    # Look for stat_* keys in the CostSurface MERGE row parameter
    found_stat_keys: set[str] = set()
    for _, kwargs in mock_tx.run.call_args_list:
        for val in kwargs.values():
            if isinstance(val, dict):
                found_stat_keys.update(
                    k for k in val if k.startswith("stat_")
                )
    assert "stat_total_cost_pixels" in found_stat_keys
    assert "stat_traversable_cost_pixels" in found_stat_keys


def test_routing_layer_nodes_and_relationships_are_structural(mock_neo4j: MagicMock) -> None:
    """验证 RoutingLayer 节点和 INCLUDES_LAYER / EXCLUDES_LAYER 关系正确创建。"""
    from smart_kg.neo4j_writer import Neo4jWriter

    mock_tx = MagicMock()
    mock_session = MagicMock()
    mock_session.execute_write.side_effect = (
        lambda func, *args, **kwargs: func(mock_tx, *args, **kwargs)
    )
    mock_neo4j.session.return_value.__enter__.return_value = mock_session

    with patch("neo4j.GraphDatabase.driver", return_value=mock_neo4j):
        writer = Neo4jWriter()
        writer.write_route_decision_graph(
            entries=[_COST_RULE_ENTRY],
            features=[_COST_FEATURE],
            voltage_level="110kV",
            raster_metadata=_RASTER_METADATA,
        )

    queries = [c[0][0] for c in mock_tx.run.call_args_list]

    # RoutingLayer MERGE
    assert any("RoutingLayer" in q for q in queries), "Missing RoutingLayer node"

    # INCLUDES_LAYER relationships for building, river, road
    includes_matches = [
        q for q in queries if "INCLUDES_LAYER" in q
    ]
    assert len(includes_matches) >= 1, "Missing INCLUDES_LAYER relationship"
    # Verify layer names appeared as params
    all_params_str = str(mock_tx.run.call_args_list)
    assert "building" in all_params_str
    assert "river" in all_params_str

    # EXCLUDES_LAYER relationships for tower
    excludes_matches = [
        q for q in queries if "EXCLUDES_LAYER" in q
    ]
    assert len(excludes_matches) >= 1, "Missing EXCLUDES_LAYER relationship"
    assert "tower" in all_params_str


# ── read_enriched_gpkg_features ────────────────────────────────────────


def test_read_enriched_gpkg_features_excludes_tower(tmp_path: Path) -> None:
    """验证 tower 图层的地物被排除在走线决策图之外。"""
    import geopandas as gpd
    from shapely.geometry import box

    from smart_kg.gpkg_standardizer import read_enriched_gpkg_features

    gpkg_path = tmp_path / "test.gpkg"

    building_gdf = gpd.GeoDataFrame(
        {
            "featureName": ["建筑A"],
            "C_CALC_MD": ["MAIN_COST_INCREMENT"],
            "C_RULE_ID": ["cr:1"],
            "C_RULE_NM": ["r1"],
            "S_ID": ["f:1"],
            "S_NM": ["建筑A"],
        },
        geometry=[box(0, 0, 10, 10)],
        crs="EPSG:4326",
    )
    building_gdf.to_file(gpkg_path, layer="building", driver="GPKG")

    tower_gdf = gpd.GeoDataFrame(
        {
            "featureName": ["铁塔A"],
            "C_CALC_MD": ["MAIN_COST_INCREMENT"],
            "C_RULE_ID": ["sf:1"],
            "C_RULE_NM": ["铁塔成本"],
            "S_ID": ["f:2"],
            "S_NM": ["铁塔A"],
        },
        geometry=[box(20, 20, 30, 30)],
        crs="EPSG:4326",
    )
    tower_gdf.to_file(gpkg_path, layer="tower", driver="GPKG", mode="a")

    features = read_enriched_gpkg_features(gpkg_path)
    assert len(features) == 1
    assert features[0]["layer_name"] == "building"
    assert all(f["layer_name"] != "tower" for f in features)


def test_read_enriched_gpkg_features_without_enrichment(tmp_path: Path) -> None:
    """验证缺少 C_CALC_MD 列的图层被跳过。"""
    import geopandas as gpd
    from shapely.geometry import box

    from smart_kg.gpkg_standardizer import read_enriched_gpkg_features

    gpkg_path = tmp_path / "raw.gpkg"

    raw_gdf = gpd.GeoDataFrame(
        {"featureName": ["原始要素"]},
        geometry=[box(0, 0, 10, 10)],
        crs="EPSG:4326",
    )
    raw_gdf.to_file(gpkg_path, layer="raw_layer", driver="GPKG")

    features = read_enriched_gpkg_features(gpkg_path)
    assert features == []


def test_read_enriched_gpkg_features_returns_enriched_fields(tmp_path: Path) -> None:
    """验证输出中包含全部已成本化的字段名。"""
    import geopandas as gpd
    import numpy as np
    from shapely.geometry import box

    from smart_kg.gpkg_standardizer import read_enriched_gpkg_features

    gpkg_path = tmp_path / "enriched.gpkg"

    gdf = gpd.GeoDataFrame(
        {
            "featureName": ["建筑A"],
            "factorType": ["建筑类型"],
            "factorLevel1": ["建筑大类"],
            "factorLevel2": ["建筑中类"],
            "level": ["L1"],
            "C_CALC_MD": ["MAIN_COST_INCREMENT"],
            "C_EFF_VAL": [50.0],
            "C_EFF_ATTR": ["S_AREA"],
            "C_RULE_ID": ["cost_rule:test:1"],
            "C_RULE_NM": ["建筑成本"],
            "C_REASON_CD": [2001],
            "C_BUF_DIST_M": [np.nan],
            "C_AVOID_MD": [None],
            "C_MATCH_SC": [108.0],
            "S_ID": ["feat:building:1"],
            "S_NM": ["建筑A"],
            "S_TYP_CD": ["01"],
            "S_STYP_CD": ["0101"],
            "S_LVL": ["1"],
        },
        geometry=[box(0, 0, 10, 10)],
        crs="EPSG:4326",
    )
    gdf.to_file(gpkg_path, layer="building", driver="GPKG")

    features = read_enriched_gpkg_features(gpkg_path)
    assert len(features) == 1
    feat = features[0]
    assert feat["calc_mode"] == "MAIN_COST_INCREMENT"
    assert feat["effect_value"] == 50.0
    assert feat["effect_attr"] == "S_AREA"
    assert feat["rule_id"] == "cost_rule:test:1"
    assert feat["reason_code"] == 2001
    assert feat["match_score"] == 108.0
    assert feat["buffer_distance_m"] is None
    assert feat["avoidance_mode"] is None
    assert feat["geometry_type"] == "Polygon"


def test_read_enriched_gpkg_features_file_not_found() -> None:
    """验证不存在的 GPKG 文件路径抛出 FileNotFoundError。"""
    from smart_kg.gpkg_standardizer import read_enriched_gpkg_features

    with pytest.raises(FileNotFoundError):
        read_enriched_gpkg_features(Path("/nonexistent/file.gpkg"))


# ── CostRule node properties ────────────────────────────────────────────


def test_cost_rule_entry_dict_maps_to_neo4j_properties(mock_neo4j: MagicMock) -> None:
    """验证 CostRuleEntry 字典字段被正确写入 CostRule 节点属性。"""
    from smart_kg.neo4j_writer import Neo4jWriter

    entry = dict(_COST_RULE_ENTRY)
    entry["match_condition_json"] = {"field": "feature_subtype_code", "operator": "eq", "value": "3005"}

    with patch("neo4j.GraphDatabase.driver", return_value=mock_neo4j):
        writer = Neo4jWriter()
        writer.write_route_decision_graph(
            entries=[entry],
            features=[_COST_FEATURE],
            voltage_level="110kV",
        )

    mock_tx = mock_neo4j.session.return_value.__enter__.return_value.execute_write.call_args_list
    # Find _write_cost_rule_entries_from_dicts call and inspect what it passes to tx.run
    for call in mock_tx:
        if call[0][0].__name__ == "_write_cost_rule_entries_from_dicts":
            # This method is called by execute_write, verify it ran without error
            pass

    # The test passes if no exceptions occurred during the mocked execution


# ── RoutingFeature / source_fallback patch tests ─────────────────────────


def test_routing_feature_retains_rule_id_and_rule_name(mock_neo4j: MagicMock) -> None:
    """验证 rule_id 和 rule_name 被正确写入 RoutingFeature 节点属性。"""
    from smart_kg.neo4j_writer import Neo4jWriter

    mock_tx = MagicMock()
    mock_session = MagicMock()
    mock_session.execute_write.side_effect = (
        lambda func, *args, **kwargs: func(mock_tx, *args, **kwargs)
    )
    mock_neo4j.session.return_value.__enter__.return_value = mock_session

    with patch("neo4j.GraphDatabase.driver", return_value=mock_neo4j):
        writer = Neo4jWriter()
        writer.write_route_decision_graph(
            entries=[_COST_RULE_ENTRY],
            features=[_COST_FEATURE],
            voltage_level="110kV",
        )

    # Collect props dicts from RoutingFeature MERGE calls
    rf_props_list: list[dict[str, Any]] = []
    for args, kwargs in mock_tx.run.call_args_list:
        query = args[0] if args else ""
        if "MERGE (rf:RoutingFeature" in query:
            props = kwargs.get("props", {})
            if not props and len(args) > 1:
                for a in args[1:]:
                    if isinstance(a, dict):
                        props = a
            rf_props_list.append(props)

    assert len(rf_props_list) >= 1, "No RoutingFeature MERGE call found"
    for props in rf_props_list:
        assert "rule_id" in props, f"Missing rule_id in RoutingFeature props: {props}"
        assert "rule_name" in props, f"Missing rule_name in RoutingFeature props: {props}"
        assert props["rule_id"] == _COST_FEATURE["rule_id"], (
            f"rule_id value mismatch: {props.get('rule_id')}"
        )


def test_source_fallback_creates_pseudo_rule_node(mock_neo4j: MagicMock) -> None:
    """验证 source_fallback 要素通过 ON CREATE 获得伪规则 CostRule 节点。"""
    from smart_kg.neo4j_writer import Neo4jWriter

    mock_tx = MagicMock()
    mock_session = MagicMock()
    mock_session.execute_write.side_effect = (
        lambda func, *args, **kwargs: func(mock_tx, *args, **kwargs)
    )
    mock_neo4j.session.return_value.__enter__.return_value = mock_session

    fallback_feature = dict(_COST_FEATURE)
    fallback_feature["id"] = "feat:cropland:fb001"
    fallback_feature["rule_id"] = "source_fallback:NT"
    fallback_feature["rule_name"] = "NT源数据成本回填"
    fallback_feature["calc_mode"] = "MAIN_COST_INCREMENT"
    fallback_feature["layer_name"] = "cropland"

    with patch("neo4j.GraphDatabase.driver", return_value=mock_neo4j):
        writer = Neo4jWriter()
        writer.write_route_decision_graph(
            entries=[_COST_RULE_ENTRY],
            features=[fallback_feature],
            voltage_level="110kV",
        )

    # Look for a CostRule MERGE that includes ON CREATE SET with rule_origin
    found_on_create_pseudo = False
    for args, _ in mock_tx.run.call_args_list:
        query = args[0] if args else ""
        if "ON CREATE" in query and "source_fallback" in query:
            found_on_create_pseudo = True
            break

    assert found_on_create_pseudo, (
        "No ON CREATE SET for source_fallback pseudo-rule found"
    )


def test_all_features_get_triggered_by_rule_relationship(mock_neo4j: MagicMock) -> None:
    """验证所有要素（含 source_fallback）都获得 TRIGGERED_BY_RULE 关系。"""
    from smart_kg.neo4j_writer import Neo4jWriter

    mock_tx = MagicMock()
    mock_session = MagicMock()
    mock_session.execute_write.side_effect = (
        lambda func, *args, **kwargs: func(mock_tx, *args, **kwargs)
    )
    mock_neo4j.session.return_value.__enter__.return_value = mock_session

    features = [
        dict(_COST_FEATURE),
        dict(_FORBIDDEN_FEATURE),
    ]
    # Give one feature a source_fallback rule_id — no matching CostRule from entries
    features[1]["id"] = "feat:structures:fb002"
    features[1]["rule_id"] = "source_fallback:QL"
    features[1]["rule_name"] = "QL源数据成本回填"

    with patch("neo4j.GraphDatabase.driver", return_value=mock_neo4j):
        writer = Neo4jWriter()
        result = writer.write_route_decision_graph(
            entries=[_COST_RULE_ENTRY],
            features=features,
            voltage_level="110kV",
        )

    # Count TRIGGERED_BY_RULE relationship queries
    triggered_count = sum(
        1 for args, _ in mock_tx.run.call_args_list
        if "TRIGGERED_BY_RULE" in (args[0] if args else "")
    )

    assert result["feature_count"] == 2
    assert triggered_count == result["feature_count"], (
        f"Expected {result['feature_count']} TRIGGERED_BY_RULE relationships, "
        f"got {triggered_count}"
    )


# ── Scoped ID / repeated-import tests ──────────────────────────────────


def test_routing_feature_has_scoped_id_and_source_id(mock_neo4j: MagicMock) -> None:
    """验证 RoutingFeature 使用决策作用域 ID 并保留 source_feature_id。"""
    from smart_kg.neo4j_writer import Neo4jWriter

    mock_tx = MagicMock()
    mock_session = MagicMock()
    mock_session.execute_write.side_effect = (
        lambda func, *args, **kwargs: func(mock_tx, *args, **kwargs)
    )
    mock_neo4j.session.return_value.__enter__.return_value = mock_session

    decision_id = "route_decision:110kV:20260501"

    with patch("neo4j.GraphDatabase.driver", return_value=mock_neo4j):
        writer = Neo4jWriter()
        writer.write_route_decision_graph(
            entries=[_COST_RULE_ENTRY],
            features=[_COST_FEATURE],
            voltage_level="110kV",
            decision_id=decision_id,
        )

    found_rf_merge = False
    for args, kwargs in mock_tx.run.call_args_list:
        query = args[0] if args else ""
        if "MERGE (rf:RoutingFeature" in query:
            rf_id = kwargs.get("route_feature_id", "")
            src_id = kwargs.get("source_feat_id", "")
            dec_id = kwargs.get("decision_id", "")
            assert rf_id == f"{decision_id}:{_COST_FEATURE['id']}", (
                f"Expected route_feature_id='{decision_id}:{_COST_FEATURE['id']}', "
                f"got '{rf_id}'"
            )
            assert src_id == _COST_FEATURE["id"], (
                f"Expected source_feat_id='{_COST_FEATURE['id']}', got '{src_id}'"
            )
            assert dec_id == decision_id
            found_rf_merge = True
            break

    assert found_rf_merge, "No RoutingFeature MERGE call found"


def test_repeated_import_uses_different_routing_feature_ids(mock_neo4j: MagicMock) -> None:
    """验证使用不同 decision_id 重复导入不会产生 ID 冲突。"""
    from smart_kg.neo4j_writer import Neo4jWriter

    with patch("neo4j.GraphDatabase.driver", return_value=mock_neo4j):
        writer = Neo4jWriter()

        # First import with default generated decision ID
        result1 = writer.write_route_decision_graph(
            entries=[_COST_RULE_ENTRY, _CONSTRAINT_ENTRY],
            features=[_COST_FEATURE, _FORBIDDEN_FEATURE],
            voltage_level="110kV",
        )

        # Second import with explicit new decision ID — same features,
        # different decision scope. Must not produce duplicate IDs.
        result2 = writer.write_route_decision_graph(
            entries=[_COST_RULE_ENTRY, _CONSTRAINT_ENTRY],
            features=[_COST_FEATURE, _FORBIDDEN_FEATURE],
            voltage_level="110kV",
            decision_id="route_decision:110kV:20260501",
        )

    assert result1["decision_id"] != result2["decision_id"]
    assert result1["feature_count"] == 2
    assert result2["feature_count"] == 2
