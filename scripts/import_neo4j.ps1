Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

if (-not $env:NEO4J_URI) { $env:NEO4J_URI = "bolt://localhost:7687" }
if (-not $env:NEO4J_USERNAME) { $env:NEO4J_USERNAME = "neo4j" }
if (-not $env:NEO4J_DATABASE) { $env:NEO4J_DATABASE = "neo4j" }
if (-not $env:NEO4J_PASSWORD) {
  throw "Please set NEO4J_PASSWORD before running this script, for example: `$env:NEO4J_PASSWORD='your-password'"
}

& "D:\conda-envs\smart_kg\python.exe" -m smart_kg.cli import-neo4j
