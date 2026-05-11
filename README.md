# smart_KG

电塔选址统一知识图谱 MVP。当前版本先解决四件事：

- 接收已经算好的空间关系 `spatial_relations.json`。
- 用结构化 `match_condition_json` 匹配地理要素和空间关系。
- 输出塔位、线路段触发的硬约束、成本规则和解释链路。
- 可选写入 Neo4j，未安装 Neo4j 时也能本地跑通 demo 和 API。

## 1. 环境安装

本机已验证的独立环境路径：

```text
D:\conda-envs\smart_kg
```

推荐使用 D 盘现有 Anaconda 创建独立沙箱：

```powershell
cd "C:\Users\11215\Desktop\知识图谱项目\smart_KG"
D:\Anaconda\Scripts\conda.exe create --prefix "D:\conda-envs\smart_kg" python=3.12 pip -y
D:\conda-envs\smart_kg\python.exe -m pip install -e .[dev]
```

激活环境：

```powershell
conda activate "D:\conda-envs\smart_kg"
```

也可以使用 `environment.yml` 创建默认命名环境：

```powershell
cd "C:\Users\11215\Desktop\知识图谱项目\smart_KG"
conda env create -f environment.yml
conda activate smart_kg
```

如果你使用 mamba：

```powershell
cd "C:\Users\11215\Desktop\知识图谱项目\smart_KG"
mamba env create -f environment.yml
conda activate smart_kg
```

如果 conda-forge 或 defaults 下载不稳定，可以用更保守的两步方式：

```powershell
cd "C:\Users\11215\Desktop\知识图谱项目\smart_KG"
D:\Anaconda\Scripts\conda.exe create --prefix "D:\conda-envs\smart_kg" python=3.12 pip -y
D:\conda-envs\smart_kg\python.exe -m pip install -e .[dev]
```

如果本机暂时没有 conda/mamba，也可以用 Python venv：

```powershell
cd "C:\Users\11215\Desktop\知识图谱项目\smart_KG"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -U pip
pip install -e .[dev]
```

## 2. 运行 demo

```powershell
smart-kg demo
```

未激活环境时可直接执行：

```powershell
D:\conda-envs\smart_kg\python.exe -m smart_kg.cli demo
```

该命令会读取：

- `configs/rules.json`
- `data/raw/sample_geo_features.csv`
- `data/raw/sample_tower_sites.csv`
- `data/raw/sample_line_segments.csv`
- `data/spatial_relations/sample_spatial_relations.json`

并生成：

- `reports/demo_result.json`

## 3. 启动 API

```powershell
smart-kg serve --host 127.0.0.1 --port 8000
```

未激活环境时可直接执行：

```powershell
D:\conda-envs\smart_kg\python.exe -m smart_kg.cli serve --host 127.0.0.1 --port 8000
```

打开：

- `http://127.0.0.1:8000/health`
- `http://127.0.0.1:8000/evaluate`
- `http://127.0.0.1:8000/explain/line-segment/line_segment:demo:A:B`
- `http://127.0.0.1:8000/explain/tower-site/tower_site:demo:A`

## 4. 从 Excel 生成标准规则

当前 Excel 作为原始配置源，系统会把以下规则表清洗为结构化规则 JSON：

- `BASE规则配置表`
- `交跨成本对应表`
- `地质气象条件成本对应表`
- `高程成本对应表`

按当前文件内容，清洗后会生成 105 条规则。

```powershell
smart-kg standardize-excel --excel "C:\Users\11215\Desktop\知识图谱项目\配置与示例GPKG.xlsx" --out data\standardized\rules_from_excel.json
```

未激活环境时可直接执行：

```powershell
D:\conda-envs\smart_kg\python.exe -m smart_kg.cli standardize-excel --excel "C:\Users\11215\Desktop\知识图谱项目\配置与示例GPKG.xlsx" --out data\standardized\rules_from_excel.json
```

说明：

- `match_condition_raw` 保留 Excel 原始条件，便于追溯。
- `match_condition_json` 是系统实际执行的结构化条件。
- Excel 中 `ALL/TOWER/LINE` 会统一为 `BOTH/TOWER_SITE/LINE_SEGMENT`。
- Excel 中 `S_TYPE_CODE/S_SUB_TYPE_CODE/S_LEVEL` 会统一映射到 `feature_type_code/feature_subtype_code/feature_level`。
- `交跨成本对应表` 中不完整行和 `/` 成本行不会作为有效规则导入。
- `?` 和 `-1` 会保留为 `NEGOTIABLE`，表示后续需要具体商议。

## 5. 可选：导入 Neo4j

本机已验证 Neo4j 安装路径：

```text
D:\Neo4j\neo4j-community-5.26.23
```

如果 Neo4j 没有作为 Windows 服务运行，可以这样启动：

```powershell
cd "D:\Neo4j\neo4j-community-5.26.23"
.\bin\neo4j.bat console
```

先复制环境变量：

```powershell
copy .env.example .env
notepad .env
```

确认 Neo4j 已启动，并设置：

```text
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=你的密码
NEO4J_DATABASE=neo4j
```

然后执行：

```powershell
smart-kg import-neo4j
```

未激活环境时可直接执行：

```powershell
$env:NEO4J_URI="bolt://localhost:7687"
$env:NEO4J_USERNAME="neo4j"
$env:NEO4J_PASSWORD="你的密码"
$env:NEO4J_DATABASE="neo4j"
D:\conda-envs\smart_kg\python.exe -m smart_kg.cli import-neo4j
```

或使用脚本：

```powershell
$env:NEO4J_PASSWORD="你的密码"
powershell -ExecutionPolicy Bypass -File .\scripts\import_neo4j.ps1
```

Neo4j 不是运行 demo 的前置条件。当前阶段建议先用 demo 和 API 验证规则模型，再接入数据库。

## 6. 项目结构

```text
smart_KG/
├── configs/                    规则、schema、字段映射
├── data/
│   ├── raw/                    样例塔位、线路、地理要素
│   ├── spatial_relations/      GIS 预处理输出
│   ├── standardized/           Excel 清洗结果
│   ├── validated/              校验通过的数据
│   └── rejected/               校验失败的数据
├── docs/                       设计说明
├── reports/                    demo 和评估报告
├── src/smart_kg/               Python 代码
└── tests/                      单元测试
```

## 7. 设计边界

当前版本不做几何计算，不读取 GPKG 几何，不做候选点排序。GIS/空间预处理模块只需要按 `configs/spatial_relation_schema.json` 输出关系文件，smart_KG 负责规则触发、成本解释和图谱写入。
