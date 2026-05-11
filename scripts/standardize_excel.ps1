Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot
& "D:\conda-envs\smart_kg\python.exe" -m smart_kg.cli standardize-excel `
  --excel "C:\Users\11215\Desktop\知识图谱项目\配置与示例GPKG.xlsx" `
  --out "data\standardized\rules_from_excel.json"
