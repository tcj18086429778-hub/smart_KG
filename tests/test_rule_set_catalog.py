"""规则集目录写入单元测试。

验证 write_rule_set_catalog() 的图结构创建、去重和读写往返一致性。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class TestWriteRuleSetCatalog:
    """write_rule_set_catalog() 写入行为测试。"""

    def test_write_rule_set_catalog_creates_correct_graph(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """验证写入后图谱包含 RuleSet、RasterSpec、CostRule 节点及全部关系。"""
        from smart_kg.neo4j_writer import Neo4jWriter

        # Collect Cypher statements for inspection
        tx_runs: list[dict] = []

        class _FakeTx:
            """模拟事务对象，收集执行过的 Cypher。"""

            def run(self, query, **params):
                """记录 Cypher 与参数。"""
                tx_runs.append({"query": query, "params": params})

        class _FakeSession:
            """模拟 Session，直接执行写事务函数。"""

            def execute_write(self, fn, *args, **kwargs):
                """将伪事务对象传入待测写入函数。"""
                # Pass a fake tx to the static method
                fn(_FakeTx(), *args, **kwargs)

            def __enter__(self):
                """支持上下文管理。"""
                return self

            def __exit__(self, *a):
                """结束上下文时不吞异常。"""
                return False

        fake_session = _FakeSession()

        class _FakeDriver:
            """模拟 Driver，返回固定 Session。"""

            def session(self, database=None):
                """返回伪造 Session。"""
                return fake_session

            def close(self):
                """模拟关闭连接。"""
                pass

        writer = Neo4jWriter.__new__(Neo4jWriter)
        writer.driver = _FakeDriver()
        writer.database = "neo4j"

        entries = [
            {
                "rule_id": "cost_rule:test:road",
                "rule_name": "Road Cost",
                "source_table": "test_table",
                "source_row": 1,
                "calc_mode": "MAIN_COST_INCREMENT",
                "effect_target": "ALL",
                "effect_value": 100.0,
                "effect_value_status": "NUMERIC",
                "effect_attr": "S_LTH",
                "effect_unit": "万元/公里",
                "voltage_level": "110kV",
                "priority": 300,
                "reason_code": 2001,
                "match_condition_json": {"field": "feature_type_code", "operator": "eq", "value": "3004"},
                "enabled": True,
            },
            {
                "rule_id": "constraint_rule:test:building",
                "rule_name": "Building Forbidden",
                "source_table": "test_table",
                "source_row": 2,
                "calc_mode": "FORBIDDEN",
                "effect_target": "ALL",
                "effect_value": None,
                "effect_value_status": "FORBIDDEN",
                "effect_attr": None,
                "effect_unit": None,
                "voltage_level": "110kV",
                "buffer_distance_m": 30.0,
                "avoidance_mode": "BUFFER",
                "priority": 1000,
                "reason_code": 1001,
                "match_condition_json": {"field": "feature_subtype_code", "operator": "eq", "value": "1002"},
                "enabled": True,
            },
        ]

        result = writer.write_rule_set_catalog(
            entries=entries,
            voltage_level="110kV",
            rule_set_version="v20260422",
            resolution=20.0,
            calculation_crs="EPSG:4547",
            base_cost=1.0,
            included_layers=["road", "river"],
            excluded_layers=["tower"],
            rule_set_id="ruleset:110kV:test",
            raster_spec_id="raster_spec:110kV:test",
        )

        assert result["rule_set_id"] == "ruleset:110kV:test"
        assert result["raster_spec_id"] == "raster_spec:110kV:test"
        assert result["rule_count"] == 2
        assert result["voltage_level"] == "110kV"
        assert result["rule_set_version"] == "v20260422"

        # Verify constraints were created
        constraint_queries = [r["query"] for r in tx_runs if "CONSTRAINT" in r["query"]]
        assert len(constraint_queries) == 4

        # Verify RuleSet node MERGE
        ruleset_queries = [r for r in tx_runs if "MERGE (rs:RuleSet" in r["query"]]
        assert len(ruleset_queries) == 1
        assert ruleset_queries[0]["params"]["props"]["id"] == "ruleset:110kV:test"
        assert ruleset_queries[0]["params"]["props"]["voltage_level"] == "110kV"
        assert ruleset_queries[0]["params"]["props"]["status"] == "active"

        # Verify CostRule CONTAINS_RULE relationships
        contains_queries = [r for r in tx_runs if "CONTAINS_RULE" in r["query"]]
        assert len(contains_queries) == 2

        # Verify RasterSpec node MERGE
        spec_queries = [r for r in tx_runs if "MERGE (spec:RasterSpec" in r["query"]]
        assert len(spec_queries) == 1
        assert spec_queries[0]["params"]["props"]["resolution"] == 20.0
        assert spec_queries[0]["params"]["props"]["calculation_crs"] == "EPSG:4547"

        # Verify USES_RASTER_SPEC relationship
        uses_spec = [r for r in tx_runs if "USES_RASTER_SPEC" in r["query"]]
        assert len(uses_spec) == 1

        # Verify INCLUDES_LAYER for both layers
        includes = [r for r in tx_runs if "INCLUDES_LAYER" in r["query"]]
        assert len(includes) == 2

        # Verify EXCLUDES_LAYER for tower
        excludes = [r for r in tx_runs if "EXCLUDES_LAYER" in r["query"]]
        assert len(excludes) == 1

    def test_write_rule_set_catalog_deduplicates_rules(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """验证相同 rule_id 的重复条目仅写入一次。"""
        from smart_kg.neo4j_writer import Neo4jWriter

        tx_runs: list[dict] = []

        class _FakeTx:
            """模拟事务对象，收集执行过的 Cypher。"""

            def run(self, query, **params):
                """记录 Cypher 与参数。"""
                tx_runs.append({"query": query, "params": params})

        class _FakeSession:
            """模拟写事务 Session。"""

            def execute_write(self, fn, *args, **kwargs):
                """执行传入的写事务函数。"""
                fn(_FakeTx(), *args, **kwargs)

            def __enter__(self):
                """支持上下文管理。"""
                return self

            def __exit__(self, *a):
                """结束上下文时不吞异常。"""
                return False

        class _FakeDriver:
            """模拟 Driver，按需创建 Session。"""

            def session(self, database=None):
                """返回新的伪造 Session。"""
                return _FakeSession()

            def close(self):
                """模拟关闭连接。"""
                pass

        writer = Neo4jWriter.__new__(Neo4jWriter)
        writer.driver = _FakeDriver()
        writer.database = "neo4j"

        # Two entries with the same rule_id should produce only 1 CONTAINS_RULE
        entries = [
            {
                "rule_id": "cost_rule:test:dup",
                "rule_name": "Duplicate Rule",
                "source_table": "test_table",
                "source_row": 1,
                "calc_mode": "MAIN_COST_INCREMENT",
                "effect_target": "ALL",
                "effect_value": 50.0,
                "effect_value_status": "NUMERIC",
                "reason_code": 2001,
                "match_condition_json": {},
                "enabled": True,
            },
            {
                "rule_id": "cost_rule:test:dup",
                "rule_name": "Duplicate Rule v2",
                "source_table": "test_table",
                "source_row": 2,
                "calc_mode": "MAIN_COST_INCREMENT",
                "effect_target": "ALL",
                "effect_value": 50.0,
                "effect_value_status": "NUMERIC",
                "reason_code": 2001,
                "match_condition_json": {},
                "enabled": True,
            },
        ]

        result = writer.write_rule_set_catalog(
            entries=entries,
            voltage_level="110kV",
            rule_set_id="ruleset:110kV:dedup",
            raster_spec_id="raster_spec:110kV:dedup",
        )

        assert result["rule_count"] == 1
        contains = [r for r in tx_runs if "CONTAINS_RULE" in r["query"]]
        assert len(contains) == 1


class TestRuleSetCatalogRoundTrip:
    """验证写入的 RuleSet 目录可被图谱读取侧正确解析和还原。"""

    def test_round_trip_catalog_and_read(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """端到端验证：写入 -> 模拟 Neo4j 读取 -> 还原为 GraphRuleBundle。"""
        from smart_kg.graph_rule_source import (
            GraphRasterSpec,
            GraphRuleBundle,
            load_graph_rule_bundle,
        )
        from smart_kg.neo4j_writer import Neo4jWriter

        entries = [
            {
                "rule_id": "cost_rule:test:river",
                "rule_name": "River Cost",
                "source_table": "river_table",
                "source_row": 5,
                "calc_mode": "MAIN_COST_INCREMENT",
                "effect_target": "ALL",
                "effect_value": 200.0,
                "effect_value_status": "NUMERIC",
                "effect_attr": "S_CNT",
                "voltage_level": "110kV",
                "priority": 200,
                "reason_code": 2005,
                "match_condition_json": {"field": "feature_subtype_code", "operator": "eq", "value": "5003"},
                "enabled": True,
            },
        ]

        # Phase 1: Write the catalog via Neo4jWriter
        tx_written: list[dict] = []
        call_order: list[str] = []

        class _WriteFakeTx:
            """模拟写事务，用于记录写入阶段生成的 Cypher。"""

            def run(self, query, **params):
                """标准化并保存 Cypher 语句。"""
                narrow = " ".join(query.split())
                tx_written.append({"query": narrow, "params": params})

        class _WriteFakeSession:
            """模拟写 Session，并记录 execute_write 调用顺序。"""

            def execute_write(self, fn, *args, **kwargs):
                """执行写事务函数并保留顺序。"""
                call_order.append(f"write:{fn.__name__}")
                fn(_WriteFakeTx(), *args, **kwargs)

            def __enter__(self):
                """支持上下文管理。"""
                return self

            def __exit__(self, *a):
                """结束上下文时不吞异常。"""
                return False

        class _WriteFakeDriver:
            """模拟 Driver，返回写 Session。"""

            def session(self, database=None):
                """返回写入阶段使用的伪造 Session。"""
                return _WriteFakeSession()

            def close(self):
                """模拟关闭连接。"""
                pass

        writer = Neo4jWriter.__new__(Neo4jWriter)
        writer.driver = _WriteFakeDriver()
        writer.database = "neo4j"

        writer.write_rule_set_catalog(
            entries=entries,
            voltage_level="110kV",
            rule_set_version="v1",
            resolution=25.0,
            calculation_crs="EPSG:4547",
            base_cost=1.5,
            included_layers=["water", "road"],
            excluded_layers=["tower_site"],
            rule_set_id="ruleset:110kV:v1",
            raster_spec_id="raster_spec:110kV:v1",
        )

        # Phase 2: Read back via graph_rule_source (simulated Neo4j)
        # Build a fake Neo4j that returns data matching what we wrote.

        def fake_read_session():
            """返回读取阶段用的伪造 Session。"""
            return _ReadFakeSession()

        class _ReadFakeResult:
            """模拟读取结果对象。"""

            def __init__(self, rows):
                """保存预置结果行。"""
                self._rows = rows

            def single(self):
                """返回首条结果。"""
                return self._rows[0] if self._rows else None

            def __iter__(self):
                """支持结果对象遍历。"""
                return iter(self._rows)

        class _ReadFakeSession:
            """模拟读取 Session，根据查询返回预设数据。"""

            def run(self, query, **params):
                """按查询类型返回对应的伪造结果。"""
                compact = " ".join(query.split())
                if "MATCH (rs:RuleSet)" in compact:
                    return _ReadFakeResult([
                        {
                            "props": {
                                "id": "ruleset:110kV:v1",
                                "rule_set_version": "v1",
                                "voltage_level": "110kV",
                                "status": "active",
                                "generated_at": "2026-05-13T00:00:00Z",
                            }
                        }
                    ])
                if "CONTAINS_RULE" in compact:
                    return _ReadFakeResult([
                        {
                            "props": {
                                "id": "cost_rule:test:river",
                                "rule_name": "River Cost",
                                "source_table": "river_table",
                                "source_row": 5,
                                "calc_mode": "MAIN_COST_INCREMENT",
                                "effect_target": "ALL",
                                "effect_value": 200.0,
                                "effect_value_status": "NUMERIC",
                                "effect_attr": "S_CNT",
                                "voltage_level": "110kV",
                                "priority": 200,
                                "reason_code": 2005,
                                "match_condition_json": '{"field":"feature_subtype_code","operator":"eq","value":"5003"}',
                                "enabled": True,
                            }
                        }
                    ])
                if "USES_RASTER_SPEC" in compact:
                    return _ReadFakeResult([
                        {
                            "props": {
                                "id": "raster_spec:110kV:v1",
                                "resolution": 25.0,
                                "base_cost": 1.5,
                                "calculation_crs": "EPSG:4547",
                            },
                            "included_layers": ["water", "road"],
                            "excluded_layers": ["tower_site"],
                        }
                    ])
                raise AssertionError(f"Unexpected query: {query}")

            def __enter__(self):
                """支持上下文管理。"""
                return self

            def __exit__(self, *a):
                """结束上下文时不吞异常。"""
                return False

        class _ReadFakeDriver:
            """模拟读取 Driver。"""

            def session(self, database=None):
                """返回读取阶段使用的伪造 Session。"""
                return _ReadFakeSession()

            def __enter__(self):
                """支持上下文管理。"""
                return self

            def __exit__(self, *a):
                """结束上下文时不吞异常。"""
                return False

        with patch("neo4j.GraphDatabase.driver", return_value=_ReadFakeDriver()):
            bundle = load_graph_rule_bundle(voltage_level="110kV")

        assert bundle.rule_set_id == "ruleset:110kV:v1"
        assert bundle.rule_set_version == "v1"
        assert bundle.raster_spec.source == "ruleset"
        assert bundle.raster_spec.spec_id == "raster_spec:110kV:v1"
        assert bundle.raster_spec.resolution == 25.0
        assert bundle.raster_spec.base_cost == 1.5
        assert bundle.raster_spec.calculation_crs == "EPSG:4547"
        assert bundle.raster_spec.included_layers == ["water", "road"]
        assert bundle.raster_spec.excluded_layers == ["tower_site"]
        assert len(bundle.rules) == 1
        assert bundle.rules[0].rule_id == "cost_rule:test:river"
