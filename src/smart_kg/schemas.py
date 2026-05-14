"""核心数据模型（已清理）。

旧版通用图谱模型（GeoFeature、TowerSite、LineSegment、SpatialRelation、Rule、
RuleTrigger、EvaluationReport、CalculationRun、RulePackage、CostSurface、
RouteDecision 等）已随 write_all 工作流一并移除。

当前主链路的数据模型定义在各自模块中：
- CostRuleEntry: cost_rule_loader.py
- GraphRuleBundle / RasterSpec 查询结果: graph_rule_source.py
"""
