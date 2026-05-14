# smart_KG

电塔选址知识图谱与成本栅格系统。当前主链路：

- 从 Excel 分类体系提取标准化成本规则（`standardize-cost-rules`）
- 对 GPKG 执行规则匹配与成本字段填充（`cost-gpkg` / `cost-gpkg-from-graph`）
- 将成本化 GPKG 栅格化为成本面（`build-cost-raster` / `build-cost-raster-from-graph`）
- 将规则集和栅格参数写入 Neo4j 图谱目录（`import-rule-set-neo4j` / `import-cost-rules-neo4j`）
- 图谱驱动全链路管道（`run-route-pipeline-from-graph`）

## 1. 环境安装

推荐使用 Anaconda 创建独立环境：

```powershell
cd "C:\Users\11215\Desktop\知识图谱项目\smart_KG"
D:\Anaconda\Scripts\conda.exe create --prefix "D:\conda-envs\smart_kg" python=3.12 pip -y
D:\conda-envs\smart_kg\python.exe -m pip install -e .[dev]
```

激活环境：

```powershell
conda activate "D:\conda-envs\smart_kg"
```

也可以使用 venv：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[dev]
```

## 2. 主链路工作流

### 2.1 从 Excel 提取成本规则

```powershell
smart-kg standardize-cost-rules --excel "选线数据分类体系20260422.xlsx" --out data/standardized/cost_rules_20260422.json
```

### 2.2 GPKG 成本化

文件驱动：

```powershell
smart-kg cost-gpkg --source-gpkg 上海_source.gpkg --out-gpkg data/standardized/上海_costed.gpkg --voltage-level 110kV --rules data/standardized/cost_rules_20260422.json
```

图谱驱动（从 Neo4j 读取活跃规则集）：

```powershell
smart-kg cost-gpkg-from-graph --source-gpkg 上海_source.gpkg --out-gpkg data/standardized/上海_costed.gpkg --voltage-level 110kV
```

### 2.3 构建成本栅格

文件驱动：

```powershell
smart-kg build-cost-raster --gpkg data/standardized/上海_costed.gpkg --out-dir exports/raster_route_eval/ --voltage-level 110kV --resolution 20
```

图谱驱动：

```powershell
smart-kg build-cost-raster-from-graph --gpkg data/standardized/上海_costed.gpkg --out-dir exports/raster_route_eval/ --voltage-level 110kV
```

### 2.4 导入 Neo4j 图谱

导入 RuleSet 目录（推荐）：

```powershell
smart-kg import-rule-set-neo4j \
  --rules data/standardized/cost_rules_20260422.json \
  --voltage-level 110kV \
  --rule-set-version 20260422 \
  --resolution 20.0 \
  --calculation-crs EPSG:4547 \
  --base-cost 1.0 \
  --included-layers "building,road,water,landuse" \
  --excluded-layers "tower"
```

导入走线决策图谱：

```powershell
smart-kg import-cost-rules-neo4j \
  --rules data/standardized/cost_rules_20260422.json \
  --gpkg data/standardized/上海_costed.gpkg \
  --voltage-level 110kV \
  --metadata exports/raster_route_eval/metadata.json
```

查看 RuleSet 目录：

```powershell
smart-kg list-rule-sets
smart-kg list-rule-sets --voltage-level 110kV
```

### 2.5 图谱驱动全链路

一条命令完成 GPKG 成本化 + 栅格构建：

```powershell
smart-kg run-route-pipeline-from-graph \
  --source-gpkg 上海_source.gpkg \
  --out-gpkg data/standardized/上海_costed.gpkg \
  --out-dir exports/raster_route_eval/ \
  --voltage-level 110kV
```

## 3. API 服务

```powershell
smart-kg serve --host 127.0.0.1 --port 8000
```

可用端点：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| POST | `/cost-rules/standardize` | 成本规则标准化 |
| POST | `/gpkg/cost-fields` | GPKG 成本字段补齐 |
| POST | `/raster/cost-surface` | 成本栅格构建 |

## 4. Neo4j 配置

在项目根目录创建 `.env` 文件：

```text
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=你的密码
NEO4J_DATABASE=neo4j
```

## 5. 清理旧版图谱

如果 Neo4j 中存在旧版 `write_all` 工作流产生的节点（TowerSite、LineSegment、GeoFeature、Rule、Condition 等），可以使用维护命令清理：

```powershell
# 先预览将要删除的节点数量
smart-kg cleanup-legacy-neo4j --dry-run

# 确认后执行删除
smart-kg cleanup-legacy-neo4j
```

此命令不会触碰当前主链路使用的 RuleSet、RouteDecision、RasterSpec、CostRule、RoutingFeature、RoutingLayer、CostSurface 节点。

## 6. 图谱结构

### RuleSet 目录层

| 节点 | 含义 |
|------|------|
| `RuleSet` | 版本化规则集目录 |
| `CostRule` | 单条成本规则 |
| `RasterSpec` | 栅格执行参数 |
| `RoutingLayer` | 走线图层 |

关系：`CONTAINS_RULE`、`USES_RASTER_SPEC`、`INCLUDES_LAYER`、`EXCLUDES_LAYER`

### 走线决策图谱

| 节点 | 含义 |
|------|------|
| `RouteDecision` | 一次走线决策 |
| `CostRule` | 成本规则 |
| `RoutingFeature` | 成本化 GPKG 要素 |
| `CostSurface` | 栅格成本面元数据 |
| `RoutingLayer` | 走线图层 |

关系：`HAS_ROUTING_FEATURE`、`TRIGGERED_BY_RULE`、`GENERATES_COST_SURFACE`、`INCLUDES_LAYER`、`EXCLUDES_LAYER`

## 7. 项目结构

```text
smart_KG/
├── configs/                    规则 schema、字段映射
├── data/
│   ├── raw/                    样例数据
│   ├── standardized/           Excel 清洗结果、成本规则 JSON
│   └── ...
├── docs/                       设计说明
├── exports/                    栅格输出
├── src/smart_kg/               Python 代码
│   ├── cost_rule_loader.py     Excel → 标准化成本规则
│   ├── gpkg_standardizer.py    GPKG 成本字段填充
│   ├── raster_executor.py      成本栅格构建
│   ├── graph_rule_source.py    Neo4j 图谱驱动规则读取
│   ├── neo4j_writer.py         图谱写入（RouteDecision / RuleSet）
│   ├── cli.py                  命令行入口
│   └── api.py                  FastAPI 服务
└── tests/                      单元测试
```
